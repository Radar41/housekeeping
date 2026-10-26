"""hk sched status | run [--once] [--dry-run] | claim <role> --minutes N | unclaim <role>"""
import json
import sys
import time

from housekeeping import config

from .engine import Scheduler, read_claims, read_json, set_claim
from .settings import SchedSettings
from .slurm import Slurm

USAGE = "usage: hk sched status | run [--once] [--dry-run] | claim <role> --minutes N | unclaim <role>"


def _print_state(state: dict, claims: dict, now: float) -> None:
    age = now - state.get("ts", 0)
    print(f"state age {int(age)}s  router_up={state.get('router_up')}")
    print(f"{'role':<8}{'node':<9}{'gpu':<11}{'embed':<9}reason")
    for role, r in state.get("roles", {}).items():
        gpu = "free" if r["ready"] else "embedding"
        embed = "yield" if r["yielding"] else "run"
        print(f"{role:<8}{r['node']:<9}{gpu:<11}{embed:<9}{r['reason']}")
        for j in r["embed_jobs"]:
            mark = " (held by sched)" if j["held_by_sched"] else ""
            print(f"          job {j['id']} {j['state']}{mark}")
    for role, until in claims.items():
        if until > now:
            print(f"claim {role} for {int((until - now) / 60)} more min")
    for a in state.get("last_actions", []):
        print(f"last action: {a['verb']} {a['job_id']} on {a['node']} -> {a['result']}")


def main(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 2
    s = SchedSettings.from_config(config.load())
    cmd, rest = argv[0], argv[1:]
    now = time.time()
    if cmd == "status" and rest in ([], ["--json"]):
        state = read_json(s.state_file)
        if state is None:
            print(f"no state at {s.state_file}; is hk-sched running?")
            return 1
        if rest:
            print(json.dumps(state))
        else:
            _print_state(state, read_claims(s.claims_file), now)
        return 0
    if cmd == "run" and set(rest) <= {"--once", "--dry-run"}:
        sched = Scheduler(s, Slurm())
        if "--once" in rest or "--dry-run" in rest:
            state = sched.tick(dry_run="--dry-run" in rest)
            _print_state(state, read_claims(s.claims_file), now)
            return 0
        sched.run_forever()
        return 0
    if cmd == "claim" and len(rest) == 3 and rest[1] == "--minutes" and rest[0] in s.nodes:
        try:
            minutes = float(rest[2])
        except ValueError:
            print(USAGE)
            return 2
        set_claim(s.claims_file, rest[0], now + minutes * 60)
        print(f"{rest[0]} claimed for {minutes:g} min; embedding on {s.nodes[rest[0]]} yields within {s.tick_s:g}s")
        return 0
    if cmd == "unclaim" and len(rest) == 1 and rest[0] in s.nodes:
        set_claim(s.claims_file, rest[0], None)
        print(f"{rest[0]} unclaimed; embedding resumes after {s.idle_release_s:g}s with no requests")
        return 0
    print(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
