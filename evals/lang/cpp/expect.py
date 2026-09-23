"""Ground truth for the C++ synthetic project.  See python/expect.py for the format.

Methods declared in a class and defined out of line (`double Circle::area() const
{...}` in a .cpp) are methods of that class; the in-class declaration without a
body is not a definition."""

H, S, M = "include/geo/shape.hpp", "src/shape.cpp", "src/main.cpp"

DEFINITIONS = [
    ("geo",              "namespace", H, "namespace geo {"),
    ("geo.detail",       "namespace", H, "namespace detail {"),
    ("geo.detail.PI",    "constant",  H, "constexpr double PI"),
    ("geo.Shape",        "class",     H, "class Shape {"),
    ("geo.Circle",       "class",     H, "class Circle : public Shape"),
    ("geo.clamp_to",     "function",  H, "T clamp_to(T v"),
    ("geo.Vec2",         "struct",    H, "struct Vec2 {"),
    ("geo.Vec2.operator+", "method",  H, "Vec2 operator+("),
    ("geo.Shape.name",   "method",    S, "std::string Shape::name() const {"),
    ("geo.Circle.Circle", "method",   S, "Circle::Circle(double r)"),
    ("geo.Circle.area",  "method",    S, "double Circle::area() const {"),
    ("geo.scale",        "function",  S, "double scale(double v, double k) {"),
    ("geo.scale",        "function",  S, "double scale(double v) {"),
    ("total_area",       "function",  M, "static double total_area("),
    ("main",             "function",  M, "int main()"),
]

ABSENT = [
    ("area",  H),     # pure virtual / override declarations
    ("scale", H),     # prototypes
    ("c",     M),     # locals
    ("v",     M),
]

SEARCHES = [
    ("area",       {"path": S},        "geo.Circle.area"),
    ("scale",      {},                 "geo.scale"),
    ("clamp",      {},                 "geo.clamp_to"),
    ("pi",         {},                 "geo.detail.PI"),
    ("Vec2",       {"kind": "struct"}, "geo.Vec2"),
    ("total area", {},                 "total_area"),
    ((S, "return detail::PI"), {},     "geo.Circle.area"),
]

CALLERS = {
    "clamp_to": {
        (S, "return clamp_to(v * k", "call"),
    },
    "scale": {
        (S, "return scale(v, 2.0)", "call"),
        (M, "geo::scale(v.x)",      "call"),
    },
    "total_area": {
        (M, "total_area(c, 3)",     "call"),
    },
    # `const geo::Shape& s; s.area()` may be a Circle: virtual dispatch.
    "Circle.area": {
        (M, "return s.area() * n;", "call"),
    },
}

CHAINS = {
    "clamp_to": {"main"},                  # geo.scale <- main (via geo::scale)
}

IMPORTS = {
    H: {S, M},
    S: set(),
}

FRESH = (M, "\nint fresh_marker() { return 1; }\n", "fresh_marker")

KNOWN_GAPS = {}
