"""Lo devuelto de un servicio (ADR-054): la tabla `sale_service_returns`.

Revision ID: 0007_devoluciones_de_servicio
Revises: 0006_clave_operacion_recepciones
Create Date: 2026-10-13

Una tabla nueva, **aditiva** y vacía: no toca una sola fila existente.

`sale_service_returns` guarda lo que se devolvió de las líneas de **servicio** de una venta (`catalog_items.item_type = 'service'`): una fila por línea devuelta y por
intento, con la venta, el producto, la variante, el depósito de la operación, la cantidad, la nota (que lleva la marca `[op:<clave>]` de la devolución idempotente), quién y cuándo.
Un servicio no tiene inventario, así que su devolución **no escribe en `stock_movements`** (hasta v0.58 escribía un `+q` sobre un ítem que no tiene stock); pero el tope de lo que
se puede devolver, el estado de la venta (`devuelta` / `devuelta_parcial`), el reintegro prorrateado, la idempotencia por `clave_operacion` y el margen salen de lo ya devuelto, y
eso tiene que quedar escrito en algún lado. Sin filas, todo se comporta como antes: la escribe sólo `erp.ventas.devolver_items`.

Corre limpia sobre las bases de Contalibra, Restolibra y VentaLibra (que comparten este motor): idempotente por introspección (la tabla se crea sólo si falta) y creada con
`op.create_table`, así que el mismo código vale en SQLite y PostgreSQL.

**Downgrade.** Baja la tabla: se pierde el registro de lo devuelto de los servicios (la caja, la cuenta corriente y el estado de las ventas no se tocan, así que un servicio ya
devuelto podría volver a devolverse). Hacer backup antes, como con cualquier operación de schema.
"""
import sqlalchemy as sa
from alembic import op
from libracore.db.migraciones import conexion_libracore

from libracommerce.db.schema import init_schema

revision = "0007_devoluciones_de_servicio"
down_revision = "0006_clave_operacion_recepciones"
branch_labels = None
depends_on = None

_TABLA = "sale_service_returns"
_INDICE = "idx_sale_service_returns_sale"


def _existe(bind) -> bool:
    return sa.inspect(bind).has_table(_TABLA)


def upgrade():
    # 🔴 **No supone que el arranque de la app ya corrió** (la misma lección de la 0005): la tabla referencia `sales`, `catalog_items`, `item_variants` y `locations`, que las crea
    # `init_schema()`, y el deploy migra ANTES de arrancar. Es idempotente (todo es `CREATE ... IF NOT EXISTS`) y es lo mismo que hace la baseline y el arranque.
    init_schema(conexion_libracore(op.get_bind()))
    if not _existe(op.get_bind()):
        op.create_table(
            _TABLA,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("sale_id", sa.Integer, sa.ForeignKey("sales.id"), nullable=False),
            sa.Column("item_id", sa.Integer, sa.ForeignKey("catalog_items.id"), nullable=False),
            sa.Column("variant_id", sa.Integer, sa.ForeignKey("item_variants.id")),
            sa.Column("location_id", sa.Integer, sa.ForeignKey("locations.id"), nullable=False),
            sa.Column("quantity", sa.Numeric, nullable=False),
            sa.Column("note", sa.Text, nullable=False, server_default=""),
            sa.Column("created_by", sa.Integer),
            sa.Column("occurred_at", sa.Text, nullable=False),
            sa.CheckConstraint("quantity > 0", name="ck_sale_service_returns_quantity"),
        )
        op.create_index(_INDICE, _TABLA, ["sale_id"])


def downgrade():
    if _existe(op.get_bind()):
        op.drop_table(_TABLA)
