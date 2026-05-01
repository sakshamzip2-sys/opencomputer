# `opencomputer/voice/` — realtime voice contract & shared runtime

This directory holds the **provider-agnostic** runtime for two-way streaming
voice (`opencomputer voice realtime`). The actual provider bridges
(OpenAI Realtime, Gemini Live, future Anthropic Voice) live under
`extensions/<provider>-provider/realtime.py` and never appear here.

This file is the playbook for:

1. Editors of this directory — what the contract is, what stays here vs
   leaks into a bridge.
2. **Anyone adding a new realtime voice provider plugin.** See § "Adding
   a provider" below; the canonical reference port is documented at
   [`extensions/gemini-provider/CLAUDE.md`](../../extensions/gemini-provider/CLAUDE.md) — clone it,
   don't write from scratch.

## What's in this directory

| File | Role | When to edit |
|---|---|---|
| `realtime_session.py` | Orchestrator — `create_realtime_voice_session` builds a `RealtimeVoiceSession`, wires bridge ↔ audio sink ↔ tool router | Only when changing the session lifecycle (rare) |
| `audio_io.py` | `LocalAudioIO` — sounddevice mic + speaker. Mic locked to 16 kHz; output rate is constructor-configurable. | Only when changing the audio I/O surface |
| `tool_router.py` | `dispatch_realtime_tool_call` — fetches tool from registry, invokes, posts result back through `bridge.submit_tool_result` | Only when changing tool dispatch policy (perm gating, etc.) |

Adding a new provider does NOT require touching any of these.

## The contract — `plugin_sdk/realtime_voice.py`

Two SDK exports define the world the shared layer sees:

```python
class BaseRealtimeVoiceBridge(ABC):
    async def connect() -> None
    def     send_audio(audio: bytes) -> None         # PCM16, 16 kHz mono
    def     send_user_message(text: str) -> None
    def     submit_tool_result(call_id: str, result: Any) -> None
    def     trigger_greeting(instructions: str | None = None) -> None
    def     close() -> None
    def     is_connected() -> bool

@dataclass(frozen=True, slots=True)
class RealtimeVoiceToolCallEvent:
    call_id:  str               # universal; what submit_tool_result echoes
    name:     str
    args:     Any               # decoded JSON, typically dict
    item_id:  str | None = None # OpenAI conversation-item id; None for others
    extra:    dict = {}         # opaque per-provider metadata
```

The bridge gets a callbacks dict at construction:

```python
on_audio(bytes)                       # PCM16 chunk for the speaker
on_clear_audio()                      # barge-in — flush speaker
on_transcript(role, text, final)      # live captions
on_tool_call(RealtimeVoiceToolCallEvent)
on_ready() / on_error(exc) / on_close(reason)
```

The session orchestrator is unaware of the underlying transport. Bridges
translate provider events to those callbacks; the shared layer never
branches on provider.

## Plugin-driven dispatch — `PluginAPI.register_realtime_bridge`

There is **no hardcoded provider list** anywhere in the runtime. Each
plugin registers itself during `load_all`:

```python
# extensions/<provider>-provider/plugin.py
def register(api):
    api.register_realtime_bridge(
        "<short-name>",                       # CLI --provider value
        _factory,                             # builds bridge from callbacks
        env_var="<PROVIDER>_API_KEY",         # CLI checks this for creds
        audio_sink_kwargs={...},              # forwarded to LocalAudioIO
    )
```

The CLI (`cli_voice.py::voice_realtime`) reads the registration to pick
the env var and audio rate — never hardcodes them. See
`opencomputer/plugins/loader.py::PluginAPI.register_realtime_bridge` for
the full API and `_RealtimeBridgeRegistration` shape.

## Adding a provider — the playbook

Use [Gemini](../../extensions/gemini-provider/CLAUDE.md) as the
copy-from template. Concretely, for `<name>` (e.g. `anthropic`):

1. **Create `extensions/<name>-provider/`** with four files:
   - `plugin.json` — manifest (id, kind=provider, env_vars, model_prefixes)
   - `plugin.py` — `register(api)` calling `api.register_realtime_bridge(...)`
   - `realtime.py` — concrete `BaseRealtimeVoiceBridge` subclass
   - `realtime_helpers.py` — pure helpers (error parser, any provider-specific maps)

