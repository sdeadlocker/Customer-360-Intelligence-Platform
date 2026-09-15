"""Validate the provisioned Prometheus alert rules (tasks 10.4, 10.6).

These are structural and cross-referential checks, not a live Prometheus evaluation: every rule
file must parse, every rule must be well-formed, and — the part that catches real drift — every
metric an alert references must be one the application actually emits. A rename in ``metrics.py``
that silently orphans an alert is exactly the failure this test exists to catch.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_ALERTS_DIR = Path(__file__).resolve().parents[2] / "docker" / "alerts"

# Base OTel instrument names the app emits, from metrics.py / audit.py / knowledge/metrics.py. The
# Prometheus exporter normalizes these (dots -> underscores) and adds a `_total` suffix to counters
# and `_bucket`/`_count`/`_sum` to histograms; the check below strips those suffixes before
# comparing, so this set is the canonical instrument inventory the alerts may reference.
_EMITTED_INSTRUMENTS = {
    # HTTP RED
    "c360.http.server.request.duration",
    "c360.http.server.requests",
    "c360.http.server.errors",
    "c360.http.server.budget_breach",
    # agents / model
    "c360.agent.duration",
    "c360.agent.outcome",
    "c360.agent.claim_rejections",
    "c360.agent.citations",
    "gen_ai.client.operation.duration",
    "gen_ai.client.token.usage",
    "c360.model.throttled",
    "c360.model.estimated_cost.micro_usd",
    # Q&A
    "c360.qa.tool_calls",
    "c360.qa.refusals",
    "c360.qa.clarifications",
    # database / graph
    "c360.db.pool.checkout_wait",
    "c360.db.query.duration",
    "c360.db.sqlite_busy",
    "c360.graph.hops",
    "c360.graph.nodes_visited",
    "c360.graph.truncated",
    # security
    "c360.security.denied_access",
    "c360.security.masking_applied",
    "c360.security.breaker_transition",
    # audit (audit.py)
    "c360.audit.records_written",
    "c360.audit.fail_closed",
    "c360.audit.queue_depth",
    # retrieval (knowledge/metrics.py)
    "c360.retrieval.stage.duration",
    "c360.retrieval.candidate_count",
    "c360.retrieval.result_count",
    "c360.retrieval.zero_result",
    "c360.retrieval.rerank_used",
}

#: The emitted instruments expressed in the Prometheus-normalized form (dots -> underscores). A
#: referenced series matches if it starts with one of these after suffix-stripping.
_NORMALIZED_INSTRUMENTS = {name.replace(".", "_") for name in _EMITTED_INSTRUMENTS}

#: Series suffixes the Prometheus exporter appends; stripped before matching a referenced series.
_SUFFIXES = ("_total", "_bucket", "_count", "_sum", "_milliseconds_bucket", "_milliseconds")

#: A metric reference inside a PromQL expression: an identifier optionally followed by a label set.
_SERIES_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b")

#: PromQL functions/keywords that look like identifiers but are not metric series.
_PROMQL_KEYWORDS = {
    "sum",
    "rate",
    "increase",
    "histogram_quantile",
    "by",
    "clamp_min",
    "and",
    "or",
    "le",
    "avg",
    "max",
    "min",
    "count",
    "without",
    "on",
    "group_left",
    "group_right",
}


def _rule_files() -> list[Path]:
    return sorted(_ALERTS_DIR.glob("*.yml"))


def test_alert_directory_exists_with_files() -> None:
    files = _rule_files()
    assert files, "no alert rule files provisioned"
    assert {f.name for f in files} >= {"security.yml", "slo.yml"}


@pytest.mark.parametrize("path", _rule_files(), ids=lambda p: p.name)
def test_rule_file_is_well_formed(path: Path) -> None:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert doc.get("groups"), f"{path.name} has no groups"
    for group in doc["groups"]:
        assert "name" in group
        assert group.get("rules")
        for rule in group["rules"]:
            # Every rule is either an alert or a recording rule, and carries a non-empty expr.
            assert ("alert" in rule) ^ ("record" in rule), rule
            assert isinstance(rule["expr"], str)
            assert rule["expr"].strip()
            if "alert" in rule:
                assert rule.get("labels", {}).get("severity") in {"page", "warning"}


def _referenced_series(expr: str) -> set[str]:
    """Extract candidate metric series names from a PromQL expression."""
    names = set()
    for token in _SERIES_RE.findall(expr):
        if token in _PROMQL_KEYWORDS or token.replace(".", "").isdigit():
            continue
        # Skip label-value bareword matches like state="open" — those are RHS of `=`.
        names.add(token)
    return names


def _matches_emitted(series: str) -> bool:
    # Recording-rule outputs use a `c360:...:...` naming convention (colons) — always allowed.
    if ":" in series:
        return True
    candidate = series
    for suffix in sorted(_SUFFIXES, key=len, reverse=True):
        if candidate.endswith(suffix):
            candidate = candidate[: -len(suffix)]
            break
    return any(candidate == inst or candidate.startswith(inst) for inst in _NORMALIZED_INSTRUMENTS)


@pytest.mark.parametrize("path", _rule_files(), ids=lambda p: p.name)
def test_every_referenced_metric_is_emitted(path: Path) -> None:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    # Recording-rule names defined in this file are also legitimate references.
    defined_records = {
        rule["record"] for group in doc["groups"] for rule in group["rules"] if "record" in rule
    }
    unknown: set[str] = set()
    for group in doc["groups"]:
        for rule in group["rules"]:
            for series in _referenced_series(rule["expr"]):
                if series in defined_records:
                    continue
                # Only check series that look like c360/gen_ai metric names; label barewords and
                # numbers are already filtered out.
                if series.startswith(("c360_", "gen_ai_", "c360:")) and not _matches_emitted(
                    series
                ):
                    unknown.add(series)
    assert not unknown, f"{path.name} references metrics the app does not emit: {sorted(unknown)}"
