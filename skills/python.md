# Skill: Python

## Style

- Follow PEP 8. Use `black`-compatible formatting (88-char line limit).
- Prefer `pathlib.Path` over `os.path` for file operations.
- Use f-strings for string formatting; avoid `%` and `.format()`.
- Type-annotate all function signatures. Use `from __future__ import annotations` at the top of every file for forward references and postponed evaluation.
- Prefer dataclasses or named tuples for plain data containers over bare dicts.

## Type annotations in depth

```python
from __future__ import annotations
from typing import TypeVar, Generic, Protocol, TypedDict, Literal, overload
from typing import runtime_checkable
from collections.abc import Callable, Iterator, Generator, Sequence

T = TypeVar("T")
K = TypeVar("K")
V = TypeVar("V")

# TypedDict — dict with known keys and types
class Config(TypedDict):
    host: str
    port: int
    debug: bool

# Protocol — structural typing (duck typing with type safety)
@runtime_checkable
class Closeable(Protocol):
    def close(self) -> None: ...

def cleanup(resource: Closeable) -> None:
    resource.close()

# Generic class
class Stack(Generic[T]):
    def __init__(self) -> None:
        self._items: list[T] = []
    def push(self, item: T) -> None:
        self._items.append(item)
    def pop(self) -> T:
        return self._items.pop()

# overload — multiple signatures for one function
@overload
def process(data: str) -> str: ...
@overload
def process(data: bytes) -> bytes: ...
def process(data: str | bytes) -> str | bytes:
    if isinstance(data, bytes):
        return data.decode()
    return data.upper()

# Literal — restrict values to a specific set
Mode = Literal["read", "write", "append"]
def open_file(path: str, mode: Mode) -> None: ...
```

## Generators and itertools

```python
import itertools
from collections.abc import Iterator

# Generator function — lazy, memory-efficient alternative to lists
def read_chunks(path: str, size: int = 8192) -> Iterator[bytes]:
    with open(path, "rb") as f:
        while chunk := f.read(size):
            yield chunk

# Generator expression (no memory allocated until iterated)
squares = (x * x for x in range(1000))

# itertools recipes
pairs = list(itertools.combinations(["a", "b", "c"], 2))
grouped = {
    k: list(v)
    for k, v in itertools.groupby(sorted_data, key=lambda x: x["type"])
}
chained = list(itertools.chain(list1, list2, list3))
batched = list(itertools.batched(items, 100))  # Python 3.12+

# zip with strict=True to catch length mismatch (Python 3.10+)
for a, b in zip(list_a, list_b, strict=True):
    ...

# enumerate with start offset
for i, item in enumerate(items, start=1):
    print(f"{i}: {item}")
```

## Decorators

```python
import functools
import time
from typing import ParamSpec

P = ParamSpec("P")
R = TypeVar("R")

# Always use functools.wraps to preserve the wrapped function's metadata
def timer(fn: Callable[P, R]) -> Callable[P, R]:
    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        start = time.perf_counter()
        result = fn(*args, **kwargs)
        print(f"{fn.__name__} took {time.perf_counter() - start:.3f}s")
        return result
    return wrapper

# Decorator factory (returns a decorator)
def retry(times: int = 3, delay: float = 1.0) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorator(fn: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            for attempt in range(times):
                try:
                    return fn(*args, **kwargs)
                except Exception:
                    if attempt == times - 1:
                        raise
                    time.sleep(delay)
            raise RuntimeError("unreachable")
        return wrapper
    return decorator

# Caching
@functools.cache  # unbounded memoization, Python 3.9+
def fib(n: int) -> int:
    if n < 2:
        return n
    return fib(n - 1) + fib(n - 2)

@functools.lru_cache(maxsize=128)  # bounded cache with LRU eviction
def expensive(n: int) -> int: ...
```

## Class design

```python
from dataclasses import dataclass, field

@dataclass
class Point:
    x: float
    y: float

    def distance_to(self, other: Point) -> float:
        return ((self.x - other.x)**2 + (self.y - other.y)**2) ** 0.5

# Defaults and validation in dataclass
@dataclass
class Config:
    host: str = "localhost"
    port: int = 8080
    tags: list[str] = field(default_factory=list)  # NEVER use mutable defaults directly

    def __post_init__(self) -> None:
        if self.port <= 0:
            raise ValueError(f"invalid port: {self.port}")

# frozen=True — immutable and hashable, usable as dict key or in set
@dataclass(frozen=True)
class Color:
    r: int
    g: int
    b: int

# __slots__ — reduces per-instance memory, prevents accidental attribute creation
class Node:
    __slots__ = ("value", "next")

    def __init__(self, value: int) -> None:
        self.value = value
        self.next: Node | None = None

# Properties — computed attributes and validated setters
class Temperature:
    def __init__(self, celsius: float) -> None:
        self._celsius = celsius

    @property
    def celsius(self) -> float:
        return self._celsius

    @celsius.setter
    def celsius(self, value: float) -> None:
        if value < -273.15:
            raise ValueError("below absolute zero")
        self._celsius = value

    @property
    def fahrenheit(self) -> float:
        return self._celsius * 9 / 5 + 32
```

## Error handling

```python
# Custom exception hierarchy — subclass from a project base exception
class AppError(Exception):
    """Base for all application errors."""

class NotFoundError(AppError):
    def __init__(self, resource: str, id: int) -> None:
        super().__init__(f"{resource} {id} not found")
        self.resource = resource
        self.id = id

class ValidationError(AppError):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field

# Exception chaining — always use `raise X from Y` when wrapping
try:
    data = json.loads(raw)
except json.JSONDecodeError as e:
    raise ValidationError("body", "invalid JSON") from e

# contextmanager — clean resource management
from contextlib import contextmanager, suppress

@contextmanager
def managed_connection(url: str):
    conn = connect(url)
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        conn.close()

# suppress — silently ignore expected exceptions
with suppress(FileNotFoundError):
    os.remove("temp.txt")
```

