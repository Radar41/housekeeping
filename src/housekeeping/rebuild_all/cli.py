"""hk rebuild-all plan|run|report|push: process 5 for every project, you only review and push.

  plan   [--dirs TSV]            pick one candidate per project (copies collapsed); writes candidates.tsv
  run    RUN [--apply]           per candidate: exclude rules (defaults, bulk-data folders, local model),
                                 each rule kept only if it drops no code; size limits; then with --apply
                                 scripts/rebuild-pipeline.sh, a README drafted by the local model, a commit,
                                 and the checker. Resumable: finished candidates are skipped.
  report RUN                     review.tsv + report.md (your edits to the push column are kept)
  push   RUN [--apply]           gh repo create --private --push for rows with push=yes

Nothing is pushed or moved by run. Sources are only read. One log line per action.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from housekeeping.log import log_line
from housekeeping.rebuild_all import select as sel

HK = Path(__file__).resolve().parents[3]
SCRIPT = HK / "scripts" / "rebuild-pipeline.sh"
BASE_EXCLUDE = HK / "scripts" / "rebuild-exclude.txt"
BATCH_EXCLUDE = HK / "scripts" / "rebuild-batch-exclude.txt"
NAMES = HK / "protected_names.txt"
HOME = Path(os.environ.get("HK_HOME", Path.home()))
PROJECTS = Path(os.environ.get("HK_PROJECTS", HOME / "projects"))
DATA = Path(os.environ.get("HK_DATA", HOME / "data" / "housekeeping"))
MAX_SIZE = os.environ.get("HK_MAX_SIZE", "5M")
KEEP_EXT = {"py", "sh", "bash", "m", "mjs", "js", "jsx", "ts", "tsx", "rs", "c", "cc", "cpp", "h", "hpp",
            "jl", "r", "go", "sql", "sbatch", "slurm", "ipynb", "cu", "tex", "ps1", "f90", "java",
            "toml", "yml", "yaml", "cfg", "ini", "cmake", "md", "rst"}
KEEP_NAMES = {"Makefile", "makefile", "GNUmakefile", "justfile", "Snakefile", "CMakeLists.txt",
              "Cargo.toml", "package.json", "environment.yml", "requirements.txt", "run.sh", "Dockerfile"}
EMDASH = chr(0x2014)
LOCK = threading.Lock()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def logpath():
    return DATA / "rebuild_all.tsv"


# ---------- listing and rules ----------

def keep_file(rel: str) -> bool:
    base = rel.rsplit("/", 1)[-1]
    return base in KEEP_NAMES or (("." in base) and base.rsplit(".", 1)[-1].lower() in KEEP_EXT)


HIDE = re.compile(r"^\[sender\] hiding (file|directory) (.+) because of pattern (.+)$")


def listing(src: str, rules: list[str], why: bool = False):
    """What rebuild-pipeline.sh would copy: same base rules and size cap, plus `rules`.
    With why=True also returns {pattern: [hidden paths]} from the same single rsync pass."""
    cmd = ["rsync", "-an", "--prune-empty-dirs", f"--max-size={MAX_SIZE}", f"--exclude-from={BASE_EXCLUDE}"]
    cmd += ["-vv"] if why else []
    cmd += [f"--exclude={r}" for r in rules]
    cmd += ["--out-format=%l\t%n", src.rstrip("/") + "/", "/nonexistent-hk-dry-run/"]
    out = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if out.returncode not in (0, 23, 24):          # 23/24: some files unreadable or vanished
        raise RuntimeError(f"rsync exit {out.returncode}")
    rows, hidden = [], {}
    for line in out.stdout.splitlines():
        if why and (m := HIDE.match(line)):
            kind, path, pat = m.groups()
            hidden.setdefault(pat, []).append(path + ("/" if kind == "directory" else ""))
            continue
        size, _, rel = line.partition("\t")
        if rel and not rel.endswith("/") and size.isdigit():
            rows.append((int(size), rel))
    return (rows, hidden) if why else rows


def summarize(files, depth=2, top=40):
    by_dir, by_ext = {}, {}
    for size, rel in files:
        parts = rel.split("/")
        k = "/".join(parts[:depth]) + "/" if len(parts) > depth else ("/".join(parts[:-1]) + "/" if len(parts) > 1 else "(top level)")
        e = rel.rsplit("/", 1)[-1]
        e = "." + e.rsplit(".", 1)[-1].lower() if "." in e else "(none)"
        for d, key in ((by_dir, k), (by_ext, e)):
            n, b = d.get(key, (0, 0))
            d[key] = (n + 1, b + size)
    fmt = lambda d, n: [f"{c:7d} files | {b / 1e6:8.1f} MB | {k}" for k, (c, b) in sorted(d.items(), key=lambda x: -x[1][0])[:n]]
    return fmt(by_dir, top), fmt(by_ext, 20)


def bulk_dirs(files, min_files=300):
    """Highest folders holding hundreds of files and no code, config or doc file (caches, run results).
    A folder that also holds code is skipped and its subfolders are tried instead."""
    count, data = {}, {}
    for _, rel in files:
        parts = rel.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            k = "/".join(parts[:i])
            count[k] = count.get(k, 0) + 1
            data[k] = data.get(k, 0) + (0 if keep_file(rel) else 1)
    picked = []
    for k in sorted(count, key=lambda k: k.count("/")):
        if any(k.startswith(p + "/") for p in picked):
            continue
        if count[k] >= min_files and data[k] == count[k]:
            picked.append(k)
    return ["/" + p + "/" for p in picked]


def default_rules():
    if not BATCH_EXCLUDE.is_file():
        return []
    return [l.strip() for l in BATCH_EXCLUDE.read_text().splitlines() if l.strip() and not l.lstrip().startswith("#")]


def clean_rule(p: str) -> str | None:
    p = p.strip().strip("`\"'").strip()
    if re.fullmatch(r"\.[A-Za-z0-9]+", p):
        p = "*" + p
    if (not p or len(p) > 200 or p in {"*", "**", "/", "*/", "**/", "/*"} or p[0] in "+-!#:"
            or any(c in p for c in "\n\r\t")):
        return None
    return p


def validate(src, files, rules, prior=()):
    """Keep a rule only if it removes something and drops no code, config or doc file.
    files is the listing with the prior rules already applied. One rsync pass reports which rule
    hid which path; offending rules are dropped and the pass repeats (usually once or twice),
    since a file hidden by a dropped rule may still match a later one."""
    prior = [r for _, r in prior]
    names = {f for _, f in files}
    keep = {f for f in names if keep_file(f)}
    live, rejected = list(rules), []
    for _ in range(10):
        after, hidden = listing(src, prior + [r for _, r in live], why=True)
        by_path = {p: pat for pat, ps in hidden.items() for p in ps}
        gone_by = {}
        for f in names:                            # the rule that hid f: its own path or its nearest folder
            parts = f.split("/")
            for path in [f] + ["/".join(parts[:i]) + "/" for i in range(len(parts) - 1, 0, -1)]:
                if path in by_path:
                    gone_by.setdefault(by_path[path], set()).add(f)
                    break
        bad, accepted, unused = [], [], []
        for source, rule in live:
            gone = gone_by.get(rule, set())
            if gone & keep:
                lost = sorted(gone & keep)
                bad.append((source, rule, f"would drop {len(lost)} code/doc files, e.g. {lost[0]}"))
            elif gone:
                accepted.append((source, rule))
            elif source == "model":
                unused.append((source, rule, "matches nothing"))
        if not bad:                               # unused rules hid nothing, so `after` is already right
            return accepted, rejected + unused, after
        rejected += bad
        drop = {(s_, r_) for s_, r_, _ in bad}
        live = [x for x in live if x not in drop]
    return accepted, rejected, listing(src, prior + [r for _, r in accepted])


# ---------- local model through the router ----------

def ask(router, role, prompt, timeout=600):
    import httpx
    # reasoning_effort none: Ollama turns thinking on by default for qwen3.5 on /v1, which costs minutes per call
    body = {"model": role, "stream": False, "reasoning_effort": "none", "max_tokens": 800,
            "messages": [{"role": "user", "content": prompt}]}
    r = httpx.post(router.rstrip("/") + "/v1/chat/completions", json=body, timeout=timeout)
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"] or ""
    return clean_text(text)


def clean_text(text: str) -> str:
    """Model output without reasoning blocks, em dashes or doubled spaces."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"\s*" + EMDASH + r"\s*", ", ", text)
    return text.strip()


