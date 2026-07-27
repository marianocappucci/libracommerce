"""Migraciones de esquema para bases ya existentes, creadas con una
version anterior de `init_schema()`.

`CREATE TABLE IF NOT EXISTS` no alcanza para evolucionar el esquema: si
la tabla ya existe en el archivo, la sentencia es un no-op silencioso --
ninguna columna nueva se agrega, ningun `CHECK` nuevo se aplica. Esto es
invisible en desarrollo (los tests siempre arrancan de una base
`:memory:`/temporal fresca) pero rompe cualquier despliegue real con
datos ya persistidos apenas se agrega una columna a una tabla existente
-- bug real encontrado el 2026-07-26 al redeployar `ventalibra-dev` con
la Fase 4 (item_variants agrego `variant_id` a `stock_movements` y
`sale_items`, ninguno de los dos se aplico a la base vieja, el
contenedor entro en crash loop con `no such column: variant_id`).

Cada migracion:
- Tiene un numero de version fijo, nunca reordenado ni reusado.
- Es idempotente por si misma (chequea `PRAGMA table_info` antes de
  tocar nada) ademas de estar trackeada en `schema_migrations` -- correr
  `init_schema()` dos veces, o contra una base ya migrada, nunca falla
  ni duplica trabajo.
- Corre en cada `init_schema()`, tanto en una base fresca (donde ya
  encuentra las columnas nuevas porque el `CREATE TABLE IF NOT EXISTS`
  las incluyo desde el vamos, y se vuelve un no-op) como en una base
  vieja real (donde efectivamente altera el esquema).
"""
import sqlite3
from typing import Callable


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migration_0001_add_variant_id(conn: sqlite3.Connection) -> None:
    """item_variants (Fase 4) agrego `variant_id` a `stock_movements` y
    `sale_items`, ninguna de las dos existia antes de esa version."""
    if "variant_id" not in _table_columns(conn, "stock_movements"):
        conn.execute(
            "ALTER TABLE stock_movements ADD COLUMN variant_id INTEGER REFERENCES item_variants(id)"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_stock_item_variant_location "
        "ON stock_movements(item_id, variant_id, location_id)"
    )

    if "variant_id" not in _table_columns(conn, "sale_items"):
        # sale_items necesita el rebuild de 12 pasos que recomienda la
        # documentacion de SQLite, no un ALTER TABLE ADD COLUMN simple:
        # el CHECK (variant_id IS NULL OR item_id IS NOT NULL) referencia
        # otra columna, y SQLite no permite agregar un CHECK multi-columna
        # via ADD COLUMN -- solo recreando la tabla entera se puede sumar.
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("ALTER TABLE sale_items RENAME TO sale_items_old")
            conn.execute(
                """
                CREATE TABLE sale_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sale_id INTEGER NOT NULL REFERENCES sales(id),
                    kind TEXT NOT NULL,
                    item_id INTEGER REFERENCES catalog_items(id),
                    variant_id INTEGER REFERENCES item_variants(id),
                    description_snapshot TEXT NOT NULL,
                    quantity NUMERIC NOT NULL,
                    unit_price NUMERIC NOT NULL,
                    discount_amount NUMERIC NOT NULL DEFAULT 0,
                    tax_rate NUMERIC NOT NULL DEFAULT 0,
                    tax_amount NUMERIC NOT NULL DEFAULT 0,
                    unit_cost_snapshot NUMERIC,
                    CHECK (kind != 'product' OR item_id IS NOT NULL),
                    CHECK (variant_id IS NULL OR item_id IS NOT NULL)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO sale_items
                    (id, sale_id, kind, item_id, variant_id, description_snapshot, quantity,
                     unit_price, discount_amount, tax_rate, tax_amount, unit_cost_snapshot)
                SELECT id, sale_id, kind, item_id, NULL, description_snapshot, quantity,
                       unit_price, discount_amount, tax_rate, tax_amount, unit_cost_snapshot
                FROM sale_items_old
                """
            )
            conn.execute("DROP TABLE sale_items_old")
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sale_items_sale ON sale_items(sale_id)")


def _migration_0002_add_min_stock_and_location_defaults(conn: sqlite3.Connection) -> None:
    """P7 (migracion Contalibra -> LibraCommerce): `catalog_items.min_stock`
    (equivalente a `productos.stock_minimo` de Contalibra, usado para alertas
    de stock bajo) y `locations.is_default`/`description` (equivalentes a
    `depositos.es_default`/`descripcion`)."""
    if "min_stock" not in _table_columns(conn, "catalog_items"):
        conn.execute("ALTER TABLE catalog_items ADD COLUMN min_stock NUMERIC NOT NULL DEFAULT 0")

    if "description" not in _table_columns(conn, "locations"):
        conn.execute("ALTER TABLE locations ADD COLUMN description TEXT NOT NULL DEFAULT ''")
    if "is_default" not in _table_columns(conn, "locations"):
        conn.execute("ALTER TABLE locations ADD COLUMN is_default INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_locations_one_default "
        "ON locations(is_default) WHERE is_default = 1"
    )


# Orden fijo: agregar al final, nunca reordenar ni reusar un numero ya
# asignado (aunque la migracion se haya borrado despues).
_MIGRATIONS: list[tuple[int, str, Callable[[sqlite3.Connection], None]]] = [
    (1, "add_variant_id_to_stock_movements_and_sale_items", _migration_0001_add_variant_id),
    (2, "add_min_stock_and_location_defaults", _migration_0002_add_min_stock_and_location_defaults),
]


def run_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()}
    for version, name, migrate in _MIGRATIONS:
        if version in applied:
            continue
        migrate(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (?, ?)", (version, name)
        )
    conn.commit()
