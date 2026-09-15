"""Stream a Q&A answer over SSE (task 9.5, design §6.3/§10.1).

The ``/customers/{id}/ask`` endpoint runs the ReAct graph and streams the result to the client. The
framing mirrors the dashboard's SSE path (:mod:`c360.agents.streaming`): hand-rolled ``event:`` /
``data:`` frames terminated by a blank line, so the route stays a thin adapter over this generator.

What is streamed, and in what order
-----------------------------------

1. one ``token`` event per chunk of the answer text as it is produced, so a long answer renders
   progressively rather than appearing all at once (the "streaming tokens" the 5 s budget assumes);
2. a single ``citations`` event carrying the fact citations and knowledge citations *separately*
   (design §9.1) plus any graph ``traversal_paths`` used (task 9.4);
3. a terminal ``done`` event carrying the behavioural flags — ``refused``, ``no_guidance``,
   ``degraded`` — and the labelling metadata the UI needs.

A refusal (non-entitled data) streams the refusal text as tokens and sets ``refused`` on ``done``,
disclosing nothing further (requirement 11.6); the audit of the denied access is written by the
route before streaming, exactly as the dashboard path audits the AI access up front.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

#: How many characters per streamed ``token`` frame — a token-ish chunk, not a real tokenizer, which
#: is all the SSE framing needs. Small enough that a short answer still streams in several frames.
_CHUNK_CHARS = 24


async def stream_qa(state: dict[str, Any], *, model_id: str = "") -> AsyncIterator[str]:
    """Stream an already-computed Q&A ``state`` as answer tokens, citations and terminal flags.

    The graph is run by the route (so it can audit the outcome before streaming); this generator
    only frames the result. Keeping the run out of the generator means a refusal is audited as a
    denied access, not merely rendered.
    """
    answer = str(state.get("answer", ""))
    for chunk in _chunks(answer):
        yield _frame("token", {"text": chunk})

    yield _frame(
        "citations",
        {
            "fact_citations": state.get("fact_citations", []),
            "passage_citations": state.get("passage_citations", []),
            "traversal_paths": state.get("traversal_paths", []),
        },
    )
    yield _frame(
        "done",
        {
            "refused": bool(state.get("refused", False)),
            "no_guidance": bool(state.get("no_guidance", False)),
            "degraded": bool(state.get("degraded", False)),
            "route": state.get("route", ""),
            "model_id": model_id,
        },
    )


def _chunks(text: str) -> list[str]:
    """Split ``text`` into progressive chunks; an empty answer yields nothing to stream."""
    return [text[i : i + _CHUNK_CHARS] for i in range(0, len(text), _CHUNK_CHARS)]


def _frame(event: str, data: dict[str, Any]) -> str:
    """One SSE frame: an ``event:`` line, a ``data:`` JSON line, and the terminating blank line."""
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


__all__ = ["stream_qa"]
