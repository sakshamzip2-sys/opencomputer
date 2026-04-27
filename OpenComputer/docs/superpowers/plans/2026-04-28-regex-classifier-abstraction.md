# RegexClassifier abstraction — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `Classifier[Label]` protocol + `RegexClassifier[Label]` implementation in `plugin_sdk/`. Migrate `vibe_classifier` and one life-event detector (`job_change`) to the new shape as worked examples. Zero behavior change — same regex patterns, same outputs.

**Architecture:** Generic protocol over a label type `L`. Pluggable back-ends (regex now; embedding/LLM later via the same protocol). One `Rule[L]` dataclass holds the (pattern, label, weight) tuple. One `ClassifierVerdict[L]` returns matched labels + weights + which rules fired. Aggregation policy is an enum: `FIRST_MATCH`, `ALL_MATCHES`, `WEIGHTED_SUM`. The vibe classifier and life-event detectors keep their public API; their implementations swap to the new abstraction.

**Tech Stack:** Python 3.12+ generics (PEP 695 `class C[L]` syntax), `re`, existing pytest setup, `plugin_sdk` package.

**Out of scope (deliberate):**
- Embedding back-end — separate PR.
- Migrating threat scanners, bash safety, python denylist, sensitive-app filter — security-critical; need separate review.
- Migrating persona classifier — its compound logic (foreground app + state-query + file extensions + time of day) doesn't cleanly fit `RegexClassifier`. Stays as-is until a richer abstraction lands.
- Graph back-end — sequenced for after this lands.

---

## File Structure

**Create:**
- `plugin_sdk/classifier.py` — `Classifier` protocol, `RegexClassifier` implementation, `Rule`, `ClassifierVerdict`, `AggregationPolicy`
- `tests/test_plugin_sdk_classifier.py` — abstraction unit tests

**Modify:**
- `plugin_sdk/__init__.py` — export the new symbols
- `opencomputer/agent/vibe_classifier.py` — keep `classify_vibe(messages) -> str` and `VALID_VIBES` API; reimplement on top of `RegexClassifier`
- `opencomputer/awareness/life_events/job_change.py` — extract trigger-term matching to a module-level `RegexClassifier`; keep the `JobChange` class API
- `tests/test_vibe_classifier.py` — no changes needed (tests behavior not implementation)
- `tests/test_life_events.py` (or whichever exists for `job_change`) — no changes needed

**Tests:**
- `tests/test_plugin_sdk_classifier.py` (new)

---

## Tasks

### Task 1: Create the protocol + dataclass shapes

**Files:**
- Create: `plugin_sdk/classifier.py`
- Test: `tests/test_plugin_sdk_classifier.py`

- [ ] **Step 1: Write the failing tests for the data shapes**

Create `tests/test_plugin_sdk_classifier.py`:

```python
"""Tests for plugin_sdk.classifier — protocol, Rule, Verdict, RegexClassifier."""
from __future__ import annotations

import re

import pytest

from plugin_sdk.classifier import (
    AggregationPolicy,
    ClassifierVerdict,
    RegexClassifier,
    Rule,
)


# ── Rule shape ───────────────────────────────────────────────────────


def test_rule_minimal_construction():
    r = Rule(pattern=re.compile(r"hello"), label="greet")
    assert r.label == "greet"
    assert r.weight == 1.0
    assert r.severity == ""
    assert r.description == ""


def test_rule_full_construction():
    r = Rule(
        pattern=re.compile(r"\bdrop\s+table\b", re.IGNORECASE),
        label="sql_drop",
        weight=0.9,
        severity="critical",
        description="SQL DROP TABLE statement",
    )
    assert r.label == "sql_drop"
    assert r.weight == 0.9
    assert r.severity == "critical"


def test_rule_is_frozen():
    r = Rule(pattern=re.compile(r"x"), label="x")
    with pytest.raises(Exception):
        r.label = "y"  # frozen → raises FrozenInstanceError or AttributeError


# ── ClassifierVerdict shape ──────────────────────────────────────────


def test_verdict_empty_when_no_matches():
    v: ClassifierVerdict[str] = ClassifierVerdict(
        matched_labels=[],
        weights_by_label={},
        triggered_rules=(),
    )
    assert v.matched_labels == []
    assert v.top_label is None
    assert not v.has_match


def test_verdict_top_label_returns_first_matched():
    rule = Rule(pattern=re.compile(r"x"), label="x")
    v: ClassifierVerdict[str] = ClassifierVerdict(
        matched_labels=["x", "y"],
        weights_by_label={"x": 1.0},
        triggered_rules=(rule,),
    )
    assert v.top_label == "x"
    assert v.has_match


def test_verdict_is_frozen():
    v: ClassifierVerdict[str] = ClassifierVerdict(
        matched_labels=[], weights_by_label={}, triggered_rules=()
    )
    with pytest.raises(Exception):
        v.matched_labels = ["x"]


# ── AggregationPolicy enum ───────────────────────────────────────────


def test_aggregation_policy_values():
    assert AggregationPolicy.FIRST_MATCH.value == "first_match"
    assert AggregationPolicy.ALL_MATCHES.value == "all_matches"
    assert AggregationPolicy.WEIGHTED_SUM.value == "weighted_sum"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/saksham/Vscode/claude/OpenComputer && source .venv/bin/activate && pytest tests/test_plugin_sdk_classifier.py -v`

