"""Domain read models (task 1.6).

These are the shapes repositories return. Three properties are enforced by the base class rather
than
left to each model, because each one is a requirement rather than a style preference.

**Every model carries provenance.** ``as_of_date`` and ``source_system`` are on
:class:`SourcedModel`, so no repository can return a value without them. Requirement 4.8 puts an
as-of timestamp on every displayed figure and design §4.3 makes the pair the basis of citation
provenance, which task 6.1 turns into ``(entity_type, entity_id, field, as_of)`` on every fact an
agent is allowed to narrate. A model that could omit it would be a model an agent could cite
without a date.

Some tables have no ``source_system`` column of their own — ``contact_info``, the four profile
tables,
``account`` and the product specializations. That is design §4.3's DDL, not an oversight: those rows
are dependent parts of an aggregate and inherit the root's system of record. The repository injects
the root's value when mapping, so the invariant holds at the model layer even though it does not
hold
column-for-column in the schema.

**Models are frozen.** A read model that a service can mutate is a read model that can be mutated
after masking has been applied. Immutability makes the field-masking serializer of task 4.4 the only
thing that can shape a payload.

**Unknown fields are an error.** ``extra="forbid"`` turns a schema change the models have not caught
up with into a loud failure on the first read, rather than a column that silently stops being
displayed.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer

from c360.domain.dates import from_iso, to_iso
from c360.domain.enums import (
    AccountStatus,
    AccountType,
    ApplicationStatus,
    AssetType,
    BusinessGroup,
    CampaignStatus,
    CustomerReaction,
    CustomerSegment,
    CustomerType,
    CustomerValue,
    DelinquencyStatus,
    DepositProductType,
    EngagementChannel,
    EngagementEventType,
    EngagementOutcome,
    FraudResult,
    HouseholdRole,
    InvestmentRiskProfile,
    LifeEventSource,
    LifeEventType,
    LoanType,
    OfferStatus,
    OfferType,
    OwnershipType,
    PartyRole,
    ProbabilityConfidence,
    PropertyType,
    RelationshipType,
)
from c360.domain.money import Bps, Cents


def _coerce_iso_date(value: Any) -> Any:
    """Parse a stored ISO-8601 string into a :class:`~datetime.date`.

    Applied as a ``BeforeValidator`` so the strictness of :func:`c360.domain.dates.from_iso` governs
    — Pydantic's own date parsing accepts the basic format and timestamps, which the ``CHECK``
    constraints in the migrations do not.
    """
    if isinstance(value, str):
        return from_iso(value)
    if isinstance(value, datetime):
        raise ValueError("date columns hold dates, not datetimes; a time component means bad data")
    return value


#: A calendar date that validates and serializes exactly as the database stores it.
type IsoDate = Annotated[
    date,
    BeforeValidator(_coerce_iso_date),
    PlainSerializer(to_iso, return_type=str, when_used="json"),
]

#: 0..1 probability or confidence score.
type Probability = Annotated[float, Field(ge=0.0, le=1.0)]

#: Basis points constrained to a share of a whole, i.e. 0%..100%.
type ShareBps = Annotated[Bps, Field(ge=0, le=10_000)]


class DomainModel(BaseModel):
    """Base for every read model."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        # Enum members validate from their string values, which is how they arrive from SQLite.
        use_enum_values=False,
        # Values are already the right types coming out of the mappers, but a str for a StrEnum and
        # an int for Cents both need coercion, so strict mode is not usable here.
        strict=False,
        validate_default=True,
    )


class SourcedModel(DomainModel):
    """A read model carrying the provenance of the row it came from."""

    as_of_date: IsoDate
    source_system: str


# ================================================================ customer aggregate
class Employer(SourcedModel):
    """``employer``. Has no provenance columns of its own; inherits the customer's."""

    employer_id: str
    employer_name: str
    industry: str | None = None
    city: str | None = None
    state: str | None = None


class Household(SourcedModel):
    """``household``."""

    household_id: str
    household_name: str
    primary_customer_id: str | None = None
    address_hash: str
    member_count: int = Field(ge=1)


