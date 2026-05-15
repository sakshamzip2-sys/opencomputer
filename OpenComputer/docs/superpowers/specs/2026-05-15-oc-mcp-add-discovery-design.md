# `oc mcp add` — surface discoverable presets instead of a typer error

**Date:** 2026-05-15
**Owner:** Saksham / Claude (Opus 4.7)
**Status:** Design — ready for review
**Branch (proposed):** `feat/oc-mcp-add-discovery`
**Files in scope:** `opencomputer/cli_mcp.py`, `tests/test_phase11c.py`, `tests/test_mcp_presets.py` (new tests file allowed)

---

## 1. Problem

Running `oc mcp add` bare drops the user into a Typer error:

```
$ oc mcp add
Usage: oc mcp add [OPTIONS] NAME
Try 'oc mcp add --help' for help.
╭─ Error ─────────────────────────────────────────────────╮
│ Missing argument 'NAME'.                                │
╰─────────────────────────────────────────────────────────╯
```

This is the *first* command a user reaches for when they want an MCP server. Three reasons it's the wrong landing point:

1. **It's a dead-end.** The user knows they want to "add an MCP" — the error tells them what didn't work, not what to try next.
2. **The system already has 19 vetted presets** in `opencomputer/mcp/presets.py:49` (filesystem, github, fetch, postgres, brave-search, sqlite, gitlab, google-drive, slack, memory, puppeteer, sequential-thinking, time, everart, notion, linear, sentry, perplexity, docker), each with a `description`, `required_env`, and `homepage`. The user just doesn't know they exist.
3. **`oc mcp presets` and `oc mcp install <slug>` exist** — see `cli_mcp.py:524` and `cli_mcp.py:790` — but they're behind names the user has to guess.

Net: we have all the data, we just don't route bare `mcp add` to it.

## 2. Goal

When `oc mcp add` is called without args on a TTY, drop into a discovery flow:

- show the preset table (slug / description / required env)
- pick a preset interactively, OR fall through to the manual-flag path
- run `mcp install <slug>` semantics for the picked preset
- non-TTY (CI / pipes / `oc mcp add < /dev/null`) keeps current strict-error behavior so scripts don't silently change shape

Bare `oc mcp add` should never be a dead-end for an interactive user.

## 3. Non-goals

- **Not redesigning `install`.** `oc mcp install <slug>` keeps its current shape — same flags, same exit codes, same OSV preflight. Discovery flow *delegates* to install internals.
- **Not changing `add` for users who already pass args.** `oc mcp add my-server --transport http --url ...` behaves identically. Only the zero-arg branch is new.
- **Not changing `presets` / `catalog` / `install` CLIs.** They keep working; this just gives users a friendlier entry point that funnels into them.
- **No remote catalog fetch in the discovery flow.** Bundled `PRESETS` only. Remote catalog (`oc mcp catalog --remote`) is opt-in and slow; keep `add` snappy.
- **No new preset entries in this change.** Pure UX. Preset content stays.

## 4. Behavior — interactive TTY

```
$ oc mcp add

  No name given — here's what's available.

  MCP Presets (19)
  ┌────────────────────┬─────────────────────────────────────┬───────────────────────────────┐
  │ Slug               │ Description                          │ Required env                  │
  ├────────────────────┼─────────────────────────────────────┼───────────────────────────────┤
  │ filesystem         │ Read/write/list files within …      │ —                             │
  │ github             │ Browse repos, read code, list iss…  │ GITHUB_PERSONAL_ACCESS_TOKEN  │
  │ fetch              │ Fetch URLs and convert HTML to …    │ —                             │
  │ …                                                                                       │
  └────────────────────┴─────────────────────────────────────┴───────────────────────────────┘

  Pick a preset slug to install, or 'custom' to add a server by hand
  (or Ctrl-C to abort).

  > github

  installed preset 'github' as 'github' → /Users/.../.opencomputer/config.yaml

  Required environment variables:
    ✗ unset  GITHUB_PERSONAL_ACCESS_TOKEN

  Set the missing vars before the next agent run, or the server will fail to start.

  docs: https://github.com/modelcontextprotocol/servers/tree/main/src/github
```

If the user types `custom`:

```
  Custom server. Re-run with the flag shape, for example:

      oc mcp add my-server --transport stdio --command npx --arg -y --arg some-mcp-package
      oc mcp add my-server --transport http  --url https://example.com/mcp \
                           --header "Authorization=Bearer XYZ"

  See `oc mcp add --help` for the full flag list.
```

Exit 0 in both cases (no error). Ctrl-C aborts with exit 130 (the standard `KeyboardInterrupt` convention used elsewhere in `cli_profile.py:846`).

## 5. Behavior — non-TTY (CI / piped)

