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


def _migration_0003_add_stock_movement_note_reason_and_author(conn: sqlite3.Connection) -> None:
    """P7: un movimiento de stock real necesita mas que su tipo semantico.

    - `note`: texto libre que describe el movimiento ("Compra a proveedor X",
      "Anulacion venta ID 12"). Contalibra lo llama `referencia`, lo muestra
      en pantalla y lo usa en su vista de logs.
    - `created_by`: que usuario lo genero (auditoria).
    - `reason_code`: refinamiento del `movement_type` propio de cada producto.
      `movement_type` es el tipo semantico del motor (sale/purchase/
      adjustment/transfer/return/waste); un producto puede necesitar un
      vocabulario mas fino dentro de un mismo tipo — Contalibra distingue
      'entrada'/'salida'/'ajuste', los tres ADJUSTMENT para el motor pero
      con iconos y semantica distintos en su UI. Guardarlo acá evita tanto
      inflar el enum del motor con vocabulario de un solo producto como
      perder el dato al migrar.
    """
    columns = _table_columns(conn, "stock_movements")
    if "note" not in columns:
        conn.execute("ALTER TABLE stock_movements ADD COLUMN note TEXT NOT NULL DEFAULT ''")
    if "created_by" not in columns:
        conn.execute("ALTER TABLE stock_movements ADD COLUMN created_by INTEGER")
    if "reason_code" not in columns:
        conn.execute("ALTER TABLE stock_movements ADD COLUMN reason_code TEXT")


def _migration_0004_add_locations_created_at(conn: sqlite3.Connection) -> None:
    """`locations` era la unica tabla del esquema sin `created_at`. Aparecio
    al comparar la salida vieja contra la nueva del CRUD de depositos de
    Contalibra (P7): era el unico campo que se perdia en toda la migracion.

    No lleva DEFAULT CURRENT_TIMESTAMP en el ALTER: SQLite no admite un
    default no-constante al agregar una columna a una tabla existente. El
    default vive en el CREATE TABLE de `schema.py` (bases nuevas) y las filas
    preexistentes quedan en NULL, que es lo honesto — no se sabe cuando se
    crearon.
    """
    if "created_at" not in _table_columns(conn, "locations"):
        conn.execute("ALTER TABLE locations ADD COLUMN created_at TEXT")


def _migration_0005_add_stock_movements_created_at(conn: sqlite3.Connection) -> None:
    """`occurred_at` es CUANDO PASO el movimiento (fecha de negocio, la
    elige el usuario); `created_at` es cuando se REGISTRO. En un ledger
    append-only de un sistema financiero los dos importan y el segundo no se
    puede reconstruir despues — Contalibra ya lo guardaba en
    `movimientos_stock.created_at` y se habria perdido al migrar.

    Sin DEFAULT en el ALTER por la misma razon que la migracion 0004: SQLite
    no admite un default no-constante al agregar una columna.
    """
    if "created_at" not in _table_columns(conn, "stock_movements"):
        conn.execute("ALTER TABLE stock_movements ADD COLUMN created_at TEXT")


def _migration_0006_add_sales_business_fields(conn: sqlite3.Connection) -> None:
    """P7: datos que toda venta de mostrador tiene y que `sales` no modelaba.
    Ninguno es especifico de Contalibra -- VentaLibra los necesita igual.

    - `occurred_on`: fecha de negocio de la venta, la elige el usuario y puede
      no ser hoy. Distinta de `created_at` (cuando se registro) y de
      `confirmed_at` (timestamp de confirmacion).
    - `customer_name_snapshot`: nombre del cliente tal como estaba al vender.
      Una venta a consumidor final no tiene `customer_party_id` pero si puede
      llevar nombre; y si el cliente se renombra o se borra, el historico no
      debe cambiar -- mismo criterio que `SaleItem.description_snapshot`.
    - `created_by`: que usuario la registro (auditoria).
    - `notes`: observaciones libres.
    - `status_detail`: refinamiento de `status` propio de cada producto, mismo
      patron que `stock_movements.reason_code`. `status` es el estado
      semantico del motor (draft/confirmed/cancelled/...); Contalibra
      distingue ademas 'cobrada'/'parcial'/'pendiente', que es estado de
      COBRANZA, no de la venta -- no corresponde inflar el enum del motor con
      eso, pero tampoco perderlo.
    """
    columns = _table_columns(conn, "sales")
    if "occurred_on" not in columns:
        conn.execute("ALTER TABLE sales ADD COLUMN occurred_on TEXT")
    if "customer_name_snapshot" not in columns:
        conn.execute(
            "ALTER TABLE sales ADD COLUMN customer_name_snapshot TEXT NOT NULL DEFAULT ''"
        )
    if "created_by" not in columns:
        conn.execute("ALTER TABLE sales ADD COLUMN created_by INTEGER")
    if "notes" not in columns:
        conn.execute("ALTER TABLE sales ADD COLUMN notes TEXT NOT NULL DEFAULT ''")
    if "status_detail" not in columns:
        conn.execute("ALTER TABLE sales ADD COLUMN status_detail TEXT")


def _migration_0007_add_price_lists_created_at(conn: sqlite3.Connection) -> None:
    """`price_lists` era la unica tabla de precios sin `created_at`
    -- Contalibra ya lo tenia en `listas_precio` (P7b)."""
    if "created_at" not in _table_columns(conn, "price_lists"):
        conn.execute("ALTER TABLE price_lists ADD COLUMN created_at TEXT")


def _migration_0008_add_sale_payments(conn: sqlite3.Connection) -> None:
    """`sale_payments` (pago mixto + efectivo recibido) es tabla nueva, no
    una columna: `CREATE TABLE IF NOT EXISTS` en `init_schema()` ya la crea
    tanto en base fresca como en base vieja, asi que aca solo hace falta el
    indice para las bases donde la tabla ya se creo sin el.

    Se registra igual como migracion numerada para dejar rastro en
    `schema_migrations` de en que version aparecio."""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sale_payments_sale ON sale_payments(sale_id)"
    )


def _migration_0009_add_commerce_settings(conn: sqlite3.Connection) -> None:
    """`commerce_settings` (preferencias del comercio: formato de la balanza,
    de aca en mas lo que haga falta) es tabla nueva, no una columna, asi que
    el `CREATE TABLE IF NOT EXISTS` de `init_schema()` ya la crea tanto en
    base fresca como en base vieja.

    Va numerada igual, sin cuerpo, para dejar rastro en `schema_migrations`
    de en que version aparecio -- mismo criterio que la 0008.
    """


# Orden fijo: agregar al final, nunca reordenar ni reusar un numero ya
# asignado (aunque la migracion se haya borrado despues).
_MIGRATIONS: list[tuple[int, str, Callable[[sqlite3.Connection], None]]] = [
    (1, "add_variant_id_to_stock_movements_and_sale_items", _migration_0001_add_variant_id),
    (2, "add_min_stock_and_location_defaults", _migration_0002_add_min_stock_and_location_defaults),
    (3, "add_stock_movement_note_reason_and_author",
     _migration_0003_add_stock_movement_note_reason_and_author),
    (4, "add_locations_created_at", _migration_0004_add_locations_created_at),
    (5, "add_stock_movements_created_at", _migration_0005_add_stock_movements_created_at),
    (6, "add_sales_business_fields", _migration_0006_add_sales_business_fields),
    (7, "add_price_lists_created_at", _migration_0007_add_price_lists_created_at),
    (8, "add_sale_payments", _migration_0008_add_sale_payments),
    (9, "add_commerce_settings", _migration_0009_add_commerce_settings),
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
