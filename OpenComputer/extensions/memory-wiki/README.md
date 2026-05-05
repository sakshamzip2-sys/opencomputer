# memory-wiki

Markdown-files-on-disk wiki memory plugin. Adds five tools:

- `WikiMemoryAdd(title, body, tags?, slug?)` → `{slug}`
- `WikiMemoryRead(slug)` → `{found, title, body, tags, ...}`
- `WikiMemorySearch(query)` → `{slugs}` (ripgrep when available; in-memory regex otherwise)
- `WikiMemoryBacklinks(slug)` → `{backlinks: [referrer-slug, ...]}`
- `WikiMemoryDelete(slug)` → `{deleted}`

Storage: `<profile-home>/wiki/<slug>.md` with YAML-style frontmatter.
Backlinks index cached at `<profile-home>/wiki/.backlinks.json`.

## Wikilinks

Reference other notes by writing `[[other-slug]]` in the body. The
backlinks index updates automatically on add. Use
`WikiMemoryBacklinks` to discover what links to a given note.

## Install + enable

```bash
oc plugin enable memory-wiki
```

No external deps — all stdlib (markdown is just text on disk; ripgrep
optional for faster search).

## MVP scope (2026-05-05)

Shipped:

- Add/read/search/delete on per-profile markdown files.
- Slug auto-generation + collision suffix.
- Frontmatter (title, tags, created_at, updated_at).
- Wikilink extraction + reverse-index cache.
- ripgrep search with in-memory fallback.

Explicitly out of scope (open issues / future PRs):

- **Conflict resolution** for simultaneous edits — last write wins.
  Use a version-controlled wiki tool (Obsidian/Logseq) if that
  matters.
- **Cross-profile sharing** — each profile's wiki is fully isolated.
- **Title-update on link rename** — renaming a slug doesn't update
  inbound `[[slug]]` references; that's a manual sweep.
- **Conflict-free merge** of edits from two devices.

## Tests

```bash
pytest tests/test_memory_wiki_backend.py -v
```
