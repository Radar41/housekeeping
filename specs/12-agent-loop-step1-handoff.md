# Coder handoff: agent loop, build step 1 (ledger and dispatcher)

Read first: `STANDARDS.md`, `specs/12-agent-loop.md`, `src/housekeeping/router/app.py`, `src/housekeeping/rebuild_all/cli.py` (how it calls the router and records state), `tests/test_router.py` (fake upstream pattern).

## Goal

A new module `hk tasks` that stores tasks in Memgraph, moves them through the lifecycle, and sends `code-card` tasks to the `reader` role through `hk-router`. No check exists yet in this step, so a successful `code-card` stops at `reported`. That is the expected end state, and it shows the rule that only a check reaches `verified`.

Out of scope for this step: the code-card check, the planner, SLURM dispatch, mem0, any other kind.

## Files

```
src/housekeeping/tasks/
  __init__.py
  cli.py          argparse, subcommands below; main(argv) -> int like the other modules
  model.py        Task, Card, Evidence dataclasses; STATUSES; ALLOWED transitions table; task_id()
  store.py        Store protocol; MemoryStore (tests); MemgraphStore (neo4j driver over Bolt)
  dispatch.py     one pass: promote, send, collect, check; run_once(store, kinds, settings, apply) -> Summary
  kinds/
    __init__.py   REGISTRY = {"code-card": code_card}
    code_card.py  NAME, INPUT_FIELDS, CLAIM_FIELDS, build_request, parse, CHECK = None
tests/tasks/
  conftest.py     MemoryStore fixture; fake router (httpx.MockTransport); tmp folder tree and tmp files_*.tsv
  test_model.py
  test_store.py
  test_dispatch.py
  test_cli.py
  test_memgraph.py   skipped unless HK_TEST_MEMGRAPH is set to a bolt URI
```

Add `neo4j>=5.20` to `pyproject.toml` dependencies (Memgraph speaks Bolt; this is the driver its docs use for Python), then `uv lock`. Add the `[tasks]` block from the spec to `config.example.toml`.

## Interfaces

`model.py`
- `STATUSES = ("proposed", "ready", "running", "reported", "verified", "failed")`
- `ALLOWED = {("proposed","ready"), ("ready","running"), ("running","reported"), ("running","failed"), ("reported","verified"), ("reported","failed"), ("failed","ready")}`
- `task_id(kind, input) = sha256(kind + "\n" + json.dumps(input, sort_keys=True, separators=(",",":")))`
- Card JSON is `{"task_id", "kind", "summary", "pointers", "claims"}`. `validate_card(card, kind, max_chars) -> list[str]` returns problems; empty means valid.

`store.py` (both backends pass the same tests)
- `propose(task) -> bool` (False if the id exists)
- `add_dependency(task_id, depends_on_id)`
- `get(task_id)`, `list(status=None, kind=None)`
- `transition(task_id, frm, to, *, by, card=None, artifact=None, evidence=None) -> bool`. Raises on a pair not in `ALLOWED`. Compare-and-set: returns False and changes nothing if the current status is not `frm`. Card, artifact and evidence nodes and their edges are written in the same transaction as the status change. Increments `attempts` on `ready -> running`.
- `blocked() -> list[(task_id, [dep ids not verified])]`
- `done() -> list[(task_id, kind, check, check_version, at)]`
- `needs_you() -> list[task_id]`: `reported` tasks whose kind has `CHECK = None`
- Every node gets `ns` from config; every query filters on it. `MemgraphStore.drop_ns(ns)` exists for tests only.
- On first connect `MemgraphStore` creates `CREATE INDEX ON :Ledger(id);` and `CREATE INDEX ON :Ledger(ns);` if missing.

`dispatch.py`, `run_once` does, in order:
1. Promote: each `proposed` task whose dependencies are all `verified` goes to `ready`.
2. Retry: each `failed` task with `attempts < max_attempts` goes to `ready`.
3. Send: each `ready` task: `kind.build_request(input)`, then `ready -> running`, then POST to `{router_url}/v1/chat/completions` with `model` set to the role. Save the raw response under `data_dir/<task id>/attempt-<n>.json` and hash it.
4. Collect: `kind.parse(raw)` gives a card. If `validate_card` returns no problems, `running -> reported` with the card and the artifact pointer. If not, or on a timeout or HTTP error, `running -> failed` with the reason as the evidence `detail` (`check = "transport"` or `"card_schema"`, `ok = false`).
5. Check: each `reported` task whose kind has a `CHECK` runs it and moves to `verified` or `failed` with the evidence. With `CHECK = None` it stays.

