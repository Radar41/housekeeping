#!/usr/bin/env bash
# Template: multi-stage pipeline as a SLURM array, one stage per task, with heartbeat.
# placeholders: job_name, partition, gres, time, mem, cpus, repo_dir, heartbeat_script, hb_interval, embed_cmd, read_cmd, code_cmd
#   Tasks 0,1,2 are embed, read, code. %1 runs them one at a time in order.
#   A stage refuses to start unless the previous stage left its marker, so a failure stops the chain
#   and sacct shows each stage as <jobid>_<n> with its own state.
#   Alternative without arrays: submit three jobs from job.sbatch chained with
#   --dependency=afterok:<previous jobid>.
#SBATCH --job-name={{job_name}}
#SBATCH --array=0-2%1
#SBATCH --partition={{partition}}
#SBATCH --gres={{gres}}
#SBATCH --time={{time}}
#SBATCH --mem={{mem}}
#SBATCH --cpus-per-task={{cpus}}
#SBATCH --chdir={{repo_dir}}
#SBATCH --output={{repo_dir}}/logs/%x-%A_%a.out

set -uo pipefail

STAGES=(embed read code)
STAGE="${STAGES[$SLURM_ARRAY_TASK_ID]}"
MARK_DIR="logs/.stages-${SLURM_ARRAY_JOB_ID}"
mkdir -p "$MARK_DIR"

# HEARTBEAT (matches heartbeat/validator_heartbeat.py and the /api/runs reader):
# 1. /api/runs reads the last JSON line containing "seen" from <log root>/<dir>/<name>-<jobid>-<task>.log
#    and takes age_sec from that file's mtime, so each call appends one line to such a file.
# 2. ValidatorHeartbeat(job_id, rank): announce() on the first call, beat(force=True) after.
#    It has no state field; running/ok/failed travel only in the log line.
hb() {
  local state="$1" first=0 root f n
  root="${SLURM_LOG_ROOT:-{{repo_dir}}/_run/slurm}"
  f="$root/${SLURM_JOB_NAME}/${SLURM_JOB_NAME}-${SLURM_ARRAY_JOB_ID}-${SLURM_ARRAY_TASK_ID}.log"
  mkdir -p "$(dirname "$f")" 2>/dev/null || return 0
  [ -e "$f" ] || first=1
  n=$(cat "$f" 2>/dev/null | wc -l)
  printf '{"seen": %s, "state": "%s", "stage": "%s", "ts": %s}\n' "$((n + 1))" "$state" "$STAGE" "$(date +%s)" >> "$f" || true
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
  if [ "$rc" -eq 0 ]; then
    touch "$MARK_DIR/$STAGE.ok"
    hb ok
  else
    hb failed
  fi
  exit "$rc"
}
trap finish EXIT
trap 'exit 143' TERM INT

if [ "$SLURM_ARRAY_TASK_ID" -gt 0 ]; then
  PREV="${STAGES[$((SLURM_ARRAY_TASK_ID - 1))]}"
  if [ ! -f "$MARK_DIR/$PREV.ok" ]; then
    echo "stage $STAGE refused: $PREV did not finish" >&2
    exit 1
  fi
fi

case "$STAGE" in
  embed) {{embed_cmd}} ;;
  read)  {{read_cmd}} ;;
  code)  {{code_cmd}} ;;
esac
