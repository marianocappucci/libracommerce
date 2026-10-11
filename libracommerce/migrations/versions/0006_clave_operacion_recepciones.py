"""La clave de operación de «Recibir mercadería» (ADR-046): `purchase_receipts.clave_operacion`.

Revision ID: 0006_clave_operacion_recepciones
Revises: 0005_min_stock_por_sucursal
Create Date: 2026-10-11

Una columna **aditiva**, **opcional** y sin tocar una sola fila existente, más un índice único parcial:

`purchase_receipts.clave_operacion TEXT` (NULL): la clave que el cliente generó para el intento que creó la recepción. NULL = una recepción hecha por las rutas de siempre (crear,
cargar líneas, confirmar), que no llevan clave: todas las existentes quedan así. La escribe sólo `erp.compras.recibir_mercaderia`; la usa para reconocer un reintento y no mover el stock dos veces.

`idx_purchase_receipts_clave_operacion` es único **sólo sobre las filas con clave** (`WHERE clave_operacion IS NOT NULL`): las recepciones sin clave no compiten entre sí. Es la red de
seguridad detrás del candado de la clave: si dos pedidos con la misma clave llegaran a la vez y el candado fallara, la base rechaza el segundo en vez de duplicar el ingreso.

Corre limpia sobre las bases de Contalibra, Restolibra y VentaLibra (que comparten este motor): idempotente por introspección (la columna y el índice se crean sólo si faltan), con el
mismo texto en SQLite y PostgreSQL (`TEXT` y el índice parcial valen en los dos).

**Downgrade.** Baja el índice y la columna: se pierden las claves guardadas (nada más: las recepciones, el ledger y las órdenes no se tocan). Un reintento de una recepción hecha con clave deja de
reconocerse. Hacer backup antes, como con cualquier operación de schema.
"""
import sqlalchemy as sa
from alembic import op
from libracore.db.migraciones import conexion_libracore

from libracommerce.db.schema import init_schema

revision = "0006_clave_operacion_recepciones"
down_revision = "0005_min_stock_por_sucursal"
branch_labels = None
depends_on = None

_TABLA = "purchase_receipts"
_COLUMNA = "clave_operacion"
_INDICE = "idx_purchase_receipts_clave_operacion"


def _tiene_columna(bind) -> bool:
    return _COLUMNA in {c["name"] for c in sa.inspect(bind).get_columns(_TABLA)}


def _tiene_indice(bind) -> bool:
    return _INDICE in {i["name"] for i in sa.inspect(bind).get_indexes(_TABLA)}


def upgrade():
    # 🔴 **No supone que el arranque de la app ya corrió** (la misma lección de la 0005): el deploy migra ANTES de arrancar, y la tabla la crea `init_schema()`. Es idempotente
    # (todo es `CREATE ... IF NOT EXISTS`) y es lo mismo que hace la baseline y el arranque, así que no cambia nada que ya esté.
    init_schema(conexion_libracore(op.get_bind()))
    if not _tiene_columna(op.get_bind()):
        op.execute(f"ALTER TABLE {_TABLA} ADD COLUMN {_COLUMNA} TEXT")
    # Sin `IF NOT EXISTS` en el SQL: la introspección de Alembic ve los índices parciales de los dos motores, y así el texto es uno solo.
    if not _tiene_indice(op.get_bind()):
        op.execute(f"CREATE UNIQUE INDEX {_INDICE} ON {_TABLA}({_COLUMNA}) WHERE {_COLUMNA} IS NOT NULL")


def downgrade():
    if _tiene_indice(op.get_bind()):
        op.drop_index(_INDICE, table_name=_TABLA)
    if _tiene_columna(op.get_bind()):
        op.drop_column(_TABLA, _COLUMNA)
