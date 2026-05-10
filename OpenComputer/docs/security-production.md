# Production Security Checklist — OpenComputer Gateway

Quick reference for hardening an OpenComputer deployment intended to
serve real users (Telegram bot, Discord bot, OpenAI-compat API, etc.).
Mirrors the Hermes "Production Checklist" with OC paths.

> Background reading: `docs/superpowers/specs/2026-05-08-hermes-security-v2-design.md`
> walks through the audit table mapping each Hermes layer to its
> OpenComputer module.

## Authorization

- [ ] **Set explicit allowlists** — never `GATEWAY_ALLOW_ALL_USERS=true`
      in production.

      ```bash
      # ~/.opencomputer/<profile>/.env
      TELEGRAM_ALLOWED_USERS=123456789
      DISCORD_ALLOWED_USERS=111222333444555666
      ```

- [ ] **Prefer DM pairing over hardcoded user IDs.** New users DM the
      bot, the bot replies with an 8-character code, you approve it
      from the CLI:

      ```bash
      oc gateway pairing approve telegram ABC12DEF
      oc gateway pairing list
      ```

      Pairing codes are cryptographically random, single-use, expire
      after 1 hour, rate-limited (1 request per user per 10 minutes),
      and lock out after 5 failed approval attempts (1 hour
      platform-wide).

- [ ] **Review pairing approvals quarterly.** Approved entries persist
      indefinitely; an ex-employee's user ID is still authorized
      until you revoke.

      ```bash
      oc gateway pairing list                     # see who's approved
      oc gateway pairing revoke telegram <id>     # remove access
      ```

## Container isolation

- [ ] **Use a sandboxed terminal backend** for the agent's `Bash` /
      `ExecuteCode` execution path.

      ```yaml
      # ~/.opencomputer/<profile>/config.yaml
      sandbox:
        strategy: docker
        image: python:3.12-slim
        memory_mb_limit: 2048
        cpu_seconds_limit: 60
        network_allowed: false
      ```

- [ ] **Configure CPU/memory limits.** The defaults are conservative,
      but production workloads sometimes need more — set them
      explicitly so you know what you're paying for.

- [ ] **The hardening flags are always-on.** OpenComputer applies these
      to every container automatically — no opt-in needed:

      | Flag | Why |
      |---|---|
      | `--cap-drop ALL` | Drop every Linux capability |
      | `--cap-add DAC_OVERRIDE` | Allow root inside container to write to bind-mounted dirs |
      | `--cap-add CHOWN` `FOWNER` | Package managers (apt, yum, apk) need these |
      | `--security-opt no-new-privileges` | Block setuid/setgid privilege escalation |
      | `--pids-limit 256` | Cap process count — fork-bomb defence |
      | `--tmpfs /tmp` (size=512m, nosuid) | World-writable /tmp on tmpfs |
      | `--tmpfs /var/tmp` (noexec, nosuid, size=256m) | No-exec for the second tmp |
      | `--tmpfs /run` (noexec, nosuid, size=64m) | Same for /run |

      Containers that need a dropped capability back should raise a
      feature request rather than patching the constant.

- [ ] **Lock down implicit container state.** Add to `config.yaml`:

      ```yaml
      sandbox:
        strategy: docker
        # container_persistent: true   # default — implicit container fs untouched
        container_persistent: false   # tmpfs /workspace + /root — explicit ephemeral
      ```

      Set `false` for cron jobs, one-shot agents, or any deployment
      where you want explicit guarantees that nothing under
      `/workspace` or `/root` can persist between calls. User-declared
      `read_paths` / `write_paths` still bind-mount in either mode —
      the toggle controls only the implicit container layer.

## Approval flow (manual mode)

When the consent gate fires a manual prompt, four verbs are available:

| Verb | Meaning | Storage |
|---|---|---|
| `once` (`y`) | Allow this single execution | Ephemeral — no state written |
| `session` | Allow until session ends (SESSION_FINALIZE) | In-memory dict, not persisted |
| `always` | Allow indefinitely | Permanent grant in `consent.db` |
| `deny` (`N`, default) | Block this execution | Ephemeral; user can re-prompt next call |

For chat-driven approvals (Telegram / Slack), four buttons render. The
matrix adapter does not currently expose `send_approval_request` and
therefore matrix-bound consent prompts auto-deny — operators who want
matrix-side approvals can use the matrix adapter's separate
`request_approval` flow (`extensions/matrix/approval.py`) which is a
2-emoji allow/deny surface.

## Tirith pre-exec scan

OpenComputer runs Tirith on every Bash command and ExecuteCode block
after the hardline blocklist. Three verdicts:

| Verdict | Behaviour |
|---|---|
| `allow` | Command runs normally |
| `warn` | Command runs; findings prefixed to tool output |
| `block` | Command refused; findings returned as error result |

When Tirith's binary is unavailable + `tirith_fail_open: true` (default),
all commands reach `allow`. Set `tirith_fail_open: false` for
strict-deny when the scanner is unreachable.

## Filesystem hygiene

- [ ] **`chmod 600 ~/.opencomputer/<profile>/.env`** — never let it be
      group/world-readable.

