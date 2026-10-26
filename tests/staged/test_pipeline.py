"""Real PostgreSQL integration tests; deterministic Atlas stand-in, no model download.

Run: uv run --python 3.12 --with pgserver pytest -q tests/staged
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import types

import psycopg
from psycopg.types.json import Jsonb
import pytest

from housekeeping.staged.adapter import Atlas, Incompatible, digest
from housekeeping.staged.engine import Pipeline, denied, safe_path
from housekeeping.staged.__main__ import defaults_from_source

pgserver = pytest.importorskip("pgserver")


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    s = pgserver.get_server(tmp_path_factory.mktemp("pg") / "data", cleanup_mode="delete")
    yield s
    s.cleanup()


@pytest.fixture
def db(server, tmp_path):
    dsn = server.get_uri()
    root = tmp_path / "corpus"
    root.mkdir()
    with psycopg.connect(dsn) as c:
        c.execute("DROP SCHEMA IF EXISTS hk_stage CASCADE; DROP SCHEMA IF EXISTS atlas CASCADE")
        c.execute("""CREATE SCHEMA atlas;
          CREATE TABLE atlas.atlas_root(root_id bigint primary key,root_path text,excludes jsonb);
          CREATE TABLE atlas.atlas_document(
             document_id bigint generated always as identity primary key,
             root_id bigint,rel_path text,sha256 text,status text,indexed_at timestamptz,
             path_depth int,parent_rel_path text,ancestor_rel_paths jsonb,
             unique(root_id,rel_path));
          CREATE TABLE atlas.atlas_chunk(
             chunk_id bigint generated always as identity primary key,
             document_id bigint references atlas.atlas_document,
             chunk_index int,content text,chunk_metadata jsonb);
          CREATE TABLE atlas.atlas_embedding(chunk_id bigint references atlas.atlas_chunk,embedding jsonb);
        """)
        c.execute("INSERT INTO atlas.atlas_root VALUES(21,%s,'[]')", (str(root),))
    return dsn, root


class FakeAtlas:
    cfg = types.SimpleNamespace(dim=2, model="test-model")

    def __init__(self):
        self.prepared, self.embedded = [], []
        self.bad_batch = False
        self.signature = "fixture-v1"
        self.m = types.SimpleNamespace(
            excluded=lambda rel, patterns: any(rel.startswith(x) for x in patterns),
            header_only_format=lambda path, st: "csv" if path.suffix == ".csv" else None,
            upsert_document=self.upsert, insert_chunk=self.insert_chunk,
        )

    def contract(self, excludes, names, txt, md, max_chunks):
        return dict(signature=self.signature + json.dumps(excludes), excludes=excludes, protected_names=names,
                    txt_threshold=txt, md_threshold=md, max_chunks=max_chunks)

    def decision(self, rel, path):
        if path.suffix == ".exe":
            return types.SimpleNamespace(action="skip", reason="branch_policy_extension_skip")
        return None

    def noise_dir(self, rel):
        return "node_modules" in rel.split("/")

    def prepare(self, path, rel, st, contract):
        self.prepared.append(rel)
        data = path.read_bytes()
        texts = data.decode().splitlines() or ["empty"]
        return dict(digest=hashlib.sha256(data).hexdigest(), hash_kind="raw_sha256",
                    texts=texts, keys=[digest(t) for t in texts], metadata={"test": True}), None

    def embed(self, texts):
        self.embedded.extend(texts)
        return [] if self.bad_batch else [[0.0, 1.0] for t in texts]

    validate_vectors = Atlas.validate_vectors

    def writer_check(self):
        return None

    def upsert(self, cur, root_id, rel, path, sha, status, lineage):
        cur.execute("""INSERT INTO atlas.atlas_document(root_id,rel_path,sha256,status)
                       VALUES(%s,%s,%s,%s) RETURNING document_id""", (root_id, rel, sha, status))
        return cur.fetchone()[0], True

    def insert_chunk(self, cur, doc, index, text, metadata, enabled):
        cur.execute("""INSERT INTO atlas.atlas_chunk(document_id,chunk_index,content,chunk_metadata)
                       VALUES(%s,%s,%s,%s) RETURNING chunk_id""", (doc, index, text, Jsonb(metadata)))
        return cur.fetchone()[0]

    def write_embedding(self, cur, column, cid, vector, document_id, text):
        cur.execute("INSERT INTO atlas.atlas_embedding VALUES(%s,%s)", (cid, Jsonb(vector)))


def pipeline(db):
    dsn, root = db
    atlas = FakeAtlas()
    atlas.m.assert_vector = lambda v, cfg: None
    pipe = Pipeline(dsn, atlas)
    pipe.check_schema = lambda c: "embedding"  # canonical vector column uses JSONB only in this fixture
    pipe.init()
    rid = pipe.create(21, ".", ["private-person"], 1024, 1024, 100, 4096)
    return pipe, rid, atlas, root


def drain(fn, *args, **kwargs):
    n = 0
    while fn(*args, **kwargs):
        n += 1
        assert n < 1000
    return n


def counts(pipe, rid, table="file"):
    return {r["state"]: r["n"] for r in pipe.status(rid)[table]}


def test_full_pipeline_preserves_and_deduplicates_chunks(db):
    p, rid, a, root = pipeline(db)
    (root / "old.txt").write_text("CHANGED OLD FILE, must not read")
    (root / "a.txt").write_text("shared\nnew")
    (root / "b.txt").write_text("shared")
    (root / "skip.exe").write_bytes(b"skip")
    with p.connection() as c:
        c.execute("INSERT INTO atlas.atlas_document(root_id,rel_path,sha256,status) VALUES(21,'old.txt','original','indexed')")
    drain(p.scan_one, rid)
    p.plan(rid)
    assert counts(p, rid) == {"preserved": 1, "ready": 2, "skip": 1}
    assert not p.prepare_one(rid)  # starts paused
    p.pause(rid, False)
    assert drain(p.prepare_one, rid) == 2
    assert "old.txt" not in a.prepared
    assert counts(p, rid, "vector") == {"pending": 2}  # shared embedded once
    assert drain(p.embed_one, rid, 4) == 1
    assert sorted(a.embedded) == ["new", "shared"]
    assert drain(p.publish_one, rid) == 2
    assert p.status(rid)["complete"]
    with p.connection() as c:
        assert c.execute("SELECT sha256 FROM atlas.atlas_document WHERE rel_path='old.txt'").fetchone()["sha256"] == "original"
        assert c.execute("SELECT count(*) n FROM atlas.atlas_chunk").fetchone()["n"] == 3
    assert not p.publish_one(rid)
    p.plan(rid)  # idempotent
    assert counts(p, rid)["published"] == 2


def test_protected_noise_and_symlinks_not_followed(db):
    p, rid, a, root = pipeline(db)
    for directory in ("node_modules", ".aws", "protected", "private-person"):
        (root / directory).mkdir()
        (root / directory / "secret.txt").write_text("secret")
    (root / "safe.txt").write_text("safe")
    (root / ".env.production").write_text("secret")
    (root / "link").symlink_to(root / "safe.txt")
    drain(p.scan_one, rid)
    p.plan(rid)
    p.pause(rid, False)
    drain(p.prepare_one, rid)
    assert a.prepared == ["safe.txt"]
    assert counts(p, rid) == {"prepared": 1, "skip": 1}
    with pytest.raises(ValueError):
        safe_path(root, "../escape")
    with pytest.raises(ValueError):
        safe_path(root, "link")


def test_changed_file_requires_review(db):
    p, rid, a, root = pipeline(db)
    (root / "a.txt").write_text("before")
    drain(p.scan_one, rid)
    p.plan(rid)
    (root / "a.txt").write_text("changed")
    p.pause(rid, False)
    drain(p.prepare_one, rid)
    assert a.prepared == []
    assert counts(p, rid) == {"review": 1}


def test_changes_during_preparation_not_enqueued(db):
    p, rid, a, root = pipeline(db)
    path = root / "a.txt"
    path.write_text("before")
    drain(p.scan_one, rid)
    p.plan(rid)
    fn = a.prepare

    def changing(*args):
        out = fn(*args)
        path.write_text("changed")
        return out

    a.prepare = changing
    p.pause(rid, False)
    assert p.prepare_one(rid)
    assert counts(p, rid) == {"review": 1}
    assert counts(p, rid, "vector") == {}


def test_bad_embedding_rolls_back_and_resumes(db):
    p, rid, a, root = pipeline(db)
    (root / "a.txt").write_text("hello")
    drain(p.scan_one, rid)
    p.plan(rid)
    p.pause(rid, False)
    p.prepare_one(rid)
    a.bad_batch = True
    with pytest.raises(RuntimeError):
        p.embed_one(rid, 4)
    assert counts(p, rid, "vector") == {"pending": 1}
    a.bad_batch = False
    assert p.embed_one(rid, 1)
    assert counts(p, rid, "vector") == {"done": 1}


def test_publish_failure_rolls_back_document_chunks_and_state(db):
    p, rid, a, root = pipeline(db)
    (root / "a.txt").write_text("hello")
    drain(p.scan_one, rid)
    p.plan(rid)
    p.pause(rid, False)
    p.prepare_one(rid)
    p.embed_one(rid, 4)
    original = a.write_embedding

    def broken(*args):
        raise RuntimeError("intentional writer failure")

    a.write_embedding = broken
    with pytest.raises(RuntimeError):
        p.publish_one(rid)
    with p.connection() as c:
        assert c.execute("SELECT count(*) n FROM atlas.atlas_document").fetchone()["n"] == 0
        assert c.execute("SELECT count(*) n FROM atlas.atlas_chunk").fetchone()["n"] == 0
    assert counts(p, rid) == {"prepared": 1}
    a.write_embedding = original
    assert p.publish_one(rid)


def test_other_ingester_publication_preserved(db):
    p, rid, a, root = pipeline(db)
    (root / "a.txt").write_text("hello")
    drain(p.scan_one, rid)
    p.plan(rid)
    p.pause(rid, False)
    p.prepare_one(rid)
    p.embed_one(rid, 4)
    with p.connection() as c:
        c.execute("INSERT INTO atlas.atlas_document(root_id,rel_path,sha256,status) VALUES(21,'a.txt','other','indexed')")
    p.publish_one(rid)
    assert counts(p, rid) == {"preserved": 1}
    with p.connection() as c:
        assert c.execute("SELECT sha256 FROM atlas.atlas_document").fetchone()["sha256"] == "other"


def test_competing_preparers_claim_distinct_files(db):
    p, rid, a, root = pipeline(db)
    for i in range(20):
        (root / f"{i}.txt").write_text(f"unique {i}")
    drain(p.scan_one, rid)
    p.plan(rid)
    p.pause(rid, False)

    def work():
        q = Pipeline(db[0], FakeAtlas())
        return drain(q.prepare_one, rid)

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(lambda _: work(), range(4))) == 20
    assert counts(p, rid) == {"prepared": 20}
    assert counts(p, rid, "vector") == {"pending": 20}


def test_competing_gpu_workers_claim_distinct_batches(db):
    p, rid, a, root = pipeline(db)
    for i in range(12):
        (root / f"{i}.txt").write_text(f"unique {i}")
    drain(p.scan_one, rid)
    p.plan(rid)
    p.pause(rid, False)
    drain(p.prepare_one, rid)

    def work():
        atlas = FakeAtlas()
        atlas.m.assert_vector = lambda v, cfg: None
        q = Pipeline(db[0], atlas)
        drain(q.embed_one, rid, 2)
        return atlas.embedded

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: work(), range(3)))
    texts = [text for result in results for text in result]
    assert len(texts) == len(set(texts)) == 12
    assert counts(p, rid, "vector") == {"done": 12}


def test_backpressure_and_pause(db):
    p, rid, a, root = pipeline(db)
    for i in range(2):
        (root / f"{i}.txt").write_text(str(i))
    drain(p.scan_one, rid)
    p.plan(rid)
    p.pause(rid, False)
    assert p.prepare_one(rid, high_water=1)
    assert not p.prepare_one(rid, high_water=1)
    p.pause(rid, True)
    assert not p.embed_one(rid, 1)
    p.pause(rid, False)
    p.embed_one(rid, 1)
    assert p.prepare_one(rid, high_water=1)


def test_source_or_excludes_change_fails_closed(db):
    p, rid, a, root = pipeline(db)
    a.signature = "changed"
    with pytest.raises(Incompatible):
        p.scan_one(rid)
    a.signature = "fixture-v1"
    with p.connection() as c:
        c.execute("""UPDATE atlas.atlas_root SET excludes='["new/"]'""")
    with pytest.raises(Incompatible):
        p.scan_one(rid)


def test_inventory_restart_and_nonindexed_review(db):
    p, rid, a, root = pipeline(db)
    (root / "sub").mkdir()
    (root / "sub" / "a.txt").write_text("a")
    (root / "failed.txt").write_text("f")
    with p.connection() as c:
        c.execute("INSERT INTO atlas.atlas_document(root_id,rel_path,status) VALUES(21,'failed.txt','failed')")
    p.scan_one(rid)
    with pytest.raises(ValueError, match="incomplete"):
        p.plan(rid)
    q = Pipeline(db[0], a)
    drain(q.scan_one, rid)
    assert not q.scan_one(rid)
    q.plan(rid)
    assert counts(q, rid) == {"ready": 1, "review": 1}


def test_defaults_read_without_executing_source(tmp_path):
    p = tmp_path / "source.py"
    p.write_text("""raise RuntimeError('must not execute')
DEFAULT_MD_THRESHOLD = 123
p.add_argument('--docs-md-threshold', default=DEFAULT_MD_THRESHOLD)
p.add_argument('--max-chunks-per-file', default=50)
""")
    assert defaults_from_source(p) == {"--docs-md-threshold": 123, "--max-chunks-per-file": 50}
