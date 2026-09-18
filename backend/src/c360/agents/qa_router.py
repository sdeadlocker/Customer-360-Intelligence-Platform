"""The Q&A router: classify a question as facts / knowledge / both (task 9.1, design §10.1).

The router is the ReAct loop's cheap first pass. Its only job is to say whether a question needs the
customer's own figures, institutional knowledge, or both — so a pure policy question ("what are the
HELOC eligibility rules?") skips the customer tools and answers faster, and a mixed question ("is
*this* customer eligible?") gets both. The classification is advisory: it shapes which tools the
agent is nudged toward, never a hard gate — the agent may still call any tool it needs.

A keyword classifier, not a model call
--------------------------------------

Design §10.1 calls the router "a cheap first pass". Spending a model round-trip on it would add
latency to the very budget it exists to protect, and the classification is coarse enough that
lexical signals carry it: words about rules, policies, eligibility and procedures point at
knowledge; words about *this customer's* balances, accounts and score point at facts; the presence
of both, or of neither strong signal, means both. Keeping it deterministic also makes it testable
without a model and identical under the mock and live providers.
"""

from __future__ import annotations

from c360.agents.qa_types import QaRoute

#: Signals that a question is about institutional knowledge — rules, policy, procedure, terms.
_KNOWLEDGE_SIGNALS: tuple[str, ...] = (
    "policy",
    "policies",
    "eligibility",
    "eligible",
    "qualify",
    "requirement",
    "criteria",
    "rule",
    "rules",
    "procedure",
    "process",
    "terms",
    "how do i",
    "how to",
    "guideline",
    "compliant",
    "compliance",
    "allowed",
    "permitted",
    "documentation",
)

#: Signals that a question is about the customer's own record — their figures, holdings, history.
_FACT_SIGNALS: tuple[str, ...] = (
    "their",
    "this customer",
    "his ",
    "her ",
    "balance",
    "net worth",
    "account",
    "deposit",
    "loan",
    "card",
    "credit score",
    "fico",
    "transaction",
    "spend",
    "spending",
    "expense",
    "income",
    "salary",
    "risk",
    "offer",
    "household",
    "relationship",
    "connected",
    "portfolio",
    "holdings",
    "customers",
    "clients",
    "cohort",
    "pitch",
    "talking point",
    "delinquent",
    "past due",
)


def classify_route(question: str) -> QaRoute:
    """Classify ``question`` into a :class:`QaRoute` from lexical signals (design §10.1).

    Both signals present, or a possessive reference alongside a knowledge word, yields ``BOTH``.
    Only knowledge signals yields ``KNOWLEDGE``; only fact signals (or nothing recognisable, which
    defaults to reading the customer) yields ``FACTS``.
    """
    lowered = f" {question.lower().strip()} "
    has_knowledge = any(signal in lowered for signal in _KNOWLEDGE_SIGNALS)
    has_facts = any(signal in lowered for signal in _FACT_SIGNALS)

    if has_knowledge and has_facts:
        return QaRoute.BOTH
    if has_knowledge:
        return QaRoute.KNOWLEDGE
    # No knowledge signal: a customer question, or an unrecognised one that still concerns the
    # customer in view — default to facts so the loop grounds in the record rather than guessing.
    return QaRoute.FACTS


__all__ = ["classify_route"]
