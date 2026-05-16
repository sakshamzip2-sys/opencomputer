# Voice — local speech synthesis with NeuTTS

OpenComputer can synthesize speech **locally** — on-device, with no API call
and no per-call cost — via [NeuTTS](https://github.com/neuphonic/neutts), an
on-device neural text-to-speech model. This is Milestone 4 of the
Hermes + OpenClaw parity plan; it complements the existing cloud TTS
(`VoiceSynthesize`, OpenAI) and Edge TTS paths.

NeuTTS is **opt-in**: it lives behind the `[neutts]` optional extra. Without
it installed, the `VoiceSynthesizeLocal` tool is not registered and
OpenComputer behaves exactly as before — zero change for users who don't
want it.

## Install

```bash
pip install opencomputer[neutts]
```

This pulls NeuTTS and its ML stack (`torch`, `transformers`) — a heavy
install, which is why it is an optional extra rather than a base dependency.
NeuTTS also needs the `espeak-ng` system package — install it with your OS
package manager (`brew install espeak-ng`, `apt install espeak-ng`, …).

Then pre-download the model weights:

```bash
oc voice install-neutts
```

The backbone + codec weights (a few hundred MB) are fetched from HuggingFace.
Running this once front-loads the one-time download so the first synthesis is
not a surprise wait. `--backbone <repo>` and `--device <cpu|cuda>` override
the defaults.

## Synthesizing speech

NeuTTS is a **voice-cloning** model: it has no fixed voice — it synthesizes
speech in the voice of a *reference clip* you supply. Both a reference audio
file and that clip's transcript are therefore required.

The agent reaches local synthesis through the **`VoiceSynthesizeLocal`** tool:

| Parameter | Required | Meaning |
|---|---|---|
| `text` | yes | The text to speak. |
| `reference_audio` | yes | Path to a `.wav` — 3–15 s of clean, continuous mono speech. The synthesized voice clones this speaker. |
| `reference_text` | yes | The exact transcript of `reference_audio`. |

It returns the absolute path of a generated 24 kHz `.wav`. Unlike
`VoiceSynthesize` (OpenAI TTS), `VoiceSynthesizeLocal` makes no network call
and incurs no cost. The tool is registered only when the `neutts` package is
importable, so an install without the `[neutts]` extra never sees it.

## When to use which

- **`VoiceSynthesizeLocal` (NeuTTS)** — offline, no cost, voice cloning.
  Needs the `[neutts]` extra and a reference clip. Best for privacy-sensitive
  or offline use, or when a specific cloned voice is wanted.
- **`VoiceSynthesize` (OpenAI)** — a fixed set of high-quality voices, no
  reference clip needed, but a network call and per-use cost.
- **Edge TTS** (`VoiceConfig(provider="edge")`) — free Microsoft neural
  voices, no API key, but still a network call.

## Notes

- NeuTTS emits 24 kHz mono WAV audio.
- The model loads lazily and is cached in-process: the first synthesis after
  process start pays the load cost; subsequent calls are fast.
- The synthesis call runs on a worker thread, so it never blocks the agent's
  event loop.
- See `opencomputer/voice/tts_neutts.py` for the provider implementation.
