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
from typing import TYPE_CHECKING, Any

from c360.agents.provider import TokenUsage
from c360.agents.qa_types import ConverseRequest, ConverseResponse, QaMessage, QaRoute, QaToolCall
from c360.agents.qa_wire import (
    current_turn,
    decode_facts,
    decode_passages,
    decode_tool_data,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


#: Keyword → customer tool name. Ordered by specificity so "credit card balance" prefers holdings
#: over a bare "credit" match; the first keyword found in the question that also names an offered
#: tool wins. Deliberately small — the mock only needs to exercise the loop, not to route perfectly.
_KEYWORD_TOOLS: tuple[tuple[str, str], ...] = (
    ("pitch", "pitch"),
    ("talking point", "pitch"),
    ("what to tell", "pitch"),
    ("what should i tell", "pitch"),
    ("what do i tell", "pitch"),
    ("how to approach", "pitch"),
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
_COHORT_TOOL = "customer_cohort"
_PITCH_TOOL = "pitch"

#: Phrases that mark a question as being about a *group* of customers rather than one named one.
#: Matched against the lowercased question. Deliberately plural/collective — "customers", "clients",
#: "book", "cohort", "portfolio", "who is/are" — so a single-customer question ("what is Jane's
#: risk?") does not trip it while "high risk customers" / "who is past due" does.
_COHORT_SIGNALS: tuple[str, ...] = (
    "customers",
    "clients",
    "cohort",
    "segment",
    "list of",
    "which of",
    "which customer",
    "which client",
    "who is",
    "who are",
    "whom should",
    "who should i",
    "how many",
    "my book",
    "the book",
    "portfolio of",
    "max profit",
    "most profit",
    "most valuable",
    "highest value",
    "best offer",
    "best to pitch",
    "anyone",
    "everyone",
    "all the",
)


def mock_converse(request: ConverseRequest, *, model_id: str) -> ConverseResponse:
    """Produce one deterministic Q&A turn: tool calls first, a grounded answer once results exist.

    Scoped to the current turn (everything from the last user message on) so a checkpointed
    follow-up does not see the prior turn's tool results as already-satisfied.

    On the cross-customer path (no ``customer_id=`` in the system framing) the loop first resolves a
    customer with ``customer_search``; once that result is in the turn the resolved id is threaded
    into the reading tools, mirroring what a live model does when handed the search tool.
    """
    turn = current_turn(request.messages)

    # Small talk — a greeting, a thank-you, or "what can you do" — is answered directly, before any
    # customer resolution. Without this the cross-customer path runs "hi" through customer_search,
    # finds no one, and replies "No matching customer was found", which reads as broken. A live
    # model would just greet; the mock does the same for the handful of conversational openers that
    # are unmistakably not about a customer. Anything substantive still flows to the grounded path.
    if not _has_tool_results(turn):
        small_talk = _small_talk_reply(_latest_user_text(turn), request)
        if small_talk is not None:
            return _plain_answer(small_talk, model_id=model_id)

    # A cohort question ("high risk customers", "my platinum clients", "who is past due") is about
    # a *set*, not a named customer, so it takes the book-level customer_cohort tool rather than the
    # single-customer resolver. Checked before resolution so "high risk customers" does not run
    # through customer_search (which would strip to "high", match nobody, and reply "no match").
    #
    # A follow-up that *refines* a prior cohort ("only high risk", "just the platinum ones", "who
    # of those is past due") is also a cohort turn even though it carries no collective noun: the
    # previous turn established we are talking about a set, so a bare refinement continues it rather
    # than being run through customer_search as if it named a person. This is the mock's stand-in
    # for the conversational memory a live model gets from the full history.
    cohort_offered = _COHORT_TOOL in {_tool_name(schema) for schema in request.tools}
    question = _latest_user_text(turn)
    if not _has_tool_results(turn) and cohort_offered:
        if _is_cohort_question(question):
            return _cohort_turn(request, turn, model_id=model_id)
        if _is_cohort_followup(request.messages, question):
            return _cohort_turn(request, turn, model_id=model_id)

    # A reading tool (anything but the resolver) has already returned — including a cohort list — so
    # the turn has what it needs to answer. Checked before resolution so a cohort result, which is
    # not keyed on a resolved customer, is not mistaken for "still needs a customer" and sent back
    # through customer_search.
    if _has_reading_results(turn):
        return _final_answer(request, turn, model_id=model_id)

    return _resolve_or_read(request, turn, model_id=model_id)


def _resolve_or_read(
    request: ConverseRequest, turn: Sequence[QaMessage], *, model_id: str
) -> ConverseResponse:
    """Decide the single-customer path: resolve first, answer a no-match, or call the reading tools.

    Split out of :func:`mock_converse` so that function's branch count stays readable: this handles
    everything after the small-talk, cohort and already-read short-circuits have been ruled out.
    """
    resolved = _resolved_customer_id(turn)
    if _needs_customer_resolution(request, turn, resolved):
        return _search_turn(request, turn, model_id=model_id)
    # Cross-customer path where the search ran but resolved no one: answer plainly rather than
    # calling a reading tool with no customer id (requirement 11.5, "no matching customer").
    cross_customer = _customer_id_hint(request) is None
    if cross_customer and _search_attempted(turn) and resolved is None:
        return _no_match_answer(model_id=model_id)
    if not request.tools:
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


def _is_cohort_followup(messages: Sequence[QaMessage], question: str) -> bool:
    """Whether this is a refinement of a cohort established earlier in the conversation.

    True when a ``customer_cohort`` tool result already appeared in the history (so the conversation
    is about a set of customers) and the current question carries a cohort filter word — a risk
    term, a segment, a value tier, or a delinquency word — without naming a customer. This lets a
    bare "only high risk" continue the prior cohort instead of being resolved as a name.
    """
    prior_cohort = any(
        message.role == "tool" and message.name == _COHORT_TOOL for message in messages
    )
    if not prior_cohort:
        return False
    lowered = f" {question.lower().strip()} "
    filter_words = (
        _RISK_WORDS
        + _DELINQUENT_WORDS
        + tuple(word for word, _ in _SEGMENT_WORDS)
        + tuple(word for word, _ in _VALUE_WORDS)
        + tuple(word for word, _ in _BAND_WORDS)
    )
    return any(word in lowered for word in filter_words)


def _is_cohort_question(question: str) -> bool:
    """Whether the question is about a *set* of customers rather than one named customer.

    A collective signal ("customers", "clients", "cohort", "who is past due") marks the book-level
    path. Kept a simple keyword test so it stays deterministic and testable without a model; a live
    provider makes the same distinction from the tool descriptions, this just lets the mock do it.
    """
    lowered = f" {question.lower().strip()} "
    return any(signal in lowered for signal in _COHORT_SIGNALS)


#: Risk-band words to the band a cohort question names. The presence of the word anywhere in the
#: question picks the band, so no multi-word phrase is required.
_BAND_WORDS: tuple[tuple[str, str], ...] = (
    ("high risk", "HIGH"),
    ("riskiest", "HIGH"),
    ("elevated", "ELEVATED"),
    ("moderate risk", "MODERATE"),
    ("low risk", "LOW"),
)

#: Segment words to the stored segment value, for a "platinum small-business clients" style ask.
_SEGMENT_WORDS: tuple[tuple[str, str], ...] = (
    ("small business", "SMALL_BUSINESS"),
    ("small-business", "SMALL_BUSINESS"),
    ("mass market", "MASS"),
    ("affluent", "AFFLUENT"),
    ("uhnw", "UHNW"),
    ("ultra high net worth", "UHNW"),
    ("high net worth", "HNW"),
    ("hnw", "HNW"),
)

#: Value-tier words to the stored value value.
_VALUE_WORDS: tuple[tuple[str, str], ...] = (
    ("platinum", "PLATINUM"),
    ("gold", "GOLD"),
    ("silver", "SILVER"),
    ("bronze", "BRONZE"),
)

#: Words that signal a risk question with no explicit band named — treated as "the riskiest",
#: i.e. the elevated-and-above floor, rather than an unfiltered list of the whole book.
_RISK_WORDS: tuple[str, ...] = (
    "risk",
    "risky",
    "rising",
    "increasing",
    "deteriorating",
    "declining",
    "worsening",
    "at risk",
)

#: Words that mean "past due" for the delinquency filter.
_DELINQUENT_WORDS: tuple[str, ...] = (
    "past due",
    "delinquent",
    "delinquency",
    "overdue",
    "behind on",
    "late payment",
)


def _cohort_args(question: str) -> dict[str, object]:
    """Parse a cohort question into ``customer_cohort`` arguments (deterministic keyword mapping).

    A bare "high risk" / "riskiest" maps to ``min_risk_band=ELEVATED`` — the *floor* meaning, "the
    riskiest customers", the top of the distribution — rather than an exact HIGH band, because that
    is what the phrase means to a user and because an exact top band can be legitimately empty. A
    named segment or value tier maps to the matching filter; "past due" maps to ``delinquent_only``.
    An unqualified cohort question ("show me my customers") passes no filters and lists the book.
    """
    lowered = question.lower()
    args: dict[str, object] = {}

    matched_band = False
    for word, band in _BAND_WORDS:
        if word in lowered:
            # "high risk"/"riskiest" mean the riskiest cohort (this band or higher); a specific
            # middle band means exactly that band.
            if band in {"HIGH"}:
                args["min_risk_band"] = "ELEVATED"
            else:
                args["risk_band"] = band
            matched_band = True
            break

    # A risk question that names no explicit band ("rising risk", "increasing risk", "customers at
    # risk") still means the riskiest, not the whole book — default to the elevated-and-above floor
    # rather than returning everyone, which reads as the feature ignoring the question.
    if not matched_band and any(word in lowered for word in _RISK_WORDS):
        args["min_risk_band"] = "ELEVATED"

    segments = [value for word, value in _SEGMENT_WORDS if word in lowered]
    if segments:
        args["segments"] = tuple(dict.fromkeys(segments))
    values = [value for word, value in _VALUE_WORDS if word in lowered]
    if values:
        args["values"] = tuple(dict.fromkeys(values))
    if any(word in lowered for word in _DELINQUENT_WORDS):
        args["delinquent_only"] = True
    return args


def _cohort_turn(
    request: ConverseRequest, turn: Sequence[QaMessage], *, model_id: str
) -> ConverseResponse:
    """First turn of a cohort question: call ``customer_cohort`` with the parsed filters."""
    question = _latest_user_text(turn)
    call = QaToolCall(
        call_id="call_1",
        name=_COHORT_TOOL,
        arguments=_cohort_args(question),
    )
    return ConverseResponse(
        tool_calls=(call,),
        model_id=model_id,
        usage=TokenUsage(input_tokens=_estimate(question), output_tokens=6),
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
    """Render an answer whose figures are copied from returned facts and cited by ``F``-id.

    A cohort or pitch result carries its shape in the tool's ``data`` block rather than as facts, so
    those two are rendered from their structured payload first; everything else falls through to the
    fact-and-passage rendering the reading tools produce.
    """
    question = _latest_user_text(turn)

    cohort_payloads = decode_tool_data(turn, _COHORT_TOOL)
    if cohort_payloads:
        return _plain_answer(_render_cohort(cohort_payloads[0]), model_id=model_id)

    pitch_payloads = decode_tool_data(turn, _PITCH_TOOL)
    if pitch_payloads:
        text = _render_pitch(pitch_payloads[0], decode_facts(turn))
        return ConverseResponse(
            text=text,
            model_id=model_id,
            usage=TokenUsage(input_tokens=_estimate(question), output_tokens=_estimate(text)),
        )

    facts = decode_facts(turn)
    passages = decode_passages(turn)

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


def _render_cohort(data: object) -> str:
    """Render a ``customer_cohort`` result as a readable, ranked member list.

    The cohort rows are display-only (id, name, segment, value, band, delinquency) and carry no
    citeable figures, so this is plain prose framing plus a bullet per member — no ``F``-ids. An
    empty cohort says so explicitly rather than falling through to a "no facts" message, because an
    empty result to a well-formed cohort question ("any UHNW customers past due?") is a real,
    informative answer.
    """
    if not isinstance(data, dict):
        return "I could not read the cohort result."
    members = data.get("members")
    members = members if isinstance(members, list) else []
    criterion = data.get("criterion")
    criterion = criterion if isinstance(criterion, str) else "matching customers"
    count = len(members)

    if count == 0:
        return f"No customers in your book match {criterion}."

    # The header carries no digit: the claim validator rejects any uncited number as a fabrication
    # (validator.py rule 1), and a cohort count is not a fact carried with an F-id. The list itself
    # conveys the size; a live model with the same data may state the count in prose.
    header = f"These customers in your book match {criterion}:"
    lines = [header]
    for member in members:
        if not isinstance(member, dict):
            continue
        lines.append(_cohort_member_line(member))
    return "\n".join(lines)


def _cohort_member_line(member: dict[str, Any]) -> str:
    """One readable line for a cohort member, folding in only the fields that are present."""
    name = member.get("customer_name") or member.get("customer_id") or "Unknown"
    cid = member.get("customer_id", "")
    attributes: list[str] = []
    band = member.get("risk_band")
    if isinstance(band, str) and band:
        attributes.append(f"{band.lower()} risk")
    segment = member.get("customer_segment")
    if isinstance(segment, str) and segment:
        attributes.append(segment)
    value = member.get("customer_value")
    if isinstance(value, str) and value:
        attributes.append(value.lower())
    delinquency = member.get("delinquency_status")
    if isinstance(delinquency, str) and delinquency and delinquency != "CURRENT":
        attributes.append(f"delinquency {delinquency}")
    suffix = f" — {', '.join(attributes)}" if attributes else ""
    return f"- {name} ({cid}){suffix}"


def _render_pitch(data: object, facts: list[dict[str, Any]]) -> str:
    """Render a ``pitch`` result as structured talking points, citing figures by ``F``-id.

    The three sections — recommended offers, financial position, risk and compliance — are laid out
    as headed bullet lists. Figures (offer expected value, balances) come from the fact block and
    are cited by their ``F``-id so the answer passes the same claim validator the live path does;
    the offer names, rationales and value-free risk alert labels are copied from the ``data`` block.
    """
    if not isinstance(data, dict):
        return "I could not read the pitch result."
    facts_by_field = _facts_by_field(facts)
    lines: list[str] = ["Here's a grounded briefing to open the conversation:"]

    lines.extend(_pitch_offer_lines(data.get("offers"), facts))
    lines.extend(_pitch_financial_lines(data.get("financial"), facts_by_field))
    lines.extend(_pitch_risk_lines(data.get("risk")))

    if len(lines) == 1:
        return "I could not assemble talking points for this customer from the available records."
    return "\n".join(lines)


def _pitch_offer_lines(offers_section: object, facts: list[dict[str, Any]]) -> list[str]:
    """The offers block: each recommended offer, with its expected value cited when present."""
    if not isinstance(offers_section, dict):
        return []
    recommended = offers_section.get("recommended")
    if not isinstance(recommended, list) or not recommended:
        return []
    value_by_offer = {
        fact["entity_id"]: fact["fact_id"]
        for fact in facts
        if fact.get("field") == "expected_value_cents"
    }
    lines = ["Recommended offers:"]
    for offer in recommended:
        if not isinstance(offer, dict):
            continue
        name = offer.get("offer_name") or offer.get("offer_id") or "Offer"
        rationale = offer.get("rationale")
        cite = value_by_offer.get(offer.get("offer_id"))
        rationale_text = f" — {rationale}" if isinstance(rationale, str) and rationale else ""
        cite_text = f" [expected value {cite}]" if cite else ""
        lines.append(f"- {name}{rationale_text}{cite_text}")
    return lines


def _pitch_financial_lines(
    financial_section: object, facts_by_field: dict[str, tuple[Any, str]]
) -> list[str]:
    """The financial headline block: each present headline figure, cited by its ``F``-id."""
    if not isinstance(financial_section, dict) or not financial_section:
        return []
    fields = (
        ("net_worth_cents", "Net worth"),
        ("total_deposits_cents", "Deposits"),
        ("total_loans_cents", "Loans"),
        ("monthly_income_cents", "Monthly income"),
    )
    lines: list[str] = []
    for field, label in fields:
        entry = facts_by_field.get(field)
        if field in financial_section and entry is not None:
            # Render the fact's own value, not the masked payload's — the latter may be a domain
            # money type whose repr ("Cents(90000)") is not the bare integer the citation must
            # match. The fact carries the plain int, cited by its F-id.
            value, fact_id = entry
            lines.append(f"- {label}: {value} [{fact_id}]")
    return ["Financial position:", *lines] if lines else []


def _pitch_risk_lines(risk_section: object) -> list[str]:
    """The risk block: the band and any value-free, severity-ordered alerts (compliance first)."""
    if not isinstance(risk_section, dict) or not risk_section:
        return []
    lines = ["Risk and compliance:"]
    band = risk_section.get("band")
    if isinstance(band, str) and band:
        lines.append(f"- Risk band: {band}")
    if risk_section.get("requires_compliance_indicator"):
        lines.append("- Compliance indicator active — review before any outreach.")
    alerts = risk_section.get("alerts")
    if isinstance(alerts, list):
        for alert in alerts:
            detail = alert.get("detail") if isinstance(alert, dict) else None
            # Skip an alert whose label carries a number (e.g. a days-past-due count): that digit
            # has no F-id, and the claim validator rejects any uncited number. A live model can
            # narrate the count from the risk facts; the mock keeps the value-free labels only.
            if isinstance(detail, str) and detail and not any(ch.isdigit() for ch in detail):
                lines.append(f"- {detail}")
    return lines if len(lines) > 1 else []


def _facts_by_field(facts: list[dict[str, Any]]) -> dict[str, tuple[Any, str]]:
    """Map each fact's field to its ``(value, F-id)``, last-wins, for citing a headline figure."""
    return {
        fact["field"]: (fact.get("value"), fact["fact_id"])
        for fact in facts
        if "field" in fact and "fact_id" in fact
    }


def _no_match_answer(*, model_id: str) -> ConverseResponse:
    """The cross-customer reply when ``customer_search`` resolved no one (requirement 11.5)."""
    text = "No matching customer was found for that question."
    return ConverseResponse(
        text=text,
        model_id=model_id,
        usage=TokenUsage(input_tokens=1, output_tokens=_estimate(text)),
    )


def _plain_answer(text: str, *, model_id: str) -> ConverseResponse:
    """A direct answer with no tool calls — used for small talk and capability questions."""
    return ConverseResponse(
        text=text,
        model_id=model_id,
        usage=TokenUsage(input_tokens=1, output_tokens=_estimate(text)),
    )


#: Conversational openers that are unmistakably not a data question. Matched against the whole
#: message (trimmed, lowercased, stripped of trailing punctuation) so only a bare greeting counts —
#: "hi" greets, but "hi, what are Jane's risks?" still flows to the grounded path.
_GREETINGS = frozenset(
    {
        "hi",
        "hii",
        "hey",
        "hello",
        "yo",
        "hola",
        "greetings",
        "good morning",
        "good afternoon",
        "good evening",
        "how are you",
        "how's it going",
        "hows it going",
    }
)

_THANKS = frozenset({"thanks", "thank you", "thankyou", "ty", "cheers", "appreciate it"})

_CAPABILITY_PHRASES = (
    "what can you do",
    "what can you help",
    "how can you help",
    "who are you",
    "what are you",
    "help me",
    "what do you do",
)


def _small_talk_reply(question: str, request: ConverseRequest) -> str | None:
    """A friendly reply for a greeting, thanks or capability question — else ``None``.

    Kept deliberately narrow so it never swallows a real data question: it fires only on an exact
    greeting/thanks match or an explicit capability phrase, and the substantive path handles the
    rest. The capability reply is tailored to whether this is the cross-customer surface or a single
    customer's dashboard, so it advertises what the user can actually ask here.
    """
    normalized = question.strip().lower().rstrip("!.?")
    if normalized == "":
        return None
    # These replies deliberately contain no digits: the claim validator rejects any uncited number
    # as a fabrication (validator.py rule 1), and "Customer 360" would trip it. Spell the product
    # name without the numeral.
    if normalized in _GREETINGS:
        return (
            "Hello. I'm your Customer intelligence assistant. Ask me about a customer's "
            "finances, risk, relationships, offers or product eligibility, and I'll answer "
            "from their records with the sources to back it up."
        )
    if normalized in _THANKS:
        return "You're welcome. Anything else you'd like to look into?"
    if any(phrase in normalized for phrase in _CAPABILITY_PHRASES):
        cross_customer = _customer_id_hint(request) is None
        scope = (
            "any customer you're entitled to — name them in your question"
            if cross_customer
            else "this customer"
        )
        return (
            f"I'm your Customer intelligence assistant. I can answer questions about {scope}: "
            "their financial position and net worth, spending, risk and compliance flags, "
            "relationships and household, recommended offers, and product eligibility from our "
            "policies. Every answer is grounded in the records, with citations you can open. "
            "What would you like to know?"
        )
    return None


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
