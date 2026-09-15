"""Name, place, product and merchant vocabularies (tasks 2.2, 2.3, 2.4).

Everything here is a ``tuple``, never a ``set``, because iteration and indexing order feed directly
into the emitted rows and ``set`` order over strings varies per process (see
:mod:`c360.generator.context`).

The vocabularies are deliberately synthetic and deliberately unremarkable. Two constraints shaped
them:

* **Nothing here may resemble a real person's contact details.** Emails use the ``.invalid``
  reserved TLD (RFC 2606) and telephone numbers use the ``555-01xx`` block reserved for fiction, so
  a
  seeded dataset that escapes into a demo cannot reach anyone.
* **Names are drawn from a wide range of origins** because the platform masks and searches on names,
  and a corpus of one naming convention would leave the search index and the masking rules tested
  against only the easy case — single-token given names, no diacritics, no particles.

Product catalogue codes are the join to the knowledge corpus. Design §17.1 requires the Phase 7
documents to use "product codes aligned to seeded products", so these codes are the authority and
the
corpus will be written against them rather than the reverse.
"""

from __future__ import annotations

from typing import Final

from c360.domain.enums import (
    DepositProductType,
    EngagementChannel,
    LoanType,
)

# ---------------------------------------------------------------- people
GIVEN_NAMES: Final[tuple[str, ...]] = (
    "Amara",
    "Priya",
    "Renata",
    "Yusuf",
    "Mei",
    "Tomas",
    "Ingrid",
    "Kwame",
    "Sofia",
    "Dmitri",
    "Aisha",
    "Hiroshi",
    "Lucia",
    "Omar",
    "Freya",
    "Rajesh",
    "Camila",
    "Bjorn",
    "Nadia",
    "Chen",
    "Elena",
    "Malik",
    "Saoirse",
    "Andres",
    "Fatima",
    "Lars",
    "Ananya",
    "Diego",
    "Zara",
    "Kofi",
    "Isabella",
    "Hassan",
    "Marta",
    "Jin",
    "Colette",
    "Ravi",
    "Adaeze",
    "Mateo",
    "Ilse",
    "Sanjay",
    "Beatriz",
    "Idris",
    "Anneke",
    "Tariq",
    "Rosalind",
    "Pavel",
    "Ngozi",
    "Emilio",
    "Solveig",
    "Arun",
    "Delphine",
    "Bashir",
    "Katarzyna",
    "Hugo",
    "Leila",
    "Stefan",
    "Chiara",
    "Kenji",
    "Aurelie",
    "Vikram",
)

FAMILY_NAMES: Final[tuple[str, ...]] = (
    "Alvarez",
    "Okonkwo",
    "Nakamura",
    "Lindqvist",
    "Bhattacharya",
    "Moreau",
    "Kowalski",
    "Haddad",
    "Fernandes",
    "Novak",
    "Sørensen",
    "Adeyemi",
    "Rossi",
    "Petrov",
    "Mensah",
    "Vasquez",
    "Larsen",
    "Chatterjee",
    "Dubois",
    "Marchetti",
    "Oyelaran",
    "Kaur",
    "Bergstrom",
    "Ferreira",
    "Zawadzki",
    "Nakagawa",
    "Andersen",
    "Rahimi",
    "Silva",
    "Kovalenko",
    "Bassi",
    "Lefevre",
    "Ibrahim",
    "Halvorsen",
    "Duarte",
    "Sandoval",
    "Weisz",
    "Achebe",
    "Colombo",
    "Iyer",
    "Grimaldi",
    "Bakker",
    "Farooqi",
    "Steiner",
    "Munoz",
    "Tanaka",
    "Villanueva",
    "Eriksen",
    "Sharma",
    "Delacroix",
)

