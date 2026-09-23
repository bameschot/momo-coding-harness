"""Edge cases for TSX: forwardRef and memo components, a generic component, a
React.FC-typed arrow component, context creation and use, fragments, a
default export wrapped in memo(), and <Context.Provider>.
Format: see evals/lang/python/expect.py."""

W, P = "src/Widgets.tsx", "src/Page.tsx"

DEFINITIONS = [
    ("ThemeContext", "variable",  W, "export const ThemeContext"),
    ("Input",        "function",  W, "export const Input = forwardRef"),
    ("ListProps",    "type",      W, "type ListProps<T>"),
    ("List",         "function",  W, "export function List<T>"),
    ("Badge",        "function",  W, "const Badge: React.FC"),
    ("Card",         "function",  W, "function Card("),
    ("Page",         "function",  P, "export function Page("),
]

ABSENT = [
    ("theme", W),
]

SEARCHES = [
    ("theme context", {},              "ThemeContext"),
    ("input",         {},              "Input"),
    ("badge",         {},              "Badge"),
    ("list",          {"path": W},     "List"),
    ((W, "<Badge text={title} />"), {}, "Card"),
]

CALLERS = {
    "Badge": {(W, "<Badge text={title} />", "call")},
    "Input": {(W, '<Input label="name" />', "call")},
    "List":  {(P, "import Card, { List, ThemeContext }", "import"),
              (P, "<List items={names}", "call")},
    "ThemeContext": {(W, "useContext(ThemeContext)", "other"),
                     (P, "import Card, { List, ThemeContext }", "import"),
                     (P, '<ThemeContext.Provider value="dark">', "other")},
}

CHAINS = {"Badge": {"Page"}}          # Badge <- Card <- Page (as <Card/>)

IMPORTS = {W: {P}, P: set()}

FRESH = (W, "\nexport function FreshMarker() { return null; }\n", "FreshMarker")

KNOWN_GAPS = {}
