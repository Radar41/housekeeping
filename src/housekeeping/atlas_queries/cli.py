"""hk atlas-queries <query> [--root-id N] [--format tsv|csv] [--out FILE] [--apply]

Runs one read-only SQL file against the Atlas. The DSN comes from the env var named by
[atlas].dsn_env. Output goes to stdout; --out FILE writes a file only with --apply.
"""
import argparse
import csv
import hashlib
import io
import os
import re
import sys
from pathlib import Path

from housekeeping import config
from housekeeping.log import log_line

SQL_DIR = Path(__file__).parent / "sql"
QUERIES = {
    "provenance-check": "provenance_check.sql",
    "inbox-three-way": "inbox_three_way.sql",
    "project-table": "project_table.sql",
    "provenance-payload-keys": "provenance_payload_keys.sql",
    "pipeline-inventory": "pipeline_inventory.sql",
    "pipeline-dirs": "pipeline_dirs.sql",
    "ingest-runs": "ingest_runs.sql",
    "secret-scan": "secret_scan.sql",
}
# Queries that return file text (headers). They refuse to run without the protected names file.
CONTENT_QUERIES = {"pipeline-inventory"}
PROTECTED_SEGMENT = re.compile(r"(^|/)protected(/|$)")
NEEDS_ROOT = {"inbox-three-way"}
OPTIONAL_ROOT = {"pipeline-inventory", "pipeline-dirs"}  # no --root-id means every root
READ_ONLY_OPTIONS = "-c default_transaction_read_only=on"


def strip_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql).strip()


def is_read_only(sql: str) -> bool:
    body = strip_comments(sql).lower()
    return body.startswith(("select", "with"))


def load_sql(query: str) -> str:
    sql = (SQL_DIR / QUERIES[query]).read_text(encoding="utf-8")
    if not is_read_only(sql):
        raise ValueError(f"{QUERIES[query]} is not SELECT or WITH")
    return sql


def render(columns: list[str], rows: list[tuple], fmt: str) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter="\t" if fmt == "tsv" else ",", lineterminator="\n")
    w.writerow(columns)
    for row in rows:
        w.writerow("" if v is None else v for v in row)
    return buf.getvalue()


def run_query(dsn: str, sql: str, params: dict) -> tuple[list[str], list[tuple]]:
    import psycopg

    with psycopg.connect(dsn, options=READ_ONLY_OPTIONS) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [c.name for c in cur.description], cur.fetchall()


def load_names(path: str | None) -> list[str] | None:
    if not path or not Path(path).is_file():
        return None
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    return [n.strip().lower() for n in lines if n.strip()]


def withhold_protected(rows: list, names: list[str]) -> tuple[list, int]:
    """Drop every row with a cell under a protected/ path segment or containing a protected name.

    Output of these queries gets pasted to outside tools, so protected material stays out of it
    even though Atlas indexes it locally.
    """
    kept = []
    for row in rows:
        cells = [str(c) for c in row if c is not None]
        if any(PROTECTED_SEGMENT.search(c) for c in cells) or any(n in c.lower() for c in cells for n in names):
            continue
        kept.append(row)
    return kept, len(rows) - len(kept)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="hk atlas-queries")
    ap.add_argument("query", choices=sorted(QUERIES))
    ap.add_argument("--root-id", type=int)
    ap.add_argument("--format", choices=["tsv", "csv"])
    ap.add_argument("--out")
    ap.add_argument("--apply", action="store_true", help="actually write --out (default is dry-run)")
    args = ap.parse_args(argv)

    cfg = config.load()
    section = cfg.get("atlas_queries", {})
    fmt = args.format or section.get("default_format", "tsv")
    log_path = section.get("log", "logs/atlas_queries.tsv")
    dsn_env = cfg["atlas"]["dsn_env"]
    dsn = os.environ.get(dsn_env)
    if not dsn:
        print(f"error: env var {dsn_env} is not set", file=sys.stderr)
        return 2
    if args.query in NEEDS_ROOT and args.root_id is None:
        print(f"error: {args.query} needs --root-id", file=sys.stderr)
        return 2

    names_file = cfg.get("checker", {}).get("protected_names_file")
    names = load_names(names_file)
    if names is None and args.query in CONTENT_QUERIES:
        print(f"error: {args.query} returns file text and needs the protected names file ({names_file}); "
              "see processes/00-setup.md step 5", file=sys.stderr)
        return 2

    sql = load_sql(args.query)
    params = {"root_id": args.root_id} if args.query in NEEDS_ROOT | OPTIONAL_ROOT else {}
    columns, rows = run_query(dsn, sql, params)
    rows, withheld = withhold_protected(rows, names or [])
    if withheld:
        print(f"withheld {withheld} rows that touch protected material", file=sys.stderr)
    text = render(columns, rows, fmt)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    sql_name = QUERIES[args.query]

    if not args.out:
        sys.stdout.write(text)
        log_line(log_path, "query", sql_name, "stdout", digest)
        return 0

    out = Path(args.out)
    if out.exists() and out.read_text(encoding="utf-8") == text:
        print(f"unchanged: {out} already holds these {len(rows)} rows")
        return 0
    if not args.apply:
        print(f"dry-run: would write {len(rows)} rows to {out} (use --apply)")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    log_line(log_path, "write", sql_name, out, digest)
    print(f"wrote {len(rows)} rows to {out}")
    return 0
