"""El vínculo propio del producto sobrevive a la venta que sube de un nodo (ADR-072): la tabla `venta_origen_nodo`.

Revision ID: 0010_vinculo_de_la_venta
Revises: 0009_sucursal_del_nodo
Create Date: 2026-10-23

Hasta ADR-071 el aplicador de `venta.registrada` marcaba la venta del central con `sales.source_type = 'libraedge:<node_id>'` y `sales.source_id = <id en el nodo>`: era la
clave con que reconocía a la venta en las operaciones siguientes (pago acreditado, anulación, devolución, factura) y para no crearla dos veces. Pero esas dos columnas son también
**el vínculo que un producto le pone a su venta**: el buffet de LibraClub ata el consumo a la reserva con `source_type = 'reserva'` y `source_id = <id de la reserva>`, y los
consumos de una reserva se buscan por ahí. El aplicador del nodo las pisaba y el consumo dejaba de pertenecer a su reserva.

La salida es que la identidad de nodo deje de vivir en esas columnas **cuando la venta trae un vínculo propio**: una tabla chica y aditiva, `venta_origen_nodo(node_id, venta_id_nodo,
venta_id)`, con una fila por venta del central que subió de un nodo **con** vínculo. Una venta sin vínculo (todas las de hoy) sigue marcada `libraedge:<node_id>` y no escribe en la
tabla. Como la `0006` a la `0009`, la tabla es de la revisión y **no** de `init_schema()` (el schema congelado no cambia): un nodo y una instalación común no la necesitan, y sin
ella todo se comporta como antes. Un central sin la revisión rechaza (revisión manual, `VentaSinRevision`) la venta que traiga un vínculo, en vez de perderlo en silencio.

Corre limpia sobre las bases de Contalibra, Restolibra, VentaLibra y LibraClub (que comparten este motor): no toca ninguna fila existente (la tabla nace vacía), es idempotente por
introspección y está creada con `op.create_table`, así que el mismo código vale en SQLite y PostgreSQL. `venta_id` es único (una venta del central viene de una sola del nodo) y
borra en cascada con la venta.

**Downgrade.** Baja la tabla: las ventas del central que se anotaron ahí conservan su vínculo del producto pero el central deja de reconocerlas como "la venta N del nodo X", así que
sus operaciones siguientes (si las hay) irían a revisión manual (`VentaNoSincronizada`). Volver a subir la revisión crea la tabla vacía: no reconstruye lo anotado.
"""
import sqlalchemy as sa
from alembic import op
from libracore.db.migraciones import conexion_libracore

from libracommerce.db.schema import init_schema

revision = "0010_vinculo_de_la_venta"
down_revision = "0009_sucursal_del_nodo"
branch_labels = None
depends_on = None

_TABLA = "venta_origen_nodo"
_INDICE = "idx_venta_origen_nodo_venta"


def _existe(bind) -> bool:
    return sa.inspect(bind).has_table(_TABLA)


def upgrade():
    # 🔴 **No supone que el arranque de la app ya corrió** (la misma lección de la 0005 y la 0009): la tabla referencia a `sales`, que la crea `init_schema()`, y el deploy migra
    # ANTES de arrancar. Es idempotente (todo es `CREATE ... IF NOT EXISTS`) y es lo mismo que hace la baseline y el arranque.
    init_schema(conexion_libracore(op.get_bind()))
    if _existe(op.get_bind()):
        return
    op.create_table(
        _TABLA,
        sa.Column("node_id", sa.Text, nullable=False),
        sa.Column("venta_id_nodo", sa.BigInteger, nullable=False),
        sa.Column("venta_id", sa.Integer, sa.ForeignKey("sales.id", ondelete="CASCADE"), nullable=False),
        sa.PrimaryKeyConstraint("node_id", "venta_id_nodo"),
    )
    op.create_index(_INDICE, _TABLA, ["venta_id"], unique=True)


def downgrade():
    if _existe(op.get_bind()):
        op.drop_table(_TABLA)
