"""Money as whole cents (design note section 1). Never a float."""

from decimal import Decimal, InvalidOperation


def to_cents(amount: Decimal | str | int) -> int:
    """Turn an amount like ``"5000.01"`` or ``Decimal("5000.01")`` into 500001 cents.

    Floats are refused because ``5000.01`` as a float is not exactly 5000.01. Amounts with more
    than two decimals are refused instead of rounded, so no cent is lost silently.
    """
    if isinstance(amount, bool) or not isinstance(amount, Decimal | str | int):
        raise TypeError(f"amount must be a Decimal, str or int, not {type(amount).__name__}")
    try:
        value = Decimal(amount)
    except InvalidOperation:
        raise ValueError(f"not an amount: {amount!r}") from None
    if not value.is_finite():
        raise ValueError(f"not an amount: {amount!r}")
    cents = value * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"amount {amount!r} has more than two decimals")
    return int(cents)


def from_cents(cents: int) -> Decimal:
    """500001 cents back to ``Decimal("5000.01")``."""
    return Decimal(cents).scaleb(-2)
