"""Bank Statement Categoriser — drop a statement in, get a categorised dashboard.

    streamlit run app.py

Everything runs locally. The only thing that ever leaves the machine is a list of
unrecognised merchant name strings, sent to Claude or OpenAI to be categorised — and only
when ANTHROPIC_API_KEY or OPENAI_API_KEY is set. No amounts, balances, or account numbers
are sent.
"""

from __future__ import annotations

import hmac
import io
import os
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from dateutil.relativedelta import relativedelta

from bankcat import insights, theme
from bankcat.categorize import (
    CATEGORIES,
    NOT_SPENDING,
    UNCATEGORISED,
    Categorizer,
    Store,
    all_categories,
    coverage,
    decided_by_labels,
    needs_review,
)
from bankcat.llm import active_model, active_provider, is_configured, provider_display_name
from bankcat.parsers import (
    StatementParseError,
    StatementPasswordError,
    combine,
    load_transactions,
)

st.set_page_config(
    page_title="Statement Categoriser",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    f"""
    <style>
      .block-container {{ padding-top: 2.2rem; max-width: 1400px; }}
      .kpi {{
        background: {theme.SURFACE};
        border: 1px solid rgba(11,11,11,0.10);
        border-radius: 10px; padding: 14px 16px; height: 100%;
      }}
      .kpi-label {{
        font-size: 11px; letter-spacing: .06em; text-transform: uppercase;
        color: {theme.MUTED}; margin-bottom: 6px;
      }}
      .kpi-value {{ font-size: 26px; font-weight: 600; color: {theme.INK}; line-height: 1.15; }}
      .kpi-note {{ font-size: 12px; color: {theme.INK_SECONDARY}; margin-top: 5px; }}
      .badge {{
        display: inline-block; padding: 4px 11px; border-radius: 999px;
        font-size: 12px; font-weight: 600;
      }}
      .badge-ok   {{ background: rgba(12,163,12,.12);  color: #076b07; }}
      .badge-warn {{ background: rgba(250,178,25,.18); color: #7a5300; }}
      .badge-info {{ background: rgba(42,120,214,.12); color: #1c5cab; }}
      h3 {{ margin-top: .4rem; }}
    </style>
    """,
    unsafe_allow_html=True,
)


# --------------------------------------------------------------------------------------
# Password gate
# --------------------------------------------------------------------------------------

def check_password() -> bool:
    """Gate the whole app behind a single shared password held in st.secrets."""

    def password_entered() -> None:
        if hmac.compare_digest(st.session_state["password"], st.secrets["app_password"]):
            st.session_state["password_correct"] = True
            del st.session_state["password"]
        else:
            st.session_state["password_correct"] = False

    if st.session_state.get("password_correct"):
        return True

    st.title("Bank Statement Categoriser")
    st.text_input("Password", type="password", on_change=password_entered, key="password")
    if "password_correct" in st.session_state:
        st.error("Incorrect password")
    return False


if not check_password():
    st.stop()


# --------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------

@st.cache_data(show_spinner=False)
def parse_files(payloads: tuple[tuple[str, bytes], ...], password: str):
    """Parse uploaded statements. Cached on file bytes so edits never re-parse."""
    frames, reports, errors = [], [], []
    for name, data in payloads:
        try:
            frame, report = load_transactions(io.BytesIO(data), password=password or None,
                                              filename=name)
            frames.append(frame)
            reports.append(report)
        except StatementPasswordError as error:
            errors.append(("password", name, str(error)))
        except StatementParseError as error:
            errors.append(("parse", name, str(error)))
        except Exception as error:  # noqa: BLE001 — surfaced to the user, not swallowed
            errors.append(("parse", name, f"{type(error).__name__}: {error}"))
    return combine(frames), reports, errors


def categorise(frame: pd.DataFrame, use_llm: bool) -> pd.DataFrame:
    store = Store()
    categorizer = Categorizer(store=store)
    return categorizer.categorize(frame, use_llm=use_llm)