Keep current strict behavior. Typer will still raise "Missing argument 'NAME'" with exit code 2, but we improve the help text:

```
Missing argument 'NAME'.

Hint: run `oc mcp add` interactively to pick from 19 bundled presets,
or `oc mcp install <slug>` directly. Run `oc mcp presets` to list them.
```

This is reachable from inside the no-args branch by detecting `not sys.stdin.isatty()` early.

## 6. Implementation sketch

```python
# opencomputer/cli_mcp.py — around the @mcp_app.command("add") definition

@mcp_app.command("add")
def add_server(
    name: str | None = typer.Argument(
        None,                                       # <-- was required, now optional
        help="Server name (used as tool prefix). Omit for interactive picker.",
    ),
    transport: str = typer.Option("stdio", ...),
    command: str = typer.Option("", ...),
    arg: list[str] = typer.Option([], ...),
    env: list[str] = typer.Option([], ...),
    url: str = typer.Option("", ...),
    header: list[str] = typer.Option([], ...),
    disabled: bool = typer.Option(False, ...),
    skip_osv_check: bool = typer.Option(False, ...),
) -> None:
    """Add an MCP server to config.yaml.

    With no NAME: interactive preset picker (TTY) or hint (non-TTY).
    With NAME: existing manual flag-driven shape.
    """
    if name is None:
        return _add_discovery_flow(disabled=disabled, skip_osv_check=skip_osv_check)

    # ... existing manual path unchanged ...
```

The discovery helper:

```python
def _add_discovery_flow(*, disabled: bool, skip_osv_check: bool) -> None:
    """Render presets + prompt for a slug. Delegate to mcp_install on pick."""
    import sys

    from opencomputer.mcp.presets import PRESETS, get_preset

    if not sys.stdin.isatty():
        console.print(
            "[red]error:[/red] Missing argument 'NAME'.\n\n"
            "[dim]Hint: run `oc mcp add` interactively to pick from "
            f"{len(PRESETS)} bundled presets, or `oc mcp install <slug>` "
            "directly. Run `oc mcp presets` to list them.[/dim]"
        )
        raise typer.Exit(code=2)

    # Render the same table as `mcp_presets()` to keep formatting consistent.
    mcp_presets()
    console.print(
        "\n[bold]Pick a preset slug to install, "
        "or 'custom' to add a server by hand.[/bold]"
    )

    from rich.prompt import Prompt
    try:
        choice = Prompt.ask("slug", console=console, default="").strip().lower()
    except (KeyboardInterrupt, EOFError):
        raise typer.Exit(code=130)

    if not choice:
        raise typer.Exit(code=0)        # silent abort — empty input

    if choice == "custom":
        console.print(
            "\n[bold]Custom server.[/bold] Re-run with the flag shape, for example:\n"
            "\n    oc mcp add my-server --transport stdio --command npx "
            "--arg -y --arg some-mcp-package"
            "\n    oc mcp add my-server --transport http  --url https://example.com/mcp \\"
            "\n                         --header \"Authorization=Bearer XYZ\""
            "\n\n[dim]See `oc mcp add --help` for the full flag list.[/dim]"
        )
        return

    if get_preset(choice) is None:
        console.print(
            f"[red]error:[/red] unknown preset {choice!r}. "
            "Run `oc mcp presets` to see the full list."
        )
        raise typer.Exit(code=1)

    # Delegate to the existing install path so OSV, env-var warnings, and
    # the homepage link all behave identically to `oc mcp install <slug>`.
    mcp_install(preset=choice, name="", disabled=disabled, skip_osv_check=skip_osv_check)
```

Key invariants:

- **`name` becomes `Optional[str]`** — Typer treats `None` default as optional, no other arg signatures change.
- **Discovery flow delegates to `mcp_install`**, not a copy of its body. Single source of truth for OSV preflight, env-var status icons, homepage line, and duplicate-name guard.
- **No I/O changes** outside the new branch. The manual-arg path is byte-for-byte unchanged.
- **`Rich.prompt.Prompt` is the existing convention** — same import shape as `cli_profile.py:830`.

## 7. Tests

### New tests in `tests/test_phase11c.py`

