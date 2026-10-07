import unittest

from shop.inventory import Inventory, Item
from shop.shipping import Parcel, shipping_cost
from shop.util import slug


class Basics(unittest.TestCase):
    def test_find(self):
        inv = Inventory([Item("a1", "Apple", 0.5, 10)])
        self.assertEqual(inv.find("a1").name, "Apple")
        self.assertIsNone(inv.find("zz"))

    def test_domestic_shipping(self):
        self.assertEqual(shipping_cost([Parcel(2)], "domestic"), 3.5)

    def test_slug(self):
        self.assertEqual(slug("Big Red Box"), "big-red-box")


if __name__ == "__main__":
    unittest.main()
