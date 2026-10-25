# Standards every module must meet

- One README.md with four headings only: What it is, State (working, partial, parked), How to run, Depends on. No history section.
- One entry point: make run, make test, make slurm. Anything else is internal.
- Dry-run on every script that moves, writes, or deletes. Default is dry-run; --apply is explicit.
- Every action logged as one line: ISO timestamp, action, source, target, identifier (chain id, job id, or sha256). Logs append, never rewrite.
- Undo for every move. The log is the undo input.
- Config in one file, read once. No hardcoded paths, ports, hosts, or model names.
- Secrets only in .env, gitignored, with .env.example committed.
- Idempotent. A second run with the same inputs changes nothing and says so.
- Fixtures in tests/ so make test runs without radar-41.
- No em dashes anywhere, code comments included.
- Nothing from ~/protected is referenced, imported, or listed. The checker greps for protected names and fails on a match.
- Python 3.12, uv, pyproject.toml. Node only for the React app. Bash only for wrappers under 40 lines.
