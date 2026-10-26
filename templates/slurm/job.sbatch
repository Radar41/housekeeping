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

# HEARTBEAT (matches heartbeat/validator_heartbeat.py and the /api/runs reader):
# 1. /api/runs reads the last JSON line containing "seen" from <log root>/<dir>/<name>-<jobid>-<task>.log
#    and takes age_sec from that file's mtime, so each call appends one line to such a file.
# 2. ValidatorHeartbeat(job_id, rank): announce() on the first call, beat(force=True) after.
#    It has no state field; running/ok/failed travel only in the log line.
hb() {
  local state="$1" first=0 root f n
  root="${SLURM_LOG_ROOT:-{{repo_dir}}/_run/slurm}"
  f="$root/${SLURM_JOB_NAME}/${SLURM_JOB_NAME}-${SLURM_JOB_ID}-0.log"
  mkdir -p "$(dirname "$f")" 2>/dev/null || return 0
  [ -e "$f" ] || first=1
  n=$(cat "$f" 2>/dev/null | wc -l)
  printf '{"seen": %s, "state": "%s", "ts": %s}\n' "$((n + 1))" "$state" "$(date +%s)" >> "$f" || true
  PYTHONPATH="$(dirname "{{heartbeat_script}}")" python3 -c 'import os, sys
from validator_heartbeat import ValidatorHeartbeat
h = ValidatorHeartbeat(job_id=os.environ["SLURM_JOB_ID"], rank=int(os.environ.get("SLURM_PROCID", "0")))
h.announce() if sys.argv[1] == "1" else h.beat(force=True)' "$first" 2>/dev/null || true
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
