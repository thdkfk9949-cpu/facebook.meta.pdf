"""Currency minor-unit handling.

Meta returns budget and spend amounts as integers in the account currency's
minor unit: ``daily_budget: 1000`` is $10.00 for USD but 1000 won for KRW,
because KRW has no subunit. Getting this wrong makes every budget number in
the report off by 100x, so the offsets are explicit rather than assumed.
"""

# Currencies Meta bills with no minor unit (offset 1). Everything else is 100.
# Source: ISO 4217 zero-decimal currencies, intersected with Meta's billing set.
ZERO_DECIMAL = frozenset(
    {
        "BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG",
        "RWF", "UGX", "UYI", "VND", "VUV", "XAF", "XOF", "XPF",
    }
)

# Currencies with three minor digits (offset 1000).
THREE_DECIMAL = frozenset({"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"})


def offset_for(currency: str) -> int:
    """Return the divisor that converts Meta's integer amount to major units."""
    code = (currency or "USD").upper()
    if code in ZERO_DECIMAL:
        return 1
    if code in THREE_DECIMAL:
        return 1000
    return 100


def to_major(amount: object, currency: str) -> float:
    """Convert a Meta minor-unit amount to major units. ``None`` -> 0.0."""
    if amount in (None, ""):
        return 0.0
    return float(amount) / offset_for(currency)


def fmt(amount: float, currency: str) -> str:
    """Format a major-unit amount for the report."""
    if offset_for(currency) == 1:
        return f"{amount:,.0f} {currency}"
    return f"{amount:,.2f} {currency}"


def to_minor(amount: float, currency: str) -> int:
    """Convert a major-unit amount to the integer Meta expects on writes.

    The inverse of :func:`to_major`. Rounds rather than truncates: a budget of
    10000.4 won must not be written as 10000 and then read back as a change.
    """
    return int(round(float(amount) * offset_for(currency)))
