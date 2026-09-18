"""Closed value sets shared by the schema and the domain models (task 1.6).

Every enum here mirrors a ``CHECK (column IN (...))`` constraint in the migrations. Keeping them in
one place makes that correspondence checkable, and :mod:`tests.test_domain_models` does check it: it
reads the ``CHECK`` clause back out of ``sqlite_master`` for each column and asserts the literal set
matches the enum's members exactly.

That test is the reason this module is worth having. Without it the two definitions drift — someone
adds ``'PARTNER'`` to a constraint and not to the enum, and the failure surfaces months later as a
``ValidationError`` on one customer in one cohort. With it, the database and the model layer cannot
disagree about what values exist.

``StrEnum`` rather than ``Enum`` so a member is directly usable as a SQL bound parameter and as a
JSON
value, with no ``.value`` at the boundary.
"""

from __future__ import annotations

from enum import StrEnum


# ---------------------------------------------------------------- customer
class CustomerType(StrEnum):
    INDIVIDUAL = "INDIVIDUAL"
    JOINT = "JOINT"
    BUSINESS = "BUSINESS"
    TRUST = "TRUST"


class CustomerSegment(StrEnum):
    MASS = "MASS"
    AFFLUENT = "AFFLUENT"
    HNW = "HNW"
    UHNW = "UHNW"
    SMALL_BUSINESS = "SMALL_BUSINESS"


class CustomerValue(StrEnum):
    """Value tier. Ordered low to high, but comparison is not defined — use an explicit ranking."""

    BRONZE = "BRONZE"
    SILVER = "SILVER"
    GOLD = "GOLD"
    PLATINUM = "PLATINUM"


# ---------------------------------------------------------------- products
class AccountType(StrEnum):
    DEPOSIT = "DEPOSIT"
    LOAN = "LOAN"
    CARD = "CARD"
    INVESTMENT = "INVESTMENT"


class AccountStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DORMANT = "DORMANT"
    CLOSED = "CLOSED"
    FROZEN = "FROZEN"


class DepositProductType(StrEnum):
    CHECKING = "CHECKING"
    SAVINGS = "SAVINGS"
    MMA = "MMA"
    CD = "CD"


class LoanType(StrEnum):
    MORTGAGE = "MORTGAGE"
    AUTO = "AUTO"
    PERSONAL = "PERSONAL"
    HELOC = "HELOC"
    STUDENT = "STUDENT"
    BUSINESS = "BUSINESS"


class InvestmentRiskProfile(StrEnum):
    CONSERVATIVE = "CONSERVATIVE"
    MODERATE = "MODERATE"
    GROWTH = "GROWTH"
    AGGRESSIVE = "AGGRESSIVE"


# ---------------------------------------------------------------- risk
class RiskBand(StrEnum):
    """A coarse bucket for the risk score, for callers that see the band but not the number.

    Lives here in the domain rather than in the risk service because it is a shared vocabulary term:
    the risk module derives it, the signal detectors branch on it, and the cross-customer cohort
    query filters by it. The score-to-band cut points stay in :mod:`c360.services.risk`, which is
    the one module that owns the numeric policy.
    """

    LOW = "LOW"
    MODERATE = "MODERATE"
    ELEVATED = "ELEVATED"
    HIGH = "HIGH"


class DelinquencyStatus(StrEnum):
    CURRENT = "CURRENT"
    DPD_1_29 = "DPD_1_29"
    DPD_30_59 = "DPD_30_59"
    DPD_60_89 = "DPD_60_89"
    DPD_90_PLUS = "DPD_90_PLUS"


# ---------------------------------------------------------------- assets
class AssetType(StrEnum):
    PROPERTY = "PROPERTY"
    VEHICLE = "VEHICLE"
    OTHER = "OTHER"


class OwnershipType(StrEnum):
    SOLE = "SOLE"
    JOINT = "JOINT"
    TRUST = "TRUST"


class PropertyType(StrEnum):
    PRIMARY_RESIDENCE = "PRIMARY_RESIDENCE"
    SECOND_HOME = "SECOND_HOME"
    INVESTMENT = "INVESTMENT"
    LAND = "LAND"
    COMMERCIAL = "COMMERCIAL"


# ---------------------------------------------------------------- journey
class ApplicationStatus(StrEnum):
    SUBMITTED = "SUBMITTED"
    IN_REVIEW = "IN_REVIEW"
    APPROVED = "APPROVED"
    DECLINED = "DECLINED"
    WITHDRAWN = "WITHDRAWN"
    FUNDED = "FUNDED"

    @property
    def is_decided(self) -> bool:
        """Whether a decision date is expected. Mirrors the ``CHECK`` on ``application``."""
        return self in _DECIDED_APPLICATION_STATUSES


