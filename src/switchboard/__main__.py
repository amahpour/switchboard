"""Entry point: ``python -m switchboard`` -> ``cli.main()`` (DESIGN.md §3)."""

import sys

from switchboard.cli import main

if __name__ == "__main__":
    sys.exit(main())
