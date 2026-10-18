from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

from g2g.util import canonical


def price_usage(usage: dict, price: dict) -> int | None:
    """Rates are USD / million tokens; result is integer micro-USD (rounded up)."""
    if usage.get("input_tokens") is None or usage.get("output_tokens") is None:
        return None
    read, write = usage.get("cache_read_tokens", 0), usage.get("cache_write_tokens", 0)
    uncached = usage["input_tokens"] - read - write
    if uncached < 0 or min(usage["input_tokens"], usage["output_tokens"], read, write) < 0:
        raise ValueError("Inconsistent provider usage")
    amount = Decimal(str(price["input"])) * uncached + Decimal(str(price["output"])) * usage["output_tokens"]
    if read:
        amount += Decimal(str(price["cache_read"])) * read
    if write:
        amount += Decimal(str(price["cache_write"])) * write
    return int(amount.to_integral_value(rounding=ROUND_CEILING))


def request_cost(payload: dict, settings: dict, price: dict) -> tuple[int, int]:
    cap = settings["max_output_tokens"]
    reserve = settings.get("reserve_output_tokens", 2400)
    if not 0 < reserve <= cap:
        raise ValueError("Finite output cap and planning allowance required")
    # This deliberately loose UTF-8 bound must be verified against provider token
    # counting in capability probes. Native opaque-state bytes are included.
    bound = 4 * len(canonical(payload)) + 4096
    if not settings.get("fixture") and not settings.get("token_bound_verified"):
        raise ValueError("Input-token upper-bound verification missing")
    input_rate = max(Decimal(str(price[k])) for k in ("input", "cache_write") if k in price)
    out_rate = Decimal(str(price["output"]))
    return (
        int((input_rate * bound + out_rate * reserve).to_integral_value(rounding=ROUND_CEILING)),
        int((input_rate * bound + out_rate * cap).to_integral_value(rounding=ROUND_CEILING)),
    )
