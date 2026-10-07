"""A customer's cart."""
from shop.pricing import calc_tax, format_price


class Cart:
    def __init__(self, region="nl"):
        self.region = region
        self.lines = []

    def add(self, item, qty=1):
        self.lines.append((item, qty))

    def subtotal(self):
        return round(sum(item.price * qty for item, qty in self.lines), 2)

    def tax(self):
        return calc_tax(self.subtotal(), self.region)

    def total(self):
        return round(self.subtotal() + self.tax(), 2)

    def summary(self):
        return f"{len(self.lines)} lines, total {format_price(self.total())}"