Expected: All tests FAIL with `ModuleNotFoundError: No module named 'plugin_sdk.classifier'`.

- [ ] **Step 3: Create the module**

Create `plugin_sdk/classifier.py`:

```python
"""Generic classifier protocol + regex implementation.

Codebase-wide pattern: ``_PATTERNS = [(regex, label), ...]`` + iterate +
match → label. Found in 7+ places (vibe, threat scanners, instruction
detector, bash safety, sensitive apps, life-event detectors). This
module abstracts the shape so:

1. Each call site stops re-rolling its own ``_PATTERNS`` table.
2. A future embedding / LLM back-end can plug in via the same protocol.
3. Tests + telemetry get a uniform surface.

Out of scope here: embedding/LLM back-ends. They land later as
separate ``EmbeddingClassifier`` / ``LLMClassifier`` implementations
of :class:`Classifier`.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Generic, Protocol, TypeVar, runtime_checkable

L = TypeVar("L")


class AggregationPolicy(str, Enum):
    """How to combine multiple matched rules into a verdict.

    - ``FIRST_MATCH``: return the first matching rule's label only.
      Rule order encodes priority (vibe classifier, bash_safety).
    - ``ALL_MATCHES``: return every matched label, in rule order.
      For fan-out detectors (life-events, threat scanners).
    - ``WEIGHTED_SUM``: accumulate ``Rule.weight`` per label, return
      labels ranked by total weight (instruction_detector style).
    """

    FIRST_MATCH = "first_match"
    ALL_MATCHES = "all_matches"
    WEIGHTED_SUM = "weighted_sum"


@dataclass(frozen=True, slots=True)
class Rule(Generic[L]):
    """One pattern → label entry in a classifier table.

    ``weight`` defaults to 1.0; only ``WEIGHTED_SUM`` policies care.
    ``severity`` and ``description`` are advisory metadata for audit
    output (security classifiers want them; vibe doesn't).
    """

    pattern: re.Pattern[str]
    label: L
    weight: float = 1.0
    severity: str = ""
    description: str = ""


@dataclass(frozen=True, slots=True)
class ClassifierVerdict(Generic[L]):
    """The result of running a Classifier.

    - ``matched_labels``: ordered list of labels that fired. For
      FIRST_MATCH: 0 or 1 entries. For ALL_MATCHES: rule order. For
      WEIGHTED_SUM: ranked descending by accumulated weight.
    - ``weights_by_label``: only populated for WEIGHTED_SUM (empty dict
      otherwise — saves callers from special-casing).
    - ``triggered_rules``: which Rule objects matched. Useful for
      audit logs ("rule X.Y fired because pattern matched 'foo'").
    """

    matched_labels: list[L]
    weights_by_label: dict[L, float]
    triggered_rules: tuple[Rule[L], ...]

    @property
    def has_match(self) -> bool:
        return bool(self.matched_labels)

    @property
    def top_label(self) -> L | None:
        return self.matched_labels[0] if self.matched_labels else None


@runtime_checkable
class Classifier(Protocol, Generic[L]):
    """Pluggable text → labels classifier.

    Implementations: :class:`RegexClassifier` (this module), future
    ``EmbeddingClassifier`` and ``LLMClassifier`` against the same
    protocol. Callers depend only on ``classify`` returning a verdict.
    """

    def classify(self, text: str) -> ClassifierVerdict[L]:
        ...


class RegexClassifier(Generic[L]):
    """Regex-table back-end of :class:`Classifier`.

    Construct with a sequence of :class:`Rule` plus an
    :class:`AggregationPolicy`; call :meth:`classify`. Stateless and
    thread-safe — :class:`re.Pattern` is fine to share.
    """

    def __init__(
        self,
        rules: Sequence[Rule[L]],
        policy: AggregationPolicy = AggregationPolicy.FIRST_MATCH,
    ) -> None:
        self._rules: tuple[Rule[L], ...] = tuple(rules)
        self._policy = policy

    @property
    def rules(self) -> tuple[Rule[L], ...]:
        return self._rules

    @property
    def policy(self) -> AggregationPolicy:
        return self._policy

    def classify(self, text: str) -> ClassifierVerdict[L]:
        if not text:
            return ClassifierVerdict(
                matched_labels=[], weights_by_label={}, triggered_rules=()
            )

        if self._policy is AggregationPolicy.FIRST_MATCH:
            for rule in self._rules:
                if rule.pattern.search(text):
                    return ClassifierVerdict(
                        matched_labels=[rule.label],
                        weights_by_label={},
                        triggered_rules=(rule,),
                    )
            return ClassifierVerdict([], {}, ())

        if self._policy is AggregationPolicy.ALL_MATCHES:
            triggered: list[Rule[L]] = []
            seen: OrderedDict[L, None] = OrderedDict()
            for rule in self._rules:
                if rule.pattern.search(text):
                    triggered.append(rule)
                    if rule.label not in seen:
                        seen[rule.label] = None
            return ClassifierVerdict(
                matched_labels=list(seen),
                weights_by_label={},
                triggered_rules=tuple(triggered),
            )

        # WEIGHTED_SUM
        triggered = []
        weights: dict[L, float] = {}
        for rule in self._rules:
            if rule.pattern.search(text):
                triggered.append(rule)
                weights[rule.label] = weights.get(rule.label, 0.0) + rule.weight
        ranked = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)
        return ClassifierVerdict(
            matched_labels=[label for label, _ in ranked],
            weights_by_label=weights,
            triggered_rules=tuple(triggered),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_plugin_sdk_classifier.py -v`

Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add plugin_sdk/classifier.py tests/test_plugin_sdk_classifier.py
git commit -m "feat(plugin_sdk): RegexClassifier abstraction — protocol + regex back-end"
```

---

### Task 2: Test the RegexClassifier behavior under each aggregation policy

**Files:**
- Modify: `tests/test_plugin_sdk_classifier.py`

- [ ] **Step 1: Append behavior tests**

Append to `tests/test_plugin_sdk_classifier.py`:

```python
# ── RegexClassifier — FIRST_MATCH policy ─────────────────────────────