OCCUPATIONS: Final[tuple[str, ...]] = (
    "Operations Manager",
    "Registered Nurse",
    "Software Engineer",
    "Secondary School Teacher",
    "Civil Engineer",
    "Accountant",
    "Logistics Coordinator",
    "Pharmacist",
    "Dental Hygienist",
    "Electrician",
    "Marketing Manager",
    "Research Scientist",
    "Paralegal",
    "Physiotherapist",
    "Quantity Surveyor",
    "Chef de Partie",
    "Airline Pilot",
    "Veterinarian",
    "Architect",
    "Data Analyst",
    "Site Foreman",
    "Insurance Underwriter",
    "Radiographer",
    "Town Planner",
    "Actuary",
    "Speech Therapist",
    "Millwright",
    "Portfolio Manager",
    "Surgeon",
    "Notary",
)

#: Occupations that fit the small-business cohort, where the customer is the proprietor.
PROPRIETOR_OCCUPATIONS: Final[tuple[str, ...]] = (
    "Restaurant Owner",
    "Independent Pharmacist",
    "Garage Proprietor",
    "Bakery Owner",
    "Freight Operator",
    "Landscaping Contractor",
    "Boutique Owner",
    "Dental Practice Partner",
    "Print Shop Owner",
    "Nursery Proprietor",
    "Consulting Practice Principal",
    "Brewery Owner",
)

EMPLOYMENT_STATUSES: Final[tuple[tuple[str, int], ...]] = (
    ("EMPLOYED", 70),
    ("SELF_EMPLOYED", 14),
    ("RETIRED", 8),
    ("STUDENT", 4),
    ("UNEMPLOYED", 4),
)

MARITAL_STATUSES: Final[tuple[tuple[str, int], ...]] = (
    ("MARRIED", 45),
    ("SINGLE", 33),
    ("DIVORCED", 12),
    ("PARTNERED", 7),
    ("WIDOWED", 3),
)

CITIZENSHIPS: Final[tuple[tuple[str, int], ...]] = (
    ("US", 82),
    ("CA", 5),
    ("GB", 4),
    ("IN", 3),
    ("DE", 2),
    ("NG", 2),
    ("JP", 1),
    ("BR", 1),
)

LANGUAGES: Final[tuple[tuple[str, int], ...]] = (
    ("en", 84),
    ("es", 9),
    ("zh", 3),
    ("fr", 2),
    ("de", 1),
    ("hi", 1),
)

PREFERRED_CHANNELS: Final[tuple[tuple[str, int], ...]] = (
    (EngagementChannel.MOBILE, 42),
    (EngagementChannel.WEB, 30),
    (EngagementChannel.EMAIL, 12),
    (EngagementChannel.BRANCH, 9),
    (EngagementChannel.CALL_CENTER, 7),
)

# ---------------------------------------------------------------- places
#: ``(city, state, postal prefix)``. A real postal prefix per city keeps the address plausible while
#: the last two digits stay generated, so no complete real address is ever produced.
CITIES: Final[tuple[tuple[str, str, str], ...]] = (
    ("Columbus", "OH", "432"),
    ("Austin", "TX", "787"),
    ("Portland", "OR", "972"),
    ("Raleigh", "NC", "276"),
    ("Denver", "CO", "802"),
    ("Madison", "WI", "537"),
    ("Tempe", "AZ", "852"),
    ("Providence", "RI", "029"),
    ("Chattanooga", "TN", "374"),
    ("Boise", "ID", "837"),
    ("Rochester", "NY", "146"),
    ("Fresno", "CA", "937"),
    ("Lincoln", "NE", "685"),
    ("Spokane", "WA", "992"),
    ("Akron", "OH", "443"),
    ("Shreveport", "LA", "711"),
)

STREET_NAMES: Final[tuple[str, ...]] = (
    "Kestrel Lane",
    "Maple Ridge Road",
    "Sycamore Court",
    "Alder Way",
    "Juniper Street",
    "Hawthorn Drive",
    "Beckett Avenue",
    "Wren Close",
    "Foxglove Terrace",
    "Larkspur Boulevard",
    "Cobblestone Row",
    "Whitfield Street",
    "Ashgrove Crescent",
    "Bramble Path",
    "Ironwood Avenue",
    "Selby Road",
    "Marlowe Street",
    "Thistledown Lane",
    "Peregrine Way",
    "Corliss Avenue",
)

