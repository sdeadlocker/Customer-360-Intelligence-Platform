"""The ``knowledge_search`` typed tool (task 7.8, design §9.6).

Retrieval is registered in the *same* tool registry as the customer tools, deliberately: it
inherits the execution span (``gen_ai.execute_tool``), the argument validation and — through the
shared :class:`ToolContext` — the same principal the customer tools authorize against. The design
calls this out (§9.6): "one more entry in the same typed tool registry ... with no parallel
mechanism to keep in sync."

Unlike a customer tool, ``knowledge_search`` returns no fact table and no masked customer payload:
knowledge is institutional, not customer-specific (design §9.1), so there is nothing to mask and no
figure to wrap as a fact. What it returns is passages with resolvable citations, plus the retrieval
counters the metrics and the "no supporting guidance" behaviour read. Entitlement still applies —
the principal's knowledge levels are the access-level pre-filter — but it is enforced in the
retrieval SQL, not by the masking serializer.

The tool does not itself write the audit ``retrieved_doc_ids``; that is written at the endpoint and,
in Phase 9, on the Q&A tool loop, because the audit record is a per-request artefact the tool layer
does not own. The tool surfaces the retrieved document ids on its result so the caller can audit
them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from c360.knowledge.models import KnowledgeDomain, Passage
from c360.tools.registry import ToolContext, ToolSpec

if TYPE_CHECKING:
    from c360.services.knowledge import KnowledgeService


class KnowledgeSearchArgs(BaseModel):
    """Arguments for ``knowledge_search`` (design §9.6: ``knowledge_search(query, domains, k)``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str = Field(min_length=1, description="The retrieval query. Must not contain PII.")
    domains: tuple[KnowledgeDomain, ...] = Field(
        default=(),
        description="Restrict to these knowledge domains, or search all when omitted.",
    )
    product_code: str | None = Field(
        default=None, description="Narrow to a single product code (e.g. 'HELOC-VAR')."
    )
    rerank: bool = Field(
        default=False,
        description="Apply Bedrock reranking (Q&A only; ignored on the dashboard-agent path).",
    )


class KnowledgePassage(BaseModel):
    """One cited passage from the tool: text plus everything needed to cite it (17.5, 17.6)."""

    model_config = ConfigDict(frozen=True)

    text: str
    score: float
    doc_id: str
    version: str
    section_path: str
    title: str
    effective_from: str
    effective_to: str | None = None
    chunk_id: str

    @classmethod
    def of(cls, passage: Passage) -> KnowledgePassage:
        c = passage.citation
        return cls(
            text=passage.text,
            score=passage.fused_score,
            doc_id=c.doc_id,
            version=c.version,
            section_path=c.section_path,
            title=c.title,
            effective_from=c.effective_from,
            effective_to=c.effective_to,
            chunk_id=c.chunk_id,
        )


class KnowledgeToolResult(BaseModel):
    """The ``knowledge_search`` result: cited passages and the retrieval behaviour counters.

    ``retrieved_doc_ids`` is surfaced so the caller (endpoint or Q&A loop) can write it to the audit
    log (requirement 12.6). ``no_guidance_found`` is the explicit "nothing relevant" signal the
    agent path turns into "no supporting guidance was found" (requirement 17.10) rather than
    fabricating an answer.
    """

    model_config = ConfigDict(frozen=True)

    passages: tuple[KnowledgePassage, ...] = ()
    candidate_count: int = 0
    reranked: bool = False

    @property
    def retrieved_doc_ids(self) -> tuple[str, ...]:
        # Deduplicated in first-seen order, so the audit list is stable and free of repeats when two
        # passages come from the same document.
        seen: dict[str, None] = {}
        for passage in self.passages:
            seen.setdefault(passage.doc_id, None)
        return tuple(seen)

    @property
    def no_guidance_found(self) -> bool:
        return len(self.passages) == 0


class KnowledgeUnavailableError(RuntimeError):
    """Raised when the knowledge base is not available (not ingested, or sqlite-vec missing).

    The tool path turns this into a graceful "no supporting guidance found" rather than a hard
    failure, because a missing knowledge base is a degraded state, not a broken request.
    """


def _knowledge_search(context: ToolContext, args: KnowledgeSearchArgs) -> KnowledgeToolResult:
    service: KnowledgeService | None = context.services.knowledge
    if service is None:
        raise KnowledgeUnavailableError("the knowledge base is not available")
    result = service.search(
        context.principal,
        args.query,
        domains=args.domains,
        product_code=args.product_code,
        rerank=args.rerank,
    )
    return KnowledgeToolResult(
        passages=tuple(KnowledgePassage.of(passage) for passage in result.passages),
        candidate_count=result.candidate_count,
        reranked=result.reranked,
    )


KNOWLEDGE_TOOL_SPEC: ToolSpec[KnowledgeSearchArgs, KnowledgeToolResult] = ToolSpec(
    name="knowledge_search",
    description=(
        "Search institutional knowledge — product catalog, policy, procedure, offer terms, "
        "playbooks and compliance language — for rules, criteria and guidance. Returns cited "
        "passages, never customer figures. Use for eligibility rules, procedures and terms; use "
        "customer tools for any customer-specific value."
    ),
    args_model=KnowledgeSearchArgs,
    result_model=KnowledgeToolResult,
    handler=_knowledge_search,
)


__all__ = [
    "KNOWLEDGE_TOOL_SPEC",
    "KnowledgePassage",
    "KnowledgeSearchArgs",
    "KnowledgeToolResult",
    "KnowledgeUnavailableError",
]
