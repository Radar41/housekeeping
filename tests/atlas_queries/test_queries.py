import hashlib
import re
import uuid

from datetime import datetime, timezone

import pytest

from conftest import SQL_DIR, sql_text

FILES = sorted(p.name for p in SQL_DIR.glob("*.sql"))


def query(db, name, **params):
    cur = db.execute(sql_text(name), params or None)
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def body(name):
    return re.sub(r"--[^\n]*", "", sql_text(name)).strip().lower()


@pytest.mark.parametrize("name", FILES)
def test_select_or_with_only(name):
    b = body(name)
    assert b.startswith(("select", "with"))
    assert not re.search(r"\b(insert|update|delete|drop|alter|create|truncate|grant)\b", b)


@pytest.mark.parametrize("name", FILES)
def test_no_em_dash(name):
    assert chr(0x2014) not in sql_text(name)


@pytest.mark.parametrize("name", FILES)
def test_parses_with_pglast(name):
    pglast = pytest.importorskip("pglast", reason="pglast not installed; DB tests cover parsing")
    pglast.parse_sql(re.sub(r"%\(\w+\)s", "1", sql_text(name)))


def add_event(db, action, when, table="atlas_document"):
    db.execute("INSERT INTO atlas.atlas_provenance (event_id, actor, action, target_table, occurred_at) "
               "VALUES (%s, 'test', %s, %s, %s)", (str(uuid.uuid4()), action, table, when))


def test_provenance_check(db):
    for d in ["2026-06-02", "2026-06-10", "2026-06-17", "2026-06-24", "2026-07-01", "2026-08-12", "2026-08-13"]:
        add_event(db, "edit", f"{d} 12:00+00")
    for d in ["2026-06-02", "2026-07-08", "2026-07-15", "2026-07-22", "2026-07-29"]:
        add_event(db, "move", f"{d} 12:00+00", "atlas_epoch_document_snapshot")
    rows = query(db, "provenance_check.sql")
    assert {r["action"] for r in rows} == {"edit", "move"}
    edit = [r for r in rows if r["action"] == "edit"]
    assert edit[0]["type_total"] == 7
    assert str(edit[0]["type_first_at"].astimezone(timezone.utc).date()) == "2026-06-02"
    assert str(edit[0]["type_last_at"].astimezone(timezone.utc).date()) == "2026-08-13"
    flagged = [r for r in edit if r["gap_in_july_2026"]]
    assert len(flagged) == 1 and str(flagged[0]["week_start"]) == "2026-08-10"
    assert flagged[0]["gap_days"] == 35
    move = [r for r in rows if r["action"] == "move"]
    flagged_move = [r for r in move if r["gap_in_july_2026"]]
    assert [str(r["week_start"]) for r in flagged_move] == ["2026-07-06"]
    assert flagged_move[0]["gap_days"] == 28
    assert move[0]["target_tables"] == "atlas_epoch_document_snapshot"
    first_week = [r for r in rows if r["gap_days"] is None]
    assert len(first_week) == 2


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def add_doc(db, doc_id, root, path, content, mtime, vstatus=None):
    db.execute("INSERT INTO atlas.atlas_document (document_id, root_id, rel_path, sha256, mtime, status, version_status) "
               "VALUES (%s, %s, %s, %s, %s, 'indexed', %s)", (doc_id, root, path, sha(content), mtime, vstatus))


def add_chain(db, chain, doc, pos, kind="version_chain"):
    db.execute("INSERT INTO atlas.atlas_version_chain (chain_id, document_id, position, chain_kind, ordering_method) "
               "VALUES (%s, %s, %s, %s, 'test')", (chain, doc, pos, kind))


