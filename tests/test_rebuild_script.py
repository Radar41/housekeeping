"""scripts/rebuild-pipeline.sh: dry run by default, source untouched, idempotent, refuses protected."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "rebuild-pipeline.sh"
NAMES = SCRIPT.parents[1] / "protected_names.txt"
pytestmark = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")


@pytest.fixture
def env(tmp_path, monkeypatch):
    if not NAMES.exists():
        NAMES.write_text("zz-protected-test-name\n")
        request_cleanup = True
    else:
        request_cleanup = False
    src = tmp_path / "old" / "fit"
    (src / "data").mkdir(parents=True)
    (src / "main.py").write_text('"""Fit."""\nimport os\nos.environ.get("ATLAS_DSN")\n')
    (src / "data" / "x.csv").write_text("1\n")
    (src / "w.bin").write_bytes(b"\0" * 6_000_000)
    e = dict(os.environ, HK_PROJECTS=str(tmp_path / "projects"), HK_DATA=str(tmp_path / "data"),
             HK_PROTECTED=str(tmp_path / "protected"), GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
             GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    yield tmp_path, src, e
    if request_cleanup:
        NAMES.unlink()


def run(e, *args):
    return subprocess.run(["bash", str(SCRIPT), *map(str, args)], env=e, capture_output=True, text=True)


def snapshot(p: Path):
    return sorted((str(f.relative_to(p)), f.stat().st_mtime_ns) for f in p.rglob("*"))


def test_dry_run_writes_nothing(env):
    tmp, src, e = env
    r = run(e, src, "fit")
    assert r.returncode == 0 and "dry run" in r.stdout and "files to copy: 1" in r.stdout
    assert not (tmp / "projects").exists()


def test_apply_then_unchanged(env):
    tmp, src, e = env
    before = snapshot(src)
    r = run(e, src, "fit", "--apply")
    assert r.returncode == 0, r.stderr
    dst = tmp / "projects" / "fit"
    tracked = subprocess.run(["git", "-C", dst, "ls-files"], capture_output=True, text=True).stdout.split()
    assert {"main.py", "README.md", "Makefile", "SOURCES.txt", "SOURCES.sha256", ".env.example",
            "pyproject.toml", ".gitignore"} <= set(tracked)
    assert "w.bin" not in tracked and not any(t.startswith("data/") for t in tracked)
    assert f"source: {src}" in (dst / "SOURCES.txt").read_text()
    assert "ATLAS_DSN=" in (dst / ".env.example").read_text()
    assert (tmp / "data" / "rebuilds.tsv").read_text().count("\n") == 1
    assert snapshot(src) == before
    r = run(e, src, "fit", "--apply")
    assert r.returncode == 0 and r.stdout.startswith("unchanged")


def test_refuses_existing_other_and_protected(env):
    tmp, src, e = env
    (tmp / "projects" / "fit").mkdir(parents=True)
    assert run(e, src, "fit", "--apply").returncode == 2
    prot = tmp / "protected" / "p"
    prot.mkdir(parents=True)
    (prot / "a.py").write_text("x = 1\n")
    r = run(e, prot, "p2", "--apply")
    assert r.returncode == 2 and "protected" in r.stderr