- [ ] **Never commit `.env` to version control.** OpenComputer's
      `.gitignore` covers `.opencomputer/`, but if you split the
      profile directory across hosts the discipline must travel.

## Process & operator posture

- [ ] **Run as non-root.** OpenComputer refuses to start `oc gateway`
      as root unless you set `OPENCOMPUTER_ALLOW_ROOT_GATEWAY=1`.
      Override only when the host environment requires it (e.g.,
      systemd in a hardened container where the host has its own
      isolation).

- [ ] **Set `MESSAGING_CWD`** to a non-sensitive directory — the
      gateway agent operates from this CWD by default. Keep it away
      from secrets and from your coding-harness working tree.

- [ ] **Rotate credentials regularly.** The bot tokens
      (`TELEGRAM_BOT_TOKEN`, `DISCORD_BOT_TOKEN`, etc.) are real
      credentials; rotate them as part of your normal credential
      lifecycle.

## Defence-in-depth

- [ ] **Use `tirith_fail_open: false`** in high-security environments.
      The default fail-open posture lets commands run when Tirith's
      pre-exec scanner is unavailable; in regulated environments the
      safer choice is fail-closed.

      ```yaml
      security:
        tirith_fail_open: false
      ```

- [ ] **Configure the website blocklist** for any internal hostname
      that should never be fetched by an LLM-driven agent.

      ```yaml
      security:
        website_blocklist:
          enabled: true
          domains:
            - "*.internal.company.com"
            - "admin.example.com"
            - "*.local"
          shared_files:
            - /etc/opencomputer/blocked-sites.txt
      ```

      Rule grammar:

      | Rule shape | Match |
      |---|---|
      | `admin.example.com` | exact host |
      | `*.internal.company.com` | host + any subdomain |
      | `*.local` | TLD wildcard — any host ending `.local` |

      Shared-file lines support `#` comments. Missing files log a
      warning and don't disable the inline `domains` list.

- [ ] **Tirith provenance verification.** The pre-exec scanner
      auto-installs from GitHub releases with SHA-256 checksum
      verification (and cosign provenance if available). Don't
      disable the verification.

## MCP credential isolation

OpenComputer strips secrets from the env passed to MCP stdio
subprocesses. The whitelist of admitted parent env vars is:

```
PATH, HOME, USER, LANG, LC_ALL, TERM, SHELL, TMPDIR
```

Plus any `XDG_*` key. Everything else (API keys, OAuth tokens,
gateway credentials) is stripped. To pass a specific secret to one
MCP server explicitly, declare it in `mcp_servers.<name>.env` in
config.yaml:

```yaml
mcp_servers:
  github:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: ghp_...   # only this passes through
```

## Monitoring

- [ ] **Tail `~/.opencomputer/<profile>/logs/`** for unauthorized
      access attempts, hardline-blocklist hits, and consent-gate
      denials.

- [ ] **Watch the consent audit log.** OpenComputer writes an
      HMAC-chained audit row for every grant/deny:

      ```bash
      oc consent history                          # full audit log
      oc consent history <capability_id>          # filter to one capability
      oc audit verify                             # verify the HMAC chain
      ```

- [ ] **Run `oc update` regularly** to pick up security patches.
      Subscribe to release notifications on
      <https://github.com/sakshamzip2-sys/opencomputer/releases>.

## Network segmentation (optional but recommended)

For maximum isolation, run the gateway on a separate machine/VM and
have the agent execute commands via a remote sandbox:

```yaml
sandbox:
  strategy: ssh
  ssh_host: agent-worker.local
  ssh_user: agent
  ssh_key: ~/.ssh/agent_worker_key
```

This keeps the gateway's messaging connections (Telegram tokens,
etc.) separate from the agent's command-execution surface.

## Hardline blocklist (always-on, no override)

OpenComputer refuses any command matching one of these patterns
regardless of `--auto`, consent grants, or persisted approvals:

| Pattern | Why hardline |
|---|---|
| `rm -rf /` and obvious variants | Wipes filesystem root |
| `rm -rf --no-preserve-root /` | Explicit root-removal flag |
| `:(){ :\|:& };:` (bash fork bomb) | Pegs host until reboot |
| `mkfs.*` against `/dev/sd*` etc. | Formats live disk |
| `dd of=/dev/sd*` | Zeroes a physical disk |
| `curl URL \| sh` / `wget URL \| sh` | Pipes untrusted bytes into a shell — RCE attack vector |

The blocklist fires before the consent gate — a tripped pattern
never produces an approval prompt. There is no override flag.
Defence-in-depth: the check applies even inside Docker containers
because bind mounts (`-v host:container:rw`) and persistent
workspaces can leak destruction back to the host.

The full list lives in `opencomputer/security/hardline.py`. To add a
new pattern, raise a PR — every entry needs a matching test and a
clear rationale.

## Quick smoke test

After deploying, run this from the gateway host:

```bash
oc doctor                                      # general health
oc gateway status                               # gateway daemon health
oc consent history --limit 10                   # recent consent decisions
```

A healthy production deployment shows:

- `oc doctor`: no red rows
- `oc gateway status`: `active` with the expected adapter list
- `oc consent history`: HMAC chain intact, no recent unexpected
  hardline hits
