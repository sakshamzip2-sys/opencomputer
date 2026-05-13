# Code Modernization (OpenComputer port)

Port of Anthropic's `code-modernization` Claude-Code plugin into OpenComputer.

A 7-step pipeline for migrating legacy systems (COBOL, classic Java,
.NET Framework, RPG, Perl CGI, etc.) to modern stacks — without losing
the business rules embedded in 30 years of patches.

## Pipeline (in order)

| Step | Skill | What it does |
|------|-------|--------------|
| 1 | `/modernize-assess`         | Full discovery + complexity + debt + effort estimation |
| 2 | `/modernize-map`            | Dependency / topology / data-lineage rendered as diagrams |
| 3 | `/modernize-extract-rules`  | Mine business rules into testable Given/When/Then specs |
| 4 | `/modernize-brief`          | Synthesize into a phased Modernization Brief |
| 5 | `/modernize-reimagine`      | Multi-agent greenfield rebuild from extracted intent |
| 6 | `/modernize-transform`      | Surgical single-module rewrite with equivalence tests |
| 7 | `/modernize-harden`         | Security hardening pass with remediation patches |

Each step is invocable via the leading-slash skill name (the OC
slash-skill fallback loads the `SKILL.md` body as authoritative
context).

## Specialist subagents

Five agents under `agents/` — invoke via the `Agent` tool with
`subagent_type=<name>`:

| Agent | Use for |
|-------|---------|
| `legacy-analyst`            | Reading old code, deriving structural understanding |
| `business-rules-extractor`  | Pulling rules out of source into specs |
| `architecture-critic`       | Adversarial review of proposed targets / transformed modules |
| `security-auditor`          | OWASP / CWE / dependency CVEs / secrets / injection |
| `test-engineer`             | Characterization + contract + equivalence tests |

## Conventions assumed by the workflow

- Legacy systems live under `legacy/<system-name>/` (one dir per system)
- Analysis artifacts go under `analysis/<system-name>/`
- Modernized output goes under `modernized/<system-name>/<module>/`

These conventions match the original plugin and are referenced by every
skill in the pipeline.

## Dependencies

None at install time. Specific skills may shell out to:
- `cloc` (line-of-code metrics) — recommended in `modernize-assess`
- `mermaid` CLI / `graphviz` (rendering diagrams) — recommended in `modernize-map`
- `bandit`, `semgrep`, language-specific SAST tools — recommended in `modernize-harden`

The skills will fall back to text output when these aren't installed.

## License

MIT (matches the upstream Anthropic plugin).
