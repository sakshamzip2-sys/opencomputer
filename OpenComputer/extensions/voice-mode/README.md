# Voice Mode (continuous push-to-talk)

Default OFF. Opt in with `opencomputer voice talk`.

## What this does

Continuous push-to-talk voice loop:

1. Press SPACEBAR (or Enter in terminal mode) to start recording.
2. Release to stop and send.
3. VAD-gates non-speech audio.
4. Transcribes via Whisper (API or local).
5. Agent responds; TTS plays through speakers.
6. Press SPACEBAR mid-playback to barge-in (interrupt + record again).

## What this does NOT do

| Thing | Status |
|---|---|
| Continuous always-on recording | No — VAD-gated AND push-to-talk required |
| Persist audio buffers to disk | No — in-memory only |
| Send audio to network (other than configured Whisper API) | No — AST-enforced no direct egress |
| Auto-take voice commands without playback | No — every turn shows the transcript before the agent runs |
| Train any model on collected audio | No |

The "no direct network egress" rule is enforced by
`tests/test_voice_mode_no_egress.py` — a CI guard that AST-scans this
directory for HTTP-client imports. Adding networking here is a contract
break, not just a code change; it requires updating the deny-list, this
README, and the CHANGELOG.

## Privacy contract

| Captured | Storage | Where it goes |
|---|---|---|
| Audio buffer | RAM only — never persisted | OpenAI Whisper API OR local mlx-whisper / whisper-cpp |
| Transcript | Session DB (per existing OC privacy rules) | Agent loop |
| Agent response | Session DB | TTS API → temp file → playback → unlink |

The OpenAI Whisper path goes through `opencomputer.voice.stt`, which is
the cost-guarded shared client. The plugin source itself imports zero
HTTP/network libraries — that's verified by the AST guard above.

## STT backends (auto-detected)

1. **OpenAI Whisper API** (default if `OPENAI_API_KEY` is set).
2. **mlx-whisper** (macOS Apple Silicon — `pip install opencomputer[voice-mlx]`).
3. **whisper-cpp** (cross-platform — `pip install opencomputer[voice-local]`).

Force local with `opencomputer voice talk --local` to skip the API even
when a key is set.

## Platform support

| Platform | Status |
|---|---|
| macOS | Supported (CoreAudio) |
| Linux | Supported (needs `apt install libportaudio2`) |
| Windows | Supported (WASAPI, no extra install) |
| SSH / headless | NOT supported (no audio device) — doctor refuses |

## Install

```bash
pip install opencomputer[voice]
# Optional local STT:
pip install opencomputer[voice-local]   # cross-platform
pip install opencomputer[voice-mlx]     # macOS Apple Silicon
```

## Usage

```bash
opencomputer voice talk                # API-first
opencomputer voice talk --local        # local-first
```

Press Q or Ctrl+C to exit.

## Troubleshooting

- **"sounddevice not installed"** — `pip install opencomputer[voice]`.
- **"PortAudio missing"** (Linux) — `sudo apt install libportaudio2`.
- **"no audio input device"** — running headless? voice-mode requires a
  real microphone.
- **"no STT backend"** — set `OPENAI_API_KEY` or install a local backend
  extra.

`opencomputer doctor` runs the same preflight (`voice-mode` row) and
points you at the exact install command for whatever's missing.

## Disabling

Voice-mode is opt-in. There's no daemon to stop — when you exit
`opencomputer voice talk`, nothing keeps running.