def test_first_match_returns_first_rule_in_order():
    rules = [
        Rule(pattern=re.compile(r"\burgent\b", re.IGNORECASE), label="urgent"),
        Rule(pattern=re.compile(r"\bnow\b", re.IGNORECASE), label="now"),
    ]
    c: RegexClassifier[str] = RegexClassifier(rules, AggregationPolicy.FIRST_MATCH)
    v = c.classify("this is urgent and i need it now")
    assert v.matched_labels == ["urgent"]
    assert v.top_label == "urgent"
    assert len(v.triggered_rules) == 1
    assert v.triggered_rules[0].label == "urgent"


def test_first_match_returns_empty_on_no_hits():
    rules = [Rule(pattern=re.compile(r"x"), label="x")]
    c: RegexClassifier[str] = RegexClassifier(rules, AggregationPolicy.FIRST_MATCH)
    v = c.classify("nothing here")
    assert v.matched_labels == []
    assert v.top_label is None
    assert v.has_match is False


def test_first_match_short_circuits_on_first_hit():
    """The classifier must not evaluate later rules once it has a match.
    Verify by including a rule whose pattern would have matched but is
    placed AFTER the winning rule."""
    rules = [
        Rule(pattern=re.compile(r"foo"), label="A"),
        Rule(pattern=re.compile(r"foo"), label="B"),
    ]
    c: RegexClassifier[str] = RegexClassifier(rules, AggregationPolicy.FIRST_MATCH)
    v = c.classify("foo")
    assert v.matched_labels == ["A"]
    assert len(v.triggered_rules) == 1


# ── RegexClassifier — ALL_MATCHES policy ─────────────────────────────


def test_all_matches_returns_every_matched_label_in_order():
    rules = [
        Rule(pattern=re.compile(r"sql"), label="db"),
        Rule(pattern=re.compile(r"http"), label="net"),
        Rule(pattern=re.compile(r"shell"), label="exec"),
    ]
    c: RegexClassifier[str] = RegexClassifier(rules, AggregationPolicy.ALL_MATCHES)
    v = c.classify("a sql + http payload")
    assert v.matched_labels == ["db", "net"]
    assert len(v.triggered_rules) == 2


def test_all_matches_dedups_repeated_labels():
    """Two rules with the same label should produce one label in the
    output but both Rules in triggered_rules (the audit trail wants
    every fire, the user-facing labels list wants distinct values)."""
    rules = [
        Rule(pattern=re.compile(r"alpha"), label="bucket"),
        Rule(pattern=re.compile(r"beta"), label="bucket"),
    ]
    c: RegexClassifier[str] = RegexClassifier(rules, AggregationPolicy.ALL_MATCHES)
    v = c.classify("alpha and beta")
    assert v.matched_labels == ["bucket"]
    assert len(v.triggered_rules) == 2


