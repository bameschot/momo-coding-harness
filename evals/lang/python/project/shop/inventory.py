from . import pricing as p


class Stock:
    def clear(self):
        """Empty the warehouse."""
        self.levels = {}

    def value(self, prices: dict) -> float:
        return p.apply_discount(sum(prices.values()), pct=0.0)
