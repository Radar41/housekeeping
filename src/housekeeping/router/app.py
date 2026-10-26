"""Role-based OpenAI-compatible proxy. Agents name a role, never a model.

Bodies are never logged: the only things written are role, target url, request id, latency.
"""
import asyncio
import json
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from housekeeping.log import log_line

from .heartbeat import write_heartbeat
from .settings import CHAT_ROLES, ROLES, Settings

_RID_OK = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RoleDown(Exception):
    def __init__(self, role: str):
        self.role = role


@dataclass
class Health:
    up: bool = False
    latency_ms: float | None = None


@dataclass(frozen=True)
class Target:
    kind: str  # "local" or "andromeda"
    url: str
    model: str
    timeout_s: float


@dataclass
class Router:
    s: Settings
    client: httpx.AsyncClient = field(default_factory=httpx.AsyncClient)

    def __post_init__(self):
        self.health = {r: Health() for r in ROLES}
        self.andromeda_health = Health()
        self.inflight = {r: 0 for r in ROLES}  # local requests in flight
        self.inflight_andromeda = {r: 0 for r in ROLES}

    # routing

    def decide(self, role: str) -> Target:
        """Local first. Andromeda only as overflow for a healthy but busy local role."""
        rc = self.s.roles[role]
        if not self.health[role].up:
            raise RoleDown(role)
        a = self.s.andromeda
        limit = self.s.overflow_inflight.get(role)
        if (
            a.enabled
            and role in a.roles_allowed
            and limit is not None
            and self.inflight[role] >= limit
            and self.andromeda_health.up
        ):
            return Target("andromeda", a.url, a.models.get(role, rc.model), rc.timeout_s)
        return Target("local", rc.url, rc.model, rc.timeout_s)

    async def forward(self, role: str, path: str, body: dict, rid: str):
        """Returns a StreamingResponse passing the upstream reply through, or a JSONResponse error."""
        for _ in range(2):  # second pass only if andromeda failed to connect and local can serve
            try:
                t = self.decide(role)
            except RoleDown:
                return _error(503, f"role '{role}' is down", role)
            counter = self.inflight if t.kind == "local" else self.inflight_andromeda
            counter[role] += 1
            t0 = time.perf_counter()
            try:
                req = self.client.build_request(
                    "POST",
                    t.url + path,
                    json={**body, "model": t.model},
                    headers={"X-Request-ID": rid, "Accept-Encoding": "identity"},
                    timeout=t.timeout_s,
                )
                resp = await self.client.send(req, stream=True)
            except httpx.TimeoutException:
                counter[role] -= 1
                self._log("route_timeout", role, t.url, rid, t0)
                return _error(504, f"role '{role}' timed out", role)
            except httpx.TransportError:
                counter[role] -= 1
                self._log("route_failed", role, t.url, rid, t0)
                if t.kind == "local":
                    self.health[role] = Health(False, None)
                    return _error(503, f"role '{role}' is down", role)
                self.andromeda_health = Health(False, None)
                continue
            return StreamingResponse(
                self._relay(resp, counter, role, t, rid, t0),
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type"),
                headers={"X-Request-ID": rid},
            )
        return _error(503, f"role '{role}' unavailable", role)

    async def _relay(self, resp, counter, role, t, rid, t0):
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        except httpx.HTTPError:
            pass  # upstream dropped mid-stream; the client sees a truncated body
        finally:
            await resp.aclose()
            counter[role] -= 1
            self._log("route", role, t.url, rid, t0)

    def _log(self, action, role, url, rid, t0):
        ms = round((time.perf_counter() - t0) * 1000)
        log_line(self.s.log_path, action, role, url, f"{rid} latency_ms={ms}")

    # health

    async def _ping(self, url: str) -> Health:
        t0 = time.perf_counter()
        try:
            r = await self.client.get(url + "/v1/models", timeout=self.s.health_timeout_s)
        except httpx.HTTPError:
            return Health(False, None)
        ms = round((time.perf_counter() - t0) * 1000, 1)
        return Health(r.is_success, ms)

    async def check_all(self) -> None:
        a = self.s.andromeda
        coros = [self._ping(self.s.roles[r].url) for r in ROLES]
        if a.enabled:
            coros.append(self._ping(a.url))
        results = await asyncio.gather(*coros)
        for r, h in zip(ROLES, results):
            self.health[r] = h
        self.andromeda_health = results[3] if a.enabled else Health()
        self._beat()

    def _beat(self) -> None:
        d = self.s.heartbeat_dir
        snap = self.snapshot()
        write_heartbeat(d, "router", snap["ok"], {"andromeda": snap["andromeda"]})
        for r in ROLES:
            write_heartbeat(d, f"router.{r}", snap["roles"][r]["up"], snap["roles"][r])

    async def health_loop(self) -> None:
        while True:
            await self.check_all()
            await asyncio.sleep(self.s.health_interval_s)

    def snapshot(self) -> dict:
        roles = {}
        for r in ROLES:
            h = self.health[r]
            try:
                target = self.decide(r).url
            except RoleDown:
                target = self.s.roles[r].url
            roles[r] = {"up": h.up, "latency_ms": h.latency_ms, "inflight": self.inflight[r], "target": target}
        a = self.s.andromeda
        return {
            "ok": all(roles[r]["up"] for r in ROLES),
            "roles": roles,
            "andromeda": {
                "enabled": a.enabled,
                "up": self.andromeda_health.up,
                "latency_ms": self.andromeda_health.latency_ms,
                "inflight": dict(self.inflight_andromeda),
            },
        }


def _error(status: int, message: str, role: str | None) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "role": role}}, status_code=status)


def _request_id(request: Request) -> str:
    rid = request.headers.get("x-request-id", "")
    return rid if _RID_OK.match(rid) else uuid.uuid4().hex


async def _read_body(request: Request):
    try:
        body = json.loads(await request.body())
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def create_app(router: Router, run_health_loop: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        await router.check_all()
        task = asyncio.create_task(router.health_loop()) if run_health_loop else None
        try:
            yield
        finally:
            if task:
                task.cancel()
            await router.client.aclose()

    app = FastAPI(title="housekeeping router", lifespan=lifespan)
    app.state.router = router

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await _read_body(request)
        if body is None:
            return _error(400, "body must be a JSON object", None)
        role = body.pop("role", None)
        if role not in CHAT_ROLES:
            return _error(400, f"role must be one of {list(CHAT_ROLES)}", None)
        return await router.forward(role, "/v1/chat/completions", body, _request_id(request))

    @app.post("/v1/embeddings")
    async def embeddings(request: Request):
        body = await _read_body(request)
        if body is None:
            return _error(400, "body must be a JSON object", None)
        role = body.pop("role", "embedder")
        if role != "embedder":
            return _error(400, "embeddings only serve role 'embedder'", None)
        return await router.forward("embedder", "/v1/embeddings", body, _request_id(request))

    @app.get("/health")
    async def health():
        snap = router.snapshot()
        return JSONResponse(snap, status_code=200 if snap["ok"] else 503)

    @app.get("/v1/models")
    async def models():
        return {"object": "list", "data": [{"id": r, "object": "model", "owned_by": "router"} for r in ROLES]}

    return app
