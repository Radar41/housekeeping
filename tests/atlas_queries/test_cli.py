import pytest

from housekeeping import config
from housekeeping.atlas_queries import cli


def write_config(tmp_path, monkeypatch, dsn=None):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[atlas]\ndsn_env = "TEST_ATLAS_DSN"\n[atlas_queries]\nlog = "{tmp_path}/q.tsv"\n')
    monkeypatch.setenv("HK_CONFIG", str(cfg))
    config.load.cache_clear()
    if dsn:
        monkeypatch.setenv("TEST_ATLAS_DSN", dsn)
    else:
        monkeypatch.delenv("TEST_ATLAS_DSN", raising=False)


def test_every_query_file_is_registered_and_read_only():
    for name in cli.QUERIES:
        assert cli.is_read_only(cli.load_sql(name))
    assert not cli.is_read_only("-- c\nDELETE FROM x")


def test_render_formats():
    assert cli.render(["a", "b"], [(1, None), ("x,y", 2)], "tsv") == "a\tb\n1\t\nx,y\t2\n"
    assert cli.render(["a", "b"], [("x,y", 2)], "csv") == 'a,b\n"x,y",2\n'


def test_missing_dsn(tmp_path, monkeypatch, capsys):
    write_config(tmp_path, monkeypatch)
    assert cli.main(["provenance-check"]) == 2
    assert "TEST_ATLAS_DSN" in capsys.readouterr().err


def test_root_id_required(tmp_path, monkeypatch, capsys):
    write_config(tmp_path, monkeypatch, "host=nowhere")
    assert cli.main(["inbox-three-way"]) == 2
    assert "--root-id" in capsys.readouterr().err


def test_dry_run_apply_and_idempotent(tmp_path, monkeypatch, capsys, db, pg_dsn):
    write_config(tmp_path, monkeypatch, pg_dsn)
    db.execute("INSERT INTO atlas.atlas_provenance (event_id, actor, action, target_table) "
               "VALUES (gen_random_uuid(), 'a', 'edit', 't')")
    out = tmp_path / "o" / "prov.tsv"
    assert cli.main(["provenance-check", "--out", str(out)]) == 0
    assert "dry-run" in capsys.readouterr().out and not out.exists()
    assert cli.main(["provenance-check", "--out", str(out), "--apply"]) == 0
    assert out.read_text().startswith("action\t")
    assert cli.main(["provenance-check", "--out", str(out), "--apply"]) == 0
    assert "unchanged" in capsys.readouterr().out
    lines = (tmp_path / "q.tsv").read_text().splitlines()
    assert len(lines) == 1 and lines[0].split("\t")[1] == "write"
    assert cli.main(["provenance-check", "--format", "csv"]) == 0
    assert capsys.readouterr().out.startswith("action,")


def test_connection_is_read_only(pg_dsn):
    import psycopg

    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        cli.run_query(pg_dsn, "CREATE TABLE atlas.nope (x int)", {})


def test_withhold_protected():
    rows = [(1, "radar-41-0/protected/a.py", "x"), (2, "radar-41-0/proj/a.py", "uses ZZname here"),
            (3, "radar-41-0/proj/b.py", None), (4, "protected", "")]
    kept, n = cli.withhold_protected(rows, ["zzname"])
    assert [r[0] for r in kept] == [3] and n == 3


def test_content_query_needs_names_file(tmp_path, monkeypatch, capsys):
    write_config(tmp_path, monkeypatch, "host=nowhere")
    assert cli.main(["pipeline-inventory"]) == 2
    assert "protected names file" in capsys.readouterr().err
