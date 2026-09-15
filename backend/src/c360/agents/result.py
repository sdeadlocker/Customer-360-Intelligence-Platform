"""The value types an agent node produces (task 8.6, 8.7, design §8.2).

An agent does not return free text. It returns an :class:`AgentResult`: a schema-validated Pydantic
``outputs`` model, the citations that back it (facts and passages kept apart), the inputs it could
not obtain, a confidence, and the labelling metadata the UI and audit need — ``generated_at``,
``model_id``, ``prompt_version``, ``cache_hit`` and ``degraded``. That last flag is how an open
circuit breaker or a failed claim-validation surfaces to the user: a degraded card is honest about
being a deterministic fallback rather than a model narrative (design §8.4).

Citations come in two clearly-distinguished kinds because the UI renders them differently and the
claim validator treats them differently: a :class:`FactCitation` opens the owning widget and
highlights a field; a :class:`PassageCitation` opens the passage viewer. A figure may resolve to a
fact; guidance may resolve to a passage; a figure may never resolve to a passage (design §9.1).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class FactCitation(BaseModel):
    """A citation to a fact in the agent's fact table (design §8.4)."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["fact"] = "fact"
    fact_id: str = Field(pattern=r"^F\d+$")
    entity_type: str
    entity_id: str
    field: str


class PassageCitation(BaseModel):
    """A citation to a retrieved knowledge passage (design §8.4, requirement 17.6)."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["passage"] = "passage"
    passage_id: str = Field(pattern=r"^P\d+$")
    doc_id: str
    section_path: str
    title: str
    version: str
    effective_from: str
    effective_to: str | None = None


Citation = FactCitation | PassageCitation


class AgentError(BaseModel):
    """A recorded node failure. Never raised past the node — it lands in ``state.errors``.

    The message is value-free (an agent name and an error class), so it is safe to serialize to the
    client and the logs (requirement 18.8). It exists so a wave never blocks on a straggler and a
    dependent agent can see which inputs were unavailable rather than crashing the whole run.
    """

    model_config = ConfigDict(frozen=True)

    agent: str
    code: str
    message: str


class AgentResult(BaseModel):
    """One agent's validated, cited, labelled output (design §8.2)."""

    model_config = ConfigDict(frozen=True)

    agent: str
    outputs: BaseModel
    fact_citations: tuple[FactCitation, ...] = ()
    passage_citations: tuple[PassageCitation, ...] = ()
    unavailable_inputs: tuple[str, ...] = ()
    confidence: float | None = None
    generated_at: datetime
    model_id: str
    prompt_version: str
    cache_hit: bool = False
    degraded: bool = False


__all__ = [
    "AgentError",
    "AgentResult",
    "Citation",
    "FactCitation",
    "PassageCitation",
]
