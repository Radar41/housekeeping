"""hk router serve | health"""
import json
import sys

import httpx
import uvicorn

from housekeeping import config

from .app import Router, create_app
from .settings import ROLES, Settings


def serve(s: Settings) -> int:
    uvicorn.run(create_app(Router(s)), host=s.host, port=s.port)
    return 0


def health(s: Settings, as_json: bool = False) -> int:
    host = "127.0.0.1" if s.host in ("0.0.0.0", "::") else s.host
    try:
        snap = httpx.get(f"http://{host}:{s.port}/health", timeout=s.health_timeout_s).json()
    except (httpx.HTTPError, ValueError) as e:
        msg = f"router not reachable at {host}:{s.port}: {type(e).__name__}"
        print(json.dumps({"ok": False, "error": msg}) if as_json else msg)
        return 2
    if as_json:
        print(json.dumps(snap))
        return 0 if snap["ok"] else 1
    print(f"{'role':<10}{'up':<7}{'latency_ms':<12}{'inflight':<10}target")
    for r in ROLES:
        x = snap["roles"][r]
        lat = "-" if x["latency_ms"] is None else x["latency_ms"]
        print(f"{r:<10}{str(x['up']):<7}{lat!s:<12}{x['inflight']:<10}{x['target']}")
    a = snap["andromeda"]
    print(f"andromeda enabled={a['enabled']} up={a['up']}")
    return 0 if snap["ok"] else 1


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("serve", "health") or argv[1:] not in ([], ["--json"]):
        print("usage: hk router serve | hk router health [--json]")
        return 2
    if argv[0] == "serve" and argv[1:]:
        print("usage: hk router serve")
        return 2
    s = Settings.from_config(config.load())
    return serve(s) if argv[0] == "serve" else health(s, argv[1:] == ["--json"])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