# ── RegexClassifier — WEIGHTED_SUM policy ────────────────────────────


def test_weighted_sum_ranks_labels_by_total_weight():
    rules = [
        Rule(pattern=re.compile(r"alpha"), label="A", weight=0.3),
        Rule(pattern=re.compile(r"beta"), label="B", weight=0.5),
        Rule(pattern=re.compile(r"gamma"), label="A", weight=0.4),
    ]
    c: RegexClassifier[str] = RegexClassifier(rules, AggregationPolicy.WEIGHTED_SUM)
    v = c.classify("alpha beta gamma")
    # A: 0.3 + 0.4 = 0.7; B: 0.5
    assert v.matched_labels == ["A", "B"]
    assert v.weights_by_label == {"A": 0.7, "B": 0.5}


def test_weighted_sum_no_matches_returns_empty():
    rules = [Rule(pattern=re.compile(r"x"), label="X", weight=0.5)]
    c: RegexClassifier[str] = RegexClassifier(rules, AggregationPolicy.WEIGHTED_SUM)
    v = c.classify("no x letter present? wait it does")  # contains 'x' actually
    # adjust to truly absent
    v = c.classify("none of those triggers")
    assert v.matched_labels == []
    assert v.weights_by_label == {}


# ── Empty input handling ─────────────────────────────────────────────


def test_classify_empty_string_returns_empty_verdict():
    rules = [Rule(pattern=re.compile(r"x"), label="x")]
    c: RegexClassifier[str] = RegexClassifier(rules, AggregationPolicy.FIRST_MATCH)
    v = c.classify("")
    assert v.matched_labels == []
    assert v.weights_by_label == {}
    assert v.triggered_rules == ()


# ── Protocol satisfaction ────────────────────────────────────────────


def test_regex_classifier_satisfies_classifier_protocol():
    from plugin_sdk.classifier import Classifier

    rules = [Rule(pattern=re.compile(r"x"), label="x")]
    c: RegexClassifier[str] = RegexClassifier(rules)
    # Runtime-checkable protocol
    assert isinstance(c, Classifier)


# ── Generics: non-string label types ─────────────────────────────────


def test_classifier_works_with_enum_labels():
    """Rule[L] should be usable with non-string labels, e.g. enums."""
    from enum import Enum

    class Severity(Enum):
        LOW = "low"
        HIGH = "high"

    rules = [
        Rule(pattern=re.compile(r"warn"), label=Severity.LOW),
        Rule(pattern=re.compile(r"crash"), label=Severity.HIGH),
    ]
    c: RegexClassifier[Severity] = RegexClassifier(rules, AggregationPolicy.ALL_MATCHES)
    v = c.classify("warn before the crash")
    assert v.matched_labels == [Severity.LOW, Severity.HIGH]
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_plugin_sdk_classifier.py -v`

Expected: All ~17 tests PASS (7 from Task 1 + 10 new).

- [ ] **Step 3: Commit**

```bash
git add tests/test_plugin_sdk_classifier.py
git commit -m "test(plugin_sdk): RegexClassifier policy + protocol behavior"
```

---

### Task 3: Export classifier symbols from plugin_sdk

**Files:**
- Modify: `plugin_sdk/__init__.py`

- [ ] **Step 1: Read current __init__**

Run: `grep -n "^from plugin_sdk\|^__all__" plugin_sdk/__init__.py | head -10`

- [ ] **Step 2: Add imports + __all__ entries**

Open `plugin_sdk/__init__.py`. Add these imports near the other re-exports (the file already has a re-export block around module top):

```python
from plugin_sdk.classifier import (
    AggregationPolicy,
    Classifier,
    ClassifierVerdict,
    RegexClassifier,
    Rule,
)
```

Add to `__all__` list (alphabetized within the existing list):

```python
"AggregationPolicy",
"Classifier",
"ClassifierVerdict",
"RegexClassifier",
"Rule",
```

- [ ] **Step 3: Verify import path works**

Run: `python -c "from plugin_sdk import RegexClassifier, Rule, AggregationPolicy, ClassifierVerdict, Classifier; print('OK')"`

Expected: prints `OK`.

- [ ] **Step 4: Run plugin_sdk boundary test**

Run: `pytest tests/test_phase6a.py::test_plugin_sdk_does_not_import_opencomputer -v`

Expected: PASS. (`plugin_sdk/classifier.py` is pure-stdlib + re; no `opencomputer` imports.)

- [ ] **Step 5: Commit**

```bash
git add plugin_sdk/__init__.py
git commit -m "feat(plugin_sdk): export Classifier protocol + RegexClassifier from package root"
```

---

### Task 4: Migrate vibe_classifier to RegexClassifier

**Files:**
- Modify: `opencomputer/agent/vibe_classifier.py`
- Test: `tests/test_vibe_classifier.py` (existing — must still pass unchanged)

- [ ] **Step 1: Verify existing tests pass before refactor**

Run: `pytest tests/test_vibe_classifier.py -q`

Expected: All ~14 tests PASS. (Establishes the baseline that the refactor must preserve.)

- [ ] **Step 2: Read current vibe_classifier.py**

Run: `cat opencomputer/agent/vibe_classifier.py`

Note the existing patterns: `_FRUSTRATED_RE`, `_EXCITED_RE`, `_TIRED_RE`, `_CURIOUS_RE`, `_STUCK_RE`. Order in `classify_vibe` is: stuck → frustrated → excited → tired → curious → calm.

- [ ] **Step 3: Replace the implementation, keep the public API**

Replace the entire body of `opencomputer/agent/vibe_classifier.py` with:

```python
"""A.4 — Mood classifier for the companion thread.

Per-turn classification of the user's apparent emotional state from
the most recent ~3 messages. Heuristic regex; zero LLM cost,
deterministic, sub-millisecond.

2026-04-28 refactor: implementation now uses
:class:`plugin_sdk.classifier.RegexClassifier` so the per-pattern
table and the FIRST_MATCH evaluation loop live in one shared
abstraction. Public API (``classify_vibe(messages) -> str`` and
``VALID_VIBES``) is unchanged.

Vibes:
    frustrated — "doesn't work", "frustrating", "nothing works"
    excited    — "amazing!", "let's", "love this", multi-!
    tired      — "tired", "exhausted", "going to sleep"
    curious    — "?" patterns, "why", "what if", "tell me more"
    calm       — neutral / default
    stuck      — "I'm stuck", "no idea", "tried everything"

Priority order is encoded in rule order (FIRST_MATCH policy):
stuck > frustrated > excited > tired > curious. Calm is the implicit
fallback when nothing else fires.
"""
from __future__ import annotations

