# 9. Share the GPUs: embedding yields to reader and coder

One-time on head, then it runs on its own. The Atlas embedding slices use the same GPUs as the reader (node-b) and coder (node-a) models. Ollama runs outside Slurm, so Slurm cannot see the conflict. `hk sched` fills that gap: when a role gets a request, the embedding slice on that role's node is stopped and put back in the queue held; after `idle_release_s` (15 min) with no requests, the node's models are unloaded and the slice is released, and it resumes from its checkpoint. The head slice (shard 0) is never touched, since no role lives on head.

How it fits together:
- The router records, per role, requests in flight, requests waiting, and seconds since last use (`/health`).
- `hk sched` reads that every 5 s, plus `squeue`, and acts with `scontrol requeuehold` (running job), `scontrol hold` (pending job, so it cannot start under a model) and `scontrol release`. It only releases jobs it held itself.
- The router gate holds a reader/coder request until `hk sched` reports the GPU free (at most `wait_s`). If `hk sched` is down (state older than `stale_s`), requests pass straight through, as before.
- Every action is one line in `logs/sched.tsv`; the router logs `gate_waited`, `gate_timeout`, `gate_stale`.

## Steps

1. Pull, and add the `[router.gate]`, `[sched]` and `[sched.nodes]` tables from `config.example.toml` to `config.toml`. Check the job name prefix matches the embedding slices:
   ```bash
   cd ~/projects/housekeeping && git pull
   squeue -u $USER -o '%i %j %N'          # the slices' names should start with embed_job_prefix
   ```

2. Test the Slurm mechanism by hand on one worker slice before trusting it. This needs a running slice on node-b (relaunch it with the `go 2 ...` line from the embedding run if it is paused). `J` is that slice's job id.
   ```bash
   J=139; C=/srv/warm/cluster/atlas/_run/checkpoints/shard2/home_folder_topdown.json
   jq -c '{seen, indexed: .counts.indexed}' $C
   scontrol requeuehold $J && sleep 60 && squeue -j $J -o '%i %T %r %n'
   scontrol release $J && sleep 180 && squeue -j $J -o '%i %T %N' && jq -c '{seen, indexed: .counts.indexed}' $C
   ```
   - After `requeuehold`: `PENDING JobHeldUser node-b`. A permission error means users cannot requeue on this Slurm; stop here and report it.
   - After `release`: `RUNNING node-b`, and `seen` at or above the first reading. If `seen` dropped back near 0, the relaunched script did not resume from the checkpoint; stop and report it.
   - The slice's `resume-<jobid>.log` may be truncated by the requeue. The checkpoint is the record that matters.

3. Dry run against the live queue. Nothing is changed or written.
   ```bash
   uv run hk sched run --dry-run
   ```
   With no recent router traffic, expect both roles `embed run` and no actions.

4. Install and start the service, then restart the router so it loads the gate.
   ```bash
   mkdir -p ~/data/housekeeping/sched ~/.config/systemd/user
   cp src/housekeeping/sched/sched.service ~/.config/systemd/user/hk-sched.service
   systemctl --user daemon-reload && systemctl --user enable --now hk-sched
   systemctl --user restart hk-router
   uv run hk sched status
   ```

5. See it work. Send one coder request; it waits while the node-a slice leaves the GPU, then answers.
   ```bash
   time curl -s localhost:8000/v1/chat/completions -H 'content-type: application/json' \
     -d '{"model":"coder","messages":[{"role":"user","content":"say ok"}]}' | head -c 200; echo
   uv run hk sched status                  # coder: gpu free, embed yield; job held by sched
   tail -5 logs/sched.tsv
   ```
   After 15 minutes with no coder requests, `status` shows the job released and `RUNNING` again.

## Daily use

- Long OpenCode session: claim the GPU up front so the first request does not wait.
  ```bash
  uv run hk sched claim coder --minutes 120
  uv run hk sched unclaim coder           # done early; embedding resumes after idle_release_s
  ```
- Pause all embedding by hand: `scancel` the slices as before. `hk sched` forgets jobs that leave the queue.
- Stop sharing: `systemctl --user stop hk-sched`. The gate sees stale state within `stale_s` and stops waiting. Jobs the scheduler held stay held; release them with `scontrol release <id>` (ids in `hk sched status`).

## Check

`hk sched status` shows state age under 10 s, and `logs/sched.tsv` shows a `requeuehold` then a `release` for the same job around a burst of reader or coder work, with the slice's checkpoint `seen` still rising afterwards.
