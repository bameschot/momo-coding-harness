"""Edge cases for C++: a class template with out-of-line member definitions
(`Box<T>::put`), a destructor, a nested class member defined out of line,
a static factory, `enum class`, `extern "C"` and a lambda.
Format: see evals/lang/python/expect.py."""

H, S = "box.hpp", "box.cpp"

DEFINITIONS = [
    ("Box",            "class",    H, "class Box {"),
    ("Box.Iter",       "class",    H, "class Iter {"),
    ("Color",          "enum",     H, "enum class Color"),
    ("Color.Red",      "constant", H, "enum class Color"),
    ("Box.put",        "method",   S, "void Box<T>::put(T v) {"),
    ("Box.take",       "method",   S, "T Box<T>::take() {"),
    ("Box.~Box",       "method",   S, "Box<T>::~Box() {}"),
    ("Box.Iter.done",  "method",   S, "bool Box<T>::Iter::done() const {"),
    ("Box.make",       "method",   S, "Box<T> Box<T>::make() {"),
    ("c_api_version",  "function", S, 'extern "C" int c_api_version(void) {'),
    ("use_box",        "function", S, "int use_box() {"),
]

ABSENT = [
    ("put",   H),                   # in-class declaration
    ("b",     S),
    ("twice", S),                   # a local lambda
]

SEARCHES = [
    ("put",        {"path": S},      "Box.put"),
    ("make box",   {},               "Box.make"),
    ("api version", {},              "c_api_version"),
    ("Color",      {"kind": "enum"}, "Color"),
    ((S, "items_.pop_back()"), {},   "Box.take"),
]

CALLERS = {
    "Box.put":  {(S, "b.put(1);",                        "call")},
    "Box.take": {(S, "return twice(b.take())",           "call")},
    "Box.make": {(S, "Box<int> b = Box<int>::make();",   "call")},
}

CHAINS = {"Box.make": set()}

IMPORTS = {H: {S}, S: set()}

FRESH = (S, "\nint fresh_marker() { return 1; }\n", "fresh_marker")

KNOWN_GAPS = {}
