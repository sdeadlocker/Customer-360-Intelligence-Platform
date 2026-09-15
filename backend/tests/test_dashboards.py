"""Validate the provisioned Grafana dashboards (task 10.7, design §13.6).

Like the alert-rules test, this is structural and cross-referential: every dashboard must be valid
JSON with a unique UID and the expected Prometheus datasource, and every metric a panel queries
must be one the application actually emits. The four dashboards design §13.6 names must all exist.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.test_alert_rules import _NORMALIZED_INSTRUMENTS, _SUFFIXES

_DASHBOARD_DIR = Path(__file__).resolve().parents[2] / "docker" / "grafana" / "dashboards"
_PROM_DATASOURCE_UID = "c360-prometheus"

_METRIC_RE = re.compile(r"\b(c360_[a-zA-Z0-9_]+|gen_ai_[a-zA-Z0-9_]+)\b")

#: Identifiers sharing the c360_ prefix that are label *names*, not metric series — the Prometheus
#: form of the retrieval `stage` dimension. Excluded so a `by (le, c360_retrieval_stage)` grouping
#: clause is not mistaken for a metric reference.
_LABEL_NAMES = {"c360_retrieval_stage"}


def _dashboard_files() -> list[Path]:
    return sorted(_DASHBOARD_DIR.glob("*.json"))


def _load(path: Path) -> dict:
    data: dict = json.loads(path.read_text(encoding="utf-8"))
    return data


def test_all_four_dashboards_exist() -> None:
    files = _dashboard_files()
    assert len(files) == 4, f"expected the four §13.6 dashboards, found {[f.name for f in files]}"
    titles = {_load(f)["title"] for f in files}
    assert titles == {
        "C360 — Platform health",
        "C360 — Agent performance and cost",
        "C360 — Retrieval quality",
        "C360 — Data and security",
    }


def test_dashboard_uids_are_unique() -> None:
    uids = [_load(f)["uid"] for f in _dashboard_files()]
    assert len(uids) == len(set(uids)), f"duplicate dashboard UID: {uids}"


@pytest.mark.parametrize("path", _dashboard_files(), ids=lambda p: p.name)
def test_dashboard_is_well_formed(path: Path) -> None:
    doc = _load(path)
    assert doc["uid"].startswith("c360-")
    assert doc["title"].startswith("C360 —")
    assert doc["panels"], "dashboard has no panels"
    for panel in doc["panels"]:
        # Every panel points at the provisioned Prometheus datasource by UID.
        assert panel["datasource"]["uid"] == _PROM_DATASOURCE_UID, panel.get("title")
        for target in panel.get("targets", []):
            assert target["expr"].strip(), f"empty expr in {panel.get('title')}"


def _matches_emitted(series: str) -> bool:
    candidate = series
    for suffix in sorted(_SUFFIXES, key=len, reverse=True):
        if candidate.endswith(suffix):
            candidate = candidate[: -len(suffix)]
            break
    return any(candidate == inst or candidate.startswith(inst) for inst in _NORMALIZED_INSTRUMENTS)


@pytest.mark.parametrize("path", _dashboard_files(), ids=lambda p: p.name)
def test_every_panel_metric_is_emitted(path: Path) -> None:
    doc = _load(path)
    unknown: set[str] = set()
    for panel in doc["panels"]:
        for target in panel.get("targets", []):
            for series in _METRIC_RE.findall(target["expr"]):
                if series in _LABEL_NAMES:
                    continue
                if not _matches_emitted(series):
                    unknown.add(series)
    assert not unknown, f"{path.name} queries metrics the app does not emit: {sorted(unknown)}"