class Customer(SourcedModel):
    """``customer``. The fields requirement 4.5 requires the profile module to display."""

    customer_id: str
    customer_name: str
    customer_type: CustomerType
    customer_segment: CustomerSegment
    customer_since: IsoDate
    date_of_birth: IsoDate | None = None
    citizenship: str | None = None
    occupation: str | None = None
    employer_id: str | None = None
    employment_status: str | None = None
    marital_status: str | None = None
    customer_value: CustomerValue
    customer_value_score: float | None = None
    preferred_language: str = "en"
    preferred_channel: str | None = None
    household_id: str | None = None


class CustomerSearchHit(DomainModel):
    """One row of an FTS5 ``customer_search`` result (requirement 3.1).

    Deliberately thin — id, name, segment and the city the match came from — because a search result
    list is a picker, not a profile. The identifying columns the index searches on (email, phone,
    account and loan numbers, card last-4) are *not* echoed back: a hit on a card's last four digits
    should return the customer, not re-display the digits that were typed, and returning them would
    put maskable values on a path the field-masking serializer does not cover. Selecting the profile
    then goes through :meth:`CustomerService.get_profile`, which is masked.
    """

    customer_id: str
    customer_name: str
    customer_segment: CustomerSegment
    city: str | None = None


class CustomerCohortHit(DomainModel):
    """One row of a book-level cohort query (cross-customer Q&A, "high risk customers").

    Deliberately thin, like :class:`CustomerSearchHit`: a cohort answer is a ranked *list* of
    customers matching a criterion (a risk band, a segment, a value tier, a delinquency status), not
    a set of profiles. It carries only the display and ranking columns the list needs — the id and
    name to identify each customer, and the coarse, non-maskable band/segment/value/delinquency
    columns the query filtered and ranked on. ``risk_band`` is the derived bucket, not a stored
    column; the repository computes it from ``risk_score`` in the SELECT so a caller sees the same
    banding the risk module shows. No raw balances ride this path, so nothing here is maskable and
    the whole row is safe to display in a cohort list a relationship manager scans.
    """

    customer_id: str
    customer_name: str
    customer_segment: CustomerSegment
    customer_value: CustomerValue
    customer_value_score: float | None = None
    risk_score: float | None = None
    risk_band: str
    delinquency_status: DelinquencyStatus | None = None


class ContactInfo(SourcedModel):
    """``contact_info``. Every field here is masked for at least one role (design §7.2)."""

    customer_id: str
    email: str | None = None
    phone_number: str | None = None
    mobile_number: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    postal_code: str | None = None


# ================================================================ financial aggregate
class FinancialProfile(SourcedModel):
    """``financial_profile``. Requirement 5.1's headline values."""

    customer_id: str
    total_deposits_cents: Cents
    total_loans_cents: Cents
    total_investments_cents: Cents
    total_assets_cents: Cents
    total_liabilities_cents: Cents
    net_worth_cents: Cents
    household_net_worth_cents: Cents | None = None
    monthly_income_cents: Cents | None = None
    monthly_expense_cents: Cents | None = None


class CreditProfile(SourcedModel):
    """``credit_profile``. Requirement 5.10."""

    customer_id: str
    fico_score: Annotated[int, Field(ge=300, le=850)] | None = None
    behavior_score: int | None = None
    propensity_score: Annotated[float, Field(ge=0, le=100)] | None = None
    credit_utilization_bps: Bps | None = None
    years_on_bureau: float | None = None
    num_inquiries: int | None = None
    num_trades: int | None = None
    num_credit_accounts: int | None = None
    credit_exposure_cents: Cents | None = None


class Account(SourcedModel):
    """``account``, the shared parent of the four product types."""

    account_id: str
    customer_id: str
    account_number: str
    account_type: AccountType
    product_name: str | None = None
    product_code: str | None = None
    balance_cents: Cents
    available_balance_cents: Cents | None = None
    interest_rate_bps: Bps | None = None
    account_status: AccountStatus
    open_date: IsoDate
    close_date: IsoDate | None = None


class Deposit(SourcedModel):
    """``deposit``. Requirement 5.4."""

    kind: Literal[AccountType.DEPOSIT] = AccountType.DEPOSIT
    account_id: str
    product_type: DepositProductType | None = None
    household_deposits_cents: Cents | None = None
    maturity_date: IsoDate | None = None


