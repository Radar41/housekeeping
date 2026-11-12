"""The ledger. MemoryStore for tests, MemgraphStore for real. Both pass the same tests.

Every write that changes a status is a compare-and-set: it happens only if the task is still in the
status the caller expected, and the card, artifact and evidence go in the same transaction.
Every node carries ns, and every query filters on it, so tests never touch the real ledger.
"""
import json
from dataclasses import asdict

from housekeeping.tasks.model import Artifact, Card, Evidence, Task, check_transition, now


class MemoryStore:
    def __init__(self, ns: str = "test"):
        self.ns = ns
        self.tasks: dict[str, Task] = {}
        self.deps: dict[str, set[str]] = {}
        self.cards: dict[str, list[Card]] = {}
        self.artifacts: dict[str, list[Artifact]] = {}
        self.evidence: dict[str, list[tuple[Evidence, str | None]]] = {}  # (evidence, card id or None)

    def propose(self, task: Task) -> bool:
        if task.id in self.tasks:
            return False
        self.tasks[task.id] = Task(**asdict(task))
        return True

    def add_dependency(self, task_id: str, on_id: str) -> bool:
        if task_id not in self.tasks or on_id not in self.tasks:
            raise KeyError("both tasks must exist")
        deps = self.deps.setdefault(task_id, set())
        if on_id in deps:
            return False
        deps.add(on_id)
        return True

    def get(self, task_id: str) -> Task | None:
        t = self.tasks.get(task_id)
        return Task(**asdict(t)) if t else None

    def list(self, status: str | None = None, kind: str | None = None) -> list[Task]:
        return [self.get(t.id) for t in sorted(self.tasks.values(), key=lambda t: (t.created_at, t.id))
                if (status is None or t.status == status) and (kind is None or t.kind == kind)]

    def dependencies(self, task_id: str) -> list[Task]:
        return [self.get(d) for d in sorted(self.deps.get(task_id, ()))]

    def transition(self, task_id, frm, to, *, card: Card | None = None, artifact: Artifact | None = None,
                   evidence: Evidence | None = None) -> bool:
        check_transition(frm, to)
        t = self.tasks.get(task_id)
        if t is None or t.status != frm:
            return False
        t.status = to
        t.updated_at = now()
        if (frm, to) == ("ready", "running"):
            t.attempts += 1
        if artifact is not None:
            self.artifacts.setdefault(task_id, []).append(artifact)
        if card is not None:
            self.cards.setdefault(task_id, []).append(card)
        if evidence is not None:
            self.evidence.setdefault(task_id, []).append((evidence, card.id if card else None))
        return True

    def latest_card(self, task_id: str) -> Card | None:
        cards = self.cards.get(task_id)
        return cards[-1] if cards else None

    def evidence_for(self, task_id: str) -> list[Evidence]:
        return [e for e, _ in self.evidence.get(task_id, [])]

    def blocked(self) -> list[tuple[str, list[str]]]:
        out = []
        for t in self.list(status="proposed"):
            waiting = [d.id for d in self.dependencies(t.id) if d.status != "verified"]
            if waiting:
                out.append((t.id, waiting))
        return out

    def done(self) -> list[tuple[str, str, str, str, str]]:
        out = []
        for t in self.list(status="verified"):
            ok = [e for e in self.evidence_for(t.id) if e.ok]
            if ok:
                e = ok[-1]
                out.append((t.id, t.kind, e.check, e.check_version, e.created_at))
        return out

    def needs_you(self, checkless_kinds) -> list[str]:
        return [t.id for t in self.list(status="reported") if t.kind in set(checkless_kinds)]


# ---------- Memgraph ----------

def _task_from(props: dict) -> Task:
    return Task(id=props["id"], kind=props["kind"], input=json.loads(props["input"]), status=props["status"],
                attempts=props["attempts"], max_attempts=props["max_attempts"],
                created_at=props["created_at"], updated_at=props["updated_at"])


