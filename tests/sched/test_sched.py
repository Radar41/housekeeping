import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI

from housekeeping.router.app import Router, create_app
from housekeeping.router.settings import Settings
from housekeeping.sched import cli as sched_cli
from housekeeping.sched.engine import Scheduler, plan, read_json, set_claim
from housekeeping.sched.settings import SchedSettings
from housekeeping.sched.slurm import Job, parse_squeue

NODES = {"coder": "node-a", "reader": "node-b"}
PREFIX = "atlas-fo"


def job(jid, node, state="RUNNING", reason="None", name="atlas-folder-topdown"):
    return Job(jid, name, state, node, reason)


IDLE = {"coder": {"busy": False, "idle_s": None}, "reader": {"busy": False, "idle_s": None}}


def act(**roles):
    a = json.loads(json.dumps(IDLE))
    for r, v in roles.items():
        a[r] = v
    return a


# plan(): pure decisions


def test_idle_leaves_embedding_alone():
    jobs = [job("138", "node-a"), job("139", "node-b"), job("140", "head")]
    actions, roles, held = plan(NODES, PREFIX, jobs, IDLE, {}, {}, 1000, 900)
    assert actions == [] and held == {}
    assert roles["coder"]["ready"] is False and roles["coder"]["yielding"] is False


def test_busy_role_yields_only_its_node():
    jobs = [job("138", "node-a"), job("139", "node-b"), job("140", "head")]
    actions, roles, held = plan(NODES, PREFIX, jobs, act(coder={"busy": True, "idle_s": 0}), {}, {}, 1000, 900)
    assert [(a.verb, a.job_id) for a in actions] == [("requeuehold", "138")]
    assert held == {"138": "coder"}
    assert roles["coder"]["yielding"] and not roles["coder"]["ready"]  # still on GPU until it exits
    assert not roles["reader"]["yielding"]


def test_recent_use_keeps_yield_then_releases_after_idle():
    jobs = [job("138", "node-a", "PENDING", "JobHeldUser")]
    held = {"138": "coder"}
    actions, roles, held2 = plan(NODES, PREFIX, jobs, act(coder={"busy": False, "idle_s": 100}), {}, held, 1000, 900)
    assert actions == [] and held2 == held and roles["coder"]["ready"]
    actions, _, held3 = plan(NODES, PREFIX, jobs, act(coder={"busy": False, "idle_s": 901}), {}, held, 1000, 900)
    assert [(a.verb, a.job_id) for a in actions] == [("release", "138")] and held3 == {}


def test_never_releases_someone_elses_hold():
    jobs = [job("138", "node-a", "PENDING", "JobHeldUser")]
    actions, _, _ = plan(NODES, PREFIX, jobs, IDLE, {}, {}, 1000, 900)
    assert actions == []


def test_pending_job_is_held_so_it_cannot_start_under_a_model():
    jobs = [job("141", "node-a", "PENDING", "Resources")]
    actions, roles, held = plan(NODES, PREFIX, jobs, act(coder={"busy": True, "idle_s": 0}), {}, {}, 1000, 900)
    assert [(a.verb, a.job_id) for a in actions] == [("hold", "141")] and roles["coder"]["ready"]


def test_claim_yields_even_with_router_down_and_router_down_changes_nothing_else():
    jobs = [job("138", "node-a"), job("139", "node-b", "PENDING", "JobHeldUser")]
    actions, roles, held = plan(NODES, PREFIX, jobs, None, {"coder": 2000}, {"139": "reader"}, 1000, 900)
    assert [(a.verb, a.job_id) for a in actions] == [("requeuehold", "138")]
    assert held == {"138": "coder", "139": "reader"}  # reader's hold is kept, not released
    assert roles["reader"]["yielding"]


def test_expired_claim_is_ignored():
    actions, _, _ = plan(NODES, PREFIX, [job("138", "node-a")], IDLE, {"coder": 999}, {}, 1000, 900)
    assert actions == []


def test_transitional_state_is_not_requeued_twice():
    jobs = [job("138", "node-a", "COMPLETING")]
    actions, roles, held = plan(NODES, PREFIX, jobs, act(coder={"busy": True, "idle_s": 0}), {}, {"138": "coder"}, 1, 900)
    assert actions == [] and held == {"138": "coder"} and not roles["coder"]["ready"]


def test_other_jobs_and_vanished_holds():
    jobs = [job("200", "node-a", name="jobs-echo_reader")]
    actions, roles, held = plan(NODES, PREFIX, jobs, act(coder={"busy": True, "idle_s": 0}), {}, {"138": "coder"}, 1, 900)
    assert actions == [] and held == {} and roles["coder"]["ready"]


