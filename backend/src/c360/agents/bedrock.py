"""``BedrockProvider`` — the live generation provider (task 8.2, design §8.5).

Wraps ``langchain_aws.ChatBedrockConverse`` for generation and reuses the Phase 7
:class:`~c360.knowledge.embeddings.BedrockEmbeddingProvider` for Titan embeddings, so there is one
embedding implementation across ingestion, retrieval and the agent path. Everything AWS is imported
*inside* ``__init__`` — the class is only ever constructed when ``LLM_PROVIDER`` is not ``mock``, so
CI, evaluation and any ``mock`` deployment never import ``boto3`` or ``langchain_aws`` (§8.5).

What this class guarantees, and why each matters
------------------------------------------------

* **Credentials come from the default AWS chain, never config.** The client is built with a region
  and model ids from settings and nothing else; a key never appears in source or configuration
  (Phase 0 rule).
* **Throttling never becomes a 500.** botocore's adaptive retry mode with a bounded attempt count
  handles transient ``ThrottlingException`` under concurrency; if retries are exhausted the error is
  re-raised as :class:`ProviderThrottledError`, which the node wrapper (task 8.6) and the circuit
  breaker (task 8.11) turn into a degraded template render, not an unhandled 500.
* **An optional Guardrail is applied to every call when configured.** ``BEDROCK_GUARDRAIL_ID`` binds
  a Bedrock Guardrail to the chat model so the same policy governs every generation.

Streaming and tool-calling round out the port for the SSE (task 8.9) and Q&A (Phase 9) paths.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from c360.agents.provider import CompletionRequest, CompletionResponse, TokenUsage
from c360.agents.qa_types import ConverseRequest, ConverseResponse, QaToolCall

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from c360.core.config import Settings
    from c360.knowledge.embeddings import Vector


class ProviderThrottledError(RuntimeError):
    """Raised when Bedrock throttling survives the retry budget (design §8.5).

    A distinct type so the node wrapper and circuit breaker can route it to the template fallback
    rather than letting it surface as a 500 — throttling under concurrency is expected, not a fault.
    """


class ProviderUnavailableError(RuntimeError):
    """Raised when the Bedrock generation call fails for a non-throttling reason.

    Also routed to the template fallback; the distinction from throttling exists only so the two are
    counted separately in metrics (task 8.10).
    """


class BedrockProvider:
    """Amazon Bedrock generation via ``ChatBedrockConverse`` plus Titan embeddings (task 8.2)."""

    __slots__ = ("_chat", "_embedder", "model_id")

    def __init__(self, settings: Settings) -> None:
        from botocore.config import Config  # noqa: PLC0415 - lazy so the SDK stays optional
        from langchain_aws import ChatBedrockConverse  # noqa: PLC0415

        from c360.knowledge.embeddings import BedrockEmbeddingProvider  # noqa: PLC0415

        self.model_id = settings.bedrock_model_id
        # Adaptive retry mode is botocore's throttling-aware backoff; the attempt cap is the
        # design's "max 3 attempts". The read timeout sits below the per-node asyncio timeout so the
        # node, not the socket, owns the deadline.
        boto_config = Config(
            retries={"max_attempts": settings.bedrock_max_retries, "mode": "adaptive"},
            read_timeout=settings.bedrock_read_timeout_s,
            region_name=settings.aws_region,
        )
        guardrail: dict[str, Any] | None = None
        if settings.bedrock_guardrail_id.strip():
            # A Guardrail, when configured, is bound to the model so it applies to every invocation.
            guardrail = {
                "guardrailIdentifier": settings.bedrock_guardrail_id,
                "guardrailVersion": "DRAFT",
            }
        self._chat = ChatBedrockConverse(
            model_id=settings.bedrock_model_id,
            region_name=settings.aws_region,
            max_tokens=settings.bedrock_max_tokens,
            temperature=0.0,
            config=boto_config,
            guardrail_config=guardrail,
        )
        self._embedder = BedrockEmbeddingProvider(
            region=settings.aws_region,
            model_id=settings.bedrock_embed_model_id,
            max_retries=settings.bedrock_max_retries,
        )

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Generate one narrative, mapping throttling and failure onto typed errors."""
        messages = _messages(request)
        try:
            result = await self._chat.ainvoke(
                messages,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
            )
        except Exception as exc:
            raise _classify(exc) from exc
        return _to_response(result, self.model_id)

    async def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        """Stream the narrative chunk by chunk for the SSE path (task 8.9)."""
        messages = _messages(request)

        async def _gen() -> AsyncIterator[str]:
            try:
                async for chunk in self._chat.astream(
                    messages, max_tokens=request.max_tokens, temperature=request.temperature
                ):
                    text = _chunk_text(chunk)
                    if text:
                        yield text
            except Exception as exc:
                raise _classify(exc) from exc

        return _gen()

    async def converse(self, request: ConverseRequest) -> ConverseResponse:
        """One tool-calling turn: bind the tool schemas and return tool calls or a final answer.

        The tool schemas (:func:`c360.tools.schemas.tool_schemas`) are bound to the model so it may
        reply with structured tool calls; the loop, not this method, runs them and calls back. The
        returned :class:`~c360.agents.qa_types.ConverseResponse` carries either the tool calls the
        model asked for or its final text — throttling and failure are mapped to the same typed
        errors the one-shot path uses, so the Q&A breaker and fallback treat them identically.
        """
        chat = self._chat.bind_tools(list(request.tools)) if request.tools else self._chat
        try:
            result = await chat.ainvoke(
                _converse_messages(request),
                max_tokens=request.max_tokens,
                temperature=request.temperature,
            )
        except Exception as exc:
            raise _classify(exc) from exc
        return _to_converse_response(result, self.model_id)

    async def embed(self, texts: Sequence[str], *, dimensions: int) -> list[Vector]:
        return self._embedder.embed(texts, dimensions=dimensions)


