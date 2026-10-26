"""hk checker <repo>... | --all <dir> [--json]. Read only: never modifies a repo."""
import json
import hashlib
import re
import subprocess
import tomllib
from pathlib import Path

from housekeeping import config
from housekeeping.log import log_line

EMDASH = chr(0x2014)
HEADINGS = ["What it is", "State", "How to run", "Depends on"]
MAX_HITS = 20

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}"),
    re.compile(r"password\s*=\s*[\"'][^\"'\s][^\"']*[\"']", re.IGNORECASE),
    re.compile(r"password\s*=\s*[^\s\"'$<{(][^\s]*", re.IGNORECASE),
]
ENV_READ = re.compile(r"os\.environ|os\.getenv|\bgetenv\(|process\.env")

CHECKS = ["git", "readme", "python", "entry", "env", "secrets", "emdash", "protected"]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True)


def tracked_files(repo: Path) -> list[str]:
    out = _git(repo, "ls-files", "-z")
    if out.returncode != 0:
        return []
    return [p for p in out.stdout.decode("utf-8", "replace").split("\0") if p]


def read_text(repo: Path, rel: str) -> str | None:
    """Text of a tracked regular file, or None if missing, a symlink, or binary."""
    p = repo / rel
    try:
        if p.is_symlink() or not p.is_file():
            return None
        data = p.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", "replace")


def _hits(repo: Path, files: list[str], test) -> list[str]:
    """path:line for every line where test(line) is true."""
    found = []
    for rel in files:
        text = read_text(repo, rel)
        if text is None:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if test(line):
                found.append(f"{rel}:{n}")
    return found


def _hit_result(found: list[str], label: str):
    if not found:
        return True, "none found"
    shown = ", ".join(f"{h} {label}" for h in found[:MAX_HITS])
    more = f", +{len(found) - MAX_HITS} more" if len(found) > MAX_HITS else ""
    return False, shown + more


def check_git(repo, files):
    if (repo / ".git").exists():
        return True, ".git present"
    return False, ".git missing"