class Loan(SourcedModel):
    """``loan``. Requirement 5.5. Current balance lives on the parent ``account``."""

    kind: Literal[AccountType.LOAN] = AccountType.LOAN
    account_id: str
    loan_number: str
    loan_type: LoanType | None = None
    original_amount_cents: Cents
    monthly_emi_cents: Cents | None = None
    loan_status: str | None = None
    loan_start_date: IsoDate | None = None
    loan_end_date: IsoDate | None = None
    collateral_asset_id: str | None = None


class CreditCard(SourcedModel):
    """``credit_card``. Requirement 5.6.

    ``card_number`` — the full PAN — is deliberately **absent**. Design §4.3 stores ``card_last4``
    separately "so hot paths never load a full PAN", and a field that is never read cannot leak
    through a masking rule that was forgotten. Nothing in requirements 5.6 or 8.6 displays a PAN.
    """

    kind: Literal[AccountType.CARD] = AccountType.CARD
    account_id: str
    card_last4: str = Field(min_length=4, max_length=4)
    card_type: str | None = None
    credit_limit_cents: Cents
    utilization_bps: Bps | None = None
    rewards_balance_cents: Cents | None = None
    monthly_spend_cents: Cents | None = None
    overlimit_events: int = Field(ge=0)
    fraud_alerts: int = Field(ge=0)


class Investment(SourcedModel):
    """``investment``. Requirement 5.7."""

    kind: Literal[AccountType.INVESTMENT] = AccountType.INVESTMENT
    account_id: str
    portfolio_value_cents: Cents
    asset_allocation: str | None = None
    mutual_funds_cents: Cents | None = None
    stocks_cents: Cents | None = None
    bonds_cents: Cents | None = None
    retirement_accounts_cents: Cents | None = None
    investment_risk_profile: InvestmentRiskProfile | None = None


#: The product-specific part of a holding, discriminated on ``kind`` so a union of the four is
#: unambiguous without relying on Pydantic's smart-union heuristics.
type ProductDetail = Annotated[
    Deposit | Loan | CreditCard | Investment,
    Field(discriminator="kind"),
]


class Holding(DomainModel):
    """An account together with its product specialization.

    Requirement 5.3 lists "all accounts, deposits, loans, credit cards and investment accounts" as
    one collection, so this is the shape that read serves. ``detail`` is ``None`` only when the
    specialization row is missing, which is a data defect the caller can surface rather than a state
    the mapper should paper over.
    """

    account: Account
    detail: ProductDetail | None = None

    @property
    def as_of_date(self) -> date:
        """Provenance of the holding, taken from the parent account."""
        return self.account.as_of_date

    @property
    def source_system(self) -> str:
        return self.account.source_system


class Transaction(SourcedModel):
    """``txn``. ``amount_cents`` is signed: debits negative, credits positive."""

    transaction_id: str
    account_id: str
    customer_id: str
    transaction_date: IsoDate
    amount_cents: Cents
    transaction_type: str
    transaction_category: str
    merchant: str | None = None
    channel: str | None = None
    status: str


class CategoryTotal(DomainModel):
    """One row of the category aggregation behind requirement 5.8.

    Computed in SQL with ``SUM``, so the arithmetic is exact integer addition inside SQLite rather
    than a Python fold over thousands of rows.
    """

    transaction_category: str
    total_cents: Cents
    transaction_count: int = Field(ge=0)


class MonthlyTotal(DomainModel):
    """One point of the monthly trend series behind requirements 5.8 and 5.9."""

    #: ``YYYY-MM``. Not a date: it identifies a month, and giving it a day would invite the reader
    #: to treat it as one.
    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    total_cents: Cents
    transaction_count: int = Field(ge=0)


# ================================================================ risk aggregate
class RiskProfile(SourcedModel):
    """``risk_profile``. Requirement 8.1."""

    customer_id: str
    risk_score: float | None = None
    fraud_score: float | None = None
    pid_score: float | None = None
    sid_score: float | None = None
    delinquency_status: DelinquencyStatus | None = None
    current_days_past_due: int = Field(ge=0)
    default_indicator: bool
    chargeoff_indicator: bool
    aml_flag: bool
    pep_flag: bool

    @property
    def requires_compliance_indicator(self) -> bool:
        """Requirement 8.4: AML or PEP forces a persistent, non-dismissible indicator."""
        return self.aml_flag or self.pep_flag


