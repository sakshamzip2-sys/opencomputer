# Voice Mode (Continuous Push-to-Talk) — Design

**Date:** 2026-04-28
**Status:** Design
**Branch:** `feat/voice-mode`
**Worktree:** `/tmp/oc-voice/`

---

## 1. Goal

Ship continuous push-to-talk voice mode for OpenComputer — closing the biggest user-visible UX gap vs. Hermes. Default OFF; user opts in with `opencomputer voice talk`. Cross-platform (mac/linux/win). Local-Whisper fallback so users without OpenAI keys can use it.

## 2. Why this and what's the gap

OC already has file-based STT/TTS in `opencomputer/voice/`. What's missing: a **continuous interactive loop** — capture audio → VAD-gate → transcribe → agent response → TTS playback → barge-in to interrupt → loop. Hermes ships this in `tools/voice_mode.py`.

Direct port + adaptation to OC's gateway/CLI patterns + local-Whisper fallback.

## 3. Architecture

### 3.1 Module shape

```
extensions/voice-mode/
├── plugin.json              (default OFF, kind=mixed)
├── plugin.py                (registration stub)
├── audio_capture.py         (sounddevice wrapper, lazy import, push-to-talk)
├── vad.py                   (webrtcvad — small, no model download)
├── stt.py                   (Whisper API + local-mlx-whisper fallback)
├── tts.py                   (existing OpenAI TTS + local-pyttsx3 fallback)
├── voice_mode.py            (main orchestrator: capture→vad→stt→agent→tts loop with barge-in)
├── README.md                (privacy contract first)
```

### 3.2 CLI

`opencomputer voice talk` — enters interactive voice loop. Uses spacebar press-and-hold for push-to-talk. Esc/Ctrl+C exits.

Existing `opencomputer voice {synthesize, transcribe, cost-estimate}` commands stay (file-based, from Tier 2.10/G.9).

### 3.3 Continuous loop (voice_mode.py)

```
[user holds spacebar]
       ↓
audio_capture.start_recording()
       ↓
[user releases]
       ↓
audio_capture.stop_recording() → wav bytes
       ↓
vad.is_speech(bytes)? if no → discard, prompt again
       ↓
stt.transcribe(bytes) → text
       ↓
agent.run_conversation(text) → response text
       ↓
tts.synthesize(response) → audio bytes
       ↓
playback.play(audio_bytes)
       ↓
[loop, with barge-in: if user presses spacebar mid-playback → interrupt]
```

### 3.4 Cross-platform audio (sounddevice)

- **macOS**: CoreAudio backend (no install needed)
- **Linux**: ALSA / PulseAudio (need PortAudio: `apt install libportaudio2`)
- **Windows**: WASAPI (works out of box with sounddevice wheel)

Doctor checks for sounddevice import + audio device available. Refuse to start in headless SSH (no audio device).

### 3.5 Privacy contract

- Audio captured locally; transcribed via Whisper API OR local mlx-whisper (configurable)
- No continuous always-on recording — VAD-gated AND push-to-talk required
- Audio buffer in-memory only; never persisted to disk
- TTS audio cached temporarily under `<profile_home>/voice/cache/` with TTL=24h
- AST no-egress test for plugin source (LLM/STT calls go through provider plugins)

### 3.6 Local-Whisper fallback

Default: OpenAI Whisper API (existing).
Fallback: `mlx-whisper` on macOS (Apple Silicon) OR `whisper-cpp` cross-platform.

Detection logic in `stt.py`:
1. Try OpenAI key → use API
2. Else: check for `mlx-whisper` package + macOS → use local
3. Else: check for `whisper-cpp` binary → use local
4. Else: raise clear error pointing user to install instructions

## 4. Phasing within T1.B

8 subtasks, ~18h total:

| # | Task | Effort |
|---|---|---|
| T1.B.1 | Plugin scaffold + CLI command stub + sounddevice audio_capture | 2h |
| T1.B.2 | VAD gating via webrtcvad | 1.5h |
| T1.B.3 | STT pipeline (API + local-mlx-whisper fallback) | 4h |
| T1.B.4 | TTS playback + barge-in detection | 3h |
| T1.B.5 | Main orchestrator loop | 2h |
| T1.B.6 | Doctor checks (sounddevice + audio device + STT backend) | 1h |
| T1.B.7 | Tests (mocked audio device + integration smoke) | 3h |
| T1.B.8 | README + CHANGELOG + CI matrix + push + PR | 1.5h |

## 5. Risks

| Risk | Mitigation |
|---|---|
| sounddevice install fails on user's machine (no PortAudio on Linux) | Lazy-import; doctor preflights; clear install message |
| VAD false-positives in noisy environments | Press-to-talk is the primary gate; VAD is secondary |
| Local-Whisper model download is large (~1GB for medium) | Use `tiny` or `base` by default (~75MB); user can opt up |
| Barge-in detection requires concurrent recording during playback | sounddevice supports duplex; use callback API |
| Headless / SSH sessions can't use audio | Doctor refuses to start; clear error |

## 6. Self-review

Default OFF; opt-in; cross-platform; local-Whisper fallback removes API key requirement; no continuous recording (VAD + push-to-talk); no disk persistence of audio buffers; AST no-egress in plugin source.

Spec is tight; ready for plan + execution.

---
