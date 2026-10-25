import csv
import hashlib
import os
import subprocess

import pytest

from housekeeping import config
from housekeeping.mover import cli, engine

HEAD = ["chain_id", "source_path", "decision", "project", "target_repo", "year", "is_canonical"]


def snapshot(root):
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_symlink():
            out[rel] = ("l", os.readlink(p))
        elif p.is_dir():
            out[rel] = ("d",)
        else:
            out[rel] = ("f", hashlib.sha256(p.read_bytes()).hexdigest())
    return out


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for d in ("projects", "archive", "quarantine", "protected", "inbox"):
        (home / d).mkdir(parents=True)
    (home / "protected" / "secret.txt").write_text("keep out")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[paths]\nhome = "{home}"\n'
        f'[mover]\nmoves_log = "{tmp_path / "moves.tsv"}"\n'
        f'quarantine = "{home / "quarantine"}"\nprotected_dir = "{home / "protected"}"\n'
    )
    monkeypatch.setenv("HK_CONFIG", str(cfg))
    config.load.cache_clear()
    monkeypatch.setattr(engine, "date", type("D", (), {"today": staticmethod(lambda: __import__("datetime").date(2026, 1, 2))}))
    inbox = home / "inbox" / "box" / "c"
    inbox.mkdir(parents=True)
    (inbox / "a.txt").write_text("alpha")
    (inbox / "b.txt").write_text("beta")
    (inbox / "old.txt").write_text("old")
    (inbox / "dup.txt").write_text("dup")
    (inbox / "tree").mkdir()
    (inbox / "tree" / "x.bin").write_bytes(b"\x00\x01")
    yield home, tmp_path
    config.load.cache_clear()


def table(tmp_path, rows):
    p = tmp_path / "t1.csv"
    with open(p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEAD)
        w.writerows(rows)
    return p


def std_rows(home):
    s = home / "inbox" / "box" / "c"
    return [
        ["c1", s / "a.txt", "live", "alpha", "", "", "1"],
        ["c2", s / "b.txt", "absorbed", "", "alpha", "", "1"],
        ["c3", s / "old.txt", "parked", "oldproj", "", "2024", "1"],
        ["c4", s / "dup.txt", "live", "alpha", "", "", "0"],
        ["c5", s / "tree", "live", "treeproj", "", "", "1"],
    ]


def run(capsys, *argv):
    rc = cli.main(list(argv))
    return rc, capsys.readouterr().out


def test_plan_prints_and_changes_nothing(env, capsys):
    home, tmp = env
    t = table(tmp, std_rows(home))
    before = snapshot(home)
    rc, out = run(capsys, "plan", str(t))
    assert rc == 0
    assert snapshot(home) == before
    assert not (tmp / "moves.tsv").exists()
    assert f"-> {home}/projects/alpha/a.txt" in out
    assert "(git init)" in out
    assert f"-> {home}/archive/2024/oldproj/old.txt" in out
    assert f"{home}/quarantine/2026-01-02-t1/inbox/box/c/dup.txt" in out
    assert "dry-run: nothing changed" in out


def test_apply_without_flag_is_dry_run(env, capsys):
    home, tmp = env
    t = table(tmp, std_rows(home))
    before = snapshot(home)
    rc, out = run(capsys, "apply", str(t))
    assert snapshot(home) == before and "dry-run" in out


