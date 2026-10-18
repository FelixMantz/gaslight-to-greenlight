from __future__ import annotations

from decimal import ROUND_CEILING, Decimal, InvalidOperation

from g2g.util import canonical


def validate_prices(price: dict) -> dict[str, Decimal]:
    rates = {}
    if not isinstance(price.get("version"), str) or not price["version"].strip():
        raise ValueError("Dated price version required")
    for name in ("input", "output", "cache_read", "cache_write"):
        if name not in price and name.startswith("cache_"):
            continue
        try:
            value = Decimal(str(price[name]))
        except (KeyError, InvalidOperation, ValueError) as exc:
            raise ValueError(f"Invalid price rate: {name}") from exc
        if not value.is_finite() or value < 0:
            raise ValueError(f"Price rate must be finite and nonnegative: {name}")
        rates[name] = value
    return rates


def validate_output_limits(settings: dict) -> tuple[int, int]:
    cap = settings.get("max_output_tokens")
    reserve = settings.get("reserve_output_tokens", 2400)
    if type(cap) is not int or type(reserve) is not int or not 0 < reserve <= cap:
        raise ValueError("Finite integer API cap and planning allowance required")
    return cap, reserve


def price_usage(usage: dict, price: dict) -> int | None:
    rates = validate_prices(price)
    if usage.get("input_tokens") is None or usage.get("output_tokens") is None:
        return None
    for name in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
        value = usage.get(name, 0)
        if type(value) is not int or value < 0:
            raise ValueError("Provider usage must contain nonnegative integer token counts")
    read, write = usage.get("cache_read_tokens", 0), usage.get("cache_write_tokens", 0)
    uncached = usage["input_tokens"] - read - write
    if uncached < 0:
        raise ValueError("Inconsistent provider usage")
    amount = rates["input"] * uncached + rates["output"] * usage["output_tokens"]
    for name, tokens in (("cache_read", read), ("cache_write", write)):
        if tokens:
            if name not in rates:
                raise ValueError(f"Missing verified {name} rate")
            amount += rates[name] * tokens
    return int(amount.to_integral_value(rounding=ROUND_CEILING))


def request_cost(payload: dict, settings: dict, price: dict) -> tuple[int, int]:
    cap, reserve = validate_output_limits(settings)
    rates = validate_prices(price)
    bound = 4 * len(canonical(payload)) + 4096
    if not settings.get("fixture") and not settings.get("token_bound_verified"):
        raise ValueError("Input-token upper-bound verification missing")
    input_rate = max(rates[k] for k in ("input", "cache_read", "cache_write") if k in rates)
    return tuple(
        int((input_rate * bound + rates["output"] * tokens).to_integral_value(rounding=ROUND_CEILING))
        for tokens in (reserve, cap)
    )
