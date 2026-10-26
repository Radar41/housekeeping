# housekeeping

## What it is
Tools for the radar-41 housekeeping plan: Atlas queries, mover with undo, repo checker, rclone intake, templates, role router, agent jobs, API, UI.

## State
partial

## How to run
make run, make test, make slurm (added as modules land)
`uv run hk checker <repo>...` or `uv run hk checker --all <dir>` checks repos against STANDARDS.md (add `--json`)
- `make run MOD=mover ARGS="plan table.csv"`; `apply table.csv --apply [--push]`; `undo logs/moves.tsv [--since ISO] --apply`. Dry-run unless --apply.
- `make run MOD=atlas-queries ARGS="<provenance-check|inbox-three-way|project-table> [--root-id N] [--format tsv|csv] [--out FILE --apply]"` runs a read-only Atlas query using the DSN in `$ATLAS_DSN`.

## Depends on
Atlas (Postgres/pgvector on head), SLURM, Python 3.12 with uv