import re

from plugin_sdk.classifier import AggregationPolicy, RegexClassifier, Rule

#: The supported vibe vocabulary. Caller must use one of these.
VALID_VIBES: tuple[str, ...] = (
    "frustrated",
    "excited",
    "tired",
    "curious",
    "calm",
    "stuck",
)


# ── Heuristic patterns ────────────────────────────────────────────────


_FRUSTRATED_RE = re.compile(
    r"\b("
    r"doesn'?t\s+work|won'?t\s+work|broken|wtf|fuck|shit|damn|"
    r"why\s+isn'?t|why\s+won'?t|"
    r"keep\s+(getting|seeing|hitting)|still\s+(failing|broken|wrong)|"
    r"can'?t\s+(get|figure|make|find)|nothing\s+works|"
    r"frustrating|annoying"
    r")\b",
    re.IGNORECASE,
)

_EXCITED_RE = re.compile(
    r"("
    r"\b(amazing|awesome|fantastic|incredible|love\s+(this|it)|"
    r"let'?s\s+(go|do|build|ship)|finally|works!|that'?s\s+great|"
    r"this\s+is\s+(great|awesome)|so\s+(cool|good))\b"
    r"|!{2,}"
    r")",
    re.IGNORECASE,
)

_TIRED_RE = re.compile(
    r"\b("
    r"tired|exhausted|sleepy|burnt\s+out|burned\s+out|wiped|"
    r"long\s+day|tomorrow|going\s+to\s+sleep|gonna\s+sleep"
    r")\b",
    re.IGNORECASE,
)

_CURIOUS_RE = re.compile(
    r"\b("
    r"why\s+do(es)?|how\s+come|what\s+if|interesting|tell\s+me\s+more|"
    r"does\s+(this|it)\s+work|how\s+does|wondering|curious"
    r")\b",
    re.IGNORECASE,
)

_STUCK_RE = re.compile(
    r"\b("
    r"i'?m\s+stuck|completely\s+stuck|no\s+idea|don'?t\s+know\s+(what|how|where)|"
    r"i'?ve\s+tried\s+everything|been\s+at\s+this|hours\s+(now|on\s+this)"
    r")\b",
    re.IGNORECASE,
)


# ── Classifier — order encodes priority ──────────────────────────────


