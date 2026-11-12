"""Runs against MemoryStore always, and against Memgraph when HK_TEST_MEMGRAPH is set."""
import pytest

from housekeeping.tasks.model import Card, Evidence, Task, TransitionError


def mk(store, n):
    t = Task.new("code-card", {"root_id": "1", "root_path": "/r", "dir": f"d{n}"}, 2)
    assert store.propose(t)
    return t


def test_propose_twice_is_one_task(store):
    t = mk(store, 1)
    assert store.propose(Task.new(t.kind, t.input, 2)) is False
    assert [x.id for x in store.list()] == [t.id]


def test_compare_and_set(store):
    t = mk(store, 1)
    assert store.transition(t.id, "proposed", "ready")
    assert store.transition(t.id, "proposed", "ready") is False  # stale from
    assert store.get(t.id).status == "ready"
    assert store.transition(t.id, "ready", "running")
    assert store.get(t.id).attempts == 1
    with pytest.raises(TransitionError):
        store.transition(t.id, "running", "verified")


def test_card_and_evidence_round_trip(store):
    t = mk(store, 1)
    store.transition(t.id, "proposed", "ready")
    store.transition(t.id, "ready", "running")
    c = Card(fields={"summary": "s", "claims": {"a": 1}}, worker="reader", model="m")
    assert store.transition(t.id, "running", "reported", card=c)
    assert store.latest_card(t.id).fields == c.fields
    ev = Evidence(check="code-card", check_version="v1", ok=True, detail="ok")
    assert store.transition(t.id, "reported", "verified", evidence=ev)
    assert [e.check for e in store.evidence_for(t.id)] == ["code-card"]


def test_blocked_done_needs_you(store):
    a, b, c = mk(store, 1), mk(store, 2), mk(store, 3)
    assert store.add_dependency(b.id, a.id)
    assert store.add_dependency(b.id, a.id) is False
    store.add_dependency(c.id, b.id)
    assert dict(store.blocked()) == {b.id: [a.id], c.id: [b.id]}
    for frm, to in [("proposed", "ready"), ("ready", "running")]:
        store.transition(a.id, frm, to)
    store.transition(a.id, "running", "reported", card=Card(fields={"x": 1}, worker="r", model="m"))
    assert store.needs_you(["code-card"]) == [a.id]
    store.transition(a.id, "reported", "verified", evidence=Evidence("code-card", "v1", True, "ok"))
    assert dict(store.blocked()) == {c.id: [b.id]}
    assert [row[0] for row in store.done()] == [a.id]
    assert store.needs_you(["code-card"]) == []
