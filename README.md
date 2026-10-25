# housekeeping

## What it is
Tools for the radar-41 housekeeping plan: Atlas queries, mover with undo, repo checker, rclone intake, templates, role router, agent jobs, API, UI.

## State
partial

## How to run
make run, make test, make slurm (added as modules land)
- `make run MOD=mover ARGS="plan table.csv"`; `apply table.csv --apply [--push]`; `undo logs/moves.tsv [--since ISO] --apply`. Dry-run unless --apply.

## Depends on
Atlas (Postgres/pgvector on head), SLURM, Python 3.12 with uv