def check_readme(repo, files):
    text = read_text(repo, "README.md") if "README.md" in files else None
    if text is None:
        return False, "README.md not tracked"
    found, sections, current, fence = [], {}, None, False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
        m = None if fence else re.match(r"(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if m:
            level, title = len(m.group(1)), m.group(2)
            if level == 1:
                current = None
                continue
            found.append(title)
            current = title
            sections.setdefault(title, [])
        elif current is not None:
            sections[current].append(line)
    if found != HEADINGS:
        return False, f"headings are {found}, expected {HEADINGS}"
    body = "\n".join(sections["State"]).lower()
    if not re.search(r"\b(working|partial|parked)\b", body):
        return False, "State must contain working, partial, or parked"
    return True, "headings and state ok"


def _requires_python_ok(spec: str) -> bool:
    m = re.match(r"\s*>=\s*(\d+)\.(\d+)", spec)
    return bool(m) and (int(m.group(1)), int(m.group(2))) >= (3, 12)


def check_python(repo, files):
    if not any(f.endswith(".py") for f in files):
        return True, "no tracked .py"
    if "pyproject.toml" not in files:
        return False, "pyproject.toml not tracked"
    try:
        with open(repo / "pyproject.toml", "rb") as f:
            spec = tomllib.load(f).get("project", {}).get("requires-python", "")
    except (OSError, tomllib.TOMLDecodeError):
        return False, "pyproject.toml unreadable"
    if not _requires_python_ok(spec):
        return False, f"requires-python is {spec!r}, need >=3.12"
    if "uv.lock" not in files:
        return False, "uv.lock not tracked"
    return True, "requires-python >=3.12 and uv.lock"


def check_entry(repo, files):
    if "run.sh" in files:
        return True, "run.sh"
    text = read_text(repo, "Makefile") if "Makefile" in files else None
    if text is None:
        return False, "no Makefile or run.sh"
    missing = [t for t in ("run", "test", "slurm")
               if not re.search(rf"^{t}\s*:", text, re.MULTILINE)]
    if missing:
        return False, "Makefile missing targets: " + ", ".join(missing)
    return True, "Makefile run, test, slurm"


def check_env(repo, files):
    problems = []
    if ".env" in files:
        problems.append(".env is tracked")
    if _git(repo, "check-ignore", "-q", ".env").returncode != 0:
        problems.append(".env is not gitignored")
    reads = bool(_hits(repo, [f for f in files if f.endswith((".py", ".js", ".ts", ".sh"))],
                       lambda line: ENV_READ.search(line)))
    if ((repo / ".env").exists() or reads) and ".env.example" not in files:
        problems.append(".env.example not committed")
    return (False, "; ".join(problems)) if problems else (True, "env ok")


def check_secrets(repo, files):
    found = _hits(repo, files, lambda line: any(p.search(line) for p in SECRET_PATTERNS))
    return _hit_result(found, "possible secret")


def unchanged_originals(repo: Path) -> set[str]:
    """Files still byte-identical to the copy recorded in SOURCES.sha256 by rebuild-pipeline.sh."""
    listing = repo / "SOURCES.sha256"
    if not listing.is_file():
        return set()
    same = set()
    for line in listing.read_text(encoding="utf-8", errors="replace").splitlines():
        digest, _, rel = line.partition("  ")
        rel = rel[2:] if rel.startswith("./") else rel
        p = repo / rel
        try:
            if rel and p.is_file() and not p.is_symlink() and hashlib.sha256(p.read_bytes()).hexdigest() == digest:
                same.add(rel)
        except OSError:
            pass
    return same


def check_emdash(repo, files):
    # Style rule for text written in the repo. Copied originals keep their bytes so they
    # still match Atlas by sha256; once a file is edited it is checked like any other.
    # Secrets and protected checks still cover every file.
    originals = unchanged_originals(repo)
    found = _hits(repo, [f for f in files if f not in originals], lambda line: EMDASH in line)
    ok, detail = _hit_result(found, "em dash")
    if ok and originals:
        detail = f"{detail}; {len(originals)} unchanged copied originals not style-checked"
    return ok, detail


def load_protected(path: str | None):
    if not path or not Path(path).is_file():
        return None
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    return [s.strip().lower() for s in lines if s.strip()]


def check_protected(repo, files, names):
    if names is None:
        return False, "protected names file not found"
    found = _hits(repo, files, lambda line: any(n in line.lower() for n in names))
    return _hit_result(found, "protected name match")


def run_checks(repo: Path, names) -> list[dict]:
    if (repo / ".git").exists():
        files = tracked_files(repo)
    else:
        files = []
    results = []
    for name in CHECKS:
        if name == "git":
            ok, detail = check_git(repo, files)
        elif name == "protected":
            ok, detail = check_protected(repo, files, names)
        elif name != "git" and not (repo / ".git").exists():
            ok, detail = False, "not a git repo"
        else:
            ok, detail = globals()[f"check_{name}"](repo, files)
        results.append({"repo": str(repo), "check": name,
                        "status": "PASS" if ok else "FAIL", "detail": detail})
    return results


def _head(repo: Path) -> str:
    out = _git(repo, "rev-parse", "HEAD")
    return out.stdout.decode().strip() if out.returncode == 0 else "-"


def main(argv: list[str]) -> int:
    as_json = "--json" in argv
    args = [a for a in argv if a != "--json"]
    if args and args[0] == "--all":
        if len(args) != 2 or not Path(args[1]).is_dir():
            print("usage: hk checker <repo>... | --all <dir> [--json]")
            return 2
        repos = sorted(p for p in Path(args[1]).iterdir()
                       if p.is_dir() and not p.name.startswith("."))
    elif args and not any(a.startswith("--") for a in args):
        repos = [Path(a) for a in args]
    else:
        print("usage: hk checker <repo>... | --all <dir> [--json]")
        return 2
    try:
        cfg = config.load()
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"config error: {e}")
        return 2
    names = load_protected(cfg.get("checker", {}).get("protected_names_file"))
    log_path = Path(cfg.get("paths", {}).get("logs", "logs")) / "checker.tsv"

    results = []
    for repo in repos:
        if not repo.is_dir():
            results.append({"repo": str(repo), "check": "git", "status": "FAIL",
                            "detail": "not a directory"})
            continue
        sha = _head(repo)
        for r in run_checks(repo, names):
            log_line(log_path, f"check-{r['status'].lower()}", r["repo"], r["check"], sha)
            results.append(r)

    if as_json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            print(f"{r['status']}\t{r['check']}\t{r['repo']}\t{r['detail']}")
    return 1 if any(r["status"] == "FAIL" for r in results) else 0
