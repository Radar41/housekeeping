"""hk intake: rclone wrapper for the Windows migration.

Reads from source remotes, writes only under <target_root>/<machine>/. Remotes are
configured by hand with rclone config on radar-41; this module never creates them.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from housekeeping import config
from housekeeping.log import log_line

FILTER_FILE = Path(__file__).with_name("filter.txt")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
REMOTE_RE = re.compile(r"^(:?[^:/\\]+:?):(.*)$")

# Every rclone call goes through run_rclone, which refuses anything that could write
# to a source. copy and check only ever take the source as their first path argument.
ALLOWED = {("copy",), ("check",), ("size",), ("backend", "features"), ("config", "dump")}


class IntakeError(Exception):
    pass


@dataclass(frozen=True)
class Source:
    name: str
    remote: str


@dataclass(frozen=True)
class Settings:
    machine: str
    target_root: Path
    sources: tuple
    logs: Path

    @property
    def machine_dir(self) -> Path:
        return self.target_root / self.machine

    @property
    def check_file(self) -> Path:
        return self.machine_dir / "CHECK.txt"

    @property
    def action_log(self) -> Path:
        return self.logs / "intake.tsv"

    def dest(self, src: Source) -> Path:
        return self.machine_dir / src.name


def load_settings() -> Settings:
    cfg = config.load()
    sec = cfg.get("intake")
    if not isinstance(sec, dict):
        raise IntakeError("config has no [intake] section")
    machine = sec.get("machine")
    root = sec.get("target_root")
    raw = sec.get("sources")
    if not machine or not NAME_RE.match(str(machine)):
        raise IntakeError(f"intake.machine missing or not a plain name: {machine!r}")
    if not root:
        raise IntakeError("intake.target_root missing")
    if not isinstance(raw, list) or not raw:
        raise IntakeError("intake.sources is empty; add {name, remote} entries")
    sources, seen = [], set()
    for item in raw:
        if not isinstance(item, dict) or not item.get("name") or not item.get("remote"):
            raise IntakeError(f"bad source entry (need name and remote): {item!r}")
        name = str(item["name"])
        if not NAME_RE.match(name):
            raise IntakeError(f"source name is not a plain name: {name!r}")
        if name in seen:
            raise IntakeError(f"duplicate source name: {name}")
        seen.add(name)
        sources.append(Source(name, str(item["remote"])))
    logs = Path(cfg.get("paths", {}).get("logs", "logs"))
    if not logs.is_absolute():
        logs = config.REPO_ROOT / logs
    return Settings(str(machine), Path(root), tuple(sources), logs)


def run_rclone(args: list) -> subprocess.CompletedProcess:
    key = tuple(args[:2]) if args[0] in ("backend", "config") else (args[0],)
    if key not in ALLOWED:
        raise IntakeError(f"rclone {args[0]} is not allowed in intake")
    if shutil.which("rclone") is None:
        raise IntakeError("rclone not found on PATH")
    return subprocess.run(["rclone", *args], capture_output=True, text=True)


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def rclone_config_dump() -> dict:
    proc = run_rclone(["config", "dump"])
    if proc.returncode != 0:
        return {}
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def local_path_of(remote: str, dump: dict, depth: int = 0):
    """Local filesystem path a remote points at, or None if it is not local."""
    if depth > 8:
        return None
    m = REMOTE_RE.match(remote)
    if not m:
        return Path(remote)
    name, rest = m.group(1), m.group(2)
    if name.lower() == ":local":
        return Path(rest)
    info = dump.get(name)
    if not isinstance(info, dict):
        return None
    if info.get("type") == "local":
        return Path("/" + rest.lstrip("/"))
    if info.get("type") == "alias":
        base = str(info.get("remote", ""))
        joined = base if not rest.strip("/") else base.rstrip("/") + "/" + rest.lstrip("/")
        return local_path_of(joined, dump, depth + 1)
    return None


def check_sources_outside_target(st: Settings) -> None:
    dump = rclone_config_dump()
    target = Path(os.path.realpath(st.target_root))
    for src in st.sources:
        path = local_path_of(src.remote, dump)
        if path is None:
            continue
        real = Path(os.path.realpath(path))
        if real == target or target in real.parents or real in target.parents:
            raise IntakeError(
                f"refusing: source {src.name} ({src.remote}) overlaps target_root {st.target_root}"
            )


def hashes_of(remote: str) -> set:
    proc = run_rclone(["backend", "features", remote])
    if proc.returncode != 0:
        return set()
    try:
        feats = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return set()
    return {str(h).lower() for h in (feats.get("Hashes") or [])}


def usable_hashes(st: Settings, src: Source) -> bool:
    """True when source and target share a hash type, so --checksum can compare content."""
    have = hashes_of(src.remote)
    if not have:
        return False
    dst = hashes_of(str(st.target_root))
    return bool(have & dst) if dst else True


def filter_text() -> str:
    return FILTER_FILE.read_text(encoding="utf-8")


def copy_args(src: Source, dest: Path, logfile: Path, apply: bool, hashed: bool) -> list:
    # rclone copy keeps modtimes by default and skips finished files, so a rerun resumes.
    args = [
        "copy", src.remote, str(dest),
        "--filter-from", str(FILTER_FILE),
        "--create-empty-src-dirs",
        "--retries", "3", "--low-level-retries", "10",
        "--log-file", str(logfile), "--log-level", "INFO",
    ]
    if hashed:
        args.append("--checksum")
    if not apply:
        args.append("--dry-run")
    return args


def check_args(src: Source, dest: Path, logfile: Path, hashed: bool) -> list:
    args = [
        "check", src.remote, str(dest),
        "--one-way", "--combined", "-",
        "--filter-from", str(FILTER_FILE),
        "--log-file", str(logfile), "--log-level", "INFO",
    ]
    if not hashed:
        args.append("--download")
    return args


def count_transfers(logfile: Path) -> int:
    try:
        text = logfile.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return len(re.findall(r": (?:Copied \(|Skipped copy as --dry-run)", text))


def cmd_copy(st: Settings, apply: bool) -> int:
    check_sources_outside_target(st)
    mode = "APPLY" if apply else "DRY-RUN (nothing is written; pass --apply to copy)"
    print(f"intake copy [{mode}] machine={st.machine} target={st.machine_dir}")
    print("filter file:", FILTER_FILE)
    print(filter_text().rstrip("\n"))
    failed = 0
    for src in st.sources:
        dest = st.dest(src)
        run_id = f"{st.machine}-{src.name}-{stamp()}"
        logfile = st.logs / f"intake-copy-{run_id}{'' if apply else '-dry'}.log"
        logfile.parent.mkdir(parents=True, exist_ok=True)
        hashed = usable_hashes(st, src)
        args = copy_args(src, dest, logfile, apply, hashed)
        print(f"\n[{src.name}] rclone {' '.join(args)}")
        proc = run_rclone(args)
        action = "intake.copy" if apply else "intake.copy.dry"
        if proc.returncode != 0:
            action += ".fail"
            failed += 1
            print(f"[{src.name}] rclone exit {proc.returncode}: {proc.stderr.strip()[:500]}")
        else:
            n = count_transfers(logfile)
            verb = "copied" if apply else "would copy"
            print(f"[{src.name}] {verb} {n} files" if n else f"[{src.name}] up to date, nothing to do")
        log_line(st.action_log, action, src.remote, dest, run_id)
    return 1 if failed else 0


@dataclass
class CheckResult:
    name: str
    remote: str
    mode: str
    match: int = 0
    differ: int = 0
    missing: int = 0
    errors: int = 0
    rc: int = 0

    @property
    def result(self) -> str:
        if self.differ or self.missing:
            return "FAIL"
        if self.errors or self.rc != 0:
            return "ERROR"
        return "OK"


def parse_combined(text: str, res: CheckResult) -> None:
    """Symbols: = identical, * differs, + missing on destination, ! error. - is ignored."""
    for line in text.splitlines():
        sym = line[:1]
        if sym == "=":
            res.match += 1
        elif sym == "*":
            res.differ += 1
        elif sym == "+":
            res.missing += 1
        elif sym == "!":
            res.errors += 1


def render_check(results: list) -> str:
    blocks = []
    for r in results:
        blocks.append(
            f"== {r.name} ({r.remote}) ==\n"
            f"mode: {r.mode}\n"
            f"match: {r.match}\n"
            f"differ: {r.differ}\n"
            f"missing: {r.missing}\n"
            f"errors: {r.errors}\n"
            f"result: {r.result}\n"
        )
    return "\n".join(blocks)


def parse_check_file(path: Path) -> dict:
    """Return {source name: {field: value}} from CHECK.txt, empty if absent."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out, cur = {}, None
    for line in text.splitlines():
        m = re.match(r"^== (\S+) \(.*\) ==$", line)
        if m:
            cur = out.setdefault(m.group(1), {})
        elif cur is not None and ": " in line:
            k, v = line.split(": ", 1)
            cur[k] = v
    return out


