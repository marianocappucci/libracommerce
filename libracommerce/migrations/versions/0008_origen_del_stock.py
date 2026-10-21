"""El stock del nodo sube al central (ADR-071): `stock_movements.origen` y `origen_id`.

Revision ID: 0008_origen_del_stock
Revises: 0007_devoluciones_de_servicio
Create Date: 2026-10-21

Un nodo offline de LibraEdge mueve el stock de su sucursal sin ver al central (ajustes, mermas, entradas y salidas a mano,
transferencias) y todo eso sube como `stock.movimiento`. El central tiene que saber **que una fila vino de un nodo y cuál era su
id allá**, para aplicar dos veces la misma operación sin duplicar nada (el outbox entrega al menos una vez) y para traducir la
entrada de una transferencia a la salida que ya subió. Dos columnas **aditivas** y **opcionales** y un índice único parcial:

- `stock_movements.origen TEXT` (NULL): el `node_id` del nodo. NULL = una fila propia del central (todas las que existen hoy).
- `stock_movements.origen_id INTEGER` (NULL): el id de la fila en la base del nodo. Sin clave foránea: es un id de otra base.
- `idx_stock_movements_origen`: único **sólo sobre las filas con origen** (`WHERE origen IS NOT NULL`), así las filas propias no
  compiten entre sí. Es la red de seguridad detrás de la pregunta del aplicador (`offline_stock.movimiento_del_central`): si dos
  aplicaciones de la misma operación corrieran a la vez, la base rechaza la segunda en vez de duplicar el movimiento.

No toca una sola fila existente. Como la `0006`, las columnas son de la revisión y **no** de `init_schema()` (el schema congelado
no cambia): un nodo no las necesita (escribe sus filas sin origen) y un central sin esta revisión rechaza `stock.movimiento` con
un mensaje que pide `libracommerce-migrar upgrade` (revisión manual en el nodo, no traba la cola).

Corre limpia sobre las bases de Contalibra, Restolibra y VentaLibra (que comparten este motor): idempotente por introspección
(cada columna y el índice se crean sólo si faltan), con el mismo texto en SQLite y PostgreSQL.

**Downgrade.** Baja el índice y las columnas: se pierde de qué nodo vino cada fila, y con eso la idempotencia: el próximo
reintento del outbox duplicaría los movimientos que ya se aplicaron. Hacer backup antes, como con cualquier operación de schema;
no bajarla en un central con nodos activos.
"""
import sqlalchemy as sa
from alembic import op
from libracore.db.migraciones import conexion_libracore

from libracommerce.db.schema import init_schema

revision = "0008_origen_del_stock"
down_revision = "0007_devoluciones_de_servicio"
branch_labels = None
depends_on = None

_TABLA = "stock_movements"
_COLUMNAS = (("origen", "TEXT"), ("origen_id", "INTEGER"))
_INDICE = "idx_stock_movements_origen"


def _columnas(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(_TABLA)}


def _tiene_indice(bind) -> bool:
    return _INDICE in {i["name"] for i in sa.inspect(bind).get_indexes(_TABLA)}


def upgrade():
    # 🔴 **No supone que el arranque de la app ya corrió** (la misma lección de la 0005): el deploy migra ANTES de arrancar, y la tabla la crea `init_schema()`. Es idempotente
    # (todo es `CREATE ... IF NOT EXISTS`) y es lo mismo que hace la baseline y el arranque, así que no cambia nada que ya esté.
    init_schema(conexion_libracore(op.get_bind()))
    existentes = _columnas(op.get_bind())
    for nombre, tipo in _COLUMNAS:
        if nombre not in existentes:
            op.execute(f"ALTER TABLE {_TABLA} ADD COLUMN {nombre} {tipo}")
    # Sin `IF NOT EXISTS` en el SQL: la introspección de Alembic ve los índices parciales de los dos motores, y así el texto es uno solo.
    if not _tiene_indice(op.get_bind()):
        op.execute(f"CREATE UNIQUE INDEX {_INDICE} ON {_TABLA}(origen, origen_id) WHERE origen IS NOT NULL")


def downgrade():
    if _tiene_indice(op.get_bind()):
        op.drop_index(_INDICE, table_name=_TABLA)
    existentes = _columnas(op.get_bind())
    for nombre, _tipo in reversed(_COLUMNAS):
        if nombre in existentes:
            op.drop_column(_TABLA, nombre)