def model_rules(router, role, folders, types):
    prompt = ("A research code folder is becoming a git repo of code, configs and docs only. Below are its "
              "files grouped by folder and by type (count, size, path). Generic output folders, logs, "
              "caches and archives are already excluded. Propose rsync exclude rules for the remaining "
              "folders or types that hold generated output, run results, caches or raw data. Never exclude "
              "source code, configs or docs. Output only lines of the form\nEXCLUDE <pattern>  # <reason>\n"
              "Use only folders and types shown. At most 15 lines. Output nothing if nothing should go."
              "\n\nFOLDERS:\n" + "\n".join(folders) + "\n\nTYPES:\n" + "\n".join(types))
    out = []
    for line in clean_text(ask(router, role, prompt)).splitlines():
        m = re.match(r"\s*EXCLUDE\s+(.+?)\s*(#.*)?$", line)
        if m and (rule := clean_rule(m.group(1))):
            out.append(rule)
    return out[:15]


def draft_readme(router, role, dst: Path, types):
    parts = []
    old = dst / "README.source.md"
    if old.is_file():
        parts.append("OLD README:\n" + old.read_text(errors="replace")[:3000])
    code = sorted((p for p in dst.rglob("*") if p.is_file() and ".git" not in p.parts
                   and keep_file(p.relative_to(dst).as_posix()) and p.suffix not in {".md", ".toml"}),
                  key=lambda p: (len(p.relative_to(dst).parts), p.name))[:20]
    for p in code:
        head = "\n".join(p.read_text(errors="replace").splitlines()[:12])[:600]
        parts.append(f"FILE {p.relative_to(dst)}:\n{head}")
    prompt = ("Below are the file types, an old README if any, and the first lines of up to 20 files of a "
              "research code repo. In two or three plain sentences say what this code does and what it works "
              "on, naming the main scripts if that is clear. No headings, no lists, no dashes as punctuation, "
              "nothing the files do not say.\n\nTYPES:\n" + "\n".join(types) + "\n\n" + "\n\n".join(parts))[:14000]
    text = " ".join(clean_text(ask(router, role, prompt)).split())
    return text[:900].strip()


