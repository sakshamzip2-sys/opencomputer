# Skill Spec Compliance — Design

**Date:** 2026-05-02
**Status:** approved (brainstorm complete, awaiting plan)
**Sub-project:** SP1 of the Anthropic-API-parity scope (C)
**Authors:** Saksham + Claude Code (Opus 4.7)

---

## 1. Context

Anthropic published a strict spec for Agent Skills covering authoring rules, frontmatter validation, progressive disclosure, and runtime constraints. OpenComputer implements skills today (127 bundled, plus an auto-skill-evolution extractor that generates new ones from observed patterns) but the implementation pre-dates the published spec. An audit on 2026-05-02 found:

- **Validator gaps:** No XML detection, no reserved-word check (`anthropic`/`claude`), no third-person voice lint, no body-size warning. Two parallel validators ([`skill_manage.py`](../../../opencomputer/tools/skill_manage.py) for agent-created skills and [`agentskills_validator.py`](../../../opencomputer/skills_hub/agentskills_validator.py) for hub-installed skills) with inconsistent rules.
- **Synthesis prompt voice bug:** [`synthesis_request.j2:19`](../../../opencomputer/evolution/prompts/synthesis_request.j2) tells the LLM "Lead with 'Use when...'" — that produces 2nd-person directive descriptions ("Use when you need..."). The Anthropic spec requires 3rd-person ("Processes...", "Synthesizes..."). Every auto-extracted skill is currently born non-compliant.
- **Bundled corpus violations:** 14 of 127 SKILL.md files exceed the spec's 500-line "optimal" body size, worst being `research-paper-writing` at 2,375 lines.
- **No spec-compliance test suite:** Zero tests audit the bundled corpus against the Anthropic spec.

Progressive disclosure is already correctly implemented (only frontmatter goes in the system prompt; bodies load on demand via `SkillTool`). That part needs no change.

## 2. Goals

1. Make every NEW skill (auto-extracted, agent-created, hub-installed) Anthropic-spec compliant by default.
2. Bring the bundled corpus to a known, documented compliance state — fix what genuinely broken, exempt what's intentional, audit what's borderline.
3. Eliminate the silent dual-validator drift that lets agent-created skills bypass the hub validator.
4. Stop the auto-skill-evolution extractor from manufacturing non-compliant descriptions.

## 3. Non-goals

- Splitting every >500-line skill (most earn their size; case-by-case audit instead).
- Auto-rewriting non-compliant existing descriptions (preserves human judgment about what each skill is for).
- LLM-based voice detection (overkill; cheap regex deny-list is sufficient).
- Changing the progressive-disclosure mechanics (already correct).
- Adding a `≤8 skills per dispatch` cap — Anthropic's ≤8 is an API request constraint, not a local dispatcher constraint; OC's slash dispatcher is a different surface.

## 4. Approach: two-tier enforcement

| Severity | Examples | Behavior |
|---|---|---|
| **Error** | reserved words in name, XML in name/description, malformed frontmatter, name regex violation | blocks ingestion always — for both new submissions AND bundled corpus |
| **Warning** | body >500 lines, missing TOC on >100-line file, voice violations in description, deeply nested references | blocks new submissions in `strict=True` mode; allowed for bundled corpus in `strict=False` (CI test mode) |

