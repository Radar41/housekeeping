"""hk tasks <command>. The agent-loop ledger in Memgraph and its dispatcher (specs/12-agent-loop.md).

  propose code-card --from-dirs FILE [--limit N] [--match SUBSTR] [--apply]
  depend TASK_ID ON_TASK_ID [--apply]
  dispatch [--once | --watch] [--apply]
  list [--status S] [--kind K] [--json]
  show TASK_ID
  blocked | done | needs-you [--json]

Every command that writes is a dry run until --apply. Task ids may be given as a unique prefix.
"""
import argparse
import csv
import json
import os
import sys
import time
from dataclasses import asdict

from housekeeping import config
from housekeeping.log import log_line
from housekeeping.tasks.dispatch import Settings, run_once
from housekeeping.tasks.kinds import REGISTRY, checkless
from housekeeping.tasks.model import Task


def open_store(s: Settings):
    from housekeeping.tasks.store import MemgraphStore

    return MemgraphStore(s.memgraph_uri, s.ns, os.environ.get(s.memgraph_user_env, ""),
                         os.environ.get(s.memgraph_password_env, ""))


def protected_names(cfg: dict) -> list[str] | None:
    from housekeeping.checker.cli import load_protected

    return load_protected(cfg.get("checker", {}).get("protected_names_file"))


def resolve(store, prefix: str) -> str:
    hits = [t.id for t in store.list() if t.id.startswith(prefix)]
    if len(hits) != 1:
        raise SystemExit(f"error: {prefix!r} matches {len(hits)} tasks")
    return hits[0]


def read_dirs(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def cmd_propose(a, store, s: Settings, cfg) -> int:
    kind = REGISTRY[a.kind]
    rows = read_dirs(a.from_dirs)
    if a.match:
        rows = [r for r in rows if a.match in f"{r.get('root_path', '')}/{r.get('dir', '')}"]
    if a.limit:
        rows = rows[:a.limit]
    new = present = bad = 0
    for row in rows:
        inp = kind.input_from_row(row)
        problems = kind.validate_input(inp)
        if problems:
            bad += 1
            print(f"skip: {'; '.join(problems)}", file=sys.stderr)
            continue
        task = Task.new(kind.NAME, inp, s.max_attempts)
        if store.get(task.id) is not None:
            present += 1
            continue
        if a.apply:
            store.propose(task)
            log_line(s.log, "propose", kind.NAME, f"{inp['root_path']}/{inp['dir']}", task.id)
        new += 1
    verb = "proposed" if a.apply else "would propose"
    print(f"{verb} {new}, already present {present}" + (f", skipped {bad}" if bad else ""))
    return 0


def cmd_depend(a, store, s: Settings, cfg) -> int:
    tid, on = resolve(store, a.task), resolve(store, a.on)
    if not a.apply:
        print(f"would add {tid[:12]} depends on {on[:12]}")
        return 0
    if store.add_dependency(tid, on):
        log_line(s.log, "depend", store.get(tid).kind, on, tid)
        print(f"added {tid[:12]} depends on {on[:12]}")
    else:
        print("unchanged: dependency already present")
    return 0


def cmd_dispatch(a, store, s: Settings, cfg) -> int:
    names = protected_names(cfg)
    if names is None:
        print("error: dispatch needs the protected names file ([checker].protected_names_file)", file=sys.stderr)
        return 2
    import httpx

    with httpx.Client() as client:
        while True:
            counts = run_once(store, s, apply=a.apply, protected_names=names, client=client)
            if counts:
                print(json.dumps(counts, sort_keys=True))
            if not a.watch:
                return 0
            time.sleep(s.poll_interval_s)


def short(t: Task) -> dict:
    return {"id": t.id, "kind": t.kind, "status": t.status, "attempts": t.attempts,
            "target": f"{t.input.get('root_path', '')}/{t.input.get('dir', '')}", "updated_at": t.updated_at}


def emit(rows: list[dict], as_json: bool, cols: list[str]) -> None:
    if as_json:
        print(json.dumps(rows, indent=1))
        return
    print("\t".join(cols))
    for r in rows:
        print("\t".join(str(r.get(c, ""))[:12] if c == "id" else str(r.get(c, "")) for c in cols))


def cmd_list(a, store, s, cfg) -> int:
    emit([short(t) for t in store.list(status=a.status, kind=a.kind)], a.json,
         ["id", "kind", "status", "attempts", "target"])
    return 0


def cmd_show(a, store, s, cfg) -> int:
    tid = resolve(store, a.task)
    t = store.get(tid)
    card = store.latest_card(tid)
    out = {"task": asdict(t), "dependencies": [d.id for d in store.dependencies(tid)],
           "card": card.fields if card else None, "card_model": card.model if card else None,
           "evidence": [asdict(e) for e in store.evidence_for(tid)]}
    print(json.dumps(out, indent=1, ensure_ascii=False))
    return 0


def cmd_blocked(a, store, s, cfg) -> int:
    emit([{"id": t, "waiting_on": ",".join(w[:12] for w in ws)} for t, ws in store.blocked()], a.json,
         ["id", "waiting_on"])
    return 0


def cmd_done(a, store, s, cfg) -> int:
    emit([{"id": i, "kind": k, "check": c, "check_version": v[:12], "at": at} for i, k, c, v, at in store.done()],
         a.json, ["id", "kind", "check", "check_version", "at"])
    return 0


def stuck(store) -> list[tuple[Task, str]]:
    """Failed tasks the dispatcher will not retry: attempts used up, or the check said a retry cannot help."""
    out = []
    for t in store.list(status="failed"):
        last = store.evidence_for(t.id)[-1:]
        if last and not last[0].retry:
            out.append((t, last[0].detail))
        elif t.attempts >= t.max_attempts:
            out.append((t, (last[0].detail if last else "") + " (attempts used up)"))
    return out


def cmd_needs_you(a, store, s, cfg) -> int:
    rows = [dict(short(store.get(i)), why="no check for this kind") for i in store.needs_you(checkless())]
    rows += [dict(short(t), why=why) for t, why in stuck(store)]
    emit(rows, a.json, ["id", "kind", "status", "target", "why"])
    return 0


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="hk tasks")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("propose")
    p.add_argument("kind", choices=sorted(REGISTRY))
    p.add_argument("--from-dirs", required=True, help="a pipeline-dirs TSV from process 2")
    p.add_argument("--limit", type=int)
    p.add_argument("--match")
    p.add_argument("--apply", action="store_true")
    p = sub.add_parser("depend")
    p.add_argument("task")
    p.add_argument("on")
    p.add_argument("--apply", action="store_true")
    p = sub.add_parser("dispatch")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true", help="one pass (the default)")
    g.add_argument("--watch", action="store_true", help="a pass every poll_interval_s")
    p.add_argument("--apply", action="store_true")
    p = sub.add_parser("list")
    p.add_argument("--status")
    p.add_argument("--kind")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show")
    p.add_argument("task")
    for name in ("blocked", "done", "needs-you"):
        sub.add_parser(name).add_argument("--json", action="store_true")
    return ap


COMMANDS = {"propose": cmd_propose, "depend": cmd_depend, "dispatch": cmd_dispatch, "list": cmd_list,
            "show": cmd_show, "blocked": cmd_blocked, "done": cmd_done, "needs-you": cmd_needs_you}


def main(argv: list[str], store=None) -> int:
    a = parser().parse_args(argv)
    cfg = config.load()
    s = Settings.from_config(cfg)
    own = store is None
    store = store or open_store(s)
    try:
        return COMMANDS[a.cmd](a, store, s, cfg)
    finally:
        if own:
            store.close()