def test_inbox_three_way(db):
    db.execute("INSERT INTO atlas.atlas_root (root_id, root_path) VALUES (1, '/inbox/'), (2, '/projects')")
    # 1: no chain at all
    add_doc(db, 1, 1, "a/solo.txt", "solo", "2026-05-01 00:00+00")
    # 2,3: chain with only inbox members
    add_doc(db, 2, 1, "a/v1.txt", "v1", "2026-05-01 00:00+00")
    add_doc(db, 3, 1, "a/v2.txt", "v2", "2026-05-02 00:00+00")
    add_chain(db, 10, 2, 0)
    add_chain(db, 10, 3, 1)
    # 4: duplicate of outside 5
    add_doc(db, 4, 1, "b/copy.txt", "same", "2026-05-01 00:00+00")
    add_doc(db, 5, 2, "keep/orig.txt", "same", "2026-04-01 00:00+00", "canonical")
    add_chain(db, 20, 4, 0)
    add_chain(db, 20, 5, 1)
    # 6: changed, inbox newer than outside 7
    add_doc(db, 6, 1, "c/new.txt", "edited", "2026-06-01 00:00+00")
    add_doc(db, 7, 2, "keep/old.txt", "original", "2026-03-01 00:00+00", "canonical")
    add_chain(db, 30, 6, 0)
    add_chain(db, 30, 7, 1)
    # 8: changed, outside 9 newer
    add_doc(db, 8, 1, "d/old.txt", "older", "2026-01-01 00:00+00")
    add_doc(db, 9, 2, "keep/newer.txt", "newer", "2026-02-01 00:00+00")
    add_chain(db, 40, 8, 0)
    add_chain(db, 40, 9, 1)
    # 10: two outside members, 11 differs and is canonical, 12 is identical: duplicate, member 12
    add_doc(db, 10, 1, "e/x.txt", "x", "2026-05-01 00:00+00")
    add_doc(db, 11, 2, "keep/x_changed.txt", "x2", "2026-05-05 00:00+00", "canonical")
    add_doc(db, 12, 2, "keep/x_same.txt", "x", "2026-04-01 00:00+00", "sibling_duplicate")
    for pos, d in enumerate([10, 11, 12]):
        add_chain(db, 50, d, pos)
    # 13: only a sibling_set links it to an outside doc, so it stays new
    add_doc(db, 13, 1, "f/sib.txt", "sib", "2026-05-01 00:00+00")
    add_chain(db, 60, 13, 0, "sibling_set")
    add_chain(db, 60, 5, 1, "sibling_set")

    rows = {r["doc_id"]: r for r in query(db, "inbox_three_way.sql", root_id=1)}
    assert sorted(rows) == [1, 2, 3, 4, 6, 8, 10, 13]
    assert {i: rows[i]["class"] for i in rows} == {
        1: "new", 2: "new", 3: "new", 4: "duplicate", 6: "changed", 8: "changed", 10: "duplicate", 13: "new"}
    assert rows[1]["chain_id"] is None and rows[2]["chain_id"] == 10
    assert rows[1]["path"] == "/inbox/a/solo.txt"
    assert rows[4]["canonical_member_path"] == "/projects/keep/orig.txt" and rows[4]["chain_id"] == 20
    assert rows[4]["newer_side"] is None
    assert rows[6]["newer_side"] == "inbox" and rows[6]["canonical_member_path"] == "/projects/keep/old.txt"
    assert rows[8]["newer_side"] == "outside"
    assert rows[10]["canonical_member_path"] == "/projects/keep/x_same.txt"
    assert rows[13]["canonical_member_path"] is None


def test_inbox_other_root_is_empty(db):
    db.execute("INSERT INTO atlas.atlas_root (root_id, root_path) VALUES (1, '/inbox')")
    assert query(db, "inbox_three_way.sql", root_id=99) == []


def test_project_table(db):
    db.execute("INSERT INTO atlas.atlas_root (root_id, root_path) VALUES (1, '/p')")
    add_doc(db, 1, 1, "alpha/README.md", "r", "2026-01-01 00:00+00")
    add_doc(db, 2, 1, "alpha/src/main.py", "m", "2026-03-01 00:00+00")
    add_doc(db, 3, 1, "alpha/.git/config", "g", "2026-02-01 00:00+00")
    add_doc(db, 4, 1, "beta/notes.txt", "n", "2026-04-01 00:00+00")
    add_doc(db, 5, 1, "gamma/old.txt", "o", "2026-05-01 00:00+00")
    for d, proj, method, status in [(1, "alpha", "keyword_v1", "active"), (2, "alpha", "keyword_v1", "active"),
                                    (2, "alpha", "embed_v1", "active"), (3, "alpha", "keyword_v1", "active"),
                                    (4, "beta", "keyword_v1", "active"), (5, "gamma", "keyword_v1", "superseded")]:
        db.execute("INSERT INTO atlas.atlas_doc_relevance (document_id, project, score, method, status) "
                   "VALUES (%s, %s, 1.0, %s, %s)", (d, proj, method, status))
    rows = {r["project"]: r for r in query(db, "project_table.sql")}
    assert sorted(rows) == ["alpha", "beta"]
    a = rows["alpha"]
    assert a["document_count"] == 3
    assert a["directories"] == "/p/alpha | /p/alpha/.git | /p/alpha/src"
    assert str(a["newest_revision"]) == "2026-03-01"
    assert a["has_git"] is True and a["has_readme"] is True and a["decision"] == ""
    b = rows["beta"]
    assert b["has_git"] is False and b["has_readme"] is False and b["document_count"] == 1


