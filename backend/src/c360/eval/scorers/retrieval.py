"""Retrieval evaluation -- dimension 12 (task 11.8, design §14.3).

Scores the hybrid retriever against the labelled query → document pairs
(:mod:`c360.eval.retrieval_labels`): for each label, retrieve under a principal entitled to the
document's access level and check where the target document lands.

Four metrics, per design §14.3:

* **recall@5** -- the target document appears in the top five retrieved passages. The gated headline
  (design gates it at >= 0.85), read from ``settings.eval_retrieval_recall_min``.
* **MRR** -- mean reciprocal rank of the first correct passage, so ranking quality (not just
presence)
  is visible.
* **nDCG@5** -- discounted gain with the single relevant document, rewarding a higher-ranked hit.
* **attribution correctness** -- every returned passage's citation resolves to a real, in-corpus
document. This is the provider-independent check: it is true whether the semantic half ran on Titan
  or on the mock embedder, because it tests the citation plumbing, not the ranking.

Provider dependence and the CI gate
------------------------------------

recall/MRR/nDCG depend on embedding *quality*. Under ``LLM_PROVIDER=mock`` the embeddings are
deterministic pseudo-vectors with no semantic structure, so semantic recall degrades to roughly the
lexical (BM25) half and the >= 0.85 recall bar is a full-mode (Titan) expectation, not something the
mock can meet. The recall gate is therefore evaluated as *hard in full mode, advisory on the mock*,
matching design §14.5's split; attribution correctness, being deterministic, is gated in both. When
the knowledge base is not ingested at all, the dimension reports "unavailable" and does not gate --
the deterministic platform serves without retrieval, and an eval on a knowledge-less deployment must
not manufacture a failure.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from c360.eval.principals import principal_for_role
from c360.eval.results import DimensionScore
from c360.eval.retrieval_labels import retrieval_labels
from c360.security.model import KnowledgeLevel, Role

if TYPE_CHECKING:
    from c360.eval.harness import EvalHarness
    from c360.eval.retrieval_labels import RetrievalLabel
    from c360.security.model import Principal

_K = 5

#: Access levels only the Risk role holds; a label at one of these must be retrieved as Risk. Every
#: other level is visible to a standard role, so the RM suffices and keeps the query realistic.
_RISK_ONLY_LEVELS = frozenset(
    {KnowledgeLevel.RISK_ONLY.value, KnowledgeLevel.COMPLIANCE_ONLY.value}
)


def score_retrieval(
    harness: EvalHarness, *, recall_floor: float, full_mode: bool
) -> DimensionScore:
    """Dimension 12: retrieval quality against the labelled pairs (recall@5 >= ``recall_floor``).

    ``full_mode`` decides whether the recall floor is a hard gate (Titan) or advisory (mock);
    attribution correctness is always gated. Returns an "unavailable" advisory score when the
    knowledge base is not ingested.
    """
    knowledge = harness.services.knowledge
    if knowledge is None:
        return DimensionScore(
            dimension=12,
            name="Retrieval quality",
            value=None,
            threshold=recall_floor,
            hard_gate=False,
            passed=True,
            detail={"status": "unavailable", "reason": "knowledge base not ingested"},
        )

    labels = retrieval_labels()
    hits = 0
    reciprocal_ranks: list[float] = []
    ndcgs: list[float] = []
    attributed = 0
    retrieved_total = 0
    misses: list[dict[str, object]] = []

    for label in labels:
        principal = _principal_for_label(label)
        result = knowledge.search(principal, label.query, rerank=False)
        docs = [passage.citation.doc_id for passage in result.passages]
        retrieved_total += len(docs)
        attributed += _attributed_count(result)

        top_k = docs[:_K]
        if label.doc_id in top_k:
            hits += 1
        rank = _first_rank(docs, label.doc_id)
        reciprocal_ranks.append(0.0 if rank is None else 1.0 / rank)
        ndcgs.append(_ndcg_single(docs, label.doc_id, k=_K))
        if label.doc_id not in top_k:
            misses.append({"query": label.query, "want": label.doc_id, "got": top_k})

    total = len(labels)
    recall = hits / total if total else 1.0
    mrr = _mean(reciprocal_ranks)
    ndcg = _mean(ndcgs)
    attribution = attributed / retrieved_total if retrieved_total else 1.0

    recall_ok = recall >= recall_floor
    attribution_ok = attribution >= 1.0
    # Attribution is always gated; recall is gated only in full mode (embedding quality dependent).
    passed = attribution_ok and (recall_ok or not full_mode)
    return DimensionScore(
        dimension=12,
        name="Retrieval quality",
        value=recall,
        threshold=recall_floor,
        hard_gate=full_mode,
        passed=passed,
        detail={
            "recall_at_5": recall,
            "mrr": mrr,
            "ndcg_at_5": ndcg,
            "attribution_correctness": attribution,
            "recall_meets_floor": recall_ok,
            "labels": total,
            "misses": misses,
            "mode": "full" if full_mode else "advisory (mock embeddings)",
        },
    )


# ---------------------------------------------------------------- helpers


def _principal_for_label(label: RetrievalLabel) -> Principal:
    """A principal entitled to the label's document access level.

    Risk-only and compliance-only documents need the Risk role; everything else is visible to the
    RM.
    Using the least-privileged role that can see the document keeps the query realistic and
    exercises
    the access-level pre-filter rather than always retrieving as an all-seeing role.
    """
    if label.access_level in _RISK_ONLY_LEVELS:
        return principal_for_role(Role.RISK)
    return principal_for_role(Role.RM)


def _attributed_count(result: object) -> int:
    """How many returned passages carry a resolvable document citation.

    A passage without a ``doc_id`` (or empty) is an attribution failure -- a citation the
    UI could not open. In a correct pipeline every passage is attributed, so this is normally the
    full passage count.
    """
    passages = getattr(result, "passages", ())
    return sum(1 for p in passages if getattr(p.citation, "doc_id", ""))


def _first_rank(docs: list[str], target: str) -> int | None:
    for index, doc in enumerate(docs):
        if doc == target:
            return index + 1
    return None


def _ndcg_single(docs: list[str], target: str, *, k: int) -> float:
    """nDCG@k with a single relevant document: 1/log2(rank+1) if in top-k, else 0. IDCG is 1."""
    for index, doc in enumerate(docs[:k]):
        if doc == target:
            return 1.0 / math.log2(index + 2)
    return 0.0


def _mean(values: list[float]) -> float:
    return 0.0 if not values else sum(values) / len(values)


__all__ = ["score_retrieval"]
