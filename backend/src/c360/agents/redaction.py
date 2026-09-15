"""``PromptRedactor`` — data minimisation before any model call (task 8.4, design §8.6).

Nothing reaches a model that the agent's reviewed, versioned prompt did not explicitly permit. The
redactor sits between ``load_context`` and the provider and does three things, in order:

1. **Allowlist filtering.** A fact survives only if its ``field`` is on the agent's declared
   ``field_allowlist`` (task 8.3). Everything else is dropped before dispatch, so an agent that
   never declared ``date_of_birth`` cannot receive it even if a tool happened to wrap it.
2. **A hard never-dispatch floor.** Independently of any allowlist, a small set of fields — full
   PAN, full account number, VIN, date of birth, street address — is *always* removed. A prompt
   author cannot opt into dispatching them by listing them; the floor is the backstop for a mistake
   in a prompt file.
3. **Identifier pseudonymisation.** The values that *do* survive have their direct identifiers
   replaced with per-session pseudonyms (``CUST_A``, ``ACCT_1``): a customer id becomes ``CUST_A``,
   an account id ``ACCT_1``. The model reasons over pseudonyms and cites facts by their ``F``-ids,
   which are unchanged; on the way back, :meth:`rehydrate` restores the real identifiers in the
   narrative so the UI shows the customer, not ``CUST_A``.

The surviving field list is recorded as the ``prompt_field_manifest`` (design §8.6, audit column),
so what was sent to the model is auditable without the values themselves ever being logged.

The same instance also redacts *retrieval queries* (:meth:`redact_query`) by delegating to the Phase
7 scrubber, so customer identity never reaches the embedding model either (design §9.4).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from c360.knowledge.redaction import redact_query as _scrub_query
from c360.tools.facts import FactTable

if TYPE_CHECKING:
    from c360.tools.facts import Fact

#: Fields that must never be dispatched to a model regardless of any prompt's allowlist (design
#: §8.6). Matched against a fact's ``field`` name. This is the floor, not the policy — the policy is
#: the per-agent allowlist; this is the backstop that a prompt-file mistake cannot defeat.
NEVER_DISPATCH_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "pan",
        "card_number",
        "full_pan",
        "account_number",
        "vin",
        "date_of_birth",
        "dob",
        "address_line1",
        "address_line2",
        "street_address",
        "ssn",
        "tax_id",
    }
)

#: Entity types whose ``entity_id`` is a direct identifier and is pseudonymised before dispatch. A
#: type not listed here keeps its id (``household`` ids are structural, not identifying on their
#: own), but its identifying *fields* are still governed by the allowlist and the floor.
_PSEUDONYM_PREFIX: Final[dict[str, str]] = {
    "customer": "CUST",
    "account": "ACCT",
}

_ALPHABET: Final = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


class RedactedContext:
    """The output of one redaction: the dispatchable fact table, the manifest, and the id map.

    ``facts`` is what actually goes to the model. ``field_manifest`` is the sorted list of field
    names that survived, for the audit record. ``rehydrate`` reverses the pseudonymisation on the
    model's output.
    """

    __slots__ = ("_reverse", "facts", "field_manifest")

    def __init__(
        self, facts: FactTable, field_manifest: tuple[str, ...], reverse: dict[str, str]
    ) -> None:
        self.facts = facts
        self.field_manifest = field_manifest
        self._reverse = reverse

    def rehydrate(self, text: str) -> str:
        """Replace every pseudonym in ``text`` with the real identifier it stood for.

        Longest pseudonym first so ``CUST_AA`` is not partially rewritten by the rule for
        ``CUST_A``. A narrative that cites only ``F``-ids and never echoes a pseudonym is returned
        unchanged, which is the common case.
        """
        for pseudonym in sorted(self._reverse, key=len, reverse=True):
            text = text.replace(pseudonym, self._reverse[pseudonym])
        return text


class PromptRedactor:
    """Applies allowlist + floor + pseudonymisation to a fact table for one agent (task 8.4).

    Stateful across a single dashboard run: the pseudonym map is built as ids are first seen and
    shared across every agent's redaction in that run, so the same customer is ``CUST_A`` in every
    card and re-hydration is consistent. Not thread-safe by construction — one redactor per graph
    invocation, which is how the graph uses it.
    """

    __slots__ = ("_counters", "_forward", "_reverse")

    def __init__(self) -> None:
        self._forward: dict[str, str] = {}
        self._reverse: dict[str, str] = {}
        self._counters: dict[str, int] = {}

    def redact(self, facts: FactTable, *, allowlist: frozenset[str]) -> RedactedContext:
        """Filter ``facts`` to ``allowlist`` minus the floor, pseudonymise ids, build manifest."""
        surviving: list[Fact] = []
        manifest: set[str] = set()
        for fact in facts.facts:
            if fact.field in NEVER_DISPATCH_FIELDS:
                continue
            if fact.field not in allowlist:
                continue
            surviving.append(self._pseudonymise(fact))
            manifest.add(fact.field)
        return RedactedContext(
            facts=FactTable(facts=tuple(surviving)),
            field_manifest=tuple(sorted(manifest)),
            reverse=dict(self._reverse),
        )

    def redact_query(self, query: str) -> str:
        """Scrub a retrieval query of PII before it is embedded (design §9.4, requirement 17.9)."""
        return _scrub_query(query)

    def rehydrate(self, text: str) -> str:
        """Restore real identifiers in a model output using the run's accumulated pseudonym map."""
        for pseudonym in sorted(self._reverse, key=len, reverse=True):
            text = text.replace(pseudonym, self._reverse[pseudonym])
        return text

    def _pseudonymise(self, fact: Fact) -> Fact:
        """Return ``fact`` with its ``entity_id`` replaced by a stable per-session pseudonym.

        The pseudonym is minted once per real id and reused, so the mapping is a bijection and
        re-hydration is unambiguous. Fields, values and ids that are not identifying entity ids are
        untouched — a balance value is data the allowlist already vetted, not an identifier.
        """
        prefix = _PSEUDONYM_PREFIX.get(fact.entity_type)
        if prefix is None:
            return fact
        pseudonym = self._pseudonym_for(prefix, fact.entity_id)
        return fact.model_copy(update={"entity_id": pseudonym})

    def _pseudonym_for(self, prefix: str, real_id: str) -> str:
        key = f"{prefix}:{real_id}"
        existing = self._forward.get(key)
        if existing is not None:
            return existing
        index = self._counters.get(prefix, 0)
        self._counters[prefix] = index + 1
        pseudonym = f"{prefix}_{_label(prefix, index)}"
        self._forward[key] = pseudonym
        self._reverse[pseudonym] = real_id
        return pseudonym


def _label(prefix: str, index: int) -> str:
    """The suffix for the ``index``-th id of a prefix: letters for customers, numbers for accounts.

    ``CUST_A``, ``CUST_B`` matches design §8.6's example for customers; accounts use ``ACCT_1``,
    ``ACCT_2``. Customer letters roll over to ``AA`` past 26 so the scheme never runs out.
    """
    if prefix == "CUST":
        letters = ""
        n = index
        while True:
            letters = _ALPHABET[n % 26] + letters
            n = n // 26 - 1
            if n < 0:
                break
        return letters
    return str(index + 1)


__all__ = [
    "NEVER_DISPATCH_FIELDS",
    "PromptRedactor",
    "RedactedContext",
]
