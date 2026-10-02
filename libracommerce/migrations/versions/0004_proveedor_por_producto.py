"""Proveedor habitual por producto (ADR-021): `catalog_items.supplier_party_id`.

Revision ID: 0004_proveedor_por_producto
Revises: 0003_parametros_reposicion
Create Date: 2026-10-02

Una columna, **aditiva**, **opcional** y sin tocar una sola fila existente:

`catalog_items.supplier_party_id INTEGER REFERENCES parties(id)` (NULL): el proveedor al que se le suele pedir ese producto.
NULL = sin proveedor definido (el de siempre). La escribe sólo `erp.reposicion.fijar_parametros`; la leen la reposición
sugerida (para mostrarlo y filtrar por proveedor) y, más adelante, la orden de compra en borrador.

Corre limpia sobre las bases de Contalibra, Restolibra y VentaLibra (que comparten este motor): idempotente por introspección,
la columna se agrega sólo si falta, con el mismo texto en SQLite y PostgreSQL (SQLite admite `REFERENCES` en un `ADD COLUMN`
con default NULL).

**Downgrade.** Baja la columna: se pierden los proveedores habituales cargados (nada más: el ledger y las órdenes de compra no
se tocan). Hacer backup antes, como con cualquier operación de schema.
"""
import sqlalchemy as sa
from alembic import op

revision = "0004_proveedor_por_producto"
down_revision = "0003_parametros_reposicion"
branch_labels = None
depends_on = None


def _columnas(bind, tabla: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(tabla)}


def upgrade():
    if "supplier_party_id" not in _columnas(op.get_bind(), "catalog_items"):
        op.execute("ALTER TABLE catalog_items ADD COLUMN supplier_party_id INTEGER REFERENCES parties(id)")


def downgrade():
    if "supplier_party_id" in _columnas(op.get_bind(), "catalog_items"):
        op.drop_column("catalog_items", "supplier_party_id")
