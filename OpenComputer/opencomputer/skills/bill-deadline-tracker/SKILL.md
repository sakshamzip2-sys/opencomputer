---
name: bill-deadline-tracker
description: Use when the user asks for upcoming bills/deadlines, wants email-deadline scanning, or to set up automatic deadline reminders.
---

# Bill / Deadline Tracker

## When to use

- User asks "what bills are due?" or "any deadlines coming up?"
- User wants automatic email-deadline scanning
- User wants 24h-before-due notifications

## What this skill does

1. **Scan email** for deadline patterns (last 30 days)
2. **Extract deadline candidates** — date + amount + biller
3. **Surface upcoming** — sorted by date, highlighting next 7 days
4. **Optional cron**: schedule a daily 8am scan that pings via push_notification

## Required setup

- Email plugin configured (`extensions/email/`) — IMAP credentials in `<profile_home>/email/config.yaml`
- (Optional) cron daemon running for automatic scans

If email plugin is not configured, skill returns: "Configure email plugin first: opencomputer email configure"

## Pattern matching

Scan email subject + first 500 chars of body for:

- **Deadline signals**: "due", "by [date]", "renewal", "invoice", "statement", "payment", "expires"
- **Date extraction**: "Friday", "March 15", "next week", "in 3 days", explicit dates "MM/DD/YYYY"
- **Amount extraction**: `$XXX.XX`, `INRX,XXX`, `EURXX.XX` patterns
- **Biller extraction**: from-address domain → known billers (Chase, Verizon, etc.) OR "Bill from X" patterns

## Output format

```
Upcoming bills + deadlines

Next 7 days
- Apr 30 — $89.99 Verizon mobile (auto-pay)
- May 02 — INR 1,250 HDFC credit card minimum
- May 04 — $15.00 NYTimes subscription renewal

Next 30 days
- May 12 — $1,200 rent
- May 15 — IRS Q1 estimated tax (US)
- ...

Ambiguous (review needed)
- Email from "support@x.com" mentions "due soon" but no date — open it?
```

## Setting up automatic reminders

```bash
# Daily 8am scan + Telegram push
opencomputer cron create \
  --name "morning-bill-scan" \
  --schedule "0 8 * * *" \
  --skill bill-deadline-tracker \
  --notify telegram
```

## CAUTION

- Email scanning involves reading mail content. Email plugin already F1-gated; this skill respects existing consent.
- **No auto-payment** — never schedule actual payments. This skill REPORTS only.
- False positives possible — "Submit by Friday" in a colleague's casual email might look like a deadline. Review before relying.

## Notes

- Date parsing uses the `dateutil` library (already a dep).
- Biller detection is best-effort; missing patterns can be added per-user via `<profile_home>/billers.yaml`.
- Output is sorted by date AND highlights amount + biller for quick scan.
