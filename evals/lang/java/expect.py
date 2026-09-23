"""Ground truth for the Java synthetic project.  See python/expect.py for the format.

Java imports name classes and members, not files; a same-package class needs no
import at all, so IMPORTS lists only explicit import statements."""

P = "com/acme/shop/"

DEFINITIONS = [
    ("Money",                  "record",      P + "Money.java",    "public record Money("),
    ("Money.DEFAULT_CURRENCY", "constant",    P + "Money.java",    "DEFAULT_CURRENCY ="),
    ("Money.add",              "method",      P + "Money.java",    "public Money add(Money other)"),
    ("Money.of",               "method",      P + "Money.java",    "public static Money of("),
    ("Cart",                   "class",       P + "Cart.java",     "public class Cart {"),
    ("Cart.MAX_LINES",         "constant",    P + "Cart.java",     "MAX_LINES = 50"),
    ("Cart.Line",              "class",       P + "Cart.java",     "public static class Line"),
    ("Cart.Line.Line",         "constructor", P + "Cart.java",     "Line(String sku, Money price) {"),
    ("Cart.Cursor",            "class",       P + "Cart.java",     "public class Cursor"),
    ("Cart.Cursor.hasNext",    "method",      P + "Cart.java",     "boolean hasNext()"),
    ("Cart.add",               "method",      P + "Cart.java",     "public void add(String sku, Money price)"),
    ("Cart.add",               "method",      P + "Cart.java",     "public void add(String sku, long cents)"),
    ("Cart.total",             "method",      P + "Cart.java",     "public Money total()"),
    ("Status",                 "enum",        P + "Status.java",   "public enum Status"),
    ("Status.OPEN",            "constant",    P + "Status.java",   "OPEN, PAID, SHIPPED"),
    ("Status.isFinal",         "method",      P + "Status.java",   "public boolean isFinal()"),
    ("Discount",               "interface",   P + "Discount.java", "public interface Discount"),
    ("Discount.PERCENT_CAP",   "constant",    P + "Discount.java", "PERCENT_CAP = 90"),
    ("Discount.apply",         "method",      P + "Discount.java", "Money apply(Money price);"),
    ("Discount.applyTwice",    "method",      P + "Discount.java", "default Money applyTwice("),
    ("Checkout",               "class",       P + "Checkout.java", "public class Checkout {"),
    ("Checkout.Checkout",      "constructor", P + "Checkout.java", "public Checkout(Discount discount)"),
    ("Checkout.pay",           "method",      P + "Checkout.java", "Money pay(T cart)"),
    ("Checkout.settle",        "method",      P + "Checkout.java", "public Status settle("),
]

ABSENT = [
    ("due",   P + "Checkout.java"),       # local variable
    ("lines", P + "Cart.java"),           # instance field, not a constant
    ("pos",   P + "Cart.java"),
]

SEARCHES = [
    ("add",         {"path": P + "Cart.java"}, "Cart.add"),
    ("total",       {},                        "Cart.total"),
    ("apply twice", {},                        "Discount.applyTwice"),
    ("max lines",   {},                        "Cart.MAX_LINES"),
    ("Line",        {"kind": "class"},         "Cart.Line"),
    ("is final",    {},                        "Status.isFinal"),
    ("paid",        {},                        "Status.PAID"),
    ((P + "Checkout.java", "return discount.applyTwice"), {}, "Checkout.pay"),
]

CALLERS = {
    "Cart.total": {
        (P + "Checkout.java", "Money due = cart.total()", "call"),
        ("com/acme/app/Main.java", "println(cart.total())", "call"),
    },
    "applyTwice": {
        (P + "Checkout.java", "return discount.applyTwice(due)", "call"),
    },
    "Money.of": {
        (P + "Cart.java", "import static com.acme.shop.Money.of", "import"),
        (P + "Cart.java", "add(sku, of(cents))",                  "call"),
        (P + "Cart.java", "reduce(of(0), Money::add)",           "call"),
    },
    # A method reference; the two Cart.add call sites are a different method.
    "Money.add": {
        (P + "Cart.java", "reduce(of(0), Money::add)",           "other"),
    },
}

CHAINS = {
    "applyTwice": {"settle"},              # Checkout.pay <- Checkout.settle
}

A = "com/acme/app/Main.java"     # another package: `import com.acme.shop.*`

IMPORTS = {
    P + "Money.java":  {P + "Cart.java", A},   # import static ...Money.of; the wildcard
    P + "Cart.java":   {P + "Checkout.java", A},  # import ...Cart.Line; the wildcard
    P + "Status.java": {A},
    A:                 set(),
}

FRESH = (P + "Status.java", "\nclass FreshMarker {}\n", "FreshMarker")

KNOWN_GAPS = {}
