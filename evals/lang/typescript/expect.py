"""Ground truth for the TypeScript synthetic project.  See python/expect.py for
the format.  Overload signatures are declarations; the implementation is the
definition the anchor names (the signatures may be indexed too)."""

DEFINITIONS = [
    ("Product",           "interface", "src/types.ts",   "export interface Product {"),
    ("PriceMap",          "type",      "src/types.ts",   "export type PriceMap"),
    ("Currency",          "enum",      "src/types.ts",   "export enum Currency {"),
    ("Rates",             "namespace", "src/types.ts",   "export namespace Rates {"),
    ("Rates.DEFAULT",     "constant",  "src/types.ts",   "export const DEFAULT = 1"),
    ("Rates.lookup",      "function",  "src/types.ts",   "export function lookup("),
    ("Pricer",            "class",     "src/pricing.ts", "export abstract class Pricer"),
    ("Pricer.price",      "method",    "src/pricing.ts", "abstract price(p: Product)"),
    ("Pricer.describe",   "method",    "src/pricing.ts", "describe(p: Product): string {"),
    ("convert",           "function",  "src/pricing.ts", "to: Currency = Currency.EUR"),
    ("FlatPricer",        "class",     "src/pricing.ts", "export class FlatPricer"),
    ("FlatPricer.price",  "method",    "src/pricing.ts", "price(p: Product): number {"),
    ("table",             "variable",  "src/pricing.ts", "export const table"),
    ("tea",               "variable",  "src/main.ts",    "const tea: Product"),
    ("run",               "function",  "src/main.ts",    "export function run()"),
]

ABSENT = []

SEARCHES = [
    ("convert",     {},                 "convert"),
    ("describe",    {},                 "Pricer.describe"),
    ("Currency",    {"kind": "enum"},   "Currency"),
    ("price map",   {},                 "PriceMap"),
    ("lookup rate", {},                 "Rates.lookup"),
    (("src/pricing.ts", "return cents * Rates.lookup"), {}, "convert"),
]

CALLERS = {
    "convert": {
        ("src/pricing.ts", "return convert(p.cents)",              "call"),
        ("src/index.ts",   "export { FlatPricer, convert } from",  "import"),
        ("src/main.ts",    "import { FlatPricer, convert }",       "import"),
        ("src/main.ts",    "+ convert(5)",                         "call"),
    },
    # Called through the subclass: `new FlatPricer()` is a Pricer, so it stays.
    "Pricer.describe": {
        ("src/main.ts",    "new FlatPricer().describe(tea)",       "call"),
    },
    "Rates.lookup": {
        ("src/pricing.ts", "Rates.lookup(to)",                     "call"),
    },
}

CHAINS = {
    "lookup": {"run"},                     # convert <- run
}

IMPORTS = {
    "src/types.ts":   {"src/pricing.ts", "src/index.ts", "src/main.ts"},
    "src/pricing.ts": {"src/index.ts"},
    "src/index.ts":   {"src/main.ts"},
}

FRESH = ("src/types.ts", "\nexport const freshMarker = 1;\n", "freshMarker")

KNOWN_GAPS = {}
