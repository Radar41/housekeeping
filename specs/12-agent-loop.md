# 12. Agent loop: planner, ledger, dispatcher, checks

Spec. No code yet. When the code lands this becomes `processes/12-agent-loop.md` and a line in `INSTRUCTIONS.md` under the order table, and the first item of "Not built yet" is struck.

## What it is

A planner model on head keeps the task list and the results, never the raw material. Workers on node-a and node-b read and write. node-c fetches and checks. A task is done only when a check written in code says so. A model can propose a task and report on it; it can never mark one verified.

## State of the pieces (checked 2026-11-11)

| Piece | State | Notes |
|---|---|---|
| `hk-router` on head :8000 | running, all roles down | `hk router health`: reader, coder, embedder all `False`. SLURM shows node-a and node-b `down* Not responding`; ssh to 10.10.1.2 and 10.10.2.2 fails. The 766 routed calls in `logs/router.tsv` are all from 2026-10-26. The loop's code and tests must not need the workers; a live trial waits until they are back. |
| Memgraph on head | running, empty | Native install, 3.14.0, Bolt on 7687, data in `/var/lib/memgraph`, WAL on, snapshot every 300 s and on exit. 0 nodes. Memgraph Lab and MAGE containers exist but are not running. No Bolt driver in the housekeeping venv yet. |
| node-c | idle | 10 CPUs, 15 GB, no GPU, 1 GbE. Mounts `/home/radar-41-0` and `/srv/warm/cluster` from head (10.10.3.1) over NFS, so pointers under `~/data` resolve there. In SLURM partitions `main` and `cpu` (shared with head): target it with `--nodelist=node-c`. |
| Inputs for the first job | present | `~/data/housekeeping/inventory/dirs_2026-10-26.tsv` and `files_2026-10-26.tsv` from process 2. |
| `checker`, `atlas-queries`, `rebuild-all`, SLURM templates | working | Reused as is. `rebuild_all.cli.checker()` shows how to call the checker in-process. |

## Roles

1. **Planner** (head GPU). Turns your intent into task proposals, reads verified cards, decides what is next. It only calls `hk tasks propose` and `hk tasks show/list`. It opens no file or page.
   - Now: `qwen3.5:9b` at 131k on head. Head's GPU also holds the Atlas query service (about 8 GB), so check `ollama ps` shows `100% GPU` with both loaded.
   - Later: if the Atlas embedder moves to a 5070, the planner gets the whole 20 GB (about a 14B model at 64k to 128k). A 32B planner needs the cross-machine split and is out of scope.
2. **Workers.** Quick calls go through `hk-router` (`reader`, `coder`). Bulk work goes out as SLURM arrays: node-a and node-b for model work, node-c for CPU work (fetch, hash, checkers, API polls). node-c is a SLURM worker, not a server.
3. **Ledger** (Memgraph). Tasks, cards, evidence and how they connect. Written only by `hk tasks` code.
4. **Dispatcher** (`hk tasks dispatch` on head). The only thing that changes a task's status.
5. **Checks.** One function per task kind, no model inside. Its result is the only way to `verified`.
6. **mem0** (later, build step 5). The planner's long-term memory of you: standing intents and past decisions. Written by a model, so it is kept apart from the ledger (see Separation).

## Task lifecycle

```
proposed -> ready -> running -> reported -> verified
                         |          |    \-> failed
                         \-> failed  \-> (no check for this kind: stays reported, listed by `hk tasks needs-you`)
failed -> ready   only while attempts < max_attempts, by the dispatcher
```

| Transition | Who | When |
|---|---|---|
| (none) to `proposed` | `hk tasks propose` | the kind is registered and the input validates against that kind's schema |
| `proposed` to `ready` | dispatcher | every `DEPENDS_ON` target is `verified` |
| `ready` to `running` | dispatcher | it has sent the work (router call made or SLURM job id returned) |
| `running` to `reported` | dispatcher | a card came back and passed the card schema and size cap |
| `running` to `failed` | dispatcher | timeout, transport error, or the card failed the schema or cap |
| `reported` to `verified` / `failed` | dispatcher | the kind's check returned pass / fail; the evidence node is written in the same transaction |

Every transition is a compare-and-set in one Memgraph transaction (`MATCH (t:Task {id:$id, status:$from}) SET t.status=$to ...`). If the status has already moved, nothing changes and the dispatcher logs `lost`. Every transition is also one line in `logs/tasks.tsv` (ISO timestamp, action `from>to`, task kind, target, task id).

## Graph model

Labels carry the `Ledger` label as well so nothing collides with mem0's graph.

