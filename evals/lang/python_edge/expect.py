"""Edge cases for Python: enum members, Protocol stubs, static/class methods,
super() calls, Optional[...] annotations, comprehension / with / except
variables that shadow a function name, a module-level lambda, async def and
star imports.  Format: see evals/lang/python/expect.py."""

M, C = "app/models.py", "app/cli.py"

DEFINITIONS = [
    ("Color",        "class",    M, "class Color(enum.Enum)"),
    ("Color.RED",    "constant", M, "RED = 1"),
    ("Priced",       "class",    M, "class Priced(Protocol)"),
    ("Priced.price", "method",   M, "def price(self) -> int: ..."),
    ("Order",        "class",    M, "class Order:"),
    ("Order.count",  "variable", M, "count = 0"),
    ("Order.empty",  "method",   M, "def empty()"),
    ("Order.of",     "method",   M, "def of(cls"),
    ("Order.price",  "method",   M, "        return self.total"),
    ("Rush.price",   "method",   M, "return super().price() * 2"),
    ("summary",      "function", M, "def summary("),
    ("total",        "function", M, "def total(xs"),
    ("report",       "function", M, "def report("),
    ("scale",        "function", M, "scale = lambda"),
    ("fetch_order",  "function", M, "async def fetch_order("),
    ("grand_total",  "function", M, "def grand_total("),
    ("main",         "function", C, "def main()"),
]

ABSENT = [
    ("doubled", M),
    ("first",   C),
    ("handlers", C),
]

SEARCHES = [
    ("empty order",   {},                 "Order.empty"),
    ("fetch order",   {},                 "fetch_order"),
    ("red",           {},                 "Color.RED"),
    ("Order.price",   {},                 "Order.price"),
    ((M, "return super().price() * 2"), {}, "Rush.price"),
]

CALLERS = {
    # Every `total` in report() is a comprehension / with / except variable —
    # and with/except make it local to all of report(), so none is a use.
    "total": {
        (M, "return total([o.price() for o in orders])", "call"),
    },
    # Passed as a value to map() is a use, not a call.
    "summary": {
        (C, "from .models import Order, Rush, summary", "import"),
        (C, "print(summary(first), summary(rush)",     "call"),
        (C, "list(map(summary, [first, rush]))",       "other"),
    },
    # order: Optional[Order] -> an Order; super().price() is the base method.
    "Order.price": {
        (M, "return super().price() * 2",               "call"),
        (M, "return str(order.price())",                "call"),
        (M, "(o.price() for o in orders)",              "call"),
        (M, "return total([o.price() for o in orders])", "call"),   # o from list[Order]
    },
    "Rush.price": {
        (M, "return str(order.price())",                "call"),
        (M, "(o.price() for o in orders)",              "call"),
        (M, "return total([o.price() for o in orders])", "call"),
    },
    "scale": {
        (C, "scale(3))",                                "call"),
    },
}

# summary <- main <- (module level): nothing at level 2.
CHAINS = {}

IMPORTS = {
    M: {C},
    C: set(),
    "other/models.py": set(),       # same file name, other package: `from .models` is not it
}

FRESH = (C, "\n\ndef fresh_marker():\n    return 1\n", "fresh_marker")

KNOWN_GAPS = {}
