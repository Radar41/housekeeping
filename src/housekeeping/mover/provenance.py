"""Provenance rows for applied moves and undos, written to atlas.atlas_provenance."""
import json
import os
import uuid

ACTOR = "hk-mover"
TARGET_TABLE = "atlas.atlas_document"
SQL = (
    "INSERT INTO atlas.atlas_provenance "
    "(event_id, actor, action, target_table, target_id, payload_jsonb) "
    "VALUES (%s, %s, %s, %s, %s, %s::jsonb)"
)

_conn = None


def build_insert(action: str, chain_id: str, from_path, to_path, sha256: str, batch: str,
                 event_id: str | None = None) -> tuple[str, list]:
    """The INSERT and its parameters. Pure, so it can be checked without a database."""
    payload = {"from_path": str(from_path), "to_path": str(to_path), "sha256": sha256,
               "chain_id": chain_id, "batch": batch}
    params = [event_id or str(uuid.uuid4()), ACTOR, action, TARGET_TABLE, chain_id,
              json.dumps(payload, sort_keys=True)]
    return SQL, params


def _connection(dsn_env: str):
    global _conn
    if _conn is None:
        import psycopg  # imported late so tests and dry-runs never need a driver or a server
        _conn = psycopg.connect(os.environ[dsn_env], autocommit=True)
    return _conn


def close() -> None:
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


def write(dsn_env: str, action: str, chain_id: str, from_path, to_path, sha256: str, batch: str) -> None:
    sql, params = build_insert(action, chain_id, from_path, to_path, sha256, batch)
    _connection(dsn_env).execute(sql, params)
