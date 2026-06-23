# Skill: Bash / Shell Scripting

## Safety defaults

Start every non-trivial script with:
```bash
set -euo pipefail
```
- `-e`: exit immediately on error
- `-u`: treat unset variables as an error
- `-o pipefail`: a pipe fails if any command in it fails (not just the last)

Use `IFS=$'\n\t'` when word-splitting safety matters.

## Variables and quoting

- Always double-quote variables: `"$var"`, `"${array[@]}"`. Unquoted variables break on spaces and glob characters.
- Use `${var:-default}` for safe defaults and `${var:?must be set}` to enforce required variables.
- Prefer `$(command)` over backticks for command substitution — they nest cleanly.
- Use `local` for variables inside functions to avoid leaking to the outer scope.

## Conditionals and checks

```bash
# File checks
[[ -f "$file" ]]   # exists and is a regular file
[[ -d "$dir" ]]    # is a directory
[[ -r "$file" ]]   # is readable

# String checks
[[ -z "$var" ]]    # empty string
[[ -n "$var" ]]    # non-empty string
[[ "$a" == "$b" ]] # string equality (use == not =)

# Numeric comparisons
(( count > 0 ))
[[ "$count" -gt 0 ]]
```

Prefer `[[ ]]` over `[ ]` — fewer surprises with spaces and special characters.

## Loops and arrays

```bash
# Loop over files safely (handles spaces in names)
while IFS= read -r line; do
    echo "$line"
done < file.txt

# Array
files=("a.txt" "b.txt")
for f in "${files[@]}"; do echo "$f"; done
```

Use `mapfile` / `readarray` to populate arrays from command output:
```bash
mapfile -t lines < <(grep "pattern" file.txt)
```

## Error handling

```bash
# Check command success explicitly when -e is off
if ! command; then
    echo "ERROR: command failed" >&2
    exit 1
fi

# Cleanup on exit
cleanup() { rm -f "$tmpfile"; }
trap cleanup EXIT
```

Send error messages to stderr: `echo "ERROR: ..." >&2`.

## Portability

- `/bin/bash` is not always at `/bin/bash` on non-Linux systems. Use `#!/usr/bin/env bash` for portability.
- Avoid bash 4+ features (`declare -A` associative arrays, `mapfile`) when the script may run on macOS system bash (which is bash 3.2). If you need them, check the version or use zsh/Python instead.
- Use `command -v tool` (not `which`) to check if a tool is available.
- POSIX-portable scripts use `#!/bin/sh` and avoid `[[ ]]`, `$(( ))` arithmetic, and arrays.

## Common patterns

```bash
# Resolve script directory (works with symlinks on Linux)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Parse simple flags
while [[ $# -gt 0 ]]; do
    case "$1" in
        -v|--verbose) verbose=1; shift ;;
        -o|--output)  output="$2"; shift 2 ;;
        *) echo "Unknown flag: $1" >&2; exit 1 ;;
    esac
done

# Temporary file that is always cleaned up
tmpfile=$(mktemp)
trap 'rm -f "$tmpfile"' EXIT
```
