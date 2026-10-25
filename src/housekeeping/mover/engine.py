"""Plan, apply and undo moves from a decision table. Nothing is deleted; every move is logged."""
import csv
import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from housekeeping import config
from housekeeping.log import log_line

DECISIONS = {"live", "parked", "protected", "absorbed"}
TRUE = {"1", "true", "yes", "y", "t"}
MOVE_ACTIONS = {"move", "quarantine"}


@dataclass
class Settings:
    home: Path
    moves_log: Path
    quarantine: Path
    protected: Path
    batch: str
    day: str

    @classmethod
    def from_config(cls, batch: str) -> "Settings":
        cfg = config.load()
        mv = cfg["mover"]
        return cls(
            home=Path(cfg["paths"]["home"]),
            moves_log=Path(mv["moves_log"]),
            quarantine=Path(mv["quarantine"]),
            protected=Path(mv["protected_dir"]),
            batch=batch,
            day=date.today().isoformat(),
        )

    @property
    def batch_dir(self) -> Path:
        return self.quarantine / f"{self.day}-{self.batch}"


@dataclass
class Row:
    chain_id: str
    source_path: str
    decision: str
    project: str
    target_repo: str
    year: str
    canonical: bool


@dataclass
class Step:
    row: Row
    status: str  # todo, done, skip, refused, error
    kind: str  # move, quarantine, none
    src: Path | None = None
    dst: Path | None = None
    sha: str = ""
    reason: str = ""
    repo: Path | None = None


def read_table(path: Path) -> list[Row]:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        need = {"chain_id", "source_path", "decision", "project", "target_repo", "year", "is_canonical"}
        missing = need - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"table is missing columns: {sorted(missing)}")
        rows = []
        for r in reader:
            g = {k: (r.get(k) or "").strip() for k in need}
            rows.append(Row(g["chain_id"], g["source_path"], g["decision"].lower(), g["project"],
                            g["target_repo"], g["year"], g["is_canonical"].lower() in TRUE))
        return rows


def _file_sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha_of(p: Path) -> str:
    """sha256 of a file, or of a sorted manifest of a tree. Symlinks are hashed by link text."""
    if p.is_symlink():
        return hashlib.sha256(("l\0" + os.readlink(p)).encode()).hexdigest()
    if p.is_dir():
        h = hashlib.sha256()
        for q in sorted(p.rglob("*")):
            rel = q.relative_to(p).as_posix()
            if q.is_symlink():
                h.update(f"l\0{rel}\0{os.readlink(q)}\n".encode())
            elif q.is_dir():
                h.update(f"d\0{rel}\n".encode())
            else:
                h.update(f"f\0{rel}\0{_file_sha(q)}\n".encode())
        return h.hexdigest()
    return _file_sha(p)


def _within(path: Path, root: Path) -> bool:
    roots = {Path(os.path.abspath(root)), root.resolve()}
    cands = {Path(os.path.abspath(path)), path.resolve()}
    for c in cands:
        for r in roots:
            if c == r or r in c.parents:
                return True
    return False


def touches_protected(path: Path, protected: Path) -> bool:
    """True if the path, or any symlink inside a tree, resolves under the protected root."""
    if _within(path, protected):
        return True
    if path.is_dir() and not path.is_symlink():
        for dirpath, dirs, files in os.walk(path):
            for name in dirs + files:
                full = Path(dirpath) / name
                if full.is_symlink() and _within(full, protected):
                    return True
    return False


def same_fs(src: Path, dst_parent: Path) -> bool:
    return os.stat(src, follow_symlinks=False).st_dev == os.stat(dst_parent).st_dev


def move_safe(src: Path, dst: Path, sha: str) -> None:
    """Rename on one filesystem; otherwise copy, verify sha256, then remove the source."""
    if os.path.lexists(dst):
        raise FileExistsError(f"refusing to overwrite {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if same_fs(src, dst.parent):
        os.rename(src, dst)
        if sha_of(dst) != sha:
            os.rename(dst, src)
            raise OSError(f"sha256 mismatch after rename, restored {src}")
        return
    if src.is_symlink():
        os.symlink(os.readlink(src), dst)
    elif src.is_dir():
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst)
    if sha_of(dst) != sha:
        raise OSError(f"sha256 mismatch after copy, source kept at {src}, partial copy at {dst}")
    if src.is_dir() and not src.is_symlink():
        shutil.rmtree(src)
    else:
        src.unlink()


