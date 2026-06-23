# Skill: Python

## Style

- Follow PEP 8. Use `black`-compatible formatting (88-char line limit).
- Prefer `pathlib.Path` over `os.path` for file operations.
- Use f-strings for string formatting; avoid `%` and `.format()`.
- Type-annotate all function signatures. Use `from __future__ import annotations` for forward references.
- Prefer dataclasses or named tuples for plain data containers over bare dicts.

## Patterns

- Use `contextlib.contextmanager` for resource management instead of manual try/finally.
- Prefer list/dict/set comprehensions over `map`/`filter` with lambdas.
- Use `logging` instead of `print` for diagnostic output in library code.
- Raise specific exception types; never `raise Exception("...")` directly.
- Keep functions short and single-purpose. If a function needs a docstring to explain what it does, it is probably too long.

## Testing

- Use `pytest`. Name test files `test_<module>.py`.
- Prefer `pytest.fixture` over `setUp`/`tearDown`.
- Use `tmp_path` fixture for file system tests.
- Mock at the boundary (external APIs, system calls), not inside business logic.

## Dependencies

- Prefer stdlib over third-party when the stdlib solution is not significantly more verbose.
- Pin direct dependencies in `requirements.txt`; use `requirements-dev.txt` for dev/test deps.
