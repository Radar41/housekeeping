# 4. Start the role router

One-time on head, then a health check whenever something looks wrong. The router gives every tool one address (`http://localhost:8000/v1`). A request names a role (`reader`, `coder`, `embedder`), either as `"role"` in the body or as the model name, and the router sends it to the node configured for that role. Overflow to Andromeda is off.

## Steps

1. Point each role at a node's Ollama in `config.toml`. Use the models `ollama list` shows on that node (process 3, step 4).
   ```toml
   [router.roles.reader]
   url = "http://127.0.0.1:11434"
   model = "qwen3.5:9b"
   timeout_s = 120

   [router.roles.coder]
   url = "http://10.10.1.2:11434"
   model = "deepseek-coder-v2:lite"
   timeout_s = 300

   [router.roles.embedder]
   url = "http://127.0.0.1:11434"     # or wherever Qwen3-Embedding-4B is served for Atlas
   model = "qwen3-embedding:4b"
   timeout_s = 30
   ```
   Set `[router] host = "127.0.0.1"` unless another machine must reach it.
2. Try it in the foreground first.
   ```bash
   cd ~/projects/housekeeping && uv run hk router serve
   ```
   In a second terminal:
   ```bash
   uv run hk router health
   curl -s localhost:8000/v1/chat/completions -H 'content-type: application/json' \
     -d '{"model":"coder","messages":[{"role":"user","content":"say ok"}]}'
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
