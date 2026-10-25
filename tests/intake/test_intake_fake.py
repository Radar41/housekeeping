import json
import shutil
import stat
import sys
from pathlib import Path

import pytest

from housekeeping import config
from housekeeping.intake import cli

HERE = Path(__file__).parent


@pytest.fixture
def env(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "rclone"
    exe.write_text(f"#!{sys.executable}\n" + (HERE / "fake_rclone.py").read_text())
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    target = tmp_path / "inbox"
    logs = tmp_path / "logs"
    record = tmp_path / "record.jsonl"
    spec_path = tmp_path / "spec.json"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[paths]\nlogs = "{logs}"\n[intake]\nmachine = "winbox"\ntarget_root = "{target}"\n'
        'sources = [{ name = "c", remote = "winbox-c:/" }, { name = "cloud", remote = "cloud-remote:/" }]\n'
    )
    monkeypatch.setenv("HK_CONFIG", str(cfg))
    monkeypatch.setenv("FAKE_RCLONE_SPEC", str(spec_path))
    config.load.cache_clear()

    class E:
        pass

    e = E()
    e.target, e.logs, e.record, e.cfg = target, logs, record, cfg
    e.spec = {"record": str(record), "hashes": {"winbox-c:/": ["MD5"], str(target): ["MD5"]}}

    def write_spec():
        spec_path.write_text(json.dumps(e.spec))

    def calls(verb=None):
        if not record.exists():
            return []
        rows = [json.loads(x) for x in record.read_text().splitlines()]
        return [r for r in rows if verb is None or r[0] == verb]

    e.write_spec, e.calls = write_spec, calls
    write_spec()
    yield e
    config.load.cache_clear()


def test_copy_defaults_to_dry_run(env, capsys):
    assert cli.main(["copy"]) == 0
    copies = env.calls("copy")
    assert len(copies) == 2
    for argv in copies:
        assert "--dry-run" in argv
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "- pagefile.sys" in out and "/Windows/**" in out
    lines = env.logs.joinpath("intake.tsv").read_text().splitlines()
    assert [l.split("\t")[1] for l in lines] == ["intake.copy.dry", "intake.copy.dry"]


def test_copy_apply_command_construction(env):
    assert cli.main(["copy", "--apply"]) == 0
    c, cloud = env.calls("copy")
    assert "--dry-run" not in c
    assert c[1] == "winbox-c:/"
    assert c[2] == str(env.target / "winbox" / "c")
    assert c[c.index("--filter-from") + 1] == str(cli.FILTER_FILE)
    assert "--checksum" in c  # MD5 on both sides
    assert "--log-file" in c and c[c.index("--log-file") + 1].startswith(str(env.logs))
    assert "--checksum" not in cloud  # no hashes reported for this remote
    assert cloud[2] == str(env.target / "winbox" / "cloud")
    lines = env.logs.joinpath("intake.tsv").read_text().splitlines()
    assert [l.split("\t")[1] for l in lines] == ["intake.copy", "intake.copy"]
    assert all(len(l.split("\t")) == 5 for l in lines)


def test_copy_failure_exit_code(env):
    env.spec["copy_rc"] = {"cloud-remote:/": 1}
    env.write_spec()
    assert cli.main(["copy", "--apply"]) == 1
    actions = [l.split("\t")[1] for l in env.logs.joinpath("intake.tsv").read_text().splitlines()]
    assert actions == ["intake.copy", "intake.copy.fail"]


def test_copy_reports_up_to_date(env, capsys):
    cli.main(["copy", "--apply"])
    assert "up to date" in capsys.readouterr().out
    env.spec["copy_files"] = ["a.txt", "b.txt"]
    env.write_spec()
    cli.main(["copy", "--apply"])
    assert "copied 2 files" in capsys.readouterr().out


def test_check_all_match(env):
    env.spec["combined"] = {"winbox-c:/": "= a\n= b\n", "cloud-remote:/": "= x\n"}
    env.write_spec()
    assert cli.main(["check"]) == 0
    c, cloud = env.calls("check")
    assert "--one-way" in c and "--download" not in c
    assert "--download" in cloud
    assert c[c.index("--combined") + 1] == "-"
    parsed = cli.parse_check_file(env.target / "winbox" / "CHECK.txt")
    assert parsed["c"]["match"] == "2" and parsed["c"]["result"] == "OK"
    assert parsed["cloud"]["mode"] == "download"