def refresh() -> None:
    """Re-run categorisation against the current overrides and cache."""
    if st.session_state.get("raw") is not None:
        st.session_state["data"] = categorise(
            st.session_state["raw"], st.session_state.get("used_llm", False)
        )


# --------------------------------------------------------------------------------------
# Sidebar
# --------------------------------------------------------------------------------------

store = Store()

with st.sidebar:
    st.markdown("### Settings")

    include_transfers = st.toggle(
        "Count transfers as spending", value=False,
        help="Off by default. Transfers between your own accounts, credit-card bill "
             "payments, and SIP investments are money that moved, not money you spent. "
             "Counting them roughly doubles an apparent burn rate.",
    )

    llm_available = is_configured()
    llm_provider = active_provider()
    use_llm = st.toggle(
        f"Ask {provider_display_name(llm_provider)} about unknown merchants",
        value=llm_available,
        disabled=not llm_available,
        help=f"Uses {active_model() or 'an LLM'} for merchants the rule dictionary does "
             "not recognise, then remembers each answer forever. Only merchant name "
             "strings are sent — never amounts, balances, or account numbers.",
    )
    if not llm_available:
        st.caption(
            "No `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` found, so this stays off. "
            "Everything else works offline; unknown merchants go to the review list "
            "instead."
        )

    st.divider()
    st.markdown("### What it has learned")
    st.caption(
        f"{len(store.overrides)} merchant(s) you tagged · {len(store.cache)} learned "
        "from the AI layer. Both persist in `data/` and make each new statement faster."
    )
    left, right = st.columns(2)
    if left.button("Clear my tags", width="stretch"):
        store.clear_overrides()
        refresh()
        st.rerun()
    if right.button("Clear learned", width="stretch"):
        store.clear_cache()
        refresh()
        st.rerun()


# --------------------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------------------

st.title("Bank Statement Categoriser")
st.caption(
    "Drop a statement from any Indian bank — PDF, CSV, or Excel. It parses the "
    "transactions, checks them against the statement's own running balance, and "
    "categorises every one."
)

uploads = st.file_uploader(
    "Drop your statement here",
    type=["pdf", "csv", "xlsx", "xls", "xlsm", "txt"],
    accept_multiple_files=True,
    help="Several files are fine — overlapping months are de-duplicated automatically.",
)

password = st.text_input(
    "PDF password (only if your statement is protected)", type="password",
    placeholder="Often PAN + date of birth, or name + date of birth",
)

SAMPLE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "samples", "hdfc_sample.csv")

if uploads:
    st.session_state.pop("demo", None)
    payloads = tuple((upload.name, upload.getvalue()) for upload in uploads)
elif st.session_state.get("demo") and os.path.exists(SAMPLE_PATH):
    with open(SAMPLE_PATH, "rb") as handle:
        payloads = (("hdfc_sample.csv", handle.read()),)
    st.caption(
        "Showing a generated sample statement — three months of realistic Indian "
        "transactions. Drop your own file above to replace it."
    )
else:
    st.info("**No statement loaded yet.** Drop a file above to begin.")
    if os.path.exists(SAMPLE_PATH):
        if st.button("Or try it with a sample statement", type="primary"):
            st.session_state["demo"] = True
            st.rerun()
    else:
        st.caption(
            "To see it work without your own data, run `python samples/make_sample.py` "
            "— it generates realistic HDFC, ICICI, and SBI statements with known totals."
        )
    st.stop()

signature = tuple(name for name, _ in payloads)

with st.spinner("Reading the statement…"):
    raw, reports, errors = parse_files(payloads, password)

for kind, name, message in errors:
    if kind == "password":
        st.error(f"**{name}** — {message}")
    else:
        st.error(f"**{name}** — {message}")

if raw.empty:
    st.warning("No transactions could be read. Check the password, or the file format.")
    st.stop()

