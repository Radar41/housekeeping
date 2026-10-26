# 11. Rebuild every pipeline as a repo

Process 5 for the whole system. The machine picks the candidates, works out the exclude rules, rebuilds each one, drafts the README and runs the checker. You come back to one review table, then push. Nothing is pushed until you run `push --apply`. The old folders are only read.

## What it does per candidate

1. Candidates come from the newest process 2 table (`~/data/housekeeping/inventory/dirs_*.tsv`). Kept: your own folders that look like a project (a marker file, a README, or 3+ code files) with 3 to 400 code files in their tree. Skipped: hidden folders, `R`, `pytorch`, `snap`, `go`, `projects`, conda, ventoy, `upstream`, `node_modules`, `site-packages`, `vendor`, `third_party`, `external`, saved web pages (`*_files`). Copies of one project are collapsed into the newest one, preferring paths that do not look like a backup or transfer. Folders with more than 400 code files are split into their parts and listed in `split_containers.tsv`.
2. Exclude rules, in order: the generic rules in `scripts/rebuild-batch-exclude.txt`, then any folder with 300+ files and no code, then up to 15 rules proposed by the local model (`reader`/`coder` through the router). Each rule is tried with rsync and kept only if it drops no code, config or doc file; rejected rules are listed in the report. If the router is down the first two still apply.
3. The rules go to `~/data/housekeeping/rebuilds/<name>.exclude`. Candidates still over 3000 files or 150 MB stop as `too_big` for you to look at.
4. `scripts/rebuild-pipeline.sh <source> <name> --exclude-from <file> --apply` (process 5, same guards: protected paths refused, existing repos never touched).
5. The local model writes "What it is" from the old README and the first lines of the code. State is `partial` if anything changed in the last 90 days, else `parked`. One commit, then the checker.

Results go to `state.jsonl` in the run folder. A rerun skips finished candidates, so a stopped run picks up where it left off.

## Steps

1. Update and plan. Read the count and the list.
   ```bash
   cd ~/projects/housekeeping && git switch main && git pull --ff-only
   uv run hk rebuild-all plan --show 60
   RUN=$(readlink -f ~/data/housekeeping/batch/latest)
   ```
   Drop any candidate you do not want by deleting its line in `$RUN/candidates.tsv`.
2. Dry run: rules and sizes only, no repos.
   ```bash
   uv run hk rebuild-all run "$RUN" --limit 4
   uv run hk rebuild-all run "$RUN" --apply --limit 4      # trial: 4 real repos
   less "$RUN/report.md"
   ```
3. The rest in the background. It keeps running after you log out.
   ```bash
   systemd-run --user --unit hk-rebuild-all --working-directory="$HOME/projects/housekeeping" \
     uv run hk rebuild-all run "$RUN" --apply
   journalctl --user -u hk-rebuild-all -f      # Ctrl-C stops watching, not the run
   ```
4. Review `$RUN/review.tsv`. `push` is `yes` only for repos that were built, pass every check, and have a drafted README. Change any row to `yes` or `no`. Your edits are kept when the report is rewritten.
5. Push.
   ```bash
   uv run hk rebuild-all push "$RUN"            # prints the gh commands
   uv run hk rebuild-all push "$RUN" --apply    # creates private repos under Radar41
   ```

## Statuses

| Status | Meaning |
|---|---|
| `built` | repo made in `~/projects/<name>`; see checks |
| `already_built` | a repo from the same source exists; checks rerun |
| `name_taken` | `~/projects/<name>` exists from another source; rename the line in `candidates.tsv` and rerun |
| `too_big` | over the file or size limit after the rules; add rules to its `.exclude` file and run process 5 by hand |
| `refused` / `rebuild_failed` | the rebuild script stopped; the message is in the report |
| `no_code` | the source folder is gone or nothing is left after the rules |
| `error` | an unexpected failure; retried on the next run |
| `planned` | dry run only; the `--apply` run builds it |
