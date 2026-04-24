# Shell-hook recipes

Three copy-paste settings-YAML recipes for the most common settings-hook
patterns. All three rely on the exit-code contract:

- `0` → pass (tool runs)
- `2` → block, stderr becomes the reason
- anything else → fail-open

They also rely on the stdin JSON blob described in the parent
`SKILL.md`: `hook_event_name`, `session_id`, `tool_name`, `tool_input`,
`runtime`, etc.

## 1. Tool-call audit logger (PostToolUse, fire-and-forget)

Append every tool call + result to a per-profile log file so you can
replay the session later.

```yaml
hooks:
  PostToolUse:
    - command: |
        python3 -c '
        import sys, json, os, datetime
        data = json.loads(sys.stdin.read() or "{}")
        line = {
          "ts": datetime.datetime.utcnow().isoformat(),
          "event": data.get("hook_event_name"),
          "tool": data.get("tool_name"),
          "session": data.get("session_id"),
        }
        path = os.path.expandvars("$OPENCOMPUTER_PROFILE_HOME/audit.log")
        with open(path, "a") as f:
          f.write(json.dumps(line) + "\n")
        '
      timeout_seconds: 5
```

Notes:
- No `matcher` — fires on every tool.
- Exits 0 on success; any failure falls open (still pass).
- `$OPENCOMPUTER_PROFILE_HOME` is the profile home dir; the env is
  inherited from the parent plus the OpenComputer-specific keys.

## 2. Block destructive bash commands (PreToolUse, blocking)

Refuse `rm -rf /`, `git reset --hard`, and similar shapes before they
reach the Bash tool.

```yaml
hooks:
  PreToolUse:
    - matcher: "^Bash$"
      command: |
        python3 -c '
        import sys, json
        data = json.loads(sys.stdin.read() or "{}")
        cmd = (data.get("tool_input") or {}).get("command", "")
        banned = ["rm -rf /", "rm -rf ~", "git reset --hard",
                  "mkfs", "dd if=", ":(){:|:&};:"]
        for b in banned:
          if b in cmd:
            print(f"blocked: command contains {b!r}", file=sys.stderr)
            sys.exit(2)
        sys.exit(0)
        '
      timeout_seconds: 5
```

Notes:
- Exit 2 blocks the Bash call; stderr becomes the model-visible reason.
- The `matcher: "^Bash$"` restricts the hook to Bash only; without it
  the subprocess would spawn for every tool (expensive).
- For richer pattern coverage, use the name-based block from the
  coding-harness `plan_block` hook as a model.

## 3. Inject project context at SessionStart (fire-and-forget)

Some teams want every session to open with a preamble pulled from the
repo — project conventions, hot TODOs, recent breakage. A
`SessionStart` hook can emit text the agent reads when it initializes.

```yaml
hooks:
  SessionStart:
    - command: |
        bash -c '
        session_dir="$OPENCOMPUTER_PROFILE_HOME/sessions/$OPENCOMPUTER_SESSION_ID"
        mkdir -p "$session_dir"
        if [ -f ./PROJECT_CONVENTIONS.md ]; then
          cp ./PROJECT_CONVENTIONS.md "$session_dir/preamble.md"
        fi
        exit 0
        '
      timeout_seconds: 10
```

This writes the preamble to the session's dir on startup; a separate
injection provider or SKILL.md can pick it up later. Keep this hook
fast — `SessionStart` runs on every new chat, and the 10-second
timeout is a hard ceiling.

## When NOT to use a shell hook

If the logic needs tight integration with OpenComputer's internals
(inspecting the full `Message` thread, mutating the session DB,
conditional on live `RuntimeContext`), write a plugin with a Python
`HookSpec` instead. Shell hooks are for:

- Audit and telemetry.
- Gross-shape content blocking.
- Preamble / teardown scripts.

They are NOT for:

- Decisions that depend on data the JSON blob doesn't carry.
- Anything that needs to survive a timeout.
- State that other hooks need to read back (the subprocess is
  ephemeral — use Python hooks for shared state).

## Timeout + fail-open posture

`timeout_seconds` is a HARD wall-clock limit. If the subprocess exceeds
it, the hook engine kills the process and returns
`HookDecision(decision="pass")` — the tool still runs. This is
deliberate: a broken or slow hook must never brick the CLI.

Default is 10 seconds. Reduce for hot-path hooks (PreToolUse,
PostToolUse); expand for SessionStart / SessionEnd if they do heavy
setup.
