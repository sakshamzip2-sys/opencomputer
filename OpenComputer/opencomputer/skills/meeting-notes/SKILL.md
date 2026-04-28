---
name: meeting-notes
description: Use when the user has a meeting audio recording, asks for meeting notes, or wants action items extracted from a transcript. Composes Whisper STT + Notion MCP (optional) + push_notification.
---

# Meeting Notes Pipeline

## When to use

- User shares an audio file (mp3/wav/m4a/opus) and asks "summarize this meeting"
- User has already-transcribed text and asks for action items
- User wants automatic notes routed to Notion

## What this skill does

1. **Transcribe** — if input is audio, use the existing `voice/transcribe` tool (Whisper API or local mlx-whisper / whisper-cpp).
2. **Summarize** — extract: meeting title, participants (if known), key decisions, open questions, action items.
3. **Extract action items** — each as `{owner, task, due_date_if_mentioned}`.
4. **Route output** — Notion if available (`Notion:create-page` skill), otherwise return as agent response.
5. **Optional**: push a Telegram summary if user has the channel configured.

## Procedure

1. **Verify input**:
   - If audio file path: confirm exists, ≤25 MB (Whisper API limit). Larger → split via `extensions/voice-mode/audio_capture` if available, else error.
   - If text: confirm reasonable length (≤500K chars).

2. **Transcribe (only if audio)**:
   - `voice_transcribe` tool with the audio file path.
   - If user has `--local` preference, pass `prefer_local=True`.

3. **Summarize**:
   - Build a structured prompt asking for: 1-line title, 3-7 bullet decisions, 3-7 open questions, action-items list (owner / task / due date).
   - Use the agent's main provider (Anthropic) — meeting summaries benefit from a strong model.

4. **Extract action items**:
   - From the summary, isolate the action-items list.
   - Each item: `{owner, task, due_date_if_mentioned}` with `due_date` parsed if pattern like "by Friday", "next week", "March 15" is present.

5. **Route output**:
   - **If Notion MCP is available** (check `Notion:create-page` skill in registry): create a new page under the user's "Meetings" parent (if exists) or as a child of the workspace root. Title = meeting title; body = full summary; properties = action items.
   - **If Notion not available**: return summary as agent response.
   - **If user has Telegram channel + `--push` flag**: send a 3-line digest (title + top 3 action items) via `push_notification`.

## CAUTION

- **Sensitive content** — meeting transcripts often contain confidential information (names, financials, strategy). Never share the full transcript with third parties.
- **PII redaction** — if the transcript contains card numbers, SSN-shapes, or identifiable personal info, redact before saving to Notion or pushing to Telegram.
- **Skip recording without consent** — if the user is recording a meeting, confirm all participants consented before processing.

## Examples

User: "Here's the recording from today's standup, can you summarize?"
Agent: [transcribes, summarizes, extracts 3 action items, asks if user wants Notion page]

User: "Take this transcript and pull out my action items: [paste]"
Agent: [skip transcription, jump to step 4]

## Notes

- If `voice_transcribe` fails (no audio backend), surface the error clearly: "Whisper backend not configured — set OPENAI_API_KEY or pip install opencomputer[voice-mlx]".
- Notion routing is OPTIONAL — never block on it.
- Future enhancement: auto-create calendar invites for action items with due dates.
