"""Stock mínimo por sucursal (ADR-024): la tabla `item_branch_min_stock`.

Revision ID: 0005_min_stock_por_sucursal
Revises: 0004_proveedor_por_producto
Create Date: 2026-10-03

Una tabla nueva, **aditiva** y vacía: no toca una sola fila existente.

`item_branch_min_stock(item_id, branch_id, min_stock NUMERIC NOT NULL CHECK (min_stock >= 0), PRIMARY KEY (item_id, branch_id))`, con FK a `catalog_items`
y a `branches`. Cada fila es el piso de reposición de UN producto en UNA sucursal; sin fila, esa sucursal usa el `catalog_items.min_stock`
global (el de siempre), así que una base recién migrada se comporta exactamente igual que antes. La escribe sólo
`erp.reposicion.fijar_minimo_sucursal`; la lee la reposición sugerida cuando se la pide con `sucursal_id`.

Corre limpia sobre las bases de Contalibra, Restolibra y VentaLibra (que comparten este motor): idempotente por introspección (la
tabla se crea sólo si falta), con el mismo texto en SQLite y PostgreSQL (`NUMERIC`, `INTEGER` y `REFERENCES` valen en los dos).

**Downgrade.** Baja la tabla: se pierden los mínimos por sucursal cargados (nada más: el global, el ledger y las órdenes no se tocan, y
cada sucursal vuelve a usar el mínimo global). Hacer backup antes, como con cualquier operación de schema.
"""
import sqlalchemy as sa
from alembic import op
from libracore.db.migraciones import conexion_libracore

from libracommerce.db.schema import init_schema

revision = "0005_min_stock_por_sucursal"
down_revision = "0004_proveedor_por_producto"
branch_labels = None
depends_on = None

_TABLA = "item_branch_min_stock"


def _existe(bind) -> bool:
    return sa.inspect(bind).has_table(_TABLA)


def upgrade():
    # 🔴 **No supone que el arranque de la app ya corrió.** La tabla referencia `branches`, y `branches` la crea
    # `init_schema()` —el arranque—, no la cadena: una instancia que migró con la baseline de hace meses (Compulibra) no la
    # tiene, y el deploy migra ANTES de arrancar. Medido sobre una copia real: sin esto, `CREATE TABLE` moría con
    # `relation "branches" does not exist` y el deploy entero se abortaba. `init_schema()` es idempotente (todo es
    # `CREATE ... IF NOT EXISTS`) y es lo mismo que hace la baseline y el arranque, así que no cambia nada que ya esté.
    init_schema(conexion_libracore(op.get_bind()))
    if not _existe(op.get_bind()):
        op.execute(
            f"""CREATE TABLE {_TABLA} (
                item_id INTEGER NOT NULL REFERENCES catalog_items(id),
                branch_id INTEGER NOT NULL REFERENCES branches(id),
                min_stock NUMERIC NOT NULL CHECK (min_stock >= 0),
                PRIMARY KEY (item_id, branch_id)
            )"""
        )


def downgrade():
    if _existe(op.get_bind()):
        op.drop_table(_TABLA)