def _safe_name(value: str) -> bool:
    return bool(value) and "/" not in value and "\\" not in value and value not in {".", ".."}


def _target_dir(s: Settings, row: Row) -> Path:
    if row.decision == "live":
        if not _safe_name(row.project):
            raise ValueError(f"bad project name {row.project!r}")
        return s.home / "projects" / row.project
    if row.decision == "absorbed":
        tr = Path(row.target_repo)
        if tr.is_absolute():
            return tr
        if not row.target_repo or ".." in tr.parts:
            raise ValueError(f"bad target_repo {row.target_repo!r}")
        return s.home / "projects" / tr
    if not (_safe_name(row.project) and _safe_name(row.year)):
        raise ValueError(f"bad project or year {row.project!r} {row.year!r}")
    return s.home / "archive" / row.year / row.project


def _quarantine_dest(s: Settings, src: Path) -> Path:
    try:
        rel = src.relative_to(s.home)
    except ValueError:
        rel = Path(*src.parts[1:]) if src.is_absolute() else src
    return s.batch_dir / rel


def _unique(p: Path, sha: str) -> Path:
    if not os.path.lexists(p):
        return p
    q = p.with_name(f"{p.name}.{sha[:8]}")
    n = 1
    while os.path.lexists(q):
        q = p.with_name(f"{p.name}.{sha[:8]}.{n}")
        n += 1
    return q


def read_log(path: Path) -> list[tuple[str, str, str, str, str]]:
    if not Path(path).exists():
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) == 5:
                out.append(tuple(parts))
    return out


def open_moves(entries) -> list[tuple[str, str, str, str, str]]:
    """Moves and quarantines not yet undone, in log order."""
    pending = []
    for e in entries:
        if e[1] in MOVE_ACTIONS:
            pending.append(e)
        elif e[1] == "undo":
            for i in range(len(pending) - 1, -1, -1):
                if pending[i][2] == e[3] and pending[i][3] == e[2]:
                    del pending[i]
                    break
    return pending


def _split_ident(ident: str) -> tuple[str, str]:
    chain, _, sha = ident.rpartition(":")
    return chain, sha


def plan_row(s: Settings, row: Row, claimed: set[Path], pending) -> Step:
    if row.decision not in DECISIONS:
        return Step(row, "error", "none", reason=f"unknown decision {row.decision!r}")
    if row.decision == "protected":
        return Step(row, "skip", "none", reason="protected decision, never touched")
    if not row.source_path:
        return Step(row, "error", "none", reason="empty source_path")
    src = Path(os.path.abspath(row.source_path))
    repo = None
    try:
        if row.canonical:
            tdir = _target_dir(s, row)
            repo = tdir if row.decision in {"live", "absorbed"} else None
            dst = tdir / src.name
            kind, why = "move", ""
        else:
            dst, kind, why = _quarantine_dest(s, src), "quarantine", "non-canonical"
    except ValueError as e:
        return Step(row, "error", "none", src=src, reason=str(e))
    for p in (src, dst):
        if touches_protected(p, s.protected):
            return Step(row, "refused", kind, src, dst, reason="resolves under the protected root")
    for e in reversed(pending):
        if e[2] == str(src) and _split_ident(e[4])[0] == row.chain_id:
            logged = Path(e[3])
            sha = _split_ident(e[4])[1]
            if not os.path.lexists(src) and os.path.lexists(logged) and sha_of(logged) == sha:
                return Step(row, "done", e[1], src, logged, sha, "already done")
            break
    if not os.path.lexists(src):
        return Step(row, "error", kind, src, dst, reason="source missing")
    sha = sha_of(src)
    if os.path.lexists(dst) or dst in claimed:
        why = f"collision at {dst}"
        dst = _unique(_quarantine_dest(s, src), sha)
        kind, repo = "quarantine", None
        if touches_protected(dst, s.protected):
            return Step(row, "refused", kind, src, dst, reason="resolves under the protected root")
    claimed.add(dst)
    return Step(row, "todo", kind, src, dst, sha, why, repo)


