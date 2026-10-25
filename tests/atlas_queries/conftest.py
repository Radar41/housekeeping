"""Throwaway Postgres loaded with only the tables the atlas_queries SQL touches."""
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

SCHEMA = Path(__file__).resolve().parents[2] / "schema.txt"
TABLES = ["atlas_root", "atlas_document", "atlas_version_chain", "atlas_doc_relevance", "atlas_provenance"]
SQL_DIR = Path(__file__).resolve().parents[2] / "src/housekeeping/atlas_queries/sql"


def schema_ddl() -> str:
    """CREATE TABLE and key constraints for TABLES, copied from schema.txt.

    The pgvector column is dropped because the extension is not needed by any query.
    """
    text = SCHEMA.read_text(encoding="utf-8")
    parts = ["CREATE SCHEMA atlas;"]
    for t in TABLES:
        m = re.search(rf"CREATE TABLE atlas\.{t} \(.*?\n\);", text, re.S)
        assert m, f"{t} missing from schema.txt"
        ddl = "\n".join(l for l in m.group(0).splitlines() if "public.vector" not in l)
        parts.append(ddl)
    names = "|".join(TABLES)
    for m in re.finditer(rf"ALTER TABLE ONLY atlas\.({names})\n\s+ADD CONSTRAINT [^;]*;", text):
        stmt = m.group(0)
        refs = re.findall(r"REFERENCES atlas\.(\w+)", stmt)
        if all(r in TABLES for r in refs):
            parts.append(stmt)
    return "\n".join(parts)


def sql_text(name: str) -> str:
    return (SQL_DIR / name).read_text(encoding="utf-8")


def _find_bin() -> Path | None:
    initdb = shutil.which("initdb")
    if initdb:
        return Path(initdb).parent
    for p in sorted(Path("/usr/lib/postgresql").glob("*/bin"), reverse=True):
        if (p / "initdb").exists():
            return p
    return None


@pytest.fixture(scope="session")
def pg_dsn():
    bindir = _find_bin()
    if bindir is None:
        pytest.skip("no Postgres server binaries (initdb) on this machine; DB tests need them")
    # not pytest tmp_path: its parents are mode 700, unreadable by the postgres user when run as root
    base = Path(tempfile.mkdtemp(prefix="hk-pg-"))
    data, sock = base / "data", base / "sock"
    sock.mkdir()
    prefix: list[str] = []
    if os.geteuid() == 0:
        base.chmod(0o755)
        shutil.chown(base, "postgres")
        shutil.chown(sock, "postgres")
        prefix = ["runuser", "-u", "postgres", "--"]
    def run(*cmd):
        return subprocess.run(prefix + [str(c) for c in cmd], capture_output=True, text=True)
    r = run(bindir / "initdb", "-D", data, "-A", "trust", "-U", "postgres")
    if r.returncode:
        pytest.skip(f"initdb failed: {r.stderr.strip()[:200]}")
    opts = f"-c listen_addresses='' -c unix_socket_directories={sock} -c fsync=off"
    r = run(bindir / "pg_ctl", "-D", data, "-o", opts, "-w", "-l", base / "pg.log", "start")
    if r.returncode:
        pytest.skip(f"pg_ctl start failed: {r.stderr.strip()[:200]}")
    dsn = f"host={sock} dbname=postgres user=postgres"
    try:
        import psycopg

        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(schema_ddl())
        yield dsn
    finally:
        run(bindir / "pg_ctl", "-D", data, "-m", "immediate", "stop")
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def db(pg_dsn):
    import psycopg

    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("TRUNCATE atlas.atlas_doc_relevance, atlas.atlas_version_chain, "
                     "atlas.atlas_document, atlas.atlas_root, atlas.atlas_provenance")
        yield conn
