"""Stock levels per item name."""


class Inventory:
    def __init__(self):
        self._stock: dict[str, int] = {}

    def add(self, name: str, qty: int) -> None:
        """Add qty units of name."""
        if qty <= 0:
            raise ValueError("qty must be positive")
        self._stock[name] = self._stock.get(name, 0) + qty

    def count(self, name: str) -> int:
        """Units of name in stock (0 when unknown)."""
        return self._stock.get(name, 0)

    def names(self) -> list[str]:
        """Every item name in stock, sorted."""
        return sorted(self._stock)
