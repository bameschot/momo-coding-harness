"""Ground truth for the Kotlin synthetic project.  See python/expect.py for the
format.  Kotlin imports name declarations (a class, an extension function), not
files."""

DEFINITIONS = [
    ("MAX_SLUG",          "constant",  "shop/Text.kt",     "const val MAX_SLUG"),
    ("DEFAULT_SEPARATOR", "constant",  "shop/Text.kt",     "val DEFAULT_SEPARATOR"),
    ("slug",              "function",  "shop/Text.kt",     "fun String.slug()"),
    ("percentOf",         "function",  "shop/Text.kt",     "infix fun Int.percentOf"),
    ("Product",           "class",     "shop/Model.kt",    "data class Product("),
    ("Product.label",     "method",    "shop/Model.kt",    "fun label()"),
    ("Product.FREE",      "constant",  "shop/Model.kt",    "const val FREE"),
    ("Product.free",      "method",    "shop/Model.kt",    "fun free(name: String)"),
    ("Payment",           "class",     "shop/Model.kt",    "sealed class Payment"),
    ("Payment.Card",      "class",     "shop/Model.kt",    "data class Card("),
    ("Payment.Cash",      "object",    "shop/Model.kt",    "object Cash"),
    ("Pricer",            "interface", "shop/Model.kt",    "interface Pricer"),
    ("Pricer.price",      "method",    "shop/Model.kt",    "fun price(p: Product)"),
    ("Checkout",          "object",    "shop/Checkout.kt", "object Checkout"),
    ("Checkout.total",    "method",    "shop/Checkout.kt", "fun total("),
    ("Checkout.receipt",  "method",    "shop/Checkout.kt", "fun receipt("),
    ("main",              "function",  "app/Main.kt",      "fun main()"),
]

ABSENT = [
    ("sum",   "shop/Checkout.kt"),        # local val
    ("items", "app/Main.kt"),
]

SEARCHES = [
    ("slug",         {}, "slug"),
    ("percent of",   {}, "percentOf"),
    ("free product", {}, "Product.free"),
    ("receipt",      {}, "Checkout.receipt"),
    ("Cash",         {}, "Payment.Cash"),
    ("max slug",     {}, "MAX_SLUG"),
    (("shop/Checkout.kt", "val sum = items.sumOf"), {}, "Checkout.total"),
]

CALLERS = {
    "slug": {
        ("shop/Model.kt", "name.slug()",            "call"),
        ("app/Main.kt",   "import shop.slug",       "import"),
        ("app/Main.kt",   '"Summer Sale".slug()',   "call"),
    },
    # An infix call is a call, though it has no parentheses.
    "percentOf": {
        ("shop/Checkout.kt", "discountPct percentOf sum", "call"),
    },
    "Checkout.total": {
        ("shop/Checkout.kt", "total(items, 10)",    "call"),
    },
}

CHAINS = {
    "percentOf": {"receipt"},              # Checkout.total <- Checkout.receipt
}

IMPORTS = {
    "shop/Model.kt":    {"shop/Checkout.kt", "app/Main.kt"},   # import shop.Product
    "shop/Checkout.kt": {"app/Main.kt"},                       # import shop.Checkout
    "shop/Text.kt":     {"app/Main.kt"},                       # import shop.slug
}

FRESH = ("shop/Text.kt", "\nfun freshMarker() = 1\n", "freshMarker")

KNOWN_GAPS = {}
