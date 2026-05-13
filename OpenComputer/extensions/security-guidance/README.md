# Security Guidance (OpenComputer port)

PreToolUse hook that warns the agent before edits introduce common
security risks: eval/exec injection, XSS sinks (innerHTML,
dangerouslySetInnerHTML, document.write), pickle deserialization,
os.system, child_process.exec, GitHub Actions injection patterns.

## Behavior

- Fires only on `Edit`, `Write`, `MultiEdit` (other tools pass).
- Each `(file_path, rule)` pair fires once per session — not on every edit.
- A blocking message is sent back to the model with the explanation.
- State per session is persisted to:
  `$OPENCOMPUTER_PROFILE_HOME/security_warnings/shown_<session_id>.json`
  (falls back to `~/.opencomputer/default/`).

## Disable

```bash
ENABLE_SECURITY_REMINDER=0 oc chat
```

Or remove the plugin from `~/.opencomputer/<profile>/plugins/` (or
remove from the profile preset).

## Patterns

Defined in `security_patterns.py`. Each pattern is either:
- a `path_check` (callable on the path — used for `.github/workflows/*.yml`)
- a `substrings` tuple (string presence in the new content)

Adding a pattern is a one-entry append. The catalogue is intentionally
small and high-confidence — false positives cost more attention than
missed warnings.

## Why a port (not a wrapper)

Anthropic's `security-guidance` plugin ships as a standalone Python
script invoked via the JSON hook contract. OC's hook engine accepts
both shell-command hooks (settings YAML) and Python `register_hook`
calls (plugin SDK). The Python path:

- Avoids subprocess startup cost on every tool call
- Has typed access to `HookContext` (no JSON parse)
- Composes with other plugin-registered hooks (priority, fire-and-forget)

Behavior matches the upstream script line-for-line; the difference is
the hosting mechanism.

## License

MIT (matches the upstream Anthropic plugin).