def add_chunk(db, chunk_id, doc, index, text):
    db.execute("INSERT INTO atlas.atlas_chunk (chunk_id, document_id, chunk_index, chunk_text, sha256) "
               "VALUES (%s, %s, %s, %s, %s)", (chunk_id, doc, index, text, sha(text)))


def add_tag(db, doc, project, score):
    db.execute("INSERT INTO atlas.atlas_doc_relevance (document_id, project, score) VALUES (%s, %s, %s)",
               (doc, project, score))


def test_provenance_payload_keys(db):
    db.execute("INSERT INTO atlas.atlas_provenance (event_id, actor, action, target_table, occurred_at, payload_jsonb) "
               "VALUES (%s, 't', 'update', 'atlas_document', '2026-07-01 00:00+00', '{\"from_path\": \"a\", \"to_path\": \"b\"}'), "
               "(%s, 't', 'update', 'atlas_document', '2026-07-02 00:00+00', '{\"sha256\": \"x\"}'), "
               "(%s, 't', 'ingest', 'atlas_document', '2026-07-03 00:00+00', '{}')",
               (str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())))
    rows = query(db, "provenance_payload_keys.sql")
    got = {(r["action"], r["payload_key"]): r["events"] for r in rows}
    assert got == {("update", "from_path"): 1, ("update", "to_path"): 1, ("update", "sha256"): 1,
                   ("ingest", "(empty)"): 1}


def seed_pipelines(db):
    db.execute("INSERT INTO atlas.atlas_root (root_id, root_path) VALUES (1, '/home/u'), (2, '/srv')")
    add_doc(db, 1, 1, "proj/run.sh", "r", "2026-01-01 00:00+00")
    add_doc(db, 2, 1, "proj/fit.py", "f", "2026-03-01 00:00+00", "canonical")
    add_doc(db, 3, 1, "proj/job.sbatch", "j", "2026-02-01 00:00+00")
    add_doc(db, 4, 1, "proj/README.md", "readme", "2026-02-01 00:00+00")
    add_doc(db, 5, 1, "proj/old_fit.py", "f0", "2025-01-01 00:00+00", "superseded_by_version")
    add_doc(db, 6, 1, "notes/todo.txt", "t", "2026-01-01 00:00+00")
    add_doc(db, 7, 1, "top.py", "top", "2026-01-01 00:00+00")
    add_doc(db, 8, 2, "x/Cargo.toml", "c", "2026-01-01 00:00+00")
    add_chunk(db, 1, 2, 0, '"""Fit the model to the residual series."""\nimport numpy')
    add_chunk(db, 2, 2, 1, "def later(): pass")
    add_tag(db, 2, "forecasting", 0.9)
    add_tag(db, 2, "misc", 0.1)
    add_tag(db, 1, "forecasting", 0.5)


def test_pipeline_inventory(db):
    seed_pipelines(db)
    rows = {r["rel_path"]: r for r in query(db, "pipeline_inventory.sql", root_id=None)}
    assert sorted(rows) == ["proj/fit.py", "proj/job.sbatch", "proj/run.sh", "top.py", "x/Cargo.toml"]
    assert rows["proj/fit.py"]["project"] == "forecasting"
    assert rows["proj/fit.py"]["header"] == '"""Fit the model to the residual series.""" \\n import numpy'
    assert rows["proj/run.sh"]["is_marker"] and rows["proj/job.sbatch"]["is_marker"]
    assert not rows["proj/fit.py"]["is_marker"]
    assert rows["top.py"]["dir"] == "." and rows["proj/fit.py"]["dir"] == "proj"
    assert rows["proj/fit.py"]["header"] is not None and rows["top.py"]["header"] is None
    only1 = query(db, "pipeline_inventory.sql", root_id=2)
    assert [r["rel_path"] for r in only1] == ["x/Cargo.toml"]


def test_pipeline_dirs(db):
    seed_pipelines(db)
    rows = {(r["root_id"], r["dir"]): r for r in query(db, "pipeline_dirs.sql", root_id=None)}
    assert sorted(rows) == [(1, "."), (1, "proj"), (2, "x")]
    p = rows[(1, "proj")]
    assert p["code_files"] == 3 and p["marker_files"] == 2
    assert p["markers"] == "job.sbatch,run.sh"
    assert p["projects"] == "forecasting"
    assert p["has_readme"] is True and rows[(1, ".")]["has_readme"] is False
    assert p["newest_mtime"] == datetime(2026, 3, 1, tzinfo=timezone.utc)
