# Hermes Agent — Voice Mode Architecture Extraction

**Source:** `sources/hermes-agent/` (read-only reference clone).
**Purpose:** Document Hermes' full-duplex voice-mode so a future OC milestone can implement the deferred "live audio loop" item. All citations are `path:line` in the Hermes repo.

> **Scope note.** Hermes does *not* ship a single "voice mode" module. It ships **three** voice surfaces that share a common STT/TTS substrate:
> 1. **CLI push-to-talk + continuous VAD loop** (`cli.py`, `tools/voice_mode.py`).
> 2. **TUI gateway voice API** (`hermes_cli/voice.py`) — a process-wide wrapper around #1 for the JSON-RPC TUI.
> 3. **Discord voice-channel bridge** (`gateway/platforms/discord.py`) — the bot joins a VC, decrypts/decodes RTP audio, runs the full agent pipeline, speaks the reply back.
>
> "Full-duplex" in Hermes means *capture and playback alternate automatically with no key press* — it is **half-duplex at the audio layer** (the mic is paused while TTS plays to prevent feedback). There is no simultaneous listen-while-speaking except in the OpenAI/realtime path, which is **not** part of this `voice_mode.py` checkout.

---

## Section list

1. The audio loop (capture → STT → agent → TTS → playback)
2. Driving model: threads, not asyncio (CLI) vs asyncio+threads (gateway)
3. Barge-in / interruption handling
4. The `AudioRecorder` VAD silence-detection state machine
5. STT provider integration
6. TTS provider integration
7. Platform bridging — Discord voice channels
8. Platform bridging — Matrix / Telegram / Slack voice messages
9. Per-platform isolation (`_voice_key`)
10. Voice-call session lifecycle / state machine
11. CLI surface (`/voice` command + Ctrl+B keybinding)
12. Dependencies
13. Failure modes and handling
14. What an OC port would need

---

## 1. The audio loop

ASCII diagram of the **CLI continuous loop** (the canonical full-duplex path):

```
                       ┌───────────────────────────────────────────────────┐
                       │              CONTINUOUS VOICE LOOP (CLI)           │
                       └───────────────────────────────────────────────────┘

   user presses Ctrl+B               (cli.py:10496 handle_voice_record)
        │
        ▼
  _voice_continuous = True
  _voice_start_recording()  ──► play_beep(880Hz)         (cli.py:8253)
        │                        sd.InputStream opens
        ▼                        callback appends PCM frames + computes RMS
  ┌─────────────────┐
  │  AudioRecorder  │  callback runs in PortAudio's own thread
  │  (sounddevice   │  ── RMS-based VAD ──► silence after speech?
  │   InputStream)  │                          │ yes
  └─────────────────┘                          ▼
        ▲                        on_silence_stop()  fired in a *daemon thread*
        │                                      │     (voice_mode.py:532-542)
        │                                      ▼
        │                        _voice_stop_and_transcribe()   (cli.py:8286)
        │                          ├─ recorder.stop() → WAV file
        │                          ├─ play_beep(660Hz ×2)
        │                          └─ transcribe_recording(wav) ─┐
        │                                                        ▼
        │                                          tools.transcription_tools
        │                                          .transcribe_audio()   (STT)
        │                                                        │
        │                          hallucination filter ◄────────┘
        │                          (voice_mode.py:772 is_whisper_hallucination)
        │                                      │ transcript ok
        │                                      ▼
        │                          _pending_input.put(transcript)   (cli.py:8339)
        │                                      │
        │                                      ▼
        │                          process_loop picks it up → agent turn
        │                          (full agent: tools, memory, session)
        │                                      │ agent reply text
        │                                      ▼
        │                          _voice_speak_response(text)      (cli.py:8386)
        │                            ├─ strip markdown / code / URLs
        │                            ├─ text_to_speech_tool() → MP3   (TTS)
        │                            ├─ _voice_tts_done.clear()
        │                            └─ play_audio_file(mp3)  ◄─ interruptable
        │                                      │ playback done
        │                                      ▼
        │                          _voice_tts_done.set()
        │                                      │
        └──────────────────────────────────────┘
          process_loop restarts recording in a daemon thread,
          *after* _voice_tts_done.wait() + 0.3s settle  (cli.py:11565-11575)
```

The loop is **event-driven, not polled**: the only "wait" is the PortAudio callback firing on every audio chunk. Each stage hands off to the next via a callback or a queue.

Stages, in code:

