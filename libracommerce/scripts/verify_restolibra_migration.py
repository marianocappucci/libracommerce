"""Verificacion de fidelidad post-migracion P8 (ver
wiki/analyses/migracion-p8-restolibra-libracommerce.md). Corre DESPUES de
`migrate_from_restolibra.migrate(conn)`, sobre la misma conexion.

`verify_contalibra_migration.verify()` es schema-shaped igual que el resto
del stack de P7 -- se reusa tal cual para las tablas compartidas
(productos/movimientos_stock/ventas/listas_precio). Este modulo agrega solo
la verificacion del dominio exclusivo de Restolibra: recetas/receta_items,
comparando el costo de cada receta calculado contra `productos.precio_costo`
(tabla vieja, intacta) versus contra `catalog_items.default_cost` (tabla
nueva) -- deben coincidir exactamente, ya que la migracion nunca reescribe
esos valores, solo los copia.

No modifica nada -- solo lectura. Pensado para correrse siempre contra una
COPIA de la base real (nunca la base viva), tanto en Fase 1 como, repetido,
en el cutover real de Fase 4.
"""
import sqlite3
from decimal import Decimal

from libracommerce.scripts.verify_contalibra_migration import VerificationReport, verify as _verify_base


def verify(conn: sqlite3.Connection) -> VerificationReport:
    report = _verify_base(conn)
    _verify_recetas(conn, report)
    return report


def _costo_receta(conn: sqlite3.Connection, producto_id: int, precio_costo_col: str,
                   tabla_producto: str) -> Decimal | None:
    receta = conn.execute(
        "SELECT id, rinde, rendimiento_pct FROM recetas WHERE producto_id=?", (producto_id,)
    ).fetchone()
    if receta is None:
        return None
    receta_id, rinde, rendimiento_pct = receta
    items = conn.execute(
        f"""SELECT ri.cantidad, p.{precio_costo_col}
            FROM receta_items ri
            JOIN {tabla_producto} p ON p.id = ri.ingrediente_id
            WHERE ri.receta_id=?""",
        (receta_id,),
    ).fetchall()
    if not items:
        return Decimal("0")
    total = sum(Decimal(str(cantidad)) * Decimal(str(costo)) for cantidad, costo in items)
    rendimiento = Decimal(str(rendimiento_pct or 100))
    rinde_dec = Decimal(str(rinde or 1))
    return (total / (rendimiento / 100)) / rinde_dec


def _verify_recetas(conn: sqlite3.Connection, report: VerificationReport) -> None:
    recetas_viejas = conn.execute("SELECT COUNT(*) FROM recetas").fetchone()[0]
    items_viejos = conn.execute("SELECT COUNT(*) FROM receta_items").fetchone()[0]

    fks_recetas = conn.execute("PRAGMA foreign_key_list(recetas)").fetchall()
    if not any(row[2] == "catalog_items" for row in fks_recetas):
        report.add("recetas_fk", "recetas.producto_id no quedo repuntado a catalog_items")
    fks_items = conn.execute("PRAGMA foreign_key_list(receta_items)").fetchall()
    if not any(row[2] == "catalog_items" for row in fks_items):
        report.add("receta_items_fk", "receta_items.ingrediente_id no quedo repuntado a catalog_items")

    huerfanos = conn.execute(
        """SELECT ri.id FROM receta_items ri
           LEFT JOIN catalog_items c ON c.id = ri.ingrediente_id
           WHERE c.id IS NULL"""
    ).fetchall()
    if huerfanos:
        report.add(
            "receta_items_huerfanos",
            f"{len(huerfanos)} fila(s) de receta_items sin catalog_items correspondiente: "
            f"ids={[r[0] for r in huerfanos]}",
        )

    for (producto_id,) in conn.execute("SELECT producto_id FROM recetas"):
        costo_viejo = _costo_receta(conn, producto_id, "precio_costo", "productos")
        costo_nuevo = _costo_receta(conn, producto_id, "default_cost", "catalog_items")
        if costo_viejo != costo_nuevo:
            report.add(
                "costo_receta",
                f"producto_id={producto_id}: costo via productos={costo_viejo} "
                f"costo via catalog_items={costo_nuevo}",
            )

    if recetas_viejas != conn.execute("SELECT COUNT(*) FROM recetas").fetchone()[0]:
        report.add("count_recetas", "recetas perdio filas durante el repointing de FK")
    if items_viejos != conn.execute("SELECT COUNT(*) FROM receta_items").fetchone()[0]:
        report.add("count_receta_items", "receta_items perdio filas durante el repointing de FK")