def cmd_check(st: Settings) -> int:
    check_sources_outside_target(st)
    results = []
    for src in st.sources:
        dest = st.dest(src)
        run_id = f"{st.machine}-{src.name}-{stamp()}"
        logfile = st.logs / f"intake-check-{run_id}.log"
        logfile.parent.mkdir(parents=True, exist_ok=True)
        hashed = usable_hashes(st, src)
        args = check_args(src, dest, logfile, hashed)
        print(f"[{src.name}] rclone {' '.join(args)}")
        proc = run_rclone(args)
        res = CheckResult(src.name, src.remote, "hash" if hashed else "download", rc=proc.returncode)
        parse_combined(proc.stdout, res)
        (st.logs / f"intake-check-{run_id}.combined").write_text(proc.stdout, encoding="utf-8")
        if res.result == "ERROR":
            print(f"[{src.name}] rclone exit {proc.returncode}: {proc.stderr.strip()[:500]}")
        print(f"[{src.name}] match={res.match} differ={res.differ} missing={res.missing} "
              f"errors={res.errors} -> {res.result}")
        log_line(st.action_log, f"intake.check.{res.result.lower()}", src.remote, dest, run_id)
        results.append(res)
    body = render_check(results)
    st.machine_dir.mkdir(parents=True, exist_ok=True)
    try:
        unchanged = st.check_file.read_text(encoding="utf-8") == body
    except OSError:
        unchanged = False
    if unchanged:
        print(f"{st.check_file} unchanged")
    else:
        tmp = st.check_file.with_suffix(".txt.tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, st.check_file)
        print(f"wrote {st.check_file}")
    return 0 if all(r.result == "OK" for r in results) else 1


def cmd_report(st: Settings) -> int:
    checks = parse_check_file(st.check_file)
    print(f"{'source':<16}{'files':>10}{'bytes':>16}  check")
    for src in st.sources:
        proc = run_rclone(["size", "--json", str(st.dest(src))])
        count = nbytes = 0
        note = ""
        if proc.returncode == 0:
            try:
                data = json.loads(proc.stdout)
                count, nbytes = int(data["count"]), int(data["bytes"])
            except (json.JSONDecodeError, KeyError, ValueError):
                note = " (size unreadable)"
        else:
            note = " (not copied)"
        result = checks.get(src.name, {}).get("result", "unchecked")
        print(f"{src.name:<16}{count:>10}{nbytes:>16}  {result}{note}")
    return 0


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(prog="hk intake")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_copy = sub.add_parser("copy", help="rclone copy each source into the inbox (dry-run by default)")
    p_copy.add_argument("--apply", action="store_true", help="really copy")
    sub.add_parser("check", help="rclone check --one-way each source, write CHECK.txt")
    sub.add_parser("report", help="file count, bytes and check result per source")
    try:
        ns = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    try:
        st = load_settings()
        if ns.cmd == "copy":
            return cmd_copy(st, ns.apply)
        if ns.cmd == "check":
            return cmd_check(st)
        return cmd_report(st)
    except IntakeError as e:
        print(f"intake: {e}", file=sys.stderr)
        return 2