# Re-categorise when the file set or the LLM toggle changes; otherwise keep the frame in
# session so tagging a merchant does not re-parse the PDF.
if (st.session_state.get("signature") != signature
        or st.session_state.get("used_llm") != use_llm):
    if st.session_state.get("signature") != signature:
        # A range chosen for the previous statement can filter the new one down to
        # nothing, so a new file starts on the full span again.
        for stale in ("period_preset", "period_start", "period_end"):
            st.session_state.pop(stale, None)
    st.session_state["signature"] = signature
    st.session_state["raw"] = raw
    st.session_state["used_llm"] = use_llm
    with st.spinner("Categorising…"):
        st.session_state["data"] = categorise(raw, use_llm)

data: pd.DataFrame = st.session_state["data"]


# --------------------------------------------------------------------------------------
# Trust bar — did the parse actually work?
# --------------------------------------------------------------------------------------

stats = coverage(data)
total_rows = sum(report.rows_total for report in reports)
reconciled_rows = sum(report.rows_reconciled for report in reports)
all_reconciled = all(report.reconciled for report in reports) and bool(reports)
any_balance = any(report.has_balance for report in reports)

badge_left, badge_right = st.columns([2, 3])
with badge_left:
    if all_reconciled:
        st.markdown(
            f'<span class="badge badge-ok">✓ Reconciled {reconciled_rows}/{total_rows} rows'
            "</span>", unsafe_allow_html=True,
        )
        st.caption(
            "Every row's balance movement matches its debit and credit, so the numbers "
            "below are the statement's own."
        )
    elif any_balance:
        st.markdown(
            f'<span class="badge badge-warn">⚠ Reconciled {reconciled_rows}/{total_rows} '
            "rows</span>", unsafe_allow_html=True,
        )
        st.caption(
            "Some rows do not add up against the running balance — treat those totals "
            "with care. The unreconciled rows are listed at the bottom of this page."
        )
    else:
        st.markdown('<span class="badge badge-info">No balance column to check against'
                    "</span>", unsafe_allow_html=True)
        st.caption("This statement has no running balance, so the parse cannot be verified.")

with badge_right:
    rate = stats["value_rate"]
    tone = "badge-ok" if rate >= 0.9 else "badge-warn"
    st.markdown(
        f'<span class="badge {tone}">{rate:.0%} of value categorised</span>',
        unsafe_allow_html=True,
    )
    st.caption(
        f"{stats['rows_categorised']} of {stats['rows']} transactions. Coverage is "
        "measured by value, not row count — the rows that matter are the big ones."
    )

st.divider()


# --------------------------------------------------------------------------------------
# Period — one range control that every number below it respects
# --------------------------------------------------------------------------------------

span_start, span_end = insights.statement_span(data)
span_days = (span_end - span_start).days + 1

# Presets end at the statement's last transaction, not at today: a statement from last
# year would otherwise resolve "Last month" to an empty range.
PRESETS = {"Last month": 1, "Last 3 months": 3, "Last 6 months": 6}

choices = ["Full statement"]
choices += [label for label, months in PRESETS.items()
            if span_days > months * insights.DAYS_PER_MONTH]
choices.append("Custom")

preset = st.segmented_control(
    "Period", choices, default="Full statement", key="period_preset",
) or "Full statement"

if preset == "Custom":
    from_column, to_column, _ = st.columns([1, 1, 3])
    start = from_column.date_input(
        "From", value=span_start, min_value=span_start, max_value=span_end,
        key="period_start", format="DD/MM/YYYY",
    )
    end = to_column.date_input(
        "To", value=span_end, min_value=span_start, max_value=span_end,
        key="period_end", format="DD/MM/YYYY",
    )
    if start > end:
        start, end = end, start
elif preset in PRESETS:
    start = max(span_end - relativedelta(months=PRESETS[preset]) + timedelta(days=1),
                span_start)
    end = span_end
else:
    start, end = span_start, span_end

view = insights.filter_period(data, start, end)
st.caption(
    f"**{start:%d %b %Y} → {end:%d %b %Y}** · showing {len(view)} of {len(data)} "
    "transactions"
)

st.divider()


# --------------------------------------------------------------------------------------
# Period summary
# --------------------------------------------------------------------------------------

