"""Edge cases for TypeScript: generic interfaces and type aliases, const enum,
a decorator, an abstract generic class with protected fields and overloaded
methods, a type guard, module augmentation, a default-exported class, `import
type`, optional receivers and optional parameters.
Format: see evals/lang/python/expect.py."""

R, U = "src/repo.ts", "src/users.ts"

DEFINITIONS = [
    ("Entity",        "interface", R, "export interface Entity {"),
    ("Id",            "type",      R, "export type Id<"),
    ("Level",         "enum",      R, "export const enum Level"),
    ("logged",        "function",  R, "function logged("),
    ("Repo",          "class",     R, "export abstract class Repo<"),
    ("Repo.find",     "method",    R, "abstract find(id: string)"),
    ("Repo.save",     "method",    R, "save(item: T): T {"),
    ("Repo.count",    "method",    R, "count(filter?: (t: T) => boolean): number {"),
    ("isEntity",      "function",  R, "export function isEntity("),
    ("User",          "interface", U, "export interface User extends Entity"),
    ("UserRepo",      "class",     U, "export default class UserRepo"),
    ("UserRepo.find", "method",    U, "find(id: string): User | undefined {"),
    ("load",          "function",  U, "export function load("),
]

ABSENT = [
    ("first", U),
    ("found", U),
]

SEARCHES = [
    ("is entity",   {},                 "isEntity"),
    ("user repo",   {},                 "UserRepo"),
    ("Level",       {"kind": "enum"},   "Level"),
    ("count",       {},                 "Repo.count"),
    ((U, "this.items.find((u)"), {},    "UserRepo.find"),
]

CALLERS = {
    # repo?: UserRepo -> the optional receiver is still a UserRepo.
    "UserRepo.find": {(U, "const found = repo?.find(first)", "call")},
    "Repo.count":    {(U, "repo!.count()", "call")},
    "isEntity":      {(U, "import { Repo, Entity, isEntity }", "import"),
                      (U, "return isEntity(found)", "call")},
}

CHAINS = {"isEntity": set()}

IMPORTS = {R: {U}, U: set()}

FRESH = (R, "\nexport const freshMarker = 1;\n", "freshMarker")

KNOWN_GAPS = {}