# ================================================================ relationship aggregate
class HouseholdMember(SourcedModel):
    """``household_member``."""

    household_id: str
    customer_id: str
    member_role: HouseholdRole
    joined_date: IsoDate | None = None


class CustomerRelationship(SourcedModel):
    """``customer_relationship``.

    Requirement 6.6 requires an inferred link to be labelled and to show its confidence, which is
    why ``is_inferred`` and ``confidence`` are not optional decoration. The schema enforces that an
    inferred row has both.

    Direction is preserved as stored. ``RelationshipType`` is half asymmetric — ``PARENT``,
    ``CHILD``,
    ``GUARDIAN``, ``REFERRED_BY`` and ``ADVISOR`` all read differently in reverse, and three of them
    have no inverse member — so a reader has to know which side the customer is on before rendering
    the label.
    """

    relationship_id: str
    from_customer_id: str
    to_customer_id: str
    relationship_type: RelationshipType
    is_inferred: bool
    confidence: Probability | None = None
    inference_basis: str | None = None

    def counterparty_of(self, customer_id: str) -> str:
        """Return the other party. Raises if ``customer_id`` is not on this edge at all."""
        if customer_id == self.from_customer_id:
            return self.to_customer_id
        if customer_id == self.to_customer_id:
            return self.from_customer_id
        raise ValueError(f"{customer_id} is not a party to relationship {self.relationship_id}")

    def is_subject(self, customer_id: str) -> bool:
        """Whether ``customer_id`` is the ``from`` side, i.e. the label reads forwards for them."""
        return customer_id == self.from_customer_id


class AccountParty(SourcedModel):
    """``account_party``. Joint holders and authorized users, requirement 6.1."""

    account_id: str
    customer_id: str
    party_role: PartyRole
    ownership_bps: ShareBps | None = None
    added_date: IsoDate | None = None


class Beneficiary(SourcedModel):
    """``beneficiary``. ``beneficiary_customer_id`` is null when the beneficiary is not a client."""

    beneficiary_id: str
    account_id: str
    beneficiary_customer_id: str | None = None
    beneficiary_name: str
    relationship: str | None = None
    share_bps: ShareBps
    is_inferred: bool
    confidence: Probability | None = None


class Asset(SourcedModel):
    """``asset``, the shared parent of ``property`` and ``vehicle``."""

    asset_id: str
    customer_id: str
    asset_type: AssetType
    asset_description: str | None = None
    current_value_cents: Cents
    ownership_type: OwnershipType | None = None
    acquired_date: IsoDate | None = None
    is_collateral: bool


class PropertyDetail(SourcedModel):
    """``property``. Named ``PropertyDetail`` because ``property`` is a builtin."""

    kind: Literal[AssetType.PROPERTY] = AssetType.PROPERTY
    asset_id: str
    property_type: PropertyType | None = None
    address_line1: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    purchase_price_cents: Cents | None = None
    assessed_value_cents: Cents | None = None
    square_feet: int | None = None
    year_built: int | None = None


class VehicleDetail(SourcedModel):
    """``vehicle``. ``vin`` is a masked field (design §7.2), never leaving a response unmasked."""

    kind: Literal[AssetType.VEHICLE] = AssetType.VEHICLE
    asset_id: str
    make: str | None = None
    model: str | None = None
    model_year: int | None = None
    vin: str | None = None
    mileage: int | None = None
    purchase_price_cents: Cents | None = None


type AssetDetail = Annotated[
    PropertyDetail | VehicleDetail,
    Field(discriminator="kind"),
]


class AssetHolding(DomainModel):
    """An asset with its specialization, mirroring :class:`Holding` for products."""

    asset: Asset
    detail: AssetDetail | None = None

    @property
    def as_of_date(self) -> date:
        return self.asset.as_of_date

    @property
    def source_system(self) -> str:
        return self.asset.source_system


