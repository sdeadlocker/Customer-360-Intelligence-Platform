"""Derived-value, search-index and graph recompute job (tasks 3.1, 3.2, 3.3, 3.4, 3.5).

Everything this module writes is a projection of the source tables migrations ``0001``-``0003``
hold. It writes nothing the generator authors: derived financial and credit totals are summed from
the actual account, asset and transaction rows; the health score is the ``fhs-v1`` heuristic
(:mod:`c360.domain.health`); the FTS5 index is denormalized identifiers; and the graph is the
relational data reshaped into nodes and edges. Design §14.6 is the governing rule — *derived values
are stored for read speed but always recomputable* — and task 3.1's stored-equals-computed test is
its executable form.

Idempotence
-----------

Every stage is ``DELETE`` then ``INSERT`` inside one transaction, so running the job twice leaves
the same rows and running it after new source data leaves the projection consistent with that data.
The graph uses ``INSERT`` into freshly-cleared tables and relies on ``graph_edge``'s
``UNIQUE (src_id, dst_id, edge_type)`` to make a double-projection impossible even if a bug emitted
an edge twice.

The write pattern mirrors the loader
-------------------------------------

Like :mod:`c360.generator.loader`, this opens the database read-write, does all its work in a single
transaction, runs ``PRAGMA foreign_key_check`` before committing, and checkpoints the WAL
afterwards. Unlike the loader it does **not** relax ``synchronous``: the loader can, because a fresh
seed is disposable, but recompute runs against a live database whose other content must survive an
interruption. Recompute is the brief writer window design §12.3 says the read-only API waits on.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from c360.core.logging import get_logger
from c360.data.engine import AccessMode, DatabaseFileMissingError, create_sqlite_engine
from c360.domain.health import HealthInputs, compute_health_score
from c360.domain.money import Cents

_logger = get_logger(__name__)

#: Tables the job owns, in the order they are cleared. Graph adjacency/subgraph reference the edge
#: and node tables, so they are cleared first; the FTS5 and derived tables stand alone.
_MANAGED_TABLES: Final[tuple[str, ...]] = (
    "graph_household_subgraph",
    "graph_adjacency",
    "graph_edge",
    "graph_node",
    "customer_search",
    "derived_health",
    "derived_credit",
    "derived_financial",
)


@dataclass(frozen=True, slots=True)
class RecomputeReport:
    """Per-stage row counts and elapsed time. Returned for the CLI and asserted on by tests."""

    database: Path
    row_counts: dict[str, int]
    elapsed_seconds: float

    @property
    def total_rows(self) -> int:
        return sum(self.row_counts.values())

    def summary_lines(self) -> list[str]:
        """Human-readable summary, for the CLI."""
        lines = [
            f"database      {self.database}",
            f"elapsed       {self.elapsed_seconds:.2f}s",
            "",
            "projected:",
        ]
        lines.extend(f"  {name:<26}{count:>9,}" for name, count in self.row_counts.items() if count)
        return lines


class RecomputeError(RuntimeError):
    """Raised when the recompute fails a referential-integrity check after writing."""


def _as_of(cursor: Any) -> str:
    """The dataset's ``as_of_date``. Read from ``customer`` so the projection dates match the data.

    Every source row carries the same ``as_of_date`` in this build; taking the max is a defensive
    choice that also gives a sensible answer if that ever stops being true.
    """
    row = cursor.execute("SELECT max(as_of_date) FROM customer").fetchone()
    if row is None or row[0] is None:
        # An empty database. today() is only ever used to satisfy the NOT NULL/date CHECK on the
        # (zero) rows that follow, so it is inert.
        return datetime.now(UTC).date().isoformat()
    return str(row[0])


# ============================================================ derived financial (task 3.1)
def _recompute_financial(cursor: Any, *, as_of: str, computed_at: str) -> int:
    """Write ``derived_financial`` from account, asset and transaction rows (design §4.6).

    Formulas (all integer cents):
      total_assets      = Σ deposit balances + Σ investment portfolio values + Σ asset values
      total_liabilities = Σ loan balances + Σ card balances
      net_worth         = total_assets - total_liabilities
      monthly_income    = Σ positive txn amounts in the trailing month
      monthly_expense   = Σ |negative txn amounts| in the trailing month

    household_net_worth is a second pass once every member's net worth is known.
    """
    customers = [str(row[0]) for row in cursor.execute("SELECT customer_id FROM customer")]

    deposits = _sum_by_customer(
        cursor,
        "SELECT a.customer_id, a.balance_cents FROM account a "
        "WHERE a.account_type = 'DEPOSIT' AND a.account_status != 'CLOSED'",
    )
    investments = _sum_by_customer(
        cursor,
        "SELECT a.customer_id, i.portfolio_value_cents FROM account a "
        "JOIN investment i ON i.account_id = a.account_id "
        "WHERE a.account_status != 'CLOSED'",
    )
    asset_values = _sum_by_customer(cursor, "SELECT customer_id, current_value_cents FROM asset")
    loans = _sum_by_customer(
        cursor,
        "SELECT a.customer_id, a.balance_cents FROM account a "
        "WHERE a.account_type = 'LOAN' AND a.account_status != 'CLOSED'",
    )
    cards = _sum_by_customer(
        cursor,
        "SELECT a.customer_id, a.balance_cents FROM account a "
        "WHERE a.account_type = 'CARD' AND a.account_status != 'CLOSED'",
    )

    # Trailing month is the 30 days up to as_of. Income is positive txns, expense is the absolute
    # value of negative ones — design §4.6 defines monthly_expense as Σ negative amounts.
    income = _sum_by_customer(
        cursor,
        "SELECT customer_id, amount_cents FROM txn "
        "WHERE amount_cents > 0 AND transaction_date > date(:as_of, '-1 month')",
        {"as_of": as_of},
    )
    expense = _sum_by_customer(
        cursor,
        "SELECT customer_id, -amount_cents FROM txn "
        "WHERE amount_cents < 0 AND transaction_date > date(:as_of, '-1 month')",
        {"as_of": as_of},
    )

    net_worth: dict[str, int] = {}
    rows: list[tuple[Any, ...]] = []
    for customer_id in customers:
        total_assets = (
            deposits.get(customer_id, 0)
            + investments.get(customer_id, 0)
            + asset_values.get(customer_id, 0)
        )
        total_liabilities = loans.get(customer_id, 0) + cards.get(customer_id, 0)
        worth = total_assets - total_liabilities
        net_worth[customer_id] = worth
        rows.append(
            (
                customer_id,
                deposits.get(customer_id, 0),
                loans.get(customer_id, 0),
                investments.get(customer_id, 0),
                total_assets,
                total_liabilities,
                worth,
                None,  # household_net_worth, filled below
                income.get(customer_id) if customer_id in income else None,
                expense.get(customer_id) if customer_id in expense else None,
                computed_at,
                as_of,
            )
        )

    # household_net_worth = Σ member net_worth. A customer with no household keeps their own.
    household_totals = _household_net_worth(cursor, net_worth)
    household_of = {
        str(cid): (str(hid) if hid is not None else None)
        for cid, hid in cursor.execute("SELECT customer_id, household_id FROM customer")
    }
    finalized = [
        (
            *row[:7],
            _household_worth_for(row[0], household_of, household_totals, net_worth),
            *row[8:],
        )
        for row in rows
    ]

    cursor.executemany(
        "INSERT INTO derived_financial ("
        "customer_id, total_deposits_cents, total_loans_cents, total_investments_cents, "
        "total_assets_cents, total_liabilities_cents, net_worth_cents, household_net_worth_cents, "
        "monthly_income_cents, monthly_expense_cents, computed_at, as_of_date) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        finalized,
    )
    return len(finalized)


def _household_worth_for(
    customer_id: str,
    household_of: dict[str, str | None],
    household_totals: dict[str, int],
    net_worth: dict[str, int],
) -> int:
    household_id = household_of.get(customer_id)
    if household_id is None:
        return net_worth.get(customer_id, 0)
    return household_totals.get(household_id, net_worth.get(customer_id, 0))


def _household_net_worth(cursor: Any, net_worth: dict[str, int]) -> dict[str, int]:
    """Σ member net_worth per household, using household_member for membership."""
    totals: dict[str, int] = {}
    for household_id, member_id in cursor.execute(
        "SELECT household_id, customer_id FROM household_member"
    ):
        totals[str(household_id)] = totals.get(str(household_id), 0) + net_worth.get(
            str(member_id), 0
        )
    return totals


# ============================================================ derived credit (task 3.1)
def _recompute_credit(cursor: Any, *, as_of: str, computed_at: str) -> int:
    """Write ``derived_credit`` (design §4.6).

    credit_exposure   = Σ loan balances + Σ card limits
    utilization_bps   = Σ card balances x 10000 / Σ card limits (truncating, as SQL divides)
    """
    customers = [str(row[0]) for row in cursor.execute("SELECT customer_id FROM customer")]

    loan_balances = _sum_by_customer(
        cursor,
        "SELECT a.customer_id, a.balance_cents FROM account a "
        "WHERE a.account_type = 'LOAN' AND a.account_status != 'CLOSED'",
    )
    card_limits = _sum_by_customer(
        cursor,
        "SELECT a.customer_id, c.credit_limit_cents FROM account a "
        "JOIN credit_card c ON c.account_id = a.account_id "
        "WHERE a.account_status != 'CLOSED'",
    )
    card_balances = _sum_by_customer(
        cursor,
        "SELECT a.customer_id, a.balance_cents FROM account a "
        "JOIN credit_card c ON c.account_id = a.account_id "
        "WHERE a.account_status != 'CLOSED'",
    )

    rows: list[tuple[Any, ...]] = []
    for customer_id in customers:
        limit = card_limits.get(customer_id, 0)
        balance = card_balances.get(customer_id, 0)
        exposure = loan_balances.get(customer_id, 0) + limit
        # Truncating division to match design §4.6's SQL expression and the per-card utilization the
        # generator stored (see Cents.ratio_bps).
        utilization = int(Cents(balance).ratio_bps(Cents(limit))) if limit > 0 else None
        rows.append((customer_id, exposure, utilization, limit, balance, computed_at, as_of))

    cursor.executemany(
        "INSERT INTO derived_credit ("
        "customer_id, credit_exposure_cents, credit_utilization_bps, total_credit_limit_cents, "
        "total_card_balance_cents, computed_at, as_of_date) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


# ============================================================ financial health (task 3.2)
_DELINQUENCY_PAYMENT_SCORE: Final[dict[str, int]] = {
    "CURRENT": 100,
    "DPD_1_29": 80,
    "DPD_30_59": 55,
    "DPD_60_89": 30,
    "DPD_90_PLUS": 5,
}


def _recompute_health(cursor: Any, *, as_of: str, computed_at: str) -> int:
    """Write ``derived_health`` — the fhs-v1 score with drivers (design §7.3).

    Runs after ``derived_financial`` and ``derived_credit`` so the score reads its inputs from the
    freshly-computed projection rather than the generator's ``financial_profile``.
    """
    financial = {
        str(row[0]): row
        for row in cursor.execute(
            "SELECT customer_id, monthly_income_cents, monthly_expense_cents, total_deposits_cents "
            "FROM derived_financial"
        )
    }
    credit = {
        str(row[0]): row[1]
        for row in cursor.execute("SELECT customer_id, credit_utilization_bps FROM derived_credit")
    }
    # Monthly debt payment from loan EMIs; delinquency status drives the payment-history factor.
    emis = _sum_by_customer(
        cursor,
        "SELECT a.customer_id, l.monthly_emi_cents FROM account a "
        "JOIN loan l ON l.account_id = a.account_id "
        "WHERE a.account_status != 'CLOSED' AND l.monthly_emi_cents IS NOT NULL",
    )
    delinquency = {
        str(row[0]): row[1]
        for row in cursor.execute("SELECT customer_id, delinquency_status FROM risk_profile")
    }

    rows: list[tuple[Any, ...]] = []
    for customer_id, income, expense, deposits in financial.values():
        status = delinquency.get(customer_id)
        payment_score = _DELINQUENCY_PAYMENT_SCORE.get(status) if status is not None else None
        inputs = HealthInputs(
            monthly_income_cents=Cents(income) if income is not None else None,
            monthly_expense_cents=Cents(expense) if expense is not None else None,
            monthly_debt_payment_cents=(
                Cents(emis[customer_id]) if customer_id in emis else Cents(0)
            ),
            liquid_savings_cents=Cents(deposits),
            credit_utilization_bps=credit.get(customer_id),
            payment_history_score=payment_score,
        )
        score = compute_health_score(inputs)
        drivers_json = json.dumps(
            [
                {"factor": d.factor, "contribution": d.contribution, "detail": d.detail}
                for d in score.drivers
            ]
        )
        rows.append(
            (
                customer_id,
                score.value,
                score.band.value,
                score.provenance,
                score.formula_version,
                drivers_json,
                computed_at,
                as_of,
            )
        )

    cursor.executemany(
        "INSERT INTO derived_health ("
        "customer_id, value, band, provenance, formula_version, drivers, computed_at, as_of_date) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


# ============================================================ FTS5 search (task 3.3)
def _recompute_search(cursor: Any) -> int:
    """Populate ``customer_search`` (design §4.4).

    Identifier columns are denormalized so one MATCH satisfies every §3.1 search key: name, email,
    phone, mobile, account number, loan number, card last-4, city. Numbers for a customer are joined
    into a single space-separated cell so the FTS5 tokenizer indexes each one.
    """
    account_numbers = _concat_by_customer(
        cursor,
        "SELECT customer_id, account_number FROM account WHERE account_status != 'CLOSED'",
    )
    loan_numbers = _concat_by_customer(
        cursor,
        "SELECT a.customer_id, l.loan_number FROM account a "
        "JOIN loan l ON l.account_id = a.account_id WHERE a.account_status != 'CLOSED'",
    )
    card_last4 = _concat_by_customer(
        cursor,
        "SELECT a.customer_id, c.card_last4 FROM account a "
        "JOIN credit_card c ON c.account_id = a.account_id WHERE a.account_status != 'CLOSED'",
    )
    contacts = {
        str(row[0]): row
        for row in cursor.execute(
            "SELECT customer_id, email, phone_number, mobile_number, city FROM contact_info"
        )
    }

    rows: list[tuple[Any, ...]] = []
    for customer_id, customer_name, segment in cursor.execute(
        "SELECT customer_id, customer_name, customer_segment FROM customer"
    ):
        cid = str(customer_id)
        contact = contacts.get(cid)
        email = contact[1] if contact else None
        phone = contact[2] if contact else None
        mobile = contact[3] if contact else None
        city = contact[4] if contact else None
        rows.append(
            (
                cid,
                customer_name,
                email,
                phone,
                mobile,
                account_numbers.get(cid, ""),
                loan_numbers.get(cid, ""),
                card_last4.get(cid, ""),
                city,
                segment,
            )
        )

    cursor.executemany(
        "INSERT INTO customer_search ("
        "customer_id, customer_name, email, phone_number, mobile_number, "
        "account_numbers, loan_numbers, card_last4, city, segment) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


# ============================================================ graph projection (tasks 3.4, 3.5)
#: (node_type, id-prefix). The prefix makes node_id self-describing: "CUSTOMER:C00042".
_NODE_PREFIX: Final[dict[str, str]] = {
    "Customer": "CUSTOMER",
    "Account": "ACCOUNT",
    "Loan": "LOAN",
    "Deposit": "DEPOSIT",
    "Card": "CARD",
    "Investment": "INVESTMENT",
    "Property": "PROPERTY",
    "Vehicle": "VEHICLE",
    "Offer": "OFFER",
    "Application": "APPLICATION",
    "Transaction": "TRANSACTION",
    "Event": "EVENT",
    "LifeEvent": "LIFEEVENT",
    "Household": "HOUSEHOLD",
    "Organization": "ORGANIZATION",
    "Employer": "EMPLOYER",
}


def _node_id(node_type: str, entity_id: str) -> str:
    return f"{_NODE_PREFIX[node_type]}:{entity_id}"


#: account_type -> (node_type, ownership edge). OWNS is emitted alongside for every holding.
_ACCOUNT_SPECIALIZATION: Final[dict[str, tuple[str, str]]] = {
    "DEPOSIT": ("Deposit", "HAS_ACCOUNT"),
    "LOAN": ("Loan", "HAS_LOAN"),
    "CARD": ("Card", "HAS_CARD"),
    "INVESTMENT": ("Investment", "HAS_INVESTMENT"),
}


class _GraphBuilder:
    """Accumulates nodes and edges, deduplicating both, before a single batched write.

    Nodes dedupe by ``node_id`` (a repeated employer or household is one node); edges dedupe on the
    ``(src, dst, edge_type)`` key that ``graph_edge`` declares UNIQUE, so the projection is
    idempotent by construction rather than relying on the constraint to reject a double insert.
    """

    __slots__ = ("_edges", "_nodes")

    def __init__(self) -> None:
        self._nodes: dict[str, tuple[str, str, str, str]] = {}
        self._edges: dict[tuple[str, str, str], tuple[str, int, float | None]] = {}

    def add_node(self, node_type: str, entity_id: str, label: str, props: dict[str, Any]) -> str:
        nid = _node_id(node_type, entity_id)
        self._nodes.setdefault(nid, (node_type, entity_id, label, json.dumps(props)))
        return nid

    def add_edge(
        self,
        src: str,
        dst: str,
        edge_type: str,
        *,
        props: dict[str, Any] | None = None,
        is_inferred: bool = False,
        confidence: float | None = None,
    ) -> None:
        self._edges.setdefault(
            (src, dst, edge_type),
            (json.dumps(props or {}), 1 if is_inferred else 0, confidence),
        )

    def node_rows(self) -> list[tuple[str, str, str, str, str]]:
        return [
            (nid, node_type, entity_id, label, props)
            for nid, (node_type, entity_id, label, props) in self._nodes.items()
        ]

    def edge_rows(self) -> list[tuple[str, str, str, str, int, float | None]]:
        return [
            (src, dst, edge_type, props, is_inferred, confidence)
            for (src, dst, edge_type), (props, is_inferred, confidence) in self._edges.items()
        ]


def _build_principal_nodes(cursor: Any, graph: _GraphBuilder) -> None:
    """Customers, employers, households and the two edges that anchor a customer to each."""
    for cid, name, segment, employer_id, household_id in cursor.execute(
        "SELECT customer_id, customer_name, customer_segment, employer_id, household_id "
        "FROM customer"
    ):
        customer_node = graph.add_node("Customer", str(cid), str(name), {"segment": str(segment)})
        if employer_id is not None:
            graph.add_edge(customer_node, _node_id("Employer", str(employer_id)), "WORKS_FOR")
        if household_id is not None:
            graph.add_edge(
                customer_node, _node_id("Household", str(household_id)), "PART_OF_HOUSEHOLD"
            )
    for eid, name in cursor.execute("SELECT employer_id, employer_name FROM employer"):
        graph.add_node("Employer", str(eid), str(name), {})
    for hid, name in cursor.execute("SELECT household_id, household_name FROM household"):
        graph.add_node("Household", str(hid), str(name), {})


def _build_holding_nodes(cursor: Any, graph: _GraphBuilder) -> None:
    """Accounts, their specializations and property/vehicle assets, each OWNed by a customer."""
    for aid, cid, acct_type, product_name in cursor.execute(
        "SELECT account_id, customer_id, account_type, product_name FROM account"
    ):
        node_type, edge_type = _ACCOUNT_SPECIALIZATION[str(acct_type)]
        label = str(product_name) if product_name is not None else str(acct_type)
        node_id = graph.add_node(node_type, str(aid), label, {"account_type": str(acct_type)})
        customer_node = _node_id("Customer", str(cid))
        graph.add_edge(customer_node, node_id, "OWNS")
        graph.add_edge(customer_node, node_id, edge_type)

    property_ids = {str(row[0]) for row in cursor.execute("SELECT asset_id FROM property")}
    vehicle_ids = {str(row[0]) for row in cursor.execute("SELECT asset_id FROM vehicle")}
    for asset_id, cid, asset_type, description in cursor.execute(
        "SELECT asset_id, customer_id, asset_type, asset_description FROM asset"
    ):
        aid = str(asset_id)
        label = str(description) if description is not None else str(asset_type)
        if aid in property_ids:
            node_id = graph.add_node("Property", aid, label, {})
            edge_type = "HAS_PROPERTY"
        elif aid in vehicle_ids:
            node_id = graph.add_node("Vehicle", aid, label, {})
            edge_type = "HAS_VEHICLE"
        else:
            # 'OTHER' assets have no node type in the §5.1 set (the 16 types include Property and
            # Vehicle but no generic asset node), so they are not projected as graph nodes.
            continue
        graph.add_edge(_node_id("Customer", str(cid)), node_id, edge_type)


def _build_activity_nodes(cursor: Any, graph: _GraphBuilder) -> None:
    """Applications, offers, life events and engagement events, and their edges to the customer."""
    for app_id, cid, product in cursor.execute(
        "SELECT application_id, customer_id, product_applied FROM application"
    ):
        node_id = graph.add_node("Application", str(app_id), str(product), {})
        graph.add_edge(_node_id("Customer", str(cid)), node_id, "APPLIED_FOR")

    for offer_id, name in cursor.execute("SELECT offer_id, offer_name FROM offer"):
        graph.add_node("Offer", str(offer_id), str(name), {})
    for cid, offer_id, reaction in cursor.execute(
        "SELECT customer_id, offer_id, customer_reaction FROM customer_offer"
    ):
        offer_node = _node_id("Offer", str(offer_id))
        customer_node = _node_id("Customer", str(cid))
        graph.add_edge(customer_node, offer_node, "RECEIVED_OFFER")
        if str(reaction) == "ACCEPTED":
            graph.add_edge(customer_node, offer_node, "ACCEPTED_OFFER")

    for le_id, cid, le_type in cursor.execute(
        "SELECT life_event_id, customer_id, life_event_type FROM life_event"
    ):
        node_id = graph.add_node("LifeEvent", str(le_id), str(le_type), {})
        graph.add_edge(_node_id("Customer", str(cid)), node_id, "GENERATED_EVENT")
    for ev_id, cid, ev_type in cursor.execute(
        "SELECT event_id, customer_id, event_type FROM customer_event"
    ):
        node_id = graph.add_node("Event", str(ev_id), str(ev_type), {})
        graph.add_edge(_node_id("Customer", str(cid)), node_id, "GENERATED_EVENT")


def _build_relationship_edges(cursor: Any, graph: _GraphBuilder) -> None:
    """Customer-to-customer RELATED_TO edges, carrying the inferred/confidence distinction."""
    for from_id, to_id, rel_type, is_inferred, confidence in cursor.execute(
        "SELECT from_customer_id, to_customer_id, relationship_type, is_inferred, confidence "
        "FROM customer_relationship"
    ):
        graph.add_edge(
            _node_id("Customer", str(from_id)),
            _node_id("Customer", str(to_id)),
            "RELATED_TO",
            props={"relationship_type": str(rel_type)},
            is_inferred=bool(is_inferred),
            confidence=confidence,
        )


def _recompute_graph(cursor: Any) -> tuple[int, int, int, int]:
    """Project ``graph_node``, ``graph_edge``, ``graph_adjacency`` and household subgraphs.

    Returns (nodes, edges, adjacency_rows, subgraph_rows). Nodes are emitted for every entity type
    present; edges connect them per the 15 relationships in design §5.1. Every edge is then
    materialized OUT and IN in ``graph_adjacency`` (design §5.2), and household membership is
    precomputed into ``graph_household_subgraph``.
    """
    graph = _GraphBuilder()
    _build_principal_nodes(cursor, graph)
    _build_holding_nodes(cursor, graph)
    _build_activity_nodes(cursor, graph)
    _build_relationship_edges(cursor, graph)

    node_rows = graph.node_rows()
    cursor.executemany(
        "INSERT INTO graph_node (node_id, node_type, entity_id, label, props) "
        "VALUES (?, ?, ?, ?, ?)",
        node_rows,
    )
    edge_rows = graph.edge_rows()
    cursor.executemany(
        "INSERT INTO graph_edge (src_id, dst_id, edge_type, props, is_inferred, confidence) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        edge_rows,
    )

    adjacency_rows = _project_adjacency(cursor)
    subgraph_rows = _project_household_subgraphs(cursor)
    return len(node_rows), len(edge_rows), adjacency_rows, subgraph_rows


def _project_adjacency(cursor: Any) -> int:
    """Materialize every logical edge OUT (from src) and IN (from dst); design §5.2.

    Reads the edges back from ``graph_edge`` so each adjacency row carries the real ``edge_id`` for
    tracing a hop back to its logical edge.
    """
    edges = cursor.execute(
        "SELECT edge_id, src_id, dst_id, edge_type, is_inferred, confidence FROM graph_edge"
    ).fetchall()
    rows: list[tuple[Any, ...]] = []
    for edge_id, src_id, dst_id, edge_type, is_inferred, confidence in edges:
        # OUT: from src, you reach dst. IN: from dst, you reach src.
        rows.append((src_id, dst_id, edge_type, "OUT", edge_id, is_inferred, confidence))
        rows.append((dst_id, src_id, edge_type, "IN", edge_id, is_inferred, confidence))
    cursor.executemany(
        "INSERT INTO graph_adjacency ("
        "src_id, dst_id, edge_type, direction, edge_id, is_inferred, confidence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def _project_household_subgraphs(cursor: Any) -> int:
    """Precompute each household's member customer nodes at depth 1 (design §5.2).

    The household node itself is depth 0; each member customer is depth 1. This is the frequent
    traversal the design caches so the household view is an indexed read.
    """
    rows: list[tuple[str, str, int]] = []
    for household_id in cursor.execute("SELECT household_id FROM household"):
        hid = str(household_id[0])
        rows.append((hid, _node_id("Household", hid), 0))
    for household_id, customer_id in cursor.execute(
        "SELECT household_id, customer_id FROM household_member"
    ):
        rows.append((str(household_id), _node_id("Customer", str(customer_id)), 1))
    # Dedup: a customer could theoretically appear twice; the PK would reject it, so guard here.
    seen: set[tuple[str, str]] = set()
    unique_rows = []
    for hid, node_id, depth in rows:
        if (hid, node_id) in seen:
            continue
        seen.add((hid, node_id))
        unique_rows.append((hid, node_id, depth))
    cursor.executemany(
        "INSERT INTO graph_household_subgraph (household_id, node_id, depth) VALUES (?, ?, ?)",
        unique_rows,
    )
    return len(unique_rows)


# ============================================================ helpers
def _sum_by_customer(
    cursor: Any, query: str, params: dict[str, Any] | None = None
) -> dict[str, int]:
    """Sum the second column grouped by the first (customer_id). Exact integer addition."""
    totals: dict[str, int] = {}
    for customer_id, value in cursor.execute(query, params or {}):
        if value is None:
            continue
        totals[str(customer_id)] = totals.get(str(customer_id), 0) + int(value)
    return totals


def _concat_by_customer(cursor: Any, query: str) -> dict[str, str]:
    """Join the second column into a space-separated string grouped by the first (customer_id)."""
    grouped: dict[str, list[str]] = {}
    for customer_id, value in cursor.execute(query):
        if value is None:
            continue
        grouped.setdefault(str(customer_id), []).append(str(value))
    return {cid: " ".join(values) for cid, values in grouped.items()}


# ============================================================ orchestration (task 3.6 support)
def recompute(database: Path) -> RecomputeReport:
    """Rebuild every derived table, the search index and the graph projection for ``database``.

    Idempotent: each managed table is cleared and rewritten inside one transaction. The database
    must already be migrated to a revision that includes the Phase 3 tables.

    Raises:
        RecomputeError: a foreign-key violation survived the projection.
    """
    if not database.resolve().is_file():
        # READ_WRITE (not _CREATE) would surface a missing file as a bare sqlite3.OperationalError
        # deep in the pool. Name it the way a read-only open does, so the CLI and the endpoint get a
        # single, catchable "seed it first" error rather than a driver-level one.
        raise DatabaseFileMissingError(
            f"cannot recompute {database.resolve()}: file does not exist. Seed it first."
        )

    started = time.perf_counter()
    computed_at = datetime.now(UTC).isoformat()
    engine = create_sqlite_engine(database, mode=AccessMode.READ_WRITE, pool_size=1)
    connection = engine.raw_connection()
    counts: dict[str, int] = {}
    try:
        cursor = connection.cursor()
        as_of = _as_of(cursor)

        # Clear first, in child-before-parent order, so the rewrite is idempotent.
        for table in _MANAGED_TABLES:
            cursor.execute(f"DELETE FROM {table}")  # noqa: S608 - constant table names

        counts["derived_financial"] = _recompute_financial(
            cursor, as_of=as_of, computed_at=computed_at
        )
        counts["derived_credit"] = _recompute_credit(cursor, as_of=as_of, computed_at=computed_at)
        counts["derived_health"] = _recompute_health(cursor, as_of=as_of, computed_at=computed_at)
        counts["customer_search"] = _recompute_search(cursor)

        node_count, edge_count, adjacency_count, subgraph_count = _recompute_graph(cursor)
        counts["graph_node"] = node_count
        counts["graph_edge"] = edge_count
        counts["graph_adjacency"] = adjacency_count
        counts["graph_household_subgraph"] = subgraph_count

        violations = cursor.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            connection.rollback()
            tables = sorted({str(row[0]) for row in violations})
            raise RecomputeError(
                f"{len(violations)} foreign-key violation(s) after recompute, in: "
                f"{', '.join(tables)}."
            )

        connection.commit()
        cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # Refresh planner statistics so the freshly-populated indexes are used (design §4.5).
        cursor.execute("ANALYZE")
        cursor.close()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
        engine.dispose()

    elapsed = time.perf_counter() - started
    _logger.info(
        "recompute complete",
        extra={
            "database": str(database),
            "rows": sum(counts.values()),
            "elapsed_seconds": round(elapsed, 3),
        },
    )
    return RecomputeReport(database=database, row_counts=counts, elapsed_seconds=elapsed)