UNIT_FORMS: Final[tuple[str, ...]] = ("Apt", "Unit", "Suite", "Flat")

# ---------------------------------------------------------------- employers
#: ``(name, industry)``. Employer links matter to the small-business cohort and to the relationship
#: graph, where a shared employer is one of the inference bases in requirement 6.6.
EMPLOYERS: Final[tuple[tuple[str, str], ...]] = (
    ("Northwind Logistics", "Transportation"),
    ("Cedarline Health System", "Healthcare"),
    ("Ravenna Software", "Technology"),
    ("Halcyon Public Schools", "Education"),
    ("Brightmoor Engineering", "Engineering"),
    ("Ashfield Financial Group", "Financial Services"),
    ("Verdant Grocers", "Retail"),
    ("Kestrel Manufacturing", "Manufacturing"),
    ("Sablewood Legal", "Legal Services"),
    ("Orchard Bay Hospitality", "Hospitality"),
    ("Quarry Hill Construction", "Construction"),
    ("Meridian Analytics", "Professional Services"),
    ("Foxglove Pharmaceuticals", "Pharmaceuticals"),
    ("Tessellate Media", "Media"),
    ("Ironbridge Utilities", "Utilities"),
    ("Solstice Agriculture", "Agriculture"),
)

# ---------------------------------------------------------------- product catalogue
#: ``(product_code, product_name, deposit product type, interest rate bps bounds)``.
DEPOSIT_PRODUCTS: Final[tuple[tuple[str, str, DepositProductType, tuple[int, int]], ...]] = (
    ("DDA-EVERYDAY", "Everyday Checking", DepositProductType.CHECKING, (0, 25)),
    ("DDA-PREMIER", "Premier Checking", DepositProductType.CHECKING, (10, 75)),
    ("SAV-CORE", "Core Savings", DepositProductType.SAVINGS, (150, 320)),
    ("SAV-HIYIELD", "High Yield Savings", DepositProductType.SAVINGS, (380, 470)),
    ("MMA-SELECT", "Money Market Select", DepositProductType.MMA, (300, 440)),
    ("CD-12M", "12 Month Certificate", DepositProductType.CD, (400, 500)),
    ("CD-36M", "36 Month Certificate", DepositProductType.CD, (420, 520)),
    ("BUS-CHK", "Business Operating Account", DepositProductType.CHECKING, (0, 40)),
)

#: ``(product_code, product_name, loan type, rate bps bounds, term months, amount cents bounds)``.
LOAN_PRODUCTS: Final[
    tuple[tuple[str, str, LoanType, tuple[int, int], int, tuple[int, int]], ...]
] = (
    (
        "MTG-30F",
        "30 Year Fixed Mortgage",
        LoanType.MORTGAGE,
        (575, 720),
        360,
        (18_000_000, 95_000_000),
    ),
    (
        "MTG-15F",
        "15 Year Fixed Mortgage",
        LoanType.MORTGAGE,
        (525, 660),
        180,
        (12_000_000, 60_000_000),
    ),
    ("AUTO-NEW", "New Vehicle Loan", LoanType.AUTO, (620, 890), 60, (1_800_000, 7_500_000)),
    ("AUTO-USED", "Used Vehicle Loan", LoanType.AUTO, (720, 1_150), 48, (900_000, 3_800_000)),
    ("PL-UNSEC", "Personal Loan", LoanType.PERSONAL, (990, 1_890), 36, (300_000, 3_500_000)),
    ("HELOC-VAR", "Home Equity Line", LoanType.HELOC, (690, 1_020), 120, (2_500_000, 20_000_000)),
    (
        "STU-CONS",
        "Student Loan Consolidation",
        LoanType.STUDENT,
        (490, 780),
        120,
        (1_200_000, 9_000_000),
    ),
    (
        "BUS-TERM",
        "Business Term Loan",
        LoanType.BUSINESS,
        (760, 1_240),
        84,
        (3_000_000, 45_000_000),
    ),
)

