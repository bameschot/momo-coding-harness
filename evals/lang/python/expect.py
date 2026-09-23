"""Ground truth for the Python synthetic project (evals/lang/python/project).

Written from the language's semantics, never from the index's output.  Every
location is a unique text anchor on the defining / using line; lang_bench
resolves it to a line number by plain text search.
"""

# (qualname, kind, file, anchor)
DEFINITIONS = [
    ("MAX_ITEMS",              "constant", "shop/cart.py",      "MAX_ITEMS = 50"),
    ("_log",                   "function", "shop/cart.py",      "def _log(msg)"),
    ("Item",                   "class",    "shop/cart.py",      "class Item:"),
    ("Item.sku",               "variable", "shop/cart.py",      "sku: str"),
    ("Cart",                   "class",    "shop/cart.py",      "class Cart:"),
    ("Cart.__init__",          "method",   "shop/cart.py",      "def __init__(self):"),
    ("Cart.size",              "method",   "shop/cart.py",      "def size(self) -> int"),
    ("Cart.size",              "method",   "shop/cart.py",      "def size(self, value"),
    ("Cart.add",               "method",   "shop/cart.py",      "def add(self, item"),
    ("Cart.total",             "method",   "shop/cart.py",      "def total(self)"),
    ("Cart.clear",             "method",   "shop/cart.py",      "def clear(self) -> None"),
    ("TAX_RATE",               "constant", "shop/pricing.py",   "TAX_RATE = 0.21"),
    ("apply_discount",         "function", "shop/pricing.py",   "def apply_discount("),
    ("apply_discount.clamp",   "function", "shop/pricing.py",   "def clamp(v)"),
    ("retry",                  "function", "shop/pricing.py",   "def retry(times)"),
    ("retry.deco",             "function", "shop/pricing.py",   "def deco(fn)"),
    ("retry.deco.wrapper",     "function", "shop/pricing.py",   "def wrapper("),
    ("fetch_rates",            "function", "shop/pricing.py",   "def fetch_rates("),
    ("Stock",                  "class",    "shop/inventory.py", "class Stock:"),
    ("Stock.clear",            "method",   "shop/inventory.py", "def clear(self):"),
    ("Stock.value",            "method",   "shop/inventory.py", "def value(self"),
    ("checkout",               "function", "shop/checkout.py",  "def checkout("),
    ("reset",                  "function", "shop/checkout.py",  "def reset("),
    ("__all__",                "variable", "shop/__init__.py",  "__all__ = "),
]

# (name, file): must NOT be a definition there — locals, loop variables.
ABSENT = [
    ("subtotal", "shop/cart.py"),
    ("rates",    "shop/checkout.py"),
    ("bonus",    "shop/checkout.py"),
]

# (query, kwargs, expected top hit qualname[, expected file]).  A (file, anchor)
# tuple as the query means "which definition is this line in".
SEARCHES = [
    ("total",                 {},                               "Cart.total"),
    ("discount",              {},                               "apply_discount"),
    ("tax rate",              {},                               "TAX_RATE"),
    ("empty the warehouse",   {},                               "Stock.clear"),
    ("download rates",        {},                               "fetch_rates"),
    ("maximum cart lines",    {},                               "MAX_ITEMS"),
    ("clear",                 {"path": "shop/inventory.py"},    "Stock.clear"),
    ("Cart",                  {"kind": "class"},                "Cart"),
    (("shop/cart.py", "subtotal = sum"), {},                    "Cart.total"),
]

# name -> the TRUE uses {(file, anchor, role)}.  Name-based lookup finds more
# (Cart.clear also matches list.clear); precision measures exactly that.
CALLERS = {
    "apply_discount": {
        ("shop/cart.py",      "from .pricing import apply_discount", "import"),
        ("shop/cart.py",      "return apply_discount(subtotal)",     "call"),
        ("shop/inventory.py", "p.apply_discount(",                   "call"),
        ("shop/__init__.py",  "import apply_discount as discount",   "import"),
    },
    "Cart.clear": {
        ("shop/checkout.py",  "cart.clear()",                        "call"),
    },
    "fetch_rates": {
        ("shop/checkout.py",  "from shop.pricing import fetch_rates", "import"),
        ("shop/checkout.py",  'rates = fetch_rates("eu")',            "call"),
    },
}

# name -> definitions that must appear at level 2 of index_callers(depth=2).
CHAINS = {
    "apply_discount": {"checkout"},        # Cart.total <- checkout
}

# file -> the files that import it.
IMPORTS = {
    "shop/pricing.py":   {"shop/cart.py", "shop/inventory.py", "shop/checkout.py", "shop/__init__.py"},
    "shop/cart.py":      {"shop/__init__.py", "shop/checkout.py"},
    "shop/inventory.py": {"shop/cart.py"},
    "shop/checkout.py":  set(),
}

# Appended to a file to check the index sees an edit: (file, text, qualname).
FRESH = ("shop/pricing.py", "\n\ndef fresh_marker():\n    return 1\n", "fresh_marker")

# "section:item" -> why it fails today.  Reported, not counted as failures.
KNOWN_GAPS = {}
