import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from housekeeping.router.app import Router, create_app
from housekeeping.router.settings import Settings

SECRET = "SECRET-PROMPT-XYZ"


def fake_upstream(name: str) -> FastAPI:
    app = FastAPI()
    app.state.seen = []
    app.state.up = True
    app.state.gate = None  # asyncio.Event; chat requests wait on it when set

    @app.get("/v1/models")
    async def models():
        return JSONResponse({}, status_code=200 if app.state.up else 500)

    async def handle(request: Request):
        body = await request.json()
        app.state.seen.append(body)
        if app.state.gate is not None:
            await app.state.gate.wait()
        if body.get("stream"):
            async def sse():
                for i in range(3):
                    yield f"data: {name}-{i}\n\n".encode()
                yield b"data: [DONE]\n\n"
            return StreamingResponse(sse(), media_type="text/event-stream")
        return {"served_by": name, "echo": SECRET}

    app.post("/v1/chat/completions")(handle)
    app.post("/v1/embeddings")(handle)
    return app


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def world(tmp_path):
    ups = {n: fake_upstream(n) for n in ("embedder", "reader", "coder", "andromeda")}
    cfg = {"router": {
        "host": "127.0.0.1", "port": 1, "log": str(tmp_path / "router.tsv"),
        "heartbeat_dir": str(tmp_path / "hb"), "health_interval_s": 60, "health_timeout_s": 1,
        "overflow_inflight": {"reader": 1, "coder": 1},
        "roles": {n: {"url": f"http://{n}.test", "model": f"{n}-model", "timeout_s": 5}
                  for n in ("embedder", "reader", "coder")},
        "andromeda": {"url": "http://andromeda.test", "roles_allowed": ["reader"], "enabled": True,
                      "models": {"reader": "andromeda-reader"}},
    }}
    mounts = {f"http://{n}.test": httpx.ASGITransport(app=a) for n, a in ups.items()}
    router = Router(Settings.from_config(cfg), client=httpx.AsyncClient(mounts=mounts))
    front = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(router)), base_url="http://router")
    return router, front, ups, tmp_path / "router.tsv"


async def chat(front, role="reader", **extra):
    return await front.post("/v1/chat/completions", json={"role": role, "model": "ignored", "messages": [
        {"role": "user", "content": SECRET}], **extra})


@pytest.mark.anyio
async def test_rewrite_and_role_stripped(world):
    router, front, ups, _ = world
    await router.check_all()
    r = await chat(front, "coder")
    assert r.json()["served_by"] == "coder"
    sent = ups["coder"].state.seen[0]
    assert sent["model"] == "coder-model" and "role" not in sent
    r = await front.post("/v1/embeddings", json={"input": "x", "model": "nope"})
    assert r.json()["served_by"] == "embedder"
    assert ups["embedder"].state.seen[0]["model"] == "embedder-model"


@pytest.mark.anyio
async def test_bad_role_400(world):
    router, front, _, _ = world
    await router.check_all()
    assert (await chat(front, "embedder")).status_code == 400
    assert (await front.post("/v1/chat/completions", json={"messages": []})).status_code == 400


@pytest.mark.anyio
async def test_stream_passthrough(world):
    router, front, ups, _ = world
    await router.check_all()
    r = await chat(front, "reader", stream=True)
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.text == "data: reader-0\n\ndata: reader-1\n\ndata: reader-2\n\ndata: [DONE]\n\n"
    assert ups["reader"].state.seen[0]["stream"] is True


@pytest.mark.anyio
async def test_overflow_to_andromeda_only_when_busy(world):
    router, front, ups, _ = world
    await router.check_all()
    ups["reader"].state.gate = asyncio.Event()
    first = asyncio.create_task(chat(front, "reader"))
    while router.inflight["reader"] < 1:
        await asyncio.sleep(0.01)
    r = await chat(front, "reader")
    assert r.json()["served_by"] == "andromeda"
    assert ups["andromeda"].state.seen[0]["model"] == "andromeda-reader"
    ups["reader"].state.gate.set()
    assert (await first).json()["served_by"] == "reader"
    assert router.inflight["reader"] == 0
    assert (await chat(front, "reader")).json()["served_by"] == "reader"


@pytest.mark.anyio
async def test_no_overflow_for_role_not_allowed(world):
    router, front, ups, _ = world
    await router.check_all()
    ups["coder"].state.gate = asyncio.Event()
    first = asyncio.create_task(chat(front, "coder"))
    while router.inflight["coder"] < 1:
        await asyncio.sleep(0.01)
    second = asyncio.create_task(chat(front, "coder"))
    await asyncio.sleep(0.05)
    assert ups["andromeda"].state.seen == []
    ups["coder"].state.gate.set()
    await asyncio.gather(first, second)
    assert len(ups["coder"].state.seen) == 2


@pytest.mark.anyio
async def test_dead_role_503_no_failover(world):
    router, front, ups, _ = world
    ups["reader"].state.up = False
    await router.check_all()
    r = await chat(front, "reader")
    assert r.status_code == 503 and r.json()["error"]["role"] == "reader"
    assert ups["andromeda"].state.seen == [] and ups["reader"].state.seen == []


@pytest.mark.anyio
async def test_health_aggregation(world):
    router, front, ups, _ = world
    await router.check_all()
    r = await front.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body["roles"]) == {"embedder", "reader", "coder"}
    assert body["roles"]["reader"]["up"] and body["roles"]["reader"]["target"] == "http://reader.test"
    ups["coder"].state.up = False
    await router.check_all()
    r = await front.get("/health")
    assert r.status_code == 503 and r.json()["roles"]["coder"]["up"] is False
    beat = json.loads((router.s.heartbeat_dir / "router.coder.json").read_text())
    assert beat["status"] == "down"
    assert json.loads((router.s.heartbeat_dir / "router.json").read_text())["status"] == "down"


@pytest.mark.anyio
async def test_models_lists_roles(world):
    _, front, _, _ = world
    ids = [m["id"] for m in (await front.get("/v1/models")).json()["data"]]
    assert ids == ["embedder", "reader", "coder"]


@pytest.mark.anyio
async def test_log_has_no_bodies(world):
    router, front, _, log = world
    await router.check_all()
    r = await chat(front, "reader")
    assert SECRET in r.text
    await front.post("/v1/embeddings", json={"input": SECRET})
    text = log.read_text()
    assert SECRET not in text
    lines = text.splitlines()
    assert len(lines) == 2
    ts, action, source, target, ident = lines[0].split("\t")
    assert (action, source, target) == ("route", "reader", "http://reader.test")
    assert "latency_ms=" in ident
