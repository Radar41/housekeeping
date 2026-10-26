#!/usr/bin/env python3
"""Render a template: replace {{name}} placeholders, fail if any is left unfilled.

Dry-run by default (prints the result). --apply writes to --out and appends one TSV log line.
usage: render.py TEMPLATE --set key=value [key=value ...] [--out PATH] [--apply] [--force]
"""
import argparse
import hashlib
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def parse_sets(items):
    values = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key:
            sys.exit(f"bad --set {item!r}, expected key=value")
        values[key] = value
    return values


def render(text, values):
    names = set(PLACEHOLDER.findall(text))
    unknown = sorted(set(values) - names)
    if unknown:
        sys.exit("unknown keys (not in template): " + ", ".join(unknown))
    out = PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), text)
    left = sorted(set(PLACEHOLDER.findall(out)))
    if left:
        sys.exit("unfilled placeholders: " + ", ".join(left))
    return out


def log_line(path, action, source, target, ident):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(p, "a", encoding="utf-8") as f:
        f.write("\t".join([ts, action, str(source), str(target), ident]) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("template")
    ap.add_argument("--set", nargs="+", action="append", default=[], metavar="key=value")
    ap.add_argument("--out", help="destination, required with --apply")
    ap.add_argument("--apply", action="store_true", help="write the file (default is dry-run)")
    ap.add_argument("--force", action="store_true", help="overwrite a different existing file, keeping it as OUT.bak")
    ap.add_argument("--log", default="logs/templates.tsv")
    args = ap.parse_args(argv)

    values = parse_sets([i for group in args.set for i in group])
    result = render(Path(args.template).read_text(encoding="utf-8"), values)

    if not args.apply:
        sys.stdout.write(result)
        print("dry-run: nothing written (use --apply --out PATH)", file=sys.stderr)
        return 0
    if not args.out:
        sys.exit("--apply requires --out")

    out = Path(args.out)
    if out.exists():
        if out.read_text(encoding="utf-8") == result:
            print(f"unchanged: {out}")
            return 0
        if not args.force:
            sys.exit(f"{out} exists and differs, use --force (old copy kept as {out}.bak)")
        Path(str(out) + ".bak").write_text(out.read_text(encoding="utf-8"), encoding="utf-8")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(result, encoding="utf-8")
    ident = hashlib.sha256(result.encode("utf-8")).hexdigest()
    log_line(args.log, "render", args.template, out, ident)
    print(f"wrote: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
