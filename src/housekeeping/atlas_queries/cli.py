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
}
NEEDS_ROOT = {"inbox-three-way"}
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

    sql = load_sql(args.query)
    params = {"root_id": args.root_id} if args.query in NEEDS_ROOT else {}
    columns, rows = run_query(dsn, sql, params)
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
