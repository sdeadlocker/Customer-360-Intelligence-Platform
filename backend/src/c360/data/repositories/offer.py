"""SQLite adapter for :class:`~c360.domain.ports.OfferRepository` (task 1.6).

Assembled from three statements rather than one three-way join, for the same reason holdings are:
``offer``, ``customer_offer`` and ``campaign`` share column names (``as_of_date``, ``start_date``,
``end_date``, ``business_group``), so a single join would need most columns aliased and then
unpicked
in Python. Three reads with clean projections are bounded and directly checkable against the models.

Expected-value ranking, duplicate-product suppression and the cooling-off window are *not* here.
Requirement 9.2's ranking needs the customer's holdings to decide what duplicates an existing
product,
and 9.6's window needs configuration; both are :class:`OfferService`'s job in Phase 5. This layer
returns what is recorded, including the suppression flag and reason the generator wrote.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from c360.data.repositories.base import (
    SqliteRepository,
    Statement,
    optional_model,
    to_models,
)
from c360.domain.models import Campaign, CustomerOffer, Offer, OfferForCustomer

if TYPE_CHECKING:
    from collections.abc import Sequence

_CUSTOMER_OFFERS = Statement(
    id="offer.customer_offers",
    collection="customer_offer",
    # `:include_suppressed` is bound rather than branching the SQL, so there is one statement and
    # one
    # plan. Requirement 9.5 keeps suppressed offers visible, so True is the normal case.
    sql="""
    SELECT co.customer_offer_id, co.customer_id, co.offer_id, co.event_date, co.customer_reaction,
           co.reaction_date, co.acceptance_probability, co.probability_confidence,
           co.expected_value_cents, co.affinity_basis, co.is_suppressed, co.suppression_reason,
           co.channel, co.as_of_date, c.source_system
    FROM customer_offer co
    JOIN customer c ON c.customer_id = co.customer_id
    WHERE co.customer_id = :customer_id
      AND (:include_suppressed = 1 OR co.is_suppressed = 0)
    ORDER BY co.event_date DESC, co.customer_offer_id
    """,
)

_OFFERS_FOR_CUSTOMER = Statement(
    id="offer.offers_for_customer",
    collection="offer",
    # Driven off `customer_offer` rather than taking an ID list, which keeps the statement static: a
    # generated `IN` list would change the SQL text with every different number of offers.
    sql="""
    SELECT o.offer_id, o.offer_name, o.business_group, o.offer_type, o.product_code, o.product_type,
           o.value_cents, o.start_date, o.end_date, o.offer_status, o.campaign_id, o.as_of_date,
           'CAMPAIGN' AS source_system
    FROM offer o
    WHERE o.offer_id IN (SELECT co.offer_id FROM customer_offer co
                         WHERE co.customer_id = :customer_id)
    """,
)

_CAMPAIGN = Statement(
    id="offer.campaign",
    collection="campaign",
    sql="""
    SELECT campaign_id, campaign_name, business_group, channel, start_date, end_date,
           campaign_status, as_of_date, 'CAMPAIGN' AS source_system
    FROM campaign
    WHERE campaign_id = :campaign_id
    """,
)

_CAMPAIGNS_FOR_CUSTOMER = Statement(
    id="offer.campaigns_for_customer",
    collection="campaign",
    sql="""
    SELECT cp.campaign_id, cp.campaign_name, cp.business_group, cp.channel, cp.start_date,
           cp.end_date, cp.campaign_status, cp.as_of_date, 'CAMPAIGN' AS source_system
    FROM campaign cp
    WHERE cp.campaign_id IN (
      SELECT o.campaign_id
      FROM customer_offer co
      JOIN offer o ON o.offer_id = co.offer_id
      WHERE co.customer_id = :customer_id AND o.campaign_id IS NOT NULL
    )
    ORDER BY cp.start_date DESC, cp.campaign_id
    """,
)


class SqliteOfferRepository(SqliteRepository):
    """Reads backing requirements 9.1 and 9.7.

    ``source_system`` for ``offer`` and ``campaign`` is the literal ``'CAMPAIGN'``. Both are
    marketing-owned catalogue rows rather than customer observations, so there is no upstream
    banking
    system to name — and inventing one by joining to a customer would attribute a campaign
    definition
    to a core banking platform that never saw it. Their ``as_of_date`` is real, which is what
    requirement 4.8 needs.
    """

    def list_offers_for_customer(
        self,
        customer_id: str,
        *,
        include_suppressed: bool = True,
    ) -> Sequence[OfferForCustomer]:
        customer_offers = to_models(
            CustomerOffer,
            self.fetch_all(
                _CUSTOMER_OFFERS,
                {
                    "customer_id": customer_id,
                    "include_suppressed": 1 if include_suppressed else 0,
                },
            ),
        )
        if not customer_offers:
            return ()

        offers = {
            offer.offer_id: offer
            for offer in to_models(
                Offer, self.fetch_all(_OFFERS_FOR_CUSTOMER, {"customer_id": customer_id})
            )
        }
        campaigns = {
            campaign.campaign_id: campaign
            for campaign in self.list_campaigns_for_customer(customer_id)
        }

        joined: list[OfferForCustomer] = []
        for customer_offer in customer_offers:
            offer = offers.get(customer_offer.offer_id)
            if offer is None:
                # The foreign key makes this unreachable while the two reads see the same snapshot.
                # Skipping rather than raising keeps one dangling row from emptying the offer
                # module,
                # and the FK plus `PRAGMA foreign_key_check` are what actually prevent it.
                continue
            campaign = campaigns.get(offer.campaign_id) if offer.campaign_id else None
            joined.append(
                OfferForCustomer(offer=offer, customer_offer=customer_offer, campaign=campaign)
            )
        return tuple(joined)

    def get_campaign(self, campaign_id: str) -> Campaign | None:
        return optional_model(Campaign, self.fetch_one(_CAMPAIGN, {"campaign_id": campaign_id}))

    def list_campaigns_for_customer(self, customer_id: str) -> Sequence[Campaign]:
        rows = self.fetch_all(_CAMPAIGNS_FOR_CUSTOMER, {"customer_id": customer_id})
        return to_models(Campaign, rows)
