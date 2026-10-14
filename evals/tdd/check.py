"""The hidden acceptance check for the TDD bench: what the finished project must
do, in cases the model never sees.  Run in a subprocess by tdd_bench.py, so the
model's code cannot hang or crash the bench.

    python evals/tdd/check.py <project dir>   →  {"passed": n, "total": 8, "failures": {...}}
"""
import json
import sys

sys.path.insert(0, sys.argv[1])
results = []


def check(name, fn):
    try:
        fn()
        results.append((name, None))
    except Exception as e:  # noqa: BLE001 — any failure is a result
        results.append((name, f"{type(e).__name__}: {e}"))


def expect_raises(exc, fn):
    try:
        fn()
    except exc:
        return
    raise AssertionError(f"{exc.__name__} not raised")


from shop.inventory import Inventory  # noqa: E402


def inv(**stock):
    i = Inventory()
    for k, v in stock.items():
        i.add(k, v)
    return i


def t_remove_decreases():
    i = inv(apple=5)
    i.remove("apple", 2)
    assert i.count("apple") == 3, i.count("apple")


def t_remove_to_zero_drops():
    i = inv(apple=2)
    i.remove("apple", 2)
    assert i.count("apple") == 0 and "apple" not in i.names(), i.names()


def t_remove_too_many():
    i = inv(apple=2)
    expect_raises(ValueError, lambda: i.remove("apple", 3))
    assert i.count("apple") == 2, "stock changed on a failed remove"


def t_remove_unknown():
    expect_raises(ValueError, lambda: Inventory().remove("pear", 1))


def t_remove_non_positive():
    i = inv(apple=2)
    expect_raises(ValueError, lambda: i.remove("apple", 0))
    expect_raises(ValueError, lambda: i.remove("apple", -1))


def t_low_stock():
    i = inv(pear=1, apple=2, fig=10)
    assert i.low_stock(3) == ["apple", "pear"], i.low_stock(3)


def t_low_stock_strict():
    assert inv(apple=3).low_stock(3) == [], "count equal to threshold is not low"


def t_low_stock_empty():
    assert Inventory().low_stock(5) == []


for name, fn in list(globals().items()):
    if name.startswith("t_"):
        check(name, fn)
print(json.dumps({"passed": sum(e is None for _, e in results), "total": len(results),
                  "failures": {n: e for n, e in results if e}}))