def build_plan(s: Settings, rows: list[Row]) -> list[Step]:
    pending = open_moves(read_log(s.moves_log))
    claimed: set[Path] = set()
    return [plan_row(s, r, claimed, pending) for r in rows]


def fmt(st: Step, label: str | None = None) -> str:
    tag = label or st.status
    parts = [tag, st.kind, st.row.chain_id, str(st.src or st.row.source_path)]
    if st.dst:
        parts.append("-> " + str(st.dst))
    if st.repo is not None and not (st.repo / ".git").exists():
        parts.append("(git init)")
    if st.reason:
        parts.append(f"[{st.reason}]")
    return "\t".join(parts)


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    return r.stdout.strip()


def _identity(repo: Path) -> list[str]:
    have = subprocess.run(["git", "-C", str(repo), "config", "user.name"], capture_output=True, text=True)
    if have.stdout.strip():
        return []
    return ["-c", "user.name=housekeeping", "-c", "user.email=housekeeping@localhost"]


def _manifest(s: Settings, st: Step) -> None:
    s.batch_dir.mkdir(parents=True, exist_ok=True)
    with open(s.batch_dir / "manifest.tsv", "a", encoding="utf-8") as f:
        f.write("\t".join([st.row.chain_id, st.sha, str(st.src), str(st.dst), st.reason]) + "\n")


def apply_step(s: Settings, st: Step, push: bool) -> list[str]:
    """Perform one planned step. Returns extra notes (for example a failed push)."""
    notes = []
    log = s.moves_log
    ident = f"{st.row.chain_id}:{st.sha}"
    if st.repo is not None and not (st.repo / ".git").exists():
        st.repo.mkdir(parents=True, exist_ok=True)
        _git(st.repo, "init")
        log_line(log, "git-init", st.repo, st.repo, st.row.chain_id)
    move_safe(st.src, st.dst, st.sha)
    log_line(log, st.kind, st.src, st.dst, ident)
    if st.kind == "quarantine":
        _manifest(s, st)
    if st.repo is not None:
        rel = st.dst.relative_to(st.repo).as_posix()
        msg = f"mover: add {st.src} (chain {st.row.chain_id})"
        _git(st.repo, "add", "--", rel)
        _git(st.repo, *_identity(st.repo), "commit", "-m", msg, "--", rel)
        log_line(log, "git-commit", st.src, st.repo, ident)
        if push:
            try:
                _git(st.repo, "push")
                log_line(log, "git-push", st.repo, "origin", st.row.chain_id)
            except subprocess.CalledProcessError as e:
                notes.append(f"push failed: {e.stderr.strip()}")
    return notes


@dataclass
class UndoStep:
    entry: tuple
    status: str
    reason: str = ""


def plan_undo(entries, since: datetime | None) -> list[UndoStep]:
    out = []
    for e in reversed(open_moves(entries)):
        if since is not None:
            ts = datetime.fromisoformat(e[0])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < since:
                continue
        _, _, src, dst, ident = e
        sha = _split_ident(ident)[1]
        if not os.path.lexists(dst):
            out.append(UndoStep(e, "error", "target missing"))
        elif os.path.lexists(src):
            out.append(UndoStep(e, "error", "original location is occupied"))
        elif sha_of(Path(dst)) != sha:
            out.append(UndoStep(e, "error", "sha256 differs from the log"))
        else:
            out.append(UndoStep(e, "todo"))
    return out


def apply_undo(log: Path, u: UndoStep) -> None:
    _, _, src, dst, ident = u.entry
    move_safe(Path(dst), Path(src), _split_ident(ident)[1])
    log_line(log, "undo", dst, src, ident)
