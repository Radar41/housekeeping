# 2. Build the metadata inventory

Repeat weekly, or after a sweep. Read-only. Atlas already read every file; this pulls out what the files say about themselves and groups them into pipelines. Nothing is read from disk.

## What each query gives you

| Query | One row per | Key columns |
|---|---|---|
| `pipeline-dirs` | folder that holds code | `code_files`, `markers` (Makefile, pyproject.toml, *.sbatch ...), `exts`, `projects`, `newest_mtime`, `has_readme` |
| `pipeline-inventory` | canonical code file | `rel_path`, `ext`, `is_marker`, `project`, `sha256`, `mtime`, `header` (first 800 characters: shebang, docstring, leading comments; line breaks stored as `\n`) |
| `project-table` | version chain | project, canonical member, revision count |

Only canonical members of each version chain are listed, so old revisions do not show up twice.

Rows that touch protected material are dropped from every query's output (any `protected/` path segment, or any name in `protected_names.txt`), and the count is printed as "withheld N rows". `pipeline-inventory` returns file text, so it refuses to run until `protected_names.txt` exists. Atlas still indexes everything locally; this only keeps it out of what you paste elsewhere.

## Steps

1. Load the DSN.
   ```bash
   cd ~/projects/housekeeping && set -a; . ./.env; set +a
   ```
2. Look at the pipeline folders on screen. Add `--root-id 21` for home only; leave it off for every root.
   ```bash
   uv run hk atlas-queries pipeline-dirs --root-id 21 | column -t -s $'\t' | less -S
   ```
3. Save all three with today's date.
   ```bash
   mkdir -p ~/data/housekeeping/inventory
   D=$(date -u +%F); O=$HOME/data/housekeeping/inventory
   make run MOD=atlas-queries ARGS="pipeline-dirs --out $O/dirs_$D.tsv --apply"
   make run MOD=atlas-queries ARGS="pipeline-inventory --out $O/files_$D.tsv --apply"
   make run MOD=atlas-queries ARGS="project-table --out $O/projects_$D.tsv --apply"
   ```
4. Pick the folders worth rebuilding. A quick filter: folders with a marker file, sorted newest first.
   ```bash
   awk -F'\t' 'NR==1 || $5>0' $O/dirs_$D.tsv | (read -r h; echo "$h"; sort -t$'\t' -k9,9r) | column -t -s $'\t' | less -S
   ```
5. Read what a folder's files say about themselves.
   ```bash
   awk -F'\t' -v d="<dir from step 4>" '$5==d {print "== "$4; print $13}' $O/files_$D.tsv | sed 's/ \\n /\n/g' | less
   ```

## Done when

The three dated files exist under `~/data/housekeeping/inventory/`. They stay outside every repo because the header text can include protected material.

## Note

The `header` column is what Atlas stored as chunk 0. If a folder shows `header` empty for most files, that root was swept without chunking; re-run the sweep for that root before relying on this.
