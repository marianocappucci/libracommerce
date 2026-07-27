"""Migracion de datos, de una sola corrida, del catalogo/stock/ventas/
recetas de Restolibra hacia el esquema de LibraCommerce -- P8 del plan de
consolidacion de la familia Libra (ver
wiki/analyses/migracion-p8-restolibra-libracommerce.md).

Restolibra es un fork de Contalibra con el mismo schema exacto de
libracore para productos/movimientos_stock/ventas/listas_precio
(mismo paquete libracore instalado en ambos). `migrate_from_contalibra.migrate()`
es puramente schema-shaped -- nunca referencia nada especifico de
Contalibra en su logica, solo en sus docstrings/nombres historicos (fue
P7, el primer consumidor) -- asi que se reusa tal cual para esa parte, sin
duplicarla.

Lo unico que agrega este modulo es el dominio exclusivo de Restolibra:
recetas/receta_items, que no existen en Contalibra y no tienen tabla
propia en LibraCommerce (quedan como tablas de Restolibra, igual que
`venta_links` quedo propio de Contalibra -- ver
wiki/analyses/arquitectura-familia-libra-alcance.md, "Recetas, elaborados,
food cost y mermas: exclusivo gastronomico"). Como los IDs de
catalog_items se preservan 1:1 desde productos.id, las filas de
recetas/receta_items no necesitan reescribirse: solo se repunta el
FOREIGN KEY de `productos(id)` a `catalog_items(id)` (rebuild de 12 pasos
de SQLite, mismo patron que `_repoint_lista_precio_items_fk`).
"""
import sqlite3
from dataclasses import dataclass

from libracommerce.scripts.migrate_from_contalibra import (
    MigrationReport,
    migrate as _migrate_base,
)


@dataclass
class RestolibraMigrationReport(MigrationReport):
    recetas_repointed: bool = False
    receta_items_repointed: bool = False


def _repoint_recetas_fk(conn: sqlite3.Connection) -> bool:
    fks = conn.execute("PRAGMA foreign_key_list(recetas)").fetchall()
    if not any(row[2] == "productos" for row in fks):
        return False  # ya reapuntada, o base sin recetas

    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("ALTER TABLE recetas RENAME TO recetas_old")
        conn.execute(
            """
            CREATE TABLE recetas (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                producto_id     INTEGER NOT NULL UNIQUE REFERENCES catalog_items(id) ON DELETE CASCADE,
                rinde           REAL NOT NULL DEFAULT 1,
                rinde_unidad    TEXT NOT NULL DEFAULT 'u',
                rendimiento_pct REAL NOT NULL DEFAULT 100,
                activo          INTEGER NOT NULL DEFAULT 1,
                notas           TEXT DEFAULT '',
                created_at      TEXT DEFAULT (datetime('now')),
                updated_at      TEXT DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            "INSERT INTO recetas (id, producto_id, rinde, rinde_unidad, rendimiento_pct, "
            "activo, notas, created_at, updated_at) "
            "SELECT id, producto_id, rinde, rinde_unidad, rendimiento_pct, activo, notas, "
            "created_at, updated_at FROM recetas_old"
        )
        conn.execute("DROP TABLE recetas_old")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    return True


def _repoint_receta_items_fk(conn: sqlite3.Connection) -> bool:
    fks = conn.execute("PRAGMA foreign_key_list(receta_items)").fetchall()
    if not any(row[2] == "productos" for row in fks):
        return False

    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("ALTER TABLE receta_items RENAME TO receta_items_old")
        conn.execute(
            """
            CREATE TABLE receta_items (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                receta_id      INTEGER NOT NULL REFERENCES recetas(id) ON DELETE CASCADE,
                ingrediente_id INTEGER NOT NULL REFERENCES catalog_items(id) ON DELETE CASCADE,
                cantidad       REAL NOT NULL DEFAULT 0,
                created_at     TEXT DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            "INSERT INTO receta_items (id, receta_id, ingrediente_id, cantidad, created_at) "
            "SELECT id, receta_id, ingrediente_id, cantidad, created_at FROM receta_items_old"
        )
        conn.execute("DROP TABLE receta_items_old")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    return True


def migrate(conn: sqlite3.Connection) -> RestolibraMigrationReport:
    """Corre la migracion base (identica a Contalibra) y despues repunta
    recetas/receta_items. Misma politica que la base: falla ruidosamente si
    se corre dos veces sobre el mismo destino, pensada para una copia
    limpia (Fase 1) y luego, con confirmacion explicita, produccion real
    (Fase 4)."""
    base_report = _migrate_base(conn)
    recetas_repointed = _repoint_recetas_fk(conn)
    receta_items_repointed = _repoint_receta_items_fk(conn)
    conn.commit()
    return RestolibraMigrationReport(
        **base_report.__dict__,
        recetas_repointed=recetas_repointed,
        receta_items_repointed=receta_items_repointed,
    )
