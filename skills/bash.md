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

## Parameter expansion (string manipulation without external tools)

```bash
str="hello_world.txt"

# Length
echo "${#str}"           # 15

# Substring removal (greedy/non-greedy from left/right)
echo "${str#hello_}"     # world.txt   (remove shortest match from start)
echo "${str##*.}"        # txt         (remove longest match from start — get extension)
echo "${str%.txt}"       # hello_world (remove shortest match from end)
echo "${str%.*}"         # hello_world (same here)
echo "${str%%_*}"        # hello       (remove longest match from end)

# Substitution
echo "${str/world/there}"  # hello_there.txt  (first match)
echo "${str//l/L}"         # heLLo_worLd.txt  (all matches)

# Substring
echo "${str:6:5}"        # world  (offset 6, length 5)

# Case conversion (bash 4+)
echo "${str^^}"          # HELLO_WORLD.TXT  (uppercase)
echo "${str,,}"          # hello_world.txt  (lowercase)
echo "${str^}"           # Hello_world.txt  (capitalise first char)
```

## Conditionals and checks

```bash
# File checks
[[ -f "$file" ]]   # exists and is a regular file
[[ -d "$dir" ]]    # is a directory
[[ -r "$file" ]]   # is readable
[[ -s "$file" ]]   # exists and is non-empty
[[ -x "$file" ]]   # is executable

# String checks
[[ -z "$var" ]]          # empty string
[[ -n "$var" ]]          # non-empty string
[[ "$a" == "$b" ]]       # string equality
[[ "$a" == *suffix ]]    # glob match (no quotes on pattern side)
[[ "$a" =~ ^[0-9]+$ ]]  # regex match

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

# Indexed array
files=("a.txt" "b.txt" "c.txt")
for f in "${files[@]}"; do echo "$f"; done
echo "${files[0]}"        # first element
echo "${#files[@]}"       # array length

# Associative array (bash 4+)
declare -A config
config[host]="localhost"
config[port]="5432"
for key in "${!config[@]}"; do
    echo "$key = ${config[$key]}"
done

# mapfile / readarray — populate array from command output
mapfile -t lines < <(grep "pattern" file.txt)
mapfile -t words < <(echo "one two three" | tr ' ' '\n')

# Iterate over glob, skip if no matches
shopt -s nullglob
for f in /tmp/*.log; do
    process "$f"
done
```

## Error handling

```bash
# Check command success explicitly
if ! command; then
    echo "ERROR: command failed" >&2
    exit 1
fi

# Cleanup on exit — always use trap for tempfiles
tmpfile=$(mktemp)
trap 'rm -f "$tmpfile"' EXIT

# Trap multiple signals
cleanup() {
    echo "Cleaning up..." >&2
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT INT TERM

# Retry with backoff
retry() {
    local n=0
    local max=3
    local delay=1
    until "$@"; do
        n=$((n + 1))
        if (( n >= max )); then
            echo "Failed after $max attempts: $*" >&2
            return 1
        fi
        echo "Attempt $n failed. Retrying in ${delay}s..." >&2
        sleep "$delay"
        delay=$((delay * 2))
    done
}
retry curl -sf "$URL"
```

Send error messages to stderr: `echo "ERROR: ..." >&2`.

## Process substitution and pipelines

```bash
# Process substitution — treat command output as a file
diff <(sort file1.txt) <(sort file2.txt)

# Read from process substitution (avoids subshell variable scoping issue)
while IFS= read -r line; do
    process "$line"
done < <(find . -name "*.log")

# Pipe into a while loop — variables set inside are lost (subshell!)
# WRONG:
find . -name "*.log" | while read -r f; do count=$((count+1)); done
echo "$count"  # 0 — count was set in a subshell

# CORRECT: use process substitution
while IFS= read -r f; do count=$((count+1)); done < <(find . -name "*.log")
echo "$count"  # correct
```

## Option parsing

```bash
# getopts — POSIX-compatible option parsing
usage() { echo "Usage: $0 [-v] [-o output] file..." >&2; exit 1; }

verbose=0
output=""
while getopts ":vo:" opt; do
    case "$opt" in
        v) verbose=1 ;;
        o) output="$OPTARG" ;;
        :) echo "ERROR: -$OPTARG requires an argument" >&2; usage ;;
        \?) echo "ERROR: unknown option -$OPTARG" >&2; usage ;;
    esac
done
shift $((OPTIND - 1))
# "$@" now contains non-option arguments

# Long options — use a while/case loop instead
while [[ $# -gt 0 ]]; do
    case "$1" in
        -v|--verbose) verbose=1; shift ;;
        -o|--output)  output="${2:?--output requires an argument}"; shift 2 ;;
        --) shift; break ;;
        -*) echo "Unknown option: $1" >&2; exit 1 ;;
        *)  break ;;
    esac
done
```

## Heredocs and herestrings

```bash
# Heredoc — multi-line string to stdin; the delimiter must be unindented
cat <<'EOF'
This text is passed literally — no variable expansion.
EOF

# Heredoc with expansion
cat <<EOF
Host: $HOSTNAME
Date: $(date)
EOF

# Indented heredoc (bash 4+) — strip leading tabs (not spaces!)
cat <<-EOF
	This line has a leading tab stripped.
	So does this one.
	EOF

# Herestring — single-line stdin
read -r first last <<< "John Doe"
grep "pattern" <<< "$variable"
```

## Debugging

```bash
# Trace execution — print each command before running it
set -x
# Turn off:
set +x

# Syntax check without executing
bash -n script.sh

# Dry-run pattern — print commands instead of running
DRY_RUN=${DRY_RUN:-0}
run() {
    if (( DRY_RUN )); then
        echo "[DRY RUN] $*"
    else
        "$@"
    fi
}
run rm -rf /tmp/work

# Print variable state
declare -p varname  # shows type, attributes, and value
```

## Portability

- `/bin/bash` is not always at `/bin/bash` on non-Linux systems. Use `#!/usr/bin/env bash` for portability.
- Avoid bash 4+ features (`declare -A`, `mapfile`) when the script may run on macOS system bash (bash 3.2). If you need them, check the version or use Python instead.
- Use `command -v tool` (not `which`) to check if a tool is available.
- POSIX-portable scripts use `#!/bin/sh` and avoid `[[ ]]`, `$(( ))`, and arrays.

## Common patterns

```bash
# Resolve script directory (works with symlinks on Linux)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Require a command to exist
require() {
    command -v "$1" &>/dev/null || { echo "ERROR: $1 not found" >&2; exit 1; }
}
require jq
require curl

# Logging with timestamps and levels
log()  { echo "[$(date '+%H:%M:%S')] INFO  $*"; }
warn() { echo "[$(date '+%H:%M:%S')] WARN  $*" >&2; }
die()  { echo "[$(date '+%H:%M:%S')] ERROR $*" >&2; exit 1; }

# Colour output (check for tty first)
if [[ -t 1 ]]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; RESET='\033[0m'
else
    RED=''; GREEN=''; RESET=''
fi
echo -e "${GREEN}OK${RESET}"

# Temporary file that is always cleaned up
tmpfile=$(mktemp)
trap 'rm -f "$tmpfile"' EXIT
```
