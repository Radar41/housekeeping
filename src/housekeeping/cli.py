"""Dispatcher: hk <module> [args]. Modules are found by name, never registered here."""
import importlib
import sys


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: hk <module> [args]")
        sys.exit(2)
    name = sys.argv[1].replace("-", "_")
    mod = importlib.import_module(f"housekeeping.{name}.cli")
    sys.exit(mod.main(sys.argv[2:]))