# ---------------------------------------------------------------- helpers


def _messages(request: CompletionRequest) -> list[tuple[str, str]]:
    """Build the (role, content) message list Converse consumes.

    The fact table and the fenced reference block go into the human turn as clearly delimited
    sections; the system instruction is the agent's role and the citation contract. Kept as tuples
    (LangChain accepts ``("system", text)`` / ``("human", text)``) so this module needs no import of
    LangChain's message classes and stays cheap to construct.
    """
    fact_lines = "\n".join(
        f"[{fact.fact_id}] {fact.entity_type}.{fact.field} = {fact.value}"
        for fact in request.facts.facts
    )
    human_parts = [request.task, "", "FACTS (cite figures by [F] id):", fact_lines or "(none)"]
    reference = request.reference_block()
    if reference:
        human_parts += ["", reference]
    return [("system", request.system), ("human", "\n".join(human_parts))]


def _to_response(result: object, model_id: str) -> CompletionResponse:
    text = _chunk_text(result)
    usage = getattr(result, "usage_metadata", None) or {}
    return CompletionResponse(
        text=text,
        model_id=model_id,
        usage=TokenUsage(
            input_tokens=int(usage.get("input_tokens", 0)) if isinstance(usage, dict) else 0,
            output_tokens=int(usage.get("output_tokens", 0)) if isinstance(usage, dict) else 0,
        ),
    )


def _chunk_text(message: object) -> str:
    """Extract text from a LangChain message or chunk whose ``content`` may be str or block list."""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return ""


def _converse_messages(request: ConverseRequest) -> list[Any]:
    """Translate the neutral Q&A history into LangChain message objects for Converse.

    A tool-calling loop replays prior turns to the model, and Bedrock's Converse (via LangChain)
    is strict about their shape: an assistant turn that asked for tools must be an ``AIMessage``
    carrying ``tool_calls`` (its ``content`` a string or block list, never a dict), and each tool
    result must be a ``ToolMessage`` bound to the ``tool_call_id`` it answers. Passing a
    ``(role, dict)`` tuple instead makes LangChain treat the dict as the message *content*, which
    fails ``AIMessage`` validation — so real LangChain message objects are built here. The imports
    are function-local so the module stays importable without the SDK on the mock path.
    """
    from langchain_core.messages import (  # noqa: PLC0415 - lazy; only the live path needs it
        AIMessage,
        HumanMessage,
        SystemMessage,
        ToolMessage,
    )

    messages: list[Any] = [SystemMessage(content=request.system)]
    for message in request.messages:
        if message.role == "tool":
            messages.append(
                ToolMessage(
                    content=message.content,
                    tool_call_id=message.tool_call_id or "",
                    name=message.name or None,
                )
            )
        elif message.role == "assistant" and message.tool_calls:
            messages.append(
                AIMessage(
                    content=message.content or "",
                    tool_calls=[
                        {
                            "id": call.call_id,
                            "name": call.name,
                            "args": call.arguments,
                        }
                        for call in message.tool_calls
                    ],
                )
            )
        elif message.role == "assistant":
            messages.append(AIMessage(content=message.content))
        else:
            messages.append(HumanMessage(content=message.content))
    return messages


def _to_converse_response(result: object, model_id: str) -> ConverseResponse:
    """Read tool calls and text off a LangChain ``AIMessage`` into a neutral response."""
    text = _chunk_text(result)
    usage = getattr(result, "usage_metadata", None) or {}
    raw_calls = getattr(result, "tool_calls", None) or []
    tool_calls: list[QaToolCall] = []
    for index, call in enumerate(raw_calls):
        if not isinstance(call, dict):
            continue
        args = call.get("args")
        tool_calls.append(
            QaToolCall(
                call_id=str(call.get("id") or f"call_{index + 1}"),
                name=str(call.get("name", "")),
                arguments=args if isinstance(args, dict) else {},
            )
        )
    return ConverseResponse(
        text=text,
        tool_calls=tuple(tool_calls),
        model_id=model_id,
        usage=TokenUsage(
            input_tokens=int(usage.get("input_tokens", 0)) if isinstance(usage, dict) else 0,
            output_tokens=int(usage.get("output_tokens", 0)) if isinstance(usage, dict) else 0,
        ),
    )


def _classify(exc: Exception) -> Exception:
    """Map a boto/LangChain exception onto a throttling vs unavailable provider error.

    ``ThrottlingException`` (and its close relatives) are matched by name rather than by importing
    botocore's exception classes, so this stays importable without the SDK and robust to LangChain
    wrapping the underlying error.
    """
    name = type(exc).__name__
    text = f"{name}: {exc}"
    if "Throttl" in text or "TooManyRequests" in name:
        return ProviderThrottledError(str(exc))
    return ProviderUnavailableError(str(exc))


__all__ = ["BedrockProvider", "ProviderThrottledError", "ProviderUnavailableError"]
