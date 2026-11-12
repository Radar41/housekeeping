"""Task kinds by name. A kind is a module with NAME, ROLE, INPUT_FIELDS, CLAIM_FIELDS,
validate_input, validate_claims, build_request, parse, and CHECK (a function or None)."""
from housekeeping.tasks.kinds import code_card

REGISTRY = {code_card.NAME: code_card}


def checkless() -> list[str]:
    return [name for name, k in REGISTRY.items() if k.CHECK is None]
