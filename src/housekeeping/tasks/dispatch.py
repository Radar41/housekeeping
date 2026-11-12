"""One dispatcher pass: promote, retry, send, collect, check. The only code that changes a task's status.

Without apply it reads the ledger, prints each move it would make, and sends, writes and logs nothing.
"""
import hashlib
import inspect
import json
from dataclasses import dataclass
from pathlib import Path

from housekeeping.log import log_line
from housekeeping.tasks.kinds import REGISTRY
from housekeeping.tasks.model import Artifact, Card, Evidence, validate_card


@dataclass(frozen=True)
class Settings:
    log: Path
    data_dir: Path
    inventory_dir: str
    ns: str
    card_max_chars: int
    max_attempts: int
    poll_interval_s: float
    router_url: str
    request_timeout_s: float
    memgraph_uri: str
    memgraph_user_env: str
    memgraph_password_env: str

    @classmethod
    def from_config(cls, cfg: dict) -> "Settings":
        t = cfg["tasks"]
        m = t["memgraph"]
        return cls(log=Path(t["log"]), data_dir=Path(t["data_dir"]), inventory_dir=t["inventory_dir"],
                   ns=t["ns"], card_max_chars=int(t["card_max_chars"]), max_attempts=int(t["max_attempts"]),
                   poll_interval_s=float(t["poll_interval_s"]), router_url=t["router_url"].rstrip("/"),
                   request_timeout_s=float(t["request_timeout_s"]), memgraph_uri=m["uri"],
                   memgraph_user_env=m["user_env"], memgraph_password_env=m["password_env"])


def check_version(kind) -> str:
    """sha256 of the kind's source, so evidence says which version of the check produced it."""
    try:
        src = inspect.getsource(kind)
    except TypeError:  # not a module (tests build kinds from a namespace)
        src = inspect.getsource(kind.CHECK)
    return hashlib.sha256(src.encode("utf-8")).hexdigest()


class Pass:
    def __init__(self, store, settings: Settings, *, apply: bool, protected_names: list[str], client=None,
                 kinds=None, out=print):
        self.store, self.s, self.apply, self.names = store, settings, apply, protected_names
        self.client, self.kinds, self.out = client, kinds or REGISTRY, out
        self.counts: dict[str, int] = {}

    def _count(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1

    def move(self, task, frm, to, target, **extra) -> bool:
        """One transition: printed in a dry run, compare-and-set plus one log line with apply."""
        key = f"{frm}>{to}"
        if not self.apply:
            self.out(f"would move {task.id[:12]} {task.kind} {key} ({target})")
            self._count(key)
            return True
        if self.store.transition(task.id, frm, to, **extra):
            log_line(self.s.log, key, task.kind, target, task.id)
            self._count(key)
            return True
        log_line(self.s.log, "lost", task.kind, key, task.id)
        self._count("lost")
        return False

    def fail(self, task, check, detail, artifact=None):
        ev = Evidence(check=check, check_version="", ok=False, detail=detail[:300])
        self.move(task, "running", "failed", f"check:{check}", artifact=artifact, evidence=ev)

    def run(self) -> dict:
        st = self.store
        for t in st.list(status="proposed"):
            if all(d.status == "verified" for d in st.dependencies(t.id)):
                self.move(t, "proposed", "ready", "deps verified")
        for t in st.list(status="failed"):
            last = st.evidence_for(t.id)[-1:]
            if last and not last[0].retry:
                continue  # a retry cannot help; listed by needs-you
            if t.attempts < t.max_attempts:
                self.move(t, "failed", "ready", f"retry {t.attempts + 1}/{t.max_attempts}")
        for t in st.list(status="ready"):
            self.send(t)
        for t in st.list(status="reported"):
            self.check(t)
        if not self.counts:
            self.out("nothing ready")
        return dict(self.counts)

    def send(self, task):
        kind = self.kinds.get(task.kind)
        if kind is None:
            self.out(f"skip {task.id[:12]}: unknown kind {task.kind}")
            return
        if not self.apply:
            self.move(task, "ready", "running", kind.ROLE)
            return
        if not self.move(task, "ready", "running", kind.ROLE):
            return
        task = self.store.get(task.id)
        try:
            req = kind.build_request(task.input, inventory_dir=self.s.inventory_dir, protected_names=self.names)
        except Exception as e:
            self.fail(task, "request", f"{type(e).__name__}: {e}")
            return
        try:
            r = self.client.post(f"{self.s.router_url}/v1/chat/completions", json=req["body"],
                                 timeout=self.s.request_timeout_s)
            r.raise_for_status()
            raw = r.json()
        except Exception as e:
            self.fail(task, "transport", f"{type(e).__name__}: {e}")
            return
        artifact = self.save(task, raw)
        pointer = {"path": artifact.path, "sha256": artifact.sha256}
        card = kind.parse(raw, task_id=task.id, inp=task.input, pointers=[pointer])
        if card is None:
            self.fail(task, "card_schema", "reply held no JSON object", artifact=artifact)
            return
        problems = validate_card(card, kind, self.s.card_max_chars)
        if problems:
            self.fail(task, "card_schema", "; ".join(problems), artifact=artifact)
            return
        model = raw.get("model", "") if isinstance(raw, dict) else ""
        self.move(task, "running", "reported", kind.ROLE, artifact=artifact,
                  card=Card(fields=card, worker=kind.ROLE, model=model))

    def save(self, task, raw) -> Artifact:
        d = self.s.data_dir / task.id
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"attempt-{task.attempts}.json"
        data = json.dumps(raw, sort_keys=True, indent=1).encode("utf-8")
        p.write_bytes(data)
        return Artifact(path=str(p), sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))

    def check(self, task):
        kind = self.kinds.get(task.kind)
        if kind is None or kind.CHECK is None:
            return  # stays reported; listed by needs-you
        card = self.store.latest_card(task.id)
        if not self.apply:
            self.out(f"would check {task.id[:12]} {task.kind}")
            self._count("would_check")
            return
        retry = True
        try:
            result = kind.CHECK(card.fields if card else None, task.input, inventory_dir=self.s.inventory_dir)
            ok, detail = result[0], result[1]
            retry = bool(result[2]) if len(result) > 2 else True
        except Exception as e:
            ok, detail = False, f"check raised {type(e).__name__}: {e}"
        ev = Evidence(check=task.kind, check_version=check_version(kind), ok=bool(ok), detail=str(detail)[:300],
                      retry=retry)
        self.move(task, "reported", "verified" if ok else "failed", f"check:{task.kind}", evidence=ev)


def run_once(store, settings: Settings, *, apply: bool, protected_names: list[str], client=None, kinds=None,
             out=print) -> dict:
    return Pass(store, settings, apply=apply, protected_names=protected_names, client=client, kinds=kinds,
                out=out).run()
