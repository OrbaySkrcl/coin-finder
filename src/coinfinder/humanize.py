"""Turkish number and duration formatting.

Shared by the bot and the diagnostics module, which is also rendered into the
web dashboard - so this cannot live under bot/ without the API importing from
the bot package.

Turkish conventions differ from English in two ways that matter here: the
decimal separator is a comma and the thousands separator a dot (so 120.292.109
and $2.501,79), and the percent sign goes before the number (%24,9).
"""

from __future__ import annotations


def tr_num(value: float, decimals: int = 2) -> str:
    """Format with a comma decimal separator and dot thousands separators."""
    formatted = f"{value:,.{decimals}f}"
    # Swap the two separators via a placeholder so neither pass clobbers the other.
    return formatted.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def _trim(value: float, decimals: int) -> str:
    """Like tr_num, but drops a trailing zero decimal: 500,0 becomes 500."""
    text = tr_num(value, decimals)
    if "," in text:
        text = text.rstrip("0").rstrip(",")
    return text


def tr_int(value: float) -> str:
    return tr_num(value, 0)


def pct(value: float, decimals: int = 1) -> str:
    """Percent sign first, as Turkish writes it."""
    return f"%{tr_num(value, decimals)}"


def usd(value: float | None) -> str:
    """Compact dollar amounts. 'B' is bin (thousand), 'M' milyon."""
    if value is None:
        return "?"
    sign = "-" if value < 0 else ""
    amount = abs(value)
    if amount >= 1_000_000:
        return f"{sign}${_trim(amount / 1_000_000, 2)}M"
    if amount >= 1_000:
        return f"{sign}${_trim(amount / 1_000, 1)}B"
    if amount >= 1:
        return f"{sign}${tr_num(amount, 0)}"
    # Sub-dollar prices need their significant digits, not two decimals.
    return f"{sign}${amount:.8f}".rstrip("0").rstrip(".").replace(".", ",")


def usd_price(value: float | None) -> str:
    """A token price, never abbreviated.

    ``usd`` compacts thousands into "B", which is right for a market cap and
    wrong for a price: a token at $2501.79 must not render as "$2,5B". Prices
    keep their digits, and sub-dollar prices keep their significant ones.
    """
    if value is None:
        return "?"
    sign = "-" if value < 0 else ""
    amount = abs(value)
    if amount >= 1:
        return f"{sign}${tr_num(amount, 2)}"
    return f"{sign}${amount:.8f}".rstrip("0").rstrip(".").replace(".", ",")


def unwrap(text: str) -> str:
    """Join lines within each paragraph, keeping blank lines as separators.

    Telegram wraps text to the reader's screen, so hard-wrapped source produces
    ragged half-empty lines on a phone. This lets the source stay readable at
    100 columns while the output is one long line per paragraph.
    """
    paragraphs = text.split("\n\n")
    return "\n\n".join(
        " ".join(line.strip() for line in p.splitlines()).strip() for p in paragraphs
    )


def duration(minutes: int | None) -> str:
    if minutes is None:
        return "?"
    if minutes < 60:
        return f"{minutes}dk"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}sa {mins}dk" if mins else f"{hours}sa"
    days, hours = divmod(hours, 24)
    return f"{days}g {hours}sa" if hours else f"{days}g"
