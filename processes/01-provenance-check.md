# 1. Check Atlas provenance

Run once now, before anything else builds on Atlas. Run again after any large sweep. Read-only: the tool opens the database in read-only mode and refuses anything that is not a `SELECT`.

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
4. Save both with today's date so the next run can be compared.
   ```bash
   mkdir -p ~/data/housekeeping/provenance
   D=$(date -u +%F)
   make run MOD=atlas-queries ARGS="provenance-check --out $HOME/data/housekeeping/provenance/check_$D.tsv --apply"
   make run MOD=atlas-queries ARGS="provenance-payload-keys --out $HOME/data/housekeeping/provenance/keys_$D.tsv --apply"
   ```

## How to read it

| You see | It means |
|---|---|
| `gap_in_july_2026 = t` on the sweep action | the July gap is real; compare with the July 30 notes in `/srv/warm/cluster/atlas` |
| an action like `move` or `rename`, or keys like `from_path` and `to_path` | moves are tracked apart from edits |
| only `ingest` or `update` with document ids | Atlas cannot tell a move from an edit; the mover's `logs/moves.tsv` is the move record from now on |

## Done when

Both files from step 4 exist and you have written the three answers (July gap, moves tracked, last event date) at the top of `~/data/housekeeping/provenance/NOTES.md`.
