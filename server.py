"""Compatibility entry point: v2 opens the native desktop, not an HTTP UI."""
from desktop import main

if __name__ == "__main__":
    raise SystemExit(main())