Rationale: hard errors represent contract violations that genuinely break things. Warnings represent quality guidelines (Anthropic's own spec calls 500 lines "for optimal performance," not a hard limit). The bundled corpus contains 14 deliberately-large skills that earn their size; we don't want to force-split them, but we also don't want NEW skills inheriting that pattern.

## 5. Design

### 5.1 Validator unification

Make [`opencomputer/tools/skill_manage.py`](../../../opencomputer/tools/skill_manage.py) **delegate** to [`opencomputer/skills_hub/agentskills_validator.py`](../../../opencomputer/skills_hub/agentskills_validator.py). The hub validator becomes the single source of truth.

New public API on `agentskills_validator`:

```python
@dataclass
class ValidationReport:
    errors: list[ValidationIssue]      # blocking
    warnings: list[ValidationIssue]    # advisory unless strict=True
    skill_path: Path | None

@dataclass
class ValidationIssue:
    rule: str                           # e.g. "name.reserved_word"
    severity: Literal["error", "warning"]
    field: str | None                   # e.g. "frontmatter.name"
    message: str
    line: int | None

def validate_skill_md(
    text: str,
    *,
    strict: bool = True,
    path: Path | None = None,
) -> ValidationReport: ...

def validate_skill_dir(
    skill_dir: Path,
    *,
    strict: bool = True,
) -> ValidationReport: ...
```

The existing `validate_frontmatter()` function is refactored to call `validate_skill_md()` internally and raise `ValidationError` on the first hard error (preserving its old contract for callers that expect raise-on-error). New code paths use `validate_skill_md()` directly and inspect the `ValidationReport`.

`skill_manage.py::_validate_frontmatter()` is removed and its callsite calls `validate_skill_md(text, strict=True)` then checks `report.errors`. Its existing test cases all become inputs to the unified validator's test suite — coverage preserved, single source of truth achieved.

### 5.2 The 4 missing checks

Implemented in `agentskills_validator.py`:

```python
RESERVED_WORDS = frozenset({"anthropic", "claude"})
XML_TAG_RE = re.compile(r"<[a-zA-Z!?/]")

# Voice deny-list — leading-word patterns that indicate non-3rd-person
VOICE_DENY_RE = re.compile(
    r"^\s*(I|You|We|Let me|I'?ll|I can|You can|This (?:helps|lets) you)\b",
    re.IGNORECASE,
)
```

| Check | Rule ID | Severity | Logic |
|---|---|---|---|
| Reserved word in name | `name.reserved_word` | error | `name in RESERVED_WORDS or any(rw in name for rw in RESERVED_WORDS)` |
| XML tag in name | `name.xml_tag` | error | `XML_TAG_RE.search(name)` |
| XML tag in description | `description.xml_tag` | error | `XML_TAG_RE.search(description)` |
| Voice violation in description | `description.voice` | warning | `VOICE_DENY_RE.search(description)` |
| Body > 500 lines | `body.size_warn` | warning | `body.count('\n') > 500` |

The voice check has one allowed exception: if the leading-word match is inside backtick code spans, it doesn't count. Implementation: strip ``` `...` ``` spans before regex.

The body-size check is suppressed if frontmatter contains `size_review_date: <ISO date>` — this is the documented-exemption pattern.

### 5.3 Synthesis prompt rewrite

Replace [`opencomputer/evolution/prompts/synthesis_request.j2`](../../../opencomputer/evolution/prompts/synthesis_request.j2) frontmatter rules section with:

```jinja2
1. **Frontmatter** (YAML, between two `---` lines):
   - `name`: lowercase + hyphens only, ≤50 chars. **Use gerund form** —
     `processing-pdfs` not `pdf-helper`; `analyzing-spreadsheets` not
     `excel-utility`. Must NOT duplicate any of these existing names:
     {{ existing_names | join(", ") }}.
     Must NOT contain reserved words: anthropic, claude.
   - `description`: ONE line, ≤280 characters. **Third-person voice** —
     describe what the skill does, like a system describing itself:
     "Processes...", "Synthesizes...", "Generates...".
     **NEVER** start with "I", "You", "We", "Let me", "I can help".
     Must include BOTH:
       - WHAT the skill does (the action verb phrase)
       - WHEN to use it (a "Use when..." clause)
     Pattern: "<3rd-person verb phrase>. Use when <trigger condition>."
     Example: "Synthesizes git commit messages from staged diffs.
              Use when the user asks for help writing commit messages."
```

Add a new "Forbidden content" subsection:

```jinja2
4. **Must NOT include**:
   - Any specific user data, file paths, or session content from the
     samples above.
   - Shell commands that delete data (`rm -rf`, `format`, `eject`, etc.).
   - Personally identifying info from the samples.
   - **Time-sensitive content** like "after August 2025" or "before next
     quarter". If you must reference a deprecated approach, use a
     collapsible "Old patterns" section instead.
```

The `≤ 280 characters` cap is intentional: well under Anthropic's 1024-char ceiling, but enough room for WHAT + WHEN. (Old cap was 100 — too tight to fit both.)

### 5.4 Constraint synchronization

Update [`opencomputer/evolution/constraints.py`](../../../opencomputer/evolution/constraints.py):

- `MAX_DESCRIPTION_LEN = 500` → `MAX_DESCRIPTION_LEN = 280` (matches new prompt cap).
- Add docstring explaining intentional divergence from Anthropic's 1024 (we want concise; long descriptions hurt skill-discovery routing).
- Add post-synthesis call to `validate_skill_md(text, strict=True)`. If errors, raise `ConstraintViolation`. If only warnings, log them with the skill slug.

### 5.5 Bundled corpus migration

A one-time pass (executed during this PR):

1. Run `validate_skill_md(strict=False)` against each of the 127 bundled skills. Capture all errors + warnings.
2. **Fix all errors immediately** in the same PR (likely zero after audit, but verify).
3. **For the 14 over-500-line skills**: audit each:
   - **Split**: `research-paper-writing` (2,375), `claude-code` (744), `hermes-agent` (705) — these have natural section boundaries (top-level h2 headings) that map cleanly to reference files.
   - **Exempt**: the remaining 11 are domain-API skills (dspy, weights-and-biases, audiocraft, p5js, segment-anything, etc.) where the size reflects API surface density — splitting would harm discoverability. Add `size_review_date: 2026-05-02` to their frontmatter.
4. **Voice-violation cleanup**: deferred to a follow-up cleanup PR. Document the count in the migration report.
5. Commit a migration report at `docs/skills/2026-05-02-bundled-corpus-audit.md` — list every warning, the decision taken, and the rationale.

### 5.6 Test plan

New tests:

- `tests/skills_hub/test_bundled_corpus_compliance.py` — iterates every `SKILL.md` under `opencomputer/skills/`, runs `validate_skill_md(strict=False)`, asserts zero errors. Warnings are reported but don't fail.
- Each new check gets unit tests in `tests/skills_hub/test_agentskills_validator.py`:
  - `test_name_reserved_word_blocks_anthropic` / `test_name_reserved_word_blocks_claude`
  - `test_name_xml_tag_blocks` / `test_description_xml_tag_blocks`
  - `test_description_voice_warning_first_person` (4 cases: "I ", "You ", "We ", "Let me ")
  - `test_description_voice_allows_code_span` (e.g. `` "Processes `you` markers" ``)
  - `test_body_size_warning_over_500_lines`
  - `test_body_size_exempt_when_review_date_present`
- Synthesis-extractor tests in `tests/test_evolution_synthesize_skill.py`:
  - `test_synthesized_skill_passes_strict_validator` (synthetic fixture, no LLM call)
  - `test_synthesized_description_includes_what_and_when`

Existing tests:

- `tests/skills_hub/test_agentskills_validator.py` — unchanged (the new validator is a strict superset).
- `tests/skills_guard/test_skill_manage_gate.py` — unchanged (skill_manage now delegates).
- `tests/test_evolution_synthesize_skill.py` — slug + atomic-write tests unchanged; description-length test updated for 280-char cap.

### 5.7 Documentation

New: `docs/skills/AUTHORING.md` — condensed Anthropic spec for authors:
- Name rules (gerund, lowercase-hyphen, ≤50 chars, reserved words).
- Description rules (3rd-person, WHAT+WHEN, ≤280 chars, no XML).
- Body size guidance (≤500 lines optimal; `size_review_date` exemption).
- Reference file depth (≤1 level from SKILL.md).
- TOC requirement for >100-line files.
- Worked good/bad examples.

Update: `docs/evolution/README.md` — add a "Description style guide" section with worked good/bad examples, lifted from AUTHORING.md.

Update: `docs/skills/2026-05-02-bundled-corpus-audit.md` (new, the migration report).

## 6. Decisions log

| Decision | Why |
|---|---|
| Composition over replacement for validator unification | Keeps existing tests valid; minimizes blast radius; single source of truth |
| Voice check as warning, not error | Regex deny-list is heuristic; making it an error would block legitimate edge cases (technical descriptions that happen to start with "Let me know if..." in a sub-clause) |
| `MAX_DESCRIPTION_LEN = 280` not Anthropic's 1024 | OC routes by description similarity; long descriptions degrade routing. 280 is enough for WHAT + WHEN |
| Audit-then-split top 3, exempt the rest of the 14 | Forcing all 14 to split would harm domain-API skill discoverability. The top 3 have natural section boundaries; the rest don't |
| `size_review_date` frontmatter field for documented exemption | Lets future audits track when a decision was made and re-evaluate without reverse-archaeology |
| Voice cleanup deferred to follow-up PR | Touching 127 skill descriptions in this PR would balloon the diff; warning gives us the data without forcing the cleanup |
| No auto-rewrite of non-compliant descriptions | Preserves human judgment about each skill's purpose |

## 7. Risks

1. **Voice regex false positives.** Technical descriptions might legitimately use "you" in a non-leading position (e.g. "Lets you query BigQuery..."). Mitigation: regex anchors to sentence start only, code-span allowlist, severity is warning not error.
2. **Splitting `research-paper-writing` (2,375 lines) might break in-skill cross-references.** Mitigation: scan for relative anchors before splitting; add fix-up step if found.
3. **`size_review_date` becomes a permanent free pass.** Mitigation: include the date in the frontmatter so future audits can re-evaluate based on age.
4. **The synthesis prompt change might confuse the LLM into producing worse skills initially while it adapts.** Mitigation: the post-synthesis validator catches violations and raises before write; the prompt change is paired with stricter post-hoc gating.

## 8. Open questions

None — all design decisions resolved. (Earlier draft had questions about LLM-based voice detection and auto-rewrite; both ruled out.)

## 9. Success criteria

- [ ] `pytest tests/skills_hub/test_bundled_corpus_compliance.py` passes — zero errors across all 127 bundled skills.
- [ ] All 4 new checks have passing unit tests.
- [ ] Synthesis prompt updated; new test verifies generated skills pass strict validator.
- [ ] `MAX_DESCRIPTION_LEN` synchronized between prompt cap and constraint constant; documented divergence from Anthropic's 1024.
- [ ] `skill_manage.py` delegates to hub validator; old `_validate_frontmatter` path removed (no dead code).
- [ ] 3 large bundled skills split into reference files; 11 exempted with `size_review_date`; migration report committed.
- [ ] `docs/skills/AUTHORING.md` exists with worked good/bad examples.
- [ ] `docs/evolution/README.md` updated with description style guide.
- [ ] Full pytest suite green; ruff clean.

## 10. Out of scope (deferred to later sub-projects)

- **SP2** (PDF + Provider Hardening): Bedrock citations footgun, PDF detection, document-block construction, channel-adapter PDF passthrough.
- **SP3** (Files API + Artifact Loop): Files API client, `oc files` CLI, tool-result spillover wiring.
- **SP4** (Server-side Tools / Skills-via-API): demand-gated, re-evaluate after SP3 completes.
- **Voice-violation cleanup pass** on the 127 bundled skills (follow-up PR after this lands).
- **Description-similarity routing** in the slash dispatcher (deferred until concrete demand from the 127→growing skill count).

## 11. References

- [Agent Skills overview](https://docs.claude.com/en/agents-and-tools/agent-skills/overview)
- [Skill authoring best practices](https://docs.claude.com/en/agents-and-tools/agent-skills/best-practices)
- [Files API](https://docs.claude.com/en/build-with-claude/files) (referenced by SP3)
- [PDF support](https://docs.claude.com/en/build-with-claude/pdf-support) (referenced by SP2)
- Audit findings: this conversation, 2026-05-02 (parallel Explore agents over `opencomputer/skills/`, `opencomputer/evolution/`, `extensions/*-provider/`).