_VIBE_CLASSIFIER: RegexClassifier[str] = RegexClassifier(
    rules=[
        Rule(pattern=_STUCK_RE, label="stuck"),
        Rule(pattern=_FRUSTRATED_RE, label="frustrated"),
        Rule(pattern=_EXCITED_RE, label="excited"),
        Rule(pattern=_TIRED_RE, label="tired"),
        Rule(pattern=_CURIOUS_RE, label="curious"),
    ],
    policy=AggregationPolicy.FIRST_MATCH,
)


def classify_vibe(messages: list[str]) -> str:
    """Return one of :data:`VALID_VIBES` based on the most recent messages.

    Examines up to the last 3 user messages, joins them, and runs the
    heuristic classifier. Falls back to ``calm`` when no pattern fires.
    Empty input returns ``calm``.
    """
    if not messages:
        return "calm"
    blob = "\n".join(m for m in messages[-3:] if isinstance(m, str))
    if not blob.strip():
        return "calm"
    verdict = _VIBE_CLASSIFIER.classify(blob)
    return verdict.top_label or "calm"
```

- [ ] **Step 4: Run vibe tests — must still pass unchanged**

Run: `pytest tests/test_vibe_classifier.py -v`

Expected: All ~14 tests PASS. (Same patterns, same priority, same outputs — refactor is invisible to tests.)

- [ ] **Step 5: Run companion-related tests for safety**

Run: `pytest tests/test_companion_persona.py tests/test_companion_life_event_hook.py tests/test_companion_anti_robot_cosplay.py -q`

Expected: All PASS. (vibe is consumed by the companion overlay; verify that pipeline still works.)

- [ ] **Step 6: Commit**

```bash
git add opencomputer/agent/vibe_classifier.py
git commit -m "refactor(vibe): migrate vibe_classifier to plugin_sdk.RegexClassifier

Same patterns, same FIRST_MATCH priority order, same outputs. The
per-pattern table now lives in a shared abstraction so the embedding/
LLM upgrade lands by swapping the back-end, not rewriting the
classifier.

14 vibe tests + companion tests pass unchanged — public API
(classify_vibe + VALID_VIBES) preserved verbatim."
```

---

### Task 5: Migrate job_change life-event detector to RegexClassifier

**Files:**
- Modify: `opencomputer/awareness/life_events/job_change.py`
- Test: existing tests (no changes)

- [ ] **Step 1: Read current job_change.py**

Run: `cat opencomputer/awareness/life_events/job_change.py`

Identify: the trigger-term frozenset (`linkedin.com/jobs`, `indeed.com`, `glassdoor.com`, `resignation`, `severance`, `unemployment`, `notice period` — 7 items), the `consider_event` method that checks if any term is in the URL/text, the `EvidenceItem` it returns.

- [ ] **Step 2: Verify existing tests pass before refactor**

Run: `pytest tests/test_life_events.py -q` (or whichever file covers `JobChange` — try `tests/` and grep)

Run: `grep -rln "JobChange\|job_change" tests/ | head -5`

Expected: identifies the test file. Confirm baseline tests pass.

- [ ] **Step 3: Replace the trigger-term matching with a RegexClassifier**

Replace the body of `opencomputer/awareness/life_events/job_change.py` with:

```python
"""JobChange life-event detector.

Watches for browser visits to job-search domains + textual signals
(resignation, severance, etc.). When confidence crosses the threshold
the pattern surfaces a hint to the chat surfacer.

2026-04-28 refactor: trigger-term matching uses
:class:`plugin_sdk.classifier.RegexClassifier` (ALL_MATCHES policy)
so the term table is shared with the abstraction; behavior unchanged.
"""
from __future__ import annotations

import re

from plugin_sdk.classifier import AggregationPolicy, RegexClassifier, Rule

from opencomputer.awareness.life_events.pattern import (
    EvidenceItem,
    LifeEventPattern,
)


# Trigger terms — domain hits on job sites + textual resignation cues.
# Patterns are matched against the lowercased URL or text payload.
# All matched terms accumulate evidence; ALL_MATCHES surfaces every
# distinct hit so the agent can cite the specific signal.
_JOB_TRIGGER_RULES: tuple[Rule[str], ...] = (
    Rule(pattern=re.compile(r"linkedin\.com/jobs", re.IGNORECASE), label="linkedin_jobs"),
    Rule(pattern=re.compile(r"indeed\.com", re.IGNORECASE), label="indeed"),
    Rule(pattern=re.compile(r"glassdoor\.com", re.IGNORECASE), label="glassdoor"),
    Rule(pattern=re.compile(r"\bresignation\b", re.IGNORECASE), label="resignation"),
    Rule(pattern=re.compile(r"\bseverance\b", re.IGNORECASE), label="severance"),
    Rule(pattern=re.compile(r"\bunemployment\b", re.IGNORECASE), label="unemployment"),
    Rule(pattern=re.compile(r"\bnotice\s+period\b", re.IGNORECASE), label="notice_period"),
)