| Stage | Where | Output |
|-------|-------|--------|
| Capture | `AudioRecorder._ensure_stream` / `start` (`voice_mode.py:435,566`) | int16 PCM frames in `self._frames` |
| End-of-speech detection | `AudioRecorder._callback` (`voice_mode.py:448-542`) | fires `on_silence_stop()` callback |
| WAV encode | `AudioRecorder._write_wav` (`voice_mode.py:703`) | 16 kHz mono WAV in `/tmp/hermes_voice/` |
| STT | `transcribe_recording` → `transcription_tools.transcribe_audio` (`voice_mode.py:789`) | `{success, transcript}` dict |
| Hallucination filter | `is_whisper_hallucination` (`voice_mode.py:772`) | drops phantom transcripts |
| Agent turn | `process_loop` consumes `_pending_input` queue (`cli.py:11454`) | reply text |
| TTS synth | `tools.tts_tool.text_to_speech_tool` (`cli.py:8419`) | MP3 file (whole reply). ElevenLabs instead streams sentence-by-sentence via `stream_tts_to_speaker` — see §3. |
| Playback | `play_audio_file` (`voice_mode.py:843`) | audio out, interruptable |

---

## 2. Driving model — threads, not asyncio

The CLI voice loop is **entirely thread-based**. There is no event loop in `tools/voice_mode.py`.

- **PortAudio callback thread.** `sd.InputStream(callback=_callback)` (`voice_mode.py:547`). PortAudio owns this thread and calls `_callback` on every audio buffer. The callback does *all* RMS/VAD math inline.
- **Silence-callback daemon thread.** When VAD decides speech ended, the callback spawns `threading.Thread(target=_safe_cb, daemon=True)` (`voice_mode.py:542`). It does **not** call the callback inline — that would block PortAudio's audio thread during STT.
- **Recording restart daemon thread.** After an agent turn, `process_loop` spawns a daemon thread to call `_voice_start_recording` (`cli.py:11566-11575`). The comment is explicit: `play_beep` (which does `sd.wait`) and `AudioRecorder.start` (lock acquire) **must not** block `process_loop`.
- **TTS daemon thread.** `_voice_speak_response` runs in a background thread spawned at `cli.py:9547`.
- **Keybinding thread-safety.** The Ctrl+B handler runs in **prompt_toolkit's event-loop thread**; every heavy action is dispatched to a daemon thread (`cli.py:10500-10553`) so the UI never freezes.

Locking: a single `threading.Lock` (`self._voice_lock`, `cli.py:2281`) guards the boolean state machine (`_voice_recording`, `_voice_processing`, `_voice_continuous`). `AudioRecorder` has its own `self._lock` (`voice_mode.py:392`).

Synchronisation primitive for feedback prevention: `threading.Event` — `self._voice_tts_done` (CLI, `cli.py:2288`) and `_tts_playing` (gateway, `hermes_cli/voice.py:109`). Cleared while TTS plays, set when silent. Initial state = **set** ("not playing").

The **Discord** bridge (Section 7) *does* use asyncio — the gateway is async — but the RTP packet handler still runs in discord.py's `SocketReader` **thread**, and STT/TTS are pushed off the loop via `asyncio.to_thread` (`discord.py:1863,1866`).

---

## 3. Barge-in / interruption handling

Barge-in is **not** acoustic (the agent does not stop speaking because it hears you). It is **key-driven** and **state-driven**:

1. **Manual barge-in (Ctrl+B during TTS).** If the user presses the record key while TTS is playing, the handler calls `stop_playback()` and sets `_voice_tts_done` (`cli.py:10529-10537`). `stop_playback()` (`voice_mode.py:823`) terminates the active playback subprocess (`_active_playback.terminate()`) and calls `sd.stop()`. A module-global `_active_playback: subprocess.Popen` + `_playback_lock` track the running player so it can be killed from another thread.
2. **Feedback-loop prevention (the real "interruption" concern).** When TTS plays the agent's reply over the speakers, the live mic would capture it and transcribe the agent's own voice — an infinite loop. Three defences:
   - The mic is **cancelled before** TTS starts: `speak_text` calls `_continuous_recorder.cancel()` before opening the speakers (`hermes_cli/voice.py:468-479`).
   - The continuous loop **waits on the TTS-done Event** before re-arming: `_tts_playing.wait(timeout=60)` then a `0.3s` settle (`hermes_cli/voice.py:408-412`; CLI equivalent `cli.py:11568-11570`).
   - Discord's `VoiceReceiver` has `pause()`/`resume()` (`discord.py:192-196`); `play_in_voice_channel` pauses the receiver around playback (`discord.py:1663-1697`).