2. **Implement the 7 ABC methods.** All seven are required.
   `submit_tool_result` typically needs a `_call_id_to_name` cache if the
   provider's wire format requires the function name on the response (Gemini does;
   OpenAI doesn't).

3. **Audio rate.** If the provider streams output at a non-16-kHz rate
   (Gemini = 24 kHz), declare `audio_sink_kwargs={"output_sample_rate": N}`
   in the plugin's `register()` call. Then **forward the audio
   byte-for-byte** in the bridge — never resample in the bridge. The CLI
   passes the kwargs to `LocalAudioIO` so playback is at native rate.

4. **Register the conftest alias.** Add ONE wrapper to
   `tests/conftest.py`:
   ```python
   _<NAME>_PROVIDER_DIR = _EXT_DIR / "<name>-provider"

   def _register_<name>_provider_alias() -> None:
       _register_extension_alias(
           "<name>_provider", _<NAME>_PROVIDER_DIR,
           submodules=("realtime", "realtime_helpers", "plugin"),
       )
   ```
   And call it at the bottom of the file. The generic helper handles the
   sys.modules synthesis; the wrapper just declares which submodules
   exist on disk.

5. **Write tests.** Copy `tests/test_gemini_realtime_bridge.py` as the
   template — fake WebSocket, push inbound frames, assert outbound shape
   + callback delivery. Adapt the wire format assertions to your provider.

6. **Smoke test the CLI.** `opencomputer voice realtime --provider <name>`
   should:
   - Reject if the env var is missing (the CLI checks `registration.env_var`)
   - Connect on real creds
   - Tool calls should round-trip through `OC's tool registry`

## Gotchas burned in by Gemini port

These are real bugs caught during the Gemini port (2026-05). Future
authors: don't re-introduce.

1. **`websockets.exceptions` is not auto-imported in v15+.** Use
   `from websockets.exceptions import ConnectionClosed` — never
   `except websockets.exceptions.ConnectionClosed`. Both bundled bridges
   already do the explicit import.

2. **Substring-vs-key in test filters.** A naive
   `if "realtimeInput" in s` substring check on JSON-serialized frames
   matches the setup message's `realtimeInputConfig` field too. Always
   parse first, then check top-level key:
   ```python
   parsed = [json.loads(s) for s in fake_ws.sent]
   audio_frames = [m for m in parsed if "realtimeInput" in m]
   ```

3. **`item_id` is OpenAI-specific.** Don't fake it for providers that
   don't have a conversation-item concept. Pass only `call_id`, `name`,
   `args`; `item_id` defaults to `None`.

4. **Provider-specific config goes in `audio_sink_kwargs` / `extra`,
   never in shared code.** If you find yourself adding a `_PROVIDER_DEFAULTS`
   table or `if provider == "x"` branch in `cli_voice.py` or anywhere
   in this directory, you're doing it wrong. The registration metadata
   is supposed to absorb that.

5. **Sibling import fallback.** Bridge files should support BOTH
   plugin-loader mode (sibling import) AND package mode (conftest alias).
   See the `try: from realtime_helpers import ... except ImportError:`
   pattern in both bundled bridges.

## Hard rules (don't break)

1. **No provider names in this directory.** The shared layer must work
   for any number of providers. Search for hardcoded `"openai"` /
   `"gemini"` strings before merging — there should be zero matches in
   this dir.

2. **`audio_io.py` mic rate is locked at 16 kHz.** Every realtime
   provider so far accepts 16 kHz mic input. Resampling on the way IN
   belongs in the bridge if a future provider demands something different
   — not here.

3. **The contract on `plugin_sdk/realtime_voice.py` is stable.**
   Changing field order, removing fields, or making optional fields
   required is a major-version break. Adding fields with defaults is OK.

## Cross-references

- **Reference plugin:** [`extensions/gemini-provider/CLAUDE.md`](../../extensions/gemini-provider/CLAUDE.md)
- **OpenAI plugin (existing):** `extensions/openai-provider/realtime.py`
- **SDK contract:** `plugin_sdk/realtime_voice.py` and `plugin_sdk/CLAUDE.md`
- **Plugin loader internals:** `opencomputer/plugins/CLAUDE.md`
