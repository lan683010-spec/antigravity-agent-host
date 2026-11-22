"""Entrypoint for `python -m agentctl`."""

import sys

from agentctl.cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
