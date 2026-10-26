"""End to end on a temp tree: real rsync, real rebuild-pipeline.sh, real checker; fake model."""
import csv
import json
import shutil
import subprocess

import pytest

from housekeeping.rebuild_all import cli

pytestmark = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")
FIELDS = ["root_id", "root_path", "dir", "code_files", "marker_files", "markers", "exts", "projects",
          "newest_mtime", "code_bytes", "has_readme"]


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home" / "u"
    proj = home / "work" / "btc"
    (proj / "engine" / "tree_store").mkdir(parents=True)
    (proj / "sweeps_v2").mkdir()
    (proj / "results").mkdir()
    (proj / "main.py").write_text('"""BTC engine driver."""\nprint(1)\n')
    (proj / "README.md").write_text("# btc\nOld readme.\n")
    (proj / "engine" / "tree.py").write_text("x = 1\n")
    (proj / "results" / "analyze.py").write_text("y = 2\n")          # code inside results/: rule rejected
    (proj / "results" / "r.csv").write_text("1\n")
    for i in range(350):
        (proj / "engine" / "tree_store" / f"n{i}.json").write_text("{}")
    for i in range(5):
        (proj / "sweeps_v2" / f"s{i}.csv").write_text("1\n")
    (proj / "run.log").write_text("log\n")
    inv = tmp_path / "dirs.tsv"
    with open(inv, "w", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(FIELDS)
        w.writerow([21, str(tmp_path / "home"), "u/work/btc", 1, 0, "", "py", "", "2026-10-20 00:00:00+00:00", 40, "True"])
        w.writerow([21, str(tmp_path / "home"), "u/work/btc/engine", 1, 0, "", "py", "", "2026-10-20 00:00:00+00:00", 6, "False"])
        w.writerow([21, str(tmp_path / "home"), "u/work/btc/results", 1, 0, "", "py", "", "2026-10-20 00:00:00+00:00", 6, "False"])
    names_created = not cli.NAMES.exists()
    if names_created:
        cli.NAMES.write_text("zz-protected-test-name\n")
    data, projects = tmp_path / "data", tmp_path / "projects"
    for k, v in dict(HOME=home, DATA=data, PROJECTS=projects).items():
        monkeypatch.setattr(cli, k, v)
    for k, v in dict(HK_PROJECTS=projects, HK_DATA=data, HK_PROTECTED=tmp_path / "protected",
                     GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                     GIT_COMMITTER_EMAIL="t@t").items():
        monkeypatch.setenv(k, str(v))
    calls = []

    def fake_ask(router, role, prompt, timeout=600):
        calls.append(role)
        if prompt.startswith("A research code folder"):
            return "EXCLUDE sweeps_v2/  # sweep results\nEXCLUDE *  # nonsense\nEXCLUDE *.py  # wrong\nEXCLUDE nothing_here/"
        return "<think>hmm</think>Driver " + chr(0x2014) + " runs the BTC engine in main.py and tree.py."
    monkeypatch.setattr(cli, "ask", fake_ask)
    yield tmp_path, inv, proj, projects, calls
    if names_created:
        cli.NAMES.unlink()


def test_plan_run_dry_then_apply_then_push_dry(world, capsys):
    tmp, inv, proj, projects, calls = world
    run = tmp / "run"
    assert cli.main(["plan", "--dirs", str(inv), "--out", str(run)]) == 0
    cands = list(csv.DictReader(open(run / "candidates.tsv"), delimiter="\t"))
    assert [c["name"] for c in cands] == ["btc"] and cands[0]["code_files"] == "3"

    before = sorted(p.relative_to(proj).as_posix() for p in proj.rglob("*"))
    assert cli.main(["run", str(run), "--roles", "reader"]) == 0
    st = cli.latest_state(run)["btc"]
    assert st["status"] == "planned" and not (projects / "btc").exists()
    rules = set(st["rules"])
    assert {"default:*.log", "bulk:/engine/tree_store/", "model:sweeps_v2/"} <= rules, rules
    assert any(r.startswith("default:results/") for r in st["rejected"])        # would drop analyze.py
    assert any(r.startswith("model:*.py") for r in st["rejected"])
    assert not any(r.endswith(":*") for r in rules)
    assert st["files"] == 5 and st["code"] == 4                                    # 3 .py + README; r.csv kept as data

    assert cli.main(["run", str(run), "--apply", "--roles", "reader"]) == 0
    st = cli.latest_state(run)["btc"]
    assert st["status"] == "built", st
    repo = projects / "btc"
    tracked = subprocess.run(["git", "-C", repo, "ls-files"], capture_output=True, text=True).stdout.split()
    assert "results/analyze.py" in tracked and "engine/tree.py" in tracked
    assert not any(t.startswith(("engine/tree_store/", "sweeps_v2/")) or t.endswith(".log") for t in tracked)
    readme = (repo / "README.md").read_text()
    assert "Driver , runs" not in readme and chr(0x2014) not in readme and "<think>" not in readme
    assert "Driver, runs the BTC engine" in readme and "## State\npartial" in readme
    assert set(st["checks"].values()) == {"PASS"}, st["checks"]
    assert sorted(p.relative_to(proj).as_posix() for p in proj.rglob("*")) == before   # source untouched

    review = list(csv.DictReader(open(run / "review.tsv"), delimiter="\t"))
    assert review[0]["push"] == "yes", review
    # Your edit to the push column survives a new report; the push dry run does nothing.
    text = (run / "review.tsv").read_text().replace("yes\tbtc", "no\tbtc")
    (run / "review.tsv").write_text(text)
    cli.main(["report", str(run)])
    assert list(csv.DictReader(open(run / "review.tsv"), delimiter="\t"))[0]["push"] == "no"
    (run / "review.tsv").write_text(text.replace("no\tbtc", "yes\tbtc"))
    capsys.readouterr()
    assert cli.main(["push", str(run)]) == 0
    out = capsys.readouterr().out
    assert "gh repo create Radar41/btc --private" in out and "dry run" in out
    assert subprocess.run(["git", "-C", repo, "remote"], capture_output=True, text=True).stdout == ""

    # Rerun is a no-op for finished candidates.
    capsys.readouterr()
    cli.main(["run", str(run), "--apply", "--roles", "reader"])
    assert "1 finished, 0 waiting, 0 this run" in capsys.readouterr().out


def test_model_down_still_plans_with_defaults(world, monkeypatch):
    tmp, inv, proj, projects, calls = world

    def down(*a, **k):
        raise ConnectionError("router down")
    monkeypatch.setattr(cli, "ask", down)
    run = tmp / "run2"
    cli.main(["plan", "--dirs", str(inv), "--out", str(run)])
    cli.main(["run", str(run), "--roles", "reader"])
    st = cli.latest_state(run)["btc"]
    assert st["status"] == "planned" and st["model"].startswith("unavailable")
    assert "bulk:/engine/tree_store/" in st["rules"]