def test_apply_idempotent_and_undo(env, capsys):
    home, tmp = env
    t = table(tmp, std_rows(home))
    before = snapshot(home / "inbox")
    rc, out = run(capsys, "apply", str(t), "--apply")
    assert rc == 0, out
    assert (home / "projects/alpha/a.txt").read_text() == "alpha"
    assert (home / "projects/alpha/b.txt").read_text() == "beta"
    assert (home / "archive/2024/oldproj/old.txt").read_text() == "old"
    assert (home / "projects/treeproj/tree/x.bin").read_bytes() == b"\x00\x01"
    q = home / "quarantine/2026-01-02-t1"
    assert (q / "inbox/box/c/dup.txt").read_text() == "dup"
    manifest = (q / "manifest.tsv").read_text().split("\t")
    assert manifest[0] == "c4" and manifest[1] == hashlib.sha256(b"dup").hexdigest()
    log = engine.read_log(tmp / "moves.tsv")
    assert [e[1] for e in log].count("move") == 4 and [e[1] for e in log].count("quarantine") == 1
    assert all(len(e[4].rpartition(":")[2]) == 64 for e in log if e[1] in ("move", "quarantine"))
    msg = subprocess.run(["git", "-C", str(home / "projects/alpha"), "log", "--format=%s"],
                         capture_output=True, text=True, check=True).stdout
    assert "chain c1" in msg and "a.txt" in msg and "chain c2" in msg
    assert snapshot(home / "inbox") != before

    size = (tmp / "moves.tsv").stat().st_size
    snap = snapshot(home)
    rc, out = run(capsys, "apply", str(t), "--apply")
    assert rc == 0 and "summary: done=5" in out
    assert snapshot(home) == snap and (tmp / "moves.tsv").stat().st_size == size

    rc, out = run(capsys, "undo", str(tmp / "moves.tsv"), "--apply")
    assert rc == 0, out
    assert snapshot(home / "inbox") == before
    assert not (home / "archive/2024/oldproj/old.txt").exists()
    assert [e[1] for e in engine.read_log(tmp / "moves.tsv")].count("undo") == 5
    rc, out = run(capsys, "undo", str(tmp / "moves.tsv"), "--apply")
    assert "nothing to undo" in out


def test_undo_dry_run_and_since(env, capsys):
    home, tmp = env
    t = table(tmp, std_rows(home)[:1])
    run(capsys, "apply", str(t), "--apply")
    rc, out = run(capsys, "undo", str(tmp / "moves.tsv"))
    assert "plan" in out and (home / "projects/alpha/a.txt").exists()
    rc, out = run(capsys, "undo", str(tmp / "moves.tsv"), "--since", "2999-01-01T00:00:00", "--apply")
    assert "nothing to undo" in out and (home / "projects/alpha/a.txt").exists()


def test_undo_refuses_changed_file(env, capsys):
    home, tmp = env
    run(capsys, "apply", str(table(tmp, std_rows(home)[:1])), "--apply")
    (home / "projects/alpha/a.txt").write_text("edited")
    rc, out = run(capsys, "undo", str(tmp / "moves.tsv"), "--apply")
    assert rc == 1 and "sha256 differs" in out
    assert (home / "projects/alpha/a.txt").read_text() == "edited"


def test_cross_filesystem_copy_verify_remove(env, capsys, monkeypatch):
    home, tmp = env
    monkeypatch.setattr(engine, "same_fs", lambda a, b: False)
    before = snapshot(home / "inbox")
    t = table(tmp, std_rows(home))
    rc, out = run(capsys, "apply", str(t), "--apply")
    assert rc == 0, out
    assert not (home / "inbox/box/c/a.txt").exists()
    assert (home / "projects/treeproj/tree/x.bin").read_bytes() == b"\x00\x01"
    rc, out = run(capsys, "undo", str(tmp / "moves.tsv"), "--apply")
    assert rc == 0 and snapshot(home / "inbox") == before


def test_failed_verify_keeps_source(env, capsys, monkeypatch):
    home, tmp = env
    monkeypatch.setattr(engine, "same_fs", lambda a, b: False)
    real = engine.shutil.copy2
    def bad_copy(s, d, **kw):
        real(s, d, **kw)
        with open(d, "ab") as f:
            f.write(b"x")
    monkeypatch.setattr(engine.shutil, "copy2", bad_copy)
    rc, out = run(capsys, "apply", str(table(tmp, std_rows(home)[:1])), "--apply")
    assert rc == 1 and "sha256 mismatch" in out
    assert (home / "inbox/box/c/a.txt").read_text() == "alpha"


def test_protected_decision_untouched(env, capsys):
    home, tmp = env
    t = table(tmp, [["p1", home / "protected" / "secret.txt", "protected", "", "", "", "1"]])
    rc, out = run(capsys, "apply", str(t), "--apply")
    assert rc == 0 and "skip" in out
    assert (home / "protected/secret.txt").read_text() == "keep out"
    assert not (tmp / "moves.tsv").exists()


