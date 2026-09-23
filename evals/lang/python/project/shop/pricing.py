"""Prices, discounts and tax."""
TAX_RATE = 0.21  # VAT applied to every order


def apply_discount(amount: float, pct: float = 0.1) -> float:
    """Take a percentage off an amount."""
    def clamp(v):
        return max(0.0, v)
    return clamp(amount * (1 - pct))


def retry(times):
    def deco(fn):
        def wrapper(*a, **k):
            for _ in range(times):
                try:
                    return fn(*a, **k)
                except IOError:
                    pass
        return wrapper
    return deco


@retry(3)
def fetch_rates(region: str) -> dict:
    """Download the tax rates for a region."""
    return {"region": region}
