import pytest

from housekeeping.tasks.kinds import code_card
from housekeeping.tasks.model import TransitionError, check_transition, task_id, validate_card

from tasks_fixtures import GOOD_CLAIMS


def card(**over):
    c = {"task_id": "t", "kind": "code-card", "summary": "Fits a model.",
         "pointers": [{"path": "/x", "sha256": ""}], "claims": dict(GOOD_CLAIMS)}
    c.update(over)
    return c


def test_task_id_ignores_key_order():
    assert task_id("code-card", {"a": 1, "b": 2}) == task_id("code-card", {"b": 2, "a": 1})
    assert task_id("code-card", {"a": 1}) != task_id("site-check", {"a": 1})


@pytest.mark.parametrize("frm,to", [("proposed", "verified"), ("running", "verified"), ("ready", "verified"),
                                    ("proposed", "running"), ("verified", "failed"), ("failed", "verified")])
def test_disallowed_transitions(frm, to):
    with pytest.raises(TransitionError):
        check_transition(frm, to)


def test_good_card_valid():
    assert validate_card(card(), code_card, 1200) == []


def test_card_missing_field():
    c = card()
    del c["claims"]["entry_point"]
    assert any("entry_point" in p for p in validate_card(c, code_card, 1200))


def test_card_unknown_state():
    c = card()
    c["claims"]["state_guess"] = "excellent"
    assert any("state_guess" in p for p in validate_card(c, code_card, 1200))


def test_card_over_cap():
    c = card()
    c["claims"]["what_it_is"] = "x" * 1300
    assert any("cap is 1200" in p for p in validate_card(c, code_card, 1200))


def test_card_wrong_kind_and_extra_keys():
    problems = validate_card(card(kind="site-check", status="verified"), code_card, 1200)
    assert any("kind is" in p for p in problems)
    assert any("unknown keys: status" in p for p in problems)
