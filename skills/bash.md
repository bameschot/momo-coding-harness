# Skill: Bash / Shell Scripting

## Core rules

- Start every non-trivial script with `set -euo pipefail`.
- ALWAYS double-quote expansions: `"$var"`, `"${array[@]}"`. Unquoted variables break on spaces and globs.
- Use `$(command)`, never backticks. Use `local` for variables inside functions.
- Use `[[ ]]`, not `[ ]` (fewer surprises). Use `(( ))` for arithmetic.
- Use `${var:-default}` for defaults, `${var:?must be set}` to require a value.
- Send errors to stderr: `echo "ERROR: ..." >&2`.
- Use `#!/usr/bin/env bash`. Check tools with `command -v tool`, not `which`.
- Clean up temp files with a trap: `trap 'rm -f "$tmp"' EXIT`.

```bash
set -euo pipefail
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
```

## Loops and reading

```bash
# Read a file line by line safely
while IFS= read -r line; do
    echo "$line"
done < file.txt

# Iterate an array (quote it!)
files=("a.txt" "b.txt")
for f in "${files[@]}"; do echo "$f"; done
```

## Common mistakes

```bash
# WRONG — piping into while runs the loop in a subshell; count is lost
find . -name '*.log' | while read -r f; do count=$((count+1)); done
echo "$count"   # 0

# CORRECT — process substitution keeps the loop in the current shell
while IFS= read -r f; do count=$((count+1)); done < <(find . -name '*.log')
echo "$count"
```

## Error handling

```bash
if ! some_command; then
    echo "ERROR: it failed" >&2
    exit 1
fi
```

## Debugging & portability

- `set -x` / `set +x` to trace; `bash -n script.sh` to syntax-check.
- macOS system bash is 3.2 — avoid `declare -A` and `mapfile` if the script must run there, or use Python instead.
- For strict POSIX (`#!/bin/sh`), avoid `[[ ]]`, `$(( ))`, and arrays.
