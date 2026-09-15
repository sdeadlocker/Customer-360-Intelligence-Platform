"""Value types for the Q&A tool-calling seam (task 9.1, design §10.1).

The dashboard path (Phase 8) needs one-shot generation: a :class:`CompletionRequest` in, a narrative
out. The Q&A ReAct loop needs something the dashboard never did — a *conversation* the model drives
by asking to call tools. This module adds the small vocabulary that seam speaks, kept apart from
:mod:`c360.agents.provider` so the one-shot types there stay unencumbered by the loop's message
shapes.

Why a provider-neutral message type rather than LangChain's
-----------------------------------------------------------

The provider port is the single boundary between the graph and any model (design §8.5), and the
whole point of it is that the graph above never imports the model SDK below. LangChain's
``AIMessage``/``ToolMessage`` are exactly such SDK types. So the loop speaks these neutral
:class:`QaMessage` / :class:`QaToolCall` objects, and each concrete provider translates them at its
own edge — the Bedrock provider to and from LangChain messages, the mock provider not at all. A test
can build a conversation without importing ``langchain_core`` and assert on tool calls without
knowing how Bedrock frames them.

The router classification lives here too because it is the first thing the loop produces and the
thing the mock provider keys its deterministic tool choice on.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from c360.agents.provider import TokenUsage


class QaRoute(StrEnum):
    """What a question needs, as the router classifies it (design §10.1).

    A pure policy question is ``KNOWLEDGE`` and never touches a customer tool; a "what is their
    balance" is ``FACTS``; "is this customer eligible for the platinum card" is ``BOTH``. The route
    is advisory — it shapes which tools the agent is nudged toward and lets a knowledge-only
    question skip customer reads — never a hard gate that could deny the model a tool it needs.
    """

    FACTS = "facts"
    KNOWLEDGE = "knowledge"
    BOTH = "both"


class QaToolCall(BaseModel):
    """One tool call the model asked for: a stable id, the tool name and its raw arguments.

    ``call_id`` is the model-assigned correlation id echoed back on the matching tool result so a
    multi-tool turn pairs each result to its request. ``arguments`` is the raw mapping the model
    produced; it is validated against the tool's declared ``args_model`` by the registry before the
    handler runs, so a malformed call fails validation rather than inside a tool.
    """

    model_config = ConfigDict(frozen=True)

    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class QaMessage(BaseModel):
    """One turn in the Q&A conversation, provider-neutral (task 9.1).

    ``role`` is ``system`` / ``user`` / ``assistant`` / ``tool``. An assistant turn that asks for
    tools carries ``tool_calls`` and usually empty ``content``; a ``tool`` turn carries the result
    ``content`` and the ``tool_call_id`` it answers. Kept a plain frozen model so the loop can
    accumulate history and the checkpointer can serialize it without an SDK type in the state.
    """

    model_config = ConfigDict(frozen=True)

    role: str
    content: str = ""
    tool_calls: tuple[QaToolCall, ...] = ()
    #: Set only on a ``tool`` message: the ``call_id`` of the assistant tool call it answers.
    tool_call_id: str | None = None
    #: Set only on a ``tool`` message: the tool name, for readability in traces and transcripts.
    name: str | None = None


class ConverseRequest(BaseModel):
    """A tool-calling turn: the system instruction, the running history, and the available tools.

    ``tools`` is the Bedrock/OpenAI function-tool list produced by
    :func:`c360.tools.schemas.tool_schemas`; the provider binds it to the model so the model may
    reply with tool calls. ``route`` is the router's classification, carried so the mock provider
    can choose deterministically and a live provider can nudge the model in the system framing.
    """

    model_config = ConfigDict(frozen=True)

    system: str
    messages: tuple[QaMessage, ...]
    tools: tuple[dict[str, Any], ...] = ()
    route: QaRoute = QaRoute.BOTH
    max_tokens: int = Field(default=1024, ge=1)
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)


class ConverseResponse(BaseModel):
    """The model's reply to a :class:`ConverseRequest`: tool calls to run, or a final answer.

    Exactly one of the two is meaningful per turn. When ``tool_calls`` is non-empty the loop runs
    them and calls back; when it is empty, ``text`` is the model's final answer and the loop moves
    to claim validation. ``model_id`` and ``usage`` flow onto the audit record and the cost metric
    exactly as on the one-shot path.
    """

    model_config = ConfigDict(frozen=True)

    text: str = ""
    tool_calls: tuple[QaToolCall, ...] = ()
    model_id: str
    usage: TokenUsage = TokenUsage()

    @property
    def wants_tools(self) -> bool:
        """Whether the model asked to call at least one tool this turn."""
        return len(self.tool_calls) > 0


__all__ = [
    "ConverseRequest",
    "ConverseResponse",
    "QaMessage",
    "QaRoute",
    "QaToolCall",
]
