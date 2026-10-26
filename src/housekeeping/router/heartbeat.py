"""Heartbeat writer for the router and each role.

ASSUMPTION: heartbeat/validator_heartbeat.py was not present in either repo when this was
written, so the real format is unknown. This writes one JSON file per component
(name, status, ts, detail) and is the only place to change once the real format is known.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def write_heartbeat(directory: Path, name: str, ok: bool, detail: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    doc = {
        "name": name,
        "status": "ok" if ok else "down",
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "detail": detail,
    }
    tmp = directory / f".{name}.json.tmp"
    tmp.write_text(json.dumps(doc), encoding="utf-8")
    os.replace(tmp, directory / f"{name}.json")
