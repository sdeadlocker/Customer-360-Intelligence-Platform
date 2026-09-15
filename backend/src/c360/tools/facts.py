"""The fact-wrapping layer (task 6.1, requirement 10.8).

A tool does not return a bare number. It returns a value bound to *where it came from*: which entity
it describes, which field of that entity it is, and the as-of date the row carried. That binding is
a :class:`Fact`, and a tool call returns a :class:`FactTable` of them with stable numbered ids.

Why this shape, and why it is not optional
------------------------------------------

Requirement 10.8 asks every narrative figure to be traceable to a source fact, and Phase 8's claim
validator makes that a hard gate: a numeric claim in a model's output is rejected unless it matches
a fact within tolerance. That check needs three things this module supplies —

* an *addressable* fact (the ``F1``, ``F2`` ids), so a citation is a reference rather than a
  repeated value that could drift from its source;
* the *provenance triple* ``(entity_type, entity_id, field)``, so two customers' balances are never
  confused and a field name in a prompt resolves back to exactly one fact;
* the *as-of date*, because a figure is only correct relative to when it was true, and design §4.3
  makes ``(as_of_date, source_system)`` the provenance every read carries.

The fact id is assigned by insertion order and is stable *within one table*: ``F1`` is the first
fact added. It is deliberately not a hash of the value — a citation must survive the value being
masked downstream, and a hash would not. Stability across calls is neither promised nor needed: a
fact table is built fresh per tool invocation and consumed within the same agent turn.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# The provenance a :class:`~c360.domain.models.SourcedModel` (or a model exposing the same two
# attributes via properties) carries. Named here so the extractor in this module and the tool layer
# agree on what "has provenance" means without importing the domain models.
_AS_OF_ATTR = "as_of_date"
_SOURCE_ATTR = "source_system"


class FactRef(BaseModel):
    """A resolvable reference to a fact by its table id (``F1``, ``F2``, ...).

    A citation in an agent's output is a :class:`FactRef`, not a copied value: the value lives in
    the :class:`FactTable` the agent was given, and the reference is what a validator resolves.
    """

    model_config = ConfigDict(frozen=True)

    fact_id: str = Field(pattern=r"^F\d+$")


class Fact(BaseModel):
    """One provenance-bound value returned by a tool (requirement 10.8).

    ``value`` is JSON-scalar — a string, int, float, bool or ``None``. A monetary value stays an
    integer number of cents here (design §1.1); it becomes decimal only at the API boundary, never
    in a fact a model reasons over. ``entity_type`` / ``entity_id`` / ``field`` are the provenance
    triple a citation resolves against; ``as_of`` is the ISO date the source row was current as of.
    """

    model_config = ConfigDict(frozen=True)

    fact_id: str = Field(pattern=r"^F\d+$")
    entity_type: str
    entity_id: str
    field: str
    value: str | int | float | bool | None
    as_of: str | None = None
    #: The source system the row came from, when the model carried it. Advisory provenance, not a
    #: citation key — a claim resolves against ``(entity_type, entity_id, field)``, not this.
    source_system: str | None = None

    @property
    def ref(self) -> FactRef:
        """A :class:`FactRef` pointing at this fact."""
        return FactRef(fact_id=self.fact_id)


def _jsonable_scalar(value: Any) -> str | int | float | bool | None:
    """Coerce a leaf value to a JSON scalar, matching how the API serializes the same types.

    An ``Enum`` becomes its value, a ``date``/``datetime`` its ISO string. ``Cents``/``Bps`` are
    ``int`` subclasses, so they arrive here already integral and stay ``int`` — the fact layer
    never converts money to decimal (design §1.1). Anything not a JSON scalar is stringified rather
    than dropped, so a fact is never silently lost, but that path is not expected on the wrapped
    fields the tools declare.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        coerced = value.value
        return coerced if isinstance(coerced, (str, int, float, bool)) else str(coerced)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _provenance(source: object) -> tuple[str | None, str | None]:
    """The ``(as_of, source_system)`` a model carries, or ``(None, None)`` if it carries neither.

    Reads the attributes rather than isinstance-checking ``SourcedModel`` so a model that exposes
    the pair through ``@property`` (``Holding``, ``OfferForCustomer``, ``AssetHolding``) is handled
    the same as one that stores them as fields.
    """
    as_of_raw = getattr(source, _AS_OF_ATTR, None)
    source_system = getattr(source, _SOURCE_ATTR, None)
    as_of = as_of_raw.isoformat() if isinstance(as_of_raw, (datetime, date)) else as_of_raw
    return as_of, (source_system if isinstance(source_system, str) else None)


