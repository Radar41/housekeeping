# Instructions

Run these in order on head (`radar-41-0@head`) unless a step says otherwise. Each line links to a page in `processes/` with the exact commands.
One-time steps are done once per machine. Repeat steps are the ones you come back to.
Nothing here moves or deletes a file. Every tool is a dry run until you add `--apply`.

## Order

| # | Process | When | What you get |
|---|---|---|---|
| 0 | [Set up this repo](processes/00-setup.md) | one-time | `config.toml`, `ATLAS_DSN`, the protected names file, a passing `make test` |
| 1 | [Check Atlas provenance](processes/01-provenance-check.md) | one-time, then after any big sweep | per-action counts, the July gap answer, whether moves are tracked apart from edits |
| 2 | [Build the metadata inventory](processes/02-metadata-inventory.md) | repeat weekly, or after a sweep | every code file with its header text and project tag, and every folder that looks like a pipeline |
| 3 | [Set up the local coders](processes/03-local-coders.md) | one-time per node | Ollama at 64k context, OpenCode, Rust, Superset; Codex, Claude Code, Mission Control optional |
| 4 | [Start the role router](processes/04-router.md) | one-time, then check health | one endpoint on head that sends `reader` and `coder` work to the right node |
| 5 | [Rebuild a pipeline as a repo](processes/05-rebuild-pipeline.md) | repeat, one pipeline at a time | a new private GitHub repo in `~/projects` that passes the checker; the original folder untouched |
| 6 | [Check repos](processes/06-repo-check.md) | repeat after every rebuild, and weekly | PASS or FAIL per repo against `STANDARDS.md` |
| 7 | [Bring in the Windows drives](processes/07-intake.md) | one-time per machine, then check | a copy under `~/inbox/<machine>/<drive>/` and a `CHECK.txt` |
| 8 | [Move files](processes/08-moves.md) | not yet | on hold until you decide to file things |
| 10 | [Atlas metadata-first ingestion](processes/10-atlas-staged.md) | staged replacement | one inventory, CPU preparation and shared GPU queues; existing embeddings preserved |

## Daily loop, once 0 to 4 are done

```bash
cd ~/projects/housekeeping && git pull
uv run hk router health                                         # all roles up?
make run MOD=atlas-queries ARGS="pipeline-dirs"                 # what pipelines exist
# pick one, rebuild it: processes/05-rebuild-pipeline.md
uv run hk checker ~/projects/<new-repo>                         # passes?
```

## Not built yet

These are planned and have no commands yet. Their pages get written when the code lands.
- Agent jobs that store what they did: a shared job skeleton, a jobs log, and three daily jobs (intake summary, duplicate review, session report).
- Local API on head: `/api/runs`, `/api/moves`, `/api/projects`, `/api/repos`.
- UI on top of the API.
