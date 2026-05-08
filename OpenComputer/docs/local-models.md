# Local Models — Setup Recipes

**Last verified:** 2026-05-08 against the public docs of each project. Flags + defaults change; if a recipe below stops working, check upstream first.

OpenComputer is provider-agnostic. Any HTTP-OpenAI-compatible local server works as a custom provider. This doc captures the load-bearing setup gotchas that bite users every time — not "what is Ollama" — so you can land the right invocation on the first try.

## Quick-start matrix

| Use case | Pick |
|---|---|
| "I have a Mac, just work" | **Ollama** |
| GPU server, production serving | **vLLM** or **SGLang** |
| Tiny box / Raspberry Pi / no GPU | **llama.cpp** |
| Clicky people, GUI-driven | **LM Studio** |
| Apple Silicon, MLX-native | **mlx-server** (bundled provider) |
| Multi-provider routing layer | OpenRouter / LiteLLM Proxy (separate products; point OC at via `custom_providers:`) |

OC ships native plugins for all of the above:
`extensions/ollama-provider/`, `lmstudio-provider/`, `llama-cpp-server-provider/`, `mlx-server-provider/`.
For vLLM / SGLang / unrecognised endpoints, use `oc model → Custom endpoint` in the setup wizard or set `custom_providers:` in `~/.opencomputer/<profile>/config.yaml`.

---

## Ollama

```bash
ollama pull qwen2.5-coder:32b
ollama serve
# In another terminal:
oc model
# Pick "Custom endpoint" → http://localhost:11434/v1 → model name = qwen2.5-coder:32b
```

**Critical: context-length default is 4096.** That kills agent loops fast — every turn after a few thousand tokens gets truncated and the loop loses coherence.

Set the context window before `ollama serve`:
```bash
export OLLAMA_CONTEXT_LENGTH=32768
ollama serve
```

Or bake it into a Modelfile if you want the bigger window per-model rather than per-server.

Verify with `ollama ps` after a request — the `CONTEXT` column shows the effective window.

OC also caches the context window after first detection; if you bumped Ollama's context but OC still shows 4096, set `model.context_length: 32768` explicitly in `config.yaml` to bypass the cache.

---

## vLLM

```bash
vllm serve meta-llama/Llama-3.1-70B-Instruct \
  --port 8000 --max-model-len 65536 \
  --enable-auto-tool-choice \
  --tool-call-parser hermes
```

Then `oc model → Custom endpoint → http://localhost:8000/v1`.

**Critical: tool calling requires both flags.** Without `--enable-auto-tool-choice`, vLLM returns tool calls as raw JSON text in the response body — agent loop sees no `tool_calls` and stalls. Without a parser, the same.

Pick the parser that matches your model:

| Model family | `--tool-call-parser` |
|---|---|
| Qwen / Hermes-3 | `hermes` |
| Llama 3 (JSON-format tools) | `llama3_json` |
| Mistral (function calling) | `mistral` |
| DeepSeek V3 | `deepseek_v3` |
| Salesforce xLAM | `xlam` |
| Pythonic-call models | `pythonic` |

`--max-model-len` defaults to the model's max — fine for inference but expensive in KV cache. Set it to your actual usage ceiling.

---

## SGLang

```bash
python -m sglang.launch_server \
  --model meta-llama/Meta-Llama-3.1-70B-Instruct \
  --port 30000 \
  --context-length 65536 \
  --tp 2 \
  --tool-call-parser qwen \
  --default-max-tokens 4096
```

Then `oc model → Custom endpoint → http://localhost:30000/v1`.

**Critical: default `max_tokens` is 128 tokens.** That cuts assistant responses mid-sentence on the first turn. Set `--default-max-tokens` server-side or pass `model.max_tokens` in OC's config.

`--tp` is tensor parallelism (number of GPUs to split across). `--context-length` defaults to model max.

---

## llama.cpp

```bash
./llama-server \
  --jinja \
  -fa \
  -c 32768 \
  -ngl 99 \
  -m model.gguf \
  --port 8080 \
  --host 0.0.0.0
```

Then `oc model → Custom endpoint → http://localhost:8080/v1`.

**Critical: `--jinja` is mandatory for tool calling.** Without it, tool calls are returned as raw JSON text in the response — the agent loop never sees structured `tool_calls`, never executes a tool, and silently degrades to a chat model. This is the single most common "tools don't work" report; it's always missing `--jinja`.