#: ``(product_code, card_name, card_type, credit limit cents bounds, rate bps bounds)``.
CARD_PRODUCTS: Final[tuple[tuple[str, str, str, tuple[int, int], tuple[int, int]], ...]] = (
    ("CC-EVERYDAY", "Everyday Rewards Card", "VISA_CLASSIC", (100_000, 600_000), (1_990, 2_690)),
    ("CC-CASHBACK", "Cashback Plus Card", "VISA_SIGNATURE", (300_000, 1_500_000), (1_790, 2_490)),
    ("CC-TRAVEL", "Travel Elite Card", "MASTERCARD_WORLD", (800_000, 4_000_000), (1_690, 2_290)),
    (
        "CC-PRIVATE",
        "Private Client Card",
        "MASTERCARD_WORLD_ELITE",
        (2_500_000, 15_000_000),
        (1_490, 1_990),
    ),
    ("CC-BUSINESS", "Business Expense Card", "VISA_BUSINESS", (500_000, 6_000_000), (1_890, 2_590)),
)

#: ``(product_code, product_name)`` for investment accounts.
INVESTMENT_PRODUCTS: Final[tuple[tuple[str, str], ...]] = (
    ("INV-BROKER", "Self Directed Brokerage"),
    ("INV-MANAGED", "Managed Portfolio"),
    ("INV-IRA", "Individual Retirement Account"),
    ("INV-TRUST", "Trust Investment Account"),
    ("INV-529", "Education Savings Plan"),
)

# ---------------------------------------------------------------- merchants by category
#: Merchant names per spend category. Keys mirror :data:`SPEND_CATEGORIES` names.
MERCHANTS: Final[dict[str, tuple[str, ...]]] = {
    "GROCERIES": (
        "Verdant Grocers",
        "Harvest Lane Market",
        "Cobblestone Foods",
        "Meadowbrook Provisions",
        "Riverbend Co-op",
    ),
    "DINING": (
        "The Copper Kettle",
        "Saffron & Thyme",
        "Northside Taqueria",
        "Bluejay Coffee House",
        "Larkspur Bistro",
        "Ember Grill",
    ),
    "TRANSPORT": (
        "Metro Transit Authority",
        "Fairway Fuel",
        "CityRide",
        "Kestrel Parking",
        "Ironbridge Tolls",
    ),
    "UTILITIES": (
        "Ironbridge Utilities",
        "Clearwater Municipal",
        "Northgate Energy",
        "Beacon Broadband",
    ),
    "SHOPPING": (
        "Alder & Co",
        "Tessellate Home",
        "Foxglove Apparel",
        "Quarry Hill Hardware",
        "Selby Department Store",
    ),
    "ENTERTAINMENT": (
        "Orpheum Cinema",
        "Riverside Concert Hall",
        "Sablewood Books",
        "Peregrine Arcade",
    ),
    "HEALTHCARE": (
        "Cedarline Health System",
        "Maple Ridge Dental",
        "Foxglove Pharmacy",
        "Whitfield Family Practice",
    ),
    "TRAVEL": (
        "Meridian Airways",
        "Orchard Bay Hotels",
        "Solstice Rail",
        "Wren Travel Agency",
    ),
    "EDUCATION": (
        "Halcyon Public Schools",
        "Ravenna Online Courses",
        "Lincoln Community College",
    ),
    "INSURANCE": (
        "Ashfield Insurance",
        "Bramble Mutual",
        "Corliss Assurance",
    ),
    "SUBSCRIPTION": (
        "Tessellate Media",
        "Ravenna Cloud",
        "Bluejay Audio",
        "Marlowe Streaming",
    ),
    "HOME_IMPROVEMENT": (
        "Quarry Hill Hardware",
        "Ironwood Timber",
        "Ashgrove Interiors",
    ),
    "CHILDCARE": (
        "Wren Nursery",
        "Foxglove Childcare",
        "Bramble Path Preschool",
    ),
    "PROFESSIONAL_SERVICES": (
        "Sablewood Legal",
        "Meridian Analytics",
        "Corliss Accountancy",
    ),
    "ATM_WITHDRAWAL": ("ATM Withdrawal",),
    "TRANSFER": (
        "Internal Transfer",
        "External Transfer",
    ),
    "FEES": (
        "Monthly Maintenance Fee",
        "Overdraft Fee",
        "Wire Transfer Fee",
        "Late Payment Fee",
    ),
    "LOAN_PAYMENT": ("Loan Payment",),
    "CARD_PAYMENT": ("Credit Card Payment",),
    "INVESTMENT": ("Wealth Platform Contribution",),
    "SALARY": ("Payroll Deposit",),
    "BUSINESS_REVENUE": (
        "Merchant Settlement",
        "Customer Invoice Payment",
    ),
}

