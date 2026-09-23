"""Ground truth for the JavaScript synthetic project.  See python/expect.py for
the format.  Covers ES module exports/re-exports, class fields, CommonJS
require/module.exports and a dynamic import()."""

DEFINITIONS = [
    ("CURRENCY",     "constant", "src/format.js", "export const CURRENCY"),
    ("formatPrice",  "function", "src/format.js", "export function formatPrice("),
    ("formatList",   "function", "src/format.js", "export default function formatList("),
    ("Cart",         "class",    "src/cart.js",   "export class Cart {"),
    ("Cart.MAX",     "constant", "src/cart.js",   "static MAX = 20"),
    ("Cart.add",     "method",   "src/cart.js",   "add = (item) =>"),
    ("Cart.total",   "method",   "src/cart.js",   "total() {"),
    ("Cart.describe", "method",  "src/cart.js",   "describe() {"),
    ("legacyTotal",  "function", "src/legacy.js", "function legacyTotal("),
    ("cart",         "variable", "src/app.js",    "const cart = new Cart()"),
    ("lazyReport",   "function", "src/app.js",    "async function lazyReport()"),
]

ABSENT = [
    ("legacyTotal", "src/app.js"),        # destructured from a dynamic import
    ("formatPrice", "src/legacy.js"),     # destructured from require()
]

SEARCHES = [
    ("format price", {}, "formatPrice"),
    ("formatList",   {}, "formatList"),
    ("total",        {}, "Cart.total"),
    ("currency",     {}, "CURRENCY"),
    ("legacy total", {}, "legacyTotal"),
    (("src/cart.js", "this.items.push(item)"), {}, "Cart.add"),
]

CALLERS = {
    "formatPrice": {
        ("src/format.js", "formatPrice(i.cents)",                 "call"),
        ("src/cart.js",   'import formatList, { formatPrice }',   "import"),
        ("src/cart.js",   "formatPrice(this.total())",            "call"),
        ("src/legacy.js", 'const { formatPrice } = require(',     "import"),
        ("src/legacy.js", "return formatPrice(cart.total())",     "call"),
    },
    "legacyTotal": {
        ("src/legacy.js", "module.exports = { legacyTotal }",     "other"),
        ("src/app.js",    "const { legacyTotal } = await import", "import"),
        ("src/app.js",    "return legacyTotal(cart)",             "call"),
    },
}

CHAINS = {
    "formatPrice": {"lazyReport"},         # legacyTotal <- lazyReport
}

IMPORTS = {
    "src/format.js": {"src/cart.js", "src/index.js", "src/legacy.js"},
    "src/cart.js":   {"src/index.js"},
    "src/index.js":  {"src/app.js"},
    "src/legacy.js": {"src/app.js"},        # dynamic import()
}

FRESH = ("src/format.js", "\nexport function freshMarker() { return 1; }\n", "freshMarker")

KNOWN_GAPS = {}