def tile(column, label: str, value: str, note: str = "") -> None:
    column.markdown(
        f'<div class="kpi"><div class="kpi-label">{label}</div>'
        f'<div class="kpi-value">{value}</div>'
        f'<div class="kpi-note">{note}</div></div>',
        unsafe_allow_html=True,
    )


if view.empty:
    st.warning(
        f"**No transactions between {start:%d %b %Y} and {end:%d %b %Y}.** "
        "Widen the period above to see something."
    )
    st.stop()

summary = insights.period_summary(data, start, end, include_transfers)

columns = st.columns(4)
tile(columns[0], "Money in", theme.rupees(summary["money_in"]),
     f"{summary['credits']} credit(s)")
tile(columns[1], "Money out", theme.rupees(summary["money_out"]),
     "including transfers" if include_transfers
     else f"{theme.rupees(summary['excluded'])} excluded as transfers")
net_note = "saved" if summary["net"] >= 0 else "overspent"
tile(columns[2], "Net", theme.rupees(summary["net"]), net_note)
tile(columns[3], "Average monthly spend", theme.rupees(summary["monthly_burn"]),
     f"over {summary['days']} day(s)")

notes = []
if summary["opening_balance"] is not None:
    arrow = "▲" if summary["balance_change"] >= 0 else "▼"
    notes.append(
        f"Balance **{theme.rupees(summary['opening_balance'])} → "
        f"{theme.rupees(summary['closing_balance'])}** {arrow} "
        f"{theme.rupees(abs(summary['balance_change']))}"
    )
largest = summary["largest"]
if largest:
    notes.append(
        f"Largest single expense **{theme.rupees(largest['amount'])}** · "
        f"{largest['merchant']} ({largest['category']})"
    )
if notes:
    st.caption("  ·  ".join(notes))

st.divider()


# --------------------------------------------------------------------------------------
# Charts
# --------------------------------------------------------------------------------------

category_totals = insights.by_category(view, include_transfers)