_DECIDED_APPLICATION_STATUSES = frozenset(
    {ApplicationStatus.APPROVED, ApplicationStatus.DECLINED, ApplicationStatus.FUNDED}
)


class FraudResult(StrEnum):
    """Outcome of the fraud check on an application. Requirement 7.5."""

    PASS = "PASS"  # noqa: S105 - a fraud-check outcome, not a credential
    REVIEW = "REVIEW"
    FAIL = "FAIL"


class EngagementEventType(StrEnum):
    LOGIN = "LOGIN"
    LOGOUT = "LOGOUT"
    OTP_REQUEST = "OTP_REQUEST"
    OTP_VERIFY = "OTP_VERIFY"
    PASSWORD_RESET = "PASSWORD_RESET"  # noqa: S105 - an event type, not a credential
    BRANCH_VISIT = "BRANCH_VISIT"
    APP_USAGE = "APP_USAGE"
    CALL = "CALL"
    CHAT = "CHAT"
    STATEMENT_VIEW = "STATEMENT_VIEW"
    SERVICE_REQUEST = "SERVICE_REQUEST"
    COMPLAINT = "COMPLAINT"


class EngagementChannel(StrEnum):
    WEB = "WEB"
    MOBILE = "MOBILE"
    BRANCH = "BRANCH"
    ATM = "ATM"
    CALL_CENTER = "CALL_CENTER"
    EMAIL = "EMAIL"
    SMS = "SMS"


class EngagementOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    ABANDONED = "ABANDONED"
    PENDING = "PENDING"


class LifeEventType(StrEnum):
    MARRIAGE = "MARRIAGE"
    DIVORCE = "DIVORCE"
    CHILD_BIRTH = "CHILD_BIRTH"
    HOME_PURCHASE = "HOME_PURCHASE"
    RELOCATION = "RELOCATION"
    JOB_CHANGE = "JOB_CHANGE"
    PROMOTION = "PROMOTION"
    RETIREMENT = "RETIREMENT"
    EDUCATION = "EDUCATION"
    BEREAVEMENT = "BEREAVEMENT"
    BUSINESS_START = "BUSINESS_START"
    INHERITANCE = "INHERITANCE"


class LifeEventSource(StrEnum):
    SYSTEM_OF_RECORD = "SYSTEM_OF_RECORD"
    CUSTOMER_DECLARED = "CUSTOMER_DECLARED"
    INFERRED = "INFERRED"


# ---------------------------------------------------------------- offers
class BusinessGroup(StrEnum):
    DEPOSITS = "DEPOSITS"
    LENDING = "LENDING"
    CARDS = "CARDS"
    WEALTH = "WEALTH"
    INSURANCE = "INSURANCE"
    BUSINESS = "BUSINESS"


class OfferType(StrEnum):
    CROSS_SELL = "CROSS_SELL"
    UPSELL = "UPSELL"
    RETENTION = "RETENTION"
    ACQUISITION = "ACQUISITION"
    SERVICE = "SERVICE"


class OfferStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    WITHDRAWN = "WITHDRAWN"


class CampaignStatus(StrEnum):
    PLANNED = "PLANNED"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class CustomerReaction(StrEnum):
    PENDING = "PENDING"
    VIEWED = "VIEWED"
    INTERESTED = "INTERESTED"
    ACCEPTED = "ACCEPTED"
    DECLINED = "DECLINED"
    IGNORED = "IGNORED"


class ProbabilityConfidence(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


# ---------------------------------------------------------------- relationships
class HouseholdRole(StrEnum):
    HEAD = "HEAD"
    SPOUSE = "SPOUSE"
    PARTNER = "PARTNER"
    CHILD = "CHILD"
    DEPENDENT = "DEPENDENT"
    OTHER = "OTHER"


class RelationshipType(StrEnum):
    SPOUSE = "SPOUSE"
    PARTNER = "PARTNER"
    PARENT = "PARENT"
    CHILD = "CHILD"
    SIBLING = "SIBLING"
    GUARDIAN = "GUARDIAN"
    BUSINESS_PARTNER = "BUSINESS_PARTNER"
    REFERRED_BY = "REFERRED_BY"
    ADVISOR = "ADVISOR"
    OTHER = "OTHER"


class PartyRole(StrEnum):
    PRIMARY = "PRIMARY"
    JOINT = "JOINT"
    AUTHORIZED_USER = "AUTHORIZED_USER"
    CUSTODIAN = "CUSTODIAN"
    POWER_OF_ATTORNEY = "POWER_OF_ATTORNEY"
