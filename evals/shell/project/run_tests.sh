#!/bin/sh
# Runs the whole suite verbosely (unittest writes its report to stderr).
cd "$(dirname "$0")"
export PYTHONPATH="$PWD"
PYTHONDONTWRITEBYTECODE=1 exec python3 -m unittest discover -v -s tests
