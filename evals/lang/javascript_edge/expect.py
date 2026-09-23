"""Edge cases for JavaScript: private #fields and #methods, getters/setters,
static fields and methods, generator methods, object-literal methods, class
expressions, a constructor function with prototype methods, CommonJS
`exports.x = function`, optional chaining calls and an IIFE.
Format: see evals/lang/python/expect.py."""

S, L, M = "lib/store.js", "lib/legacy.js", "lib/main.js"

DEFINITIONS = [
    ("Store",             "class",    S, "export class Store {"),
    ("Store.instances",   "variable", S, "static instances = 0"),
    ("Store.size",        "method",   S, "get size()"),
    ("Store.limit",       "method",   S, "set limit(n)"),
    ("Store.#touch",      "method",   S, "#touch(key) {"),
    ("Store.save",        "method",   S, "save(key, value) {"),
    ("Store.create",      "method",   S, "static create()"),
    ("Store.keys",        "method",   S, "*keys()"),
    ("api",               "variable", S, "export const api = {"),
    ("api.fetchAll",      "method",   S, "fetchAll() {"),
    ("api.remove",        "method",   S, "remove: (id) => id"),
    ("Base",              "class",    S, "export const Base = class"),
    ("Base.hello",        "method",   S, "hello() {"),
    ("Counter",           "function", L, "function Counter(start)"),
    ("Counter.increment", "method",   L, "Counter.prototype.increment ="),
    ("makeCounter",       "function", L, "exports.makeCounter ="),
    ("resetAll",          "function", L, "module.exports.resetAll ="),
    ("store",             "variable", M, "const store = Store.create()"),
    ("boot",              "function", M, "(function boot()"),
]

ABSENT = [
    ("counter",  L),
]

SEARCHES = [
    ("create store", {},               "Store.create"),
    ("fetch all",    {},               "api.fetchAll"),
    ("increment",    {},               "Counter.increment"),
    ("make counter", {},               "makeCounter"),
    ((S, "yield* this.#items.keys()"), {}, "Store.keys"),
]

CALLERS = {
    "Store.save":        {(M, 'store.save("a", 1)?.save("b", 2)', "call")},
    "Store.create":      {(M, "const store = Store.create()", "call")},
    "makeCounter":       {(M, "const { makeCounter } = require", "import"),
                          (M, "const counter = makeCounter(3)", "call")},
    "api.fetchAll":      {(M, "api.fetchAll()", "call")},
    # The block's `const total` ends with the block; `var total` would not.
    "total":             {("lib/scope.js", "return total(xs);", "call")},
}

CHAINS = {"Store.create": set()}

IMPORTS = {S: {M}, L: {M}, M: set(),
           "vendor/store.js": set()}    # same name, other folder: "./store.js" is not it

FRESH = (S, "\nexport function freshMarker() { return 1; }\n", "freshMarker")

KNOWN_GAPS = {}