Router calls are synchronous and sequential in this step (`httpx.Client`). No parallelism until step 4.

Without `--apply`, `run_once` reads the store, prints each move it would make, sends nothing, writes nothing, and logs nothing.

`kinds/code_card.py`
- Input: one `pipeline-dirs` row (`root_path`, `dir`, `code_files`, `markers`, `exts`, `projects`, `newest_mtime`).
- `build_request` reads the folder's rows from the newest `~/data/housekeeping/inventory/files_*.tsv` (path from config, not hardcoded), and sends the relative paths plus each file's `header`, with the total capped at 24k characters (largest headers dropped first, and the prompt says how many were dropped). It does not read files from disk. It asks for JSON only, with the claim fields from the spec, and sends `"think": false` the way `rebuild_all` does.
- `parse` takes the first JSON object in the reply, builds the card, and puts the folder path in `pointers` along with the raw attempt file.
- `CHECK = None`.

## CLI

```
hk tasks propose code-card --from-dirs FILE [--limit N] [--match SUBSTR] [--apply]
hk tasks depend TASK_ID ON_TASK_ID [--apply]
hk tasks dispatch [--once | --watch] [--apply]       # --watch loops every poll_interval_s
hk tasks list [--status S] [--kind K] [--json]
hk tasks show TASK_ID                               # task, its newest card, evidence
hk tasks blocked | done | needs-you [--json]
```

`propose` with no `--apply` prints `would propose N, already present M`. A second `--apply` run prints `proposed 0, already present N`. `dispatch --once` with nothing to do prints `nothing ready`.

Every applied transition writes one line with `housekeeping.log.log_line` to `[tasks].log`: action `from>to`, source the kind, target the role or `check:<name>`, identifier the task id. `propose` logs action `propose`.

## Tests (all run with `make test`, no cluster)

- `test_model`: `task_id` stable under key order; `ALLOWED` rejects `proposed -> verified` and `running -> verified`; `validate_card` rejects a missing field, an unknown `state_guess`, and a card over `card_max_chars`.
- `test_store` (MemoryStore; the same test functions run against Memgraph in `test_memgraph`): propose twice gives one task; compare-and-set returns False on a stale `frm`; `blocked` and `done` return the right ids for a three-task chain.
- `test_dispatch` with the fake router:
  - a good reply ends at `reported` with a card, an artifact whose sha256 matches the file on disk, and five log lines (propose plus four moves);
  - a reply with no JSON ends at `failed`, retries once, then stays `failed` at `max_attempts`;
  - a router timeout ends at `failed` with `check = "transport"`;
  - a kind given a fake `CHECK` that passes ends at `verified`, and one that fails ends at `failed`; no code path lets a card's own fields set `verified`;
  - a dry run leaves the store and the log unchanged;
  - a second `--apply` pass changes nothing.
- `test_cli`: `propose` from a three-row tmp `dirs.tsv`, dry run then apply then apply again, with the expected counts.
- The protected rule: the prompt built by `code_card.build_request` never contains a path under `protected/` or a name in the protected names file (reuse `atlas_queries.cli.withhold_protected`).
- `hk checker ~/projects/housekeeping` still passes.

## Done when

1. `make test` passes on head with node-a and node-b down (they are now).
2. `HK_TEST_MEMGRAPH=bolt://127.0.0.1:7687 uv run pytest tests/tasks/test_memgraph.py` passes and leaves 0 nodes outside `ns = "prod"`: `echo 'MATCH (n:Ledger) WHERE n.ns <> "prod" RETURN count(n);' | mgconsole`.
3. Against the real ledger: `hk tasks propose code-card --from-dirs ~/data/housekeeping/inventory/dirs_2026-10-26.tsv --limit 3 --apply` makes 3 tasks, a repeat makes 0, and `hk tasks list` shows them `proposed`.
4. When `hk router health` shows `reader True`: `hk tasks dispatch --once --apply` takes the 3 to `reported`, `hk tasks needs-you` lists them, and `hk tasks show <id>` prints a card under 1200 characters. Until the workers are back, this item is marked waiting in the PR description, not skipped silently.
5. One branch `hk/tasks-ledger`, one PR, nothing pushed to main directly.
