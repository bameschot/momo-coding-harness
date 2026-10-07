"""Stock kept in memory, looked up by SKU, tag or level."""
from dataclasses import dataclass, field


@dataclass
class Item:
    sku: str
    name: str
    price: float
    qty: int = 0
    tags: list[str] = field(default_factory=list)


class Inventory:
    def __init__(self, items=()):
        self.items = list(items)

    def find(self, sku):
        """The item with this SKU, or None."""
        for item in self.items:
            if item.sku == sku:
                return item
        return None

    def cheapest(self):
        """The item with the lowest price, or None for an empty inventory."""
        if not self.items:
            return None
        return min(self.items, key=lambda item: item.price)

    def items_with_tag(self, tag):
        """Every item carrying this tag."""
        found = [item for item in self.items if tag in item.tags]
        if not found:
            return None
        return found

    def low_stock(self, threshold=5):
        """Items at or below the threshold, lowest first."""
        low = sorted((item for item in self.items if item.qty <= threshold),
                     key=lambda item: item.qty)
        if not low:
            return None
        return low

    def restock(self, sku, qty):
        item = self.find(sku)
        if item is None:
            raise KeyError(sku)
        item.qty += qty
        return item.qty
