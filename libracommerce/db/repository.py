import json
import sqlite3
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Sequence

from libracommerce.domain.catalog import CatalogItem, CatalogItemType, Unit
from libracommerce.domain.entities import Party, PartyType
from libracommerce.domain.inventory import Location, StockMovement, StockMovementType
from libracommerce.domain.sales import Sale, SaleItem, SaleStatus


def _to_bool(value: int) -> bool:
    return bool(value)


def _to_decimal(value: str | int | float) -> Decimal:
    return Decimal(str(value))


class SqliteCommerceRepository:
    """SQLite adapter for CommerceRepository. Lives outside the domain layer."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    # parties

    def save_party(self, party: Party) -> Party:
        cur = self._conn.cursor()
        if party.id is None:
            cur.execute(
                """
                INSERT INTO parties
                    (party_type, display_name, legal_name, tax_id, tax_id_type, email, phone, active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    party.party_type,
                    party.display_name,
                    party.legal_name,
                    party.tax_id,
                    party.tax_id_type,
                    party.email,
                    party.phone,
                    int(party.active),
                ),
            )
            self._conn.commit()
            return replace(party, id=cur.lastrowid)
        cur.execute(
            """
            UPDATE parties
            SET party_type = ?, display_name = ?, legal_name = ?, tax_id = ?,
                tax_id_type = ?, email = ?, phone = ?, active = ?
            WHERE id = ?
            """,
            (
                party.party_type,
                party.display_name,
                party.legal_name,
                party.tax_id,
                party.tax_id_type,
                party.email,
                party.phone,
                int(party.active),
                party.id,
            ),
        )
        self._conn.commit()
        return party

    def get_party(self, party_id: int) -> Party | None:
        row = self._conn.execute(
            """
            SELECT id, party_type, display_name, legal_name, tax_id, tax_id_type, email, phone, active
            FROM parties WHERE id = ?
            """,
            (party_id,),
        ).fetchone()
        if row is None:
            return None
        return Party(
            id=row[0],
            party_type=PartyType(row[1]),
            display_name=row[2],
            legal_name=row[3],
            tax_id=row[4],
            tax_id_type=row[5],
            email=row[6],
            phone=row[7],
            active=_to_bool(row[8]),
        )

    # catalog

    def _upsert_unit(self, unit: Unit) -> None:
        self._conn.execute(
            """
            INSERT INTO units (code, name, allows_fraction, decimal_scale)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name = excluded.name,
                allows_fraction = excluded.allows_fraction,
                decimal_scale = excluded.decimal_scale
            """,
            (unit.code, unit.name, int(unit.allows_fraction), unit.decimal_scale),
        )

    def save_catalog_item(self, item: CatalogItem) -> CatalogItem:
        self._upsert_unit(item.unit)
        metadata_json = json.dumps(item.metadata)
        cur = self._conn.cursor()
        if item.id is None:
            cur.execute(
                """
                INSERT INTO catalog_items
                    (item_type, name, description, category_id, unit_code, active,
                     sellable, purchasable, tax_profile, metadata_json,
                     default_sale_price, default_cost)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.item_type,
                    item.name,
                    item.description,
                    item.category_id,
                    item.unit.code,
                    int(item.active),
                    int(item.sellable),
                    int(item.purchasable),
                    item.tax_profile,
                    metadata_json,
                    str(item.default_sale_price),
                    str(item.default_cost),
                ),
            )
            self._conn.commit()
            return replace(item, id=cur.lastrowid)
        cur.execute(
            """
            UPDATE catalog_items
            SET item_type = ?, name = ?, description = ?, category_id = ?, unit_code = ?,
                active = ?, sellable = ?, purchasable = ?, tax_profile = ?, metadata_json = ?,
                default_sale_price = ?, default_cost = ?
            WHERE id = ?
            """,
            (
                item.item_type,
                item.name,
                item.description,
                item.category_id,
                item.unit.code,
                int(item.active),
                int(item.sellable),
                int(item.purchasable),
                item.tax_profile,
                metadata_json,
                str(item.default_sale_price),
                str(item.default_cost),
                item.id,
            ),
        )
        self._conn.commit()
        return item

    def get_catalog_item(self, item_id: int) -> CatalogItem | None:
        row = self._conn.execute(
            """
            SELECT ci.id, ci.item_type, ci.name, ci.description, ci.category_id, ci.active,
                   ci.sellable, ci.purchasable, ci.tax_profile, ci.metadata_json,
                   ci.default_sale_price, ci.default_cost,
                   u.code, u.name, u.allows_fraction, u.decimal_scale
            FROM catalog_items ci
            JOIN units u ON u.code = ci.unit_code
            WHERE ci.id = ?
            """,
            (item_id,),
        ).fetchone()
        if row is None:
            return None
        unit = Unit(
            code=row[12],
            name=row[13],
            allows_fraction=_to_bool(row[14]),
            decimal_scale=row[15],
        )
        return CatalogItem(
            id=row[0],
            item_type=CatalogItemType(row[1]),
            name=row[2],
            unit=unit,
            category_id=row[4],
            description=row[3],
            active=_to_bool(row[5]),
            sellable=_to_bool(row[6]),
            purchasable=_to_bool(row[7]),
            tax_profile=row[8],
            metadata=json.loads(row[9]),
            default_sale_price=_to_decimal(row[10]),
            default_cost=_to_decimal(row[11]),
        )

    # locations

    def save_location(self, location: Location) -> Location:
        cur = self._conn.cursor()
        if location.id is None:
            cur.execute(
                "INSERT INTO locations (name, branch_id, location_type, active) VALUES (?, ?, ?, ?)",
                (location.name, location.branch_id, location.location_type, int(location.active)),
            )
            self._conn.commit()
            return replace(location, id=cur.lastrowid)
        cur.execute(
            "UPDATE locations SET name = ?, branch_id = ?, location_type = ?, active = ? WHERE id = ?",
            (location.name, location.branch_id, location.location_type, int(location.active), location.id),
        )
        self._conn.commit()
        return location

    def get_location(self, location_id: int) -> Location | None:
        row = self._conn.execute(
            "SELECT id, name, branch_id, location_type, active FROM locations WHERE id = ?",
            (location_id,),
        ).fetchone()
        if row is None:
            return None
        return Location(
            id=row[0], name=row[1], branch_id=row[2], location_type=row[3], active=_to_bool(row[4])
        )

    # inventory

    def append_stock_movement(self, movement: StockMovement) -> StockMovement:
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO stock_movements
                (item_id, location_id, movement_type, quantity_delta, occurred_at,
                 source_type, source_id, unit_cost, lot_code, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                movement.item_id,
                movement.location_id,
                movement.movement_type,
                str(movement.quantity_delta),
                movement.occurred_at.isoformat(),
                movement.source_type,
                movement.source_id,
                str(movement.unit_cost) if movement.unit_cost is not None else None,
                movement.lot_code,
                movement.expires_at.isoformat() if movement.expires_at else None,
            ),
        )
        self._conn.commit()
        return replace(movement, id=cur.lastrowid)

    def list_stock_movements(self, item_id: int, location_id: int) -> Sequence[StockMovement]:
        rows = self._conn.execute(
            """
            SELECT id, item_id, location_id, movement_type, quantity_delta, occurred_at,
                   source_type, source_id, unit_cost, lot_code, expires_at
            FROM stock_movements
            WHERE item_id = ? AND location_id = ?
            ORDER BY occurred_at, id
            """,
            (item_id, location_id),
        ).fetchall()
        return [
            StockMovement(
                id=row[0],
                item_id=row[1],
                location_id=row[2],
                movement_type=StockMovementType(row[3]),
                quantity_delta=_to_decimal(row[4]),
                occurred_at=datetime.fromisoformat(row[5]),
                source_type=row[6],
                source_id=row[7],
                unit_cost=_to_decimal(row[8]) if row[8] is not None else None,
                lot_code=row[9],
                expires_at=datetime.fromisoformat(row[10]) if row[10] else None,
            )
            for row in rows
        ]

    def current_stock(self, item_id: int, location_id: int) -> Decimal:
        row = self._conn.execute(
            """
            SELECT COALESCE(SUM(quantity_delta), 0)
            FROM stock_movements
            WHERE item_id = ? AND location_id = ?
            """,
            (item_id, location_id),
        ).fetchone()
        return _to_decimal(row[0])

    # sales

    def save_sale(self, sale: Sale) -> Sale:
        cur = self._conn.cursor()
        if sale.id is None:
            cur.execute(
                """
                INSERT INTO sales
                    (number, status, customer_party_id, branch_id, register_id, source_type,
                     source_id, subtotal, discount_total, tax_total, total, confirmed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sale.number,
                    sale.status,
                    sale.customer_party_id,
                    sale.branch_id,
                    sale.register_id,
                    sale.source_type,
                    sale.source_id,
                    str(sale.subtotal),
                    str(sale.discount_total),
                    str(sale.tax_total),
                    str(sale.total),
                    sale.confirmed_at.isoformat() if sale.confirmed_at else None,
                ),
            )
            sale_id = cur.lastrowid
        else:
            sale_id = sale.id
            cur.execute(
                """
                UPDATE sales
                SET number = ?, status = ?, customer_party_id = ?, branch_id = ?, register_id = ?,
                    source_type = ?, source_id = ?, subtotal = ?, discount_total = ?, tax_total = ?,
                    total = ?, confirmed_at = ?
                WHERE id = ?
                """,
                (
                    sale.number,
                    sale.status,
                    sale.customer_party_id,
                    sale.branch_id,
                    sale.register_id,
                    sale.source_type,
                    sale.source_id,
                    str(sale.subtotal),
                    str(sale.discount_total),
                    str(sale.tax_total),
                    str(sale.total),
                    sale.confirmed_at.isoformat() if sale.confirmed_at else None,
                    sale_id,
                ),
            )
            cur.execute("DELETE FROM sale_items WHERE sale_id = ?", (sale_id,))

        for line in sale.items:
            cur.execute(
                """
                INSERT INTO sale_items
                    (sale_id, item_id, description_snapshot, quantity, unit_price,
                     discount_amount, tax_rate, tax_amount, unit_cost_snapshot)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sale_id,
                    line.item_id,
                    line.description_snapshot,
                    str(line.quantity),
                    str(line.unit_price),
                    str(line.discount_amount),
                    str(line.tax_rate),
                    str(line.tax_amount),
                    str(line.unit_cost_snapshot) if line.unit_cost_snapshot is not None else None,
                ),
            )
        self._conn.commit()
        return replace(sale, id=sale_id)

    def get_sale(self, sale_id: int) -> Sale | None:
        row = self._conn.execute(
            """
            SELECT id, number, status, customer_party_id, branch_id, register_id, source_type,
                   source_id, subtotal, discount_total, tax_total, total, confirmed_at
            FROM sales WHERE id = ?
            """,
            (sale_id,),
        ).fetchone()
        if row is None:
            return None
        item_rows = self._conn.execute(
            """
            SELECT item_id, description_snapshot, quantity, unit_price, discount_amount,
                   tax_rate, tax_amount, unit_cost_snapshot
            FROM sale_items WHERE sale_id = ?
            ORDER BY id
            """,
            (sale_id,),
        ).fetchall()
        items = tuple(
            SaleItem(
                item_id=item_row[0],
                description_snapshot=item_row[1],
                quantity=_to_decimal(item_row[2]),
                unit_price=_to_decimal(item_row[3]),
                discount_amount=_to_decimal(item_row[4]),
                tax_rate=_to_decimal(item_row[5]),
                tax_amount=_to_decimal(item_row[6]),
                unit_cost_snapshot=_to_decimal(item_row[7]) if item_row[7] is not None else None,
            )
            for item_row in item_rows
        )
        return Sale(
            id=row[0],
            number=row[1],
            items=items,
            status=SaleStatus(row[2]),
            customer_party_id=row[3],
            branch_id=row[4],
            register_id=row[5],
            source_type=row[6],
            source_id=row[7],
            subtotal=_to_decimal(row[8]),
            discount_total=_to_decimal(row[9]),
            tax_total=_to_decimal(row[10]),
            total=_to_decimal(row[11]),
            confirmed_at=datetime.fromisoformat(row[12]) if row[12] else None,
        )
