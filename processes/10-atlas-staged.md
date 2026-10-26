# Atlas metadata-first ingestion

This replaces the per-GPU directory walks with one metadata inventory, a set-based selection pass, a CPU preparation queue and a shared GPU embedding queue. Existing Atlas documents are never overwritten. The original ingest file, launcher, checkpoints and embedding tables are not patched by installation.

Implementation branch: `hk/atlas-staged`. These scripts were tested in a sandbox, including a real temporary PostgreSQL server with fixture Atlas functions. They have not been run on Radar-41 or verified against the complete installed Atlas module. The live compatibility check is mandatory; a passing syntax test alone is not approval to write canonical data.

## What was used to design this

The design incorporates the detailed October 26 session, the preceding local inference history, the housekeeping status attachment, the current housekeeping repository and the ingest/schema excerpts supplied in the conversation. Important constraints are root 21, the current credential excludes, the existing in-process Atlas embedder, shared Atlas storage over the two direct-link NFS mounts, PostgreSQL on head and the three established GPU allocations.

The whole Atlas ingest source is not in the connected GitHub repositories. Therefore the adapter reads the installed module on head, discovers its configuration factory and model class, and checks the extraction and writer interfaces before use. It refuses unsupported interfaces rather than guessing table writes. `doctor` reports signatures and aggregate compatibility information without exporting function bodies or corpus text.

## Architecture

| Stage | Placement | Work |
|---|---|---|
| Inventory | Head, one scanner | Directory enumeration and stat metadata, no file-content reads |
| Selection | PostgreSQL | Join inventory against existing Atlas paths; preserve existing records; retain explicit skip/review reasons |
| Preparation | Initially four CPU workers on head | Native Atlas structured sampling, extraction, fingerprinting and chunking |
| Embedding | One Slurm process per GPU | Consume prepared chunks from the same queue, without filesystem traversal |
| Publication | Head, one writer | Insert new documents/chunks/vectors transactionally using the native Atlas helpers |

Metadata is stored as queryable rows, not first converted into vectors. Existing path/name/extension policy is deterministic. Structured summaries are prepared on CPUs and then their text is embedded by the GPUs; no additional LLM rewriting, OCR model or new embedding space is introduced.

The initial GPU workers all use the existing embedding backend and model. This avoids changing retrieval semantics. They are not tied to directory shards: whichever GPU is free takes another batch. CPU preparation supports `--lane structured` and `--lane text`, but the default pool lets every preparer take either lane to avoid stranding capacity.

