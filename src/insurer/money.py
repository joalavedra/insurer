"""Integer-cent money helpers."""

from decimal import ROUND_HALF_UP, Decimal


def cents(value: float | int | Decimal) -> int:
    """Round a euro-cent amount using the accounting half-up convention."""
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
