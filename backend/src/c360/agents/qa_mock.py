"""The mock provider's deterministic Q&A tool-calling behaviour (task 9.1, design §8.5).

Kept out of :mod:`c360.agents.provider` so the port module stays small and the emulation logic — a
keyword heuristic plus a fact-grounded renderer — lives on its own. Two rules make this a *faithful*
stand-in for a tool-using model rather than a canned reply:

1. It only ever asks for tools that were actually offered in the request. A test that binds the full
   registry sees the loop pick real tools; a test that offers none gets a straight answer.
2. Its final answer copies figures out of the returned facts and cites them by ``F``-id, and copies
   guidance out of returned passages and cites them by ``P``-id — never inventing a number and never
   citing a figure to a passage — so it passes the *same* claim validator the live path must pass.

The tool-result wire format
---------------------------

The Q&A graph serializes each tool result to JSON in the ``tool`` message's ``content`` via
:func:`c360.agents.qa_wire.encode_tool_result`. This module reads that same shape back
(:func:`c360.agents.qa_wire.decode_facts` / :func:`~c360.agents.qa_wire.decode_passages`) so the
mock and the graph share one contract and neither hard-codes the other's internals.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from c360.agents.provider import TokenUsage
from c360.agents.qa_types import ConverseRequest, ConverseResponse, QaMessage, QaRoute, QaToolCall
from c360.agents.qa_wire import current_turn, decode_facts, decode_passages

if TYPE_CHECKING:
    from collections.abc import Sequence


#: Keyword → customer tool name. Ordered by specificity so "credit card balance" prefers holdings
#: over a bare "credit" match; the first keyword found in the question that also names an offered
#: tool wins. Deliberately small — the mock only needs to exercise the loop, not to route perfectly.
_KEYWORD_TOOLS: tuple[tuple[str, str], ...] = (
    ("net worth", "financial_profile"),
    ("balance", "holdings"),
    ("deposit", "holdings"),
    ("holding", "holdings"),
    ("account", "holdings"),
    ("transaction", "transactions_query"),
    ("spend", "expense_analytics"),
    ("expense", "expense_analytics"),
    ("credit score", "credit_profile"),
    ("fico", "credit_profile"),
    ("credit", "credit_profile"),
    ("risk", "risk_profile"),
    ("offer", "offers"),
    ("recommend", "offers"),
    ("life event", "life_events"),
    ("household", "household"),
    ("relationship", "relationships"),
    ("connected", "graph_neighborhood"),
    ("network", "graph_neighborhood"),
    ("contact", "contact"),
    ("email", "contact"),
    ("phone", "contact"),
)

_KNOWLEDGE_TOOL = "knowledge_search"
_PROFILE_TOOL = "profile"
_SEARCH_TOOL = "customer_search"


def mock_converse(request: ConverseRequest, *, model_id: str) -> ConverseResponse:
    """Produce one deterministic Q&A turn: tool calls first, a grounded answer once results exist.

    Scoped to the current turn (everything from the last user message on) so a checkpointed
    follow-up does not see the prior turn's tool results as already-satisfied.

    On the cross-customer path (no ``customer_id=`` in the system framing) the loop first resolves a
    customer with ``customer_search``; once that result is in the turn the resolved id is threaded
    into the reading tools, mirroring what a live model does when handed the search tool.
    """
    turn = current_turn(request.messages)
    resolved = _resolved_customer_id(turn)
    if _needs_customer_resolution(request, turn, resolved):
        return _search_turn(request, turn, model_id=model_id)
    # Cross-customer path where the search ran but resolved no one: answer plainly rather than
    # calling a reading tool with no customer id (requirement 11.5, "no matching customer").
    cross_customer = _customer_id_hint(request) is None
    if cross_customer and _search_attempted(turn) and resolved is None:
        return _no_match_answer(model_id=model_id)
    if _has_reading_results(turn) or not request.tools:
        return _final_answer(request, turn, model_id=model_id)
    return _tool_turn(request, turn, resolved, model_id=model_id)


# ---------------------------------------------------------------- first turn: choose tools


def _search_turn(
    request: ConverseRequest, turn: Sequence[QaMessage], *, model_id: str
) -> ConverseResponse:
    """First cross-customer turn: resolve the customer with ``customer_search`` (task 9.6).

    A live model would read the question and search for the identifying part (the customer's name or
    an identifier). The mock approximates that deterministically with :func:`_search_terms`, which
    lifts the proper-noun-like and identifier-like tokens out of the question — because the FTS
    query ANDs its terms, feeding it the whole sentence ("what is the net worth of …") would match
    nobody.
    """
    question = _latest_user_text(turn)
    call = QaToolCall(
        call_id="call_1",
        name=_SEARCH_TOOL,
        arguments={"query": _search_terms(question)},
    )
    return ConverseResponse(
        tool_calls=(call,),
        model_id=model_id,
        usage=TokenUsage(input_tokens=_estimate(question), output_tokens=4),
    )


#: Lowercase words that are never part of a customer's name or identifier, so they are dropped from
#: the resolver query. Small and closed — enough to strip a natural-language frame down to the name.
#: Matched case-insensitively, so a lowercased name in the question ("marta farooqi") still survives
#: while the surrounding filler ("give me details of …") is dropped.
_STOP_WORDS = frozenset(
    {
        "what",
        "whats",
        "what's",
        "who",
        "whos",
        "who's",
        "is",
        "are",
        "the",
        "a",
        "an",
        "of",
        "for",
        "to",
        "and",
        "on",
        "in",
        "at",
        "with",
        "their",
        "his",
        "her",
        "net",
        "worth",
        "risk",
        "risks",
        "balance",
        "balances",
        "account",
        "accounts",
        "profile",
        "detail",
        "details",
        "info",
        "information",
        "finance",
        "finances",
        "financial",
        "financials",
        "score",
        "scores",
        "relationship",
        "relationships",
        "household",
        "offer",
        "offers",
        "spending",
        "spend",
        "expense",
        "expenses",
        "income",
        "transactions",
        "transaction",
        "give",
        "get",
        "find",
        "look",
        "lookup",
        "search",
        "top",
        "does",
        "do",
        "did",
        "have",
        "has",
        "show",
        "me",
        "my",
        "tell",
        "about",
        "customer",
        "customers",
        "client",
        "clients",
        "how",
        "much",
        "many",
        "any",
        "all",
        "please",
        "can",
        "you",
        "give-me",
    }
)


def _search_terms(question: str) -> str:
    """Lift the name/identifier tokens out of a question for the ``customer_search`` query.

    Strips the natural-language frame (question words, verbs, prepositions and topic words like
    "risk"/"balance") and keeps what remains — typically the customer's name or an identifier. This
    is deliberately case-insensitive: a live model would understand "give me details of marta
    farooqi" regardless of capitalisation, so the mock keeps a lowercased "marta farooqi" too rather
    than only capitalised proper nouns.

    Because the FTS query ANDs its terms, leaving a filler word in ("give") would require a customer
    to match it and resolve nobody — which is exactly why the frame must be removed. Falls back to
    the whole question when nothing survives, so the resolver always has something to match on.
    """
    kept: list[str] = []
    for raw in question.split():
        token = raw.strip(".,?!;:\"'()")
        # Drop a trailing possessive ("farooqi's" -> "farooqi") so the name matches the index.
        lowered_token = token.lower()
        if lowered_token.endswith(("'s", "s'")):
            token = token[:-2]
        if not token or token.lower() in _STOP_WORDS:
            continue
        kept.append(token)
    return " ".join(kept) if kept else question.strip()


def _tool_turn(
    request: ConverseRequest,
    turn: Sequence[QaMessage],
    resolved: str | None,
    *,
    model_id: str,
) -> ConverseResponse:
    """Ask for the tools the route and the question keywords imply, from those on offer."""
    offered = {_tool_name(schema) for schema in request.tools}
    question = _latest_user_text(turn)
    # A customer resolved by an earlier customer_search wins over the (absent) system-framed id.
    customer_id = resolved or _customer_id_hint(request)

    calls: list[QaToolCall] = []
    if request.route is not QaRoute.KNOWLEDGE:
        tool = _pick_customer_tool(question, offered)
        if tool is not None:
            calls.append(
                QaToolCall(
                    call_id=f"call_{len(calls) + 1}",
                    name=tool,
                    arguments=_customer_args(tool, customer_id),
                )
            )
    if request.route is not QaRoute.FACTS and _KNOWLEDGE_TOOL in offered:
        calls.append(
            QaToolCall(
                call_id=f"call_{len(calls) + 1}",
                name=_KNOWLEDGE_TOOL,
                arguments={"query": _knowledge_query(question), "rerank": True},
            )
        )

    if not calls:
        # Nothing to call (e.g. FACTS route but no customer tool matched): answer straight so the
        # loop terminates rather than spinning.
        return _final_answer(request, turn, model_id=model_id)
    return ConverseResponse(
        tool_calls=tuple(calls),
        model_id=model_id,
        usage=TokenUsage(input_tokens=_estimate(question), output_tokens=4 * len(calls)),
    )


def _pick_customer_tool(question: str, offered: set[str]) -> str | None:
    lowered = question.lower()
    for keyword, tool in _KEYWORD_TOOLS:
        if keyword in lowered and tool in offered:
            return tool
    # A customer question with no keyword match still wants *some* customer grounding; profile is
    # the safe, always-available default.
    return _PROFILE_TOOL if _PROFILE_TOOL in offered else None


def _customer_args(tool: str, customer_id: str | None) -> dict[str, object]:
    if customer_id is None:
        return {}
    return {"customer_id": customer_id}


# ---------------------------------------------------------------- second turn: grounded answer


def _final_answer(
    request: ConverseRequest, turn: Sequence[QaMessage], *, model_id: str
) -> ConverseResponse:
    """Render an answer whose figures are copied from returned facts and cited by ``F``-id."""
    facts = decode_facts(turn)
    passages = decode_passages(turn)
    question = _latest_user_text(turn)

    lines: list[str] = []
    if facts:
        lines.append("Based on the customer's record:")
        for fact in facts:
            value = fact["value"]
            if isinstance(value, bool) or value is None:
                lines.append(f"- {fact['field']}: {value} [{fact['fact_id']}]")
            elif isinstance(value, (int, float)):
                lines.append(f"- {fact['field']} is {value} [{fact['fact_id']}].")
            else:
                lines.append(f"- {fact['field']}: {value} [{fact['fact_id']}].")
    if passages:
        lines.append("Relevant guidance:")
        for passage in passages:
            snippet = passage["text"].strip().splitlines()[0][:160] if passage["text"] else ""
            lines.append(f"- {snippet} [{passage['passage_id']}]")
    if not lines:
        lines.append(
            "No supporting facts or guidance were found for this question against the "
            f"available tools. Question: {question.strip()[:120]}"
        )

    text = "\n".join(lines)
    return ConverseResponse(
        text=text,
        model_id=model_id,
        usage=TokenUsage(input_tokens=_estimate(question), output_tokens=_estimate(text)),
    )


def _no_match_answer(*, model_id: str) -> ConverseResponse:
    """The cross-customer reply when ``customer_search`` resolved no one (requirement 11.5)."""
    text = "No matching customer was found for that question."
    return ConverseResponse(
        text=text,
        model_id=model_id,
        usage=TokenUsage(input_tokens=1, output_tokens=_estimate(text)),
    )


# ---------------------------------------------------------------- helpers


def _has_tool_results(messages: Sequence[QaMessage]) -> bool:
    return any(message.role == "tool" for message in messages)


def _has_reading_results(messages: Sequence[QaMessage]) -> bool:
    """Whether any *reading* tool (anything but ``customer_search``) has returned in this turn.

    The resolver's own result does not count: after ``customer_search`` the loop still needs to read
    the customer's details before it can answer, so a search-only history is not "done".
    """
    return any(message.role == "tool" and message.name != _SEARCH_TOOL for message in messages)


def _needs_customer_resolution(
    request: ConverseRequest, turn: Sequence[QaMessage], resolved: str | None
) -> bool:
    """Whether this cross-customer turn must resolve a customer before reading anything.

    True only when: the search tool is offered, the question is not pure-knowledge, no single
    customer is fixed in the system framing, and no ``customer_search`` result has resolved one yet.
    """
    offered = {_tool_name(schema) for schema in request.tools}
    if _SEARCH_TOOL not in offered or request.route is QaRoute.KNOWLEDGE:
        return False
    if _customer_id_hint(request) is not None or resolved is not None:
        return False
    return not _search_attempted(turn)


def _search_attempted(messages: Sequence[QaMessage]) -> bool:
    """Whether ``customer_search`` has already returned in this turn (resolved or empty)."""
    return any(message.role == "tool" and message.name == _SEARCH_TOOL for message in messages)


def _resolved_customer_id(messages: Sequence[QaMessage]) -> str | None:
    """The first customer id a ``customer_search`` result in this turn matched, if any.

    The resolver encodes its matches in the tool message's ``DATA:`` block; this reads the first
    match's id back so the reading tools can be scoped to it, exactly as a live model would carry
    the id forward from the search result it just read.
    """
    for message in messages:
        if message.role != "tool" or message.name != _SEARCH_TOOL or not message.content:
            continue
        if "DATA:\n" not in message.content:
            continue
        try:
            payload = json.loads(message.content.split("DATA:\n", 1)[1])
        except json.JSONDecodeError:
            continue
        matches = payload.get("matches") if isinstance(payload, dict) else None
        if isinstance(matches, list) and matches:
            first = matches[0]
            if isinstance(first, dict):
                cid = first.get("customer_id")
                if isinstance(cid, str):
                    return cid
    return None


def _latest_user_text(messages: Sequence[QaMessage]) -> str:
    for message in reversed(messages):
        if message.role == "user":
            return message.content
    return ""


def _customer_id_hint(request: ConverseRequest) -> str | None:
    """The customer id the loop stamped into the system framing, if any (``customer_id=<id>``)."""
    match = re.search(r"customer_id=([A-Za-z0-9\-_]+)", request.system)
    return match.group(1) if match else None


def _knowledge_query(question: str) -> str:
    """A non-identifying retrieval query: the question with any ``customer_id=`` hint stripped."""
    return re.sub(r"customer_id=[A-Za-z0-9\-_]+", "", question).strip() or question.strip()


def _tool_name(schema: dict[str, object]) -> str:
    function = schema.get("function")
    if isinstance(function, dict):
        name = function.get("name")
        if isinstance(name, str):
            return name
    return ""


def _estimate(text: str) -> int:
    return max(1, len(text) // 4)


__all__ = ["mock_converse"]
