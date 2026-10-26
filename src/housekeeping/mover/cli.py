"""hk mover plan|apply|undo. Dry-run unless --apply is given."""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from housekeeping.mover import engine, provenance


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hk mover")
    sub = p.add_subparsers(dest="cmd", required=True)
    plan = sub.add_parser("plan", help="print the full plan, change nothing")
    plan.add_argument("table")
    plan.add_argument("--batch", help="quarantine batch name, default is the table file stem")
    ap = sub.add_parser("apply", help="execute the plan (dry-run without --apply)")
    ap.add_argument("table")
    ap.add_argument("--apply", action="store_true", dest="do_apply")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--batch", help="quarantine batch name, default is the table file stem")
    un = sub.add_parser("undo", help="replay a moves log backward (dry-run without --apply)")
    un.add_argument("moves_log")
    un.add_argument("--since", help="ISO timestamp, undo only entries at or after it")
    un.add_argument("--apply", action="store_true", dest="do_apply")
    un.add_argument("--batch", help="batch name for provenance rows, default is the log file stem")
    return p


def _summary(steps) -> str:
    counts: dict[str, int] = {}
    for st in steps:
        counts[st.status] = counts.get(st.status, 0) + 1
    return "summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) if counts else "summary: no rows"


def _plan(args, execute: bool) -> int:
    table = Path(args.table)
    s = engine.Settings.from_config(args.batch or table.stem)
    rows = engine.read_table(table)
    steps = engine.build_plan(s, rows)
    if not execute:
        for st in steps:
            print(engine.fmt(st, "plan" if st.status == "todo" else None))
        print(_summary(steps))
        print("dry-run: nothing changed")
        return 1 if any(st.status in {"refused", "error"} for st in steps) else 0
    rc = 0
    pending = engine.open_moves(engine.read_log(s.moves_log))
    results = []
    for row in rows:
        # replanned against the disk so earlier moves in this run are seen
        st = engine.plan_row(s, row, set(), pending)
        note = []
        if st.status == "todo":
            try:
                note = engine.apply_step(s, st, args.push)
                st.status = "applied"
            except Exception as e:  # one bad row must not stop the batch
                st.status, st.reason = "error", f"{type(e).__name__}: {e}"
        if st.status in {"refused", "error"} or note:
            rc = 1
        print("\t".join([engine.fmt(st), *note]))
        results.append(st)
    print(_summary(results))
    return rc


def _undo(args) -> int:
    log = Path(args.moves_log)
    since = None
    if args.since:
        since = datetime.fromisoformat(args.since)
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
    s = engine.Settings.from_config(args.batch or log.stem)
    steps = engine.plan_undo(engine.read_log(log), since)
    if not steps:
        print("nothing to undo")
        return 0
    rc = 0
    for u in steps:
        _, action, src, dst, ident = u.entry
        label = "plan" if not args.do_apply and u.status == "todo" else u.status
        if args.do_apply and u.status == "todo":
            try:
                notes = engine.apply_undo(s, log, u)
                label = "undone"
                if notes:
                    rc, u.reason = 1, "; ".join(notes)
            except Exception as e:
                label, u.reason = "error", f"{type(e).__name__}: {e}"
        if label == "error":
            rc = 1
        print("\t".join([label, "undo", ident.rpartition(":")[0], dst, "-> " + src] + ([f"[{u.reason}]"] if u.reason else [])))
    if not args.do_apply:
        print("dry-run: nothing changed")
    return rc


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    try:
        return _dispatch(args)
    finally:
        provenance.close()


def _dispatch(args) -> int:
    try:
        if args.cmd == "plan":
            return _plan(args, execute=False)
        if args.cmd == "apply":
            return _plan(args, execute=args.do_apply)
        return _undo(args)
    except (OSError, ValueError, KeyError) as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
