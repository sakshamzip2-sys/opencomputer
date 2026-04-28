---
name: inbox-triage
description: Use when the user wants to summarize unread messages across channels (Telegram/Discord/Slack/iMessage), draft replies, or flag urgent items. Read-only by default.
---

# Inbox Triage Across Channels

## When to use

- User asks "what's new in my messages?" or "summarize my inbox"
- User wants AI-drafted replies for review (not auto-sent)
- User asks to flag urgent items across multiple channels

## What this skill does NOT do

- **Never auto-sends** — every drafted reply requires explicit user approval
- **Never reads private DMs without channel access already granted** — uses existing channel adapters' OAuth/token state
- **Never archives or marks-read** — purely observational

## Procedure

1. **Discover available channels**:
   - Check which channel adapters are loaded: `telegram`, `discord`, `slack`, `imessage`, `matrix`, `whatsapp`, `signal`, `email`.
   - Skip channels not configured (no API key / not paired).

2. **Fetch recent messages** per channel:
   - Default window: last 24 hours.
   - Per channel, use the adapter's history API if available, else "what came in since I last checked" semantics.

3. **Per-message triage**:
   - **Sender** — known contact? extract name.
   - **Urgency signals** — keyword scan for "URGENT", "ASAP", "?", deadlines, financial keywords.
   - **Sentiment** — positive / neutral / negative / question.
   - **Reply needed?** — heuristic: ends with `?`, references a past commitment, requests an action.

4. **Group + summarize**:
   - **Urgent** (top 3) — sender + 1-line summary.
   - **Replies needed** (top 5) — sender + draft suggestion.
   - **Informational** (count + 1-line) — "5 newsletter / promotional / status updates".
   - **Skipped sensitive** (count) — "2 messages from sensitive contacts (banking / private) not analyzed".

5. **Output format**:
   ```
   Inbox Triage (last 24h)

   Urgent (3)
   - [Telegram from Saksham] ATLANTAELE breaking down — review chart?
   - ...

   Replies needed (5)
   - [Slack from Ravi] "Can we sync at 4pm?" -> suggested draft: "Sure, dialing in"
   - ...

   Informational (12)
   - 12 newsletter / promotional / status updates

   Skipped sensitive (2)
   ```

6. **User-driven follow-up**:
   - User can pick: "draft replies to all #2", "send draft #3 verbatim", "show full message #1", etc.
   - Drafts go through the channel adapter's `send_reply` only with explicit user approval.

## Sensitive-app awareness

Reuse `extensions/ambient-sensors/sensitive_apps.py::is_sensitive` to filter sensitive senders / app contexts. Banking / password-manager / healthcare conversation contexts skip the triage entirely.

## Examples

User: "what's in my inbox?"
Agent: [fetches across channels, presents triage summary]

User: "draft replies to the urgent ones"
Agent: [generates per-message draft, asks user to confirm send]

User: "send draft #3 as-is"
Agent: [calls channel adapter's send method]

## Notes

- Cross-channel = multiple network calls; can be slow. Show progress: "Fetching Telegram... Slack... Discord...".
- Drafts are CONSERVATIVE — better to under-draft and let user fill in than to over-confidently send something off-tone.
- If a channel adapter is misconfigured, skip with clear note in output (don't block the rest).
