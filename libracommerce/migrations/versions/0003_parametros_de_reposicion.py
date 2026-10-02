"""Parámetros de reposición por producto (ADR-020): `catalog_items.lead_time_days` y `catalog_items.max_stock`.

Revision ID: 0003_parametros_reposicion
Revises: 0002_vencimientos_lotes
Create Date: 2026-10-02

Dos columnas, las dos **aditivas**, **opcionales** y sin tocar una sola fila existente:

1. `catalog_items.lead_time_days INTEGER` (NULL): el plazo de entrega propio del producto, en días. NULL = usa el plazo
   general de la consulta de reposición (el de siempre).
2. `catalog_items.max_stock NUMERIC` (NULL): el techo de stock del producto. NULL = sin techo (el de siempre).

Todos los productos que ya existen quedan en NULL y la reposición les da lo mismo que antes. Sin valor por defecto a
propósito: «no definido» no es lo mismo que 0 (un techo de 0 sería «no tener nunca nada»).

**Corre limpia sobre las bases de Contalibra, Restolibra y VentaLibra** (que comparten este motor): es idempotente por
introspección, cada columna se agrega sólo si falta, con el mismo texto en SQLite y PostgreSQL.

**Downgrade.** Baja las dos columnas; se pierden los plazos y techos cargados (nada más: el ledger no se toca). Hacer
backup antes, como con cualquier operación de schema.
"""
import sqlalchemy as sa
from alembic import op

revision = "0003_parametros_reposicion"
down_revision = "0002_vencimientos_lotes"
branch_labels = None
depends_on = None

_COLUMNAS = (("lead_time_days", "INTEGER"), ("max_stock", "NUMERIC"))


def _columnas(bind, tabla: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(tabla)}


def upgrade():
    existentes = _columnas(op.get_bind(), "catalog_items")
    for nombre, tipo in _COLUMNAS:
        if nombre not in existentes:
            op.execute(f"ALTER TABLE catalog_items ADD COLUMN {nombre} {tipo}")


def downgrade():
    existentes = _columnas(op.get_bind(), "catalog_items")
    for nombre, _ in reversed(_COLUMNAS):
        if nombre in existentes:
            op.drop_column("catalog_items", nombre)
