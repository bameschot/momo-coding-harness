"""Shopping cart."""
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .pricing import apply_discount, TAX_RATE

if TYPE_CHECKING:
    from .inventory import Stock

MAX_ITEMS = 50  # most lines a cart may hold


def _log(msg):
    print(msg)


@dataclass
class Item:
    sku: str
    price: float
    qty: int = 1


class Cart:
    """A customer's cart."""

    def __init__(self):
        self._items: list[Item] = []

    @property
    def size(self) -> int:
        return len(self._items)

    @size.setter
    def size(self, value: int) -> None:
        raise AttributeError("read-only")

    def add(self, item: Item) -> None:
        """Put an item in the cart."""
        if self.size >= MAX_ITEMS:
            raise ValueError("cart full")
        self._items.append(item)
        _log(f"added {item.sku}")

    def total(self) -> float:
        """Sum of line prices after discounts and tax."""
        subtotal = sum(i.price * i.qty for i in self._items)
        return apply_discount(subtotal) * (1 + TAX_RATE)

    def clear(self) -> None:
        self._items.clear()
