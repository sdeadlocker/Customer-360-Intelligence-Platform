"""Optional Bedrock reranking (task 7.6, design §9.4 step 6, requirement 17.13).

Reranking is a second-pass relevance model applied to the fused candidates before context assembly.
It is region-limited (Amazon Rerank 1.0 / Cohere Rerank 3.5 are not available in every region) and
adds a network round trip the 500 ms dashboard-agent retrieval budget cannot absorb, so design §9.4
scopes it to **Q&A only** and puts it behind ``RERANK_ENABLED`` plus a model id.

Two behaviours are load-bearing:

* **Graceful fallback.** When reranking is disabled, unconfigured, or unavailable at call time (the
  model is not in-region, the call throttles, boto3 is not installed), the reranker returns the
  fusion order unchanged rather than failing the request (requirement 17.13). A retrieval must never
  fail *because* an optional quality stage was unavailable.
* **Order only, never content.** The reranker reorders and may trim the candidate list; it never
  invents a passage or a score the fusion layer did not already produce. The passages it returns are
  the same passages, in a possibly better order.

The :class:`NullReranker` is the default and the fallback target. :class:`BedrockReranker` imports
``boto3`` lazily, like the Bedrock embedding provider, so the knowledge package stays importable
without the AWS SDK.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from c360.core.logging import get_logger
from c360.knowledge.models import Passage

_logger = get_logger(__name__)


@runtime_checkable
class Reranker(Protocol):
    """Reorders assembled passages by relevance to the query. Must never fail the request."""

    def rerank(self, query: str, passages: list[Passage]) -> list[Passage]:
        """Return ``passages`` reordered best-first, or unchanged on any unavailability."""
        ...


class NullReranker:
    """The default: return passages in the order fusion produced (design §9.4 fallback).

    Used whenever reranking is disabled or unconfigured, and is what :class:`BedrockReranker` falls
    back to internally when the model call cannot be made. Having an explicit no-op reranker means
    the retrieval path is identical whether or not reranking is on — the pipeline always calls
    ``rerank``; only the implementation changes.
    """

    def rerank(self, query: str, passages: list[Passage]) -> list[Passage]:
        return passages


class BedrockReranker:
    """Amazon Bedrock Rerank over fused candidates (task 7.6, design §9.4 step 6).

    ``boto3`` is imported in ``__init__`` so the SDK stays optional. Any failure to construct the
    client or to call the model is swallowed and the passages are returned in their incoming order —
    the request must not fail because an optional stage was unavailable (requirement 17.13). The
    concrete request shape and throttling behaviour are completed with the Bedrock provider in
    Phase 8; here the contract and the fallback are what matter.
    """

    def __init__(self, *, region: str, model_id: str, max_retries: int = 3) -> None:
        self.model_id = model_id
        self._region = region
        self._client: object | None = None
        try:
            import boto3  # noqa: PLC0415 - lazy so the SDK is optional
            from botocore.config import Config  # noqa: PLC0415

            self._client = boto3.client(
                "bedrock-agent-runtime",
                region_name=region,
                config=Config(retries={"max_attempts": max_retries, "mode": "adaptive"}),
            )
        except Exception as exc:
            _logger.warning(
                "Bedrock rerank client unavailable; falling back to fusion order",
                extra={"error_type": type(exc).__name__},
            )

    def rerank(self, query: str, passages: list[Passage]) -> list[Passage]:
        if self._client is None or not passages:
            return passages
        try:
            order = self._invoke(query, [passage.text for passage in passages])
        except Exception as exc:
            _logger.warning(
                "Bedrock rerank call failed; falling back to fusion order",
                extra={"error_type": type(exc).__name__},
            )
            return passages
        # `order` is a list of indices into `passages`, best-first. Any index the model omits is
        # appended in its original position so no passage is silently dropped.
        seen: set[int] = set()
        reordered: list[Passage] = []
        for index in order:
            if 0 <= index < len(passages) and index not in seen:
                reordered.append(passages[index])
                seen.add(index)
        for index, passage in enumerate(passages):
            if index not in seen:
                reordered.append(passage)
        return reordered

    def _invoke(self, query: str, documents: list[str]) -> list[int]:
        """Call Bedrock Rerank and return candidate indices best-first.

        Isolated so the fallback logic in :meth:`rerank` is testable without AWS and so Phase 8 can
        flesh out the exact API shape in one place.
        """
        import json  # noqa: PLC0415

        assert self._client is not None  # noqa: S101 - guarded by the caller
        sources = [
            {
                "type": "INLINE",
                "inlineDocumentSource": {"type": "TEXT", "textDocument": {"text": doc}},
            }
            for doc in documents
        ]
        response = self._client.rerank(  # type: ignore[attr-defined]
            queries=[{"type": "TEXT", "textQuery": {"text": query}}],
            sources=sources,
            rerankingConfiguration={
                "type": "BEDROCK_RERANKING_MODEL",
                "bedrockRerankingConfiguration": {
                    "modelConfiguration": {"modelArn": self.model_id},
                    "numberOfResults": len(documents),
                },
            },
        )
        _ = json  # reserved for future response parsing shapes
        return [int(result["index"]) for result in response["results"]]


def build_reranker(settings: object) -> Reranker:
    """Select the reranker from configuration (task 7.6).

    Returns :class:`NullReranker` unless ``RERANK_ENABLED`` is set with a model id, in which case a
    :class:`BedrockReranker` is built — which itself degrades to fusion order if the client cannot
    be constructed. So the caller always gets a working ``Reranker``; the only question is whether
    it actually reranks.
    """
    rerank_enabled = bool(getattr(settings, "rerank_enabled", False))
    model_id = str(getattr(settings, "rerank_model_id", "") or "")
    if not rerank_enabled or not model_id:
        return NullReranker()
    return BedrockReranker(
        region=str(getattr(settings, "aws_region", "")),
        model_id=model_id,
        max_retries=int(getattr(settings, "bedrock_max_retries", 3)),
    )


__all__ = ["BedrockReranker", "NullReranker", "Reranker", "build_reranker"]
