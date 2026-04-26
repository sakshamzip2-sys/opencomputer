---
name: incident-response
description: Use when handling a production outage, post-mortem, runbook execution, or live debugging of a critical issue
---

# Incident Response

## When to use

- A user-facing system is broken or degraded
- An alert fired and you're on-call
- Writing a post-mortem after an incident

## Steps

1. **Stabilize first, diagnose second.** If a rollback or restart returns service, do that. Diagnose can wait.
2. **One incident commander.** One person decides; everyone else investigates. Clear comms beat parallel uncoordinated fixes.
3. **Write as you go.** Slack thread or doc with timestamps + actions taken + observations. Memory is unreliable under stress.
4. **Customer comms within 15 min.** "We are aware of X, investigating, ETA next update in Y minutes." Vague is fine; silent is not.
5. **Rollback ≠ fix.** Once stable, file a "fix forward" ticket. Rolling back doesn't address the root cause.
6. **Post-mortem within 48 hours.** Five whys. Blameless. Action items with owners + dates, or it's theatre.
7. **Add the alert.** Whatever you wished you'd known sooner, alert on it next time.

## Notes

- "Did we change anything in the last hour?" is the first question. Deploys are correlated with incidents.
- Don't make schema or config changes during an incident unless the incident IS the schema/config. Stability over cleanup.
- Save logs/dashboards/screenshots before they expire.
