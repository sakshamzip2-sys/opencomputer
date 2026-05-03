# BRIEF — `_utils/` (Wave 0a)

> Small leaf utilities used everywhere else. Build these first; everything depends on them.
> Deep-dive context: §7 of [BLUEPRINT.md](BLUEPRINT.md), plus utility sections of [06-client-and-utils.md](../refs/openclaw/browser/06-client-and-utils.md).

## What to build

`extensions/browser-control/_utils/`:

| File | Public API | Notes |
|---|---|---|
| `atomic_write.py` | `atomic_write_text(path: str, content: str) -> None` · `atomic_write_bytes(path: str, content: bytes) -> None` · `atomic_write_json(path: str, data: Any, *, indent: int \| None = 2) -> None` | Sequence: write to sibling tmp → `os.fsync(fd)` → `os.replace(tmp, path)`. **The fsync is mandatory** — OpenClaw skips it; we don't. |
| `url_pattern.py` | `match(pattern: str, url: str, *, mode: Literal["exact", "glob", "substring"]) -> bool` | Glob supports `*` only. Drop `?` from the docstring (OpenClaw claims it works, doesn't implement). Case-sensitive by default; trailing slashes normalized. |
| `safe_filename.py` | `sanitize(name: str, *, max_len: int = 200) -> str` | Strip control chars (`< 0x20`, `0x7F`), replace `os.sep` and `/`, cap length, preserve extension. |
| `trash.py` | `move_to_trash(path: str) -> None` | Just call `send2trash.send2trash(path)`. Don't build a per-platform fallback. |
| `errors.py` | `class BrowserServiceError(Exception): ...` with `status: int \| None`, `code: str \| None`, plus `from_response(status: int, body: dict) -> BrowserServiceError` | Single typed error class — server side throws subclasses, client side catches the base. Maps 429 specially with a hint message. |

## What to read first

1. [BLUEPRINT.md §7](BLUEPRINT.md#7-bugs-we-dont-reproduce) — the bug list (4 of 5 land here).
2. [06-client-and-utils.md](../refs/openclaw/browser/06-client-and-utils.md) — utility sections in both the skeleton and deep second-pass. Especially the "stricter than skeleton implied" notes on `paths.ts` and the missing-fsync finding on `output-atomic.ts`.

You should not need to read OpenClaw TS source for this brief — the deep dive notes capture the relevant invariants.

## Acceptance

- [ ] All 5 files exist with the public API above
- [ ] Each has unit tests in `tests/test__utils_*.py` covering the happy path + at least one edge case (e.g. atomic-write on a path with no parent dir; URL pattern with leading wildcard)
- [ ] No imports from `opencomputer/*` (SDK boundary test passes)
- [ ] `ruff check` clean
- [ ] `atomic_write_*` includes `fsync` — verify via test that mocks `os.fsync` and asserts it was called
- [ ] `trash.py` uses `send2trash` exclusively — no shelling out

## Do NOT reproduce

| OpenClaw bug | Don't do |
|---|---|
| Non-atomic `Preferences` JSON write ([01-chrome-and-profiles.md](../refs/openclaw/browser/01-chrome-and-profiles.md)) | Use `atomic_write_json` everywhere we touch JSON. Never `open("w")` directly. |
| Missing fsync in `output-atomic.ts` | Always `os.fsync(fd)` before `os.replace()` |
| Buggy Linux trash fallback | Don't try to ship our own — `send2trash` is the answer |
| `?` claimed but unimplemented in `url-pattern.ts` | Either drop the `?` claim from docstring (recommended) or implement properly |

## Open questions

- Symlink policy for `paths.py` (which we'd put here too if it grows): OpenClaw rejects most symlinks, not just escaping ones. Recommend: replicate strict policy via `O_NOFOLLOW`. Confirm with W0b owner before they need it.

## Where to ask

If you hit something the deep dive doesn't cover, post a `**Question:**` line in your PR description. Don't block — proceed under a stated assumption and flag it.
