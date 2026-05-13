---
name: session-report
description: Generate an explorable HTML report of OpenComputer session usage (tokens, cache reads/writes, compactions, top prompts, per-source breakdown) from the per-profile sessions.db. Use when the user asks for "session report", "OC usage summary", "cost report", "weekly OC usage", "where did my tokens go", or "expensive prompts last week". Reads SessionDB directly (no JSONL transcript parsing). Outputs a self-contained HTML file in the current working directory.
version: 0.1.0
---

# OpenComputer Session Report

Produce a self-contained HTML report of OC usage and save it to the current
working directory. Adapted from Anthropic's session-report skill — reads
`~/.opencomputer/<profile>/sessions.db` (SQLite) instead of
`~/.claude/projects/**.jsonl`.

## Steps

1. **Run the analyzer.** Default window: last 7 days. Honor `24h`, `30d`,
   `all`, or any `<n>d` / `<n>h` value:

   ```bash
   python3 <skill-dir>/analyze_sessions.py --since 7d > /tmp/oc-session-report.json
   ```

   Optional flags:
   - `--profile <name>` — restrict to a single profile (default: all)
   - `--out <path>` — write JSON to a file instead of stdout
   - `--top 15` — number of "top prompts" to include
   - `--cache-break 100000` — uncached-input threshold for the cache-break list

2. **Read** `/tmp/oc-session-report.json`. Skim:
   - `overall` — totals for the window (sessions, input_tokens.{uncached,cache_read,cache_write,total}, output_tokens, compactions, cache_breaks_over_100k)
   - `by_project` — per-source breakdown (cli / webui / discord / telegram / cron / tool / api_server)
   - `by_subagent_type` — delegate-lineage rollup (per `agent_type`) — empty if the `subagents` table is absent
   - `by_skill` — Skill-tool invocation counts from `tool_usage`
   - `cache_breaks` — sessions with uncached input ≥ threshold
   - `top_prompts` — user prompts ranked by character length (proxy for cost)

3. **Copy the template** to the output path in the current working
   directory:

   ```bash
   cp <skill-dir>/template.html ./oc-session-report-$(date +%Y%m%d-%H%M).html
   ```

4. **Edit the output file** (use Edit, not Write — preserve the
   template's JS/CSS):

   - Replace the contents of `<script id="report-data" type="application/json">`
     with the full JSON from step 1. The page's JS renders the hero total,
     all tables, bars, and drill-downs from this blob automatically.
   - Fill the `<!-- AGENT: anomalies -->` block with **3–5 one-line findings**.
     Express figures as a **% of total tokens** wherever possible
     (total = `overall.input_tokens.total + overall.output_tokens`). One
     line per finding, exact markup:
     ```html
     <div class="take bad"><div class="fig">41.2%</div><div class="txt"><b>cli</b> consumed 41% of the week across just 3 sessions</div></div>
     ```
     Classes: `.take bad` (red — waste/anomalies), `.take good` (green —
     healthy), `.take info` (blue — neutral facts). The `.fig` is one
     short number (a %, a count, a multiplier). Look for: a source/skill
     eating a disproportionate share, cache_read_tokens / total_input <
     85%, a single prompt > 2% of total, subagent types averaging > 1M
     tokens/call, cache_breaks_over_100k clustering on one source.
   - Fill the `<!-- AGENT: optimizations -->` block at the bottom with
     1–4 `<div class="callout">` suggestions tied to specific rows
     (e.g. "`/weekly-status` triggered 7 subagents = 8.1% of total — scope
     to fewer parallel agents").

5. **Report** the saved file path to the user. Do not open or render it.

## Notes

- The template is the source of interactivity (sorting, expand/collapse,
  block-char bars). Your job is data + narrative, not markup.
- Keep commentary terse and specific — reference actual session ids,
  source names, numbers, timestamps.
- `top_prompts` length is character-count, not tokens. It's a useful
  proxy when SessionDB doesn't carry per-message token usage.
- If the JSON is > 2MB, trim `top_prompts` to 100 and `cache_breaks` to
  100 before embedding.

## How this differs from the Claude Code session-report

| Aspect | Claude Code | OC |
|--------|-------------|----|
| Data source | `~/.claude/projects/**.jsonl` | `~/.opencomputer/<profile>/sessions.db` |
| Per-message tokens | from `usage.{input,cache_creation,cache_read}_tokens` | from per-session `input_tokens` / `cache_read_tokens` columns (no per-message breakdown) |
| Subagents | `<project>/<sessionId>/subagents/*.jsonl` | `subagents` table (delegate-lineage) |
| Resume/dedup | uuid-based | not needed (DB rows are canonical) |
| Cross-profile | n/a (one Claude install) | iterates every `~/.opencomputer/<profile>/` |
| Project field | derived from cwd path | `source` column (cli/webui/discord/telegram/cron/tool/api_server) |

## See also

- `oc usage sessions` — compact CLI rollup of token usage
- `oc context show` / `oc context list` — per-session context-window utilisation
- `oc memory audit` — separate tool for `MEMORY.md` / `USER.md` curation