with st.expander("Charts and trends", expanded=False):
    left, right = st.columns([1, 1])

    with left:
        st.markdown("### Where the money went")
        if category_totals.empty:
            st.caption("No spending in range.")
        else:
            chart = category_totals.iloc[::-1]  # largest at the top of a horizontal bar
            figure = go.Figure(go.Bar(
                x=chart["amount"], y=chart["category"], orientation="h",
                marker=dict(color=theme.PRIMARY, line=dict(width=0)),
                text=[theme.compact(v) for v in chart["amount"]],
                textposition="outside", textfont=dict(size=11, color=theme.INK_SECONDARY),
                cliponaxis=False,
                customdata=chart[["transactions", "share"]].to_numpy(),
                hovertemplate="<b>%{y}</b><br>%{customdata[0]} transactions"
                              "<br>%{customdata[1]:.1%} of spending<extra></extra>",
            ))
            # One measure, one colour: bar length carries the magnitude, so hue would only
            # add noise. Direct labels satisfy the contrast-relief rule.
            theme.style(figure, height=max(300, 30 * len(chart) + 40))
            figure.update_xaxes(showgrid=True, gridcolor=theme.GRID, showline=False,
                                tickformat="~s", tickprefix="₹")
            figure.update_yaxes(showgrid=False, tickprefix="", tickfont=dict(
                color=theme.INK_SECONDARY, size=12))
            st.plotly_chart(figure, width="stretch",
                            config=theme.PLOTLY_CONFIG)

    with right:
        st.markdown("### Month by month")
        pivot, order = insights.by_month(view, include_transfers)
        if pivot.empty:
            st.caption("No spending in range.")
        else:
            colors = theme.series_colors(len(order))
            figure = go.Figure()
            for index, category in enumerate(order):
                figure.add_bar(
                    x=pivot.index, y=pivot[category], name=category,
                    marker=dict(color=colors[index],
                                line=dict(color=theme.SURFACE, width=2)),
                    hovertemplate=f"<b>{category}</b><br>%{{x|%b %Y}}"
                                  "<br>₹%{y:,.0f}<extra></extra>",
                )
            figure.update_layout(barmode="stack")
            theme.style(figure, height=max(300, 30 * len(category_totals) + 40),
                        show_legend=True)
            figure.update_xaxes(tickformat="%b %Y", showgrid=False)
            figure.update_yaxes(tickformat="~s")
            st.plotly_chart(figure, width="stretch",
                            config=theme.PLOTLY_CONFIG)
            if len(category_totals) > len(order) - (1 if "Other" in order else 0):
                st.caption(
                    "Categories past the top seven are grouped as “Other” — beyond eight, "
                    "colours stop being reliably distinguishable."
                )

    left, right = st.columns([1, 1])

    with left:
        st.markdown("### Top merchants")
        merchants = insights.top_merchants(view, 15, include_transfers)
        if merchants.empty:
            st.caption("No spending in range.")
        else:
            chart = merchants.iloc[::-1]
            figure = go.Figure(go.Bar(
                x=chart["amount"], y=chart["merchant"], orientation="h",
                marker=dict(color=theme.PRIMARY_SOFT, line=dict(width=0)),
                text=[theme.compact(v) for v in chart["amount"]],
                textposition="outside", textfont=dict(size=11, color=theme.INK_SECONDARY),
                cliponaxis=False,
                customdata=chart[["transactions", "category"]].to_numpy(),
                hovertemplate="<b>%{y}</b><br>%{customdata[1]}"
                              "<br>%{customdata[0]} transactions<extra></extra>",
            ))
            theme.style(figure, height=max(300, 26 * len(chart) + 40))
            figure.update_xaxes(showgrid=True, gridcolor=theme.GRID, showline=False,
                                tickformat="~s", tickprefix="₹")
            figure.update_yaxes(showgrid=False, tickprefix="", tickfont=dict(
                color=theme.INK_SECONDARY, size=12))
            st.plotly_chart(figure, width="stretch",
                            config=theme.PLOTLY_CONFIG)

    with right:
        st.markdown("### Daily spending")
        series = insights.daily(view, include_transfers)
        if series.empty:
            st.caption("No spending in range.")
        else:
            figure = go.Figure()
            figure.add_bar(
                x=series["date"], y=series["amount"], name="Daily",
                marker=dict(color=theme.PRIMARY_SOFT, line=dict(width=0)),
                hovertemplate="%{x|%d %b %Y}<br>₹%{y:,.0f}<extra></extra>",
            )
            figure.add_scatter(
                x=series["date"], y=series["rolling"], name="7-day average",
                mode="lines", line=dict(color=theme.SERIES[1], width=2),
                hovertemplate="%{x|%d %b %Y}<br>avg ₹%{y:,.0f}<extra></extra>",
            )
            theme.style(figure, height=380, show_legend=True)
            figure.update_layout(hovermode="x unified")
            figure.update_xaxes(showgrid=False)
            figure.update_yaxes(tickformat="~s")
            st.plotly_chart(figure, width="stretch",
                            config=theme.PLOTLY_CONFIG)

st.divider()


# --------------------------------------------------------------------------------------
# Recurring
# --------------------------------------------------------------------------------------