def test_parse_squeue_uses_requested_node_when_pending():
    out = "138|atlas-folder-topdown|RUNNING|node-a|node-a|None\n141|atlas-folder-topdown|PENDING|||node-b|JobHeldUser\n"
    with pytest.raises(ValueError):
        parse_squeue(out)
    out = "138|atlas-folder-topdown|RUNNING|node-a|node-a|None\n141|atlas-folder-topdown|PENDING||node-b|JobHeldUser\n"
    a, b = parse_squeue(out)
    assert a.node == "node-a" and a.on_gpu and b.node == "node-b" and b.held


# Scheduler.tick(): I/O with fakes


class FakeSlurm:
    def __init__(self, jobs):
        self.jobs_ = {j.id: j for j in jobs}
        self.calls = []
        self.fail = set()

    def jobs(self):
        return list(self.jobs_.values())

    def _set(self, jid, state, reason):
        j = self.jobs_[jid]
        self.jobs_[jid] = Job(j.id, j.name, state, j.node, reason)

    def requeuehold(self, jid):
        self.calls.append(("requeuehold", jid))
        if "requeuehold" in self.fail:
            raise RuntimeError("Access/permission denied")
        self._set(jid, "PENDING", "JobHeldUser")  # instant exit, for the test

    def hold(self, jid):
        self.calls.append(("hold", jid))
        self._set(jid, "PENDING", "JobHeldUser")

    def release(self, jid):
        self.calls.append(("release", jid))
        self._set(jid, "RUNNING", "None")


def fake_http(activity, unloaded):
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"ok": True, "roles": activity}

    @app.get("/api/ps")
    async def ps():
        return {"models": [{"name": "qwen3.5:9b"}]}

    @app.post("/api/generate")
    async def gen(body: dict):
        unloaded.append(body)
        return {}

    return _asgi_sync(app)


def _asgi_sync(app):
    """Sync httpx client over an ASGI app, via a private event loop per request."""
    transport = httpx.ASGITransport(app=app)

    class T(httpx.BaseTransport):
        def handle_request(self, request):
            async def go():
                req = httpx.Request(request.method, request.url, headers=request.headers, content=request.read())
                r = await transport.handle_async_request(req)
                body = await r.aread()
                return httpx.Response(r.status_code, headers=r.headers, content=body)
            return asyncio.run(go())

    return httpx.Client(transport=T())


def cfg(tmp_path):
    return {
        "router": {"roles": {"coder": {"url": "http://node-a.test"}, "reader": {"url": "http://node-b.test"}}},
        "sched": {
            "router_url": "http://router.test", "state_file": str(tmp_path / "state.json"),
            "claims_file": str(tmp_path / "claims.json"), "log": str(tmp_path / "sched.tsv"),
            "tick_s": 5, "idle_release_s": 900, "embed_job_prefix": PREFIX, "unload_before_release": True,
            "nodes": NODES,
        },
    }


def test_tick_yields_then_resumes_with_unload(tmp_path):
    s = SchedSettings.from_config(cfg(tmp_path))
    activity = {"coder": {"inflight": 0, "waiting": 1, "idle_s": 0}, "reader": {"inflight": 0, "waiting": 0, "idle_s": None}}
    unloaded = []
    slurm = FakeSlurm([job("138", "node-a"), job("139", "node-b")])
    sch = Scheduler(s, slurm, client=fake_http(activity, unloaded), clock=lambda: 1000.0)

    st = sch.tick()
    assert slurm.calls == [("requeuehold", "138")]
    assert read_json(s.state_file)["held"] == {"138": "coder"}
    st = sch.tick()  # job now held: GPU free, no repeat action
    assert slurm.calls == [("requeuehold", "138")] and st["roles"]["coder"]["ready"]

    activity["coder"] = {"inflight": 0, "waiting": 0, "idle_s": 1000}
    st = sch.tick()
    assert slurm.calls[-1] == ("release", "138")
    assert unloaded == [{"model": "qwen3.5:9b", "keep_alive": 0}]
    assert st["held"] == {} and not st["roles"]["coder"]["ready"]
    log = (tmp_path / "sched.tsv").read_text()
    assert "requeuehold\tcoder\tnode-a\t138" in log and "release\tcoder\tnode-a\t138" in log


def test_tick_dry_run_touches_nothing(tmp_path):
    s = SchedSettings.from_config(cfg(tmp_path))
    activity = {"coder": {"inflight": 2, "waiting": 0, "idle_s": 0}}
    slurm = FakeSlurm([job("138", "node-a")])
    st = Scheduler(s, slurm, client=fake_http(activity, []), clock=lambda: 1.0).tick(dry_run=True)
    assert slurm.calls == [] and not s.state_file.exists()
    assert st["last_actions"][0]["result"] == "dry-run"


