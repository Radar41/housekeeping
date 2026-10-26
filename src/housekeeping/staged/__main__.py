"""Use the existing Atlas venv: python -m housekeeping.staged --help."""
import argparse
import ast
import inspect
import json
import os
import operator
from pathlib import Path
import signal
import sys
import time

from .adapter import Atlas, Incompatible
from .engine import Pipeline


def positive(value):
    value = int(value)
    if value < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def defaults_from_source(path, extra_symbols=None):
    """Read literal argparse defaults without running Atlas's main entry point."""
    tree = ast.parse(Path(path).read_text())
    result = {}
    symbols = {k: v for k, v in (extra_symbols or {}).items() if type(v) is int}

    def number(node):
        if isinstance(node, ast.Name):
            return symbols[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in {
            ast.Add, ast.Sub, ast.Mult, ast.FloorDiv
        }:
            fn = {ast.Add: operator.add, ast.Sub: operator.sub,
                  ast.Mult: operator.mul, ast.FloorDiv: operator.floordiv}[type(node.op)]
            return fn(number(node.left), number(node.right))
        return ast.literal_eval(node)
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            try:
                value = number(node.value)
            except (ValueError, TypeError, KeyError):
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    symbols[target.id] = value
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != "add_argument":
            continue
        flags = [a.value for a in node.args if isinstance(a, ast.Constant)]
        for flag in ("--docs-txt-threshold", "--docs-md-threshold", "--max-chunks-per-file"):
            if flag not in flags:
                continue
            for kw in node.keywords:
                if kw.arg == "default":
                    try:
                        value = number(kw.value)
                        if isinstance(value, int):
                            result[flag] = value
                    except (ValueError, KeyError, TypeError):
                        pass
    return result


def doctor_report(path):
    """Source signatures only; no full source, secrets, paths from corpus, or prompts."""
    tree = ast.parse(Path(path).read_text())
    report = {"functions": {}, "classes": {}, "threshold_defaults": defaults_from_source(path)}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            report["functions"][node.name] = ast.unparse(node.args)
        elif isinstance(node, ast.ClassDef):
            report["classes"][node.name] = {
                n.name: ast.unparse(n.args) for n in node.body if isinstance(n, ast.FunctionDef)
            }
    # Signatures may contain default URLs: restrict report to relevant functions and
    # omit defaults altogether in the shareable portion.
    wanted = {"upsert_document", "insert_chunk", "insert_embedding", "chunks_for",
              "header_only_format", "structured_header_summary", "metadata_fingerprint",
              "read_document_text", "assert_vector", "text_size_limit"}
    report["functions"] = {n: [p.arg for p in node.args.args]
        for node in tree.body if isinstance(node, ast.FunctionDef) and (n := node.name) in wanted}
    report["classes"] = {node.name: {n.name: [p.arg for p in n.args.args]
        for n in node.body if isinstance(n, ast.FunctionDef) and n.name in
        ("__init__", "embed_batch", "sentence_transformers_embed_batch")}
        for node in tree.body if isinstance(node, ast.ClassDef)}
    return report


def parser():
    p = argparse.ArgumentParser(description="Metadata-first Atlas staging; existing documents are never overwritten.")
    p.add_argument("--source", default="/srv/warm/cluster/atlas/code/atlas/atlas_embed_ingest.py")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    sub.add_parser("init")
    create = sub.add_parser("create")
    create.add_argument("--root-id", type=positive, default=21)
    create.add_argument("--start-rel", default=".")
    create.add_argument("--protected-names", required=True, type=Path)
    create.add_argument("--txt-threshold", type=positive)
    create.add_argument("--md-threshold", type=positive)
    create.add_argument("--max-chunks", type=positive)
    create.add_argument("--max-read-bytes", type=positive, default=512 * 1024**2)
    for name in ("scan", "plan", "status", "pause", "resume", "prepare", "embed", "publish"):
        s = sub.add_parser(name)
        s.add_argument("run_id")
        if name in ("prepare", "embed", "publish"):
            s.add_argument("--watch", action="store_true", help="poll until stopped; idle waits do not mean completed")
            s.add_argument("--limit", type=positive, default=1000000000)
            s.add_argument("--poll-s", type=float, default=5)
        if name == "prepare":
            s.add_argument("--lane", choices=("any", "text", "structured"), default="any")
            s.add_argument("--high-water", type=positive, default=20000)
        if name == "embed":
            s.add_argument("--batch", type=positive, default=4)
        if name == "publish":
            s.add_argument("--apply", action="store_true", help="required to insert new documents in canonical Atlas")
    return p


def stage_drained(status, stage):
    dirs = {r["state"] for r in status["directory"]}
    files = {r["state"] for r in status["file"]}
    vectors = {r["state"] for r in status["vector"]}
    if "pending" in dirs or files & {"candidate", "ready"}:
        return False
    if stage == "prepare":
        return True
    if "pending" in vectors:
        return False
    return stage == "embed" or "prepared" not in files


def main(argv=None):
    args = parser().parse_args(argv)
    dsn = os.getenv("ATLAS_DSN", "dbname=atlas")
    if args.command == "doctor":
        report = doctor_report(args.source)
        try:
            atlas = Atlas(args.source)
            atlas.writer_check()
            pipe = Pipeline(dsn, atlas)
            with pipe.connection() as c:
                root = pipe.root(c, 21)
                column = pipe.check_schema(c)
            report.update(compatible=True, vector_column=column, dimension=atlas.cfg.dim,
                          device=str(getattr(atlas.cfg, "device", "unknown")),
                          excludes_count=len(root["excludes"]))
        except Exception as e:
            report.update(compatible=False, failure_type=type(e).__name__)
            # Known adapter errors contain function/field names only.
            if isinstance(e, Incompatible):
                report["reason"] = str(e)
        print(json.dumps(report, indent=2))
        return 0 if report["compatible"] else 2
    atlas = Atlas(args.source)
    pipe = Pipeline(dsn, atlas)
    if args.command == "init":
        atlas.writer_check()
        with pipe.connection() as c:
            pipe.check_schema(c)
        pipe.init()
        print("created additive hk_stage tables; canonical Atlas data untouched")
        return 0
    if args.command == "create":
        names = [s.strip().lower() for s in args.protected_names.read_text().splitlines()
                 if s.strip() and not s.lstrip().startswith("#")]
        defaults = defaults_from_source(args.source, vars(atlas.m))
        txt = args.txt_threshold or defaults.get("--docs-txt-threshold")
        md = args.md_threshold or defaults.get("--docs-md-threshold")
        cap = args.max_chunks or defaults.get("--max-chunks-per-file")
        if not all(isinstance(x, int) and x > 0 for x in (txt, md, cap)):
            raise Incompatible("cannot read Atlas threshold defaults; specify --txt-threshold --md-threshold --max-chunks")
        print(pipe.create(args.root_id, args.start_rel, names, txt, md, cap, args.max_read_bytes))
        return 0
    if args.command == "status":
        print(json.dumps(pipe.status(args.run_id), indent=2, default=str))
        return 0
    if args.command in ("pause", "resume"):
        pipe.pause(args.run_id, args.command == "pause")
        print(args.command)
        return 0
    if args.command == "plan":
        pipe.plan(args.run_id)
        print(json.dumps(pipe.status(args.run_id), indent=2, default=str))
        return 0
    if args.command == "publish" and not args.apply:
        print("no writes: use publish RUN --apply after reviewing staged status")
        return 2
    if args.command == "embed" and not str(getattr(atlas.cfg, "device", "")).startswith("cuda"):
        raise Incompatible("embedding worker is not configured for CUDA; check ATLAS_EMBED_DEVICE and backend config")
    stopped = False

    def stop(*_):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    n = 0
    print(json.dumps({"stage": args.command, "event": "starting"}), flush=True)
    while not stopped and n < getattr(args, "limit", 1000000000):
        if args.command == "scan":
            worked = pipe.scan_one(args.run_id)
        elif args.command == "prepare":
            worked = pipe.prepare_one(args.run_id, args.lane, args.high_water)
        elif args.command == "embed":
            worked = pipe.embed_one(args.run_id, args.batch)
        else:
            worked = pipe.publish_one(args.run_id)
        if worked:
            n += 1
            if n % 100 == 0:
                print(json.dumps({"stage": args.command, "committed_units": n}), flush=True)
        elif getattr(args, "watch", False):
            if pipe.drained(args.run_id, args.command):
                break
            if args.poll_s <= 0:
                raise ValueError("poll-s must be positive")
            time.sleep(args.poll_s)
        else:
            break
    print(json.dumps({"stage": args.command, "committed_units": n, "stopped": stopped}), flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        print(json.dumps({"error": type(e).__name__,
                          "reason": str(e) if isinstance(e, Incompatible) else "stage failed; transaction rolled back"}),
              file=sys.stderr)
        sys.exit(1)
