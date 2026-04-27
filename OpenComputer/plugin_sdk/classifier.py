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
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, TypeVar, runtime_checkable

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
class Rule[L]:
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
class ClassifierVerdict[L]:
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
class Classifier[L](Protocol):
    """Pluggable text → labels classifier.

    Implementations: :class:`RegexClassifier` (this module), future
    ``EmbeddingClassifier`` and ``LLMClassifier`` against the same
    protocol. Callers depend only on ``classify`` returning a verdict.
    """

    def classify(self, text: str) -> ClassifierVerdict[L]:
        ...


class RegexClassifier[L]:
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
