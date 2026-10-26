# 5. Rebuild a pipeline as a repo

Repeat for each old pipeline you want back in use. One pipeline at a time. The old folder is only read; it stays where it is until you decide to file things (process 8).

The script copies code, configs, and docs into `~/projects/<name>`, skips environments, caches, data, weights, and anything over 5 MB (rules in `scripts/rebuild-exclude.txt`), adds the standard files, makes the first commit, and runs the checker. It does not push.

## Steps

1. Pick a folder from the inventory (process 2, step 4). Note its full path: `root_path` + `/` + `dir`.
2. Dry run. Shows the file list and how many files are skipped for size.
   ```bash
   cd ~/projects/housekeeping
   scripts/rebuild-pipeline.sh "/home/radar-41-0/<dir>" <name>
   ```
3. Run it. Pass the command `make run` should call if it is not `run.sh` or `main.py`.
   ```bash
   scripts/rebuild-pipeline.sh "/home/radar-41-0/<dir>" <name> --entry "python fit.py" --apply
   ```
4. Fix what the checker flags. Usual ones:

   | FAIL | Fix |
   |---|---|
   | `python` requires-python | edit `requires-python = ">=3.12"` in `pyproject.toml`, then `uv lock` |
   | `entry` | the old Makefile is kept as is; add `run`, `test`, and `slurm` targets (copy from `templates/Makefile`) |
   | `secrets` | move the value to `.env`, keep the name in `.env.example`, rewrite history before pushing |
   | `protected` | stop. Remove the file from the repo and from history; it does not go to GitHub |

   Re-check: `uv run hk checker ~/projects/<name>`
5. Write the README's "What it is" in two or three lines. The old README is `README.source.md`; the file headers from process 2 step 5 say what each script does.
6. If it runs on SLURM, render its job file.
   ```bash
   cd ~/projects/<name> && mkdir -p slurm
   python ~/projects/housekeeping/templates/render.py ~/projects/housekeeping/templates/slurm/job.sbatch \
     --set job_name=<name> partition=<partition> gres=gpu:1 time=02:00:00 mem=16G cpus=4 \
           repo_dir=$PWD slurm_log_root=/srv/warm/cluster/atlas/_run/slurm \
           heartbeat_script=$HOME/projects/housekeeping/heartbeat/validator_heartbeat.py hb_interval=30 \
           command="make run" \
     --out slurm/main.sbatch --apply
   make slurm JOB=main
   ```
   The heartbeat makes the run show up in the Atlas instrument's `/api/runs`.
7. Commit and push, private.
   ```bash
   git add -A && git commit -m "Make it run"
   gh repo create Radar41/<name> --private --source . --push
   ```

## What it leaves behind

| File | Holds |
|---|---|
| `SOURCES.txt` | source path, host, time, copy rules |
| `SOURCES.sha256` | hash of every copied file, to match back to Atlas by `sha256` |
| `~/data/housekeeping/rebuilds.tsv` | one line per rebuild: time, name, source, destination, file count, first commit |

Running the script again on the same source and name prints `unchanged`. A different source with an existing name is refused.

## Done when

The checker shows all PASS and the repo is on GitHub as private.
