import json
from pathlib import Path

from housekeeping import config
from test_mover_flow import env, std_rows, table  # noqa: F401
from housekeeping.mover import cli, engine, provenance

FIX = Path(__file__).parent / "fixtures" / "provenance_insert.json"


def test_insert_matches_fixture():
    fx = json.loads(FIX.read_text())
    sql, params = provenance.build_insert(**fx["args"])
    assert sql == fx["sql"]
    assert params == fx["params"]


def test_insert_columns_exist_in_schema():
    schema = (Path(__file__).parents[2] / "schema.txt").read_text()
    block = schema.split("CREATE TABLE atlas.atlas_provenance (")[1].split(");")[0]
    cols = {ln.split()[0] for ln in block.strip().splitlines()}
    sql = json.loads(FIX.read_text())["sql"]
    named = sql.split("(")[1].split(")")[0].split(", ")
    assert set(named) <= cols


class FakeConn:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((sql, params))


def _enable(tmp_path, monkeypatch):
    cfg = Path(config.os.environ["HK_CONFIG"])
    cfg.write_text(cfg.read_text().replace("write_provenance = false", "write_provenance = true")
                   + '[atlas]\ndsn_env = "ATLAS_DSN"\n')
    config.load.cache_clear()
    fake = FakeConn()
    monkeypatch.setattr(provenance, "_connection", lambda dsn_env: fake)
    return fake


def test_rows_written_on_apply_and_undo_only(env, capsys, monkeypatch):
    home, tmp = env
    fake = _enable(tmp, monkeypatch)
    t = table(tmp, std_rows(home)[:1] + std_rows(home)[3:4])
    cli.main(["apply", str(t)])
    cli.main(["plan", str(t)])
    assert fake.calls == []
    assert cli.main(["apply", str(t), "--apply"]) == 0
    assert [json.loads(p[5])["chain_id"] for _, p in fake.calls] == ["c1", "c4"]
    assert {p[2] for _, p in fake.calls} == {"file_move"}
    payload = json.loads(fake.calls[0][1][5])
    assert payload["batch"] == "t1" and len(payload["sha256"]) == 64
    cli.main(["apply", str(t), "--apply"])
    assert len(fake.calls) == 2  # idempotent rerun writes nothing
    assert cli.main(["undo", str(tmp / "moves.tsv"), "--apply"]) == 0
    assert [p[2] for _, p in fake.calls[2:]] == ["file_move_undo", "file_move_undo"]


def test_db_failure_is_reported_not_fatal(env, capsys, monkeypatch):
    home, tmp = env
    _enable(tmp, monkeypatch)
    def boom(dsn_env):
        raise RuntimeError("db down")
    monkeypatch.setattr(provenance, "_connection", boom)
    rc = cli.main(["apply", str(table(tmp, std_rows(home)[:1])), "--apply"])
    out = capsys.readouterr().out
    assert rc == 1 and "provenance write failed" in out
    assert (home / "projects/alpha/a.txt").exists()
    assert any(e[1] == "move" for e in engine.read_log(tmp / "moves.tsv"))
