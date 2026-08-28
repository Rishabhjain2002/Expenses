"""Aggregations and pattern detection for the dashboard.

Nothing here talks to the network or the filesystem — it takes the categorised frame
and returns tables ready to chart.
"""

from __future__ import annotations

import pandas as pd

from .categorize import NOT_SPENDING

# Cadences worth naming, as (label, low, high) day-gap windows.
_CADENCES = [
    ("Weekly", 6, 8),
    ("Fortnightly", 13, 16),
    ("Monthly", 26, 35),
    ("Quarterly", 85, 95),
    ("Half-yearly", 175, 190),
    ("Yearly", 355, 375),
]


def spending(frame: pd.DataFrame, include_transfers: bool = False) -> pd.DataFrame:
    """Debit rows that count as spending.

    By default this excludes transfers between your own accounts, credit-card bill
    payments, and investments — money that moved but was not consumed. Including them
    inflates every total on the dashboard.
    """
    if frame.empty:
        return frame
    result = frame[frame["debit"] > 0]
    if not include_transfers:
        result = result[~result["category"].isin(NOT_SPENDING)]
    return result


def income(frame: pd.DataFrame) -> pd.DataFrame:
    """Credit rows that are genuinely money in, not transfers between own accounts."""
    if frame.empty:
        return frame
    return frame[(frame["credit"] > 0) & (frame["category"] != "Transfers")]


def headline(frame: pd.DataFrame, include_transfers: bool = False) -> dict:
    """The KPI row."""
    if frame.empty:
        return {
            "money_in": 0.0, "money_out": 0.0, "net": 0.0, "monthly_burn": 0.0,
            "transactions": 0, "months": 0, "start": None, "end": None,
            "excluded": 0.0, "largest": None,
        }

    out_frame = spending(frame, include_transfers)
    money_out = float(out_frame["debit"].sum())
    money_in = float(income(frame)["credit"].sum())
    excluded = float(frame["debit"].sum()) - money_out

    start, end = frame["date"].min(), frame["date"].max()
    months = max(1, len(frame["date"].dt.to_period("M").unique()))

    largest = None
    if not out_frame.empty:
        row = out_frame.loc[out_frame["debit"].idxmax()]
        largest = {"merchant": row["merchant"], "amount": float(row["debit"]),
                   "date": row["date"], "category": row["category"]}

    return {
        "money_in": money_in,
        "money_out": money_out,
        "net": money_in - money_out,
        "monthly_burn": money_out / months,
        "transactions": int(len(frame)),
        "months": months,
        "start": start,
        "end": end,
        "excluded": excluded,
        "largest": largest,
    }


def by_category(frame: pd.DataFrame, include_transfers: bool = False) -> pd.DataFrame:
    """Total spend per category, largest first."""
    out_frame = spending(frame, include_transfers)
    if out_frame.empty:
        return pd.DataFrame(columns=["category", "amount", "transactions", "share"])

    grouped = (
        out_frame.groupby("category")
        .agg(amount=("debit", "sum"), transactions=("debit", "size"))
        .reset_index()
        .sort_values("amount", ascending=False)
    )
    total = grouped["amount"].sum()
    grouped["share"] = grouped["amount"] / total if total else 0.0
    return grouped.reset_index(drop=True)


def by_month(frame: pd.DataFrame, include_transfers: bool = False,
             top_n: int = 7) -> tuple[pd.DataFrame, list[str]]:
    """Spend per month per category, with the tail folded into 'Other'.

    Returns the pivot table and the category order. Categorical color has a hard limit
    of eight distinct hues, so anything past the top seven becomes 'Other' rather than
    being given a colour nobody can distinguish.
    """
    out_frame = spending(frame, include_transfers)
    if out_frame.empty:
        return pd.DataFrame(), []

    ranking = by_category(frame, include_transfers)["category"].tolist()
    keep = ranking[:top_n]

    working = out_frame.copy()
    working["bucket"] = working["category"].where(working["category"].isin(keep), "Other")
    working["month"] = working["date"].dt.to_period("M").dt.to_timestamp()

    pivot = (
        working.pivot_table(index="month", columns="bucket", values="debit",
                            aggfunc="sum", fill_value=0.0)
        .sort_index()
    )
    order = [c for c in keep if c in pivot.columns]
    if "Other" in pivot.columns:
        order.append("Other")
    return pivot[order], order


