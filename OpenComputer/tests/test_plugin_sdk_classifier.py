"""Tests for plugin_sdk.classifier — protocol, Rule, Verdict, RegexClassifier."""
from __future__ import annotations

import dataclasses
import re

import pytest

from plugin_sdk.classifier import (
    AggregationPolicy,
    Classifier,
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
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.label = "y"  # type: ignore[misc]


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
    with pytest.raises(dataclasses.FrozenInstanceError):
        v.matched_labels = ["x"]  # type: ignore[misc]


# ── AggregationPolicy enum ───────────────────────────────────────────


def test_aggregation_policy_values():
    assert AggregationPolicy.FIRST_MATCH.value == "first_match"
    assert AggregationPolicy.ALL_MATCHES.value == "all_matches"
    assert AggregationPolicy.WEIGHTED_SUM.value == "weighted_sum"


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
    rules = [Rule(pattern=re.compile(r"\bxyzzy\b"), label="X", weight=0.5)]
    c: RegexClassifier[str] = RegexClassifier(rules, AggregationPolicy.WEIGHTED_SUM)
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
    rules = [Rule(pattern=re.compile(r"x"), label="x")]
    c: RegexClassifier[str] = RegexClassifier(rules)
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