class FactTable(BaseModel):
    """An ordered, id-stable collection of facts returned by a tool (task 6.1).

    Built through :meth:`builder`; the builder assigns ``F1``, ``F2``, ... in insertion order and is
    the only way ids are minted, so a fact's id is a pure function of when it was added and nothing
    else. The table is immutable once built.
    """

    model_config = ConfigDict(frozen=True)

    facts: tuple[Fact, ...] = ()

    @classmethod
    def builder(cls) -> FactTableBuilder:
        """A fresh builder that mints ``F1``, ``F2``, ... in the order facts are added."""
        return FactTableBuilder()

    def by_id(self, fact_id: str) -> Fact | None:
        """The fact with ``fact_id``, or ``None``. Used by a citation resolver / claim validator."""
        for fact in self.facts:
            if fact.fact_id == fact_id:
                return fact
        return None

    def __len__(self) -> int:
        return len(self.facts)


class FactTableBuilder:
    """Accumulates facts and assigns stable numbered ids (task 6.1).

    Not a Pydantic model: it is mutable working state, discarded once :meth:`build` snapshots it
    into an immutable :class:`FactTable`. One builder per tool call.
    """

    __slots__ = ("_facts",)

    def __init__(self) -> None:
        self._facts: list[Fact] = []

    def add(
        self,
        *,
        entity_type: str,
        entity_id: str,
        field: str,
        value: Any,
        as_of: str | None = None,
        source_system: str | None = None,
    ) -> Fact:
        """Add one fact, minting the next id, and return it (so a caller can cite it at once)."""
        fact = Fact(
            fact_id=f"F{len(self._facts) + 1}",
            entity_type=entity_type,
            entity_id=entity_id,
            field=field,
            value=_jsonable_scalar(value),
            as_of=as_of,
            source_system=source_system,
        )
        self._facts.append(fact)
        return fact

    def add_model_fields(
        self,
        model: BaseModel,
        *,
        entity_type: str,
        entity_id: str,
        fields: tuple[str, ...],
    ) -> tuple[Fact, ...]:
        """Wrap named fields of a domain ``model`` as facts, inheriting the model's provenance.

        ``as_of`` and ``source_system`` are read once from the model (via :func:`_provenance`) and
        applied to every field, because a model's fields share the row's provenance. A field whose
        value is ``None`` is still wrapped — "no value as of this date" is itself a fact a narrative
        may need to state, and dropping it would make the absence indistinguishable from a field the
        tool never read.
        """
        as_of, source_system = _provenance(model)
        added: list[Fact] = []
        for field in fields:
            added.append(
                self.add(
                    entity_type=entity_type,
                    entity_id=entity_id,
                    field=field,
                    value=getattr(model, field, None),
                    as_of=as_of,
                    source_system=source_system,
                )
            )
        return tuple(added)

    def build(self) -> FactTable:
        """Snapshot the accumulated facts into an immutable table."""
        return FactTable(facts=tuple(self._facts))


# Rebuild now that the builder it forward-references is defined.
FactTable.model_rebuild()


__all__ = ["Fact", "FactRef", "FactTable", "FactTableBuilder"]
