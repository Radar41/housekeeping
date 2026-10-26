# housekeeping

## What it is
The repeatable processes for running radar-41: checking Atlas provenance, building the metadata inventory of every pipeline, bringing in the Windows drives, checking repos, setting up the local coders, routing work to them, and rebuilding old pipelines as clean repos. Each process is one page in `processes/` with numbered terminal steps. `INSTRUCTIONS.md` is the full list in the order you run them.

## State
partial

## How to run
Start with `INSTRUCTIONS.md`. It lists every process in order, says which are one-time and which repeat, and links to the page with the exact commands.
Every command is a dry run unless you add `--apply`. Every run writes a line to `logs/`.
`make test` runs the test suite. `make run MOD=<tool> ARGS="..."` runs one tool.

## Depends on
Atlas (Postgres and pgvector on head), SLURM, Python 3.12 with uv, rclone, Ollama on each node