# ---------------------------------------------------------------- transaction model
#: Channels a transaction can arrive through. No ``CHECK`` on the column, but a closed vocabulary
#: anyway so expense analytics can group on it.
TXN_CHANNELS: Final[tuple[tuple[str, int], ...]] = (
    ("CARD", 46),
    ("ACH", 20),
    ("ONLINE", 14),
    ("MOBILE", 10),
    ("ATM", 5),
    ("BRANCH", 3),
    ("WIRE", 2),
)

TXN_STATUS_POSTED: Final = "POSTED"
TXN_STATUS_PENDING: Final = "PENDING"
TXN_STATUS_FAILED: Final = "FAILED"

TXN_TYPE_DEBIT: Final = "DEBIT"
TXN_TYPE_CREDIT: Final = "CREDIT"


#: Discretionary spend model. ``weight`` is the share of the discretionary budget, and
#: ``(low, high)`` bounds a single transaction in cents.
#:
#: Requirement 5.9 aggregates spend by category and 7.4 needs major transactions to stand out, so
#: the
#: per-transaction ranges are wide enough that a monthly total is not simply the count times a
#: constant.
SPEND_CATEGORIES: Final[tuple[tuple[str, int, tuple[int, int]], ...]] = (
    ("GROCERIES", 22, (3_500, 28_000)),
    ("DINING", 16, (1_800, 19_000)),
    ("TRANSPORT", 12, (1_200, 14_000)),
    ("SHOPPING", 14, (2_500, 45_000)),
    ("ENTERTAINMENT", 8, (1_500, 16_000)),
    ("HEALTHCARE", 7, (2_000, 60_000)),
    ("TRAVEL", 6, (8_000, 320_000)),
    ("HOME_IMPROVEMENT", 5, (4_000, 180_000)),
    ("PROFESSIONAL_SERVICES", 4, (6_000, 90_000)),
    ("EDUCATION", 3, (5_000, 140_000)),
    ("ATM_WITHDRAWAL", 3, (4_000, 40_000)),
)

#: Seasonal multiplier per calendar month, in percent, per category group. December lifts shopping
#: and dining; summer lifts travel. Design §15 asks for seasonality so the trend chart in
#: requirement 5.9 has structure and the Financial Health Agent has something real to describe.
SEASONALITY_PERCENT: Final[dict[str, tuple[int, ...]]] = {
    # Jan  Feb  Mar  Apr  May  Jun  Jul  Aug  Sep  Oct  Nov  Dec
    "SHOPPING": (85, 80, 90, 95, 100, 100, 95, 105, 100, 110, 135, 190),
    "DINING": (85, 95, 100, 100, 105, 110, 115, 110, 100, 100, 110, 130),
    "TRAVEL": (60, 70, 90, 110, 120, 165, 185, 170, 110, 90, 95, 140),
    "UTILITIES": (135, 130, 115, 100, 90, 95, 115, 120, 100, 95, 110, 130),
    "ENTERTAINMENT": (90, 95, 100, 100, 105, 115, 120, 115, 100, 100, 105, 125),
    "EDUCATION": (120, 90, 90, 90, 85, 70, 80, 165, 150, 95, 90, 85),
}

