"""``C360Aggregator`` — concurrent composition with per-domain isolation (task 5.7, design §5.7).

The 360 view is every module's read at once. Fetching them serially would add their latencies; the
SQLite driver is blocking, so the honest concurrency model is a thread pool over the bounded
read-only connection pool (design §6.1) — each module's reads run on a worker thread, the pool (not
the aggregator) bounds how many connections are live at once.

Per-domain failure isolation (requirement 4.4, 13.6)
----------------------------------------------------

A dashboard must not go dark because one module failed. Each module runs in its own future; a module
that raises is caught, recorded as a :class:`~c360.api.envelope.ModuleError`, and its slot in the
result is left ``None``. The response is a partial 200 carrying ``meta.errors[]`` rather than a
total
failure, so a risk-service outage still shows the profile, holdings and offers. Authorization and
the
"does the customer exist" decision happen *before* the fan-out (design §7.2), so a 403 or 404 is a
whole-request outcome; only genuine per-module read failures degrade to a partial.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from c360.api.envelope import ErrorCode, ModuleError
from c360.core.logging import get_logger

if TYPE_CHECKING:
    from c360.domain.models import ContactInfo, Customer, FinancialProfile, Household
    from c360.security.model import Principal
    from c360.services.customer import CustomerService
    from c360.services.financial import ExpenseAnalytics, FinancialService
    from c360.services.journey import JourneyService, TimelineEntry
    from c360.services.offer import OfferService, RankedOffers
    from c360.services.relationship import HouseholdRollup, RelationshipService
    from c360.services.risk import RiskService, RiskView

_logger = get_logger(__name__)


@dataclass(slots=True)
class Customer360:
    """The composed 360 read model (design §4, requirement 4.1).

    Every module is optional: a slot is ``None`` when that module failed, and the failure is in
    ``errors``. The profile is the one exception a caller can rely on being present, because the
    request is rejected before the fan-out if the customer cannot be read at all.
    """

    customer_id: str
    profile: Customer | None = None
    contact: ContactInfo | None = None
    household: Household | None = None
    household_rollup: HouseholdRollup | None = None
    financial_profile: FinancialProfile | None = None
    expense_analytics: ExpenseAnalytics | None = None
    risk: RiskView | None = None
    offers: RankedOffers | None = None
    timeline: tuple[TimelineEntry, ...] | None = None
    errors: list[ModuleError] = field(default_factory=list)


class C360Aggregator:
    """Fans out the module reads concurrently and composes a partial-tolerant 360 view."""

    __slots__ = (
        "_customer",
        "_financial",
        "_journey",
        "_max_workers",
        "_offer",
        "_relationship",
        "_risk",
    )

    def __init__(
        self,
        *,
        customer: CustomerService,
        financial: FinancialService,
        relationship: RelationshipService,
        risk: RiskService,
        offer: OfferService,
        journey: JourneyService,
        max_workers: int = 8,
    ) -> None:
        self._customer = customer
        self._financial = financial
        self._relationship = relationship
        self._risk = risk
        self._offer = offer
        self._journey = journey
        self._max_workers = max_workers

    def build_360(self, principal: Principal, customer_id: str) -> Customer360:
        """Compose the 360 view, isolating each module's failure into ``errors`` (requirement 4.4).

        Assumes authorization has already passed for ``customer_id``: this is called after the route
        has run :func:`c360.security.authorization.authorize_customer`, so a module raising here is
        a
        read failure, not an entitlement decision. Entitlement scoping is applied *inside* each
        service, so nothing here branches on ``principal.entitlement`` — it is passed only to the
        services that need it.
        """
        del principal  # entitlement is applied inside the services; documented, not used here.
        result = Customer360(customer_id=customer_id)

        # Each spec pairs the ``Customer360`` slot to fill with the thunk that produces it and the
        # module name a failure is recorded against (several slots share a module — financial owns
        # both the profile and the analytics — so the name is explicit rather than the slot).
        specs: tuple[_ModuleSpec, ...] = (
            _ModuleSpec("profile", "profile", lambda: self._customer.get_profile(customer_id)),
            _ModuleSpec("contact", "contact", lambda: self._customer.get_contact(customer_id)),
            _ModuleSpec(
                "household", "household", lambda: self._relationship.get_household(customer_id)
            ),
            _ModuleSpec(
                "household_rollup",
                "household",
                lambda: self._relationship.get_household_rollup(customer_id),
            ),
            _ModuleSpec(
                "financial_profile",
                "financial",
                lambda: self._financial.get_financial_profile(customer_id),
            ),
            _ModuleSpec(
                "expense_analytics",
                "financial",
                lambda: self._financial.get_expense_analytics(customer_id),
            ),
            _ModuleSpec("risk", "risk", lambda: self._risk.get_risk(customer_id)),
            _ModuleSpec("offers", "offers", lambda: self._offer.get_offers(customer_id)),
            _ModuleSpec(
                "timeline", "journey", lambda: tuple(self._journey.get_timeline(customer_id))
            ),
        )

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = {spec.slot: pool.submit(_guard(spec)) for spec in specs}
            for spec in specs:
                value, error = futures[spec.slot].result()
                if error is not None:
                    result.errors.append(error)
                    continue
                setattr(result, spec.slot, value)

        return result


@dataclass(frozen=True, slots=True)
class _ModuleSpec:
    """One module's contribution to the 360 view: which slot it fills and how."""

    slot: str
    module: str
    thunk: Callable[[], object]


def _guard(spec: _ModuleSpec) -> Callable[[], tuple[object, ModuleError | None]]:
    """Wrap a module thunk so a raise becomes a recorded :class:`ModuleError`, never a crash.

    The error message is a fixed, value-free string: an exception message can carry a bound value or
    a customer detail, and ``meta.errors[]`` is serialized to the client and to logs (requirement
    18.8). The real cause goes to the redacted log with the module name only.
    """

    def _run() -> tuple[object, ModuleError | None]:
        try:
            return spec.thunk(), None
        except Exception as exc:  # per-domain isolation is the whole point here
            _logger.warning(
                "module read failed",
                extra={"failed_module": spec.module, "error_type": type(exc).__name__},
            )
            return None, ModuleError(
                module=spec.module,
                code=ErrorCode.UPSTREAM_UNAVAILABLE,
                message=f"the {spec.module} module could not be loaded",
            )

    return _run


__all__ = ["C360Aggregator", "Customer360"]
