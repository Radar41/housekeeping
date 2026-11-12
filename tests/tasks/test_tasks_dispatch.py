import hashlib
import types
from pathlib import Path

from housekeeping.tasks.dispatch import run_once
from housekeeping.tasks.kinds import code_card
from housekeeping.tasks.model import Task

from tasks_fixtures import SECRET


def root_of(settings) -> str:
    return str(Path(settings.inventory_dir).parent / "u")


def propose(store, settings, d="fit"):
    exts = {"fit": "py", "plot": "m", "sim": "py"}[d]
    t = Task.new("code-card", code_card.input_from_row({"root_id": "21", "root_path": root_of(settings), "dir": d,
                                                        "exts": exts}), settings.max_attempts)
    store.propose(t)
    return t


def go(store, settings, client, names, apply=True, kinds=None):
    lines = []
    counts = run_once(store, settings, apply=apply, protected_names=names, client=client, kinds=kinds,
                      out=lines.append)
    return counts, lines


def log_lines(settings):
    p = Path(settings.log)
    return p.read_text().splitlines() if p.exists() else []


def test_good_reply_is_checked_and_verified(store, settings, client, names):
    t = propose(store, settings)
    counts, _ = go(store, settings, client, names)
    assert counts == {"proposed>ready": 1, "ready>running": 1, "running>reported": 1, "reported>verified": 1}
    assert store.get(t.id).status == "verified"
    ev = store.evidence_for(t.id)[-1]
    assert ev.ok and "1 files exist" in ev.detail and "1 other folders hold copies" in ev.detail
    assert [row[0] for row in store.done()] == [t.id]
    card = store.latest_card(t.id).fields
    assert card["claims"]["state_guess"] == "partial"
    ptr = card["pointers"][1]
    assert hashlib.sha256(Path(ptr["path"]).read_bytes()).hexdigest() == ptr["sha256"]
    assert [l.split("\t")[1] for l in log_lines(settings)] == ["proposed>ready", "ready>running",
                                                                "running>reported", "reported>verified"]


def test_second_pass_changes_nothing(store, settings, client, names):
    propose(store, settings)
    go(store, settings, client, names)
    n = len(log_lines(settings))
    counts, lines = go(store, settings, client, names)
    assert counts == {} and lines == ["nothing ready"]
    assert len(log_lines(settings)) == n


def test_dry_run_writes_nothing(store, settings, client, names, router):
    t = propose(store, settings)
    counts, lines = go(store, settings, client, names, apply=False)
    assert counts == {"proposed>ready": 1}
    assert store.get(t.id).status == "proposed"
    assert router.seen == [] and log_lines(settings) == []
    assert not Path(settings.data_dir).exists()


def test_no_json_fails_retries_once_then_stays_failed(store, settings, client, names, router):
    router.mode = "nojson"
    t = propose(store, settings)
    go(store, settings, client, names)
    assert store.get(t.id).status == "failed"
    assert store.evidence_for(t.id)[-1].check == "card_schema"
    go(store, settings, client, names)  # retry 2/2
    t2 = store.get(t.id)
    assert t2.status == "failed" and t2.attempts == 2
    counts, _ = go(store, settings, client, names)
    assert counts == {} and store.get(t.id).status == "failed"
    assert len(router.seen) == 2


def test_bad_claims_and_oversize_fail(store, settings, client, names, router):
    for mode, d in (("badclaims", "fit"), ("huge", "plot")):
        router.mode = mode
        t = propose(store, settings, d)
        go(store, settings, client, names)
        assert store.get(t.id).status == "failed", mode
        assert store.evidence_for(t.id)[-1].check == "card_schema"


def test_timeout_and_http_error_are_transport(store, settings, client, names, router):
    for mode, d in (("timeout", "fit"), ("http500", "plot")):
        router.mode = mode
        t = propose(store, settings, d)
        go(store, settings, client, names)
        assert store.get(t.id).status == "failed"
        assert store.evidence_for(t.id)[-1].check == "transport"


def kind_with_check(result):
    def CHECK(card, inp, **ctx):
        return result, "checked by code"
    attrs = {k: getattr(code_card, k) for k in dir(code_card) if not k.startswith("__")}
    attrs["CHECK"] = CHECK if result is not None else None
    return types.SimpleNamespace(**attrs)


def test_only_a_check_reaches_verified(store, settings, client, names):
    t = propose(store, settings)
    go(store, settings, client, names, kinds={"code-card": kind_with_check(None)})  # no check: stops at reported
    assert store.get(t.id).status == "reported"
    assert store.needs_you(["code-card"]) == [t.id]
    go(store, settings, client, names, kinds={"code-card": kind_with_check(True)})
    assert store.get(t.id).status == "verified"
    ev = store.evidence_for(t.id)[-1]
    assert ev.ok and ev.check == "code-card" and len(ev.check_version) == 64
    assert [row[0] for row in store.done()] == [t.id]


def test_failing_check_fails(store, settings, client, names):
    t = propose(store, settings)
    go(store, settings, client, names, kinds={"code-card": kind_with_check(False)})
    assert store.get(t.id).status == "failed"
    assert store.evidence_for(t.id)[-1].ok is False


def test_card_cannot_claim_verified(store, settings, client, names, router):
    """A reply that tries to set status or verified gets a failed card, not a verified task."""
    t = propose(store, settings)
    original = router.reply
    router.reply = lambda content: original(content[:-1] + ', "status": "verified", "verified": true}')
    go(store, settings, client, names)
    assert store.get(t.id).status == "failed"
    assert "unknown" in store.evidence_for(t.id)[-1].detail


def test_prompt_withholds_protected(store, settings, client, names, router):
    propose(store, settings)
    go(store, settings, client, names)
    prompt = router.seen[0]["messages"][0]["content"]
    assert "fit/run.py" in prompt and "Fit the model." in prompt
    assert "protected/key.py" not in prompt and "zeta" not in prompt
    assert router.seen[0]["model"] == "reader"


def test_logs_hold_no_prompt_or_reply(store, settings, client, names):
    propose(store, settings)
    go(store, settings, client, names)
    text = Path(settings.log).read_text()
    assert SECRET not in text and "Fit the model" not in text


def test_waits_for_dependency(store, settings, client, names):
    a = propose(store, settings, "fit")
    b = propose(store, settings, "plot")
    store.add_dependency(b.id, a.id)
    go(store, settings, client, names, kinds={"code-card": kind_with_check(None)})
    assert store.get(a.id).status == "reported"
    assert store.get(b.id).status == "proposed"  # a is reported, not verified
    assert dict(store.blocked()) == {b.id: [a.id]}
    go(store, settings, client, names)  # the real check verifies a; b is promoted on the next pass
    assert store.get(a.id).status == "verified" and store.get(b.id).status == "proposed"
    go(store, settings, client, names)
    assert store.get(b.id).status != "proposed"
