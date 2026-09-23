"""Edge cases for Rust: an inline module, a generic impl with an associated
const, enum methods with match, `impl FromStr`, a trait object parameter,
closures, `pub(crate) async fn`, turbofish and a module-path call.
Format: see evals/lang/python/expect.py."""

L = "src/lib.rs"

DEFINITIONS = [
    ("inner",            "module",   L, "pub mod inner {"),
    ("inner.helper",     "function", L, "pub fn helper()"),
    ("Wrapper",          "struct",   L, "pub struct Wrapper<T>"),
    ("Wrapper.LABEL",    "constant", L, "pub const LABEL"),
    ("Wrapper.show",     "method",   L, "pub fn show(&self)"),
    ("Shape",            "enum",     L, "pub enum Shape {"),
    ("Shape.area",       "method",   L, "pub fn area(&self)"),
    ("Named",            "trait",    L, "pub trait Named {"),
    ("Named.name",       "method",   L, "fn name(&self) -> String;"),
    ("Shape.from_str",   "method",   L, "fn from_str(s: &str)"),
    ("load",             "function", L, "pub(crate) async fn load("),
    ("total",            "function", L, "pub fn total("),
]

ABSENT = [
    ("double", L),                  # a local closure
]

SEARCHES = [
    ("helper",        {},                 "inner.helper"),
    ("from str",      {},                 "Shape.from_str"),
    ("label",         {},                 "Wrapper.LABEL"),
    ("load",          {},                 "load"),
    ((L, "Shape::Square(s) => s * s"), {}, "Shape.area"),
]

CALLERS = {
    "Shape.area":   {(L, "double(s.area())", "call")},
    "inner.helper": {(L, "inner::helper() as f64", "call")},
    "Named.name":   {(L, "let _ = named.name();", "call")},
}

CHAINS = {"inner.helper": set()}

IMPORTS = {L: set()}

FRESH = (L, "\npub fn fresh_marker() -> i64 { 1 }\n", "fresh_marker")

KNOWN_GAPS = {}
