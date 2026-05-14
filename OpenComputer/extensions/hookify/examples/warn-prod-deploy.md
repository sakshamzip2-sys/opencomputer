---
name: warn-prod-deploy
enabled: true
event: bash
action: warn
conditions:
  - field: command
    operator: contains
    pattern: 'deploy'
  - field: command
    operator: contains
    pattern: 'prod'
---

You're invoking what looks like a production deploy. Before continuing:

- Run the test suite (`pytest tests/` / `npm test`) and confirm green.
- Check that the deploy target really is the intended environment.
- If a feature flag governs this rollout, verify it's set correctly.
