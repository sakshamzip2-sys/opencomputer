# `extensions/gemini-provider/` — canonical realtime-voice provider template

This plugin is the **reference implementation** for adding a new realtime
voice provider to OpenComputer. If you're being asked to add Anthropic
Voice / Cohere / any other realtime provider, **copy this directory** and
adapt the wire format. The contract you're satisfying is documented at
[`opencomputer/voice/CLAUDE.md`](../../opencomputer/voice/CLAUDE.md).

Currently this plugin ships only the realtime voice surface. A
chat-completion `BaseProvider` for `models/gemini-*` is **not yet ported**
(separate scope — google-genai SDK shape, multimodal, streaming).

## Files in this plugin

| File | Lines | Role |
|---|---|---|
| `plugin.json` | ~25 | Manifest. `kind: provider`, env vars, model prefixes |
| `plugin.py` | ~50 | Entry: `register(api)` calls `api.register_realtime_bridge(...)` with the factory + metadata |
| `realtime.py` | ~370 | `GeminiRealtimeBridge(BaseRealtimeVoiceBridge)` — the WebSocket lifecycle + event dispatch |
| `realtime_helpers.py` | ~50 | Pure helpers (`vad_threshold_to_sensitivity`, `read_realtime_error_detail`) |

A full bundled plugin = ~500 lines. Most of that is the wire-format
glue in `realtime.py`; the SDK contract handles everything else.

## How each ABC method maps to Gemini Live's wire

The contract is in `plugin_sdk/realtime_voice.py`. Here's how Gemini's
BidiGenerateContent maps onto it:

| ABC method | Gemini wire |
|---|---|
| `connect()` | Open WS to `wss://generativelanguage.googleapis.com/...?key=<KEY>`; send `{"setup": {...}}`; spawn read loop |
| `send_audio(bytes)` | `{"realtimeInput": {"audio": {"mimeType": "audio/pcm;rate=16000", "data": "<b64>"}}}` |
| `send_user_message(text)` | `{"clientContent": {"turns": [{"role": "user", "parts": [{"text": text}]}], "turnComplete": True}}` |
| `submit_tool_result(call_id, result)` | `{"toolResponse": {"functionResponses": [{"id": call_id, "name": <cached>, "response": <wrapped>}]}}` |
| `trigger_greeting()` | Synthetic clientContent user message — Gemini has no native greeting trigger |
| `close()` | Set intentional flag, close WS |
| `is_connected()` | `_connected and _session_configured` |

Inbound dispatch in `_handle_event`:

| Gemini server message | Bridge action |
|---|---|
| `setupComplete` | `_session_configured = True`, flush pending audio queue, fire `on_ready()` |
| `goAway.timeLeft` | Log; ConnectionClosed handles the rest via `_attempt_reconnect` |
| `toolCall.functionCalls[]` | Cache name → `_call_id_to_name`, fire `on_tool_call(RealtimeVoiceToolCallEvent)` |
| `toolCallCancellation.ids[]` | Pop from name cache; OC has no cancel callback |
| `serverContent.interrupted` | `on_clear_audio()` (barge-in) |
| `serverContent.modelTurn.parts[].inlineData (audio/pcm)` | Base64-decode → `on_audio(bytes)` (PCM16 24 kHz, forwarded as-is) |
| `serverContent.modelTurn.parts[].text` | `on_transcript("assistant", text, False)` |
| `serverContent.{input,output}Transcription.text` | `on_transcript(role, text, False)` per role |
| `serverContent.turnComplete` | Synthetic empty-string `final=True` per role so consumers can latch UI flush |

## Three Gemini-specific wire quirks worth understanding

These are why a Gemini bridge isn't an OpenAI bridge with renamed strings.

1. **Asymmetric audio rates.** Mic in is 16 kHz (universal); model out is
   **24 kHz**. The bridge does NOT resample — it forwards bytes as-is and
   relies on `LocalAudioIO(output_sample_rate=24_000)` configured by the
   CLI from the registration's `audio_sink_kwargs`.

2. **Tool result requires the function name.** OpenAI's
   `function_call_output` only needs `call_id`. Gemini's
   `functionResponses[]` needs **both** `id` AND `name`. The OC ABC
   only gives the bridge `call_id` on `submit_tool_result(call_id, result)`.
   Fix: cache `_call_id_to_name[call_id] = name` when the inbound
   `toolCall` arrives, look it up at submit time, pop after.

3. **No `response.create` follow-up.** OpenAI requires an explicit
   `response.create` after `submit_tool_result` / `send_user_message` to
   tell the model to respond. Gemini auto-responds whenever a turn
   completes. Don't issue extra response triggers.

