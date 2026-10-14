import unittest

from shop.inventory import Inventory


class AddTests(unittest.TestCase):
    def test_add_and_count(self):
        inv = Inventory()
        inv.add("apple", 3)
        inv.add("apple", 2)
        self.assertEqual(inv.count("apple"), 5)

    def test_unknown_is_zero(self):
        self.assertEqual(Inventory().count("pear"), 0)

    def test_add_rejects_non_positive(self):
        with self.assertRaises(ValueError):
            Inventory().add("apple", 0)


if __name__ == "__main__":
    unittest.main()
