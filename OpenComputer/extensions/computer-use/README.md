# computer-use

**macOS-only.** Universal background desktop control for OpenComputer.

Registers a single `computer_use` tool that drives any macOS app —
screenshots, mouse, keyboard, scroll — **without stealing the user's cursor,
keyboard focus, or Space**. The agent and the user can co-work on the same
machine.

Ported from Hermes Agent's `tools/computer_use/` package. Backed by
[`cua-driver`](https://github.com/trycua/cua) over an MCP stdio transport.

## Why background computer-use

`cua-driver` uses private SkyLight SPIs (`SLEventPostToPid`, pid-scoped event
posting, remote AX observers) to focus and post events to a specific process
*without* raising its window or routing the system cursor. The agent can
click a button in a backgrounded Safari window while you keep typing in your
editor.

## Install

The plugin is **not enabled by default**. The `cua-driver` binary is an
external dependency installed via an upstream curl-piped script:

```sh
oc computer-use install            # fresh install
oc computer-use install --upgrade  # re-pull the latest release
oc computer-use status             # check the binary
```

`oc doctor` reports a health row; `oc doctor --fix` runs the installer for
you on macOS.

After installing, **grant macOS permissions** — both are required:

* System Settings → Privacy & Security → **Accessibility**
* System Settings → Privacy & Security → **Screen Recording**

Both must allow the terminal / OpenComputer process.

## The `computer_use` tool

One consolidated tool with an `action` discriminator:

| action | side effects | what it does |
|---|---|---|
| `capture` | none | screenshot + element index (`mode`: `som` / `vision` / `ax`) |
| `list_apps` | none | enumerate running apps |
| `wait` | none | sleep up to 30 s |
| `click` / `double_click` / `right_click` / `middle_click` | mutating | click by `element` index or `coordinate` |
| `scroll` | mutating | wheel scroll |
| `type` | mutating | type text (dangerous shell patterns hard-blocked) |
| `key` | mutating | key combo, e.g. `cmd+s` (destructive system combos hard-blocked) |
| `set_value` | mutating | set a popup / slider value directly (no menu open) |
| `focus_app` | mutating | route input to an app without raising its window |
| `drag` | mutating | not supported by the cua-driver backend |

Preferred workflow for vision models: `capture(mode='som')` returns a
screenshot with numbered overlays on every interactable element plus an
accessibility tree — then `click(element=N)`. Far more reliable than pixel
coordinates. Text-only models can drive via `mode='ax'` (tree only, no image).

### Screenshot return shape

`ToolResult.content` is a plain string in OpenComputer. Capture results
write the PNG/JPEG to `<profile>/cache/computer_use_screenshots/` and return
its path as `screenshot_path` in the JSON. The agent surfaces it to the user
via `MEDIA:<path>` — the same convention `browser-harness`'s `browser_vision`
uses. Captures older than 24 h are pruned automatically.

## Safety

* **Consent.** The whole tool is gated at `ConsentTier.EXPLICIT` via a
  `CapabilityClaim` (`computer_use.macos_desktop_control`). A single
  `BaseTool` can't vary its claim per-action, and the mutating action set
  dominates, so the read-only actions (`capture` / `wait` / `list_apps`)
  share the EXPLICIT gate — safer than under-claiming.
* **Hard-blocked `type` patterns.** `curl … | bash`, `wget … | bash`,
  `sudo rm -rf`, fork bombs — refused regardless of consent.
* **Hard-blocked key combos.** Empty-trash, force-delete, lock-screen,
  log-out, force-log-out — refused regardless of consent (log-out would kill
  the session OpenComputer runs in).
* **No focus theft.** `focus_app` is a pure window-selector;
  `raise_window=True` is intentionally ignored.

## Configuration

| env var | default | meaning |
|---|---|---|
| `OPENCOMPUTER_COMPUTER_USE_BACKEND` | `cua` | `cua` or `noop` (tests) |
| `OPENCOMPUTER_CUA_DRIVER_CMD` | `cua-driver` | binary name / path |
| `OPENCOMPUTER_CUA_DRIVER_VERSION` | `0.5.0` | version pin reference |

## Architecture

```
plugin.py      register() — surfaces ComputerUseTool + `oc computer-use` CLI + doctor row
tool.py        ComputerUseTool(BaseTool) — dispatch, safety guards, capture persistence
backend.py     ComputerUseBackend ABC + UIElement / CaptureResult / ActionResult
cua_backend.py CuaDriverBackend — MCP stdio client + background asyncio bridge
schema.py      the OpenAI function-calling schema for `computer_use`
installer.py   install_cua_driver() — the cua-driver curl-piped installer
doctor.py      health check (macOS gate, binary present, mcp SDK importable)
cli.py         `oc computer-use install|status`
```

The cua-driver SPIs are not Apple-public and can break on OS updates. Pin a
known-good release with `OPENCOMPUTER_CUA_DRIVER_VERSION` if reproducibility
across an OS bump matters.
