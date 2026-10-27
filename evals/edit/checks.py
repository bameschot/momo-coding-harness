"""Hidden checks for the edit suite (evals/tasks.py EDIT_TASKS).

They live outside evals/edit/project, so the model never sees them.  Each check
runs a script in a fresh interpreter against the model's edited copy of the
project and passes when it exits 0; the assertion that failed is the detail.
They test behaviour (and, for the rename, the code's names), never formatting.
"""
import subprocess
import sys
from pathlib import Path

_PRELUDE = """
import ast, pathlib, sys
sys.path.insert(0, {root!r})
ROOT = pathlib.Path({root!r})
def src(rel):
    return (ROOT / rel).read_text()
"""

SCRIPTS = {
    # The discover suite (evals/tasks.py DISCOVER_TASKS): tasks whose ideal tool is
    # one /tools discover leaves on demand, plus a control that needs none.
    # shop/util.py renamed to shop/text.py, its one importer (the tests) updated.
    "rename_module": """
assert not (ROOT / "shop/util.py").exists(), "shop/util.py still exists"
from shop.text import slug, parse_duration
assert "shop.util" not in src("tests/test_shop.py"), "the tests still import shop.util"
import tests.test_shop
assert slug("A b") == "a-b"
""",
    # The unused shop/reports.py deleted; everything else still imports.
    "delete_module": """
import importlib
assert not (ROOT / "shop/reports.py").exists(), "shop/reports.py still exists"
for m in ("cart", "checkout", "inventory", "pricing", "shipping", "util"):
    importlib.import_module("shop." + m)
import tests.test_shop
""",
    # Control: a plain edit_file change.
    "slug_clean": """
from shop.util import slug
assert slug("Hello, World!") == "hello-world", slug("Hello, World!")
assert slug("A  b") == "a-b", slug("A  b")
assert slug("x2 y") == "x2-y", slug("x2 y")
""",
    # Only items_with_tag changes: [] instead of None; the other three Nones stay.
    "ambiguous_edit": """
from shop.inventory import Inventory, Item
inv = Inventory([Item("a", "A", 2.0, 3, ["red"]), Item("b", "B", 1.0, 9)])
assert inv.items_with_tag("blue") == [], inv.items_with_tag("blue")
assert [i.sku for i in inv.items_with_tag("red")] == ["a"]
assert inv.find("zz") is None, "find must still return None"
assert Inventory().cheapest() is None, "cheapest must still return None"
assert inv.low_stock(threshold=0) is None, "low_stock must still return None"
""",
    # A uk zone: 7.0 + 1.5/kg, plus 10.0 for a parcel over 20 kg.
    "nested_branch": """
from shop.shipping import Parcel, shipping_cost
assert shipping_cost([Parcel(2)], "uk") == 10.0, shipping_cost([Parcel(2)], "uk")
assert shipping_cost([Parcel(30)], "uk") == 62.0, shipping_cost([Parcel(30)], "uk")
assert shipping_cost([Parcel(20)], "uk") == 37.0, shipping_cost([Parcel(20)], "uk")
assert shipping_cost([Parcel(2)], "domestic") == 3.5
assert shipping_cost([Parcel(2)], "eu") == 7.0
assert shipping_cost([Parcel(2)], "us") == 16.0
assert shipping_cost([Parcel(0)], "uk") == 0.0
""",
    # Two changes in apply_discount: reject a negative total, never go below 0.
    "two_edits": """
from shop.checkout import apply_discount
try:
    apply_discount(-1, None)
except ValueError:
    pass
else:
    raise AssertionError("a negative total must raise ValueError")
assert apply_discount(3.0, "FIVEOFF") == 0, apply_discount(3.0, "FIVEOFF")
assert apply_discount(30.0, "FIVEOFF") == 25.0
assert apply_discount(100.0, "welcome10") == 90.0
assert apply_discount(10.0, None) == 10.0
assert apply_discount(0, "TWENTYOFF") == 0
""",
    # parse_duration: d/h/m/s to seconds, ValueError on bad input.
    "rewrite_function": """
from shop.util import parse_duration, slug
cases = {"2d": 172800, "1h30m": 5400, "45s": 45, "1d2h3m4s": 93784, "90m": 5400,
         " 2H 5M ": 7500, "0s": 0}
for text, want in cases.items():
    got = parse_duration(text)
    assert got == want, f"parse_duration({text!r}) = {got!r}, want {want}"
for bad in ("", "5x", "h", "12", "1h-5m"):
    try:
        got = parse_duration(bad)
    except ValueError:
        continue
    raise AssertionError(f"parse_duration({bad!r}) returned {got!r} instead of raising ValueError")
assert slug("A B") == "a-b"
""",
    # calc_tax -> compute_tax everywhere; Ledger.calc_tax is another thing and stays.
    "rename_across": """
from shop import pricing, cart, checkout, reports
from shop.inventory import Item
assert hasattr(pricing, "compute_tax"), "pricing.compute_tax is missing"
assert not hasattr(pricing, "calc_tax"), "pricing.calc_tax still exists"
assert pricing.compute_tax(100, "nl") == 21.0
assert pricing.gross(100, "de") == 119.0
c = cart.Cart("uk"); c.add(Item("a", "A", 10.0), 2)
assert c.tax() == 4.0 and c.total() == 24.0
assert checkout.receipt_line("x", 10, "nl") == "x: 10.00 (tax 2.10)"
led = reports.Ledger([("a", 1.0), ("b", 2.5)])
assert led.calc_tax() == 3.5, "Ledger.calc_tax must keep its name"
assert reports.tax_report([100], "nl", led) == {"owed": 21.0, "booked": 3.5, "open": 17.5}
for rel in ("shop/pricing.py", "shop/cart.py", "shop/checkout.py", "shop/reports.py"):
    tree = ast.parse(src(rel))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "calc_tax":
            raise AssertionError(f"{rel}:{node.lineno} still uses calc_tax")
        if isinstance(node, ast.alias) and node.name == "calc_tax":
            raise AssertionError(f"{rel} still imports calc_tax")
        if isinstance(node, ast.FunctionDef) and node.name == "calc_tax" and rel != "shop/reports.py":
            raise AssertionError(f"{rel}:{node.lineno} still defines calc_tax")
""",
    # format_price(amount, currency) -> '12.50 EUR'; every caller passes "EUR".
    "signature_change": """
import inspect
from shop import pricing, cart, checkout, reports
from shop.inventory import Item
assert pricing.format_price(12.5, "USD") == "12.50 USD", pricing.format_price(12.5, "USD")
p = inspect.signature(pricing.format_price).parameters
assert list(p)[:2] == ["amount", "currency"], list(p)
assert p["currency"].default is inspect.Parameter.empty, "currency must be required"
c = cart.Cart("nl"); c.add(Item("a", "A", 10.0), 1)
assert c.summary() == "1 lines, total 12.10 EUR", c.summary()
assert checkout.receipt_line("x", 10, "nl") == "x: 10.00 EUR (tax 2.10 EUR)", checkout.receipt_line("x", 10, "nl")
assert checkout.checkout(c)["text"] == "Pay 12.10 EUR", checkout.checkout(c)
assert reports.revenue_line([1, 2]) == "Revenue: 3.00 EUR", reports.revenue_line([1, 2])
""",
}


def run_check(name: str, root: Path, timeout: int = 60) -> tuple[bool, str]:
    """(passed, detail) for one task's check on the edited project at root."""
    code = _PRELUDE.format(root=str(root)) + SCRIPTS[name]
    try:
        p = subprocess.run([sys.executable, "-B", "-c", code], cwd=root, capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "check timed out"
    if p.returncode == 0:
        return True, ""
    lines = [ln for ln in (p.stderr or p.stdout).strip().splitlines() if ln.strip()]
    return False, lines[-1][:300] if lines else f"exit {p.returncode}"
