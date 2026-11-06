"""Minimal package entry point; the discovery CLI is added in a later PR."""

from . import __version__


def main() -> int:
    print(f"meta-tool-online {__version__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
