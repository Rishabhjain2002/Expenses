"""Chart palette and Plotly styling.

Colours come from a validated categorical palette — eight slots, checked for lightness
band, chroma floor, colour-vision-deficiency separation, and contrast against the
#fcfcfb chart surface this app renders on. Slots are assigned in fixed order and never
cycled: a ninth series folds into "Other" instead of inventing a colour nobody can
distinguish from the other eight.
"""

from __future__ import annotations

# Categorical slots, in fixed order.
SERIES = [
    "#2a78d6",  # blue
    "#eb6834",  # orange
    "#1baf7a",  # aqua
    "#eda100",  # yellow
    "#e87ba4",  # magenta
    "#008300",  # green
    "#4a3aa7",  # violet
    "#e34948",  # red
]

# Single-hue blue for ranked, one-measure charts, where length carries the magnitude
# and colour carries nothing — so it should not pretend to.
PRIMARY = "#2a78d6"
PRIMARY_SOFT = "#86b6ef"

SURFACE = "#fcfcfb"
PAGE = "#f9f9f7"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"

GOOD = "#0ca30c"
CRITICAL = "#d03b3b"
WARNING = "#fab219"

FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'


def series_colors(count: int) -> list[str]:
    """Colours for ``count`` series, capped at the eight validated slots."""
    return SERIES[:min(count, len(SERIES))]


def style(figure, height: int = 320, show_legend: bool = False, y_prefix: str = "₹"):
    """Apply the shared chart chrome: recessive grid, muted axes, no chart junk."""
    figure.update_layout(
        height=height,
        margin=dict(l=8, r=8, t=8, b=8),
        paper_bgcolor=SURFACE,
        plot_bgcolor=SURFACE,
        font=dict(family=FONT, size=12, color=INK_SECONDARY),
        showlegend=show_legend,
        legend=dict(
            orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0,
            font=dict(size=11, color=INK_SECONDARY), title=None,
        ),
        hoverlabel=dict(
            bgcolor=SURFACE, bordercolor=AXIS, font=dict(family=FONT, size=12, color=INK)
        ),
        bargap=0.25,
    )
    figure.update_xaxes(
        showgrid=False, zeroline=False, linecolor=AXIS, ticks="outside",
        tickcolor=AXIS, ticklen=4, tickfont=dict(color=MUTED, size=11),
    )
    figure.update_yaxes(
        showgrid=True, gridcolor=GRID, gridwidth=1, zeroline=False,
        showline=False, tickfont=dict(color=MUTED, size=11), tickprefix=y_prefix,
    )
    return figure


def rupees(value: float, decimals: int = 0) -> str:
    """Format a number with Indian lakh/crore grouping: ₹1,23,456."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"

    sign = "-" if number < 0 else ""
    text = f"{abs(number):.{decimals}f}"
    whole, _, fraction = text.partition(".")

    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])

    return f"{sign}₹{whole}" + (f".{fraction}" if fraction else "")


def compact(value: float) -> str:
    """Short form for axis ticks and tiles: ₹1.2L, ₹45.0k."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    sign = "-" if number < 0 else ""
    magnitude = abs(number)
    if magnitude >= 10_000_000:
        return f"{sign}₹{magnitude / 10_000_000:.2f}Cr"
    if magnitude >= 100_000:
        return f"{sign}₹{magnitude / 100_000:.2f}L"
    if magnitude >= 1_000:
        return f"{sign}₹{magnitude / 1_000:.1f}k"
    return f"{sign}₹{magnitude:.0f}"