## How `plugin.py` wires into the registry

```python
def register(api):
    api.register_realtime_bridge(
        "gemini",                                  # CLI --provider gemini
        _gemini_realtime_factory,
        env_var="GEMINI_API_KEY",                  # cli_voice.py validates this
        audio_sink_kwargs={                        # cli_voice.py passes to LocalAudioIO
            "output_sample_rate": OUTPUT_RATE_HZ,  # = 24_000
        },
    )
```

The factory signature matches every other realtime factory:

```python
def _gemini_realtime_factory(*, callbacks, api_key, model, instructions, **kwargs):
    return GeminiRealtimeBridge(
        api_key=api_key, model=model or None, instructions=instructions,
        on_audio=callbacks["on_audio"],
        on_clear_audio=callbacks["on_clear_audio"],
        on_transcript=callbacks.get("on_transcript"),
        on_tool_call=callbacks.get("on_tool_call"),
        on_ready=callbacks.get("on_ready"),
        on_error=callbacks.get("on_error"),
        on_close=callbacks.get("on_close"),
    )
```

`**kwargs` swallows any forward-compat fields the CLI might add (e.g.
`voice="alloy"` is passed but Gemini ignores it because Gemini has no
voice picker). Match this shape in your new provider — the CLI calls
all factories with the same kwargs.

## Cloning this plugin for a new provider

If you're being asked to add a new realtime provider (e.g. `anthropic`,
`cohere`):

1. **Copy this whole directory** to `extensions/<name>-provider/`.
2. Rename `GeminiRealtimeBridge` → `<Name>RealtimeBridge` everywhere.
3. Replace the wire format in `realtime.py`:
   - WS URL + auth (header vs query param vs OAuth bearer)
   - Setup message shape (Gemini's `setup{}` vs OpenAI's `session.update`)
   - The 16 inbound event types in `_handle_event` (some will collapse,
     others split — depends on the provider)
   - Outbound message wrappers in `send_audio`, `send_user_message`, etc.
4. Update `plugin.json`: id, env_vars, model_prefixes, signup_url.
5. Update `plugin.py`: `register_realtime_bridge("<name>", ...)` with the
   right `env_var` and `audio_sink_kwargs` (drop the kwarg if the
   provider does symmetric 16 kHz like OpenAI).
6. Add a conftest alias in `tests/conftest.py` (one 5-line wrapper).
7. Copy `tests/test_gemini_realtime_bridge.py` and adapt the wire frames.
8. Smoke test: `opencomputer voice realtime --provider <name>` should
   work end-to-end with a real API key.

## Tests for this plugin

`tests/test_gemini_realtime_bridge.py` covers:

- Setup-message shape (model, modalities, VAD config, transcripts)
- Audio frame wrapping (`realtimeInput.audio` with 16 kHz mime)
- Pending-audio flush after `setupComplete`
- Native-rate audio forward (24 kHz byte-for-byte to `on_audio`)
- Barge-in (`serverContent.interrupted` → `on_clear_audio`)
- Tool-call round-trip with name caching + name echoed in toolResponse
- Scalar-result wrapping (`{"result": value}` for non-dict results)
- Transcript chunks emitted as `final=False`; turnComplete latches finals
- VAD threshold → sensitivity enum mapping
- `extra` field defaults per-instance, accepts populated dict
- `item_id` defaults to `None` for Gemini events (the OpenAI-specific
  conversation-item id has no Gemini analog)

If you copy this file as a template, you'll get all this test coverage
for free; only the wire-frame fixtures change per provider.

## Hard rules (don't break)

1. **Forward audio byte-for-byte. Never resample in the bridge.** The
   CLI configures `LocalAudioIO` at the native output rate via
   `audio_sink_kwargs`. Resampling here would double-process audio.

2. **`item_id` stays `None`** for Gemini events. The OC contract makes
   `item_id` optional precisely because Gemini doesn't have a separate
   conversation-item id concept. Don't fake it by setting `item_id=call_id`.

3. **No provider-specific fields leak through callbacks** without
   landing in `RealtimeVoiceToolCallEvent.extra`. Cross-provider consumers
   in shared code MUST NOT branch on extra contents.

## Cross-references

- **Contract & shared layer:** [`opencomputer/voice/CLAUDE.md`](../../opencomputer/voice/CLAUDE.md)
- **The OpenAI bridge** (other reference impl): `extensions/openai-provider/realtime.py`
- **Plugin loader internals:** `opencomputer/plugins/CLAUDE.md`
- **Wire format research notes:** `/tmp/gemini-live-research/` (cleaned up after first port)
