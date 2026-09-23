"""Edge cases for Kotlin: typealias, enum class with a constructor and
methods, extension property and generic extension function, open/override,
init block, secondary constructor, inner class, `when` with smart casts,
destructuring declarations, scope functions and an aliased import.
Format: see evals/lang/python/expect.py."""

S, M = "demo/Shapes.kt", "demo/Main.kt"

DEFINITIONS = [
    ("Area",               "type",     S, "typealias Area"),
    ("Unit",               "enum",     S, "enum class Unit("),
    ("Unit.CM",            "constant", S, "CM(1.0), INCH(2.54)"),
    ("Unit.toCm",          "method",   S, "fun toCm("),
    ("words",              "variable", S, "val String.words"),
    ("second",             "function", S, "fun <T> List<T>.second()"),
    ("Shape",              "class",    S, "open class Shape("),
    ("Shape.area",         "method",   S, "open fun area()"),
    ("Square",             "class",    S, "class Square(val side"),
    ("Square.area",        "method",   S, "override fun area()"),
    ("Square.Scaler",      "class",    S, "inner class Scaler"),
    ("Square.Scaler.scaled", "method", S, "fun scaled("),
    ("describe",           "function", S, "fun describe("),
    ("biggest",            "function", S, "fun biggest("),
    ("main",               "function", M, "fun main()"),
]

ABSENT = [
    ("first", S),                   # destructured local
    ("sq",    M),
    ("items", M),
]

SEARCHES = [
    ("to cm",      {},                "Unit.toCm"),
    ("second",     {},                "second"),
    ("words",      {},                "words"),
    ("Scaler",     {"kind": "class"}, "Square.Scaler"),
    ((S, "is Square ->"), {},         "describe"),
]

CALLERS = {
    "describe": {(M, "println(describe(sq)", "call")},
    "second":   {(M, "items.second().area()", "call")},
    "Unit.toCm": {(M, "Unit.CM.toCm(2.0)",   "call")},
}

CHAINS = {"describe": set()}

IMPORTS = {S: {M}, M: set()}

FRESH = (S, "\nfun freshMarker() = 1\n", "freshMarker")

KNOWN_GAPS = {}