with st.expander("Recurring payments and subscriptions (across all statements)",
                 expanded=False):
    # Deliberately not the period view: a cadence needs three occurrences to be found,
    # so a one-month window would hide every monthly subscription.
    recurring = insights.detect_recurring(data)
    if recurring.empty:
        st.caption(
            "Nothing repeated often enough to call recurring yet — this needs about three "
            "months of statements to find a pattern."
        )
    else:
        monthly_equivalent = {
            "Weekly": 52 / 12, "Fortnightly": 26 / 12, "Monthly": 1.0,
            "Quarterly": 1 / 3, "Half-yearly": 1 / 6, "Yearly": 1 / 12,
        }
        # The run rate covers recurring *spending* only. Rent counts; a credit-card bill or
        # a SIP does not — they are in the table because they are real commitments, but
        # adding them here would describe money you invested or already spent as an
        # ongoing cost.
        run_rate = sum(
            row.typical_amount * monthly_equivalent.get(row.cadence, 1.0)
            for row in recurring.itertuples(index=False)
            if row.category not in NOT_SPENDING
        )
        committed = len(recurring[recurring["category"].isin(NOT_SPENDING)])
        note = (f" {committed} more repeat regularly but are transfers or investments, "
                "so they are listed below without counting toward that.") if committed else ""
        st.caption(
            f"**{len(recurring)} recurring payment(s)** — the spending ones come to about "
            f"**{theme.rupees(run_rate)} a month**, or {theme.rupees(run_rate * 12)} a year."
            + note
        )
        display = recurring.copy()
        display["typical_amount"] = display["typical_amount"].map(theme.rupees)
        display["total"] = display["total"].map(theme.rupees)
        display["last_paid"] = display["last_paid"].dt.strftime("%d %b %Y")
        display["next_expected"] = display["next_expected"].dt.strftime("%d %b %Y")
        st.dataframe(
            display, width="stretch", hide_index=True,
            column_config={
                "merchant": "Merchant", "category": "Category",
                "typical_amount": "Each time", "cadence": "Every",
                "occurrences": "Times", "total": "Paid so far",
                "last_paid": "Last paid", "next_expected": "Next expected",
            },
        )

st.divider()


# --------------------------------------------------------------------------------------
# Review and edit
# --------------------------------------------------------------------------------------

EDITOR_COLUMNS = ["date", "merchant", "description", "amount", "direction", "category",
                  "category_source", "confidence", "merchant_key"]


def as_editable(frame: pd.DataFrame) -> pd.DataFrame:
    view = frame.copy()
    view["amount"] = view["debit"].fillna(0.0) + view["credit"].fillna(0.0)
    view["direction"] = ["Out" if debit > 0 else "In" for debit in view["debit"].fillna(0.0)]
    view["category_source"] = decided_by_labels(view, store)
    return view[EDITOR_COLUMNS]


def apply_filters(
    frame: pd.DataFrame, categories: list[str], search: str, minimum: float
) -> pd.DataFrame:
    """Category/search/minimum-amount filter shared by the All transactions and Export tabs."""
    filtered = frame
    if categories:
        filtered = filtered[filtered["category"].isin(categories)]
    if search:
        needle = search.strip().lower()
        filtered = filtered[
            filtered["merchant"].str.lower().str.contains(needle, na=False)
            | filtered["description"].str.lower().str.contains(needle, na=False)
        ]
    if minimum:
        amounts = filtered["debit"].fillna(0.0) + filtered["credit"].fillna(0.0)
        filtered = filtered[amounts >= minimum]
    return filtered


def sort_transactions(frame: pd.DataFrame, sort_by: str, ascending: bool) -> pd.DataFrame:
    """Sort by a real column, or by the derived debit+credit amount."""
    if sort_by == "amount":
        key = frame["debit"].fillna(0.0) + frame["credit"].fillna(0.0)
        return (
            frame.assign(_sort_key=key)
            .sort_values("_sort_key", ascending=ascending, kind="mergesort")
            .drop(columns="_sort_key")
        )
    return frame.sort_values(sort_by, ascending=ascending, kind="mergesort")


EDITOR_CONFIG = {
    "date": st.column_config.DateColumn("Date", format="DD MMM YYYY", disabled=True),
    "merchant": st.column_config.TextColumn("Merchant", disabled=True),
    "description": st.column_config.TextColumn("Narration", width="large", disabled=True),
    "amount": st.column_config.NumberColumn("Amount", format="₹%.2f", disabled=True),
    "direction": st.column_config.TextColumn("In/Out", width="small", disabled=True),
    "category": st.column_config.SelectboxColumn(
        "Category", options=all_categories(store.custom_categories), required=True,
        width="medium"
    ),
    "category_source": st.column_config.TextColumn("Decided by", width="medium",
                                                   disabled=True),
    "confidence": st.column_config.ProgressColumn(
        "Confidence", min_value=0.0, max_value=1.0, format="%.2f", width="small"
    ),
    "merchant_key": None,
}


