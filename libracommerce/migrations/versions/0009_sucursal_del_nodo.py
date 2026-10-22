"""El nodo sabe su sucursal (ADR-071, enmienda de v0.72.0): la tabla `nodo_local`.

Revision ID: 0009_sucursal_del_nodo
Revises: 0008_origen_del_stock
Create Date: 2026-10-22

Un nodo offline de LibraEdge mueve el stock de **su** sucursal, pero el espejo le trae el `locations.is_default` del central (el de «Casa central»), así que un
movimiento sin `deposito_id` (el ajuste de ContaLibra, la producción, una venta sin depósito) caía en un depósito que no es del nodo y el central lo mandaba a
revisión manual (`DepositoFueraDeLaSucursal`). El arreglo es que **en un nodo el depósito por defecto sea el de su sucursal**, y para eso el nodo tiene que saber
cuál es: no está en el entorno ni en `node_identity` (esa tabla es del central).

Una tabla nueva, **aditiva** y chica, propia de LibraCommerce (no toca las de LibraEdge): `nodo_local(clave TEXT PRIMARY KEY, valor TEXT NOT NULL)`. Hoy guarda
una sola clave, `sucursal_id`, que escribe `offline_stock.aplicar_carga_inicial` en la misma transacción que los ajustes de la carga inicial. Como la `0006`, la
`0007` y la `0008`, la tabla es de la revisión y **no** de `init_schema()` (el schema congelado no cambia): el central y una instalación común no la necesitan
y sin ella todo se comporta como antes.

**Nodos ya instalados.** La revisión **deduce** la sucursal de lo que el nodo ya tiene: las filas del ledger con `reason_code = 'carga_inicial'` las escribe sólo
`aplicar_carga_inicial` y sólo en depósitos de la sucursal del nodo. Si todas son de **una** sucursal, la guarda (y el nodo queda arreglado sin más pasos); si no
hay ninguna (la sucursal no tenía stock al vincular) o hay de varias, no guarda nada y queda igual que antes hasta que se corra `libraedge-nodo carga-inicial
--forzar`, que vuelve a llamar a `aplicar_carga_inicial` (delta 0) y la guarda. En el central y en una instalación común no hay filas así: la tabla queda vacía.

Corre limpia sobre las bases de Contalibra, Restolibra y VentaLibra (que comparten este motor): idempotente por introspección (la tabla se crea sólo si falta, y la
sucursal se deduce sólo si la clave no está) y creada con `op.create_table`, así que el mismo código vale en SQLite y PostgreSQL.

**Downgrade.** Baja la tabla: el nodo olvida su sucursal y vuelve a usar el depósito por defecto de la instancia (el defecto de antes). No toca el ledger. Se recupera
volviendo a subir la revisión (deduce otra vez) o con `carga-inicial --forzar`.
"""
import logging

import sqlalchemy as sa
from alembic import op
from libracore.db.migraciones import conexion_libracore

from libracommerce.db.schema import init_schema

revision = "0009_sucursal_del_nodo"
down_revision = "0008_origen_del_stock"
branch_labels = None
depends_on = None

_TABLA = "nodo_local"
_CLAVE = "sucursal_id"
_MOTIVO_CARGA_INICIAL = "carga_inicial"      # offline_stock.MOTIVO_CARGA_INICIAL: acá no se importa el módulo de ERP

logger = logging.getLogger("alembic.runtime.migration")


def _existe(bind) -> bool:
    return sa.inspect(bind).has_table(_TABLA)


def _deducir_sucursal(bind) -> None:
    """Guarda la sucursal del nodo si las filas `carga_inicial` del ledger son todas de una sola y la clave todavía no está."""
    if bind.execute(sa.text(f"SELECT 1 FROM {_TABLA} WHERE clave = :clave"), {"clave": _CLAVE}).fetchone() is not None:
        return
    sucursales = [f[0] for f in bind.execute(sa.text(
        "SELECT DISTINCT l.branch_id FROM stock_movements sm JOIN locations l ON l.id = sm.location_id "
        "WHERE sm.reason_code = :motivo AND l.branch_id IS NOT NULL"), {"motivo": _MOTIVO_CARGA_INICIAL}).fetchall()]
    if len(sucursales) > 1:
        logger.warning("nodo_local: la carga inicial toca varias sucursales %s; no se deduce la del nodo (correr carga-inicial --forzar)", sorted(sucursales))
    if len(sucursales) == 1:
        bind.execute(sa.text(f"INSERT INTO {_TABLA} (clave, valor) VALUES (:clave, :valor)"), {"clave": _CLAVE, "valor": str(sucursales[0])})


def upgrade():
    # 🔴 **No supone que el arranque de la app ya corrió** (la misma lección de la 0005): la deducción lee `stock_movements` y `locations`, que las crea `init_schema()`, y el
    # deploy migra ANTES de arrancar. Es idempotente (todo es `CREATE ... IF NOT EXISTS`) y es lo mismo que hace la baseline y el arranque.
    init_schema(conexion_libracore(op.get_bind()))
    if not _existe(op.get_bind()):
        op.create_table(
            _TABLA,
            sa.Column("clave", sa.Text, primary_key=True),
            sa.Column("valor", sa.Text, nullable=False),
        )
    _deducir_sucursal(op.get_bind())


def downgrade():
    if _existe(op.get_bind()):
        op.drop_table(_TABLA)
