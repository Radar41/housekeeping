import json
import shutil

import pytest

from housekeeping.checker import cli
from conftest import build_repo, git

EMDASH = chr(0x2014)
# Built at runtime so this file never contains a secret-shaped literal.
FAKE_KEY = "-----BEGIN RSA " + "PRIVATE KEY-----"

BREAKS = {
    "readme": dict(files={"README.md": "# demo\n\n## What it is\nx\n\n## State\nparked\n"}),
    "python": dict(files={"pyproject.toml": '[project]\nrequires-python = ">=3.10"\n'}),
    "entry": dict(remove=("Makefile",)),
    "env": dict(files={".gitignore": "logs/\n"}),
    "secrets": dict(files={"conf.txt": "key = " + FAKE_KEY + "\n"}),
    "emdash": dict(files={"notes.md": "a " + EMDASH + " b\n"}),
    "protected": dict(files={"notes.md": "uses ALPHA_CORE here\n"}),
}


def statuses(repo, names=("alpha_core",)):
    return {r["check"]: r["status"] for r in cli.run_checks(repo, list(names))}


def test_good_repo_passes_all(tmp_path):
    repo = build_repo(tmp_path)
    assert set(statuses(repo).values()) == {"PASS"}


@pytest.mark.parametrize("check", BREAKS)
def test_each_check_fails_alone(tmp_path, check):
    repo = build_repo(tmp_path, **BREAKS[check])
    res = statuses(repo)
    assert res[check] == "FAIL"
    assert [c for c, s in res.items() if s == "FAIL"] == [check]


def test_git_missing(tmp_path):
    repo = build_repo(tmp_path)
    shutil.rmtree(repo / ".git")
    res = statuses(repo)
    assert res["git"] == "FAIL"
    assert all(s == "FAIL" for c, s in res.items() if c != "protected")


def test_tracked_env_fails(tmp_path):
    repo = build_repo(tmp_path, files={".env": "KEY=1\n"})
    assert statuses(repo)["env"] == "FAIL"


def test_env_example_required_when_env_exists(tmp_path):
    repo = build_repo(tmp_path, remove=(".env.example", "app.py"))
    assert statuses(repo)["env"] == "PASS"
    (repo / ".env").write_text("KEY=1\n")
    assert statuses(repo)["env"] == "FAIL"


def test_missing_uv_lock_fails(tmp_path):
    repo = build_repo(tmp_path, remove=("uv.lock",))
    assert statuses(repo)["python"] == "FAIL"


def test_secret_output_has_no_match(tmp_path):
    repo = build_repo(tmp_path, files={"conf.txt": "ok\n" + FAKE_KEY + "\n"})
    det = [r for r in cli.run_checks(repo, []) if r["check"] == "secrets"][0]["detail"]
    assert "conf.txt:2" in det and "PRIVATE" not in det


def test_protected_never_prints_name(tmp_path):
    repo = build_repo(tmp_path, files={"notes.md": "x\nalpha_core\n"})
    det = [r for r in cli.run_checks(repo, ["alpha_core"]) if r["check"] == "protected"][0]["detail"]
    assert det == "notes.md:2 protected name match"


def test_protected_file_missing_fails(tmp_path):
    repo = build_repo(tmp_path)
    r = [r for r in cli.run_checks(repo, None) if r["check"] == "protected"][0]
    assert (r["status"], r["detail"]) == ("FAIL", "protected names file not found")


def test_repo_not_modified(tmp_path):
    repo = build_repo(tmp_path)
    before = sorted(p.name for p in repo.iterdir())
    index = (repo / ".git" / "index").read_bytes()
    statuses(repo)
    assert sorted(p.name for p in repo.iterdir()) == before
    assert (repo / ".git" / "index").read_bytes() == index


def test_main_exit_codes_and_output(hk_config, capsys):
    root = hk_config / "repos"
    root.mkdir()
    good = build_repo(root, "good")
    assert cli.main([str(good)]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 8 and all(l.startswith("PASS\t") for l in lines)
    bad = build_repo(root, "bad", **BREAKS["emdash"])
    assert cli.main([str(good), str(bad)]) == 1
    assert "FAIL\temdash\t" in capsys.readouterr().out


def test_all_and_json_and_log(hk_config, capsys):
    root = hk_config / "repos"
    root.mkdir()
    build_repo(root, "a")
    build_repo(root, "b", **BREAKS["readme"])
    (root / "stray.txt").write_text("x")
    assert cli.main(["--all", str(root), "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 16
    assert {(d["repo"].rsplit("/", 1)[1], d["check"]) for d in data if d["status"] == "FAIL"} == {("b", "readme")}
    log = (hk_config / "logs" / "checker.tsv").read_text().splitlines()
    assert len(log) == 16 and log[0].count("\t") == 4


def test_protected_file_missing_via_main(hk_config, capsys):
    (hk_config / "protected_names.txt").unlink()
    root = hk_config / "r"
    root.mkdir()
    good = build_repo(root, "good")
    assert cli.main([str(good)]) == 1
    assert "protected names file not found" in capsys.readouterr().out


def test_usage_errors(hk_config):
    assert cli.main([]) == 2
    assert cli.main(["--all"]) == 2
