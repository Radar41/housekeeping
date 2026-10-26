#!/usr/bin/env bash
# Template: single SLURM job with heartbeat (start, interval update, trap on exit).
# placeholders: job_name, partition, gres, time, mem, cpus, repo_dir, heartbeat_script, hb_interval, command
#   gres              e.g. gpu:1
#   time              e.g. 04:00:00
#   hb_interval       seconds between beats; keep well under 120 (amber at 2 min, red at 5 min)
#   heartbeat_script  path to heartbeat/validator_heartbeat.py (see HEARTBEAT INTERFACE below)
#SBATCH --job-name={{job_name}}
#SBATCH --partition={{partition}}
#SBATCH --gres={{gres}}
#SBATCH --time={{time}}
#SBATCH --mem={{mem}}
#SBATCH --cpus-per-task={{cpus}}
#SBATCH --chdir={{repo_dir}}
#SBATCH --output={{repo_dir}}/logs/%x-%j.out
# The logs/ directory must exist at submit time: `make slurm` creates it.

set -uo pipefail

# HEARTBEAT INTERFACE (UNVERIFIED, adjust only this function):
# validator_heartbeat.py was not available when this template was written.
# Assumed call: <script> --run-id ID --state running|ok|failed
hb() {
  uv run python "{{heartbeat_script}}" --run-id "${SLURM_JOB_NAME}-${SLURM_JOB_ID}" --state "$1" || true
}

beat_loop() {
  while sleep "{{hb_interval}}"; do hb running; done
}

hb running
beat_loop &
BEAT_PID=$!

finish() {
  rc=$?
  kill "$BEAT_PID" 2>/dev/null
  wait "$BEAT_PID" 2>/dev/null
  if [ "$rc" -eq 0 ]; then hb ok; else hb failed; fi
  exit "$rc"
}
trap finish EXIT
trap 'exit 143' TERM INT

{{command}}