`-c 32768` sets context window. `-ngl 99` offloads as many layers as fit on GPU (use a smaller number on tiny GPUs). `-fa` enables flash attention (free perf if your build supports it).

---

## LM Studio

GUI-friendly. From the command line:
```bash
lms server start
lms load <model-name> --context-length 32768
```

Then `oc model → LM Studio` in the setup wizard. OC auto-discovers loaded models via the LM Studio REST API.

Manual: base URL `http://localhost:1234/v1`, no API key required by default.

---

## mlx-server (Apple Silicon)

OC ships `mlx-server-provider/` natively for the MLX inference stack on Apple Silicon. Install + load per the upstream `mlx-server` docs, then `oc model → mlx-server`.

---

## WSL2 networking (Windows users running local servers)

If OC runs on the Windows side and the local model server runs in WSL2 (or vice versa), `localhost` may not resolve.

**Option 1 (recommended, Windows 11 22H2+) — mirrored mode:**

Put this in `%USERPROFILE%\.wslconfig`:
```ini
[wsl2]
networkingMode=mirrored
```

Then `wsl --shutdown` and reopen the WSL shell. After this, `localhost` works bidirectionally.

**Option 2 (NAT mode, default on older Windows) — Windows host IP:**

From inside WSL:
```bash
ip route show | grep default | awk '{ print $3 }'
# e.g. 172.29.192.1
```

Use that IP as the base URL: `http://172.29.192.1:11434/v1`.

In NAT mode, the local server **must bind to `0.0.0.0`** (not `127.0.0.1`), and Windows Firewall needs an inbound rule for the port.

| Server | Bind-all-interfaces flag |
|---|---|
| Ollama | `OLLAMA_HOST=0.0.0.0` (system env var on the WSL side) |
| llama.cpp | `--host 0.0.0.0` |
| LM Studio | "Serve on Network" toggle in Developer tab |
| vLLM | already binds `0.0.0.0` by default |
| SGLang | already binds `0.0.0.0` by default |

---

## Common issues

| Problem | Cause | Fix |
|---|---|---|
| Tool calls appear as raw JSON in chat output | Tool calling not enabled in the server | `--jinja` (llama.cpp) / `--enable-auto-tool-choice` + `--tool-call-parser <name>` (vLLM) / `--tool-call-parser` (SGLang) |
| Agent loop loses context after a few turns | Context window too small | Set ≥ 32K via `OLLAMA_CONTEXT_LENGTH` / `--max-model-len` / `-c` / `--context-length` |
| Startup log says "Context limit: 2048" | Server defaulted low | Bump via the appropriate flag; OC may also need `model.context_length: 32768` in `config.yaml` to bypass the detection cache |
| Responses cut mid-sentence | Server's `max_tokens` cap | SGLang `--default-max-tokens 4096`; vLLM has no equivalent (use `model.max_tokens` in OC's config); llama.cpp doesn't cap |
| `oc model` picker doesn't see the local server | Server not bound on the right interface, or wrong port | `curl http://<host>:<port>/v1/models` first; if that 200s, the picker should too |

---

## What OpenComputer does for you

- **Detects + caches context window** from the server's `/v1/models` endpoint. Manual override via `model.context_length` in `config.yaml` if the server reports wrong.
- **Names the local provider plugins** so you don't write transport code: `ollama-provider`, `lmstudio-provider`, `llama-cpp-server-provider`, `mlx-server-provider`.
- **`oc doctor`** flags missing API keys + unreachable endpoints.
- **`custom_providers:` config** lets you register more endpoints by name and switch via `/model custom:<name>:<model>`.
- **No vendor lock-in** — same agent runs against any compatible endpoint.

---

## Related docs

- `OpenComputer/docs/refs/hermes-agent/2026-05-08-quickstart-cli-tui-wsl2-config-parity.md` — Wave 1 parity snapshot.
- `OpenComputer/docs/refs/hermes-agent/2026-05-08-dashboard-extensions-rl-providers-parity.md` — this PR's findings doc (Wave 2).
- `OpenComputer/CLAUDE.md` — session context.

If a recipe above is wrong or out of date, file an issue with the upstream version + your reproducer.
