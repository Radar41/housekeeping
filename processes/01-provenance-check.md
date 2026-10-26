# 1. Check Atlas provenance

Run once now, before anything else builds on Atlas. Run again after any large sweep. Steps 2 to 5 are read-only: the tool opens the database in read-only mode and refuses anything that is not a `SELECT`.

## Steps

1. Load the DSN (see step 0).
   ```bash
   cd ~/projects/housekeeping && set -a; . ./.env; set +a
   ```
2. Counts and date ranges per action, with week-by-week gaps.
   ```bash
   make run MOD=atlas-queries ARGS="provenance-check"
   ```
3. Payload keys per action. This shows whether moves are logged apart from edits.
   ```bash
   make run MOD=atlas-queries ARGS="provenance-payload-keys"
   ```
4. Ingest runs. Atlas logs ingest per run, not per file, so this is the whole ingest history.
   ```bash
   uv run hk atlas-queries ingest-runs | cut -f1,3,5,7-11 | column -t -s $'\t'
   ```
5. How stale the index is. The database cannot know about files written after the last ingest, so this reads the disk (read-only). Use the `root_path` of root 21 and the last ingest time from step 4.
   ```bash
   LAST="2026-08-11 16:34"     # last ingest from step 4
   find "$HOME" -xdev -type f -newermt "$LAST" -not -path '*/.*' 2>/dev/null | wc -l
   ```
6. Check that the ingest timer actually succeeds. A timer that fires but fails writes nothing to Atlas, so steps 2 to 4 only show it as silence.
   ```bash
   systemctl --user list-timers --all --no-pager | grep -iE 'atlas|ingest|parity'
   systemctl --user show atlas-vector-ingest.service -p Result -p ExecMainStatus -p ExecMainExitTimestamp
   journalctl --user -u atlas-vector-ingest.service -n 5 --no-pager
   ```
   `Result=success` is healthy. Anything else: read the journal line before changing anything.
7. Save all three with today's date so the next run can be compared.
   ```bash
   mkdir -p ~/data/housekeeping/provenance
   D=$(date -u +%F); P=$HOME/data/housekeeping/provenance
   make run MOD=atlas-queries ARGS="provenance-check --out $P/check_$D.tsv --apply"
   make run MOD=atlas-queries ARGS="provenance-payload-keys --out $P/keys_$D.tsv --apply"
   make run MOD=atlas-queries ARGS="ingest-runs --out $P/ingest_$D.tsv --apply"
   ```

## Refresh the index (writes to Atlas)

When step 5 shows many new files or step 6 shows a failing timer. Use the Atlas launcher; it runs a preflight, a 50-file dry smoke, waits for you to type START, then submits one Slurm job pinned to head (home is head-local, so this uses head's GPU only).

1. Credential folders out first. Add to the root's excludes anything that holds keys (patterns match at any depth, like `.venv/`):
   ```bash
   psql -d atlas -Atc "select x from atlas.atlas_root, jsonb_array_elements_text(excludes) x where root_id=21 order by 1"
   ```
2. Free head's GPU and run.
   ```bash
   systemctl --user stop hk-router; sudo systemctl stop ollama
   cd /srv/warm/cluster/atlas
   ./bin/atlas-home-topdown preflight          # must end in PASS
   ATLAS_HOME_SLURM_TIME=48:00:00 ./bin/atlas-home-topdown go
   squeue -u $USER; ./bin/atlas-home-topdown status
   ```
   If the job runs out of time: `./bin/atlas-home-topdown submit-resume` continues from the checkpoint.
3. After it finishes: `sudo systemctl start ollama; systemctl --user start hk-router`, then rerun steps 4 and 7 here.

## Secret scan

Paths and match kinds only, never the text. Save it on head; do not paste the file anywhere.
```bash
make run MOD=atlas-queries ARGS="secret-scan --out $HOME/data/housekeeping/provenance/secret_scan_$(date -u +%F).tsv --apply"
cut -f3 ~/data/housekeeping/provenance/secret_scan_$(date -u +%F).tsv | tr ',' '\n' | sort | uniq -c
```
Any real key it finds: rotate it at the provider first, then remove the document from Atlas.

## How to read it

| You see | It means |
|---|---|
| `gap_in_july_2026 = t` on the sweep action | the July gap is real; compare with the July 30 notes in `/srv/warm/cluster/atlas` |
| an action like `move` or `rename`, or keys like `from_path` and `to_path` | moves are tracked apart from edits |
| only `ingest` or `update` with document ids | Atlas cannot tell a move from an edit; the mover's `logs/moves.tsv` is the move record from now on |
| ingest payload keys are only `backend`, `counts`, `run_id` | same: ingest is logged per run, with no per-file history |
| an ingest run with `unchanged` near the document count | incremental run; files changed during an earlier gap were picked up here |
| an ingest run with `indexed = 0` and `failed > 0` | the run started and did nothing |
| `endpoint` empty, `kind = sentence_transformers` | Atlas embeds in-process; there is no embedding server for the router to call |
| `Result=exit-code`, `status=3`, "internal wrapper" in the journal | the timer calls the old ingest entry point, which now refuses direct starts; nothing has been ingested since the last manual run |
| many files in step 5 | the inventory in process 2 misses them until the next ingest |

Result on radar-41, 2026-10-26: July gap real (no ingest Jul 14 to Jul 30, two failed runs Jul 31, parity at about a third of its hourly rate); covered content-wise by the incremental run on Aug 11 (6,082 indexed, 208,634 unchanged); moves not tracked; last ingest Aug 11. Cause: `atlas-vector-ingest.timer` has exited with status 3 every 15 minutes since then ("internal wrapper, use ./bin/atlas-epoch-ingest"). 919,309 files in home are newer than the last ingest.

## Done when

The three files from step 7 exist and you have written the three answers (July gap, moves tracked, last event date) at the top of `~/data/housekeeping/provenance/NOTES.md`.