def test_refuses_source_under_protected(env, capsys):
    home, tmp = env
    t = table(tmp, [["p2", home / "protected" / "secret.txt", "live", "leak", "", "", "1"]])
    rc, out = run(capsys, "apply", str(t), "--apply")
    assert rc == 1 and "refused" in out
    assert (home / "protected/secret.txt").exists() and not (home / "projects/leak").exists()


def test_refuses_via_symlink(env, capsys):
    home, tmp = env
    link = home / "inbox" / "link.txt"
    link.symlink_to(home / "protected" / "secret.txt")
    linkdir = home / "inbox" / "linkdir"
    linkdir.symlink_to(home / "protected")
    holder = home / "inbox" / "holder"
    holder.mkdir()
    (holder / "inner").symlink_to(home / "protected" / "secret.txt")
    # a project directory that is a symlink into protected: the target resolves there
    (home / "projects" / "sneaky").symlink_to(home / "protected")
    rows = [
        ["s1", link, "live", "x", "", "", "1"],
        ["s2", linkdir / "secret.txt", "live", "x", "", "", "1"],
        ["s3", holder, "live", "x", "", "", "1"],
        ["s4", home / "inbox/box/c/a.txt", "live", "sneaky", "", "", "1"],
        ["s5", link, "live", "x", "", "", "0"],
    ]
    rc, out = run(capsys, "apply", str(table(tmp, rows)), "--apply")
    assert rc == 1 and out.count("refused\t") == 5
    assert (home / "protected/secret.txt").read_text() == "keep out"
    assert os.listdir(home / "protected") == ["secret.txt"]
    assert link.is_symlink() and (home / "inbox/box/c/a.txt").exists()


def test_collision_goes_to_quarantine(env, capsys):
    home, tmp = env
    (home / "projects/alpha").mkdir()
    (home / "projects/alpha/a.txt").write_text("different")
    rc, out = run(capsys, "apply", str(table(tmp, std_rows(home)[:1])), "--apply")
    assert rc == 0, out
    assert (home / "projects/alpha/a.txt").read_text() == "different"
    q = home / "quarantine/2026-01-02-t1/inbox/box/c/a.txt"
    assert q.read_text() == "alpha"
    assert "collision" in (home / "quarantine/2026-01-02-t1/manifest.tsv").read_text()
    assert not (home / "inbox/box/c/a.txt").exists()


def test_collision_between_rows_in_plan(env, capsys):
    home, tmp = env
    s = home / "inbox/box/c"
    (home / "inbox/box/d").mkdir()
    (home / "inbox/box/d/a.txt").write_text("other")
    rows = [["k1", s / "a.txt", "live", "alpha", "", "", "1"],
            ["k2", home / "inbox/box/d/a.txt", "live", "alpha", "", "", "1"]]
    rc, out = run(capsys, "plan", str(table(tmp, rows)))
    assert "collision" in out
    rc, out = run(capsys, "apply", str(table(tmp, rows)), "--apply")
    assert (home / "projects/alpha/a.txt").read_text() == "alpha"
    assert (home / "quarantine/2026-01-02-t1/inbox/box/d/a.txt").read_text() == "other"


def test_existing_repo_reused(env, capsys):
    home, tmp = env
    repo = home / "projects/alpha"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    rc, out = run(capsys, "plan", str(table(tmp, std_rows(home)[:1])))
    assert "(git init)" not in out
    run(capsys, "apply", str(table(tmp, std_rows(home)[:1])), "--apply")
    log = [e[1] for e in engine.read_log(tmp / "moves.tsv")]
    assert "git-init" not in log and "git-commit" in log


def test_missing_source_reported(env, capsys):
    home, tmp = env
    rc, out = run(capsys, "plan", str(table(tmp, [["m1", home / "inbox/nope", "live", "alpha", "", "", "1"]])))
    assert rc == 1 and "source missing" in out