3. **Re-arm after TTS.** `speak_text` restarts the recorder in its `finally` block once `paused_recording` is true and the loop is still active (`hermes_cli/voice.py:536-548`).

Mid-sentence interruption depends on the TTS provider. Streaming **sentence-by-sentence** TTS *is* implemented, but it is **provider-gated to ElevenLabs**: when ElevenLabs is the configured TTS provider and `sounddevice` is importable, the CLI sets `use_streaming_tts` and runs `stream_tts_to_speaker` in a daemon thread, which buffers the agent's generated tokens into sentences and streams each sentence to the speaker as it completes (`cli.py:9157-9214` streaming setup; `tts_tool.py:1916` `stream_tts_to_speaker`, `_speak_sentence` ~`tts_tool.py:1991`). For **all other** TTS providers the fallback path is `_voice_speak_response` — the whole reply text synthesised and played as one MP3 — selected at `cli.py:9545` (`if self._voice_tts and response and not use_streaming_tts:`). This non-streaming fallback path has **no** mid-sentence interruption.

---

## 4. The `AudioRecorder` VAD silence-detection state machine

`AudioRecorder` (`voice_mode.py:373-721`) is the heart of the loop. It is a **two-stage RMS VAD** that runs inside the PortAudio callback.

**Parameters** (`voice_mode.py:185-192`):
- `SAMPLE_RATE = 16000` (Whisper-native), `CHANNELS = 1`, `DTYPE = int16`.
- `SILENCE_RMS_THRESHOLD = 200` (int16 0-32767).
- `SILENCE_DURATION_SECONDS = 3.0`.
- `_min_speech_duration = 0.3s`, `_max_dip_tolerance = 0.3s`, `_max_wait = 15.0s`.

**Stage 1 — speech confirmation** (`voice_mode.py:467-491`): audio must stay above the RMS threshold for ≥ `0.3s`. Brief dips between syllables (≤ `0.3s`) are tolerated via `_dip_start`. Once confirmed, `_has_spoken = True`.

**Stage 2 — end detection** (`voice_mode.py:519-526`): after speech is confirmed, `3.0s` of continuous sub-threshold audio fires the callback. "Resume" tracking (`_resume_start`, `_resume_dip_start`) lets sustained renewed speech reset the silence timer, while brief noise spikes do not.

**No-speech timeout** (`voice_mode.py:527-530`): if no speech at all for `_max_wait = 15s`, fire anyway (so the loop does not hang on an empty room).

**Fire-once semantics** (`voice_mode.py:532-542`): the callback is consumed (`_on_silence_stop = None`) under the lock and dispatched on a daemon thread.

**Persistent-stream trick** (`voice_mode.py:435-444`): the `InputStream` is opened **once** and kept alive for the recorder's lifetime. Between recordings the callback just discards frames (`if not self._recording: return`). Reason in the docstring: *closing and reopening an `InputStream` hangs indefinitely on macOS CoreAudio*. `shutdown()` (`voice_mode.py:691`) closes it via `_close_stream_with_timeout` (a 3 s polled join).

**`stop()` quality gates** (`voice_mode.py:636-677`): discards recordings < `0.3s` of audio, and discards recordings whose **peak** RMS < threshold (peak, not average — average is diluted by trailing silence).

There is a second backend, `TermuxAudioRecorder` (`voice_mode.py:245-367`), for Android/Termux: it shells out to `termux-microphone-record` (AAC output), has **no** live VAD (`supports_silence_autostop = False`). `create_audio_recorder()` (`voice_mode.py:724`) picks the backend.

---

## 5. STT provider integration

`tools/transcription_tools.py` is the shared STT layer (used by CLI, gateway, and the voice-channel bridge). Six providers:

| Provider | Backend | Key env var | Notes |
|----------|---------|-------------|-------|
| `local` (default) | `faster-whisper`, in-process | none | Auto-downloads model (~150 MB `base`). Singleton model cached (`transcription_tools.py:104`). CUDA→CPU fallback. |
| `local_command` | external `whisper` binary | none | Shell command template (`HERMES_LOCAL_STT_COMMAND`). |
| `groq` | Groq Whisper API (OpenAI-compatible) | `GROQ_API_KEY` | `whisper-large-v3-turbo` default. |
| `openai` | OpenAI Whisper API | `VOICE_TOOLS_OPENAI_KEY` | `whisper-1` default. |
| `mistral` | Mistral Voxtral | `MISTRAL_API_KEY` | |
| `xai` | xAI Grok STT | `XAI_API_KEY` | diarization, 21 langs. |

