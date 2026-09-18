"""The tool-result wire format the Q&A loop and the mock provider share (task 9.1, design §10.1).

A tool returns a Pydantic model (a :class:`~c360.tools.customer_tools.ToolResult` or a
:class:`~c360.tools.knowledge_tool.KnowledgeToolResult`). The ReAct loop must hand that back to the
model as the *content* of a ``tool`` message — text a model reads. This module is the one place that
turns a tool result into that text and reads it back, so:

* the model (live or mock) always sees facts numbered by their ``F``-id and passages by their
  ``P``-id, framed so a figure is citeable and a passage is clearly untrusted reference material;
* the claim validator downstream can bind a narrative's ``[F1]`` to the fact the tool actually
  returned, because the same ``F``-ids reach the model and the validator;
* the mock provider can reconstruct the returned facts and passages to render a grounded answer
  without importing the tool result classes.

Facts keep their ``F``-ids exactly as the tool minted them within a single tool call. When several
tool calls each return their own ``F1..Fn``, the loop renumbers them into one contiguous space
(:func:`c360.agents.qa_grounding.merge_fact_tables`) before validation, and this encoder emits
whatever id the fact carries at encode time — so the loop encodes *after* renumbering.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from c360.knowledge.redaction import wrap_untrusted

if TYPE_CHECKING:
    from collections.abc import Sequence

    from c360.agents.qa_types import QaMessage
    from c360.tools.facts import FactTable

#: A machine-readable block the mock decodes and the validator's ids are drawn from, fenced so the
#: live model reads it as data. Human framing precedes it so a live model narrates naturally.
_FACTS_MARKER = "C360_FACTS_JSON:"
_PASSAGES_MARKER = "C360_PASSAGES_JSON:"


def encode_tool_result(
    *,
    facts: FactTable | None = None,
    passages: Sequence[dict[str, Any]] = (),
    data: Any = None,
) -> str:
    """Encode a tool's facts, passages and raw data into a ``tool`` message's content string.

    ``facts`` are emitted both as a readable ``[F1] field = value`` list (for a live model) and as a
    machine block (for the mock and for exactness). ``passages`` are wrapped in the untrusted-
    reference fence so a passage containing instruction-like text is read as data, never obeyed
    (requirement 17.11). ``data`` is any extra structured payload (e.g. a graph traversal path) that
    a model may want but that carries no citeable figure.
    """
    parts: list[str] = []
    fact_rows = _fact_rows(facts)
    if fact_rows:
        readable = "\n".join(
            f"[{row['fact_id']}] {row['field']} = {row['value']}" for row in fact_rows
        )
        parts.append("FACTS (cite figures by [F] id):\n" + readable)
        parts.append(_FACTS_MARKER + json.dumps(fact_rows, separators=(",", ":")))
    if passages:
        rows = [_passage_row(p) for p in passages]
        fenced = wrap_untrusted([f"[{row['passage_id']}] {row['text']}" for row in rows])
        parts.append(fenced)
        parts.append(_PASSAGES_MARKER + json.dumps(rows, separators=(",", ":")))
    if data is not None:
        parts.append("DATA:\n" + json.dumps(data, separators=(",", ":"), default=str))
    if not parts:
        parts.append("(the tool returned no facts, passages or data)")
    return "\n\n".join(parts)


def decode_facts(messages: Sequence[QaMessage]) -> list[dict[str, Any]]:
    """Every fact row carried by any ``tool`` message in the history, in order."""
    return _decode(messages, _FACTS_MARKER)


def decode_passages(messages: Sequence[QaMessage]) -> list[dict[str, Any]]:
    """Every passage row carried by any ``tool`` message in the history, in order."""
    return _decode(messages, _PASSAGES_MARKER)


def decode_tool_data(messages: Sequence[QaMessage], tool_name: str) -> list[Any]:
    """Every ``DATA:`` payload carried by a ``tool`` message from ``tool_name``, in order.

    The mock renderer uses this to read a tool's structured ``data`` (a cohort's member list, a
    pitch's sections) back out of the wire format, the same way :func:`decode_facts` reads the fact
    block — so it can format that data without importing the tool result classes. Only the named
    tool's messages are considered, so a cohort answer never picks up a search result's payload.
    """
    payloads: list[Any] = []
    for message in messages:
        if message.role != "tool" or message.name != tool_name or not message.content:
            continue
        if "DATA:\n" not in message.content:
            continue
        chunk = message.content.split("DATA:\n", 1)[1]
        try:
            payloads.append(json.loads(chunk))
        except json.JSONDecodeError:
            continue
    return payloads


def current_turn(messages: Sequence[QaMessage]) -> list[QaMessage]:
    """The messages belonging to the current question — everything from the last ``user`` on.

    On a checkpointed thread the history spans several questions, each with its own tool calls whose
    encoded ``F``/``P`` ids restart at 1. Scoping fact and passage decoding to the current turn
    keeps a citation's id unambiguous: a ``[F1]`` in this answer resolves to this turn's first fact,
    not a prior turn's.
    """
    last_user = 0
    for index, message in enumerate(messages):
        if message.role == "user":
            last_user = index
    return list(messages[last_user:])


# ---------------------------------------------------------------- helpers


def _fact_rows(facts: FactTable | None) -> list[dict[str, Any]]:
    if facts is None:
        return []
    return [
        {
            "fact_id": fact.fact_id,
            "entity_type": fact.entity_type,
            "entity_id": fact.entity_id,
            "field": fact.field,
            "value": fact.value,
        }
        for fact in facts.facts
    ]


def _passage_row(passage: dict[str, Any]) -> dict[str, Any]:
    return {
        "passage_id": passage["passage_id"],
        "doc_id": passage.get("doc_id", ""),
        "section_path": passage.get("section_path", ""),
        "title": passage.get("title", ""),
        "version": passage.get("version", ""),
        "effective_from": passage.get("effective_from", ""),
        "effective_to": passage.get("effective_to"),
        "text": passage.get("text", ""),
    }


def _decode(messages: Sequence[QaMessage], marker: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for message in messages:
        if message.role != "tool" or not message.content:
            continue
        for line in message.content.splitlines():
            if line.startswith(marker):
                payload = line[len(marker) :]
                try:
                    decoded = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if isinstance(decoded, list):
                    rows.extend(item for item in decoded if isinstance(item, dict))
    return rows


__all__ = [
    "current_turn",
    "decode_facts",
    "decode_passages",
    "decode_tool_data",
    "encode_tool_result",
]
