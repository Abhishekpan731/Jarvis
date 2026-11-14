"""Token accounting. Prices are estimates, labelled as such, never billed."""

# USD per million tokens, input then output. Update when a model changes price.
_PRICE_PER_MILLION = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
}


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Return an estimate, or None when the model has no known price."""
    price = _PRICE_PER_MILLION.get(model)
    if price is None:
        return None
    return round((input_tokens * price[0] + output_tokens * price[1]) / 1_000_000, 6)