**Provider selection** — `_get_provider(stt_config)` (`transcription_tools.py:200-280`):
- If `stt.provider` is **explicitly set** in `~/.hermes/config.yaml`, that choice is honoured — no silent cloud fallback. If the chosen provider is unavailable (missing package/key), it returns `"none"` and logs a warning.
- If **no** provider is configured, auto-detect priority is **`local > local_command > groq > openai > mistral > xai`**.

**Dispatch** — `transcribe_audio(file_path, model)` (`transcription_tools.py:789`) routes to `_transcribe_local` / `_transcribe_groq` / etc. Returns `{"success": bool, "transcript": str, "error": str?}`.

Swapping a provider = editing `stt.provider` (+ `stt.local.model`) in `config.yaml`. No code change. The voice loop never names a provider; it calls `transcribe_audio` and lets config decide.

---

## 6. TTS provider integration

`tools/tts_tool.py` — `text_to_speech_tool(text, output_path)`. Default provider `edge`. Providers (`tts_tool.py:5-23`, dispatch `tts_tool.py:1628-1697`):

| Provider | Type | Key | Notes |
|----------|------|-----|-------|
| `edge` (default) | cloud, free | none | Microsoft Edge neural voices. Needs ffmpeg for Opus. |
| `elevenlabs` | cloud, paid | `ELEVENLABS_API_KEY` | premium. |
| `openai` | cloud, paid | OpenAI key | `gpt-4o-mini-tts`. |
| `minimax` | cloud, paid | `MINIMAX_API_KEY` | voice cloning. |
| `mistral` | cloud | `MISTRAL_API_KEY` | native Opus. |
| `gemini` | cloud | `GEMINI_API_KEY` | PCM/L16 output. |
| `xai` | cloud | `XAI_API_KEY` | Grok voices. |
| `neutts` | local, free | none | on-device, downloads model. |
| `kittentts` | local, free | none | 25 MB model. |
| `piper` | local, free | none | VITS, 44 languages. |
| `<custom>` | `type: command` | n/a | arbitrary shell command per `tts.providers.<name>`. |

Output: **Opus/OGG** for Telegram/Discord voice bubbles, **MP3** for CLI/Discord-VC playback. The CLI deliberately requests an MP3 path even when the tool auto-converts to OGG, because `afplay`'s OGG support is flaky (`hermes_cli/voice.py:501-509`, `cli.py:8411-8417`).

Provider swap = edit `tts.provider` in `config.yaml`.

---

## 7. Platform bridging — Discord voice channels

This is the most complex surface and the **main thing OC lacks**. Lives in `gateway/platforms/discord.py`.

### 7.1 `VoiceReceiver` — inbound audio (`discord.py:121-470`)

