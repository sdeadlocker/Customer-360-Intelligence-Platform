"""Retrieval metrics (task 7.9, requirement 18.5, design §13.3).

The design's metric inventory for retrieval is: latency by stage, candidate counts, a zero-result
counter and a rerank-used counter. This module owns those instruments, created lazily on first use
so a process with telemetry disabled pays nothing (the same pattern the audit subsystem uses).

Only low-cardinality labels are attached — ``stage`` and ``reranked`` — never the query, a document
id or any customer reference (design §13.4). A query string on a metric would be both a cardinality
explosion and a privacy incident, so it never appears here; the query is not customer data but the
label allowlist is a bright line and retrieval stays on the safe side of it.
"""

from __future__ import annotations

from typing import Final

from c360.core.telemetry import get_meter

_METER_NAME: Final = "c360.knowledge.retrieval"


class RetrievalMetrics:
    """Lazily-created retrieval instruments. One instance is built per retriever."""

    __slots__ = ("_candidates", "_latency", "_rerank_used", "_results", "_zero_result")

    def __init__(self) -> None:
        meter = get_meter(_METER_NAME)
        self._latency = meter.create_histogram(
            "c360.retrieval.stage.duration",
            unit="ms",
            description="Retrieval stage latency in milliseconds, labelled by stage.",
        )
        self._candidates = meter.create_histogram(
            "c360.retrieval.candidate_count",
            description="Distinct candidate chunks surfaced by the two retrievers before fusion.",
        )
        self._results = meter.create_histogram(
            "c360.retrieval.result_count",
            description="Passages returned after the min-score floor and context budget.",
        )
        self._zero_result = meter.create_counter(
            "c360.retrieval.zero_result",
            description="Retrievals that returned no passage (the 'no guidance found' signal).",
        )
        self._rerank_used = meter.create_counter(
            "c360.retrieval.rerank_used",
            description="Retrievals in which Bedrock reranking actually ran.",
        )

    def record_stage(self, stage: str, duration_ms: float) -> None:
        self._latency.record(duration_ms, {"c360.retrieval.stage": stage})

    def record_outcome(self, *, candidate_count: int, result_count: int, reranked: bool) -> None:
        self._candidates.record(candidate_count)
        self._results.record(result_count)
        if result_count == 0:
            self._zero_result.add(1)
        if reranked:
            self._rerank_used.add(1)


__all__ = ["RetrievalMetrics"]