# ================================================================ offer aggregate
class Campaign(SourcedModel):
    """``campaign``. Requirement 9.7."""

    campaign_id: str
    campaign_name: str
    business_group: BusinessGroup
    channel: str | None = None
    start_date: IsoDate
    end_date: IsoDate | None = None
    campaign_status: CampaignStatus


class Offer(SourcedModel):
    """``offer``. The catalogue entry, independent of any customer."""

    offer_id: str
    offer_name: str
    business_group: BusinessGroup
    offer_type: OfferType
    product_code: str | None = None
    product_type: str | None = None
    value_cents: Cents | None = None
    start_date: IsoDate
    end_date: IsoDate | None = None
    offer_status: OfferStatus
    campaign_id: str | None = None


class CustomerOffer(SourcedModel):
    """``customer_offer``. This customer's relationship to one offer."""

    customer_offer_id: str
    customer_id: str
    offer_id: str
    event_date: IsoDate
    customer_reaction: CustomerReaction
    reaction_date: IsoDate | None = None
    acceptance_probability: Probability | None = None
    probability_confidence: ProbabilityConfidence | None = None
    expected_value_cents: Cents | None = None
    affinity_basis: str | None = None
    is_suppressed: bool
    suppression_reason: str | None = None
    channel: str | None = None


class OfferForCustomer(DomainModel):
    """An offer joined to this customer's reaction to it.

    Requirement 9.1 displays fields from both rows in one list, and 9.2 ranks by expected value,
    which needs the offer's worth and the customer's probability together. Returning them joined
    keeps the ranking a pure function of one object rather than of a pair the caller has to align.
    """

    offer: Offer
    customer_offer: CustomerOffer
    campaign: Campaign | None = None

    @property
    def as_of_date(self) -> date:
        return self.customer_offer.as_of_date

    @property
    def source_system(self) -> str:
        return self.customer_offer.source_system


# ================================================================ journey aggregate
class LifeEvent(SourcedModel):
    """``life_event``. Requirements 7.2 and 7.3.

    ``signals`` is the JSON-text citation of what an inference was drawn from. It stays text here
    rather than being parsed into a list: requirement 7.3 needs it displayed and cited, no consumer
    computes over it, and parsing it at the repository would mean handling malformed JSON on a read
    path that has nothing useful to do about it.
    """

    life_event_id: str
    customer_id: str
    life_event_type: LifeEventType
    event_date: IsoDate
    confidence: Probability | None = None
    source: LifeEventSource
    is_inferred: bool
    signals: str | None = None


class Application(SourcedModel):
    """``application``. Requirement 7.5. ``event_date`` is the application date."""

    application_id: str
    customer_id: str
    product_applied: str
    product_code: str | None = None
    event_date: IsoDate
    channel: str | None = None
    application_status: ApplicationStatus
    fraud_result: FraudResult | None = None
    decision_date: IsoDate | None = None
    requested_amount_cents: Cents | None = None
    approved_amount_cents: Cents | None = None
    account_id: str | None = None


class EngagementEvent(SourcedModel):
    """``customer_event``. Requirement 7.6, filterable by channel and event type."""

    event_id: str
    customer_id: str
    event_type: EngagementEventType
    event_date: IsoDate
    channel: EngagementChannel
    session_id: str | None = None
    device_type: str | None = None
    outcome: EngagementOutcome | None = None
    notes: str | None = None


__all__ = [
    "Account",
    "AccountParty",
    "Application",
    "Asset",
    "AssetDetail",
    "AssetHolding",
    "Beneficiary",
    "Campaign",
    "CategoryTotal",
    "ContactInfo",
    "CreditCard",
    "CreditProfile",
    "Customer",
    "CustomerOffer",
    "CustomerRelationship",
    "CustomerSearchHit",
    "Deposit",
    "DomainModel",
    "Employer",
    "EngagementEvent",
    "FinancialProfile",
    "Holding",
    "Household",
    "HouseholdMember",
    "Investment",
    "IsoDate",
    "LifeEvent",
    "Loan",
    "MonthlyTotal",
    "Offer",
    "OfferForCustomer",
    "Probability",
    "ProductDetail",
    "PropertyDetail",
    "RiskProfile",
    "ShareBps",
    "SourcedModel",
    "Transaction",
    "VehicleDetail",
]
