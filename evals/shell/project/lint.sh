#!/bin/sh
# A quiet linter: short output, so a windowed view must not cost an extra call.
echo "inventory.py:21:9: E501 line too long (88 > 79 characters)"
echo "inventory.py:29:47: W291 trailing whitespace"
echo "Found 2 issues."
exit 1
