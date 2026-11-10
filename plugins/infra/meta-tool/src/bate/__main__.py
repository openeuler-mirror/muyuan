"""Package identity or BATE extraction CLI."""

import sys

from . import __version__


def main() -> int:
    if len(sys.argv) > 1:
        from .cli import main as extract
        return extract()
    print(f"meta-tool-online {__version__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