def top_merchants(frame: pd.DataFrame, limit: int = 15,
                  include_transfers: bool = False) -> pd.DataFrame:
    """Where the money actually went."""
    out_frame = spending(frame, include_transfers)
    if out_frame.empty:
        return pd.DataFrame(columns=["merchant", "amount", "transactions", "category"])

    grouped = (
        out_frame.groupby(["merchant_key", "merchant"])
        .agg(amount=("debit", "sum"), transactions=("debit", "size"),
             category=("category", lambda values: values.mode().iloc[0]))
        .reset_index()
        .sort_values("amount", ascending=False)
        .head(limit)
    )
    return grouped.reset_index(drop=True)


def daily(frame: pd.DataFrame, include_transfers: bool = False) -> pd.DataFrame:
    """Spend per day with a 7-day rolling average, gaps filled with zero."""
    out_frame = spending(frame, include_transfers)
    if out_frame.empty:
        return pd.DataFrame(columns=["date", "amount", "rolling"])

    series = out_frame.groupby(out_frame["date"].dt.normalize())["debit"].sum()
    full_range = pd.date_range(series.index.min(), series.index.max(), freq="D")
    series = series.reindex(full_range, fill_value=0.0)

    result = pd.DataFrame({"date": series.index, "amount": series.to_numpy()})
    result["rolling"] = result["amount"].rolling(7, min_periods=1).mean()
    return result


def detect_recurring(frame: pd.DataFrame, min_occurrences: int = 3,
                     amount_tolerance: float = 0.15) -> pd.DataFrame:
    """Find subscriptions and standing payments.

    A merchant qualifies when it was paid at least ``min_occurrences`` times, the
    amounts cluster tightly, and the gaps between payments land consistently in one
    cadence window. This is usually the most actionable panel on the dashboard — it is
    where the money you forgot you were spending shows up.
    """
    columns = ["merchant", "category", "typical_amount", "cadence", "occurrences",
               "total", "last_paid", "next_expected"]
    if frame.empty:
        return pd.DataFrame(columns=columns)

    debits = frame[(frame["debit"] > 0) & (frame["category"] != "Cash Withdrawal")]
    if debits.empty:
        return pd.DataFrame(columns=columns)

    rows = []
    for (_, merchant), group in debits.groupby(["merchant_key", "merchant"], sort=False):
        if len(group) < min_occurrences:
            continue

        group = group.sort_values("date")
        amounts = group["debit"]
        median_amount = float(amounts.median())
        if median_amount <= 0:
            continue

        # Amounts must cluster: a merchant you happen to visit often is not a subscription.
        spread = float((amounts - median_amount).abs().max()) / median_amount
        if spread > amount_tolerance:
            continue

        gaps = group["date"].diff().dt.days.dropna()
        if gaps.empty:
            continue
        median_gap = float(gaps.median())

        cadence = next(
            (label for label, low, high in _CADENCES if low <= median_gap <= high), None
        )
        if cadence is None:
            continue
        # Gaps must be consistent, not merely median-correct.
        if float((gaps - median_gap).abs().max()) > max(5.0, median_gap * 0.35):
            continue

        last_paid = group["date"].max()
        rows.append({
            "merchant": merchant,
            "category": group["category"].mode().iloc[0],
            "typical_amount": median_amount,
            "cadence": cadence,
            "occurrences": int(len(group)),
            "total": float(amounts.sum()),
            "last_paid": last_paid,
            "next_expected": last_paid + pd.Timedelta(days=round(median_gap)),
        })

    if not rows:
        return pd.DataFrame(columns=columns)

    result = pd.DataFrame(rows).sort_values("typical_amount", ascending=False)
    return result[columns].reset_index(drop=True)


def monthly_totals(frame: pd.DataFrame, include_transfers: bool = False) -> pd.DataFrame:
    """Income, spend, and net per month."""
    if frame.empty:
        return pd.DataFrame(columns=["month", "income", "spend", "net"])

    months = frame["date"].dt.to_period("M").dt.to_timestamp()
    spend_rows = spending(frame, include_transfers)
    income_rows = income(frame)

    spend_series = spend_rows.groupby(
        spend_rows["date"].dt.to_period("M").dt.to_timestamp()
    )["debit"].sum()
    income_series = income_rows.groupby(
        income_rows["date"].dt.to_period("M").dt.to_timestamp()
    )["credit"].sum()

    index = pd.Index(sorted(months.unique()), name="month")
    result = pd.DataFrame({
        "income": income_series.reindex(index, fill_value=0.0),
        "spend": spend_series.reindex(index, fill_value=0.0),
    }).reset_index()
    result["net"] = result["income"] - result["spend"]
    return result