_JOB_CLASSIFIER: RegexClassifier[str] = RegexClassifier(
    rules=_JOB_TRIGGER_RULES,
    policy=AggregationPolicy.ALL_MATCHES,
)


class JobChange(LifeEventPattern):
    """Detects user activity around job changes.

    Triggers on browser_visit events whose URL or accompanying text
    matches any of the curated job-search trigger terms.
    """

    pattern_id: str = "job_change"
    surface_threshold: float = 0.7
    surfacing: str = "hint"
    window_days: float = 30.0
    decay_half_life_days: float = 14.0

    def consider_event(
        self, event_type: str, metadata: dict[str, object]
    ) -> EvidenceItem | None:
        if event_type != "browser_visit":
            return None
        # Concatenate URL + text into one blob for the classifier;
        # both fields can carry a trigger.
        url = str(metadata.get("url", ""))
        text = str(metadata.get("text", ""))
        blob = f"{url}\n{text}"
        verdict = _JOB_CLASSIFIER.classify(blob)
        if not verdict.has_match:
            return None
        # Each matched label contributes 0.4 — two distinct signals
        # (e.g. linkedin_jobs + resignation) cross threshold quickly.
        weight = min(1.0, 0.4 * len(verdict.matched_labels))
        return EvidenceItem(
            timestamp=float(metadata.get("timestamp", 0.0)) or 0.0,
            weight=weight,
            source="job_change",
            payload={
                "url": url,
                "matched_labels": verdict.matched_labels,
            },
        )

    def hint_text(self) -> str:
        return (
            "I noticed activity around job-change signals (job-board "
            "visits, resignation/severance/notice-period mentions). "
            "Want to talk through it?"
        )
```

- [ ] **Step 4: Run job_change tests**

Run: `grep -rln "job_change\|JobChange" tests/ | head -3`

Then run whatever test files come back: `pytest <test_file> -v`

Expected: All PASS. (If there are no specific JobChange tests, run the broader `tests/test_life_events.py` if it exists, else just the companion-life-event hook test which exercises the detector via the registry.)

- [ ] **Step 5: Run companion-life-event-hook test**

Run: `pytest tests/test_companion_life_event_hook.py -v`

Expected: All PASS — the registry-level integration still works.

- [ ] **Step 6: Commit**

```bash
git add opencomputer/awareness/life_events/job_change.py
git commit -m "refactor(life_events): migrate JobChange to plugin_sdk.RegexClassifier

Same trigger-term set (7 patterns), same surface threshold + decay,
same EvidenceItem output. Now uses ALL_MATCHES policy via the shared
RegexClassifier so when the ML/embedding back-end lands it can swap
the matcher without touching JobChange itself."
```

---

### Task 6: Full suite + lint + push + PR

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/ -q`

Expected: 4029+ passing, 0 regressions. (vibe + companion + life-events + classifier tests all green.)

- [ ] **Step 2: Lint everything touched**

Run: `ruff check plugin_sdk/classifier.py plugin_sdk/__init__.py opencomputer/agent/vibe_classifier.py opencomputer/awareness/life_events/job_change.py tests/test_plugin_sdk_classifier.py`

Expected: `All checks passed!`

- [ ] **Step 3: Update CHANGELOG**

Append to `CHANGELOG.md` under `## [Unreleased]`:

```markdown
### Added (plugin_sdk — RegexClassifier abstraction)

Codebase audit found 7+ files implementing the same shape: `_PATTERNS = [...]`
+ iterate + match → label. Each one re-rolled its own pattern table,
weight conventions, result dataclass, and test harness. This change
ships the shared abstraction:

- `plugin_sdk.Classifier[L]` — `Protocol[L]` with `classify(text) -> ClassifierVerdict[L]`. Future `EmbeddingClassifier` / `LLMClassifier` plug in via the same protocol.
- `plugin_sdk.RegexClassifier[L]` — regex back-end with three aggregation policies: `FIRST_MATCH` (priority order via rule order), `ALL_MATCHES` (fan-out detectors), `WEIGHTED_SUM` (instruction_detector style).
- `plugin_sdk.Rule[L]` — frozen dataclass: `(pattern, label, weight, severity, description)`.
- `plugin_sdk.ClassifierVerdict[L]` — frozen dataclass: `matched_labels`, `weights_by_label`, `triggered_rules`, `has_match`, `top_label`.

### Changed (vibe + JobChange life-event use the abstraction)

- `opencomputer/agent/vibe_classifier.py` — `classify_vibe()` and `VALID_VIBES` API unchanged. Implementation now uses `RegexClassifier` with `FIRST_MATCH` policy; rule order encodes priority (stuck > frustrated > excited > tired > curious > calm).
- `opencomputer/awareness/life_events/job_change.py` — `JobChange.consider_event` uses `RegexClassifier` with `ALL_MATCHES` to surface every matched trigger label. Same patterns + thresholds.

### Out of scope (deferred)

- Migrating the security classifiers (threat scanners, bash safety, python denylist, sensitive-app filter, instruction detector) — separate PRs since they're security-critical and need targeted review.
- Migrating the persona auto-classifier — its compound logic (foreground app + state-query + file extensions + time of day) doesn't cleanly fit `RegexClassifier`. Stays as-is.
- Embedding back-end — separate PR. Will be the first non-regex `Classifier` implementation, likely targeting vibe + life-event detectors first.

### Tests

17 new tests in `tests/test_plugin_sdk_classifier.py` covering each policy + protocol satisfaction + generics + empty input. All 14 vibe tests + companion tests + life-event tests pass unchanged (refactor is invisible to consumers).
```

