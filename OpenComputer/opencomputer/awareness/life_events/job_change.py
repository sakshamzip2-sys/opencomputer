"""JobChange life-event detector.

Watches for browser visits to job-search domains + textual signals
(resignation, severance, etc.). Each matched trigger contributes
weight=0.4 evidence; 2 visits in the window crosses the 0.7
surface threshold.

2026-04-28 refactor: trigger-term matching uses
:class:`plugin_sdk.classifier.RegexClassifier` (FIRST_MATCH policy)
so the term table is shared with the abstraction. Behavior preserved
exactly — same 7 triggers, same weight, same payload shape.
"""
from __future__ import annotations

import re

from opencomputer.awareness.life_events.pattern import (
    EvidenceItem,
    LifeEventPattern,
    SurfacingPolicy,
)
from plugin_sdk.classifier import AggregationPolicy, RegexClassifier, Rule

# Trigger rules — domain hits on job-search sites + textual cues.
# Pattern bodies are quoted literally because the original codebase
# uses simple substring containment; using ``re.escape`` here would
# also work but keeping explicit bytes is more readable for an audit.
# Matched against the lowercased URL+title concatenation. FIRST_MATCH
# policy: any single trigger fires once with weight 0.4.
_JOB_TRIGGER_RULES: tuple[Rule[str], ...] = (
    Rule(pattern=re.compile(re.escape("linkedin.com/jobs")), label="linkedin.com/jobs"),
    Rule(pattern=re.compile(re.escape("indeed.com")), label="indeed.com"),
    Rule(pattern=re.compile(re.escape("glassdoor.com")), label="glassdoor.com"),
    Rule(pattern=re.compile(re.escape("resignation")), label="resignation"),
    Rule(pattern=re.compile(re.escape("severance")), label="severance"),
    Rule(pattern=re.compile(re.escape("unemployment")), label="unemployment"),
    Rule(pattern=re.compile(re.escape("notice period")), label="notice period"),
)


_JOB_CLASSIFIER: RegexClassifier[str] = RegexClassifier(
    rules=_JOB_TRIGGER_RULES,
    policy=AggregationPolicy.FIRST_MATCH,
)


class JobChange(LifeEventPattern):
    pattern_id: str = "job_change"
    surfacing: SurfacingPolicy = "hint"
    surface_threshold: float = 0.7

    def consider_event(self, event_type, metadata):
        if event_type != "browser_visit":
            return None
        url = str(metadata.get("url", "")).lower()
        title = str(metadata.get("title", "")).lower()
        blob = url + " " + title
        verdict = _JOB_CLASSIFIER.classify(blob)
        if not verdict.has_match:
            return None
        # FIRST_MATCH → exactly one label; preserve the original
        # `term` field name in the payload for backward compatibility.
        return EvidenceItem(
            timestamp=float(metadata.get("visit_time", 0.0)),
            weight=0.4,  # 2 hits = 0.8 = above 0.7 threshold
            source="browser",
            payload={"term": verdict.top_label, "url": url[:200]},
        )

    def hint_text(self) -> str:
        return (
            "I noticed your work rhythm has shifted recently — different tabs, "
            "different patterns. If anything's on your mind work-wise, I'm here."
        )
