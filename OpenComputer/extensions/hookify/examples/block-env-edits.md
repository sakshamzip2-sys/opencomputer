---
name: block-env-edits
enabled: true
event: file
action: block
conditions:
  - field: file_path
    operator: ends_with
    pattern: '.env'
---

Refusing to edit `.env` files directly. Two safer paths:

1. Edit `.env.example` (committed) and instruct the user to copy it.
2. Use a SecretRef from `plugin_sdk.SecretRef` for typed secret values.

Drop this rule (or set `enabled: false`) if the project genuinely
needs in-band `.env` edits.
