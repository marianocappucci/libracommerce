"""Los tres puntos de extensión del ledger de stock, a nivel de módulo (ADR-071).

El ledger (`stock_movements`) tiene **dos escritores físicos**: `erp.stock.add_movimiento_stock` (lo que usan las capas
ERP de los productos: ventas, ajustes, mermas, lotes) y `db.repository.SqliteCommerceRepository.append_stock_movement`
(el repositorio hexagonal: transferencias y recepciones de compra). Un nodo offline de LibraEdge tiene que enterarse de
**cada fila** que se escribe en cualquiera de los dos, y el central tiene que poder negarse a escribir en el depósito de
una sucursal que ya tiene nodo. Ninguno de los dos recibe ganchos por parámetro (cambiarles la firma es tocar a todos los
productos), así que van registrados acá, como `libracore.altas_en_nodo.configurar`: una vez, al armar la app.

| Punto | Cuándo corre | Para qué |
|---|---|---|
| `configurar_antes_de_mover(fn)` | `fn(conn, deposito_id)`, **antes** del `INSERT`, con el depósito ya resuelto | la guarda del central (`offline_stock.DepositoDeSucursalConNodo`) |
| `configurar_al_mover(fn)` | `fn(conn, movimiento)`, **después** del `INSERT`, con la misma conexión y **dentro de la transacción** | el encolado del nodo (`stock.movimiento`) |
| `configurar_deposito_por_defecto(fn)` | `fn(conn)`, devuelve un id o `None`, cada vez que `catalogo.get_default_deposito_id` resuelve el depósito de un movimiento sin `deposito_id` | el depósito por defecto **de un nodo** es el de su sucursal, no el de la instancia (`offline_stock.deposito_por_defecto_del_nodo`) |

`movimiento` es un `dict` con la fila tal como quedó en el ledger: `id`, `item_id`, `variant_id`, `location_id`,
`movement_type`, `quantity_delta`, `occurred_at`, `source_type`, `source_id`, `unit_cost`, `lot_code`, `expires_at`, `note`,
`created_by`, `reason_code`. Se lee de la base (no se arma con lo que se pasó al `INSERT`): es lo que el central va a
reproducir.

El tercero no escribe nada ni frena nada: **sólo contesta una pregunta de lectura**. Si devuelve un id, `get_default_deposito_id` lo da como el depósito por
defecto; si devuelve `None` (o no hay gancho), sigue el comportamiento de siempre (el marcado `is_default` de la instancia).

Un gancho de los dos primeros que levanta frena el movimiento: en `add_movimiento_stock` sube la excepción al llamador (que revierte su
transacción); en el repositorio, fuera de `transaction()`, se revierte lo escrito antes de dejarla subir (nunca queda un
movimiento sin su operación). Sin `configurar_*` (o con `None`) no pasa nada: el camino de siempre.

🔴 Es **global y del proceso**, como `altas_en_nodo`: registrar otra reemplaza a la anterior. Los tests tienen que
dejarlo en `None` al terminar.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

#: Las columnas del ledger que ve `configurar_al_mover`, en el orden del `SELECT`.
COLUMNAS = (
    "id", "item_id", "variant_id", "location_id", "movement_type", "quantity_delta", "occurred_at",
    "source_type", "source_id", "unit_cost", "lot_code", "expires_at", "note", "created_by", "reason_code",
)

AntesDeMover = Callable[[Any, int], None]
AlMover = Callable[[Any, dict], None]
DepositoPorDefecto = Callable[[Any], int | None]

_antes: AntesDeMover | None = None
_despues: AlMover | None = None
_deposito: DepositoPorDefecto | None = None


def configurar_antes_de_mover(fn: AntesDeMover | None) -> None:
    """Registra (o saca, con `None`) el gancho `fn(conn, deposito_id)` que corre antes de cada `INSERT` del ledger."""
    global _antes
    _antes = fn


def configurar_al_mover(fn: AlMover | None) -> None:
    """Registra (o saca, con `None`) el gancho `fn(conn, movimiento)` que corre después de cada `INSERT` del ledger,
    dentro de la transacción del llamador."""
    global _despues
    _despues = fn


def configurar_deposito_por_defecto(fn: DepositoPorDefecto | None) -> None:
    """Registra (o saca, con `None`) el resolvedor `fn(conn) -> int | None` del depósito por defecto. Es global y reemplaza al anterior."""
    global _deposito
    _deposito = fn


def deposito_por_defecto(conn: Any) -> int | None:
    """Lo que contesta el resolvedor registrado, o `None` si no hay ninguno (o no sabe): quien llama sigue con su comportamiento de siempre."""
    return None if _deposito is None else _deposito(conn)


def antes_de_mover(conn: Any, deposito_id: int) -> None:
    """Lo llaman los dos escritores del ledger, con el depósito ya resuelto y antes de escribir."""
    if _antes is not None:
        _antes(conn, deposito_id)


def despues_de_mover(conn: Any, movimiento_id: int) -> None:
    """Lo llaman los dos escritores del ledger apenas insertan. Sin gancho registrado no lee nada."""
    if _despues is None:
        return
    fila = conn.execute(f"SELECT {', '.join(COLUMNAS)} FROM stock_movements WHERE id = ?", (movimiento_id,)).fetchone()
    # Por posición y no por nombre: la conexión puede ser una `sqlite3` pelada (sin `row_factory`).
    _despues(conn, {c: fila[i] for i, c in enumerate(COLUMNAS)})
