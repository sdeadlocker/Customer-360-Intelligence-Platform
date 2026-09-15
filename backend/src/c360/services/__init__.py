"""Application services (Phase 5, design §6.1).

One service per bounded context, each composed over the repository ports in
:mod:`c360.domain.ports` and holding no per-request state of its own. Services are where the
deterministic business rules live — expected-value ranking, the major-transaction rule, expense
deviation, graph redaction — sitting above the repositories (which only read rows) and below the
REST handlers (which only shape envelopes and apply masking). The tool registry in Phase 6 and the
LangGraph agents in Phase 8 call these same services, so a rule implemented here is enforced
identically whether it is reached over REST, a tool call, or an agent.

Every service method takes plain domain arguments and returns domain models; entitlement and field
masking are applied at the boundary (authorization before the call, the serializer on the response),
never inside a service, so a service is a pure function of its repositories and its configuration.
The one documented exception is graph redaction, which design §5.4 places in the service layer so
the
UI and the Q&A agent share one implementation.
"""

from __future__ import annotations

from c360.services.aggregator import C360Aggregator, Customer360
from c360.services.customer import CustomerService, RecentlyViewedTracker
from c360.services.financial import ExpenseAnalytics, FinancialService
from c360.services.journey import JourneyService, TimelineEntry
from c360.services.offer import OfferService, RankedOffers
from c360.services.relationship import RelationshipService
from c360.services.risk import RiskService, RiskView

__all__ = [
    "C360Aggregator",
    "Customer360",
    "CustomerService",
    "ExpenseAnalytics",
    "FinancialService",
    "JourneyService",
    "OfferService",
    "RankedOffers",
    "RecentlyViewedTracker",
    "RelationshipService",
    "RiskService",
    "RiskView",
    "TimelineEntry",
]
