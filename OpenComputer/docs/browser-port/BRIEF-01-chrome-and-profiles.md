# BRIEF — `profiles/` + `chrome/` (Waves W0b + W0c)

> Chrome process management + profile/config resolution. The foundation everything else launches on top of.
> Deep dive: [01-chrome-and-profiles.md](../refs/openclaw/browser/01-chrome-and-profiles.md) (628 lines — read end-to-end).

## What to build

### `extensions/browser-control/profiles/`

| File | Public API |
|---|---|
| `config.py` | `@dataclass(slots=True) class ResolvedBrowserConfig(...)` · `@dataclass(slots=True) class ResolvedBrowserProfile(name, driver, cdp_url, cdp_port, user_data_dir, executable, color, ...)` (full field list in deep dive §"Data structure field reference") |
| `capabilities.py` | `@dataclass(frozen=True) class BrowserProfileCapabilities(uses_chrome_mcp: bool, mode: str)` · `get_browser_profile_capabilities(profile: ResolvedBrowserProfile) -> BrowserProfileCapabilities` |
| `resolver.py` | `resolve_browser_config(raw: dict, full_config: dict) -> ResolvedBrowserConfig` · `resolve_profile(resolved: ResolvedBrowserConfig, profile_name: str) -> ResolvedBrowserProfile \| None` · **Pull-based, NOT a watcher** — re-call per request |
| `service.py` | `create_profile(...)` · `delete_profile(...)` · `reset_profile(...)` — manipulate the user-data-dir on disk |

### `extensions/browser-control/chrome/`

| File | Public API |
|---|---|
| `executables.py` | `resolve_chrome_executable(platform: str = sys.platform) -> str \| None` · `read_browser_version(path: str) -> str \| None` · `parse_browser_major_version(version: str) -> int \| None` |
| `launch.py` | `async def launch_openclaw_chrome(resolved: ResolvedBrowserConfig, profile: ResolvedBrowserProfile) -> RunningChrome` · `class RunningChrome` |
| `lifecycle.py` | `async def stop_openclaw_chrome(running: RunningChrome, *, timeout_ms: int = 2500) -> None` · `is_chrome_reachable(cdp_url, *, timeout_ms, ssrf_policy) -> bool` · `is_chrome_cdp_ready(cdp_url, ...) -> bool` |
| `decoration.py` | `decorate_openclaw_profile(user_data_dir: str, *, name: str, color_argb: int) -> None` — atomic mutation of `Local State` and `Default/Preferences` JSON |

## What to read first

1. The deep dive's "Bootstrap launch" algorithm (13 steps, with timeouts).
2. The cross-platform Chrome detection section — three different strategies per OS.
3. The "User-data-dir lifecycle" — bootstrap-spawn-then-close pattern is non-obvious.
4. The data-structure field reference at the bottom of the deep dive — defines every field of `ResolvedBrowserProfile` with TS shape + Python `@dataclass` mapped next to it.

## Acceptance

- [ ] On macOS, `resolve_chrome_executable()` finds Chrome via plist OR osascript OR hardcoded fallback (test on a clean macOS without `which chrome`)
- [ ] On Linux, finds Chrome via xdg-mime OR `which`. Mock test with no `xdg-mime` available.
- [ ] On Windows, finds Chrome via `winreg` (`HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\...App Paths\chrome.exe`) OR Program Files fallback
- [ ] `launch_openclaw_chrome` spawns Chrome and returns when CDP `/json/version` becomes reachable AND `Browser.getVersion` over WS replies (the stronger readiness check)
- [ ] `decorate_openclaw_profile` mutates `Preferences` JSON **atomically** via `_utils/atomic_write.py` (no direct `open("w")`)
- [ ] `resolve_profile` returns the right capability bits: `uses_chrome_mcp=True` for `existing-session` driver, `False` for `openclaw`
- [ ] Tests in `tests/test_chrome_*.py` and `tests/test_profiles_*.py`. Cover: cross-platform exe resolution (mocked subprocess + os hooks), bootstrap-launch readiness loop, profile decoration writes the right JSON keys.
- [ ] No imports from `opencomputer/*`

## Do NOT reproduce

| OpenClaw bug | Don't do |
|---|---|
| Non-atomic `Preferences` JSON write | Use `_utils/atomic_write.atomic_write_json` |
| `extraArgs` security gap (allows arbitrary Chrome flags from config) | Allowlist what's permitted; reject the rest |
| No file watcher for hot-reload | Keep pull-based per-request — that's correct, not a bug |

## Configuration shape

Profile config nests under `~/.opencomputer/<profile>/config.yaml`:

```yaml
browser:
  enabled: true
  control_port: 18792    # default; choose differently from browser-bridge's 18791
  profiles:
    openclaw:
      driver: openclaw
      executable: null   # auto-detect
      color_argb: 0xFF4A90E2
    user:
      driver: existing-session
      # CDP URL discovered at runtime via Chrome MCP
```

## Open questions

- Which existing OpenComputer profile-config helper do we reuse for parsing (`agent/profile_config.py`)? Confirm before duplicating.
- Color value format: ARGB int (OpenClaw) vs `#RRGGBBAA` string (more YAML-friendly)? Recommend hex string in YAML, convert to int in `resolve_profile`.

## Where to ask

PR description with `**Question:**` line. Don't block.
