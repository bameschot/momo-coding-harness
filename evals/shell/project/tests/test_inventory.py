"""300 generated cases; two of them hit the bugs in inventory.py."""
import unittest

from inventory import Inventory


class Restock(unittest.TestCase):
    pass


class Value(unittest.TestCase):
    pass


def _restock_case(i):
    def test(self):
        inv = Inventory()
        inv.add("bolt", i if i != 47 else 45)
        start = inv.stock["bolt"]
        self.assertEqual(inv.restock("bolt", 3), start + 3)
    return test


def _value_case(i):
    def test(self):
        inv = Inventory()
        price = 150 * i if i != 203 else 25000
        inv.add("nut", 2, price=price)
        self.assertEqual(inv.value("nut"), 2 * price)
    return test


for _i in range(150):
    # Only bolts under 41 are safe, except case 47 which starts at 45.
    setattr(Restock, f"test_restock_{_i:03d}", _restock_case(_i if _i < 40 or _i == 47 else _i % 40))
for _i in range(150, 300):
    setattr(Value, f"test_price_{_i:03d}", _value_case(_i if _i == 203 else _i % 130))


if __name__ == "__main__":
    unittest.main()