GPU batching groups similar character lengths and uses the native embedder, starting at the historically used batch sizes of 8 on head and 4 on each worker. These are starting settings, not proven optima; throughput depends on model, hardware and input lengths ([Sentence Transformers efficiency documentation](https://sbert.net/docs/sentence_transformer/usage/efficiency.html)).

## Guarantees and deliberate boundaries

- **Preserve existing work:** all existing indexed paths are excluded before content preparation, even if their current content may have changed. This is an explicit preservation policy, not a claim that size and timestamp prove identical content. Existing non-indexed rows go to review rather than being overwritten.
- **Preserve sampling semantics:** structured summaries retain the native `metadata_fingerprint`; raw-content hashes and summary fingerprints are not compared as though they were interchangeable. The earlier `.mat` mismatch test did not prove those files unchanged.
- **Avoid repeated inference:** identical prepared text chunks share one embedding task within a run. Each file still gets its own ordered chunks and lineage. This does not delete duplicate documents or merge historical revisions.
- **Resume safely:** one transaction owns one directory, preparation item or GPU batch. Completed units persist; an interrupted transaction rolls back. Row locking with `SKIP LOCKED` allows independent workers to claim queue items; PostgreSQL explicitly describes this as suitable for queue-like consumers ([PostgreSQL SELECT documentation](https://www.postgresql.org/docs/16/sql-select.html)).
- **Bound the backlog:** preparers stop when 20,000 unembedded chunks are waiting, with at most one file of overshoot per preparer. File preparation has a 512 MiB safety cap; larger files go to review, not silent loss. These are tunable safety settings, not replacements for Atlas's own type/size policies.
- **Keep data local:** prepared text stays in `hk_stage` on the existing database. Vectors are staged as packed little-endian float32, not JSON arrays. Logs and status contain counts, timings and error classes, not file contents.
- **Record omissions:** policy-pruned directory boundaries are recorded, but their descendants are not individually inventoried. This is an eligible-scope inventory, not a forensic census of secret folders or every excluded subtree. Symlinks are not followed; other filesystems are not descended into.
- **Freeze semantics per run:** source hashes, embedding configuration, exclusions and extraction thresholds form a run contract. A restarted worker with different source/configuration fails closed. New policies require a new staging run; already indexed paths still remain preserved.
- **Protect publication:** a publisher rechecks file metadata and existing canonical records. New documents and all their chunks/vectors commit together. A brief table lock prevents an uncoordinated legacy writer from racing the existence check. Use one publisher, and stop legacy ingestion before publication.
- **Separate completion from cleanliness:** `complete` means no queued work remains. `needs_review` reports errors or review rows; terminal errors are never treated as successful indexing.

This release does not reconcile or delete old secrets, re-embed old records, change the retrieval model, add filesystem watchers, or automatically arbitrate Ollama versus Slurm. It also does not claim a five-minute inventory or a measured cluster speedup.

## Install a separate shared checkout

Do this on head. A shared checkout avoids switching your active housekeeping/router branch and makes the same scripts available on the workers.

```bash
set -e
DEST=/srv/warm/cluster/atlas/tools/housekeeping-staged
mkdir -p "$(dirname "$DEST")"
git clone --branch hk/atlas-staged https://github.com/Radar41/housekeeping.git "$DEST"
cd "$DEST"
./scripts/atlas-staged doctor
```

`doctor` must finish with `"compatible": true`. It imports the installed Atlas module but does not instantiate the model, create staging tables, alter canonical tables or start jobs. A failure means stop at that boundary; the script does not fall back to approximate logic. Its JSON report is the one useful thing to return for adaptation.

If this checkout already exists, do not clone over it. Check its local modifications, then fetch and fast-forward the same branch. Do not update it while a pool is active.

The scripts use `/srv/warm/cluster/atlas/.venv/bin/python`, not system Python and not a fresh environment. They load their package directly from this checkout. No `pip install`, model download, credential rotation or sudo operation is performed by the scripts.

## Inventory and selection

Keep the existing protected-names file outside this repository. The example uses the path already configured by the housekeeping setup; use the actual configured path if different.

```bash
cd /srv/warm/cluster/atlas/tools/housekeeping-staged
./scripts/atlas-staged-inventory \
  /home/radar-41-0/projects/housekeeping/protected_names.txt
```

This runs the compatibility check, creates only additive `hk_stage` tables, records a new run ID, scans metadata, and plans the work. The run starts paused. It does not cancel legacy jobs, submit GPUs, read content for extraction or write canonical Atlas documents.

The scan can overlap a legacy run because planning and later publication recheck canonical existence. To avoid disk contention, stop the specific legacy slices first if you want the fastest inventory. List them with `squeue -u "$USER" -o '%i %j %T %N %r'`; cancel only reviewed current IDs, not remembered IDs or every job you own.

For a smaller first inventory, append `--start-rel RELATIVE_DIRECTORY` to the inventory command. Relative paths are resolved against root 21; no source file edits are needed.

```bash
RUN=$(cat /srv/warm/cluster/atlas/_run/staged/latest-run)
./scripts/atlas-staged status "$RUN"
```

If interrupted, resume the same inventory rather than create another:

```bash
./scripts/atlas-staged scan "$RUN"
./scripts/atlas-staged plan "$RUN"
```

`scan` checkpoints complete directories. A killed scan repeats only its uncommitted directory. Scan errors remain visible in status; the plan may proceed over successfully scanned directories but is not a claim of complete coverage.

## Run the preparation and GPU pool

Before submission, stop the specific old embedding jobs and release GPU memory used by other processes. This launcher never stops or unloads Ollama for you, and it refuses to submit if `nvidia-smi` reports compute processes on any of the three nodes.

```bash
cd /srv/warm/cluster/atlas/tools/housekeeping-staged
RUN=$(cat /srv/warm/cluster/atlas/_run/staged/latest-run)
./scripts/atlas-staged-submit "$RUN" --apply
./scripts/atlas-staged resume "$RUN"
./scripts/atlas-staged status "$RUN"
```

The submitter creates four CPU preparation jobs on head and one embedding job on each GPU. It uses the existing head/local and direct-link worker DSNs with the already-installed `.pgpass`, never credentials on the command line. Slurm resource values follow the previous launcher: partition `main`, the established long GRES names, 32 GiB for head embedding, 12 GiB for each worker, and a 48-hour time limit. Confirm those allocations remain appropriate for current cluster policy.

Job IDs and logs are under `_run/staged/RUN_ID/`. Successful IDs are recorded immediately; the submitter refuses duplicate pools. If a later submission fails, earlier submissions may remain queued, but the run stays paused until you explicitly resume it. Inspect `jobs.tsv` and `squeue` before any recovery.

No publisher is launched by this command. CPU workers exit when preparation drains; GPUs exit when the prepared embedding queue drains, even if publication is not yet complete. No GPUs remain allocated just to wait for the writer.

## Publish only new documents

First inspect status, test a single publication, then continue if that succeeds. The existing Atlas adapter and schema must pass the live check; publication uses the original helpers and is independently transactional per document.

```bash
./scripts/atlas-staged status "$RUN"
./scripts/atlas-staged publish "$RUN" --limit 1 --apply
./scripts/atlas-staged status "$RUN"
./scripts/atlas-staged publish "$RUN" --apply --watch
```

Publication can run alongside preparation and embedding; it selects only files whose vectors are all complete. If you prefer inspecting the entire staged result first, wait for embedding to finish before launching publication. Neither mode rewrites already-existing Atlas documents.

The record of inserted document IDs is in `hk_stage.event` with `kind='publish'`. Do not delete those canonical rows as a general rollback; any removal needs its own reviewed operation.

## Pause, resume and failures

```bash
./scripts/atlas-staged pause "$RUN"
./scripts/atlas-staged status "$RUN"
./scripts/atlas-staged resume "$RUN"
```

Pause prevents claiming new preparation/embedding/publication work; a current transaction may finish. It is not a GPU unload command. To give a GPU to the reader/coder, pause the run and hold or cancel the specific embedding job using its recorded ID. Stop or requeue only that process; finished vectors remain in PostgreSQL.

The older scheduler PR is not automatically enabled here. Its fail-open router gate is not a sufficient GPU exclusivity guarantee, and Ollama lives outside Slurm. Do not run it as though this branch had validated or repaired that integration.

After an embedding error such as OOM, the batch is still pending. Restart that worker with a smaller `--batch`; do not clear state or mark it done. The CLI prints the failure category, not file text. Preparation errors and changed files go to `error` or `review`; no automated retry loop repeatedly reads a broken file.

Stopping the new jobs is enough to return to the legacy launcher. The old script and shard checkpoints are unchanged. New documents already published remain in Atlas, and the legacy ingest should encounter them through its normal existing-row path.

## Measuring whether it helped

Use `status RUN_ID` plus the per-job Slurm logs. Stage events record scanned files, prepared chunks, embedded unique chunks, published documents and worker elapsed seconds. Parallel worker time is not wall time: do not divide total files by summed worker seconds and call it cluster throughput.

Compare over the same wall-clock interval:

- **Ready-file backlog growing:** preparation is lagging or constrained by its size cap.
- **Pending-vector backlog growing:** GPUs are lagging, busy elsewhere, or under-batched.
- **Prepared files with completed vectors accumulating:** publication is lagging or not started.
- **GPUs idle while ready files remain:** inspect preparation jobs, source-contract errors, memory limits and extraction failures.
- **No work queued but review/errors remain:** ingestion is terminal for this run, not a clean corpus.

Do not tune model precision, pooling, token limits or embedding normalization just for a faster rate; those affect the embedding contract. This implementation changes scheduling and redundant work first.

## Verification scope

Tests cover the PostgreSQL queue lifecycle, no reads of existing indexed files, protected paths, noise pruning, symlink rejection, changed-file detection, unique chunk tasks, multiple preparers, pause/backpressure, source/exclude changes, restartable scans, failed embedding rollback and failed canonical publication rollback. Adapter tests cover raw versus structured fingerprints, broken summaries, invalid vectors, unknown writer signatures and sanitized diagnostics.

The database tests use a real PostgreSQL instance but fixture Atlas writer/model functions and JSONB in the fixture's canonical embedding table. They are not evidence of successful live pgvector publication, Qwen inference, or Slurm execution on Radar-41. Those are the on-head acceptance checks above.

Sandbox result on this branch: 139 tests passed, 22 existing tests skipped; 21 tests cover the new staged implementation. The baseline without this change is 118 passed and 22 skipped. The separate scheduler PR's tests are not part of this branch.

```bash
uv run --python 3.12 --with pgserver pytest -q tests/staged
uv run --python 3.12 --with pgserver pytest -q
```
