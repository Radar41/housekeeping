# 3. Set up the local coders

One-time per node. Do head first, then node-a, then node-b. Steps marked (sudo) prompt for your password; nothing here needs passwordless sudo.

## Why the earlier setup failed

Ollama gives a 4,096-token context to any model on a GPU with less than 24 GB, and every card here is under 24 GB. OpenCode and Codex both need at least 64k, so with 4k their tool calls get cut off and fail without an error ([Ollama context length](https://docs.ollama.com/context-length), [Ollama OpenCode guide](https://docs.ollama.com/integrations/opencode)). Step 2 fixes this.

## Steps on every node

1. See what is installed. Read-only.
   ```bash
   for c in ollama opencode codex claude cargo rustc node npm git gh rsync nvidia-smi; do
     printf '%-11s' "$c"; command -v "$c" >/dev/null && ("$c" --version 2>&1 | head -1) || echo missing
   done
   ollama list
   systemctl show ollama -p Environment
   nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
   ```
2. Raise the Ollama context to 64k. (sudo)
   ```bash
   sudo systemctl edit ollama
   ```
   In the editor, add these lines between the comment markers, then save:
   ```ini
   [Service]
   Environment="OLLAMA_CONTEXT_LENGTH=65536"
   ```
   ```bash
   sudo systemctl restart ollama
   ollama run qwen3.5:9b "say ok" && ollama ps
   ```
   `ollama ps` must show `CONTEXT 65536` and `100% GPU`. If it shows a CPU split, the card is too small for that model at 64k: add the two memory savers below, or use a smaller model on that node.

   Going above 64k. 64k is OpenCode's minimum, not a limit. The ceiling is the smaller of the model's own window and what fits in VRAM:

   | Model | Own window ([Ollama library](https://ollama.com/library)) | Use for |
   |---|---|---|
   | `qwen3.5:9b` | 256K | OpenCode, Codex, Claude Code, reader |
   | `deepseek-coder-v2:lite` (16b) | 160K | coder role |
   | `qwen2.5-coder:14b` | 32K | short completions only; cannot reach OpenCode's 64k |

   The context memory (K/V cache) grows with context length. Two settings shrink it ([Ollama FAQ](https://docs.ollama.com/faq)); add them in the same `sudo systemctl edit ollama` block:
   ```ini
   Environment="OLLAMA_FLASH_ATTENTION=1"
   Environment="OLLAMA_KV_CACHE_TYPE=q8_0"     # about half the memory of the default f16, very small quality loss
   Environment="OLLAMA_NUM_PARALLEL=1"         # memory scales with parallel x context; keep 1 for big contexts
   ```
   Then raise `OLLAMA_CONTEXT_LENGTH` one step at a time (65536, 98304, 131072, ...), restart, run the model, and check `ollama ps`. Keep the largest value that still shows `100% GPU`. `q4_0` saves more but loses more precision, and Qwen models are more sensitive to it.
   A single session can also ask for more without changing the default: `/set parameter num_ctx 131072` inside `ollama run`, or `"options": {"num_ctx": 131072}` in an API call.
3. Workers only (node-a, node-b): let head reach this node's Ollama over the direct link, and nowhere else. (sudo) Add one more line in `sudo systemctl edit ollama`:
   ```ini
   Environment="OLLAMA_HOST=10.10.1.2:11434"     # node-a; use 10.10.2.2:11434 on node-b
   ```
   ```bash
   sudo systemctl restart ollama
   echo 'export OLLAMA_HOST=10.10.1.2:11434' >> ~/.bashrc     # node-b: 10.10.2.2
   ```
   From head, check: `curl -s http://10.10.1.2:11434/v1/models | head -c 300`.
   After this, scripts on the worker must call `$OLLAMA_HOST`, not `127.0.0.1`. The batch runner in `~/radar41-orchestrator` needs that change.
4. Pull the models this node will serve.
   ```bash
   ollama pull qwen3.5:9b               # every node: the agent model (256K window, tool use)
   ollama pull deepseek-coder-v2:lite   # node-a and node-b: coder role (160K window)
   ```
5. Rust toolchain, for Rust pipelines and anything that builds from source. No sudo.
   ```bash
   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
   . "$HOME/.cargo/env"
   rustup component add clippy rustfmt rust-analyzer
   cargo --version
   ```

## Coding agents, on head (and any node you code on)

6. OpenCode ([install](https://opencode.ai/docs/)).
   ```bash
   curl -fsSL https://opencode.ai/install | bash
   opencode --version
   ```
   Write `~/.config/opencode/opencode.json` ([config location](https://opencode.ai/docs/config/)). Two providers: this node's Ollama, and the housekeeping router from process 4, where the model name is the role.
   ```json
   {
     "$schema": "https://opencode.ai/config.json",
     "provider": {
       "ollama": {
         "npm": "@ai-sdk/openai-compatible",
         "name": "Ollama (this node)",
         "options": { "baseURL": "http://127.0.0.1:11434/v1" },
         "models": {
           "qwen3.5:9b": { "name": "Qwen3.5 9B" }
         }
       },
       "housekeeping": {
         "npm": "@ai-sdk/openai-compatible",
         "name": "radar-41 router",
         "options": { "baseURL": "http://localhost:8000/v1" },
         "models": {
           "coder": { "name": "coder role" },
           "reader": { "name": "reader role" }
         }
       }
     }
   }
   ```
   On a worker, change the first `baseURL` to `http://$OLLAMA_HOST/v1` written out (for example `http://10.10.1.2:11434/v1`). Shortcut that writes the Ollama part for you: `ollama launch opencode --config`.
   Test: `cd ~/projects/housekeeping && opencode`, pick `ollama/qwen3.5:9b`, ask it to list the files in `processes/`.
7. Codex CLI with Ollama ([Ollama Codex guide](https://docs.ollama.com/integrations/codex)). Needs Node.js; Codex talks to Ollama directly, not through the router.
   ```bash
   npm install -g @openai/codex
   ollama launch codex --config
   codex --oss -m qwen3.5:9b
   ```
8. Claude Code with Ollama ([Ollama Claude Code guide](https://docs.ollama.com/integrations/claude-code)). Also direct to Ollama.
   ```bash
   curl -fsSL https://claude.ai/install.sh | bash
   ollama launch claude
   # or by hand:
   ANTHROPIC_AUTH_TOKEN=ollama ANTHROPIC_API_KEY="" ANTHROPIC_BASE_URL=http://127.0.0.1:11434 claude --model qwen3.5:9b
   ```
   If `ollama launch` is an unknown command, update Ollama first: `curl -fsSL https://ollama.com/install.sh | sh` (sudo).

## Orchestrators, head only

9. Superset, to run several agents side by side, each in its own git worktree ([superset-sh/superset](https://github.com/superset-sh/superset)). Linux builds are marked experimental. (sudo)
   ```bash
   mkdir -p ~/apps/superset && cd ~/apps/superset
   gh release download -R superset-sh/superset --pattern '*amd64.deb' --clobber
   sudo apt install ./superset-*-amd64.deb
   ```
   Open it from the desktop session on head. OpenCode, Claude Code, and Codex are built-in agents and use the configs from steps 6 to 8. Point it at `~/projects/<repo>`.
10. Mission Control, optional, to dispatch and review agent runs ([builderz-labs/mission-control](https://github.com/builderz-labs/mission-control)). Needs Node.js 22 and pnpm. It lists Claude Code and Codex adapters; OpenCode is not listed. Keep it separate from the Atlas instrument's RUN panel.
    ```bash
    cd ~/apps && git clone https://github.com/builderz-labs/mission-control.git && cd mission-control
    bash install.sh --local
    ```
    Open `http://localhost:3000/setup` on head and make the admin account. Do not expose port 3000 beyond head.

## Done when

On each node, `ollama ps` shows 65536 and 100% GPU, and from head `curl` reaches both workers. On head, OpenCode answers through both providers (the router one after process 4), and `codex --oss` and `claude` start.

## Record it

```bash
mkdir -p ~/data/housekeeping && for c in ollama opencode codex claude cargo superset; do
  printf '%s\t%s\t%s\t%s\n' "$(date -u +%FT%TZ)" "$(hostname)" "$c" "$($c --version 2>&1 | head -1)"; done >> ~/data/housekeeping/coders.tsv
```
