"""``OfferService`` — ranking, suppression and cooling-off (task 5.5, design §6.1).

The repository returns offers joined to the customer's reaction, including the suppression flag and
reason the generator recorded. The service adds the requirement 9.2-9.6 business rules the
repository
deliberately leaves to it:

* **Expected-value ranking (9.2).** Offers are ranked by ``expected_value_cents`` descending, with a
  rationale attached — the affinity basis and the probability that produced the ranking — so a
  ranked list is explainable rather than a bare order.
* **Duplicate-product suppression (9.5).** A suppressed offer is *kept* in the response, marked and
  carrying its reason, and sorted after the live offers so it renders de-emphasized rather than
  vanishing. Dropping it would make the requirement's "show why" unimplementable above this layer.
* **Cooling-off (9.6).** An offer the customer declined within the configured window is suppressed
  for that window with a cooling-off reason, computed against the response's as-of date so the same
  data reads consistently regardless of when it is fetched.
* **Cross-sell vs upsell (9.3).** Carried straight from the offer's type, with the affinity basis,
so
  the two are distinguished with their product-affinity reasoning intact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

from c360.domain.enums import CustomerReaction, OfferType

if TYPE_CHECKING:
    from collections.abc import Sequence

    from c360.data.repositories.offer import SqliteOfferRepository
    from c360.domain.models import Campaign, OfferForCustomer

#: Reactions that start a cooling-off window (requirement 9.6). A decline is the signal; a pending
#: or
#: viewed offer is not "declined" and does not cool off.
_COOLED_REACTIONS = frozenset({CustomerReaction.DECLINED})


@dataclass(frozen=True, slots=True)
class RankedOffer:
    """An offer with its ranking rationale (requirement 9.2, 9.3).

    ``expected_value_cents`` is the key the list was ranked by; ``rationale`` is the short,
    value-free explanation (affinity basis plus probability band) a UI shows beside the rank.
    ``is_cross_sell`` / ``is_upsell`` distinguish the two per requirement 9.3. ``suppressed`` and
    ``suppression_reason`` carry a kept-but-de-emphasized offer's status (requirement 9.5).
    """

    offer: OfferForCustomer
    rank: int
    expected_value_cents: int
    is_cross_sell: bool
    is_upsell: bool
    rationale: str
    suppressed: bool
    suppression_reason: str | None


@dataclass(frozen=True, slots=True)
class RankedOffers:
    """The ranked offer list plus the campaigns the customer belongs to (requirement 9.7)."""

    offers: tuple[RankedOffer, ...]
    campaigns: tuple[Campaign, ...]


class OfferService:
    """Offer ranking, suppression and campaign reads over the offer repository."""

    __slots__ = ("_cooling_off_days", "_repository")

    def __init__(self, repository: SqliteOfferRepository, *, cooling_off_days: int) -> None:
        if cooling_off_days < 0:
            raise ValueError(f"cooling_off_days must be non-negative, got {cooling_off_days}")
        self._repository = repository
        self._cooling_off_days = cooling_off_days

    def get_offers(self, customer_id: str, *, as_of: date | None = None) -> RankedOffers:
        """The customer's offers, ranked, with suppression and cooling-off applied (req 9.1-9.7)."""
        as_of = as_of or date.today()
        raw = self._repository.list_offers_for_customer(customer_id, include_suppressed=True)
        ranked = self._rank(raw, as_of)
        campaigns = tuple(self._repository.list_campaigns_for_customer(customer_id))
        return RankedOffers(offers=ranked, campaigns=campaigns)

    def get_next_best_offer(
        self, customer_id: str, *, as_of: date | None = None
    ) -> RankedOffer | None:
        """The highest-ranked live (non-suppressed) offer, or ``None`` if there is none (req
        9.2)."""
        ranked = self.get_offers(customer_id, as_of=as_of).offers
        for offer in ranked:
            if not offer.suppressed:
                return offer
        return None

    def _rank(self, offers: Sequence[OfferForCustomer], as_of: date) -> tuple[RankedOffer, ...]:
        """Rank live offers by expected value; keep suppressed ones after, de-emphasized (9.5)."""
        prepared = [self._prepare(offer, as_of) for offer in offers]
        # Live offers first (suppressed sort last), then by expected value descending, with the
        # offer id as a stable tiebreaker so the order is reproducible.
        prepared.sort(
            key=lambda item: (
                item[0],  # suppressed flag: False (0) sorts before True (1)
                -item[1],  # expected value descending
                item[2].offer.offer_id,
            )
        )
        ranked: list[RankedOffer] = []
        for position, (suppressed, ev, offer, reason) in enumerate(prepared, start=1):
            offer_type = offer.offer.offer_type
            ranked.append(
                RankedOffer(
                    offer=offer,
                    rank=position,
                    expected_value_cents=ev,
                    is_cross_sell=offer_type is OfferType.CROSS_SELL,
                    is_upsell=offer_type is OfferType.UPSELL,
                    rationale=self._rationale(offer),
                    suppressed=suppressed,
                    suppression_reason=reason,
                )
            )
        return tuple(ranked)

    def _prepare(
        self, offer: OfferForCustomer, as_of: date
    ) -> tuple[bool, int, OfferForCustomer, str | None]:
        """Decide suppression (recorded or cooling-off) and the expected value for sorting."""
        customer_offer = offer.customer_offer
        ev = int(customer_offer.expected_value_cents or 0)

        if customer_offer.is_suppressed:
            return True, ev, offer, customer_offer.suppression_reason

        cooled_reason = self._cooling_off_reason(offer, as_of)
        if cooled_reason is not None:
            return True, ev, offer, cooled_reason

        return False, ev, offer, None

    def _cooling_off_reason(self, offer: OfferForCustomer, as_of: date) -> str | None:
        """A cooling-off suppression reason if the offer was declined within the window (9.6)."""
        customer_offer = offer.customer_offer
        if customer_offer.customer_reaction not in _COOLED_REACTIONS:
            return None
        reaction_date = customer_offer.reaction_date
        if reaction_date is None:
            return None
        window_end = reaction_date + timedelta(days=self._cooling_off_days)
        if as_of <= window_end:
            return (
                f"COOLING_OFF: declined on {reaction_date.isoformat()}, "
                f"suppressed until {window_end.isoformat()}"
            )
        return None

    def _rationale(self, offer: OfferForCustomer) -> str:
        """A short, value-free ranking rationale (requirement 9.2)."""
        parts: list[str] = []
        basis = offer.customer_offer.affinity_basis
        if basis:
            parts.append(basis)
        confidence = offer.customer_offer.probability_confidence
        if confidence is not None:
            parts.append(f"{confidence.value.lower()} confidence")
        return "; ".join(parts) if parts else "ranked by expected value"


__all__ = ["OfferService", "RankedOffer", "RankedOffers"]
