---
name: remind-tests-before-push
enabled: true
event: bash
action: warn
conditions:
  - field: command
    operator: regex_match
    pattern: 'git\s+push'
---

About to `git push`. If you haven't already this turn:

- `pytest tests/ -x --tb=line` (Python projects)
- `npm test` / `yarn test` / `pnpm test` (Node projects)
- `cargo test` (Rust)
- `go test ./...` (Go)

Push only after the suite is green.
