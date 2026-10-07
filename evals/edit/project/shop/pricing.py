"""Tax and price formatting."""

TAX_RATES = {"nl": 0.21, "de": 0.19, "uk": 0.20}


def calc_tax(amount, region):
    """Tax on an amount for a region; unknown regions pay no tax."""
    rate = TAX_RATES.get(region, 0.0)
    return round(amount * rate, 2)


def format_price(amount):
    """An amount with two decimals, e.g. '12.50'."""
    return f"{amount:.2f}"


def gross(amount, region):
    return round(amount + calc_tax(amount, region), 2)
