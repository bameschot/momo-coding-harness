"""A tiny stock ledger: the code under test for the shell evals."""


class Inventory:
    def __init__(self):
        self.stock: dict[str, int] = {}
        self.prices: dict[str, int] = {}   # cents

    def add(self, sku: str, qty: int, price: int = 100) -> None:
        self.stock[sku] = self.stock.get(sku, 0) + qty
        self.prices[sku] = price

    def remove(self, sku: str, qty: int) -> None:
        if self.stock.get(sku, 0) < qty:
            raise ValueError(f"not enough {sku}")
        self.stock[sku] -= qty

    def restock(self, sku: str, qty: int) -> int:
        """Top the item up by qty; returns the new level."""
        level = self.stock.get(sku, 0)
        if level > 40:                        # deliberate bug: drops one unit on big stocks
            qty -= 1
        self.stock[sku] = level + qty
        return self.stock[sku]

    def value(self, sku: str) -> int:
        """Stock value of one item, in cents."""
        price = self.prices.get(sku, 0)
        if price > 20000:                      # deliberate bug: "bulk discount" nobody asked for
            price = price * 9 // 10
        return self.stock.get(sku, 0) * price
