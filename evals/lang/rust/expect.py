"""Ground truth for the Rust synthetic project.  See python/expect.py for the
format.  Methods in `impl X` / `impl Trait for X` blocks belong to X."""

DEFINITIONS = [
    ("model",           "module",   "src/lib.rs",     "pub mod model;"),
    ("pricing",         "module",   "src/lib.rs",     "pub mod pricing;"),
    ("cents",           "macro",    "src/lib.rs",     "macro_rules! cents"),
    ("MAX_ITEMS",       "constant", "src/model.rs",   "pub const MAX_ITEMS"),
    ("Item",            "struct",   "src/model.rs",   "pub struct Item {"),
    ("Status",          "enum",     "src/model.rs",   "pub enum Status {"),
    ("Priced",          "trait",    "src/model.rs",   "pub trait Priced {"),
    ("Priced.cents",    "method",   "src/model.rs",   "fn cents(&self) -> i64;"),
    ("Priced.doubled",  "method",   "src/model.rs",   "fn doubled(&self)"),
    ("Item.cents",      "method",   "src/model.rs",   "fn cents(&self) -> i64 {"),
    ("Item.fmt",        "method",   "src/model.rs",   "fn fmt(&self"),
    ("Item.new",        "method",   "src/model.rs",   "pub fn new(sku"),
    ("TAX_RATE",        "constant", "src/pricing.rs", "pub static TAX_RATE"),
    ("apply_discount",  "function", "src/pricing.rs", "pub fn apply_discount("),
    ("total",           "function", "src/pricing.rs", "pub fn total<"),
    ("receipt",         "function", "src/pricing.rs", "pub fn receipt("),
    ("main",            "function", "src/main.rs",    "fn main()"),
    ("round_ten",       "function", "src/util/mod.rs", "pub fn round_ten("),
]

ABSENT = [
    ("sum",   "src/pricing.rs"),
    ("items", "src/main.rs"),
]

SEARCHES = [
    ("discount",              {},                 "apply_discount"),
    ("new item",              {},                 "Item.new"),
    ("tax",                   {},                 "TAX_RATE"),
    ("Priced",                {"kind": "trait"},  "Priced"),
    ("receipt",               {},                 "receipt"),
    ("take a percentage off", {},                 "apply_discount"),
    (("src/pricing.rs", "let sum: i64"), {},      "total"),
]

CALLERS = {
    "apply_discount": {
        ("src/lib.rs",     "pub use pricing::{apply_discount", "import"),
        ("src/pricing.rs", "apply_discount(sum, 10)",          "call"),
    },
    "receipt": {
        ("src/main.rs", "use shop::pricing::receipt", "import"),
        ("src/main.rs", "receipt(&items)",            "call"),
    },
    "Item.new": {
        ("src/main.rs", 'Item::new("tea"',            "call"),
    },
    "total": {
        ("src/pricing.rs", "total(items))",           "call"),
    },
}

CHAINS = {
    "apply_discount": {"receipt"},         # total <- receipt
}

IMPORTS = {
    "src/model.rs":   {"src/pricing.rs", "src/main.rs"},   # crate::model, shop::model
    "src/pricing.rs": {"src/lib.rs", "src/main.rs"},       # pub use pricing::, shop::pricing
    "src/util/mod.rs": {"src/pricing.rs"},                 # use crate::util:: -> util/mod.rs
}

FRESH = ("src/pricing.rs", "\npub fn fresh_marker() -> i64 { 1 }\n", "fresh_marker")

KNOWN_GAPS = {}