- [ ] **Step 4: Commit CHANGELOG**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): RegexClassifier abstraction + vibe/JobChange migration"
```

- [ ] **Step 5: Push branch**

```bash
git push -u origin feat/regex-classifier-abstraction
```

- [ ] **Step 6: Open PR**

Run:

```bash
gh pr create --base main --head feat/regex-classifier-abstraction \
  --title "feat(plugin_sdk): RegexClassifier abstraction + vibe/JobChange migration" \
  --body "$(cat <<'EOF'
## Summary

Codebase audit found 7+ classifiers reinventing the same shape (\`_PATTERNS = [...]\` + iterate + match → label). This ships the shared abstraction in \`plugin_sdk\` so future embedding/LLM back-ends are one protocol implementation away.

## What ships

- \`Classifier[L]\` Protocol — pluggable back-end interface
- \`RegexClassifier[L]\` — regex back-end, 3 aggregation policies (FIRST_MATCH / ALL_MATCHES / WEIGHTED_SUM)
- \`Rule[L]\`, \`ClassifierVerdict[L]\` — frozen dataclasses
- Vibe classifier migrated (FIRST_MATCH; preserved priority order)
- JobChange life-event detector migrated (ALL_MATCHES)

## What does NOT change

- Public APIs of \`classify_vibe()\` / \`VALID_VIBES\` / \`JobChange.consider_event\` — verbatim
- Vibe + life-event tests pass unchanged
- Security classifiers (threat/bash/python/sensitive-app/instruction) NOT migrated — separate PR

## Test plan

- [x] 17 new tests in \`tests/test_plugin_sdk_classifier.py\`
- [x] All 14 vibe tests + companion + life-event tests pass unchanged
- [x] Full suite ≥4029 passing, 0 regressions
- [x] \`ruff check\` clean
- [x] \`plugin_sdk\` boundary test still passes (no \`opencomputer\` import in classifier.py)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 7: Merge after CI green**

Run: `gh pr merge <PR#> --squash --delete-branch`

---

## Self-Review Checklist

1. **Spec coverage:**
   - ✅ `Classifier` protocol (Task 1)
   - ✅ `RegexClassifier` with 3 policies (Tasks 1, 2)
   - ✅ `Rule[L]` + `ClassifierVerdict[L]` dataclasses (Task 1)
   - ✅ Exported from `plugin_sdk` (Task 3)
   - ✅ vibe migration (Task 4)
   - ✅ JobChange migration (Task 5)
   - ✅ Full-suite + lint + PR (Task 6)
   - ✅ Out-of-scope items explicitly listed in CHANGELOG (Task 6)

2. **Placeholder scan:** No TBD/TODO/etc. Each step contains complete code.

3. **Type consistency:**
   - `Rule[L]` everywhere — never `Rule` bare
   - `ClassifierVerdict[L]` consistent
   - `AggregationPolicy.FIRST_MATCH` / `ALL_MATCHES` / `WEIGHTED_SUM` — same names across all tasks
   - `classify(text: str) -> ClassifierVerdict[L]` signature consistent

4. **Failure modes covered:**
   - Empty input returns empty verdict (test in Task 2)
   - No-match returns `has_match=False`, `top_label=None`
   - Frozen dataclasses raise on mutation
   - WEIGHTED_SUM no-matches returns empty dict (not raises)

5. **Migration safety:**
   - Both Task 4 + Task 5 explicitly run pre-existing tests UNCHANGED. If any fail, the refactor broke behavior — abort and re-implement.
   - Both migrations preserve module-level public symbol names (`classify_vibe`, `VALID_VIBES`, `JobChange.consider_event`) so consumers don't change.
