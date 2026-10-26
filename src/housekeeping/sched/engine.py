"""GPU yield/resume decisions for embedding jobs that share a node with a router role.

plan() is pure: given jobs, router activity, claims and what we already hold, it returns the
Slurm actions and the per-role state the router gate reads. Scheduler.tick() does the I/O.

Rules, per role whose model server sits on a Slurm node:
- demand = an active claim, or a request in flight or waiting at the gate, or a request finished
  less than idle_release_s ago.
- demand: every embedding job on that node that is running gets requeuehold; a pending one gets hold.
  The role is ready once no embedding job on the node is running.
- no demand: every job this scheduler holds is released (after unloading the node's models).
  Jobs held by anyone else are never released.
- router unreachable: no new yields from activity, no releases. Claims still yield.
"""
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from housekeeping.log import log_line

from .settings import SchedSettings
from .slurm import Job

STOPPABLE = {"RUNNING", "CONFIGURING", "SUSPENDED"}  # other on-GPU states are already on their way out


@dataclass(frozen=True)
class Action:
    verb: str     # requeuehold | hold | release
    job_id: str
    role: str
    node: str


def demand_reason(role, activity, claims, now, idle_release_s) -> str | None:
    until = claims.get(role)
    if until is not None and until > now:
        return f"claim until {int(until)}"
    if activity is None:
        return None
    a = activity.get(role)
    if a is None:
        return None
    if a["busy"]:
        return "request in flight"
    if a["idle_s"] is not None and a["idle_s"] < idle_release_s:
        return f"used {int(a['idle_s'])}s ago"
    return None


def plan(nodes, prefix, jobs, activity, claims, held, now, idle_release_s):
    """Returns (actions, roles_state, new_held). held maps job_id -> role for jobs this scheduler holds."""
    live_ids = {j.id for j in jobs}
    new_held = {jid: r for jid, r in held.items() if jid in live_ids}  # forget jobs that left the queue
    actions: list[Action] = []
    roles = {}
    for role, node in nodes.items():
        mine = [j for j in jobs if j.name.startswith(prefix) and j.node == node]
        reason = demand_reason(role, activity, claims, now, idle_release_s)
        if reason is None and activity is None:
            # Router unreachable: keep whatever we hold, start nothing.
            keep = any(new_held.get(j.id) == role for j in mine)
            roles[role] = _role_state(node, mine, new_held, role, keep, "router unreachable, holding state")
            continue
        if reason is not None:
            for j in mine:
                if j.state in STOPPABLE:
                    actions.append(Action("requeuehold", j.id, role, node))
                    new_held[j.id] = role
                elif j.state == "PENDING" and not j.held:
                    actions.append(Action("hold", j.id, role, node))
                    new_held[j.id] = role
            roles[role] = _role_state(node, mine, new_held, role, True, reason)
        else:
            released = False
            for j in mine:
                if new_held.get(j.id) == role and j.held:
                    actions.append(Action("release", j.id, role, node))
                    del new_held[j.id]
                    released = True
            roles[role] = _role_state(node, mine, new_held, role, False, "idle")
            if released:
                roles[role]["ready"] = False  # the released job can start any second
    return actions, roles, new_held


def _role_state(node, mine, held, role, yielding, reason):
    return {
        "node": node,
        "yielding": yielding,
        "ready": not any(j.on_gpu for j in mine),   # no embedding job on the GPU right now
        "reason": reason,
        "embed_jobs": [{"id": j.id, "state": j.state, "held_by_sched": held.get(j.id) == role} for j in mine],
    }


# state files


def write_json_atomic(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def read_claims(path: Path) -> dict[str, float]:
    d = read_json(path) or {}
    return {r: float(t) for r, t in d.items()}


def set_claim(path: Path, role: str, until: float | None) -> None:
    d = read_claims(path)
    if until is None:
        d.pop(role, None)
    else:
        d[role] = until
    write_json_atomic(path, d)


# I/O


def router_activity(client: httpx.Client, url: str, roles) -> dict | None:
    try:
        snap = client.get(url + "/health", timeout=3).json()
    except (httpx.HTTPError, ValueError):
        return None
    out = {}
    for r in roles:
        x = snap.get("roles", {}).get(r)
        if x is None:
            continue
        out[r] = {"busy": x.get("inflight", 0) + x.get("waiting", 0) > 0, "idle_s": x.get("idle_s")}
    return out


def unload_models(client: httpx.Client, base_url: str) -> list[str]:
    """Ask an Ollama server to drop every loaded model so the GPU is empty before embedding resumes."""
    ps = client.get(base_url + "/api/ps", timeout=5).json()
    names = [m["name"] for m in ps.get("models", [])]
    for n in names:
        client.post(base_url + "/api/generate", json={"model": n, "keep_alive": 0}, timeout=30)
    return names


class Scheduler:
    def __init__(self, s: SchedSettings, slurm, client: httpx.Client | None = None, clock=time.time):
        self.s = s
        self.slurm = slurm
        self.client = client or httpx.Client()
        self.clock = clock

    def tick(self, dry_run: bool = False) -> dict:
        now = self.clock()
        prev = read_json(self.s.state_file) or {}
        held = dict(prev.get("held", {}))
        jobs = self.slurm.jobs()
        activity = router_activity(self.client, self.s.router_url, self.s.nodes)
        claims = read_claims(self.s.claims_file)
        actions, roles, new_held = plan(
            self.s.nodes, self.s.embed_job_prefix, jobs, activity, claims, held, now, self.s.idle_release_s)
        done = []
        for a in actions:
            if dry_run:
                done.append({**a.__dict__, "result": "dry-run"})
                continue
            result = self._apply(a)
            done.append({**a.__dict__, "result": result})
            if result != "ok":
                # Failed release: still ours, retry next tick. Failed hold on a job we did not
                # already hold: not ours. A repeat requeuehold on a job we hold stays ours.
                if a.verb == "release":
                    new_held[a.job_id] = a.role
                elif a.job_id not in held:
                    new_held.pop(a.job_id, None)
        state = {"ts": now, "router_up": activity is not None, "roles": roles, "held": new_held, "last_actions": done}
        if not dry_run:
            write_json_atomic(self.s.state_file, state)
        return state

    def _apply(self, a: Action) -> str:
        try:
            if a.verb == "release" and self.s.unload_before_release:
                try:
                    names = unload_models(self.client, self.s.ollama_urls[a.role])
                    log_line(self.s.log_path, "unload", a.role, a.node, ",".join(names) or "-")
                except (httpx.HTTPError, ValueError, KeyError) as e:
                    log_line(self.s.log_path, "unload_failed", a.role, a.node, type(e).__name__)
            getattr(self.slurm, a.verb)(a.job_id)
        except Exception as e:  # noqa: BLE001 - logged and retried next tick
            log_line(self.s.log_path, a.verb + "_failed", a.role, a.node, f"{a.job_id} {type(e).__name__}")
            return f"failed: {e}"
        log_line(self.s.log_path, a.verb, a.role, a.node, a.job_id)
        return "ok"

    def run_forever(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - squeue hiccups must not kill the daemon
                log_line(self.s.log_path, "tick_failed", "-", "-", type(e).__name__)
            time.sleep(self.s.tick_s)
