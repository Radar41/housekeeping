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

# HEARTBEAT INTERFACE (UNVERIFIED, adjust only this function):
# Assumed call: <script> --run-id ID --state running|ok|failed
hb() {
  uv run python "{{heartbeat_script}}" --run-id "${SLURM_JOB_NAME}-${STAGE}-${SLURM_JOB_ID}" --state "$1" || true
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
