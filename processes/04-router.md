# 4. Start the role router

One-time on head, then a health check whenever something looks wrong. The router gives every tool one address (`http://localhost:8000/v1`). A request names a role (`reader`, `coder`, `embedder`), either as `"role"` in the body or as the model name, and the router sends it to the node configured for that role. Overflow to Andromeda is off.

## Steps

1. Point each role at a node's Ollama in `config.toml`. Use the models `ollama list` shows on that node (process 3, step 4).
   ```toml
   [router]
   host = "127.0.0.1"
   port = 8000
   log = "logs/router.tsv"
   health_interval_s = 10
   health_timeout_s = 3

   [router.roles.coder]              # node-a, RTX 5070 12 GB, 64k context
   url = "http://10.10.1.2:11434"
   model = "qwen3.5:9b"
   timeout_s = 300

   [router.roles.reader]             # node-b, RTX 5070 12 GB, 64k context
   url = "http://10.10.2.2:11434"
   model = "qwen3.5:9b"
   timeout_s = 120

   [router.roles.embedder]           # wherever Atlas serves Qwen3-Embedding-4B
   url = "http://127.0.0.1:8101"
   model = "qwen3-embedding-4b"
   timeout_s = 30
   ```
   This is the working radar-41 layout (verified 2026-10-26): the workers serve the router, and head's GPU (131k context) stays free for your own OpenCode sessions. Keep `[router.overflow_inflight]` and `[router.andromeda]` from the example.
   Set `[router] host = "127.0.0.1"` unless another machine must reach it.

   The embedder must be the same model and precision that made the existing Atlas vectors (Qwen3-Embedding-4B, 2,560 dimensions). Vectors from another model, such as `nomic-embed-text` (768 dimensions), cannot be compared with them. A different quantization of the same model gives close but not identical vectors, so check one known document against its stored vector before using it for new embeddings.
2. Try it in the foreground first.
   ```bash
   cd ~/projects/housekeeping && uv run hk router serve
   ```
   In a second terminal:
   ```bash
   uv run hk router health
   for r in coder reader; do
     curl -s localhost:8000/v1/chat/completions -H 'content-type: application/json' \
       -d "{\"model\":\"$r\",\"messages\":[{\"role\":\"user\",\"content\":\"say ok\"}]}" | head -c 300; echo
   done
   tail -3 logs/router.tsv
   ```
   Stop it with Ctrl-C.
3. Run it as a user service so it survives logout and reboot.
   ```bash
   mkdir -p ~/.config/systemd/user
   cp src/housekeeping/router/router.service ~/.config/systemd/user/hk-router.service
   systemctl --user daemon-reload
   systemctl --user enable --now hk-router
   loginctl enable-linger "$USER"      # may ask for your password
   ```

## Check any time

```bash
uv run hk router health            # table: role, up, latency, inflight, target
uv run hk router health --json     # same, for scripts
journalctl --user -u hk-router -n 50
tail logs/router.tsv               # one line per request: role, target, latency. Never the prompt.
```
`health` exits non-zero if any role is down; a down embedder does not stop reader or coder from working.

## Done when

`hk router health` shows every role `True`, and OpenCode's `housekeeping/coder` model answers (process 3, step 6).
