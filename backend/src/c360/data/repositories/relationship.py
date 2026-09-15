"""SQLite adapter for :class:`~c360.domain.ports.RelationshipRepository` (task 1.6).

Relational reads only. Graph traversal has its own port over the projected ``graph_adjacency`` table
(design §5.2, §5.3) and arrives with the projection in Phase 3 — a recursive CTE over these tables
directly would be the repeated full scan §5.2 exists to avoid.

Redaction of non-entitled customer nodes (requirement 6.4) is not here either. Design §5.4 puts it
in
the service layer so the dashboard and Q&A share one implementation of it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from c360.data.repositories.base import (
    SqliteRepository,
    Statement,
    to_models,
)
from c360.domain.enums import AssetType
from c360.domain.models import (
    AccountParty,
    Asset,
    AssetDetail,
    AssetHolding,
    Beneficiary,
    CustomerRelationship,
    HouseholdMember,
    PropertyDetail,
    VehicleDetail,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from c360.domain.models import SourcedModel

_HOUSEHOLD_MEMBERS = Statement(
    id="relationship.household_members",
    collection="household_member",
    sql="""
    SELECT hm.household_id, hm.customer_id, hm.member_role, hm.joined_date, hm.as_of_date,
           c.source_system
    FROM household_member hm
    JOIN customer c ON c.customer_id = hm.customer_id
    WHERE hm.household_id = :household_id
    ORDER BY hm.member_role, hm.customer_id
    """,
)

_HOUSEHOLD_FOR_CUSTOMER = Statement(
    id="relationship.household_for_customer",
    collection="household_member",
    # `customer.household_id` and `household_member` can both answer this. `household_member` is the
    # membership record with a role and a joined date; `customer.household_id` is the denormalized
    # convenience copy. Reading the membership table and falling back keeps the answer right when
    # the
    # two disagree, and makes the fallback visible rather than a silent preference.
    sql="""
    SELECT COALESCE(
             (SELECT hm.household_id FROM household_member hm WHERE hm.customer_id = :customer_id),
             (SELECT c.household_id FROM customer c WHERE c.customer_id = :customer_id)
           )
    """,
)

_RELATIONSHIPS = Statement(
    id="relationship.list",
    collection="customer_relationship",
    # Both directions, stored orientation preserved. See the port docstring for why normalizing the
    # direction would corrupt the label on asymmetric relationship types.
    sql="""
    SELECT cr.relationship_id, cr.from_customer_id, cr.to_customer_id, cr.relationship_type,
           cr.is_inferred, cr.confidence, cr.inference_basis, cr.source_system, cr.as_of_date
    FROM customer_relationship cr
    WHERE cr.from_customer_id = :customer_id OR cr.to_customer_id = :customer_id
    ORDER BY cr.is_inferred, cr.relationship_type, cr.relationship_id
    """,
)

_ACCOUNT_PARTIES = Statement(
    id="relationship.account_parties",
    collection="account_party",
    sql="""
    SELECT ap.account_id, ap.customer_id, ap.party_role, ap.ownership_bps, ap.added_date,
           ap.as_of_date, c.source_system
    FROM account_party ap
    JOIN customer c ON c.customer_id = ap.customer_id
    WHERE ap.account_id = :account_id
    ORDER BY ap.party_role, ap.customer_id
    """,
)

_JOINT_ACCOUNTS = Statement(
    id="relationship.joint_accounts",
    collection="account_party",
    # Requirement 6.1 asks for joint account holders. A row where this customer is the sole PRIMARY
    # party is just their own account, so the filter is "party to an account that has more than one
    # party" rather than "party_role <> 'PRIMARY'" — a customer can be the primary holder of an
    # account that also has a joint holder, and that account is exactly what the requirement wants.
    sql="""
    SELECT ap.account_id, ap.customer_id, ap.party_role, ap.ownership_bps, ap.added_date,
           ap.as_of_date, c.source_system
    FROM account_party ap
    JOIN customer c ON c.customer_id = ap.customer_id
    WHERE ap.customer_id = :customer_id
      AND (SELECT COUNT(*) FROM account_party other
           WHERE other.account_id = ap.account_id) > 1
    ORDER BY ap.account_id, ap.party_role
    """,
)

_BENEFICIARIES = Statement(
    id="relationship.beneficiaries",
    collection="beneficiary",
    sql="""
    SELECT b.beneficiary_id, b.account_id, b.beneficiary_customer_id, b.beneficiary_name,
           b.relationship, b.share_bps, b.is_inferred, b.confidence, b.as_of_date, c.source_system
    FROM beneficiary b
    JOIN account a ON a.account_id = b.account_id
    JOIN customer c ON c.customer_id = a.customer_id
    WHERE a.customer_id = :customer_id
    ORDER BY b.account_id, b.share_bps DESC, b.beneficiary_id
    """,
)

_ASSETS = Statement(
    id="relationship.assets",
    collection="asset",
    sql="""
    SELECT asset_id, customer_id, asset_type, asset_description, current_value_cents,
           ownership_type, acquired_date, is_collateral, as_of_date, source_system
    FROM asset
    WHERE customer_id = :customer_id
    ORDER BY asset_type, asset_id
    """,
)

_PROPERTIES = Statement(
    id="relationship.asset_property",
    collection="property",
    sql="""
    SELECT p.asset_id, p.property_type, p.address_line1, p.city, p.state, p.postal_code,
           p.purchase_price_cents, p.assessed_value_cents, p.square_feet, p.year_built
    FROM property p
    JOIN asset s ON s.asset_id = p.asset_id
    WHERE s.customer_id = :customer_id
    """,
)

_VEHICLES = Statement(
    id="relationship.asset_vehicle",
    collection="vehicle",
    sql="""
    SELECT v.asset_id, v.make, v.model, v.model_year, v.vin, v.mileage, v.purchase_price_cents
    FROM vehicle v
    JOIN asset s ON s.asset_id = v.asset_id
    WHERE s.customer_id = :customer_id
    """,
)

#: Which specialization statement and model belong to each asset type. ``OTHER`` has none.
_ASSET_SPECIALIZATIONS: dict[AssetType, tuple[Statement, type[SourcedModel]]] = {
    AssetType.PROPERTY: (_PROPERTIES, PropertyDetail),
    AssetType.VEHICLE: (_VEHICLES, VehicleDetail),
}


class SqliteRelationshipRepository(SqliteRepository):
    """Reads backing requirement 6.1."""

    def list_household_members(self, household_id: str) -> Sequence[HouseholdMember]:
        rows = self.fetch_all(_HOUSEHOLD_MEMBERS, {"household_id": household_id})
        return to_models(HouseholdMember, rows)

    def get_household_id_for_customer(self, customer_id: str) -> str | None:
        value = self.fetch_scalar(_HOUSEHOLD_FOR_CUSTOMER, {"customer_id": customer_id})
        return None if value is None else str(value)

    def list_relationships(self, customer_id: str) -> Sequence[CustomerRelationship]:
        rows = self.fetch_all(_RELATIONSHIPS, {"customer_id": customer_id})
        return to_models(CustomerRelationship, rows)

    def list_account_parties(self, account_id: str) -> Sequence[AccountParty]:
        rows = self.fetch_all(_ACCOUNT_PARTIES, {"account_id": account_id})
        return to_models(AccountParty, rows)

    def list_joint_accounts(self, customer_id: str) -> Sequence[AccountParty]:
        rows = self.fetch_all(_JOINT_ACCOUNTS, {"customer_id": customer_id})
        return to_models(AccountParty, rows)

    def list_beneficiaries(self, customer_id: str) -> Sequence[Beneficiary]:
        rows = self.fetch_all(_BENEFICIARIES, {"customer_id": customer_id})
        return to_models(Beneficiary, rows)

    def list_assets(self, customer_id: str) -> Sequence[AssetHolding]:
        """Assets with their specializations. Same bounded fan-out as holdings: three reads max."""
        assets = to_models(Asset, self.fetch_all(_ASSETS, {"customer_id": customer_id}))
        if not assets:
            return ()
        details = self._load_asset_details(customer_id, assets)
        return tuple(
            AssetHolding(asset=asset, detail=details.get(asset.asset_id)) for asset in assets
        )

    def _load_asset_details(
        self,
        customer_id: str,
        assets: Sequence[Asset],
    ) -> Mapping[str, AssetDetail]:
        provenance = {asset.asset_id: (asset.as_of_date, asset.source_system) for asset in assets}
        present = {asset.asset_type for asset in assets} & _ASSET_SPECIALIZATIONS.keys()
        details: dict[str, AssetDetail] = {}

        for asset_type in sorted(present):
            statement, model = _ASSET_SPECIALIZATIONS[asset_type]
            for row in self.fetch_all(statement, {"customer_id": customer_id}):
                data: dict[str, Any] = dict(row._mapping)
                asset_id = str(data["asset_id"])
                if asset_id not in provenance:
                    # Defensive for the same reason as the holdings loader: the specialization reads
                    # are per customer, and the parent list can be a subset.
                    continue
                data["as_of_date"], data["source_system"] = provenance[asset_id]
                details[asset_id] = model.model_validate(data)  # type: ignore[assignment]
        return details
