"""Vencimientos y lotes (A-1, ADR-018): `catalog_items.tracks_expiry` y un índice por lote.

Revision ID: 0002_vencimientos_lotes
Revises: 0001_baseline_commerce
Create Date: 2026-09-30

Dos cambios, los dos **aditivos** y sin tocar una sola fila existente:

1. `catalog_items.tracks_expiry INTEGER NOT NULL DEFAULT 0`: la marca opt-in de «este producto vence». Todos los
   productos que ya existen quedan en 0 (no vencen) y así se comportan igual que hoy. La escribe sólo
   `erp.vencimientos.marcar_vence`; nada del camino de ventas la lee (eso es A-4).
2. `idx_stock_item_location_lot` sobre `stock_movements(item_id, location_id, lot_code)`, el índice que necesita
   agrupar existencias por lote. **Sin índice parcial** a propósito: el mismo `CREATE INDEX` corre idéntico en
   SQLite y PostgreSQL.

`stock_movements.lot_code` y `expires_at` **ya existían** desde la baseline: no hay tabla `lots` ni columna nueva
en el ledger (ver ADR-018).

**Por qué es una revisión y no una línea en `init_schema()`**: esa función y la cadena numerada de
`db/migrations.py` quedaron de sólo lectura en la baseline (`tests/test_schema_congelado.py`). Por eso las fixtures
del gate **no cambian** con esta revisión: congelan lo que produce `init_schema()`, que no se toca. Lo que verifica
esta revisión es `tests/test_vencimientos.py` (upgrade sobre una base con datos previos, en los dos motores).

**Corre limpia sobre las bases de Contalibra, Restolibra y VentaLibra** (que comparten este motor): es idempotente
por introspección —la columna se agrega sólo si falta— y el `CREATE INDEX` lleva `IF NOT EXISTS`. Un producto que no
marque nada no ve ninguna diferencia: el ledger no cambia.

**Downgrade.** Baja el índice y la columna. Perder la columna borra la marca «vence» de los productos (no la
información de lotes: `lot_code` y `expires_at` viven en el ledger, que es aditivo y no se toca). Hacer backup antes,
como con cualquier operación de schema.
"""
import sqlalchemy as sa
from alembic import op

revision = "0002_vencimientos_lotes"
down_revision = "0001_baseline_commerce"
branch_labels = None
depends_on = None

_INDICE = "idx_stock_item_location_lot"


def _columnas(bind, tabla: str) -> set[str]:
    """Qué columnas tiene ya `tabla`: la revisión corre sobre bases vivas y tiene que poder correr dos veces."""
    return {c["name"] for c in sa.inspect(bind).get_columns(tabla)}


def upgrade():
    if "tracks_expiry" not in _columnas(op.get_bind(), "catalog_items"):
        # El mismo texto en los dos motores (es el estilo de `db/migrations.py`, p. ej. `min_stock`).
        op.execute("ALTER TABLE catalog_items ADD COLUMN tracks_expiry INTEGER NOT NULL DEFAULT 0")
    op.execute(f"CREATE INDEX IF NOT EXISTS {_INDICE} ON stock_movements(item_id, location_id, lot_code)")


def downgrade():
    op.execute(f"DROP INDEX IF EXISTS {_INDICE}")
    if "tracks_expiry" in _columnas(op.get_bind(), "catalog_items"):
        op.drop_column("catalog_items", "tracks_expiry")
