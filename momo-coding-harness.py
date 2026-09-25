#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# Guarded: the code index's worker processes (spawn) import this file again,
# and must neither start the harness nor pay for importing it.
if __name__ == "__main__":
    from harness.main import main

    main()