def apply_edits(before: pd.DataFrame, after: pd.DataFrame) -> int:
    """Persist any category a person changed, as a permanent merchant override."""
    changed = 0
    for key, old, new in zip(before["merchant_key"], before["category"], after["category"]):
        if new != old:
            store.set_override(key, new)
            changed += 1
    return changed


def add_category_widget(key: str) -> None:
    """A small inline control to add a custom category, shown above an editor table."""
    field, action = st.columns([4, 1])
    name = field.text_input(
        "Add a new category", key=f"new_cat_{key}", placeholder="e.g. Pet care",
        label_visibility="collapsed",
    )
    if action.button("Add category", key=f"add_cat_{key}", width="stretch"):
        if store.add_category(name):
            st.rerun()
        else:
            st.toast("That category already exists (or the name was empty).")


REVIEW_SORT_OPTIONS = {"Amount": "amount", "Date": "date", "Merchant": "merchant",
                       "Confidence": "confidence"}
EXPORT_SORT_OPTIONS = {"Date": "date", "Amount": "amount", "Merchant": "merchant",
                       "Category": "category"}
REVIEW_SHOW_OPTIONS = {"Everything": "all", "Uncategorised only": "uncategorised",
                       "Low confidence only": "low_confidence"}

review_all = needs_review(view)
review_elsewhere = len(needs_review(data)) - len(review_all)
review_label = f"Needs review ({len(review_all)})" if len(review_all) else "Needs review (0)"
tab_review, tab_all, tab_export = st.tabs([review_label, "All transactions", "Export"])

with tab_review:
    if review_elsewhere:
        st.info(
            f"{review_elsewhere} more need review outside the selected period."
        )
    if review_all.empty:
        st.success(
            "Nothing needs review in this period — every transaction matched a rule, a "
            "merchant you already tagged, or something previously learned."
        )
    else:
        sort_col, order_col, show_col = st.columns([2, 2, 2])
        sort_label = sort_col.selectbox(
            "Sort by", list(REVIEW_SORT_OPTIONS), key="review_sort_by"
        )
        order_label = order_col.radio(
            "Order", ["Descending", "Ascending"], key="review_order", horizontal=True
        )
        show_label = show_col.selectbox(
            "Show", list(REVIEW_SHOW_OPTIONS), key="review_show"
        )
        sort_by = REVIEW_SORT_OPTIONS[sort_label]
        ascending = order_label == "Ascending"
        reason = REVIEW_SHOW_OPTIONS[show_label]
        review = needs_review(view, sort_by=sort_by, ascending=ascending, reason=reason)

        st.caption(
            f"Sorted by {sort_label.lower()}, {order_label.lower()}. Change a category "
            "here and it is remembered for that merchant permanently — every future "
            "statement uses your answer."
        )
        add_category_widget("review")
        before = as_editable(review)
        after = st.data_editor(
            before, width="stretch", hide_index=True,
            column_config=EDITOR_CONFIG,
            key=f"review_editor_{sort_by}_{ascending}_{reason}",
        )
        if st.button("Save these categories", type="primary"):
            count = apply_edits(before, after)
            if count:
                refresh()
                st.success(f"Saved {count} categor{'y' if count == 1 else 'ies'}.")
                st.rerun()
            else:
                st.info("Nothing changed.")

