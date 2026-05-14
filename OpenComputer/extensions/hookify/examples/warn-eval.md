---
name: warn-eval
enabled: true
event: file
action: warn
conditions:
  - field: new_text
    operator: regex_match
    pattern: 'eval\('
---

The file you're editing introduces an `eval(` call. `eval` evaluates
arbitrary code and is a major injection sink.

Alternatives by use case:
- Parsing JSON → `JSON.parse` / `json.loads`.
- Looking up a function by name → registry dict, not eval.
- Templating math expressions → a real expression parser
  (e.g. `mathjs`, `sympy`).

Keep `eval` only if evaluating arbitrary user-supplied code is the
literal feature.
