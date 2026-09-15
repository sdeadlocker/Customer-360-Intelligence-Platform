"""The in-memory plan the generators build before any row is emitted.

Why a plan exists at all
-----------------------

Design §15 requires life events to be seeded **with corroborating transactions** — "a vehicle
purchase gets a down-payment debit and an insurance recurring payment" — so that the Life Event
Agent's inferences are checkable and design §14.3's precision and recall are measurable against
something real rather than against a label with no evidence behind it.

That requirement conflicts with load order. ``life_event`` rows and ``txn`` rows both reference
``customer``, and the transactions have to *contain* the evidence for events that are written later.
Generating strictly in load order would mean the transaction generator could not know which events
it
was supposed to corroborate.

The resolution is two passes. The first pass decides — cohort, identity, income, which life events
happened and when, which products exist — and records the decisions here. The second pass emits rows
in :data:`c360.generator.tables.LOAD_ORDER`, with every generator reading a plan that is already
complete. The transaction generator therefore sees the planned life events, and the life-event
generator can cite transaction identifiers that already exist.

Everything on these types is mutable and filled progressively by stages, which is a deliberate
trade: a frozen plan would need a parallel builder type per stage, and the stages run once, in a
fixed order, inside one function.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from c360.domain.enums import (
    AccountStatus,
    AccountType,
    AssetType,
    CustomerSegment,
    CustomerType,
    CustomerValue,
    DepositProductType,
    HouseholdRole,
    InvestmentRiskProfile,
    LifeEventSource,
    LifeEventType,
    LoanType,
)
from c360.domain.money import Cents
from c360.generator.cohorts import Cohort, CohortProfile


@dataclass(slots=True)
class PlannedLifeEvent:
    """A life event decided in the planning pass.

    ``signals`` is the citation requirement 7.3 needs: an inferred event has to name what it was
    inferred from. The transaction generator appends transaction IDs to it as it emits the
    corroborating debits, so by the time the ``life_event`` row is written the signals point at rows
    that exist.

    The schema pairs ``source = 'INFERRED'`` with ``is_inferred = 1`` and requires both
    ``confidence`` and ``signals`` to be non-NULL in that case (migration ``0003``), so
    :meth:`is_inferred` and the two fields have to stay consistent — which is why ``confidence`` is
    set at planning time for inferred events and left ``None`` otherwise.
    """

    life_event_type: LifeEventType
    event_date: date
    source: LifeEventSource
    confidence: float | None = None
    signals: list[str] = field(default_factory=list)

    @property
    def is_inferred(self) -> bool:
        return self.source is LifeEventSource.INFERRED


@dataclass(slots=True)
class AccountRecord:
    """One product the customer holds, as decided by the product stage.

    Carries the specialization fields for every account type rather than being subclassed, because
    the consumers — the transaction generator, the profile aggregator — switch on ``account_type``
    anyway, and a union type would buy type safety at the cost of a cast at each of those sites.

    ``balance`` follows the sign convention the rest of the platform uses: positive for an asset
    (deposit, investment) and positive for an amount owed (loan principal outstanding, card
    balance).
    The profile aggregator is what assigns them to the asset or liability side; nothing infers it
    from the sign.
    """

    account_id: str
    account_type: AccountType
    account_number: str
    product_code: str
    product_name: str
    balance: Cents
    interest_rate_bps: int
    status: AccountStatus
    open_date: date
    close_date: date | None = None

    # -------------------------------------------------- deposit
    deposit_type: DepositProductType | None = None
    maturity_date: date | None = None

    # -------------------------------------------------- loan
    loan_type: LoanType | None = None
    loan_number: str | None = None
    original_amount: Cents | None = None
    monthly_emi: Cents | None = None
    loan_start_date: date | None = None
    loan_end_date: date | None = None
    collateral_asset_id: str | None = None
    #: ``loan.loan_status`` — the performance state (CURRENT, DELINQUENT, CHARGED_OFF, PAID_OFF).
    #: Distinct from :attr:`status`, which is the ``account.account_status`` lifecycle state: a
    #: charged-off loan is still an open account.
    product_status: str | None = None

    # -------------------------------------------------- card
    card_number: str | None = None
    credit_limit: Cents | None = None
    card_type: str | None = None

    # -------------------------------------------------- investment
    portfolio_value: Cents | None = None
    investment_components: dict[str, Cents] = field(default_factory=dict)
    investment_risk_profile: InvestmentRiskProfile | None = None

    @property
    def is_open(self) -> bool:
        return self.status is not AccountStatus.CLOSED

    @property
    def is_transactable(self) -> bool:
        """Whether the transaction generator posts activity to this account.

        Deposits and cards carry transaction traffic; loans carry their EMI as a debit from the
        funding deposit account, and investments settle through it too, so neither is posted to
        directly.
        """
        return (
            self.account_type in (AccountType.DEPOSIT, AccountType.CARD)
            and self.status is AccountStatus.ACTIVE
        )


@dataclass(slots=True)
class AssetRecord:
    """A property, vehicle or other asset, as decided by the asset stage."""

    asset_id: str
    asset_type: AssetType
    description: str
    current_value: Cents
    ownership_type: str
    acquired_date: date
    is_collateral: bool = False

    # -------------------------------------------------- property
    property_type: str | None = None
    address_line1: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    purchase_price: Cents | None = None
    assessed_value: Cents | None = None
    square_feet: int | None = None
    year_built: int | None = None

    # -------------------------------------------------- vehicle
    make: str | None = None
    model: str | None = None
    model_year: int | None = None
    vin: str | None = None
    mileage: int | None = None


@dataclass(slots=True)
class HouseholdPlan:
    """A household and its members.

    ``address_hash`` is the grouping key of design §4.3 — households are derived from address plus
    relationships — and it is a hash rather than the address so that grouping never requires loading
    a maskable field.
    """

    household_id: str
    name: str
    address_hash: str
    city: str
    state: str
    postal_code: str
    street_address: str
    member_ids: list[str] = field(default_factory=list)
    primary_customer_id: str | None = None
    roles: dict[str, HouseholdRole] = field(default_factory=dict)

    @property
    def member_count(self) -> int:
        # The column carries CHECK (member_count >= 1), and a household is only ever created with a
        # head, so this cannot legitimately be zero.
        return max(1, len(self.member_ids))


@dataclass(slots=True)
class CustomerPlan:
    """Everything decided about one customer, across all planning stages."""

    # -------------------------------------------------- identity
    customer_id: str
    index: int
    cohort: Cohort
    profile: CohortProfile
    customer_name: str
    given_name: str
    family_name: str
    customer_type: CustomerType
    segment: CustomerSegment
    value_tier: CustomerValue
    value_score: float
    customer_since: date

    # -------------------------------------------------- demographics
    date_of_birth: date | None
    citizenship: str | None
    occupation: str | None
    employment_status: str | None
    marital_status: str | None
    language: str
    preferred_channel: str | None

    # -------------------------------------------------- contact and place
    city: str
    state: str
    postal_code: str
    street_address: str
    unit: str | None
    email: str | None
    phone_number: str | None
    mobile_number: str | None

    # -------------------------------------------------- economics
    monthly_income: Cents
    employer_id: str | None = None

    # -------------------------------------------------- filled by later stages
    household_id: str | None = None
    household_role: HouseholdRole | None = None
    accounts: list[AccountRecord] = field(default_factory=list)
    assets: list[AssetRecord] = field(default_factory=list)
    life_events: list[PlannedLifeEvent] = field(default_factory=list)
    #: Calendar months, as ``YYYY-MM``, in which a deliberate spend anomaly was injected. Written by
    #: the transaction stage and read by the evaluation ground truth of design §15.1.
    anomaly_months: list[str] = field(default_factory=list)
    #: Monthly discretionary + recurring outflow, summed from the emitted transactions.
    monthly_expense: Cents | None = None
    #: Days past due, set by the risk stage for the delinquent cohort.
    days_past_due: int = 0
    #: Fraud alert count, set by the product stage on cards for the fraud-flagged cohort.
    fraud_alerts: int = 0

    # -------------------------------------------------- convenience
    @property
    def has_bureau_file(self) -> bool:
        """Whether a ``credit_profile`` row exists. False for the thin-file cohort."""
        return self.profile.fico is not None

    def accounts_of(self, account_type: AccountType) -> list[AccountRecord]:
        return [account for account in self.accounts if account.account_type is account_type]

    @property
    def primary_deposit(self) -> AccountRecord | None:
        """The account salary lands in and recurring debits leave from.

        Checking is preferred; a customer with only a savings account uses that instead, and the
        thin-file cohort may legitimately have no open deposit account at all.
        """
        deposits = [
            account
            for account in self.accounts_of(AccountType.DEPOSIT)
            if account.status is AccountStatus.ACTIVE
        ]
        for account in deposits:
            if account.deposit_type is DepositProductType.CHECKING:
                return account
        return deposits[0] if deposits else None


@dataclass(slots=True)
class Population:
    """The whole planned dataset, before rows are emitted.

    Lists rather than dicts for the collections that are iterated, so emission order never depends
    on
    a hash. ``by_id`` exists for the lookups the relationship and offer stages need; it is only ever
    indexed, never iterated.
    """

    customers: list[CustomerPlan] = field(default_factory=list)
    households: list[HouseholdPlan] = field(default_factory=list)
    #: ``(employer_id, name, industry, city, state)``.
    employers: list[tuple[str, str, str, str, str]] = field(default_factory=list)
    by_id: dict[str, CustomerPlan] = field(default_factory=dict)

    def add_customer(self, plan: CustomerPlan) -> None:
        self.customers.append(plan)
        self.by_id[plan.customer_id] = plan

    def cohort_counts(self) -> dict[Cohort, int]:
        """Realized cohort sizes. Asserted against the requested allocation by the gate test."""
        counts: dict[Cohort, int] = dict.fromkeys(Cohort, 0)
        for plan in self.customers:
            counts[plan.cohort] += 1
        return counts
