"""Grounding helpers for the Q&A loop: fact merging and citation building (tasks 9.3, 9.4).

A Q&A answer draws on several tool calls, each returning its own ``F1..Fn`` fact table and, for
``knowledge_search``, its own ``P1..Pn`` passages. Two things have to be true before the claim
validator runs and before citations reach the client:

* the facts from every tool call must live in **one** table with contiguous, unique ``F``-ids, so a
  narrative's ``[F7]`` resolves to exactly one fact (the dashboard's ``load_context`` does the same
  renumber for the same reason);
* passages must be numbered once across the whole answer, so ``[P2]`` is stable no matter which
  retrieval turn produced it.

This module owns that accounting so the graph node stays a thin orchestrator. It returns the merged
fact table (for the validator and the ``FactCitation`` list), the numbered passages (for the model
prompt and the ``PassageCitation`` list), and the two citation lists themselves — kept apart because
a figure may cite a fact, guidance may cite a passage, and a figure may never cite a passage
(design §9.1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from c360.agents.result import FactCitation, PassageCitation
from c360.tools.facts import FactTable

if TYPE_CHECKING:
    from collections.abc import Sequence


class QaAccumulator:
    """Accumulates facts and passages across a Q&A run, minting stable ids across tool calls.

    One instance per question. Each tool result is added; the accumulator renumbers facts into a
    single ``F``-space and passages into a single ``P``-space as they arrive, so the ids a later
    turn's prompt shows the model match the ids the citations and the validator use.
    """

    __slots__ = ("_fact_rows", "_passages", "_traversal_paths")

    def __init__(self) -> None:
        self._fact_rows: list[dict[str, Any]] = []
        self._passages: list[dict[str, Any]] = []
        self._traversal_paths: list[list[str]] = []

    def add_facts(self, facts: FactTable) -> dict[str, str]:
        """Add a tool call's facts; return a ``{old_fact_id: new_fact_id}`` remap for this call.

        The remap lets the caller rewrite the ids in the *encoded* tool message so the model sees
        the same ``F``-ids the accumulator will validate against.
        """
        remap: dict[str, str] = {}
        for fact in facts.facts:
            new_id = f"F{len(self._fact_rows) + 1}"
            remap[fact.fact_id] = new_id
            self._fact_rows.append(
                {
                    "fact_id": new_id,
                    "entity_type": fact.entity_type,
                    "entity_id": fact.entity_id,
                    "field": fact.field,
                    "value": fact.value,
                    "as_of": fact.as_of,
                    "source_system": fact.source_system,
                }
            )
        return remap

    def add_passages(self, passages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Add a retrieval turn's passages; return them numbered into the run's ``P``-space.

        Each returned dict carries a ``passage_id`` (``P1``, ``P2``, ...) unique across the run, so
        the encoder shows the model the same id the ``PassageCitation`` will carry.
        """
        numbered: list[dict[str, Any]] = []
        for passage in passages:
            new_id = f"P{len(self._passages) + 1}"
            row = {**passage, "passage_id": new_id}
            self._passages.append(row)
            numbered.append(row)
        return numbered

    def add_traversal_path(self, path: Sequence[str]) -> None:
        """Record a graph traversal path a ``graph_path``/``graph_neighborhood`` call produced."""
        if path:
            self._traversal_paths.append(list(path))

    def merged_facts(self) -> FactTable:
        """The single, contiguous fact table for the claim validator and fact citations."""
        builder = FactTable.builder()
        for row in self._fact_rows:
            builder.add(
                entity_type=row["entity_type"],
                entity_id=row["entity_id"],
                field=row["field"],
                value=row["value"],
                as_of=row["as_of"],
                source_system=row["source_system"],
            )
        return builder.build()

    def fact_citations(self) -> tuple[FactCitation, ...]:
        """A citation per accumulated fact, resolvable by ``fact_id`` (design §9.1)."""
        return tuple(
            FactCitation(
                fact_id=row["fact_id"],
                entity_type=row["entity_type"],
                entity_id=row["entity_id"],
                field=row["field"],
            )
            for row in self._fact_rows
        )

    def passage_citations(self) -> tuple[PassageCitation, ...]:
        """A citation per accumulated passage, resolvable by document/section/version (17.6)."""
        return tuple(
            PassageCitation(
                passage_id=row["passage_id"],
                doc_id=row.get("doc_id", ""),
                section_path=row.get("section_path", ""),
                title=row.get("title", ""),
                version=row.get("version", ""),
                effective_from=row.get("effective_from", ""),
                effective_to=row.get("effective_to"),
            )
            for row in self._passages
        )

    def traversal_paths(self) -> tuple[tuple[str, ...], ...]:
        """Every graph traversal path used, for a graph question's answer (design §10.1)."""
        return tuple(tuple(path) for path in self._traversal_paths)

    @property
    def has_passages(self) -> bool:
        return bool(self._passages)


__all__ = ["QaAccumulator"]