def test_failed_requeuehold_is_not_recorded_as_ours(tmp_path):
    s = SchedSettings.from_config(cfg(tmp_path))
    slurm = FakeSlurm([job("138", "node-a")])
    slurm.fail.add("requeuehold")
    activity = {"coder": {"inflight": 1, "waiting": 0, "idle_s": 0}}
    st = Scheduler(s, slurm, client=fake_http(activity, []), clock=lambda: 1.0).tick()
    assert st["held"] == {} and st["last_actions"][0]["result"].startswith("failed")
    assert "requeuehold_failed" in (tmp_path / "sched.tsv").read_text()


def test_settings_reject_shared_node_and_empty_prefix(tmp_path):
    c = cfg(tmp_path)
    c["sched"]["nodes"] = {"coder": "node-a", "reader": "node-a"}
    with pytest.raises(ValueError):
        SchedSettings.from_config(c)
    c = cfg(tmp_path)
    c["sched"]["embed_job_prefix"] = ""
    with pytest.raises(ValueError):
        SchedSettings.from_config(c)


def test_cli_claim_unclaim(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sched_cli.config, "load", lambda: cfg(tmp_path))
    assert sched_cli.main(["claim", "coder", "--minutes", "60"]) == 0
    assert "coder" in json.loads((tmp_path / "claims.json").read_text())
    assert sched_cli.main(["unclaim", "coder"]) == 0
    assert json.loads((tmp_path / "claims.json").read_text()) == {}
    assert sched_cli.main(["claim", "embedder", "--minutes", "5"]) == 2


# router gate


@pytest.fixture
def anyio_backend():
    return "asyncio"


def gated_router(tmp_path, wait_s=2.0, stale_s=30.0):
    up = FastAPI()

    @up.get("/v1/models")
    async def models():
        return {}

    @up.post("/v1/chat/completions")
    async def chat(body: dict):
        return {"served_by": "coder"}

    rc = {"router": {
        "host": "127.0.0.1", "port": 1, "log": str(tmp_path / "router.tsv"),
        "health_interval_s": 60, "health_timeout_s": 1, "overflow_inflight": {},
        "roles": {n: {"url": f"http://{n}.test", "model": "m", "timeout_s": 5} for n in ("embedder", "reader", "coder")},
        "andromeda": {"url": "http://andromeda.test", "roles_allowed": [], "enabled": False},
        "gate": {"enabled": True, "state_file": str(tmp_path / "state.json"), "wait_s": wait_s,
                 "stale_s": stale_s, "poll_s": 0.05},
    }}
    mounts = {f"http://{n}.test": httpx.ASGITransport(app=up) for n in ("embedder", "reader", "coder")}
    router = Router(Settings.from_config(rc), client=httpx.AsyncClient(mounts=mounts))
    front = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(router, run_health_loop=False)),
                              base_url="http://router")
    return router, front


def write_state(tmp_path, ready, ts=None):
    import time as _t
    (tmp_path / "state.json").write_text(json.dumps(
        {"ts": _t.time() if ts is None else ts, "roles": {"coder": {"ready": ready}}}))


async def post(front):
    return await front.post("/v1/chat/completions", json={"role": "coder", "messages": []})


@pytest.mark.anyio
async def test_gate_waits_until_ready_and_counts_waiting(tmp_path):
    router, front = gated_router(tmp_path)
    await router.check_all()
    write_state(tmp_path, False)
    task = asyncio.create_task(post(front))
    await asyncio.sleep(0.2)
    assert not task.done() and router.snapshot()["roles"]["coder"]["waiting"] == 1
    write_state(tmp_path, True)
    r = await asyncio.wait_for(task, 2)
    assert r.json()["served_by"] == "coder"
    snap = router.snapshot()["roles"]["coder"]
    assert snap["waiting"] == 0 and snap["idle_s"] is not None
    assert "gate_waited\tcoder" in (tmp_path / "router.tsv").read_text()


@pytest.mark.anyio
async def test_gate_passes_on_stale_missing_or_timeout(tmp_path):
    router, front = gated_router(tmp_path, wait_s=0.3)
    await router.check_all()
    assert (await post(front)).status_code == 200            # no state file
    write_state(tmp_path, False, ts=0)
    assert (await post(front)).status_code == 200            # stale: scheduler down
    write_state(tmp_path, False)
    r = await post(front)                                      # never ready: pass after wait_s
    assert r.status_code == 200 and "gate_timeout" in (tmp_path / "router.tsv").read_text()


def test_router_config_without_gate_still_loads(tmp_path):
    rc = {"router": {
        "host": "h", "port": 1, "log": "x", "health_interval_s": 1, "health_timeout_s": 1, "overflow_inflight": {},
        "roles": {n: {"url": "u", "model": "m", "timeout_s": 1} for n in ("embedder", "reader", "coder")},
        "andromeda": {"url": "a", "roles_allowed": [], "enabled": False},
    }}
    assert Settings.from_config(rc).gate.enabled is False