def test_check_differ_and_missing_exit_nonzero(env):
    env.spec["combined"] = {"winbox-c:/": "= a\n* b\n+ c\n+ d\n", "cloud-remote:/": "= x\n"}
    env.spec["check_rc"] = {"winbox-c:/": 1}
    env.write_spec()
    assert cli.main(["check"]) == 1
    text = (env.target / "winbox" / "CHECK.txt").read_text()
    assert "== c (winbox-c:/) ==" in text and "== cloud (cloud-remote:/) ==" in text
    parsed = cli.parse_check_file(env.target / "winbox" / "CHECK.txt")
    assert (parsed["c"]["match"], parsed["c"]["differ"], parsed["c"]["missing"]) == ("1", "1", "2")
    assert parsed["c"]["result"] == "FAIL" and parsed["cloud"]["result"] == "OK"
    actions = [l.split("\t")[1] for l in env.logs.joinpath("intake.tsv").read_text().splitlines()]
    assert actions == ["intake.check.fail", "intake.check.ok"]


def test_check_rclone_failure_is_error(env):
    env.spec["check_rc"] = {"winbox-c:/": 3}
    env.spec["check_err"] = {"winbox-c:/": "directory not found\n"}
    env.write_spec()
    assert cli.main(["check"]) == 1
    parsed = cli.parse_check_file(env.target / "winbox" / "CHECK.txt")
    assert parsed["c"]["result"] == "ERROR"


def test_check_is_idempotent(env, capsys):
    cli.main(["check"])
    first = (env.target / "winbox" / "CHECK.txt").read_text()
    capsys.readouterr()
    cli.main(["check"])
    assert (env.target / "winbox" / "CHECK.txt").read_text() == first
    assert "unchanged" in capsys.readouterr().out


def test_report(env, capsys):
    env.spec["combined"] = {"winbox-c:/": "* a\n"}
    env.spec["size"] = {str(env.target / "winbox" / "c"): {"count": 7, "bytes": 1234}}
    env.write_spec()
    cli.main(["check"])
    capsys.readouterr()
    assert cli.main(["report"]) == 0
    out = capsys.readouterr().out
    c_row = next(l for l in out.splitlines() if l.startswith("c "))
    assert "7" in c_row and "1234" in c_row and "FAIL" in c_row
    cloud_row = next(l for l in out.splitlines() if l.startswith("cloud"))
    assert "not copied" in cloud_row


def test_refuses_source_inside_target(env, tmp_path):
    inside = env.target / "winbox" / "c"
    env.cfg.write_text(
        f'[paths]\nlogs = "{env.logs}"\n[intake]\nmachine = "winbox"\ntarget_root = "{env.target}"\n'
        f'sources = [{{ name = "c", remote = "{inside}" }}]\n'
    )
    config.load.cache_clear()
    assert cli.main(["copy", "--apply"]) == 2
    assert env.calls("copy") == []


def test_refuses_alias_remote_into_target(env):
    env.spec["config"] = {"loop": {"type": "alias", "remote": str(env.target / "winbox")}}
    env.write_spec()
    env.cfg.write_text(
        f'[paths]\nlogs = "{env.logs}"\n[intake]\nmachine = "winbox"\ntarget_root = "{env.target}"\n'
        'sources = [{ name = "c", remote = "loop:sub" }]\n'
    )
    config.load.cache_clear()
    assert cli.main(["check"]) == 2
    assert env.calls("check") == []


def test_bad_config(env):
    env.cfg.write_text(f'[intake]\nmachine = "w"\ntarget_root = "{env.target}"\nsources = []\n')
    config.load.cache_clear()
    assert cli.main(["copy"]) == 2


def test_only_allowed_verbs_run(env):
    with pytest.raises(cli.IntakeError):
        cli.run_rclone(["sync", "a", "b"])
    with pytest.raises(cli.IntakeError):
        cli.run_rclone(["delete", "a"])


def test_filter_file_content():
    text = cli.filter_text()
    for needle in ["pagefile.sys", "hiberfil.sys", "swapfile.sys", "System Volume Information",
                   "$Recycle.Bin", "Windows/", "Program Files*", "ProgramData/", "AppData/Local/Temp"]:
        assert needle in text
    assert "\u2014" not in text