class MemgraphStore:
    def __init__(self, uri: str, ns: str, user: str = "", password: str = ""):
        from neo4j import GraphDatabase

        self.ns = ns
        self.driver = GraphDatabase.driver(uri, auth=(user, password) if user else None)
        self._ensure_indexes()

    def close(self):
        self.driver.close()

    def _ensure_indexes(self):
        # Index creation must run outside an explicit transaction in Memgraph.
        with self.driver.session() as s:
            for stmt in ("CREATE INDEX ON :Ledger(id);", "CREATE INDEX ON :Ledger(ns);"):
                try:
                    s.run(stmt).consume()
                except Exception as e:  # already exists
                    if "exist" not in str(e).lower():
                        raise

    def _read(self, query: str, **params):
        with self.driver.session() as s:
            return s.execute_read(lambda tx: [r.data() for r in tx.run(query, ns=self.ns, **params)])

    def _write(self, fn):
        with self.driver.session() as s:
            return s.execute_write(fn)

    def propose(self, task: Task) -> bool:
        def fn(tx):
            if tx.run("MATCH (t:Ledger:Task {ns:$ns, id:$id}) RETURN t.id", ns=self.ns, id=task.id).single():
                return False
            tx.run("CREATE (:Ledger:Task {ns:$ns, id:$id, kind:$kind, input:$input, status:$status, "
                   "attempts:$attempts, max_attempts:$max_attempts, created_at:$created_at, "
                   "updated_at:$updated_at})",
                   ns=self.ns, id=task.id, kind=task.kind, input=json.dumps(task.input, sort_keys=True),
                   status=task.status, attempts=task.attempts, max_attempts=task.max_attempts,
                   created_at=task.created_at, updated_at=task.updated_at).consume()
            return True
        return self._write(fn)

    def add_dependency(self, task_id: str, on_id: str) -> bool:
        def fn(tx):
            rows = tx.run("MATCH (a:Ledger:Task {ns:$ns, id:$a}), (b:Ledger:Task {ns:$ns, id:$b}) "
                          "OPTIONAL MATCH (a)-[r:DEPENDS_ON]->(b) RETURN count(r) AS n",
                          ns=self.ns, a=task_id, b=on_id).data()
            if not rows:
                raise KeyError("both tasks must exist")
            if rows[0]["n"]:
                return False
            tx.run("MATCH (a:Ledger:Task {ns:$ns, id:$a}), (b:Ledger:Task {ns:$ns, id:$b}) "
                   "CREATE (a)-[:DEPENDS_ON]->(b)", ns=self.ns, a=task_id, b=on_id).consume()
            return True
        return self._write(fn)

    def get(self, task_id: str) -> Task | None:
        rows = self._read("MATCH (t:Ledger:Task {ns:$ns, id:$id}) RETURN properties(t) AS p", id=task_id)
        return _task_from(rows[0]["p"]) if rows else None

    def list(self, status: str | None = None, kind: str | None = None) -> list[Task]:
        rows = self._read("MATCH (t:Ledger:Task {ns:$ns}) WHERE ($status IS NULL OR t.status = $status) "
                          "AND ($kind IS NULL OR t.kind = $kind) RETURN properties(t) AS p "
                          "ORDER BY t.created_at, t.id", status=status, kind=kind)
        return [_task_from(r["p"]) for r in rows]

    def dependencies(self, task_id: str) -> list[Task]:
        rows = self._read("MATCH (:Ledger:Task {ns:$ns, id:$id})-[:DEPENDS_ON]->(d:Ledger:Task {ns:$ns}) "
                          "RETURN properties(d) AS p ORDER BY d.id", id=task_id)
        return [_task_from(r["p"]) for r in rows]

    def transition(self, task_id, frm, to, *, card: Card | None = None, artifact: Artifact | None = None,
                   evidence: Evidence | None = None) -> bool:
        check_transition(frm, to)
        bump = 1 if (frm, to) == ("ready", "running") else 0
        ts = now()

        def fn(tx):
            moved = tx.run("MATCH (t:Ledger:Task {ns:$ns, id:$id}) WHERE t.status = $frm "
                           "SET t.status = $to, t.updated_at = $ts, t.attempts = t.attempts + $bump "
                           "RETURN t.attempts AS attempts",
                           ns=self.ns, id=task_id, frm=frm, to=to, ts=ts, bump=bump).single()
            if moved is None:
                return False
            if artifact is not None:
                tx.run("MATCH (t:Ledger:Task {ns:$ns, id:$id}) "
                       "CREATE (t)-[:ATTEMPT_OUTPUT]->(:Ledger:Artifact {ns:$ns, path:$path, sha256:$sha, "
                       "bytes:$bytes, created_at:$ts})",
                       ns=self.ns, id=task_id, path=artifact.path, sha=artifact.sha256, bytes=artifact.bytes,
                       ts=ts).consume()
            if card is not None:
                tx.run("MATCH (t:Ledger:Task {ns:$ns, id:$id}) "
                       "CREATE (t)-[:REPORTED {attempt:$attempt}]->(c:Ledger:Card {ns:$ns, id:$cid, fields:$fields, "
                       "worker:$worker, model:$model, created_at:$at})",
                       ns=self.ns, id=task_id, attempt=moved["attempts"], cid=card.id,
                       fields=json.dumps(card.fields, sort_keys=True), worker=card.worker, model=card.model,
                       at=card.created_at).consume()
                if artifact is not None:
                    tx.run("MATCH (c:Ledger:Card {ns:$ns, id:$cid}), (a:Ledger:Artifact {ns:$ns, path:$path}) "
                           "WHERE a.sha256 = $sha MERGE (c)-[:POINTS_TO]->(a)",
                           ns=self.ns, cid=card.id, path=artifact.path, sha=artifact.sha256).consume()
            if evidence is not None:
                tx.run("MATCH (t:Ledger:Task {ns:$ns, id:$id}) "
                       "CREATE (t)-[:HAS_EVIDENCE]->(:Ledger:Evidence {ns:$ns, id:$eid, check:$check, "
                       "check_version:$ver, ok:$ok, detail:$detail, retry:$retry, created_at:$at})",
                       ns=self.ns, id=task_id, eid=evidence.id, check=evidence.check,
                       ver=evidence.check_version, ok=evidence.ok, detail=evidence.detail,
                       retry=evidence.retry, at=evidence.created_at).consume()
                cid = card.id if card is not None else None
                if cid is None:  # a check verifies the newest card on the task
                    rec = tx.run("MATCH (:Ledger:Task {ns:$ns, id:$id})-[r:REPORTED]->(c:Ledger:Card) "
                                 "RETURN c.id AS cid ORDER BY r.attempt DESC LIMIT 1",
                                 ns=self.ns, id=task_id).single()
                    cid = rec["cid"] if rec and (frm, to) in {("reported", "verified"), ("reported", "failed")} else None
                if cid is not None:
                    tx.run("MATCH (e:Ledger:Evidence {ns:$ns, id:$eid}), (c:Ledger:Card {ns:$ns, id:$cid}) "
                           "MERGE (e)-[:VERIFIES]->(c)", ns=self.ns, eid=evidence.id, cid=cid).consume()
            return True
        return self._write(fn)

    def latest_card(self, task_id: str) -> Card | None:
        rows = self._read("MATCH (:Ledger:Task {ns:$ns, id:$id})-[r:REPORTED]->(c:Ledger:Card) "
                          "RETURN properties(c) AS p ORDER BY r.attempt DESC LIMIT 1", id=task_id)
        if not rows:
            return None
        p = rows[0]["p"]
        return Card(fields=json.loads(p["fields"]), worker=p["worker"], model=p["model"], created_at=p["created_at"])

    def evidence_for(self, task_id: str) -> list[Evidence]:
        rows = self._read("MATCH (:Ledger:Task {ns:$ns, id:$id})-[:HAS_EVIDENCE]->(e:Ledger:Evidence) "
                          "RETURN properties(e) AS p ORDER BY e.created_at", id=task_id)
        return [Evidence(check=r["p"]["check"], check_version=r["p"]["check_version"], ok=r["p"]["ok"],
                         detail=r["p"]["detail"], retry=r["p"].get("retry", True),
                         created_at=r["p"]["created_at"]) for r in rows]

    def blocked(self) -> list[tuple[str, list[str]]]:
        rows = self._read("MATCH (t:Ledger:Task {ns:$ns, status:'proposed'})-[:DEPENDS_ON]->(d:Ledger:Task {ns:$ns}) "
                          "WHERE d.status <> 'verified' WITH t, d ORDER BY d.id "
                          "RETURN t.id AS id, collect(d.id) AS waiting ORDER BY id")
        return [(r["id"], r["waiting"]) for r in rows]

    def done(self) -> list[tuple[str, str, str, str, str]]:
        rows = self._read("MATCH (t:Ledger:Task {ns:$ns, status:'verified'})-[:HAS_EVIDENCE]->(e:Ledger:Evidence) "
                          "WHERE e.ok = true WITH t, e ORDER BY e.created_at "
                          "WITH t, last(collect(e)) AS e "
                          "RETURN t.id AS id, t.kind AS kind, e.check AS check, e.check_version AS ver, "
                          "e.created_at AS at ORDER BY id")
        return [(r["id"], r["kind"], r["check"], r["ver"], r["at"]) for r in rows]

    def needs_you(self, checkless_kinds) -> list[str]:
        rows = self._read("MATCH (t:Ledger:Task {ns:$ns, status:'reported'}) WHERE t.kind IN $kinds "
                          "RETURN t.id AS id ORDER BY t.created_at, t.id", kinds=list(checkless_kinds))
        return [r["id"] for r in rows]

    def drop_ns(self, ns: str) -> None:
        """Tests only: delete every node in a namespace other than prod."""
        if ns == "prod":
            raise ValueError("refusing to drop the prod namespace")
        with self.driver.session() as s:
            s.run("MATCH (n:Ledger {ns:$ns}) DETACH DELETE n", ns=ns).consume()
