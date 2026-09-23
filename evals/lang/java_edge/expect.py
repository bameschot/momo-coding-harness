"""Edge cases for Java: abstract methods, constructor chaining this(...)/super(),
generic static methods, enhanced-for variables with a declared type, lambdas,
anonymous classes, switch pattern matching and static calls on a class.
Format: see evals/lang/python/expect.py."""

P = "org/demo/"

DEFINITIONS = [
    ("Shape",            "class",       P + "Shape.java",  "public abstract class Shape"),
    ("Shape.area",       "method",      P + "Shape.java",  "public abstract double area()"),
    ("Shape.unit",       "method",      P + "Shape.java",  "public static Shape unit()"),
    ("Shape.doubled",    "method",      P + "Shape.java",  "public double doubled()"),
    ("Square",           "class",       P + "Square.java", "public class Square extends Shape"),
    ("Square.Square",    "constructor", P + "Square.java", "public Square(double side)"),
    ("Square.Square",    "constructor", P + "Square.java", "private Square(double side, boolean checked)"),
    ("Square.area",      "method",      P + "Square.java", "public double area() {"),
    ("Square.sum",       "method",      P + "Square.java", "double sum(List<T> shapes)"),
    ("Square.biggest",   "method",      P + "Square.java", "public static double biggest("),
    ("Report",           "class",       P + "Report.java", "public class Report"),
    ("Report.describe",  "method",      P + "Report.java", "public String describe("),
    ("Report.total",     "method",      P + "Report.java", "public double total("),
]

ABSENT = [
    ("s",      P + "Square.java"),
    ("areaOf", P + "Square.java"),
    ("t",      P + "Report.java"),
]

SEARCHES = [
    ("biggest square", {},                             "Square.biggest"),
    ("unit",           {},                             "Shape.unit"),
    ("area",           {"path": P + "Square.java"},    "Square.area"),
    ((P + "Report.java", "t += sq.area()"), {},       "Report.total"),
]

CALLERS = {
    # Square.area: through Square-typed variables (lambda param sq is
    # untyped — kept), not through a generic T or a Shape.
    "Square.area": {
        (P + "Square.java", "areaOf = sq -> sq.area()",         "call"),
        (P + "Report.java", 'case Square sq -> "square " + sq.area()', "call"),
        (P + "Report.java", "t += sq.area()",                  "call"),
        (P + "Square.java", "s += shape.area()",               "call"),   # T extends Shape: unknown, kept
    },
    "Shape.doubled": {
        (P + "Report.java", 'default -> "shape " + shape.doubled()', "call"),
    },
    "Square.sum": {
        (P + "Report.java", "Square.sum(squares)",             "call"),
    },
    "Shape.unit": {
        (P + "Report.java", "Shape.unit().area()",             "call"),
    },
}

CHAINS = {
    "Shape.unit": set(),
}

IMPORTS = {
    P + "Shape.java":  set(),        # same package: no import statement
    P + "Square.java": set(),
}

FRESH = (P + "Report.java", "\nclass FreshMarker {}\n", "FreshMarker")

# Not a failure, but it lowers caller precision: in `Shape.unit().area()` the
# receiver's type is Shape.unit()'s declared return type.  Reading return types
# across files is not done, so the call stays in Square.area's results.
KNOWN_GAPS = {}