| Node | Key properties |
|---|---|
| `(:Ledger:Task)` | `id` (sha256 of kind + canonical input JSON), `kind`, `status`, `input` (JSON), `attempts`, `max_attempts`, `created_at`, `updated_at`, `ns` |
| `(:Ledger:Card)` | `id` (sha256 of card JSON), `fields` (JSON, capped), `worker` (role or SLURM job id), `model`, `created_at`, `ns` |
| `(:Ledger:Artifact)` | `path`, `sha256`, `bytes`, `ns`. The full raw output, saved under `[tasks].data_dir/<task id>/` |
| `(:Ledger:Evidence)` | `id`, `check` (name), `check_version` (sha256 of the check's source file), `ok`, `detail` (short text), `created_at`, `ns` |

| Edge | Meaning |
|---|---|
| `(Task)-[:DEPENDS_ON]->(Task)` | must be verified first |
| `(Task)-[:REPORTED]->(Card)` | one per attempt; the newest is current |
| `(Card)-[:POINTS_TO]->(Artifact)` | the raw output behind the card |
| `(Evidence)-[:VERIFIES]->(Card)` | the check result for that card |
| `(Task)-[:PRODUCED]->(Artifact)` | files the task made (rebuilds, fetched pages) |

`ns` is the namespace from config (`prod` normally). Tests use a random `ns` and delete it after, so a test run never touches the real ledger.

The two questions are one query each:
- What is blocked: `MATCH (t:Ledger:Task {ns:$ns, status:'proposed'})-[:DEPENDS_ON]->(d:Ledger:Task) WHERE d.status <> 'verified' RETURN t.id, collect(d.id)`
- What is done and on what evidence: `MATCH (t:Ledger:Task {ns:$ns, status:'verified'})-[:REPORTED]->(c)<-[:VERIFIES]-(e {ok:true}) RETURN t.id, t.kind, e.check, e.check_version, e.created_at`

## Cards

A card is the only thing the planner reads. Fixed fields per kind, serialized JSON at most `[tasks].card_max_chars` (default 1200, about 300 tokens), with pointers to the full output. A card over the cap or missing a field is a failed attempt, not a truncated card. 100 cards at that size is about 30k tokens, which fits the planner's context; the raw material behind them would not.

Fields every card has: `task_id`, `kind`, `summary` (at most 200 characters), `pointers` (list of `{path, sha256}`), `claims` (the kind's fields).

## Separation

- The ledger is written only by `hk tasks` code. Model output enters it only as card fields, which are labelled as claims until a check verifies them.
- A check verifies the facts on a card (files exist, hashes match, exit codes). Judgements on a card (for example a code-card's `state_guess`) stay claims after verification; the table shows them as the model's guess.
- mem0 is written by a model. It uses Memgraph for its graph but never the `Ledger` label, and the planner treats what it recalls as context, not evidence.

## Task kinds

A kind is one Python file under `src/housekeeping/tasks/kinds/` with: `NAME`, an input schema, a card schema, `build_request(input)` (what to send and to which role or SLURM template), `parse(raw)` (raw output to card), and `check(card, input)` or `CHECK = None`. A kind without a check stops at `reported`.

### First kind: `code-card`

| | |
|---|---|
| Input | one row of `pipeline-dirs`: `root_path`, `dir`, `code_files`, `markers`, `exts`, `projects`, `newest_mtime` |
| Request | `reader` role. The dispatcher builds the prompt from the folder's file list and the `header` text in `files_*.tsv` (process 2); it does not paste whole files |
| Claims | `what_it_is`, `entry_point` (relative path), `language`, `dependencies` (up to 10), `related_project`, `state_guess` (`working`, `partial`, `parked`, `dead`), `files_cited` (up to 20 relative paths) |
| Check (build step 2), run on node-c | `entry_point` and every `files_cited` exist under the folder; their mtimes match Atlas; `language` agrees with `exts`; exact duplicates found by sha256 against `files_*.tsv`, not by the model |
| Later (needs node-a) | for `state_guess = working`, a SLURM job runs the entry point on a scratch copy and records the exit code; "it runs" means exit code 0 |

The end product is one table, one row per folder: the verified facts, the model's `state_guess`, and a verdict column for you (`rebuild`, `archive`, `quarantine candidate`, `needs you`). Rows you mark `rebuild` go to `hk rebuild-all` (process 11).

### Kinds after that

| Kind | Worker | Check |
|---|---|---|
| `site-check` | node-c fetches on a schedule; `reader` summarizes only the diff | HTTP status and content sha256 |
| `api-pull` (FRED first) | node-c | row count and date range |
| `audit-check` | `atlas-queries provenance-check` | the query comes back clean |

## Config (`[tasks]` in `config.toml`, every key required)

```toml
[tasks]
log = "logs/tasks.tsv"
data_dir = "/home/radar-41-0/data/housekeeping/tasks"
inventory_dir = "/home/radar-41-0/data/housekeeping/inventory"
ns = "prod"
card_max_chars = 1200
max_attempts = 2
poll_interval_s = 10
request_timeout_s = 180
router_url = "http://127.0.0.1:8000"

[tasks.memgraph]
uri = "bolt://127.0.0.1:7687"
user_env = "MEMGRAPH_USER"          # empty or unset means no auth, as now
password_env = "MEMGRAPH_PASSWORD"
```

## Rules this inherits from STANDARDS.md

- Every `hk tasks` command that writes is a dry run until `--apply`. The dry run prints what would be proposed, sent, or moved.
- One log line per transition, append only.
- Idempotent: the task id is a hash of kind plus input, so proposing the same task twice changes nothing and says so. Dispatching with nothing ready says so.
- `make test` runs without Memgraph, the router, or SLURM.
- No em dashes.

## Build order

1. Ledger in Memgraph and the dispatcher, with one kind (`code-card`), router only. Handoff: `specs/12-agent-loop-step1-handoff.md`.
2. The `code-card` check, run in-process first and then as a node-c SLURM job.
3. The planner prompt: it writes proposals and reads cards, nothing else.
4. SLURM arrays for bulk work (render from `templates/slurm/array.sbatch`).
5. mem0.
6. The second kind.

## Open questions for you

- Should a failed task retry automatically (`max_attempts = 2`) or always stop for you?
- When node-a and node-b come back, keep `qwen3.5:9b` for both roles or move `coder` to `deepseek-coder-v2:lite` (process 3)?