```python
def test_mcp_cli_add_no_args_non_tty_emits_hint(tmp_home: Path) -> None:
    """No args + no TTY → exit 2 with hint mentioning presets and install."""
    result = _runner_invoke(["mcp", "add"])     # CliRunner has no TTY by default
    assert result.exit_code != 0
    body = result.stdout + (result.stderr or "")
    assert "presets" in body.lower()
    assert "install" in body.lower()


def test_mcp_cli_add_no_args_tty_renders_picker(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No args + TTY + user picks 'filesystem' → preset installed."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    result = _runner_invoke(["mcp", "add"], input="filesystem\n")
    assert result.exit_code == 0
    assert "installed" in result.stdout.lower()
    cfg = load_config()
    assert any(s.name == "filesystem" for s in cfg.mcp.servers)


def test_mcp_cli_add_no_args_tty_custom_prints_examples(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No args + TTY + 'custom' → exit 0 with example flag shapes printed."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    result = _runner_invoke(["mcp", "add"], input="custom\n")
    assert result.exit_code == 0
    assert "--transport stdio" in result.stdout
    assert "--transport http" in result.stdout
    # No server should have been written.
    cfg = load_config()
    assert not any(s.name == "my-server" for s in cfg.mcp.servers)


def test_mcp_cli_add_no_args_tty_empty_input_aborts(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty input at the prompt is treated as a silent abort (exit 0)."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    result = _runner_invoke(["mcp", "add"], input="\n")
    assert result.exit_code == 0


def test_mcp_cli_add_no_args_tty_unknown_preset_errors(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown slug → exit 1 with hint to run `mcp presets`."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    result = _runner_invoke(["mcp", "add"], input="does-not-exist\n")
    assert result.exit_code == 1
    assert "unknown preset" in result.stdout.lower()
```

### Existing tests to verify still pass

These must remain green — they pin the manual-arg path:

- `test_mcp_cli_add_rejects_unknown_transport` (`test_phase11c.py:266`)
- `test_mcp_cli_add_rejects_stdio_without_command` (`test_phase11c.py:273`)
- `test_mcp_cli_add_rejects_sse_without_url` (`test_phase11c.py:282`)
- `test_mcp_cli_add_rejects_duplicate_name` (`test_phase11c.py:291`)

These all pass `NAME` explicitly, so they exercise the existing branch unchanged.

## 8. Migration / compatibility

- **CLI signature change** — `NAME` flips from required to optional. **Forward-compatible**: every existing call site keeps working. Scripts that relied on the "Missing argument" exit code for a non-TTY no-args call still see exit != 0, just with a different message.
- **Help text** — `oc mcp add --help` updates to reflect the optional name + the discovery branch. Worth a single-line note: *"With no NAME on a TTY, interactively pick from bundled presets."*
- **`oc mcp install` and `oc mcp presets` are untouched.** Power users keep their scripts.
- **`docs/mcp-catalog.md`** referenced at `cli_mcp.py:12` — add one paragraph noting the new entry point. Cheap.

## 9. Out-of-scope follow-ups

Not in this change, but worth listing so we don't forget:

- **Auto-set env vars from the picker.** Right now the picker installs the preset and warns about missing env vars; it could *prompt* for them and write to `<profile>/.env` (reuse `cli_profile.py:830` shape). Lands cleanly as a v2.
- **Search-while-typing.** `rich.prompt` is one-shot; for 19 presets a plain prompt is fine. If we ever hit ~50+ presets, swap in a fuzzy picker (e.g. `questionary` or our own `BrowserHarness`-style fuzzy widget).
- **Remote catalog in the picker.** Could merge `oc mcp catalog --remote` results into the table. Slow on a cold cache (24h TTL), so behind a flag at minimum.
- **`mcp install` should also accept zero args** and trigger the same picker. Same pattern, smaller surface. Worth doing in the same PR if review time allows.

## 10. Acceptance checklist

- [ ] `oc mcp add` on a TTY shows the presets table + prompt
- [ ] Picking a valid slug installs the preset (same outcome as `oc mcp install <slug>`)
- [ ] Picking `custom` prints example flag shapes and exits 0
- [ ] Picking an unknown slug exits 1 with a discoverable hint
- [ ] Empty input exits 0 (silent abort)
- [ ] Ctrl-C exits 130
- [ ] `oc mcp add` on non-TTY exits != 0 with a hint mentioning `presets` and `install`
- [ ] `oc mcp add NAME --transport ...` (existing path) is byte-for-byte identical
- [ ] All four existing `test_phase11c.py` add-tests still pass
- [ ] New tests cover all five discovery-flow branches
- [ ] `ruff check` clean
- [ ] `oc mcp add --help` reflects the optional name

## 11. Why this is the right shape

Three alternatives considered:

1. **Just fix the help text.** Cheapest. But the error still looks like a wall — most users won't read help they just saw fail. Rejected: solves nothing real.

2. **Auto-render presets on bare `add` (no prompt).** Print the table and exit 0. Better than the error, but still a dead-end — the user has to copy-paste a slug into a fresh command. Rejected: missed the easy follow-through.

3. **Discovery picker (this design).** Renders the table AND moves the user one keystroke from a working install. Falls back gracefully on non-TTY. Reuses `mcp_install` so behavior diverges in zero places. Picked: lowest surface area for the largest UX delta.

The whole change is ~50 LOC plus tests. Risk is contained to the no-args branch.
