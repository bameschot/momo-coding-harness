"""Ground truth for the TSX (React) synthetic project.  See python/expect.py for
the format.  Rendering a component (<LineItem .../>) is a use of role "call"."""

DEFINITIONS = [
    ("CartLine",      "interface", "src/useCart.tsx",  "export interface CartLine {"),
    ("useCart",       "function",  "src/useCart.tsx",  "export function useCart("),
    ("useCart.add",   "function",  "src/useCart.tsx",  "const add = (sku: string)"),
    ("CartViewProps", "interface", "src/CartView.tsx", "export interface CartViewProps"),
    ("LineItem",      "function",  "src/CartView.tsx", "const LineItem = ("),
    ("CartView",      "function",  "src/CartView.tsx", "export default function CartView("),
    ("APP_TITLE",     "constant",  "src/App.tsx",      "export const APP_TITLE"),
    ("App",           "function",  "src/App.tsx",      "export function App()"),
]

ABSENT = [
    ("lines", "src/useCart.tsx"),         # local destructured state
]

SEARCHES = [
    ("use cart",  {},                    "useCart"),
    ("CartView",  {},                    "CartView"),
    ("line item", {},                    "LineItem"),
    ("props",     {"kind": "interface"}, "CartViewProps"),
    ("app title", {},                    "APP_TITLE"),
    (("src/CartView.tsx", "const { lines, add } = useCart"), {}, "CartView"),
]

CALLERS = {
    "useCart": {
        ("src/CartView.tsx", "import { useCart, CartLine }",  "import"),
        ("src/CartView.tsx", "= useCart(initial)",            "call"),
    },
    "LineItem": {
        ("src/CartView.tsx", "<LineItem key=",                "call"),
    },
    "CartView": {
        ("src/App.tsx", "import CartView from",               "import"),
        ("src/App.tsx", "<CartView title=",                   "call"),
    },
}

CHAINS = {
    "useCart": {"App"},                    # CartView <- App
}

IMPORTS = {
    "src/useCart.tsx":  {"src/CartView.tsx"},
    "src/CartView.tsx": {"src/App.tsx"},
}

FRESH = ("src/App.tsx", "\nexport function FreshMarker() { return null; }\n", "FreshMarker")

KNOWN_GAPS = {}