def set_readme(dst: Path, what: str | None, state: str):
    p = dst / "README.md"
    s = p.read_text()
    if what:
        s = re.sub(r"(## What it is\n)(.*?)(?=\n## )", lambda m: m.group(1) + what + "\n", s, count=1, flags=re.S)
    s = re.sub(r"(## State\n)(.*?)(?=\n## )", lambda m: m.group(1) + state + "\n", s, count=1, flags=re.S)
    p.write_text(s)


def checker(dst: Path):
    from housekeeping.checker import cli as chk
    names = chk.load_protected(str(NAMES))
    return {r["check"]: r["status"] for r in chk.run_checks(dst, names)}


# ---------- state ----------

def read_tsv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def write_tsv(path, rows, fields):
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter="\t", extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def latest_state(run: Path):
    state = {}
    p = run / "state.jsonl"
    if p.is_file():
        for line in p.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                state[r["name"]] = r
    return state


def record(run: Path, rec: dict):
    with LOCK:
        with open(run / "state.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        log_line(logpath(), "rebuild_all." + rec["status"], rec.get("source", ""), rec.get("dst", ""), rec["name"])


# ---------- one candidate ----------

FINAL = {"built", "already_built", "name_taken", "too_big", "no_code", "refused", "rebuild_failed"}


def process(run: Path, c: dict, apply: bool, router: str, role: str, limits: dict) -> dict:
    name, src = c["name"], c["source"]
    dst = PROJECTS / name
    rec = {"name": name, "source": src, "dst": str(dst), "role": role, "at": now(), "copies": c.get("copies", "")}
    if dst.exists():
        same = (dst / "SOURCES.txt").is_file() and f"source: {src}" in (dst / "SOURCES.txt").read_text(errors="replace")
        rec["status"] = "already_built" if same else "name_taken"
        if same:
            rec["checks"] = checker(dst)
        return rec
    if not Path(src).is_dir():
        return {**rec, "status": "no_code", "note": "source folder missing"}
    t = time.monotonic()
    base = listing(src, [])
    rec["files_before"] = len(base)
    rules = [("default", r) for r in default_rules()] + [("bulk", r) for r in bulk_dirs(base)]
    accepted, rejected, files = validate(src, base, rules)
    folders, types = summarize(files)
    rec["t_rules"] = round(time.monotonic() - t, 1)
    t = time.monotonic()
    try:
        proposed = model_rules(router, role, folders, types) if files else []
        rec["model"] = "ok"
    except Exception as e:                         # model down: defaults and bulk rules still apply
        proposed, rec["model"] = [], f"unavailable ({type(e).__name__})"
    more, rej2, files = validate(src, files, [("model", r) for r in proposed if r not in {x for _, x in accepted}],
                              prior=accepted)
    rec["t_model_rules"] = round(time.monotonic() - t, 1)
    accepted += more
    rejected += rej2
    keep = sum(1 for _, rel in files if keep_file(rel))
    rec.update(files=len(files), mb=round(sum(s for s, _ in files) / 1e6, 1), code=keep,
               rules=[f"{s}:{r}" for s, r in accepted], rejected=[f"{s}:{r} ({why})" for s, r, why in rejected])
    excl = DATA / "rebuilds" / f"{name}.exclude"
    excl.parent.mkdir(parents=True, exist_ok=True)
    excl.write_text(f"# hk rebuild-all {run.name} for {src}\n# default: generic outputs; bulk: folders of "
                    "generated files; model: proposed by the local model. Each rule dropped no code.\n"
                    + "".join(f"{r}\n" for _, r in accepted))
    rec["exclude_file"] = str(excl)
    if keep == 0:
        return {**rec, "status": "no_code"}
    if len(files) > limits["files"] or rec["mb"] > limits["mb"]:
        rec["top"] = folders[:10]
        return {**rec, "status": "too_big"}
    if not apply:
        return {**rec, "status": "planned"}
    t = time.monotonic()
    r = subprocess.run(["bash", str(SCRIPT), src, name, "--exclude-from", str(excl), "--apply"],
                       capture_output=True, text=True, errors="replace", cwd=HK,
                       env={**os.environ, "HK_SKIP_CHECKER": "1"})
    rec["t_build"] = round(time.monotonic() - t, 1)
    if r.returncode != 0 or not (dst / ".git").exists():
        tail = (r.stderr or r.stdout).strip().splitlines()[-3:]
        status = "refused" if any("protected" in t or "refusing" in t for t in tail) else "rebuild_failed"
        return {**rec, "status": status, "note": " | ".join(tail)}
    newest = c.get("newest", "")
    try:
        recent = datetime.fromisoformat(newest) > datetime.now(timezone.utc) - timedelta(days=90)
    except ValueError:
        recent = False
    t = time.monotonic()
    try:
        what = draft_readme(router, role, dst, types)
        rec["readme"] = "drafted"
    except Exception as e:
        what, rec["readme"] = None, f"todo ({type(e).__name__})"
    rec["t_readme"] = round(time.monotonic() - t, 1)
    set_readme(dst, what, "partial" if recent else "parked")
    subprocess.run(["git", "-C", str(dst), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(dst), "commit", "-q", "-m", "README: what it is, state",
                    "-m", "Drafted by the local model in hk rebuild-all; review before pushing."], check=False)
    rec["what"] = what or ""
    rec["checks"] = checker(dst)
    return {**rec, "status": "built"}


# ---------- commands ----------

def cmd_plan(a):
    dirs_tsv = Path(a.dirs) if a.dirs else max((DATA / "inventory").glob("dirs_*.tsv"), default=None)
    if not dirs_tsv or not dirs_tsv.is_file():
        print("no pipeline-dirs table; run process 2 first or pass --dirs"); return 2
    cands, too_big = sel.select(sel.read_dirs(dirs_tsv, str(HOME)), min_code=a.min_code, max_code=a.max_code)
    run = Path(a.out) if a.out else DATA / "batch" / datetime.now().strftime("%Y%m%dT%H%M%S")
    run.mkdir(parents=True, exist_ok=True)
    rows = [{"name": c.name, "source": c.path, "code_files": c.subtree_code, "newest": c.newest,
             "nested": c.nested, "copies": len(c.copies), "copy_paths": " | ".join(c.copies)} for c in cands]
    write_tsv(run / "candidates.tsv", rows, list(rows[0]) if rows else ["name"])
    write_tsv(run / "split_containers.tsv", [{"source": d.path, "code_files": d.subtree_code} for d in too_big],
              ["source", "code_files"])
    latest = DATA / "batch" / "latest"
    latest.parent.mkdir(parents=True, exist_ok=True)
    if latest.is_symlink() or not latest.exists():
        latest.unlink(missing_ok=True); latest.symlink_to(run.resolve())
    print(f"from {dirs_tsv}\n{len(cands)} candidates ({sum(len(c.copies) for c in cands)} copies collapsed), "
          f"{len(too_big)} large containers split into their parts\nrun folder: {run}")
    for c in cands[:a.show]:
        print(f"  {c.name:40s} {c.subtree_code:5d} code  {c.newest[:10]}  {len(c.copies)} copies  {c.rel}")
    log_line(logpath(), "rebuild_all.plan", dirs_tsv, run, len(cands))
    return 0


def cmd_run(a):
    run = Path(a.run)
    cands = read_tsv(run / "candidates.tsv")
    if a.only:
        cands = [c for c in cands if c["name"] in set(a.only)]
    done = latest_state(run)
    todo = [c for c in cands if done.get(c["name"], {}).get("status") not in FINAL
            and not (not a.apply and done.get(c["name"], {}).get("status") == "planned")]
    finished, waiting = len(cands) - len(todo), len(todo)
    todo = todo[:a.limit] if a.limit else todo
    roles = [r for r in a.roles.split(",") if r]
    print(f"{len(cands)} candidates: {finished} finished, {waiting} waiting, {len(todo)} this run; roles {roles} x {a.jobs} jobs; "
          f"{'APPLY: builds local repos, pushes nothing' if a.apply else 'dry run: rules and sizes only'}", flush=True)
    limits = {"files": a.max_files, "mb": a.max_mb}
    stop = threading.Event()

    def worker(i):
        role = roles[i % len(roles)]       # workers alternate roles, so each node gets a.jobs at a time
        n = 0
        while not stop.is_set():
            with LOCK:
                if not todo:
                    return n
                c = todo.pop(0)
            try:
                rec = process(run, c, a.apply, a.router, role, limits)
            except Exception as e:
                rec = {"name": c["name"], "source": c["source"], "status": "error", "note": f"{type(e).__name__}: {e}"[:300]}
            record(run, rec)
            n += 1
            print(json.dumps({k: rec.get(k) for k in ("name", "status", "files", "mb", "code", "model", "readme",
                                                     "t_rules", "t_model_rules", "t_build", "t_readme")
                              if rec.get(k) is not None}), flush=True)
            if n % 5 == 0:
                with LOCK:
                    write_report(run)
        return n

    try:
        n_workers = len(roles) * a.jobs
        with ThreadPoolExecutor(n_workers) as ex:
            list(ex.map(worker, range(n_workers)))
    except KeyboardInterrupt:
        stop.set()
    write_report(run)
    print(f"report: {run / 'report.md'}\nreview: {run / 'review.tsv'} (set push to yes/no), then hk rebuild-all push {run}")
    return 0


REVIEW_FIELDS = ["push", "name", "status", "checks_failed", "files", "mb", "code", "readme", "what", "source",
                 "copies", "rules", "rejected", "note"]


def write_report(run: Path):
    state = latest_state(run)
    # Your push edits survive a new report while the candidate's status is unchanged.
    old = ({(r["name"], r["status"]): r["push"] for r in read_tsv(run / "review.tsv")}
           if (run / "review.tsv").is_file() else {})
    rows = []
    for name, r in sorted(state.items(), key=lambda x: (x[1]["status"], x[0])):
        failed = [k for k, v in (r.get("checks") or {}).items() if v != "PASS"]
        ok = r["status"] == "built" and not failed and r.get("readme") == "drafted"
        rows.append({"push": old.get((name, r["status"]), "yes" if ok else "no"), "name": name, "status": r["status"],
                     "checks_failed": ",".join(failed), "files": r.get("files", ""), "mb": r.get("mb", ""),
                     "code": r.get("code", ""), "readme": r.get("readme", ""), "what": r.get("what", ""),
                     "source": r.get("source", ""), "copies": r.get("copies", ""),
                     "rules": " ; ".join(r.get("rules", [])), "rejected": " ; ".join(r.get("rejected", [])),
                     "note": r.get("note", "") or r.get("model", "")})
    write_tsv(run / "review.tsv", rows, REVIEW_FIELDS)
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    md = [f"# Rebuild-all {run.name}", "", "Status counts: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())), "",
          "Push defaults to yes only for built repos with every check PASS and a drafted README. "
          f"Edit the push column in `{run / 'review.tsv'}`, then `hk rebuild-all push {run}` (dry run) and add `--apply`.", ""]
    for status in ("built", "already_built", "too_big", "planned", "refused", "rebuild_failed", "name_taken", "no_code", "error"):
        group = [r for r in rows if r["status"] == status]
        if not group:
            continue
        md += [f"## {status} ({len(group)})", ""]
        for r in group:
            md.append(f"- **{r['name']}** push={r['push']} files={r['files']} MB={r['mb']} code={r['code']}"
                      + (f" FAIL={r['checks_failed']}" if r["checks_failed"] else "") + f"  \n  `{r['source']}`")
            if r["what"]:
                md.append(f"  \n  {r['what']}")
            if status == "too_big" and state[r["name"]].get("top"):
                md.append("  \n  largest folders after rules: " + "; ".join(x.split("|")[-1].strip() + " (" + x.split("|")[0].strip() + ")"
                                                                     for x in state[r["name"]]["top"][:5]))
            if r["note"] and r["note"] != "ok":
                md.append(f"  \n  note: {r['note']}")
        md.append("")
    (run / "report.md").write_text("\n".join(md))


def cmd_report(a):
    write_report(Path(a.run))
    print(Path(a.run) / "report.md")
    return 0


def cmd_push(a):
    run = Path(a.run)
    write_report(run)
    rows = [r for r in read_tsv(run / "review.tsv") if r["push"].strip().lower() == "yes"]
    n = 0
    for r in rows:
        dst = PROJECTS / r["name"]
        if not (dst / ".git").exists():
            print(f"skip {r['name']}: no repo at {dst}"); continue
        if subprocess.run(["git", "-C", str(dst), "remote", "get-url", "origin"], capture_output=True).returncode == 0:
            print(f"skip {r['name']}: already has a remote"); continue
        cmd = ["gh", "repo", "create", f"{a.owner}/{r['name']}", "--private", "--source", str(dst), "--push"]
        print(" ".join(cmd))
        if a.apply:
            p = subprocess.run(cmd, capture_output=True, text=True)
            ok = p.returncode == 0
            print("  pushed" if ok else f"  failed: {(p.stderr or p.stdout).strip().splitlines()[-1:]}")
            log_line(logpath(), "rebuild_all.push" if ok else "rebuild_all.push_failed", dst, f"{a.owner}/{r['name']}", r["name"])
            n += ok
    print(f"{len(rows)} rows marked yes; " + (f"{n} pushed" if a.apply else "dry run: add --apply to create the private repos"))
    return 0


def parser():
    p = argparse.ArgumentParser(prog="hk rebuild-all", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("plan")
    pl.add_argument("--dirs", help="pipeline-dirs TSV (default: newest in ~/data/housekeeping/inventory)")
    pl.add_argument("--out")
    pl.add_argument("--min-code", type=int, default=3)
    pl.add_argument("--max-code", type=int, default=400, help="larger folders are split into their parts")
    pl.add_argument("--show", type=int, default=40)
    ru = sub.add_parser("run")
    ru.add_argument("run")
    ru.add_argument("--apply", action="store_true")
    ru.add_argument("--limit", type=int, default=0)
    ru.add_argument("--only", nargs="*")
    ru.add_argument("--roles", default="reader,coder", help="comma separated; work is spread across them")
    ru.add_argument("--jobs", type=int, default=3,
                    help="candidates in flight per role; rsync and git overlap the model calls")
    ru.add_argument("--router", default=os.environ.get("HK_ROUTER", "http://127.0.0.1:8000"))
    ru.add_argument("--max-files", type=int, default=3000)
    ru.add_argument("--max-mb", type=float, default=150)
    rp = sub.add_parser("report")
    rp.add_argument("run")
    pu = sub.add_parser("push")
    pu.add_argument("run")
    pu.add_argument("--owner", default="Radar41")
    pu.add_argument("--apply", action="store_true")
    return p


def main(argv):
    a = parser().parse_args(argv)
    return {"plan": cmd_plan, "run": cmd_run, "report": cmd_report, "push": cmd_push}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