with tab_all:
    add_category_widget("all")
    filter_left, filter_middle, filter_right = st.columns([2, 2, 1])
    chosen = filter_left.multiselect(
        "Categories", options=all_categories(store.custom_categories),
        default=[], placeholder="All categories",
    )
    search = filter_middle.text_input("Search", placeholder="Merchant or narration")
    minimum = filter_right.number_input("Min amount", min_value=0, value=0, step=100)

    filtered = apply_filters(view, chosen, search, minimum)

    st.caption(
        f"{len(filtered)} of {len(view)} transactions · "
        f"out {theme.rupees(filtered['debit'].sum())} · "
        f"in {theme.rupees(filtered['credit'].sum())}"
    )

    before_all = as_editable(filtered.sort_values("date", ascending=False))
    after_all = st.data_editor(
        before_all, width="stretch", hide_index=True,
        column_config=EDITOR_CONFIG, key="all_editor", height=520,
    )
    if st.button("Save these categories", type="primary", key="save_all"):
        count = apply_edits(before_all, after_all)
        if count:
            refresh()
            st.success(f"Saved {count} categor{'y' if count == 1 else 'ies'}.")
            st.rerun()
        else:
            st.info("Nothing changed.")

with tab_export:
    st.caption("Everything below reflects your current categories, including any you edited.")

    main, options = st.columns([3, 1])
    with options:
        st.markdown("**Filter & sort**")
        export_categories = st.multiselect(
            "Categories", options=all_categories(store.custom_categories),
            default=[], placeholder="All categories", key="export_categories",
        )
        export_search = st.text_input(
            "Search", placeholder="Merchant or narration", key="export_search"
        )
        export_minimum = st.number_input(
            "Min amount", min_value=0, value=0, step=100, key="export_minimum"
        )
        export_sort_label = st.selectbox(
            "Sort by", list(EXPORT_SORT_OPTIONS), key="export_sort_by"
        )
        export_order_label = st.radio(
            "Order", ["Ascending", "Descending"], key="export_order",
        )

    filtered_export = apply_filters(view, export_categories, export_search, export_minimum)
    filtered_export = sort_transactions(
        filtered_export, EXPORT_SORT_OPTIONS[export_sort_label],
        export_order_label == "Ascending",
    )

    export = filtered_export.copy()
    export["amount"] = export["debit"].fillna(0.0) + export["credit"].fillna(0.0)
    export["direction"] = ["Out" if d > 0 else "In" for d in export["debit"].fillna(0.0)]
    export["category_source"] = decided_by_labels(export, store)
    export = export[["date", "description", "merchant", "category", "channel",
                     "direction", "debit", "credit", "balance", "category_source",
                     "confidence", "source_file"]]

    excel_buffer = io.BytesIO()
    with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
        export.to_excel(writer, sheet_name="Transactions", index=False)
        insights.by_category(filtered_export, include_transfers).to_excel(
            writer, sheet_name="By category", index=False)
        insights.monthly_totals(filtered_export, include_transfers).to_excel(
            writer, sheet_name="By month", index=False)
        recurring_sheet = insights.detect_recurring(filtered_export)
        if not recurring_sheet.empty:
            recurring_sheet.to_excel(writer, sheet_name="Recurring", index=False)

    with main:
        st.caption(
            f"{len(filtered_export)} of {len(view)} transactions match your filters, "
            f"sorted by {export_sort_label.lower()} ({export_order_label.lower()})."
        )
        download_left, download_right = st.columns(2)
        download_left.download_button(
            "Download Excel workbook", data=excel_buffer.getvalue(),
            file_name="categorised-statement.xlsx", width="stretch",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        download_right.download_button(
            "Download CSV", data=export.to_csv(index=False).encode("utf-8"),
            file_name="categorised-statement.csv", mime="text/csv",
            width="stretch",
        )

        st.dataframe(export.head(200), width="stretch", hide_index=True)


# --------------------------------------------------------------------------------------
# Parse diagnostics
# --------------------------------------------------------------------------------------

problem_reports = [report for report in reports
                   if report.unreconciled_rows or report.warnings]
if problem_reports:
    with st.expander("Parsing details", expanded=not all_reconciled):
        for report in reports:
            st.markdown(f"**{report.source_file}** — {report.strategy}")
            st.caption(report.summary())
            for warning in report.warnings:
                st.warning(warning)
            if report.unreconciled_rows:
                st.caption(
                    "Rows whose balance movement does not match their debit or credit "
                    f"(0-indexed): {report.unreconciled_rows[:40]}"
                )