## Async patterns

```python
import asyncio
from collections.abc import AsyncIterator

# Parallel async requests
async def fetch_all(urls: list[str]) -> list[str]:
    return await asyncio.gather(*[fetch(url) for url in urls])

# TaskGroup (Python 3.11+) — structured concurrency with automatic error propagation
async def fetch_all_v2(urls: list[str]) -> list[str]:
    async with asyncio.TaskGroup() as tg:
        tasks = [tg.create_task(fetch(url)) for url in urls]
    return [t.result() for t in tasks]

# Async generator
async def paginate(url: str) -> AsyncIterator[dict]:
    page = 1
    while True:
        items = await fetch_json(f"{url}?page={page}")
        if not items:
            break
        for item in items:
            yield item
        page += 1

# Timeout (Python 3.11+)
async def fetch_with_timeout(url: str) -> str:
    async with asyncio.timeout(5.0):
        return await fetch(url)

# Semaphore — limit concurrency
async def fetch_limited(urls: list[str], max_concurrent: int = 10) -> list[str]:
    sem = asyncio.Semaphore(max_concurrent)
    async def _fetch(url: str) -> str:
        async with sem:
            return await fetch(url)
    return await asyncio.gather(*[_fetch(u) for u in urls])
```

## Common stdlib: collections

```python
from collections import defaultdict, Counter, deque, ChainMap
import heapq, functools, operator

# defaultdict — no KeyError on missing keys
graph: dict[str, list[str]] = defaultdict(list)
graph["a"].append("b")

# Counter — count hashable items, supports arithmetic
words = Counter("the quick brown fox".split())
most_common = words.most_common(3)
words.update(["the", "fox"])

# deque — O(1) append/pop from both ends; maxlen makes a ring buffer
queue: deque[str] = deque(maxlen=100)
queue.appendleft("first")
queue.append("last")

# heapq — min-heap on a regular list
heap: list[int] = []
heapq.heappush(heap, 5)
heapq.heappush(heap, 1)
smallest = heapq.heappop(heap)  # 1
top3 = heapq.nsmallest(3, items, key=lambda x: x.score)

# ChainMap — layered lookup without copying dicts
defaults = {"color": "blue", "size": 10}
overrides = {"color": "red"}
merged = ChainMap(overrides, defaults)
merged["color"]  # "red"  — checks overrides first
merged["size"]   # 10     — falls through to defaults

# partial — fix some arguments of a function
double = functools.partial(operator.mul, 2)
add5 = functools.partial(operator.add, 5)
```

## Testing

```python
import pytest
from unittest.mock import patch, MagicMock

# Parametrize — test multiple cases without repeating code
@pytest.mark.parametrize("input,expected", [
    ("hello", "HELLO"),
    ("", ""),
    ("123", "123"),
])
def test_upper(input: str, expected: str) -> None:
    assert input.upper() == expected

# Session-scoped fixture — created once for the whole test session
@pytest.fixture(scope="session")
def db():
    conn = create_test_db()
    yield conn
    conn.close()

# Expect a specific exception
def test_raises() -> None:
    with pytest.raises(ValueError, match="invalid port"):
        Config(port=-1)

# Patch an external dependency
def test_fetch() -> None:
    with patch("mymodule.requests.get") as mock_get:
        mock_get.return_value.json.return_value = {"status": "ok"}
        result = fetch_status("http://example.com")
    assert result == "ok"
    mock_get.assert_called_once_with("http://example.com")

# tmp_path fixture for filesystem tests (no cleanup needed)
def test_write(tmp_path) -> None:
    f = tmp_path / "out.txt"
    write_report(f)
    assert f.read_text() == "expected content"
```

## Common pitfalls

```python
# WRONG — mutable default argument is shared across all calls
def append_to(item, lst=[]):
    lst.append(item)
    return lst  # lst persists between calls!

# CORRECT
def append_to(item, lst=None):
    if lst is None:
        lst = []
    lst.append(item)
    return lst

# WRONG — late binding closure: i is looked up at call time, not definition time
funcs = [lambda x: x + i for i in range(3)]
funcs[0](0)  # returns 2, not 0

# CORRECT — bind at definition time with default argument
funcs = [lambda x, i=i: x + i for i in range(3)]

# WRONG — modifying a list while iterating it skips elements
for item in lst:
    if condition(item):
        lst.remove(item)

# CORRECT — filter into a new list
lst = [item for item in lst if not condition(item)]

# WRONG — broad except swallows bugs
try:
    result = risky()
except Exception:
    pass

# CORRECT — catch what you expect, handle specifically
try:
    result = risky()
except (ValueError, OSError) as e:
    log.warning("expected failure: %s", e)
    result = default_value

# None checks: use `is` (None is a singleton)
if x is None: ...     # correct
if x is not None: ... # correct
if x == None: ...     # works but wrong idiom
```

## Dependencies

- Prefer stdlib over third-party when the stdlib solution is not significantly more verbose.
- Declare dependencies in `pyproject.toml`; use a lockfile (`uv.lock`, `poetry.lock`) for reproducibility.
- Use `uv` for fast, deterministic installs: `uv add requests`, `uv sync`.
- Virtual environments: `uv venv` or `python -m venv .venv && source .venv/bin/activate`.
