"""Task lifecycle: statuses, the allowed transitions, task ids, and card validation.

Only the dispatcher moves a task, and only along ALLOWED. Nothing a model writes can set a status.
"""
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

STATUSES = ("proposed", "ready", "running", "reported", "verified", "failed")
ALLOWED = frozenset({
    ("proposed", "ready"),
    ("ready", "running"),
    ("running", "reported"),
    ("running", "failed"),
    ("reported", "verified"),
    ("reported", "failed"),
    ("failed", "ready"),
})
CARD_KEYS = ("task_id", "kind", "summary", "pointers", "claims")
SUMMARY_MAX = 200


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def task_id(kind: str, input: dict) -> str:
    return sha256_text(kind + "\n" + canonical(input))


class TransitionError(ValueError):
    pass


def check_transition(frm: str, to: str) -> None:
    if (frm, to) not in ALLOWED:
        raise TransitionError(f"transition {frm} > {to} is not allowed")


@dataclass
class Task:
    id: str
    kind: str
    input: dict
    status: str = "proposed"
    attempts: int = 0
    max_attempts: int = 2
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)

    @classmethod
    def new(cls, kind: str, input: dict, max_attempts: int) -> "Task":
        return cls(id=task_id(kind, input), kind=kind, input=input, max_attempts=max_attempts)


@dataclass
class Artifact:
    path: str
    sha256: str
    bytes: int


@dataclass
class Card:
    fields: dict
    worker: str
    model: str
    created_at: str = field(default_factory=now)

    @property
    def id(self) -> str:
        return sha256_text(canonical(self.fields))


@dataclass
class Evidence:
    check: str
    check_version: str
    ok: bool
    detail: str
    retry: bool = True  # False when asking the model again cannot help (the input itself is stale)
    created_at: str = field(default_factory=now)

    @property
    def id(self) -> str:
        return sha256_text(canonical([self.check, self.check_version, self.ok, self.detail, self.created_at]))


def validate_card(card: dict, kind, max_chars: int) -> list[str]:
    """Problems with a card; empty means valid. kind supplies CLAIM_FIELDS and validate_claims."""
    problems = []
    if not isinstance(card, dict):
        return ["card is not an object"]
    missing = [k for k in CARD_KEYS if k not in card]
    if missing:
        problems.append(f"missing keys: {','.join(missing)}")
    extra = sorted(set(card) - set(CARD_KEYS))
    if extra:
        problems.append(f"unknown keys: {','.join(extra)}")
    if card.get("kind") != kind.NAME:
        problems.append(f"kind is {card.get('kind')!r}, expected {kind.NAME!r}")
    summary = card.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        problems.append("summary is empty")
    elif len(summary) > SUMMARY_MAX:
        problems.append(f"summary over {SUMMARY_MAX} characters")
    pointers = card.get("pointers")
    if not isinstance(pointers, list) or not all(isinstance(p, dict) and "path" in p and "sha256" in p
                                                 for p in pointers):
        problems.append("pointers must be a list of {path, sha256}")
    claims = card.get("claims")
    if not isinstance(claims, dict):
        problems.append("claims is not an object")
    else:
        problems.extend(kind.validate_claims(claims))
    size = len(canonical(card))
    if size > max_chars:
        problems.append(f"card is {size} characters, cap is {max_chars}")
    return problems
