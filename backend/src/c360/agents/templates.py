"""Jinja narrative templates for the mock provider and the breaker fallback (task 8.1, 8.11).

The mock renders an agent's narrative from its fact table with these templates. The same renderer is
what an open circuit breaker (task 8.11) falls back to when live generation is unavailable, so its
output must be *accurate and cited*, not filler: an advisor reading a degraded card sees the real
figures, just not a model's prose around them.

The rendering contract, which is what makes the output pass the claim validator (task 8.5):

* every figure in the narrative is copied verbatim from a fact and immediately tagged with that
  fact's id, e.g. ``net worth is 4200000 [F3]``. The renderer never computes or invents a number.
* guidance drawn from a passage is tagged with that passage's id (``[P1]``) and states no figure.

A single base template drives all seven agents; a per-agent lead-in line gives each card its voice
without forking the grounding logic seven ways (which is how a template quietly starts fabricating).
Templates are packaged strings rather than files so the mock has no filesystem dependency and cannot
be affected by the operator-editable ``prompts/`` registry, which governs the *live* path only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from jinja2 import Environment, StrictUndefined

if TYPE_CHECKING:
    from c360.agents.provider import CompletionRequest


#: One lead-in sentence per agent, giving the card its voice. An unknown agent falls back to a
#: neutral lead-in rather than raising, so a new agent renders something correct before it gets a
#: bespoke line.
_AGENT_LEADIN: Final[dict[str, str]] = {
    "financial_health": "Financial health summary, grounded in the figures on file:",
    "risk": "Risk assessment, grounded in the risk record on file:",
    "life_event": "Life-event summary, grounded in detected signals:",
    "relationship": "Relationship and household summary:",
    "offer_recommendation": "Recommended offers, ranked by fit:",
    "journey": "Customer journey summary:",
    "customer_summary": "Executive summary of this customer:",
}

_DEFAULT_LEADIN: Final = "Summary, grounded in the figures on file:"

# One fact per line as `<entity_type>.<field> is <value> [<fact_id>]`, so the number and its id
# are inseparable. The `as_of` date is deliberately NOT rendered inline: it is provenance carried on
# the fact, not a claim, and printing it inline would put bare date digits into the narrative that a
# claim validator would (correctly) have no fact to match. Passages are listed by id only — a
# template never quotes a passage's text as a figure. Assembled from short lines so no source line
# exceeds the line-length limit while the rendered output is unchanged.
_FACT_LINE: Final = "- {{ f.entity_type }}.{{ f.field }} is {{ f.value }} [{{ f.fact_id }}]"
_PASSAGE_LINE: Final = "- {{ p.doc_id }} section {{ p.section_path }} [{{ p.passage_id }}]"
_BASE_TEMPLATE: Final = "".join(
    (
        "{{ leadin }}\n",
        "{% for f in facts %}" + _FACT_LINE + "\n{% endfor %}",
        "{% if passages %}\nGuidance from institutional knowledge:\n",
        "{% for p in passages %}" + _PASSAGE_LINE + "\n{% endfor %}{% endif %}",
    )
)


class TemplateRenderer:
    """Renders a :class:`~c360.agents.provider.CompletionRequest` into a grounded narrative.

    Holds one Jinja :class:`~jinja2.Environment` with ``StrictUndefined`` so a template that
    references a field a fact does not carry fails loudly in a test rather than silently emitting an
    empty string that might read as a fabricated absence. Autoescaping is off deliberately: the
    output is plain text for an advisor and a validator, not HTML, and escaping would corrupt the
    figures.
    """

    __slots__ = ("_env", "_template")

    def __init__(self) -> None:
        self._env = Environment(
            undefined=StrictUndefined,
            autoescape=False,  # noqa: S701 - plain-text narrative, not HTML; see docstring
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=False,
        )
        self._template = self._env.from_string(_BASE_TEMPLATE)

    def render(self, request: CompletionRequest) -> str:
        """Render ``request`` deterministically into a fact-cited narrative."""
        leadin = _AGENT_LEADIN.get(request.agent, _DEFAULT_LEADIN)
        return self._template.render(
            leadin=leadin,
            facts=request.facts.facts,
            passages=request.passages,
        ).strip()


__all__ = ["TemplateRenderer"]
