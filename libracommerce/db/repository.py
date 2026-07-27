import json
import sqlite3
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Sequence

from libracommerce.domain.catalog import (
    CatalogItem,
    CatalogItemType,
    ItemCode,
    ItemCodeType,
    ItemPrice,
    ItemVariant,
    PriceList,
    Unit,
)
from libracommerce.domain.entities import Party, PartyType
from libracommerce.domain.inventory import Location, StockMovement, StockMovementType
from libracommerce.domain.purchasing import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
    PurchaseReceipt,
    PurchaseReceiptItem,
    PurchaseReceiptStatus,
)
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
                     default_sale_price, default_cost, min_stock)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    str(item.min_stock),
                ),
            )
            self._conn.commit()
            return replace(item, id=cur.lastrowid)
        cur.execute(
            """
            UPDATE catalog_items
            SET item_type = ?, name = ?, description = ?, category_id = ?, unit_code = ?,
                active = ?, sellable = ?, purchasable = ?, tax_profile = ?, metadata_json = ?,
                default_sale_price = ?, default_cost = ?, min_stock = ?
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
                str(item.min_stock),
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
                   ci.default_sale_price, ci.default_cost, ci.min_stock,
                   u.code, u.name, u.allows_fraction, u.decimal_scale
            FROM catalog_items ci
            JOIN units u ON u.code = ci.unit_code
            WHERE ci.id = ?
            """,
            (item_id,),
        ).fetchone()
        return self._catalog_item_from_row(row)

    def list_catalog_items(
        self,
        *,
        active_only: bool = False,
        sellable_only: bool = False,
        item_type: CatalogItemType | None = None,
        search: str = "",
    ) -> Sequence[CatalogItem]:
        sql = """
            SELECT ci.id, ci.item_type, ci.name, ci.description, ci.category_id, ci.active,
                   ci.sellable, ci.purchasable, ci.tax_profile, ci.metadata_json,
                   ci.default_sale_price, ci.default_cost, ci.min_stock,
                   u.code, u.name, u.allows_fraction, u.decimal_scale
            FROM catalog_items ci
            JOIN units u ON u.code = ci.unit_code
        """
        where, params = [], []
        if active_only:
            where.append("ci.active = 1")
        if sellable_only:
            where.append("ci.sellable = 1")
        if item_type is not None:
            where.append("ci.item_type = ?")
            params.append(item_type)
        if search:
            where.append("ci.name LIKE ?")
            params.append(f"%{search}%")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ci.name"
        rows = self._conn.execute(sql, params).fetchall()
        return [self._catalog_item_from_row(row) for row in rows]

    def _catalog_item_from_row(self, row) -> CatalogItem | None:
        if row is None:
            return None
        unit = Unit(
            code=row[13],
            name=row[14],
            allows_fraction=_to_bool(row[15]),
            decimal_scale=row[16],
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
            min_stock=_to_decimal(row[12]),
        )

    # item codes

    def save_item_code(self, item_code: ItemCode) -> ItemCode:
        cur = self._conn.cursor()
        if item_code.id is None:
            cur.execute(
                """
                INSERT INTO item_codes (item_id, code_type, code, is_primary)
                VALUES (?, ?, ?, ?)
                """,
                (item_code.item_id, item_code.code_type, item_code.code, int(item_code.is_primary)),
            )
            self._conn.commit()
            return replace(item_code, id=cur.lastrowid)
        cur.execute(
            """
            UPDATE item_codes SET item_id = ?, code_type = ?, code = ?, is_primary = ?
            WHERE id = ?
            """,
            (item_code.item_id, item_code.code_type, item_code.code, int(item_code.is_primary), item_code.id),
        )
        self._conn.commit()
        return item_code

    def list_item_codes(self, item_id: int) -> Sequence[ItemCode]:
        rows = self._conn.execute(
            """
            SELECT id, item_id, code_type, code, is_primary
            FROM item_codes WHERE item_id = ?
            ORDER BY id
            """,
            (item_id,),
        ).fetchall()
        return [
            ItemCode(
                id=row[0],
                item_id=row[1],
                code_type=ItemCodeType(row[2]),
                code=row[3],
                is_primary=_to_bool(row[4]),
            )
            for row in rows
        ]

    def find_item_by_code(self, code: str) -> CatalogItem | None:
        row = self._conn.execute(
            """
            SELECT ci.id, ci.item_type, ci.name, ci.description, ci.category_id, ci.active,
                   ci.sellable, ci.purchasable, ci.tax_profile, ci.metadata_json,
                   ci.default_sale_price, ci.default_cost, ci.min_stock,
                   u.code, u.name, u.allows_fraction, u.decimal_scale
            FROM item_codes ic
            JOIN catalog_items ci ON ci.id = ic.item_id
            JOIN units u ON u.code = ci.unit_code
            WHERE ic.code = ?
            """,
            (code,),
        ).fetchone()
        return self._catalog_item_from_row(row)

    # item variants

    def save_item_variant(self, variant: ItemVariant) -> ItemVariant:
        cur = self._conn.cursor()
        attributes_json = json.dumps(variant.attributes)
        if variant.id is None:
            cur.execute(
                """
                INSERT INTO item_variants (item_id, sku, name, attributes_json, active)
                VALUES (?, ?, ?, ?, ?)
                """,
                (variant.item_id, variant.sku, variant.name, attributes_json, int(variant.active)),
            )
            self._conn.commit()
            return replace(variant, id=cur.lastrowid)
        cur.execute(
            """
            UPDATE item_variants SET item_id = ?, sku = ?, name = ?, attributes_json = ?, active = ?
            WHERE id = ?
            """,
            (variant.item_id, variant.sku, variant.name, attributes_json, int(variant.active), variant.id),
        )
        self._conn.commit()
        return variant

    def _item_variant_from_row(self, row) -> ItemVariant | None:
        if row is None:
            return None
        return ItemVariant(
            id=row[0],
            item_id=row[1],
            sku=row[2],
            name=row[3],
            attributes=json.loads(row[4]),
            active=_to_bool(row[5]),
        )

    def get_item_variant(self, variant_id: int) -> ItemVariant | None:
        row = self._conn.execute(
            "SELECT id, item_id, sku, name, attributes_json, active FROM item_variants WHERE id = ?",
            (variant_id,),
        ).fetchone()
        return self._item_variant_from_row(row)

    def list_item_variants(self, item_id: int) -> Sequence[ItemVariant]:
        rows = self._conn.execute(
            """
            SELECT id, item_id, sku, name, attributes_json, active
            FROM item_variants WHERE item_id = ?
            ORDER BY id
            """,
            (item_id,),
        ).fetchall()
        return [self._item_variant_from_row(row) for row in rows]

    # price lists

    def save_price_list(self, price_list: PriceList) -> PriceList:
        cur = self._conn.cursor()
        if price_list.id is None:
            cur.execute(
                """
                INSERT INTO price_lists (name, description, active, is_default)
                VALUES (?, ?, ?, ?)
                """,
                (price_list.name, price_list.description, int(price_list.active), int(price_list.is_default)),
            )
            self._conn.commit()
            return replace(price_list, id=cur.lastrowid)
        cur.execute(
            """
            UPDATE price_lists SET name = ?, description = ?, active = ?, is_default = ?
            WHERE id = ?
            """,
            (
                price_list.name, price_list.description, int(price_list.active),
                int(price_list.is_default), price_list.id,
            ),
        )
        self._conn.commit()
        return price_list

    def get_price_list(self, price_list_id: int) -> PriceList | None:
        row = self._conn.execute(
            "SELECT id, name, description, active, is_default FROM price_lists WHERE id = ?",
            (price_list_id,),
        ).fetchone()
        if row is None:
            return None
        return PriceList(
            id=row[0], name=row[1], description=row[2], active=_to_bool(row[3]), is_default=_to_bool(row[4])
        )

    def save_item_price(self, item_price: ItemPrice) -> ItemPrice:
        cur = self._conn.cursor()
        values = (
            item_price.item_id,
            item_price.price_list_id,
            str(item_price.amount),
            item_price.currency,
            item_price.valid_from.isoformat(),
            item_price.valid_until.isoformat() if item_price.valid_until else None,
            str(item_price.min_quantity) if item_price.min_quantity is not None else None,
            item_price.branch_id,
        )
        if item_price.id is None:
            cur.execute(
                """
                INSERT INTO item_prices
                    (item_id, price_list_id, amount, currency, valid_from, valid_until, min_quantity, branch_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            self._conn.commit()
            return replace(item_price, id=cur.lastrowid)
        cur.execute(
            """
            UPDATE item_prices
            SET item_id = ?, price_list_id = ?, amount = ?, currency = ?, valid_from = ?,
                valid_until = ?, min_quantity = ?, branch_id = ?
            WHERE id = ?
            """,
            values + (item_price.id,),
        )
        self._conn.commit()
        return item_price

    def _item_price_from_row(self, row) -> ItemPrice:
        return ItemPrice(
            id=row[0],
            item_id=row[1],
            price_list_id=row[2],
            amount=_to_decimal(row[3]),
            currency=row[4],
            valid_from=datetime.fromisoformat(row[5]),
            valid_until=datetime.fromisoformat(row[6]) if row[6] else None,
            min_quantity=_to_decimal(row[7]) if row[7] is not None else None,
            branch_id=row[8],
        )

    def list_item_prices(self, item_id: int) -> Sequence[ItemPrice]:
        rows = self._conn.execute(
            """
            SELECT id, item_id, price_list_id, amount, currency, valid_from, valid_until, min_quantity, branch_id
            FROM item_prices WHERE item_id = ?
            ORDER BY valid_from
            """,
            (item_id,),
        ).fetchall()
        return [self._item_price_from_row(row) for row in rows]

    def resolve_price(
        self,
        item_id: int,
        *,
        price_list_id: int | None = None,
        quantity: Decimal = Decimal("1"),
        at: datetime | None = None,
        branch_id: int | None = None,
    ) -> Decimal | None:
        """Resuelve el precio efectivo: si `price_list_id` no se pasa, usa la
        lista marcada `is_default` (activa). Entre los precios vigentes en
        `at` para esa cantidad, prioriza el más específico: precio por
        sucursal antes que general, luego el quiebre de cantidad más alto
        aplicable, y como último desempate el más reciente.
        """
        list_id = price_list_id
        if list_id is None:
            row = self._conn.execute(
                "SELECT id FROM price_lists WHERE is_default = 1 AND active = 1"
            ).fetchone()
            if row is None:
                return None
            list_id = row[0]

        moment = (at or datetime.now()).isoformat()
        rows = self._conn.execute(
            """
            SELECT amount, min_quantity, branch_id, valid_from
            FROM item_prices
            WHERE item_id = ? AND price_list_id = ?
              AND valid_from <= ?
              AND (valid_until IS NULL OR valid_until >= ?)
              AND (min_quantity IS NULL OR min_quantity <= ?)
              AND (branch_id IS NULL OR branch_id = ?)
            """,
            (item_id, list_id, moment, moment, str(quantity), branch_id),
        ).fetchall()
        if not rows:
            return None

        def sort_key(row):
            amount, min_quantity, row_branch_id, valid_from = row
            return (
                1 if row_branch_id is not None else 0,
                _to_decimal(min_quantity) if min_quantity is not None else Decimal("0"),
                valid_from,
            )

        best = max(rows, key=sort_key)
        return _to_decimal(best[0])

    # locations

    def save_location(self, location: Location) -> Location:
        cur = self._conn.cursor()
        if location.id is None:
            cur.execute(
                """
                INSERT INTO locations (name, branch_id, location_type, active, description, is_default)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    location.name, location.branch_id, location.location_type, int(location.active),
                    location.description, int(location.is_default),
                ),
            )
            self._conn.commit()
            return replace(location, id=cur.lastrowid)
        cur.execute(
            """
            UPDATE locations SET name = ?, branch_id = ?, location_type = ?, active = ?,
                description = ?, is_default = ?
            WHERE id = ?
            """,
            (
                location.name, location.branch_id, location.location_type, int(location.active),
                location.description, int(location.is_default), location.id,
            ),
        )
        self._conn.commit()
        return location

    def get_location(self, location_id: int) -> Location | None:
        row = self._conn.execute(
            "SELECT id, name, branch_id, location_type, active, description, is_default "
            "FROM locations WHERE id = ?",
            (location_id,),
        ).fetchone()
        return self._location_from_row(row)

    def list_locations(self, *, active_only: bool = False) -> Sequence[Location]:
        sql = (
            "SELECT id, name, branch_id, location_type, active, description, is_default FROM locations"
        )
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY is_default DESC, name"
        rows = self._conn.execute(sql).fetchall()
        return [self._location_from_row(row) for row in rows]

    def _location_from_row(self, row) -> Location | None:
        if row is None:
            return None
        return Location(
            id=row[0], name=row[1], branch_id=row[2], location_type=row[3], active=_to_bool(row[4]),
            description=row[5], is_default=_to_bool(row[6]),
        )

    # inventory

    def append_stock_movement(self, movement: StockMovement) -> StockMovement:
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO stock_movements
                (item_id, variant_id, location_id, movement_type, quantity_delta, occurred_at,
                 source_type, source_id, unit_cost, lot_code, expires_at,
                 note, created_by, reason_code)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                movement.item_id,
                movement.variant_id,
                movement.location_id,
                movement.movement_type,
                str(movement.quantity_delta),
                movement.occurred_at.isoformat(),
                movement.source_type,
                movement.source_id,
                str(movement.unit_cost) if movement.unit_cost is not None else None,
                movement.lot_code,
                movement.expires_at.isoformat() if movement.expires_at else None,
                movement.note,
                movement.created_by,
                movement.reason_code,
            ),
        )
        self._conn.commit()
        return replace(movement, id=cur.lastrowid)

    def list_stock_movements(
        self, item_id: int, location_id: int, *, variant_id: int | None = None
    ) -> Sequence[StockMovement]:
        variant_filter = "variant_id IS NULL" if variant_id is None else "variant_id = ?"
        params = (item_id, location_id) if variant_id is None else (item_id, location_id, variant_id)
        rows = self._conn.execute(
            f"""
            SELECT id, item_id, variant_id, location_id, movement_type, quantity_delta, occurred_at,
                   source_type, source_id, unit_cost, lot_code, expires_at,
                   note, created_by, reason_code
            FROM stock_movements
            WHERE item_id = ? AND location_id = ? AND {variant_filter}
            ORDER BY occurred_at, id
            """,
            params,
        ).fetchall()
        return [
            StockMovement(
                id=row[0],
                item_id=row[1],
                variant_id=row[2],
                location_id=row[3],
                movement_type=StockMovementType(row[4]),
                quantity_delta=_to_decimal(row[5]),
                occurred_at=datetime.fromisoformat(row[6]),
                source_type=row[7],
                source_id=row[8],
                unit_cost=_to_decimal(row[9]) if row[9] is not None else None,
                lot_code=row[10],
                expires_at=datetime.fromisoformat(row[11]) if row[11] else None,
                note=row[12],
                created_by=row[13],
                reason_code=row[14],
            )
            for row in rows
        ]

    def current_stock(self, item_id: int, location_id: int, *, variant_id: int | None = None) -> Decimal:
        variant_filter = "variant_id IS NULL" if variant_id is None else "variant_id = ?"
        params = (item_id, location_id) if variant_id is None else (item_id, location_id, variant_id)
        row = self._conn.execute(
            f"""
            SELECT COALESCE(SUM(quantity_delta), 0)
            FROM stock_movements
            WHERE item_id = ? AND location_id = ? AND {variant_filter}
            """,
            params,
        ).fetchone()
        return _to_decimal(row[0])

    # sales

    def save_sale(self, sale: Sale, *, commit: bool = True) -> Sale:
        cur = self._conn.cursor()
        if sale.id is None:
            cur.execute(
                """
                INSERT INTO sales
                    (number, status, customer_party_id, branch_id, register_id, source_type,
                     source_id, subtotal, discount_total, tax_total, total, confirmed_at,
                     occurred_on, customer_name_snapshot, created_by, notes, status_detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    sale.occurred_on,
                    sale.customer_name_snapshot,
                    sale.created_by,
                    sale.notes,
                    sale.status_detail,
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
                    total = ?, confirmed_at = ?, occurred_on = ?, customer_name_snapshot = ?,
                    created_by = ?, notes = ?, status_detail = ?
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
                    sale.occurred_on,
                    sale.customer_name_snapshot,
                    sale.created_by,
                    sale.notes,
                    sale.status_detail,
                    sale_id,
                ),
            )
            cur.execute("DELETE FROM sale_items WHERE sale_id = ?", (sale_id,))

        for line in sale.items:
            cur.execute(
                """
                INSERT INTO sale_items
                    (sale_id, kind, item_id, variant_id, description_snapshot, quantity, unit_price,
                     discount_amount, tax_rate, tax_amount, unit_cost_snapshot)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sale_id,
                    line.kind,
                    line.item_id,
                    line.variant_id,
                    line.description_snapshot,
                    str(line.quantity),
                    str(line.unit_price),
                    str(line.discount_amount),
                    str(line.tax_rate),
                    str(line.tax_amount),
                    str(line.unit_cost_snapshot) if line.unit_cost_snapshot is not None else None,
                ),
            )
        if commit:
            self._conn.commit()
        return replace(sale, id=sale_id)

    def get_sale(self, sale_id: int) -> Sale | None:
        row = self._conn.execute(
            """
            SELECT id, number, status, customer_party_id, branch_id, register_id, source_type,
                   source_id, subtotal, discount_total, tax_total, total, confirmed_at,
                   occurred_on, customer_name_snapshot, created_by, notes, status_detail
            FROM sales WHERE id = ?
            """,
            (sale_id,),
        ).fetchone()
        if row is None:
            return None
        item_rows = self._conn.execute(
            """
            SELECT kind, item_id, variant_id, description_snapshot, quantity, unit_price, discount_amount,
                   tax_rate, tax_amount, unit_cost_snapshot
            FROM sale_items WHERE sale_id = ?
            ORDER BY id
            """,
            (sale_id,),
        ).fetchall()
        items = tuple(
            SaleItem(
                kind=CatalogItemType(item_row[0]),
                item_id=item_row[1],
                variant_id=item_row[2],
                description_snapshot=item_row[3],
                quantity=_to_decimal(item_row[4]),
                unit_price=_to_decimal(item_row[5]),
                discount_amount=_to_decimal(item_row[6]),
                tax_rate=_to_decimal(item_row[7]),
                tax_amount=_to_decimal(item_row[8]),
                unit_cost_snapshot=_to_decimal(item_row[9]) if item_row[9] is not None else None,
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
            occurred_on=row[13],
            customer_name_snapshot=row[14],
            created_by=row[15],
            notes=row[16],
            status_detail=row[17],
        )

    # purchasing

    def save_purchase_order(self, order: PurchaseOrder) -> PurchaseOrder:
        cur = self._conn.cursor()
        if order.id is None:
            cur.execute(
                """
                INSERT INTO purchase_orders
                    (number, supplier_party_id, branch_id, status, ordered_at, expected_at,
                     notes, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order.number,
                    order.supplier_party_id,
                    order.branch_id,
                    order.status,
                    order.ordered_at.isoformat() if order.ordered_at else None,
                    order.expected_at.isoformat() if order.expected_at else None,
                    order.notes,
                    order.created_by,
                ),
            )
            order_id = cur.lastrowid
        else:
            order_id = order.id
            cur.execute(
                """
                UPDATE purchase_orders
                SET number = ?, supplier_party_id = ?, branch_id = ?, status = ?, ordered_at = ?,
                    expected_at = ?, notes = ?, created_by = ?
                WHERE id = ?
                """,
                (
                    order.number,
                    order.supplier_party_id,
                    order.branch_id,
                    order.status,
                    order.ordered_at.isoformat() if order.ordered_at else None,
                    order.expected_at.isoformat() if order.expected_at else None,
                    order.notes,
                    order.created_by,
                    order_id,
                ),
            )
            cur.execute("DELETE FROM purchase_order_items WHERE purchase_order_id = ?", (order_id,))

        for line in order.items:
            cur.execute(
                """
                INSERT INTO purchase_order_items
                    (purchase_order_id, item_id, quantity_ordered, quantity_received,
                     unit_cost, tax_rate)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    line.item_id,
                    str(line.quantity_ordered),
                    str(line.quantity_received),
                    str(line.unit_cost),
                    str(line.tax_rate),
                ),
            )
        self._conn.commit()
        return replace(order, id=order_id)

    def get_purchase_order(self, order_id: int) -> PurchaseOrder | None:
        row = self._conn.execute(
            """
            SELECT id, number, supplier_party_id, branch_id, status, ordered_at, expected_at,
                   notes, created_by
            FROM purchase_orders WHERE id = ?
            """,
            (order_id,),
        ).fetchone()
        return self._purchase_order_from_row(row)

    def _purchase_order_from_row(self, row) -> PurchaseOrder | None:
        if row is None:
            return None
        item_rows = self._conn.execute(
            """
            SELECT item_id, quantity_ordered, quantity_received, unit_cost, tax_rate
            FROM purchase_order_items WHERE purchase_order_id = ?
            ORDER BY id
            """,
            (row[0],),
        ).fetchall()
        items = tuple(
            PurchaseOrderItem(
                item_id=item_row[0],
                quantity_ordered=_to_decimal(item_row[1]),
                quantity_received=_to_decimal(item_row[2]),
                unit_cost=_to_decimal(item_row[3]),
                tax_rate=_to_decimal(item_row[4]),
            )
            for item_row in item_rows
        )
        return PurchaseOrder(
            id=row[0],
            number=row[1],
            supplier_party_id=row[2],
            items=items,
            status=PurchaseOrderStatus(row[4]),
            branch_id=row[3],
            ordered_at=datetime.fromisoformat(row[5]) if row[5] else None,
            expected_at=datetime.fromisoformat(row[6]) if row[6] else None,
            notes=row[7],
            created_by=row[8],
        )

    def list_purchase_orders(self) -> Sequence[PurchaseOrder]:
        rows = self._conn.execute(
            """
            SELECT id, number, supplier_party_id, branch_id, status, ordered_at, expected_at,
                   notes, created_by
            FROM purchase_orders
            ORDER BY id DESC
            """,
        ).fetchall()
        return [self._purchase_order_from_row(row) for row in rows]

    def save_purchase_receipt(self, receipt: PurchaseReceipt) -> PurchaseReceipt:
        cur = self._conn.cursor()
        if receipt.id is None:
            cur.execute(
                """
                INSERT INTO purchase_receipts
                    (purchase_order_id, supplier_party_id, status, received_at,
                     document_reference, created_by)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.purchase_order_id,
                    receipt.supplier_party_id,
                    receipt.status,
                    receipt.received_at.isoformat() if receipt.received_at else None,
                    receipt.document_reference,
                    receipt.created_by,
                ),
            )
            receipt_id = cur.lastrowid
        else:
            receipt_id = receipt.id
            cur.execute(
                """
                UPDATE purchase_receipts
                SET purchase_order_id = ?, supplier_party_id = ?, status = ?, received_at = ?,
                    document_reference = ?, created_by = ?
                WHERE id = ?
                """,
                (
                    receipt.purchase_order_id,
                    receipt.supplier_party_id,
                    receipt.status,
                    receipt.received_at.isoformat() if receipt.received_at else None,
                    receipt.document_reference,
                    receipt.created_by,
                    receipt_id,
                ),
            )
            cur.execute("DELETE FROM purchase_receipt_items WHERE receipt_id = ?", (receipt_id,))

        for line in receipt.items:
            cur.execute(
                """
                INSERT INTO purchase_receipt_items
                    (receipt_id, item_id, quantity, unit_cost, lot_code, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    line.item_id,
                    str(line.quantity),
                    str(line.unit_cost),
                    line.lot_code,
                    line.expires_at.isoformat() if line.expires_at else None,
                ),
            )
        self._conn.commit()
        return replace(receipt, id=receipt_id)

    def get_purchase_receipt(self, receipt_id: int) -> PurchaseReceipt | None:
        row = self._conn.execute(
            """
            SELECT id, purchase_order_id, supplier_party_id, status, received_at,
                   document_reference, created_by
            FROM purchase_receipts WHERE id = ?
            """,
            (receipt_id,),
        ).fetchone()
        return self._purchase_receipt_from_row(row)

    def _purchase_receipt_from_row(self, row) -> PurchaseReceipt | None:
        if row is None:
            return None
        item_rows = self._conn.execute(
            """
            SELECT item_id, quantity, unit_cost, lot_code, expires_at
            FROM purchase_receipt_items WHERE receipt_id = ?
            ORDER BY id
            """,
            (row[0],),
        ).fetchall()
        items = tuple(
            PurchaseReceiptItem(
                item_id=item_row[0],
                quantity=_to_decimal(item_row[1]),
                unit_cost=_to_decimal(item_row[2]),
                lot_code=item_row[3],
                expires_at=datetime.fromisoformat(item_row[4]) if item_row[4] else None,
            )
            for item_row in item_rows
        )
        return PurchaseReceipt(
            id=row[0],
            supplier_party_id=row[2],
            items=items,
            purchase_order_id=row[1],
            status=PurchaseReceiptStatus(row[3]),
            received_at=datetime.fromisoformat(row[4]) if row[4] else None,
            document_reference=row[5],
            created_by=row[6],
        )

    def list_purchase_receipts(self) -> Sequence[PurchaseReceipt]:
        rows = self._conn.execute(
            """
            SELECT id, purchase_order_id, supplier_party_id, status, received_at,
                   document_reference, created_by
            FROM purchase_receipts
            ORDER BY id DESC
            """,
        ).fetchall()
        return [self._purchase_receipt_from_row(row) for row in rows]
