"""Decimal helpers.

All prices, rates and cash amounts in this project are `decimal.Decimal`, never
binary floats, so results are exact and repeatable. Rates are stored as plain
fractions (0.06 means 6%). Basis points (bp) are hundredths of a percent.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, getcontext

# 40 significant digits is far more than any bond calculation here needs.
getcontext().prec = 40

ZERO = Decimal(0)
ONE = Decimal(1)
HUNDRED = Decimal(100)
BP = Decimal("0.0001")


def D(value: object) -> Decimal:
    """Convert a value to Decimal via its string form (so 0.1 stays 0.1)."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        # Floats are accepted only from trusted config/test code; repr keeps the
        # shortest round-tripping digits rather than the binary expansion.
        return Decimal(repr(value))
    return Decimal(str(value))


def bp(value: object) -> Decimal:
    """Basis points to a fraction: bp(25) == 0.0025."""
    return D(value) * BP


def q(value: Decimal, places: int = 6) -> Decimal:
    """Round for display or storage. Banker's rounding avoids upward bias."""
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)


def money(value: Decimal) -> Decimal:
    """Round a cash amount to cents."""
    return q(value, 2)
