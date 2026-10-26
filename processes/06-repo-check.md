# 6. Check repos

Repeat after every rebuild, and weekly across all of `~/projects`. Read-only. Checks each repo against `STANDARDS.md`: git, README headings and state, Python 3.12 with `uv.lock`, Makefile `run`/`test`/`slurm`, `.env` handling, secrets, em dashes, protected names.

## Steps

1. One repo, or several:
   ```bash
   cd ~/projects/housekeeping
   uv run hk checker ~/projects/<name>
   ```
2. Every repo under a folder, saved for comparison:
   ```bash
   mkdir -p ~/data/housekeeping/checks
   uv run hk checker --all ~/projects | tee ~/data/housekeeping/checks/$(date -u +%F).tsv | grep FAIL
   ```
3. For scripts: add `--json`.

## Done when

`grep FAIL` prints nothing, or each FAIL has a reason written next to the repo in your notes.
`protected ... protected names file not found` means process 0 step 5 is not done.
