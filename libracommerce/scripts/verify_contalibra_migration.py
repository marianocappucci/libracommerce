"""Verificacion de fidelidad post-migracion (Fase 1 del plan P7, ver
wiki/analyses/migracion-p7-contalibra-libracommerce.md). Corre DESPUES de
`migrate_from_contalibra.migrate(conn)`, sobre la misma conexion -- compara
las tablas viejas de Contalibra contra las nuevas de LibraCommerce y
levanta discrepancias en vez de asumir que la migracion salio bien.

No modifica nada -- solo lectura. Pensado para correrse siempre contra una
COPIA de la base real (nunca la base viva), tanto en la verificacion de
Fase 1 como, repetido, en el cutover real de la Fase 4.
"""
import json
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class Discrepancy:
    check: str
    detail: str


@dataclass
class VerificationReport:
    discrepancies: list[Discrepancy] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.discrepancies

    def add(self, check: str, detail: str) -> None:
        self.discrepancies.append(Discrepancy(check, detail))


def _count(conn: sqlite3.Connection, sql: str) -> int:
    return conn.execute(sql).fetchone()[0]


def verify(conn: sqlite3.Connection) -> VerificationReport:
    report = VerificationReport()

    _verify_counts(conn, report)
    _verify_stock_per_item_location(conn, report)
    _verify_sale_totals(conn, report)
    _verify_price_lists(conn, report)

    return report


def _verify_counts(conn: sqlite3.Connection, report: VerificationReport) -> None:
    productos = _count(conn, "SELECT COUNT(*) FROM productos")
    catalog_items = _count(conn, "SELECT COUNT(*) FROM catalog_items")
    if productos != catalog_items:
        report.add("count_productos", f"productos={productos} catalog_items={catalog_items}")

    movimientos = _count(
        conn, "SELECT COUNT(*) FROM movimientos_stock WHERE deposito_id IS NOT NULL"
    )
    stock_movements = _count(conn, "SELECT COUNT(*) FROM stock_movements")
    if movimientos != stock_movements:
        report.add(
            "count_movimientos_stock",
            f"movimientos_stock(con deposito)={movimientos} stock_movements={stock_movements}",
        )

    ventas = _count(conn, "SELECT COUNT(*) FROM ventas")
    sales = _count(conn, "SELECT COUNT(*) FROM sales")
    if ventas != sales:
        report.add("count_ventas", f"ventas={ventas} sales={sales}")

    items_json_total = 0
    for (items_raw,) in conn.execute("SELECT items FROM ventas"):
        items_json_total += len(json.loads(items_raw))
    sale_items = _count(conn, "SELECT COUNT(*) FROM sale_items")
    if items_json_total != sale_items:
        report.add(
            "count_sale_items",
            f"lineas en ventas.items (JSON)={items_json_total} sale_items={sale_items}",
        )


def _verify_stock_per_item_location(conn: sqlite3.Connection, report: VerificationReport) -> None:
    old_stock = {
        (producto_id, deposito_id): Decimal(str(total))
        for producto_id, deposito_id, total in conn.execute(
            """
            SELECT producto_id, deposito_id, SUM(cantidad)
            FROM movimientos_stock
            WHERE deposito_id IS NOT NULL
            GROUP BY producto_id, deposito_id
            """
        ).fetchall()
    }
    new_stock = {
        (item_id, location_id): Decimal(str(total))
        for item_id, location_id, total in conn.execute(
            """
            SELECT item_id, location_id, SUM(quantity_delta)
            FROM stock_movements
            GROUP BY item_id, location_id
            """
        ).fetchall()
    }
    for key, old_qty in old_stock.items():
        new_qty = new_stock.get(key)
        if new_qty != old_qty:
            producto_id, deposito_id = key
            report.add(
                "stock_por_producto_deposito",
                f"producto_id={producto_id} deposito_id={deposito_id}: "
                f"movimientos_stock={old_qty} stock_movements={new_qty}",
            )
    for key in new_stock:
        if key not in old_stock:
            item_id, location_id = key
            report.add(
                "stock_por_producto_deposito",
                f"item_id={item_id} location_id={location_id}: "
                f"presente en stock_movements pero no en movimientos_stock",
            )


def _verify_sale_totals(conn: sqlite3.Connection, report: VerificationReport) -> None:
    old_totals = dict(conn.execute("SELECT id, total FROM ventas").fetchall())
    new_totals = dict(conn.execute("SELECT id, total FROM sales").fetchall())
    for sale_id, old_total in old_totals.items():
        new_total = new_totals.get(sale_id)
        if new_total is None:
            report.add("sale_total", f"venta {sale_id}: no migrada a sales")
            continue
        if Decimal(str(new_total)) != Decimal(str(old_total)):
            report.add(
                "sale_total",
                f"venta {sale_id}: ventas.total={old_total} sales.total={new_total}",
            )

    line_sums = {}
    for sale_id, unit_price, quantity, discount_amount in conn.execute(
        "SELECT sale_id, unit_price, quantity, discount_amount FROM sale_items"
    ):
        line_total = Decimal(str(unit_price)) * Decimal(str(quantity)) - Decimal(str(discount_amount))
        line_sums[sale_id] = line_sums.get(sale_id, Decimal("0")) + line_total
    for sale_id, subtotal in conn.execute("SELECT id, subtotal FROM sales"):
        lines_sum = line_sums.get(sale_id, Decimal("0"))
        if lines_sum != Decimal(str(subtotal)):
            report.add(
                "sale_items_sum_vs_subtotal",
                f"venta {sale_id}: suma de sale_items={lines_sum} sales.subtotal={subtotal}",
            )


def _verify_price_lists(conn: sqlite3.Connection, report: VerificationReport) -> None:
    listas = _count(conn, "SELECT COUNT(*) FROM listas_precio")
    price_lists = _count(conn, "SELECT COUNT(*) FROM price_lists")
    if listas != price_lists:
        report.add("count_listas_precio", f"listas_precio={listas} price_lists={price_lists}")

    items_viejos = _count(conn, "SELECT COUNT(*) FROM lista_precio_items")
    item_prices = _count(conn, "SELECT COUNT(*) FROM item_prices")
    if items_viejos != item_prices:
        report.add(
            "count_lista_precio_items",
            f"lista_precio_items={items_viejos} item_prices={item_prices}",
        )

    old_precios = {
        (lista_id, producto_id): Decimal(str(precio))
        for lista_id, producto_id, precio in conn.execute(
            "SELECT lista_id, producto_id, precio FROM lista_precio_items"
        ).fetchall()
    }
    new_precios = {
        (price_list_id, item_id): Decimal(str(amount))
        for price_list_id, item_id, amount in conn.execute(
            "SELECT price_list_id, item_id, amount FROM item_prices "
            "WHERE branch_id IS NULL AND min_quantity IS NULL"
        ).fetchall()
    }
    for key, old_amount in old_precios.items():
        new_amount = new_precios.get(key)
        if new_amount != old_amount:
            lista_id, producto_id = key
            report.add(
                "precio_lista",
                f"lista_id={lista_id} producto_id={producto_id}: "
                f"lista_precio_items={old_amount} item_prices={new_amount}",
            )
