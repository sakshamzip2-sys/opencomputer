---
name: research-paper-writing
title: Research Paper Writing Pipeline
description: End-to-end pipeline for writing ML/AI research papers — from experiment design through analysis, drafting, revision, and submission. Covers NeurIPS, ICML, ICLR, ACL, AAAI, COLM. Integrates automated experiment monitoring, statistical analysis, iterative writing, and citation verification.
version: 1.1.0
author: Orchestra Research
license: MIT
dependencies: [semanticscholar, arxiv, habanero, requests, scipy, numpy, matplotlib, SciencePlots]
platforms: [linux, macos]
metadata:
  hermes:
    tags: [Research, Paper Writing, Experiments, ML, AI, NeurIPS, ICML, ICLR, ACL, AAAI, COLM, LaTeX, Citations, Statistical Analysis]
    category: research
    related_skills: [arxiv, ml-paper-writing, subagent-driven-development, plan]
    requires_toolsets: [terminal, files]

---

# Research Paper Writing Pipeline

End-to-end pipeline for producing publication-ready ML/AI research papers targeting **NeurIPS, ICML, ICLR, ACL, AAAI, and COLM**. This skill covers the full research lifecycle: experiment design, execution, monitoring, analysis, paper writing, review, revision, and submission.

This is **not a linear pipeline** — it is an iterative loop. Results trigger new experiments. Reviews trigger new analysis. The agent must handle these feedback loops.