#: Recurring monthly debits every customer with a checking account carries, as
#: ``(category, cents bounds, merchant category key)``.
RECURRING_DEBITS: Final[tuple[tuple[str, tuple[int, int]], ...]] = (
    ("UTILITIES", (8_000, 42_000)),
    ("SUBSCRIPTION", (900, 6_500)),
    ("INSURANCE", (6_000, 38_000)),
)

# ---------------------------------------------------------------- assets
VEHICLE_MAKES: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("Toyota", ("Corolla", "RAV4", "Highlander", "Camry")),
    ("Honda", ("Civic", "CR-V", "Accord", "Pilot")),
    ("Ford", ("F-150", "Escape", "Explorer", "Maverick")),
    ("Subaru", ("Outback", "Forester", "Crosstrek")),
    ("Volkswagen", ("Golf", "Tiguan", "Passat")),
    ("Volvo", ("XC60", "XC90", "S60")),
    ("Tesla", ("Model 3", "Model Y")),
    ("BMW", ("3 Series", "X3", "X5")),
)

#: Free-text descriptions for the ``OTHER`` asset type, which HNW customers get.
OTHER_ASSET_DESCRIPTIONS: Final[tuple[str, ...]] = (
    "Fine art collection",
    "Private equity interest",
    "Restricted share units",
    "Numismatic collection",
    "Marine vessel",
    "Vineyard partnership interest",
)

# ---------------------------------------------------------------- engagement
DEVICE_TYPES: Final[tuple[tuple[str, int], ...]] = (
    ("IOS_PHONE", 32),
    ("ANDROID_PHONE", 28),
    ("DESKTOP_CHROME", 20),
    ("DESKTOP_SAFARI", 8),
    ("TABLET", 7),
    ("BRANCH_TERMINAL", 5),
)

#: Benign service-request and complaint notes. The adversarial counterparts live in
#: :mod:`c360.generator.adversarial` so the injected strings are enumerable from one place.
SERVICE_NOTES: Final[tuple[str, ...]] = (
    "Customer asked about the statement cycle date.",
    "Requested a replacement debit card after loss.",
    "Queried a duplicate subscription charge; resolved as merchant re-authorization.",
    "Asked to update mailing preferences to paperless.",
    "Reported difficulty completing the mobile login step.",
    "Requested an increase to the daily transfer limit.",
    "Asked for an explanation of the monthly maintenance fee.",
    "Wanted to add a joint party to the savings account.",
)

# ---------------------------------------------------------------- campaigns and offers
#: ``(campaign_name, business_group)``. Business group is a
#: :class:`c360.domain.enums.BusinessGroup` value; kept as a plain string here so this module does
#: not need the import for a value it only passes through.
CAMPAIGNS: Final[tuple[tuple[str, str], ...]] = (
    ("Spring Deposit Growth", "DEPOSITS"),
    ("Home Lending Refresh", "LENDING"),
    ("Card Activation Drive", "CARDS"),
    ("Wealth Advisory Outreach", "WEALTH"),
    ("Protection Review", "INSURANCE"),
    ("Small Business Momentum", "BUSINESS"),
    ("Digital Adoption Push", "DEPOSITS"),
    ("Premier Upgrade Wave", "CARDS"),
)

#: Telephone area codes, drawn independently of the city so no complete plausible-real number is
#: assembled. The exchange is always ``555`` and the line number always ``01xx``, the block reserved
#: for fictional use, so a seeded number cannot reach anyone.
AREA_CODES: Final[tuple[str, ...]] = (
    "614",
    "512",
    "503",
    "919",
    "303",
    "608",
    "480",
    "401",
    "423",
    "208",
    "585",
    "559",
    "402",
    "509",
    "330",
    "318",
)

#: Household name forms. ``{family}`` is substituted with the household's shared family name.
HOUSEHOLD_NAME_FORMS: Final[tuple[str, ...]] = (
    "{family} Household",
    "{family} Family",
    "The {family} Household",
)
