# Skill: Python

## Core rules

- ALWAYS type-annotate function signatures. Put `from __future__ import annotations` at the top of each file.
- ALWAYS use f-strings. NEVER use `%` or `.format()`.
- ALWAYS use `pathlib.Path` for file paths, not `os.path`.
- Use a `@dataclass` for plain data. Do NOT pass around bare dicts with fixed keys.
- Catch specific exceptions (`except ValueError:`). NEVER use a bare `except:` or `except Exception: pass`.
- Compare to None with `is` / `is not`, never `==`.
- Follow PEP 8, 88-char lines (black style).
- Prefer the stdlib over third-party packages when the stdlib is not much more verbose.

## Errors

- Raise a specific exception type; subclass a project base exception for your own errors.
- When re-raising after catching, use `raise NewError(...) from original`.
- Use `with` (context managers) for files, locks, and connections — never open without closing.

```python
try:
    data = json.loads(raw)
except json.JSONDecodeError as e:
    raise ValidationError("invalid JSON") from e
```

## Common mistakes (these are the ones models get wrong)

```python
# WRONG — mutable default is shared across all calls
def add(item, lst=[]):
    lst.append(item)
    return lst

# CORRECT
def add(item, lst=None):
    if lst is None:
        lst = []
    lst.append(item)
    return lst

# WRONG — late-binding closure: i is read at call time
funcs = [lambda x: x + i for i in range(3)]  # all use i == 2

# CORRECT — bind now with a default arg
funcs = [lambda x, i=i: x + i for i in range(3)]

# WRONG — mutating a list while iterating it skips elements
for item in lst:
    if drop(item):
        lst.remove(item)

# CORRECT — build a new list
lst = [item for item in lst if not drop(item)]
```

## Testing

- Use `pytest`. Name tests for the behaviour: `test_returns_empty_when_no_match`.
- Use `@pytest.mark.parametrize` for multiple input/output cases.
- Use `pytest.raises(ValueError, match="...")` to assert on errors.
- Use the `tmp_path` fixture for filesystem tests. Mock only at the boundary (network, clock), not your own logic.

## Dependencies

- Declare deps in `pyproject.toml`; commit a lockfile.
- Use `uv` (`uv add`, `uv sync`) or `python -m venv .venv && source .venv/bin/activate`.
