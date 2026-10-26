"""Postgres work queues. One transaction owns one directory/file or one GPU batch.

Locks, rather than expiring leases, fence writers: a dead process loses its lock and
rolls back. No worker can commit a stale lease. Work is restartable after each unit.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import struct
import time
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .adapter import Incompatible

DENIED_PARTS = {
    "protected", ".ssh", ".aws", ".kube", ".mcp-auth", ".pki", ".codex", ".openhands",
    ".continue", ".cline", ".local/share/keyrings",
}


def denied(rel, contract, atlas, directory=False):
    lower = rel.lower()
    parts = set(lower.split("/"))
    if parts & DENIED_PARTS or ".local/share/keyrings" in lower:
        return "protected_or_credentials"
    if any(name in lower for name in contract["protected_names"]):
        return "protected_name"
    name = Path(rel).name.lower()
    if name == ".env" or name.startswith(".env.") or name.endswith((".env", ".pem", ".key")):
        return "credential_file"
    if atlas.m.excluded(rel + ("/" if directory else ""), contract["excludes"]):
        return "root_exclude"
    if directory and atlas.noise_dir(rel):
        return "branch_policy_noise_subtree"
    return None


def stat_values(st):
    return (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_dev, st.st_ino)


def matches(st, row):
    return stat_values(st) == tuple(row[k] for k in ("size", "mtime_ns", "ctime_ns", "device", "inode"))


def safe_path(root, rel):
    """Never follow a symlink or escape the root. Rechecked before and after preparation."""
    root = Path(root)
    sub = Path(rel)
    if sub.is_absolute() or ".." in sub.parts:
        raise ValueError("path_escape")
    path = root
    for part in sub.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("symlink")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("path_escape")
    return path


class Pipeline:
    def __init__(self, dsn, atlas):
        self.dsn, self.atlas = dsn, atlas
        self._conn = None

    @contextmanager
    def connection(self):
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.dsn, row_factory=dict_row, autocommit=True,
                                         application_name="hk-atlas-staged")
        c = self._conn
        with c.transaction():
            yield c

    def init(self):
        with self.connection() as c:
            user = c.execute("SELECT current_user AS u").fetchone()["u"]
            # PostgreSQL checks database CREATE privilege before IF NOT EXISTS, so only
            # attempt CREATE SCHEMA when the schema is genuinely missing.
            if c.execute("SELECT to_regnamespace('hk_stage') IS NULL AS missing").fetchone()["missing"]:
                try:
                    with c.transaction():
                        c.execute("CREATE SCHEMA hk_stage")
                except psycopg.errors.InsufficientPrivilege:
                    raise Incompatible(
                        "this role cannot create schemas in the atlas database. Run once: "
                        f"sudo -u postgres psql -d atlas -c 'CREATE SCHEMA IF NOT EXISTS hk_stage "
                        f"AUTHORIZATION \"{user}\"'") from None
            row = c.execute("""SELECT pg_get_userbyid(nspowner) AS owner,
                                      has_schema_privilege('hk_stage', 'CREATE') AS can_create
                               FROM pg_namespace WHERE nspname = 'hk_stage'""").fetchone()
            if not row["can_create"]:
                raise Incompatible(
                    f"schema hk_stage exists (owner {row['owner']}) but this role cannot create tables in it. "
                    f"Run once: sudo -u postgres psql -d atlas -c 'ALTER SCHEMA hk_stage OWNER TO \"{user}\"'")
            c.execute(Path(__file__).with_name("schema.sql").read_text())

    def root(self, c, root_id):
        row = c.execute("SELECT root_path, excludes FROM atlas.atlas_root WHERE root_id=%s",
                        (root_id,)).fetchone()
        if not row or not isinstance(row["excludes"], list):
            raise Incompatible("root missing or excludes is not an array")
        return row

    def create(self, root_id, start_rel, names, txt, md, max_chunks, max_read_bytes):
        with self.connection() as c:
            root = self.root(c, root_id)
            path = safe_path(root["root_path"], start_rel)
            if not path.is_dir():
                raise ValueError("start path is not a directory")
            contract = self.atlas.contract(root["excludes"], names, txt, md, max_chunks)
            contract["max_read_bytes"] = max_read_bytes
            contract["root_device"] = Path(root["root_path"]).stat().st_dev
            if denied(start_rel, contract, self.atlas, True):
                raise ValueError("start path is excluded")
            rid = uuid.uuid4()
            c.execute("""INSERT INTO hk_stage.run(id,root_id,root_path,start_rel,contract)
                         VALUES(%s,%s,%s,%s,%s)""",
                      (rid, root_id, str(Path(root["root_path"]).resolve()), start_rel, Jsonb(contract)))
            c.execute("INSERT INTO hk_stage.directory(run_id,rel) VALUES(%s,%s)", (rid, start_rel))
            return str(rid)

    def run(self, c, rid, check=True):
        run = c.execute("SELECT * FROM hk_stage.run WHERE id=%s", (rid,)).fetchone()
        if not run:
            raise ValueError("unknown run")
        if check:
            old = run["contract"]
            root = self.root(c, run["root_id"])
            if str(Path(root["root_path"]).resolve()) != run["root_path"]:
                raise Incompatible("root path changed: create a new run")
            current = self.atlas.contract(root["excludes"], old["protected_names"],
                                          old["txt_threshold"], old["md_threshold"], old["max_chunks"])
            # Staging-tool code (stage/*) is recorded but not frozen: fixing this tool must
            # not strand a run. Atlas source, backend settings and excludes stay frozen.
            def semantic(settings):
                keep = {k: v for k, v in settings.items() if k != "signature"}
                keep["source"] = {k: v for k, v in keep.get("source", {}).items() if not k.startswith("stage/")}
                return keep
            # Compare only the fields the contract defines; create() adds run-local fields.
            b = semantic(current)
            a = {k: v for k, v in semantic(old).items() if k in b}
            if a != b:
                changed = sorted(k for k in b if a.get(k) != b[k])
                if "source" in changed:
                    changed += sorted(k for k in b["source"] if a["source"].get(k) != b["source"][k])
                raise Incompatible(f"run is frozen; changed: {', '.join(changed)}; create a new run")
        return run

    def event(self, c, rid, kind, n=1, elapsed=None, detail=None):
        c.execute("INSERT INTO hk_stage.event(run_id,kind,n,elapsed_s,detail) VALUES(%s,%s,%s,%s,%s)",
                  (rid, kind, n, elapsed, Jsonb(detail or {})))

    def scan_one(self, rid):
        """Head-only scanner: one directory per checkpoint, no content reads."""
        t = time.monotonic()
        with self.connection() as c:
            run = self.run(c, rid)
            d = c.execute("""SELECT rel FROM hk_stage.directory WHERE run_id=%s AND state='pending'
                              ORDER BY rel FOR UPDATE SKIP LOCKED LIMIT 1""", (rid,)).fetchone()
            if d is None:
                return False
            rel_dir, contract = d["rel"], run["contract"]
            counts = {"files": 0, "dirs": 0, "pruned": 0, "errors": 0}
            try:
                current = safe_path(run["root_path"], rel_dir)
                with os.scandir(current) as entries:
                    batch = []
                    for entry in entries:
                        rel = entry.name if rel_dir == "." else rel_dir + "/" + entry.name
                        try:
                            if entry.is_symlink():
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                reason = denied(rel, contract, self.atlas, True)
                                if not reason and entry.stat(follow_symlinks=False).st_dev != contract["root_device"]:
                                    reason = "other_filesystem"
                                c.execute("""INSERT INTO hk_stage.directory(run_id,rel,state,reason)
                                             VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                                          (rid, rel, "done" if reason else "pending", reason))
                                counts["pruned" if reason else "dirs"] += 1
                            elif entry.is_file(follow_symlinks=False):
                                reason = denied(rel, contract, self.atlas)
                                dec = self.atlas.decision(rel, Path(entry.name)) if not reason else None
                                if dec and dec.action == "skip":
                                    reason = dec.reason
                                # Even name-skipped files get metadata rows; content is never opened.
                                st = entry.stat(follow_symlinks=False)
                                lane = "structured" if self.atlas.m.header_only_format(Path(entry.name), st) else "text"
                                if dec and dec.action == "sample":
                                    lane = "structured"
                                batch.append((rid, rel, *stat_values(st), lane,
                                              "skip" if reason else "candidate", reason))
                                counts["files"] += 1
                                if len(batch) >= 2000:
                                    self._inventory_batch(c, batch)
                                    batch.clear()
                        except OSError:
                            counts["errors"] += 1
                            c.execute("""INSERT INTO hk_stage.file(run_id,rel,state,reason)
                                         VALUES(%s,%s,'error','stat_failed') ON CONFLICT DO NOTHING""", (rid, rel))
                    self._inventory_batch(c, batch)
                c.execute("UPDATE hk_stage.directory SET state='done' WHERE run_id=%s AND rel=%s", (rid, rel_dir))
            except (OSError, ValueError) as e:
                c.execute("UPDATE hk_stage.directory SET state='error',reason=%s WHERE run_id=%s AND rel=%s",
                          (type(e).__name__, rid, rel_dir))
            self.event(c, rid, "scan", counts["files"], time.monotonic() - t, counts)
            return True

    def _inventory_batch(self, c, rows):
        if rows:
            with c.cursor() as cur:
                cur.executemany("""INSERT INTO hk_stage.file
                    (run_id,rel,size,mtime_ns,ctime_ns,device,inode,lane,state,reason)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""", rows)

    def plan(self, rid):
        """A set-based join, not a query per file. Existing documents are never overwritten."""
        with self.connection() as c:
            run = self.run(c, rid)
            if c.execute("SELECT 1 FROM hk_stage.directory WHERE run_id=%s AND state='pending' LIMIT 1",
                         (rid,)).fetchone():
                raise ValueError("scan incomplete: finish scan before planning")
            c.execute("""UPDATE hk_stage.file f SET
                    state=CASE WHEN d.status='indexed' THEN 'preserved' ELSE 'review' END,
                    reason=CASE WHEN d.status='indexed' THEN 'existing_index_preserved'
                                ELSE 'existing_nonindexed_review' END,
                    document_id=d.document_id
                FROM atlas.atlas_document d
                WHERE f.run_id=%s AND f.state='candidate'
                  AND d.root_id=%s AND d.rel_path=f.rel""", (rid, run["root_id"]))
            c.execute("UPDATE hk_stage.file SET state='ready' WHERE run_id=%s AND state='candidate'", (rid,))
            self.event(c, rid, "plan")

    def _file_state(self, c, rid, rel, state, reason=None, payload=None, elapsed=None):
        c.execute("""UPDATE hk_stage.file SET state=%s,reason=%s,payload=%s,elapsed_s=%s
                     WHERE run_id=%s AND rel=%s""",
                  (state, reason, Jsonb(payload) if payload else None, elapsed, rid, rel))

    def prepare_one(self, rid, lane="any", high_water=20000):
        t = time.monotonic()
        with self.connection() as c:
            run = self.run(c, rid)
            if run["paused"]:
                return False
            # Bounded prepared backlog; at most one file per worker can overshoot.
            pending = c.execute("""SELECT count(*) n FROM
                (SELECT 1 FROM hk_stage.vector WHERE run_id=%s AND state='pending' LIMIT %s) q""",
                                (rid, high_water)).fetchone()["n"]
            if pending >= high_water:
                return False
            f = c.execute("""SELECT * FROM hk_stage.file WHERE run_id=%s AND state='ready'
                  AND (%s='any' OR lane=%s) ORDER BY size,rel FOR UPDATE SKIP LOCKED LIMIT 1""",
                          (rid, lane, lane)).fetchone()
            if f is None:
                return False
            rel = f["rel"]
            # Check again if another ingest published after inventory.
            existing = c.execute("SELECT 1 FROM atlas.atlas_document WHERE root_id=%s AND rel_path=%s",
                                 (run["root_id"], rel)).fetchone()
            if existing:
                self._file_state(c, rid, rel, "preserved", "appeared_after_inventory")
                return True
            try:
                path = safe_path(run["root_path"], rel)
                st = path.stat()
                if not matches(st, f):
                    self._file_state(c, rid, rel, "review", "changed_since_inventory")
                    return True
                reason = denied(rel, run["contract"], self.atlas)
                if reason:
                    self._file_state(c, rid, rel, "skip", reason)
                    return True
                if st.st_size > run["contract"]["max_read_bytes"]:
                    self._file_state(c, rid, rel, "review", "preparation_size_safety_cap")
                    return True
                payload, reason = self.atlas.prepare(path, rel, st, run["contract"])
                if not matches(safe_path(run["root_path"], rel).stat(), f):
                    self._file_state(c, rid, rel, "review", "changed_during_preparation")
                    return True
            except Exception as e:
                # No exception messages or file content in aggregate output.
                self._file_state(c, rid, rel, "error", type(e).__name__, elapsed=time.monotonic() - t)
                return True
            if payload is None:
                self._file_state(c, rid, rel, "skip", reason, elapsed=time.monotonic() - t)
                return True
            texts = dict(zip(payload["keys"], payload.pop("texts")))
            try:
                # Savepoint: text PostgreSQL rejects (NUL bytes, invalid encodings in text or
                # JSON metadata) marks this one file and the worker continues.
                with c.transaction():
                    for key, text in sorted(texts.items()):
                        c.execute("""INSERT INTO hk_stage.vector(run_id,text_sha,text_body) VALUES(%s,%s,%s)
                                     ON CONFLICT DO NOTHING""", (rid, key, text))
                    # Text stored once per unique chunk; document keeps ordered keys and metadata.
                    self._file_state(c, rid, rel, "prepared", payload=payload, elapsed=time.monotonic() - t)
            except psycopg.DataError as e:
                self._file_state(c, rid, rel, "error", "db_" + type(e).__name__, elapsed=time.monotonic() - t)
                return True
            self.event(c, rid, "prepare", len(payload["keys"]), time.monotonic() - t)
            return True

    def embed_one(self, rid, batch):
        t = time.monotonic()
        with self.connection() as c:
            run = self.run(c, rid)
            if run["paused"]:
                return False
            rows = c.execute("""SELECT text_sha,text_body FROM hk_stage.vector
                     WHERE run_id=%s AND state='pending' ORDER BY length(text_body),text_sha
                     FOR UPDATE SKIP LOCKED LIMIT %s""", (rid, batch)).fetchall()
            if not rows:
                return False
            texts = [r["text_body"] for r in rows]
            try:
                vectors = self.atlas.embed(texts)
                self.atlas.validate_vectors(texts, vectors)
            except Exception as e:
                # Roll back whole batch; operator can reduce batch then restart. Never write
                # zero vectors, silently mark complete, or retry an OOM in a tight loop.
                raise RuntimeError(f"embedding batch failed ({type(e).__name__}); batch rolled back") from e
            elapsed = time.monotonic() - t
            for r, v in zip(rows, vectors):
                packed = struct.pack(f"<{len(v)}f", *v)
                c.execute("""UPDATE hk_stage.vector SET vector=%s,state='done',elapsed_s=%s
                             WHERE run_id=%s AND text_sha=%s""", (packed, elapsed / len(rows), rid, r["text_sha"]))
            self.event(c, rid, "embed", len(rows), elapsed)
            return True

    def pause(self, rid, paused):
        with self.connection() as c:
            self.run(c, rid)
            c.execute("UPDATE hk_stage.run SET paused=%s WHERE id=%s", (paused, rid))
            self.event(c, rid, "pause" if paused else "resume")

    def publish_one(self, rid):
        """All canonical writes for one NEW document commit atomically."""
        with self.connection() as c:
            run = self.run(c, rid)
            if run["paused"]:
                return False
            f = c.execute("""SELECT f.* FROM hk_stage.file f WHERE f.run_id=%s AND f.state='prepared'
                AND NOT EXISTS (
                    SELECT 1 FROM jsonb_array_elements_text(f.payload->'keys') k
                    LEFT JOIN hk_stage.vector v ON v.run_id=f.run_id AND v.text_sha=k.value
                    WHERE v.state IS DISTINCT FROM 'done')
                ORDER BY f.rel FOR UPDATE OF f SKIP LOCKED LIMIT 1""", (rid,)).fetchone()
            if f is None:
                return False
            rel = f["rel"]
            # Cooperative lock across staged runs. The guard below also serializes legacy
            # inserts; publication deliberately takes a brief table lock for safety.
            c.execute("SET LOCAL lock_timeout = '5s'")
            c.execute("LOCK TABLE atlas.atlas_document IN SHARE ROW EXCLUSIVE MODE")
            if c.execute("SELECT 1 FROM atlas.atlas_document WHERE root_id=%s AND rel_path=%s",
                         (run["root_id"], rel)).fetchone():
                self._file_state(c, rid, rel, "preserved", "existing_at_publication")
                return True
            try:
                path = safe_path(run["root_path"], rel)
                same = matches(path.stat(), f)
            except (OSError, ValueError):
                self._file_state(c, rid, rel, "review", "missing_or_unsafe_before_publication")
                return True
            if not same:
                self._file_state(c, rid, rel, "review", "changed_before_publication")
                return True
            column = self.check_schema(c)
            self.atlas.writer_check()
            payload = f["payload"]
            rows = c.execute("""SELECT text_sha,text_body,vector FROM hk_stage.vector
                                WHERE run_id=%s AND text_sha=ANY(%s) AND state='done'""",
                             (rid, list(set(payload["keys"])))).fetchall()
            by_key = {r["text_sha"]: r for r in rows}
            for row in rows:
                row["vector"] = list(struct.unpack(f"<{self.atlas.cfg.dim}f", row["vector"]))
            ordered = [by_key[k] for k in payload["keys"]]
            self.atlas.validate_vectors([r["text_body"] for r in ordered], [r["vector"] for r in ordered])
            # Existing helpers use tuple-row cursors.
            from psycopg.rows import tuple_row
            with c.cursor(row_factory=tuple_row) as cur:
                document_id, _ = self.atlas.m.upsert_document(
                    cur, run["root_id"], rel, path, payload["digest"], "pending", True)
                for index, row in enumerate(ordered):
                    cid = self.atlas.m.insert_chunk(cur, document_id, index, row["text_body"],
                                                    payload["metadata"], True)
                    self.atlas.write_embedding(cur, column, cid, row["vector"], document_id, row["text_body"])
                if not matches(safe_path(run["root_path"], rel).stat(), f):
                    raise RuntimeError("changed_during_publication")
                cur.execute("""UPDATE atlas.atlas_document SET status='indexed',indexed_at=now()
                               WHERE document_id=%s""", (document_id,))
            c.execute("""UPDATE hk_stage.file SET state='published',document_id=%s
                         WHERE run_id=%s AND rel=%s""", (document_id, rid, rel))
            self.event(c, rid, "publish", 1, detail={"document_id": document_id, "chunks": len(ordered)})
            return True

    def check_schema(self, c):
        fields = c.execute("""SELECT a.attname,format_type(a.atttypid,a.atttypmod) type
             FROM pg_attribute a WHERE a.attrelid='atlas.atlas_embedding'::regclass
             AND a.attnum>0 AND NOT a.attisdropped""").fetchall()
        vectors = [f for f in fields if f["type"] == f"vector({self.atlas.cfg.dim})"]
        if len(vectors) != 1 or vectors[0]["attname"] not in ("embedding", "vec"):
            raise Incompatible("expected one matching Atlas vector column")
        needed = {"atlas_document": {"path_depth", "parent_rel_path", "ancestor_rel_paths"},
                  "atlas_chunk": {"chunk_metadata"}}
        for table, required in needed.items():
            names = {r["column_name"] for r in c.execute("""SELECT column_name FROM information_schema.columns
                WHERE table_schema='atlas' AND table_name=%s""", (table,)).fetchall()}
            if not required <= names:
                raise Incompatible(f"{table}: missing lineage/metadata columns")
        return vectors[0]["attname"]

    def status(self, rid):
        with self.connection() as c:
            run = self.run(c, rid, check=False)
            result = {"run_id": str(rid), "paused": run["paused"]}
            for table in ("directory", "file", "vector"):
                result[table] = c.execute(f"""SELECT state,count(*) n FROM hk_stage.{table}
                                              WHERE run_id=%s GROUP BY state ORDER BY state""", (rid,)).fetchall()
            result["reasons"] = c.execute("""SELECT reason,count(*) n FROM hk_stage.file
               WHERE run_id=%s AND reason IS NOT NULL GROUP BY reason ORDER BY count(*) DESC""", (rid,)).fetchall()
            result["stages"] = c.execute("""SELECT kind,sum(n) units,round(sum(elapsed_s)::numeric,3) elapsed_s
                FROM hk_stage.event WHERE run_id=%s GROUP BY kind ORDER BY kind""", (rid,)).fetchall()
            result["complete"] = not any(
                r["state"] in {"pending", "candidate", "ready", "prepared"}
                for table in ("directory", "file", "vector") for r in result[table])
            result["needs_review"] = any(r["state"] in {"review", "error"}
                for table in ("directory", "file", "vector") for r in result[table])
            return result

    def drained(self, rid, stage):
        with self.connection() as c:
            for table, states in (("directory", ["pending"]), ("file", ["candidate", "ready"])):
                if c.execute(f"SELECT 1 FROM hk_stage.{table} WHERE run_id=%s AND state=ANY(%s) LIMIT 1",
                             (rid, states)).fetchone():
                    return False
            if stage == "prepare":
                return True
            if c.execute("SELECT 1 FROM hk_stage.vector WHERE run_id=%s AND state='pending' LIMIT 1",
                         (rid,)).fetchone():
                return False
            if stage == "embed":
                return True
            return c.execute("SELECT 1 FROM hk_stage.file WHERE run_id=%s AND state='prepared' LIMIT 1",
                             (rid,)).fetchone() is None
