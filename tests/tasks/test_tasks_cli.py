import pytest

from housekeeping import config
from housekeeping.tasks import cli
from housekeeping.tasks.store import MemoryStore


@pytest.fixture
def cfg(tmp_path, inventory, monkeypatch):
    names = tmp_path / "names.txt"
    names.write_text("zeta-codename\n")
    p = tmp_path / "config.toml"
    p.write_text(f"""
[checker]
protected_names_file = "{names}"

[tasks]
log = "{tmp_path}/logs/tasks.tsv"
data_dir = "{tmp_path}/data"
inventory_dir = "{inventory}"
ns = "test"
card_max_chars = 1200
max_attempts = 2
poll_interval_s = 1
request_timeout_s = 5
router_url = "http://router.test"

[tasks.memgraph]
uri = "bolt://unused"
user_env = "X_UNSET"
password_env = "X_UNSET"
""")
    monkeypatch.setenv("HK_CONFIG", str(p))
    config.load.cache_clear()
    yield tmp_path
    config.load.cache_clear()


def test_propose_dry_apply_repeat(cfg, capsys):
    store = MemoryStore("test")
    dirs = str(cfg / "dirs.tsv")
    assert cli.main(["propose", "code-card", "--from-dirs", dirs], store=store) == 0
    assert "would propose 3, already present 0" in capsys.readouterr().out
    assert store.list() == []
    cli.main(["propose", "code-card", "--from-dirs", dirs, "--apply"], store=store)
    assert "proposed 3, already present 0" in capsys.readouterr().out
    cli.main(["propose", "code-card", "--from-dirs", dirs, "--apply"], store=store)
    assert "proposed 0, already present 3" in capsys.readouterr().out
    assert len((cfg / "logs/tasks.tsv").read_text().splitlines()) == 3


def test_limit_match_list_show(cfg, capsys):
    store = MemoryStore("test")
    cli.main(["propose", "code-card", "--from-dirs", str(cfg / "dirs.tsv"), "--match", "plot", "--apply"],
             store=store)
    capsys.readouterr()
    cli.main(["list"], store=store)
    out = capsys.readouterr().out
    assert "proposed" in out and "/u/plot" in out
    tid = store.list()[0].id
    cli.main(["show", tid[:10]], store=store)
    assert '"status": "proposed"' in capsys.readouterr().out


def test_depend_and_blocked(cfg, capsys):
    store = MemoryStore("test")
    cli.main(["propose", "code-card", "--from-dirs", str(cfg / "dirs.tsv"), "--limit", "2", "--apply"],
             store=store)
    a, b = [t.id for t in store.list()]
    cli.main(["depend", b, a], store=store)
    assert store.blocked() == []
    cli.main(["depend", b, a, "--apply"], store=store)
    capsys.readouterr()
    cli.main(["blocked"], store=store)
    assert a[:12] in capsys.readouterr().out
