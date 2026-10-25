import os
import shutil

import pytest

from housekeeping import config
from housekeeping.intake import cli

pytestmark = pytest.mark.skipif(shutil.which("rclone") is None, reason="rclone not installed")


@pytest.fixture
def tree(tmp_path, monkeypatch):
    src = tmp_path / "src"
    (src / "Users" / "a" / "AppData" / "Local" / "Temp").mkdir(parents=True)
    (src / "Windows").mkdir()
    (src / "docs").mkdir()
    (src / "docs" / "one.txt").write_text("hello")
    (src / "docs" / "two.txt").write_text("world!")
    (src / "pagefile.sys").write_text("junk")
    (src / "Windows" / "x.dll").write_text("junk")
    (src / "Users" / "a" / "AppData" / "Local" / "Temp" / "t.tmp").write_text("junk")
    os.utime(src / "docs" / "one.txt", (1_600_000_000, 1_600_000_000))
    target = tmp_path / "inbox"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[paths]\nlogs = "{tmp_path / "logs"}"\n[intake]\nmachine = "box"\ntarget_root = "{target}"\n'
        f'sources = [{{ name = "c", remote = "{src}" }}]\n'
    )
    monkeypatch.setenv("HK_CONFIG", str(cfg))
    config.load.cache_clear()
    yield src, target / "box" / "c", target / "box" / "CHECK.txt"
    config.load.cache_clear()


def test_roundtrip(tree, capsys):
    src, dest, check_file = tree
    assert cli.main(["copy"]) == 0
    assert not dest.exists()  # dry-run wrote nothing
    assert cli.main(["copy", "--apply"]) == 0
    assert (dest / "docs" / "one.txt").read_text() == "hello"
    assert int((dest / "docs" / "one.txt").stat().st_mtime) == 1_600_000_000
    assert not (dest / "pagefile.sys").exists()
    assert not (dest / "Windows").exists()
    assert not (dest / "Users" / "a" / "AppData" / "Local" / "Temp" / "t.tmp").exists()

    assert cli.main(["check"]) == 0
    parsed = cli.parse_check_file(check_file)
    assert parsed["c"]["match"] == "2" and parsed["c"]["result"] == "OK"

    assert cli.main(["report"]) == 0
    row = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("c "))
    assert "2" in row and "11" in row and "OK" in row

    (dest / "docs" / "one.txt").write_text("HELLO")  # same size, different content
    (dest / "docs" / "two.txt").unlink()
    assert cli.main(["check"]) == 1
    parsed = cli.parse_check_file(check_file)
    assert parsed["c"]["differ"] == "1" and parsed["c"]["missing"] == "1"

    assert (src / "docs" / "one.txt").read_text() == "hello"  # source untouched