The bot joins a VC and must decrypt + decode the raw RTP/UDP stream itself (discord.py has no built-in voice-receive). Pipeline per packet (`_on_packet`, `discord.py:243-375`, runs in discord.py's `SocketReader` thread):

```
  UDP packet ─► RTP header parse (version=2, payload-type=0x78)   discord.py:261
            ─► skip bot's own SSRC                                discord.py:270
            ─► NaCl transport decrypt                             discord.py:307-314
               (aead_xchacha20_poly1305_rtpsize, nacl.secret.Aead)
            ─► strip RTP padding (RFC 3550 §5.1)                   discord.py:325-343
            ─► DAVE E2EE decrypt (per-user, lib `davey`)           discord.py:346-360
            ─► Opus decode → PCM   (discord.opus.Decoder per SSRC) discord.py:365-374
            ─► append to per-SSRC bytearray buffer                 discord.py:371
```

- **SSRC → user mapping.** Discord identifies each speaker by RTP SSRC. The mapping arrives in `SPEAKING` websocket events (opcode 5). `_install_speaking_hook` (`discord.py:206-237`) wraps the voice-websocket hook to capture them. If a user was already speaking when the bot (re)joined, `_infer_user_for_ssrc` (`discord.py:381-405`) maps the SSRC to the sole allowed channel member.
- **Per-user Opus decoders.** Each SSRC gets its own `discord.opus.Decoder()` (`discord.py:368`) — decoder state is per-stream.
- **Silence detection.** `check_silence()` (`discord.py:407-438`): an utterance is "complete" after `SILENCE_THRESHOLD = 1.5s` of no packets, given `MIN_SPEECH_DURATION = 0.5s` of buffered audio. (Different constants than the CLI's 3.0 s / 0.3 s — Discord audio is 48 kHz stereo, `discord.py:132-133`.)
- **PCM → WAV.** `pcm_to_wav` (`discord.py:444-470`) shells to `ffmpeg` to downsample 48 kHz stereo → 16 kHz mono WAV for Whisper.

### 7.2 The listen loop (`discord.py:1823-1888`)

`_voice_listen_loop` is an **asyncio task** per guild that polls `receiver.check_silence()` every `0.2s`, sends a UDP keepalive every `_KEEPALIVE_INTERVAL` (so Discord doesn't drop the UDP session after ~60 s silence), and for each completed utterance calls `_process_voice_input` → `pcm_to_wav` → `transcribe_audio` (both via `asyncio.to_thread`) → hallucination filter → `_voice_input_callback`.

### 7.3 Bridge into the agent (`gateway/run.py:8264-8321`)

`_voice_input_callback` is wired by the runner to `GatewayRunner._handle_voice_channel_input`. It:
1. Builds a synthetic `MessageEvent` with `message_type = MessageType.VOICE`, reusing the **linked text channel's** `SessionSource` so voice and text share one session.
2. Echoes the transcript into the text channel as `**[Voice]** <@user>: …` (with `@everyone`/`@here` sanitisation).
3. Feeds it through `adapter.handle_message(event)` — the **full** normal pipeline (session, tools, memory, agent).

### 7.4 Reply playback (`discord.py:1656-1697`, `run.py:8377-8417`)

`_send_voice_reply` synthesises TTS, and if the bot is in a VC, calls `play_in_voice_channel`, which: pauses the `VoiceReceiver` (echo prevention) → waits for any current playback → `vc.play(discord.FFmpegPCMAudio(path))` → awaits an `asyncio.Event` set by the `after=` callback → resumes the receiver.

### 7.5 Join/leave (`discord.py:1599-1652`, `run.py:8183-8260`)

`/voice join` → `join_voice_channel` → `channel.connect()` → start `VoiceReceiver` + listen task. Per-guild `asyncio.Lock` serialises join/leave. Auto-disconnect after `VOICE_TIMEOUT = 300s` of inactivity (`_voice_timeout_handler`, `discord.py:1720`).

---

## 8. Platform bridging — Matrix / Telegram / Slack voice messages

These platforms have **no live audio channel** — voice is exchanged as discrete **voice-message files**, not a duplex stream.

- **Inbound.** A user sends a voice clip; the adapter downloads it, caches it locally, tags the `MessageEvent` as `MessageType.VOICE`, and the gateway auto-transcribes it via `transcribe_audio`. Matrix detects voice via the MSC3245 `org.matrix.msc3245.voice` content field (`tests/gateway/test_matrix_voice.py:122-165`); regular `m.audio` stays `MessageType.AUDIO` (`test_matrix_voice.py:167-184`). On download failure it falls back to the HTTP URL (`test_matrix_voice.py:230-273`).
- **Outbound.** `send_voice` synthesises TTS to OGG/Opus and uploads it as a native voice bubble. Matrix's `send_voice` re-attaches the MSC3245 field for native rendering (`test_matrix_voice.py:298-331`).
- **Modes.** Per-chat `voice_mode` ∈ `{off, voice_only, all}`. `voice_only` = speak only when the user sent a voice message; `all` = speak every reply. `_should_send_voice_reply` (`run.py:8323-8375`) enforces this, with dedup against the agent already calling the `text_to_speech` tool.

So "full-duplex voice" is **Discord-only**. Telegram/Slack/Matrix are async voice-message exchange.

---

## 9. Per-platform isolation (`_voice_key`)

Voice-mode state is keyed by `platform:chat_id`, **not** bare `chat_id` (`test_voice_mode_platform_isolation.py` — fixes bug #12542 where Telegram chat `123` and Slack chat `123` collided).

- `GatewayRunner._voice_key(platform, chat_id)` → `"telegram:123"` (`run.py:1232`).
- State dict `self._voice_mode: Dict[str, str]` persisted to `~/.hermes/gateway_voice_mode.json` (`run.py:1230,1262`).
- `_load_voice_modes` (`run.py:1236`) **skips legacy unprefixed keys** and logs a warning, and filters invalid mode values (`test_voice_mode_platform_isolation.py:65-139`).
- `_sync_voice_mode_state_to_adapter` (`run.py:1303`) syncs only the entries whose prefix matches that adapter's platform into the adapter's `_auto_tts_disabled_chats` set.

CLI voice state is a separate concern — plain instance booleans on the CLI object (`_voice_mode`, `_voice_tts`, `_voice_continuous`), no persistence.

---

## 10. Voice-call session lifecycle / state machine

There is no formal `StateMachine` class. State is a set of booleans + Events. Effective states:

**CLI (`cli.py` instance flags):**

```
        OFF ──/voice on──► IDLE ──Ctrl+B──► RECORDING ──silence/Ctrl+B──► PROCESSING
         ▲                  ▲                                                │
         │                  │                                          (STT + agent)
    /voice off          (loop)                                               │
         │                  │                                                ▼
         └──────────────────┴───── re-arm after TTS ◄──── SPEAKING (TTS) ◄────┘
```

Flags: `_voice_mode` (master on/off), `_voice_recording`, `_voice_processing`, `_voice_continuous` (auto re-arm), `_voice_tts` (speak replies). Event `_voice_tts_done`. 3 consecutive no-speech cycles drop `_voice_continuous` (`cli.py:8362-8368`).

**Gateway continuous loop (`hermes_cli/voice.py`):** `_continuous_active` bool, statuses emitted to a callback: `"listening" → "transcribing" → "idle"`. `_CONTINUOUS_NO_SPEECH_LIMIT = 3` (`hermes_cli/voice.py:115`) → loop self-stops and fires `on_silent_limit`.

**Discord VC (`discord.py` per-guild dicts):** `_voice_clients`, `_voice_receivers`, `_voice_listen_tasks`, `_voice_timeout_tasks`, `_voice_text_channels`. Lifecycle: `join → listen-loop running → (utterance → agent → speak)* → idle-timeout 300 s → leave`.

---

## 11. CLI surface

`/voice [on|off|tts|status]` — handler `_handle_voice_command` (`cli.py:8438`):
- `/voice on` → `_enable_voice_mode` (`cli.py:8472`) — runs `check_voice_requirements`, may auto-enable TTS if `voice.auto_tts: true`.
- `/voice off` → `_disable_voice_mode` (`cli.py:8532`) — stops playback, resets flags.
- `/voice tts` → `_toggle_voice_tts` (`cli.py:8564`).
- `/voice status` → prints requirement diagnostics (`cli.py:8584`).

**Record key:** Ctrl+B by default, configurable via `voice.record_key` in `config.yaml`. The prompt_toolkit binding (`cli.py:10496`) translates `"ctrl+b"` → `"c-b"`. Pressing it toggles record/stop; pressing during a recording also drops `_voice_continuous` (manual exit from the loop).

`hermes_cli/voice.py` is the **process-wide API for the TUI gateway**, not a CLI command file. It exposes:
- Push-to-talk: `start_recording()` / `stop_and_transcribe() -> str`.
- Continuous VAD: `start_continuous(on_transcript, on_status, on_silent_limit, ...)` / `stop_continuous()` / `is_continuous_active()`.
- `speak_text(text)` — TTS with the feedback-guard logic.
The TUI gateway's `voice.record` / `voice.toggle` / `voice.tts` JSON-RPC handlers call these from a dedicated thread.

There is **no** top-level `hermes voice` subcommand in this checkout — voice is a `/voice` slash command inside the interactive CLI/TUI.

---

## 12. Dependencies

**Audio (optional — `hermes-agent[voice]`):**
- `sounddevice` + `numpy` — mic capture & WAV/beep playback. Lazy-imported (`_import_audio`, `voice_mode.py:32`) so headless boxes don't crash.
- System: **PortAudio** (`libportaudio2` / `brew install portaudio`).
- Playback fallbacks: `afplay` (macOS), `ffplay`, `aplay` (Linux ALSA).

**STT:** `faster-whisper` (local, default) — pure-pip, downloads model. Or cloud (`openai` SDK reused for Groq, `mistralai`).

**TTS:** `edge-tts` (default), `elevenlabs`, `openai`, `mistralai`, `kittentts`, `piper`/`piper-tts`, `neutts`. **ffmpeg** for MP3↔Opus.

**Discord voice (`hermes-agent[messaging]` → `discord.py[voice]`):**
- `PyNaCl` (`nacl.secret.Aead`) — RTP transport decryption.
- `davey` — DAVE end-to-end encryption decrypt.
- **Opus** codec library (`libopus`) — auto-loaded from `/opt/homebrew/lib/libopus.dylib` (macOS) or `libopus.so.0` (Linux).
- `ffmpeg` — PCM→WAV downsample and `FFmpegPCMAudio` playback source.

**Termux:** `termux-api` package + Termux:API Android app (`termux-microphone-record`).

**Not in this checkout:** no LiveKit, no WebRTC, no `pyaudio`. Realtime/OpenAI-Realtime voice is referenced in docs but the duplex socket is not in `voice_mode.py`.

---

## 13. Failure modes and handling

| Failure | Detection | Handling | Cite |
|---------|-----------|----------|------|
| No audio libs / no PortAudio | `detect_audio_environment`, `_audio_available` | Hard-fail with install hints; voice mode refuses to start | `voice_mode.py:88-180` |
| Headless (SSH/Docker/WSL) | env-var / `/proc/version` / `is_container` probes | Blocked unless WSL has `PULSE_SERVER` | `voice_mode.py:101-145` |
| macOS CoreAudio stream-close hang | known bug | Stream kept alive for recorder lifetime; close via 3 s polled-join thread | `voice_mode.py:435-444, 612-634` |
| `sd.wait()` hangs forever on stalled device | known bug | Replaced with `time.monotonic()`-bounded poll + `sd.stop()` | `voice_mode.py:233-237, 873-878` |
| Whisper hallucination on silence | `is_whisper_hallucination` — 26-phrase set + repeat regex | Transcript replaced with `""`, `filtered: True` | `voice_mode.py:735-811` |
| Recording too short / too quiet | `stop()` quality gates | Returns `None`, recording discarded | `voice_mode.py:664-675` |
| TTS→mic feedback loop | `_tts_playing` / `_voice_tts_done` Event | Mic cancelled before TTS; loop waits on Event + 0.3 s before re-arm | `hermes_cli/voice.py:408-412, 468-479` |
| Infinite empty-room loop | `_no_speech_count` / `_CONTINUOUS_NO_SPEECH_LIMIT` | 3 silent cycles stop continuous mode | `cli.py:8362-8368`, `hermes_cli/voice.py:373` |
| STT provider misconfigured | `_get_provider` returns `"none"` | Warning logged; explicit choice never silently falls back to cloud | `transcription_tools.py:216-272` |
| Discord RTP decrypt failure | NaCl/DAVE/Opus exceptions | Packet dropped, debug-logged (first 10 only); loop continues | `discord.py:311-374` |
| Discord SSRC not mapped (rejoin) | missing SPEAKING event | `_infer_user_for_ssrc` maps the sole allowed member | `discord.py:381-405` |
| Discord UDP session drop after 60 s | timer | Periodic `b'\xf8\xff\xfe'` keepalive packet | `discord.py:1833-1843` |
| Stuck playback | `PLAYBACK_TIMEOUT = 120 s` / 300 s subprocess timeout | `vc.stop()` / `proc.kill()` | `discord.py:1670-1692`, `voice_mode.py:902-909` |
| Background-thread broken stderr pipe | `_debug` | `BrokenPipeError`/`OSError` swallowed | `hermes_cli/voice.py:54-58` |
| Unauthorized voice speaker | `_is_allowed_user` / `_is_user_authorized` | Audio silently ignored (`DISCORD_ALLOWED_USERS`) | `discord.py:1847,1890`, `run.py:8297` |

General philosophy: **lazy imports** everywhere so missing optional deps surface at call-time, not startup; **best-effort** background work (beeps, debug, TTS) never kills the loop; **bounded waits** replace every unbounded `wait()`.

---

## 14. What an OC port would need

**OC's existing voice tooling** (surveyed in this worktree):
- `extensions/voice-mode/` — already has `audio_capture.py`, `vad.py`, `stt.py`, `tts_playback.py`, and an orchestrator `voice_mode.py` with `run_single_turn` / `run_voice_loop`, plus `slash_commands/voice_cmd.py`. This is a **CLI/local** continuous loop comparable to Hermes' `hermes_cli/voice.py` + `tools/voice_mode.py`. **Roughly at parity for surface #1.**
- `opencomputer/voice/` — `stt.py`, `tts.py`, `edge_tts.py`, `tts_piper.py`, `groq_stt.py`, `stt_mlx_whisper.py`, `tts_command.py`; `opencomputer/tools/voice_transcribe.py` + `voice_synthesize.py` — the STT/TTS substrate (analogous to `transcription_tools.py` + `tts_tool.py`).
- `opencomputer/cli_voice.py` — CLI voice entry (804 lines).
- `plugin_sdk/realtime_voice.py` — a realtime-voice surface.
- Channel adapters in `extensions/discord/`, `extensions/dingtalk/`, `extensions/feishu/`, plus Matrix/Telegram/Slack adapters.

So the **deferred gap is not "voice tools" — OC has those — it is the platform-bridged live audio loop**, specifically Discord voice channels and a gateway-side continuous VAD loop. Port mapping:

| Hermes piece | OC target | Work needed |
|--------------|-----------|-------------|
| `tools/voice_mode.py` `AudioRecorder` + VAD | `extensions/voice-mode/audio_capture.py` + `vad.py` | Already exists. **Audit** for the macOS CoreAudio persistent-stream trick and the `sd.wait()` bounded-poll fix — these are non-obvious bug fixes worth copying verbatim. |
| `is_whisper_hallucination` 26-phrase filter | OC STT layer (`opencomputer/voice/stt.py`) | Port the phrase set + repeat regex. Cheap, high-value. |
| `hermes_cli/voice.py` continuous loop (gateway side) | A new gateway-side voice module | OC's `extensions/voice-mode` loop is CLI-oriented. Needs a **process-wide, callback-driven** wrapper the gateway can drive from a thread, with the `_tts_playing` feedback-guard Event. |
| `gateway/platforms/discord.py` `VoiceReceiver` | New module under `extensions/discord/` | **The big lift.** RTP parse + NaCl decrypt (`PyNaCl`) + DAVE decrypt (`davey`) + per-SSRC Opus decode + SSRC→user mapping via SPEAKING hook + silence-poll. Requires `discord.py[voice]`, `libopus`, `ffmpeg`. ~350 LOC. discord.py has no built-in receive — this must be hand-rolled. |
| Discord `_voice_listen_loop` + `_process_voice_input` | `extensions/discord/` | asyncio task: poll `check_silence`, UDP keepalive, `asyncio.to_thread(transcribe)`, fire callback. |
| `_handle_voice_channel_input` synthetic `MessageEvent` | OC gateway runner | Bridge transcript → synthetic channel event → existing agent pipeline → reply → `play_in_voice_channel`. OC's channel-adapter architecture already has the message-event abstraction; reuse it. |
| `play_in_voice_channel` (`FFmpegPCMAudio` + receiver pause) | `extensions/discord/` | TTS → MP3 → `vc.play`; **pause the receiver around playback** (echo prevention). |
| `_voice_key` platform-prefixed state | OC gateway state | Key voice-mode state by `platform:chat_id` from day one — Hermes shipped a collision bug (#12542) by not doing this. Persist to a JSON file with legacy-key skipping. |
| `_should_send_voice_reply` modes (`off`/`voice_only`/`all`) | OC gateway | Per-chat mode + dedup against the agent's own `text_to_speech` tool call. |
| Matrix MSC3245 voice tagging | OC Matrix adapter | Detect `org.matrix.msc3245.voice`; re-attach on `send_voice`. |
| `detect_audio_environment` headless/WSL/Termux gating | OC voice entry | Hermes has thorough environment detection — port the SSH/Docker/WSL/Termux probes so voice fails gracefully on servers. |

**Architectural guidance for OC:**
1. **Keep the audio layer thread-based even though OC's gateway is async.** PortAudio and discord.py's `SocketReader` both call back on their own threads. Push STT/TTS off any event loop with `to_thread`.
2. **Half-duplex by design.** Pause the mic / receiver while TTS plays; gate re-arm on a "TTS done" `Event` + a small settle delay. Acoustic echo cancellation is *not* attempted — and need not be.
3. **One shared STT/TTS substrate.** Every surface (CLI, gateway voice-message, Discord VC) must call the same `transcribe` / `synthesize` functions so a config-level provider change applies everywhere. OC already has `opencomputer/voice/` — route the new Discord bridge through it.
4. **Provider choice is config, never code.** `stt.provider` / `tts.provider` in config; explicit choice does not silently fall back to a paid cloud provider.
5. **Copy the non-obvious bug fixes:** macOS persistent-`InputStream`, bounded `sd.wait` poll, no-speech-count loop breaker, RTP-padding strip, UDP keepalive, hallucination filter. These are battle-tested and non-trivial to rediscover.

**Estimated port size:** STT/TTS substrate and CLI loop ≈ already present. The net-new work is the Discord `VoiceReceiver` + listen loop + gateway bridge + per-platform voice-mode state ≈ **600–800 LOC plus 3 new dependencies** (`discord.py[voice]`/`PyNaCl`/`libopus`, `davey`, `ffmpeg`). Matrix/Telegram voice-message support is much smaller (adapter-local, no live stream).