```
┌─────────────────────────────────────────────────────────────┐
│                    RESEARCH PAPER PIPELINE                  │
│                                                             │
│  Phase 0: Project Setup ──► Phase 1: Literature Review      │
│       │                          │                          │
│       ▼                          ▼                          │
│  Phase 2: Experiment     Phase 5: Paper Drafting ◄──┐      │
│       Design                     │                   │      │
│       │                          ▼                   │      │
│       ▼                    Phase 6: Self-Review      │      │
│  Phase 3: Execution &           & Revision ──────────┘      │
│       Monitoring                 │                          │
│       │                          ▼                          │
│       ▼                    Phase 7: Submission               │
│  Phase 4: Analysis ─────► (feeds back to Phase 2 or 5)     │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## When To Use This Skill

Use this skill when:
- **Starting a new research paper** from an existing codebase or idea
- **Designing and running experiments** to support paper claims
- **Writing or revising** any section of a research paper
- **Preparing for submission** to a specific conference or workshop
- **Responding to reviews** with additional experiments or revisions
- **Converting** a paper between conference formats
- **Writing non-empirical papers** — theory, survey, benchmark, or position papers (see [Paper Types Beyond Empirical ML](#paper-types-beyond-empirical-ml))
- **Designing human evaluations** for NLP, HCI, or alignment research
- **Preparing post-acceptance deliverables** — posters, talks, code releases

## Core Philosophy

1. **Be proactive.** Deliver complete drafts, not questions. Scientists are busy — produce something concrete they can react to, then iterate.
2. **Never hallucinate citations.** AI-generated citations have ~40% error rate. Always fetch programmatically. Mark unverifiable citations as `[CITATION NEEDED]`.
3. **Paper is a story, not a collection of experiments.** Every paper needs one clear contribution stated in a single sentence. If you can't do that, the paper isn't ready.
4. **Experiments serve claims.** Every experiment must explicitly state which claim it supports. Never run experiments that don't connect to the paper's narrative.
5. **Commit early, commit often.** Every completed experiment batch, every paper draft update — commit with descriptive messages. Git log is the experiment history.

### Proactivity and Collaboration

**Default: Be proactive. Draft first, ask with the draft.**

| Confidence Level | Action |
|-----------------|--------|
| **High** (clear repo, obvious contribution) | Write full draft, deliver, iterate on feedback |
| **Medium** (some ambiguity) | Write draft with flagged uncertainties, continue |
| **Low** (major unknowns) | Ask 1-2 targeted questions via `clarify`, then draft |

| Section | Draft Autonomously? | Flag With Draft |
|---------|-------------------|-----------------|
| Abstract | Yes | "Framed contribution as X — adjust if needed" |
| Introduction | Yes | "Emphasized problem Y — correct if wrong" |
| Methods | Yes | "Included details A, B, C — add missing pieces" |
| Experiments | Yes | "Highlighted results 1, 2, 3 — reorder if needed" |
| Related Work | Yes | "Cited papers X, Y, Z — add any I missed" |

**Block for input only when**: target venue unclear, multiple contradictory framings, results seem incomplete, explicit request to review first.

---

## Phase 0: Project Setup
For workspace setup, contribution identification, compute budgeting, and multi-author coordination, see [references/phase-0-project-setup.md](references/phase-0-project-setup.md).
Quick start: `git init` a `paper-draft` branch, create `paper/ experiments/ code/ results/ tasks/ human_eval/`, articulate the one-sentence contribution before any writing.

## Phase 1: Literature Review
For seed-paper discovery, breadth-first/depth-second search, citation verification, and related-work organization, see [references/phase-1-literature-review.md](references/phase-1-literature-review.md).
Quick start: list 3-5 anchor papers, expand via Semantic Scholar / arXiv, then verify EVERY citation programmatically — AI-generated bib entries hallucinate ~40% of the time.

## Phase 2: Experiment Design
For mapping claims to experiments, baseline design, evaluation protocols, runner scripts, and human eval, see [references/phase-2-experiment-design.md](references/phase-2-experiment-design.md).
Quick start: write the claims first, then design the minimum experiment that could falsify each claim. Every experiment must declare which claim it serves.

## Phase 3: Experiment Execution & Monitoring
For launch patterns, cron monitoring, failure handling, result-commit discipline, and the experiment journal, see [references/phase-3-experiment-execution.md](references/phase-3-experiment-execution.md).
Quick start: launch under tmux/nohup, set a `cronjob` to poll for `final_info.json`, commit completed result batches with descriptive messages, append a one-paragraph entry to `experiment_journal.md` per run.

## Phase 4: Result Analysis
For aggregation, statistical significance, story-finding, figure/table generation, and the `experiment_log.md` bridge to writeup, see [references/phase-4-result-analysis.md](references/phase-4-result-analysis.md).
Quick start: aggregate raw JSON into a long-format DataFrame, run McNemar / bootstrapped CIs / Cohen's h as appropriate, write the experiment log BEFORE drafting — it is the connective tissue between data and prose.

## Iterative Refinement: Strategy Selection
For when to use autoreason vs critique-and-revise vs single pass — and the generation-evaluation gap that drives the choice — see [references/iterative-refinement.md](references/iterative-refinement.md).
Quick start: mid-tier model on a constrained task → autoreason. Frontier model on an unconstrained task → critique-and-revise or single pass. Code with tests → autoreason (code variant).

## Phase 5: Paper Drafting
For the full drafting pipeline — title, abstract, figure 1, intro, methods, experiments, related work, limitations, conclusion, appendix, ethics, datasheets, plus complete LaTeX preamble, templates, tables, figures, pseudocode, TikZ patterns, and revision tracking — see [references/phase-5-paper-drafting.md](references/phase-5-paper-drafting.md).
Quick start: draft top-down (title → abstract → figure 1 → intro → methods → results → related work → limitations → conclusion). Two-pass refinement on each section. Use `\method{}` macro consistently from day one.

## Phase 6: Self-Review & Revision
For the reviewer-ensemble simulation, visual review pass (VLM), claim verification, feedback prioritization, revision cycle, rebuttal writing, and paper-evolution tracking, see [references/phase-6-self-review.md](references/phase-6-self-review.md).
Quick start: spawn 3 reviewer personas (skeptical, generous, methodologist) in parallel, each producing scores + structured concerns. Prioritize the union of MUST-FIX items before the one-week-out deadline.

## Phase 7: Submission Preparation
For the conference checklist, anonymization, formatting verification, pre-compilation validation, final compilation, conference-specific requirements, format conversion, camera-ready, arXiv strategy, and code packaging, see [references/phase-7-submission-preparation.md](references/phase-7-submission-preparation.md).
Quick start: anonymize → run `chktex` and `latexdiff` → compile clean → submit. Camera-ready and arXiv go up only after acceptance.

## Phase 8: Post-Acceptance Deliverables
For the conference poster, talk/spotlight slides, and blog post / social media impact-maximization workflow, see [references/phase-8-post-acceptance.md](references/phase-8-post-acceptance.md).
Quick start: poster repurposes Figure 1 + the table that won the paper. Talk: 5-7 slides per 10 min. Blog post within 2 weeks of camera-ready while attention is fresh.

## Workshop & Short Papers
For workshop submission strategy and ACL Short Papers / Findings adaptations, see [references/workshop-and-short-papers.md](references/workshop-and-short-papers.md).
Quick start: workshops accept work-in-progress (positive results not required); short papers (4 pages) need ONE clean experiment with one clean finding.

## Paper Types Beyond Empirical ML
For theory papers, survey/tutorial papers, benchmark papers, and position papers — and how the pipeline adapts to each — see [references/paper-types-non-empirical.md](references/paper-types-non-empirical.md).
Quick start: theory papers replace experiments with proofs (always include intuition before formalism). Benchmark papers need stronger ethics + datasheets. Position papers must be falsifiable.

## Hermes Agent Integration
For the related Hermes skills, tools reference, tool usage patterns, state management with `memory`/`todo`, cron monitoring, communication patterns, and decision points requiring human input, see [references/hermes-agent-integration.md](references/hermes-agent-integration.md).
Quick start: persist project state via `memory`, track TODOs via `todo`, schedule polling via `cronjob`, and use `clarify` only when truly blocked.

## Reviewer Evaluation Criteria

Understanding what reviewers look for helps focus effort:

| Criterion | What They Check |
|-----------|----------------|
| **Quality** | Technical soundness, well-supported claims, fair baselines |
| **Clarity** | Clear writing, reproducible by experts, consistent notation |
| **Significance** | Community impact, advances understanding |
| **Originality** | New insights (doesn't require new method) |

**Scoring (NeurIPS 6-point scale):**
- 6: Strong Accept — groundbreaking, flawless
- 5: Accept — technically solid, high impact
- 4: Borderline Accept — solid, limited evaluation
- 3: Borderline Reject — weaknesses outweigh
- 2: Reject — technical flaws
- 1: Strong Reject — known results or ethics issues

See [references/reviewer-guidelines.md](references/reviewer-guidelines.md) for detailed guidelines, common concerns, and rebuttal strategies.

---

## Common Issues and Solutions

| Issue | Solution |
|-------|----------|
| Abstract too generic | Delete first sentence if it could prepend any ML paper. Start with your specific contribution. |
| Introduction exceeds 1.5 pages | Split background into Related Work. Front-load contribution bullets. |
| Experiments lack explicit claims | Add: "This experiment tests whether [specific claim]..." before each one. |
| Reviewers find paper hard to follow | Add signposting, use consistent terminology, make figure captions self-contained. |
| Missing statistical significance | Add error bars, number of runs, statistical tests, confidence intervals. |
| Scope creep in experiments | Every experiment must map to a specific claim. Cut experiments that don't. |
| Paper rejected, need to resubmit | See Conference Resubmission in Phase 7. Address reviewer concerns without referencing reviews. |
| Missing broader impact statement | See Step 5.10. Most venues require it. "No negative impacts" is almost never credible. |
| Human eval criticized as weak | See Step 2.5 and [references/human-evaluation.md](references/human-evaluation.md). Report agreement metrics, annotator details, compensation. |
| Reviewers question reproducibility | Release code (Step 7.9), document all hyperparameters, include seeds and compute details. |
| Theory paper lacks intuition | Add proof sketches with plain-language explanations before formal proofs. See [references/paper-types.md](references/paper-types.md). |
| Results are negative/null | See Phase 4.3 on handling negative results. Consider workshops, TMLR, or reframing as analysis. |

---

## Reference Documents

| Document | Contents |
|----------|----------|
| [references/writing-guide.md](references/writing-guide.md) | Gopen & Swan 7 principles, Perez micro-tips, Lipton word choice, Steinhardt precision, figure design |
| [references/citation-workflow.md](references/citation-workflow.md) | Citation APIs, Python code, CitationManager class, BibTeX management |
| [references/checklists.md](references/checklists.md) | NeurIPS 16-item, ICML, ICLR, ACL requirements, universal pre-submission checklist |
| [references/reviewer-guidelines.md](references/reviewer-guidelines.md) | Evaluation criteria, scoring, common concerns, rebuttal template |
| [references/sources.md](references/sources.md) | Complete bibliography of all writing guides, conference guidelines, APIs |
| [references/experiment-patterns.md](references/experiment-patterns.md) | Experiment design patterns, evaluation protocols, monitoring, error recovery |
| [references/autoreason-methodology.md](references/autoreason-methodology.md) | Autoreason loop, strategy selection, model guide, prompts, scope constraints, Borda scoring |
| [references/human-evaluation.md](references/human-evaluation.md) | Human evaluation design, annotation guidelines, agreement metrics, crowdsourcing QC, IRB guidance |
| [references/paper-types.md](references/paper-types.md) | Theory papers (proof writing, theorem structure), survey papers, benchmark papers, position papers |

### LaTeX Templates

Templates in `templates/` for: **NeurIPS 2025**, **ICML 2026**, **ICLR 2026**, **ACL**, **AAAI 2026**, **COLM 2025**.

See [templates/README.md](templates/README.md) for compilation instructions.

### Key External Sources

**Writing Philosophy:**
- [Neel Nanda: How to Write ML Papers](https://www.alignmentforum.org/posts/eJGptPbbFPZGLpjsp/highly-opinionated-advice-on-how-to-write-ml-papers)
- [Sebastian Farquhar: How to Write ML Papers](https://sebastianfarquhar.com/on-research/2024/11/04/how_to_write_ml_papers/)
- [Gopen & Swan: Science of Scientific Writing](https://cseweb.ucsd.edu/~swanson/papers/science-of-writing.pdf)
- [Lipton: Heuristics for Scientific Writing](https://www.approximatelycorrect.com/2018/01/29/heuristics-technical-scientific-writing-machine-learning-perspective/)
- [Perez: Easy Paper Writing Tips](https://ethanperez.net/easy-paper-writing-tips/)

**APIs:** [Semantic Scholar](https://api.semanticscholar.org/api-docs/) | [CrossRef](https://www.crossref.org/documentation/retrieve-metadata/rest-api/) | [arXiv](https://info.arxiv.org/help/api/basics.html)

**Venues:** [NeurIPS](https://neurips.cc/Conferences/2025/PaperInformation/StyleFiles) | [ICML](https://icml.cc/Conferences/2025/AuthorInstructions) | [ICLR](https://iclr.cc/Conferences/2026/AuthorGuide) | [ACL](https://github.com/acl-org/acl-style-files)
