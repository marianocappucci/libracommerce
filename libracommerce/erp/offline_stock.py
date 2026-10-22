"""El stock de la sucursal de un nodo offline de LibraEdge: el encolado, la guarda del central, la carga inicial y los aplicadores (ADR-071).

Fase 2 de LibraEdge. Un nodo mueve el stock de su sucursal **sin ver al central** (ajustes, mermas, entradas y salidas a mano,
transferencias, recepciones de compra) y la decisión del humano (2026-10-21) es que, desde la carga inicial, **el stock de una
sucursal con nodo lo mueve sólo el nodo**. Este módulo es lo que hace `erp.offline` con las ventas y `erp.offline_caja` con la
caja, más lo que el stock tiene de propio: el central no sólo aplica lo que sube, también **se niega** a moverlo él.

## Las dos operaciones

Ambas con `schema_version = 1` (el mismo `SCHEMA_VERSION` de las ventas) y un `payload` con `"version": 1`.

| `operation_type` | `aggregate_type` / `aggregate_id` | Lo encola | El central |
|---|---|---|---|
| `stock.movimiento` | `stock_movimiento` / `<node_id>:stock_movimiento:<id>` | el gancho de movimientos (`movimientos_con_outbox`), una por fila del ledger | inserta la fila tal cual, con su `origen`/`origen_id` |
| `compra.recibida` | `compra_recepcion` / `<node_id>:compra_recepcion:<id de la recepción>` | `recepcion_con_outbox`, el `al_recibir` de «Recibir mercadería» y «Confirmar recepción» | `recibir_mercaderia` con la clave de la operación |

Payload de `stock.movimiento` (`ids` del **nodo**, que espeja los productos, las variantes y los depósitos del central con la
misma clave; cantidades y costos como texto):

```json
{"version": 1, "movimiento_id": 41, "producto_id": 7, "variante_id": null, "deposito_id": 3,
 "movement_type": "adjustment", "reason_code": "merma", "cantidad": "-2.0", "fecha": "2026-10-21T00:00:00",
 "lote": "L1", "vence": "2026-12-31", "costo": "60.0", "nota": "se cayó", "usuario_id": 4,
 "ref_tipo": "transfer", "ref_id": 40}
```

`ref_tipo`/`ref_id` son el `source_type`/`source_id` de la fila: sólo importan en una **transferencia** (la salida lleva
`ref_tipo = "transfer"` y `ref_id = null`; la entrada, `ref_tipo = "transfer"` y `ref_id` = el id de su salida en el nodo). Cualquier otro
`ref_tipo` el central lo ignora (no hay a qué atar un id de otra base).

Payload de `compra.recibida` (`ids` del nodo = los del central: el proveedor y la orden se crean en la web y bajan por el espejo):

```json
{"version": 1, "recepcion_id": 12, "proveedor_id": 5, "orden_id": 9, "remito": "R-0001-00004567", "deposito_id": 3,
 "fecha": "2026-10-21T13:05:00+00:00",
 "lineas": [{"producto_id": 7, "cantidad": "24", "costo": "250", "lote": "L9", "vence": "2027-03-01T00:00:00"}]}
```

## Qué sube y qué no (🔑 sin duplicados)

El nodo escribe **todo** movimiento en el ledger, pero no todo sube como `stock.movimiento`. `movimientos_con_outbox` deja afuera:

- **las filas de una venta** (`source_type` `venta`, `sale` o `sale_return`: la salida, la anulación y la devolución): suben con
  su operación (`venta.registrada` lleva la salida; `venta.anulada` y `venta.devolucion` las reproduce el central con
  `anular_venta` y `devolver_items`). Subirlas también las duplicaría;
- **las filas de una recepción de compra** (`source_type = "purchase_receipt"`): suben juntas como `compra.recibida`;
- **la carga inicial** (`reason_code = "carga_inicial"`): viene del central;
- **lo que escribe un aplicador** (`aplicando_del_nodo`): no es una decisión del nodo.

⚠ Un producto que monta Compras en un nodo **tiene que** pasar `OpcionesCompras(al_recibir=recepcion_con_outbox(OUTBOX))`: sin eso las
filas de la recepción no suben ni como movimiento ni como compra, y el stock del central no se entera.

## En la misma transacción

Los dos escritores del ledger (`erp.stock.add_movimiento_stock` y `db.repository.append_stock_movement`) llaman a los ganchos de
`db.ganchos_de_movimientos` con la conexión del movimiento, así que el encolado usa `next_sequence(commit=False)` y
`enqueue_operation(commit=False)`: la fila del ledger y su operación viven o mueren juntas.

## La guarda del central: 409 a todo movimiento sobre un depósito de una sucursal con nodo

En el central, antes de escribir, los mismos dos escritores le preguntan a `guarda_del_central`: si `locations.branch_id` de ese depósito
tiene un nodo **activo** en `node_identity` (comparando `node_identity.branch_id`, texto, con el id de la sucursal), levanta
`DepositoDeSucursalConNodo`. Cubre las ventas desde la web, anular desde la web una venta del nodo, recibir mercadería desde la web, los
ajustes, las mermas y las transferencias: todo lo que mueve stock en esos depósitos. Una base sin la tabla `node_identity` no tiene nodos:
no hay guarda.

🔴 **Es una `Exception`, no un `ValueError`, a propósito**: varios routers convierten `ValueError` en 422 («datos inválidos»), y esto no es un
dato inválido sino «ese depósito lo mueve otro equipo». La app del producto la convierte en **409** con un manejador, una vez:

```python
from libracommerce.erp import offline_stock
offline_stock.instalar_manejador_409(app)       # FastAPI: {"detail": "...", "codigo": "deposito_de_sucursal_con_nodo", ...}
```

o, a mano, `@app.exception_handler(offline_stock.DepositoDeSucursalConNodo)` y un `JSONResponse(409, ...)`.

**Los aplicadores no la disparan.** Lo que sube de un nodo (`venta.registrada` descuenta stock en los depósitos de ese nodo, `compra.recibida`
llama a `recibir_mercaderia`) es el nodo moviendo lo suyo. `aplicadores()` envuelve cada aplicador con `aplicando_del_nodo` (una
`contextvars.ContextVar`), que la guarda y el encolado leen. Quien llame a un `aplicar_*` suelto, sin pasar por `aplicadores()`/`registrar_en`,
tiene que envolverlo con `aplicador_del_nodo`.

Ni la guarda ni el encolado corren a la vez: `outbox.nodo()` distingue (hay nodo: encola y no guarda; no hay: guarda y no encola), y se lee en
cada movimiento, así que se arma igual en las dos puntas.

## La carga inicial

Al vincular, el nodo trae las existencias de **su** sucursal del central; desde ahí manda el nodo.

- En el central, `carga_inicial_del_central(conn, node_id, branch_id)` devuelve `{version, depositos, saldos, corte}`: los depósitos de la
  sucursal y el saldo de cada `(producto, variante, depósito, lote, vencimiento)` con stock (`erp.lotes.saldos_por_bucket`), JSON puro:

  ```json
  {"version": 1, "sucursal_id": 2, "depositos": [{"id": 3, "nombre": "Salón", "activo": true}],
   "saldos": [{"item_id": 7, "variant_id": null, "location_id": 3, "lote": null, "vence": null, "cantidad": "8"}],
   "corte": {"movimiento_id": 1250, "a": "2026-10-21T16:00:00+00:00"}}
  ```

  `sucursal_id` (aditivo desde v0.72.0, la `version` sigue en 1) es la sucursal del nodo según `node_identity`: el nodo la guarda (ver «El depósito
  por defecto de un nodo»). Un central anterior no la manda y un nodo anterior no la lee: no hay nada que romper.

- En el nodo, `aplicar_carga_inicial(conn, traer) -> dict` es lo que LibraEdge llama
  (`LIBRAEDGE_CARGA_INICIAL=libracommerce.erp.offline_stock:aplicar_carga_inicial`). `conn` es la conexión del nodo (ya abierta, **sin
  commitear**: es de quien llama) y `traer()` devuelve el dict de arriba (el HTTP es de LibraEdge). Escribe, por cada bucket, **un `ajuste`
  con `reason_code = "carga_inicial"` y delta = saldo del central − saldo local** (así una venta del nodo que ya subió no se cuenta dos veces,
  y volver a correrla da delta 0: idempotente). Devuelve siempre un resumen; el campo que decide es `hecha`:

  | `hecha` | Cuándo | Qué hace LibraEdge |
  |---|---|---|
  | `True` | se escribió (o no hacía falta: `ajustes` puede ser 0) | commitea las escrituras junto con la marca de «ya hecha» |
  | `False` | `motivo` dice por qué: `operaciones_pendientes` (hay operaciones sin confirmar), `stock_en_revision` (una de stock en `manual_review`), `el_stock_cambio` (se movió stock mientras se traía la carga), `espejo_incompleto` (falta un producto, variante o depósito en el espejo) | no marca nada, hace rollback y reintenta en el ciclo siguiente |

  La carga espera a que el outbox esté limpio **porque** el delta supone que el central ya aplicó todo lo que el nodo tiene: una operación
  pendiente se contaría dos veces (la carga la descontaría y después subiría). Para no perderse un movimiento que entra mientras se trae
  la carga, toma el saldo local y el último id del ledger **antes** de llamar a `traer()` y verifica **después** que ni el ledger ni el
  outbox cambiaron; si cambiaron, no escribe nada.

## El depósito por defecto de un nodo (v0.72.0)

🔴 Un movimiento sin `deposito_id` (el ajuste de ContaLibra, que no usa `OpcionesStock.por_deposito`; la producción; una venta sin depósito) cae en
`catalogo.get_default_deposito_id`, y el espejo del nodo trae el `locations.is_default` **del central**: el de «Casa central». Un nodo de otra
sucursal escribía en un depósito que no es suyo y el central lo mandaba a revisión manual (`DepositoFueraDeLaSucursal`). Ahora, **en un nodo, el
depósito por defecto es el activo de la sucursal del nodo** (`catalogo.deposito_activo_de_la_sucursal`: el `branches.default_location_id` si sigue
activo y, si no, el activo de menor id). Son tres piezas:

- **El nodo sabe su sucursal.** `carga_inicial_del_central` suma `sucursal_id` al payload y `aplicar_carga_inicial` lo guarda, **en la misma transacción
  que los ajustes**, en `nodo_local(clave, valor)` (revisión `0009_sucursal_del_nodo`; `clave = "sucursal_id"`). Es una tabla chica y propia de
  LibraCommerce: no toca las tablas de LibraEdge (`node_identity` es del central; el nodo no la tiene). `sucursal_guardada_del_nodo(conn)` la lee
  (`None` si no hay tabla o no hay valor) y `guardar_sucursal_del_nodo(conn, id)` la escribe.
- **El resolvedor.** `configurar(OUTBOX)` registra `deposito_por_defecto_del_nodo(OUTBOX)` en `db.ganchos_de_movimientos.configurar_deposito_por_defecto`
  (global, como los otros dos ganchos). Contesta sólo si `outbox.nodo()` no es `None` y el nodo tiene su sucursal guardada; en cualquier otro caso
  contesta `None` y `get_default_deposito_id` hace lo de siempre. En el central y fuera de un nodo no cambia nada.
- **Nodos ya instalados.** La revisión `0009` **deduce** la sucursal de las filas `reason_code = 'carga_inicial'` que ya tienen (esas filas sólo las
  escribe un nodo y sólo en depósitos de su sucursal): si todas son de una sola sucursal, la guarda. Un nodo cuya sucursal no tenía stock al vincular
  no tiene esas filas: ahí se actualiza el central **primero** y se corre en el nodo `libraedge-nodo carga-inicial --forzar`, que borra la marca y
  vuelve a llamar a `aplicar_carga_inicial` (delta 0 si el nodo está al día; espera si hay operaciones sin confirmar) y deja guardada la sucursal.

Quedan **sin cambio** `get_default_sucursal_id` (el `is_default` de `branches`: sólo lo usa la alta de depósitos de la web y `adoptar_sucursal_unica`,
que no corren en un nodo) y todo lo que ya pasa `deposito_id` explícito.

## Los depósitos sin sucursal

Un depósito sin `branch_id` no es de ningún nodo: la guarda no lo ve y la carga inicial no lo trae. Por eso se **exige asignarlos antes
de vincular**: `depositos_sin_sucursal(conn) -> list[str]` devuelve un problema legible por depósito y es directamente el
`Aprovisionamiento(validar_registro=offline_stock.depositos_sin_sucursal)` de libraedge; `listar_depositos_sin_sucursal(conn)` da los datos
(`[{"id", "nombre", "activo"}]`); `exigir_depositos_con_sucursal(conn, adoptar_si_hay_una=True)` levanta `DepositosSinSucursal` (con la
lista) o, con una sola sucursal, las adopta con `catalogo.adoptar_sucursal_unica`.

## Qué se traduce y qué se rechaza

- **Ids**: el producto, la variante y el depósito son los mismos en las dos puntas (se espejan). El nodo (`node_identity.branch_id`) fija
  cuál es su sucursal: un movimiento sobre un depósito de otra sucursal, o sin sucursal, es `DepositoFueraDeLaSucursal`.
- **La entrada de una transferencia** apunta a su salida: el central busca la salida por `(origen, origen_id)` y escribe la entrada con
  **su** id (`SalidaNoSincronizada` si la salida no subió). En el nodo, una transferencia entre sucursales ni se escribe
  (`TransferenciaEntreSucursales`, antes de encolar nada).
- **Todo lo que falta o choca es un `ValueError`** (revisión manual en el central, `manual_review` en el nodo y **la cola no se traba**,
  ADR-020 de libraedge): una excepción que no lo sea es un fallo de transporte y corta la tanda. Por eso las claves foráneas se verifican
  a mano antes de escribir (un `IntegrityError` lo lee el receptor como «duplicado» y la operación se perdería), y por eso la falta de la
  revisión `0008` es `StockSinRevision` y no un `RuntimeError`.
- **`compra.recibida`**: `OrdenNoEncontrada`, `EstadoInvalido` (la orden se canceló en la web mientras tanto) y `SinRevision` son
  `CompraRechazadaPorElCentral`.

## Idempotencia en el central

`SyncReceiver` ya descarta un `operation_id` repetido. Además `stock.movimiento` lo es por `(origen, origen_id)` (columnas e índice único
parcial de la revisión `0008`; `origen` es el `node_id`, como en la caja) y `compra.recibida` por la `clave_operacion` que sale del
`operation_id` (`offline.clave_de_operacion`): `recibir_mercaderia` contesta `repetida` sin escribir.

## Cómo lo cablea un producto

```python
OUTBOX = Outbox(nodo=nodo_local, en_linea=en_linea, caja_con_turno=True)            # el mismo en el nodo y en el central
offline_stock.configurar(OUTBOX)                                                   # una vez, al armar la app (las dos puntas)
offline_stock.instalar_manejador_409(app)                                          # el central: DepositoDeSucursalConNodo -> 409
build_compras_router(..., opciones=OpcionesCompras(al_recibir=offline_stock.recepcion_con_outbox(OUTBOX)))
```

En el central no hay nada más que hacer: `registrar_en(Despachador(), GANCHOS)` registra los trece tipos.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import logging
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .. import hora
from ..db import ganchos_de_movimientos
from . import lotes
from .offline import SCHEMA_VERSION, NodoLocal, Outbox, _texto, _tiene_tabla, _verificar, clave_de_operacion
from .offline_caja import _encolar

logger = logging.getLogger(__name__)

TIPO_STOCK_MOVIMIENTO = "stock.movimiento"
TIPO_COMPRA_RECIBIDA = "compra.recibida"
TIPOS_DE_STOCK = (TIPO_STOCK_MOVIMIENTO, TIPO_COMPRA_RECIBIDA)

#: Los `aggregate_type` de las operaciones (el `aggregate_id` es `<node_id>:<tipo>:<id local>`).
AGREGADO_MOVIMIENTO = "stock_movimiento"
AGREGADO_RECEPCION = "compra_recepcion"

#: La versión de la forma de los payloads de las dos operaciones. Si cambia, sube: el central rechaza lo que no entiende.
VERSION = 1
#: La versión de la forma de lo que devuelve `carga_inicial_del_central`.
VERSION_CARGA_INICIAL = 1

#: El `reason_code` de los ajustes de la carga inicial: no suben (vienen del central).
MOTIVO_CARGA_INICIAL = "carga_inicial"

#: La tabla de la revisión `0009_sucursal_del_nodo` donde el nodo guarda lo suyo (`clave`, `valor`) y la clave de su sucursal.
TABLA_NODO_LOCAL = "nodo_local"
CLAVE_SUCURSAL = "sucursal_id"

#: Los `source_type` de las filas que viajan con una venta y por eso no suben como `stock.movimiento`.
_ORIGENES_DE_VENTA = ("venta", "sale", "sale_return")
_ORIGEN_DE_RECEPCION = "purchase_receipt"
_ORIGEN_DE_TRANSFERENCIA = "transfer"

#: Los `movement_type` que entiende el ledger (los de `erp.stock._TIPO_A_MOVEMENT_TYPE` más `purchase`, que escribe la recepción).
_MOVEMENT_TYPES = ("sale", "return", "adjustment", "waste", "transfer_out", "transfer_in", "purchase")


class StockDelNodoError(ValueError):
    """Una operación de stock del nodo que el central no puede aplicar (o un movimiento que el nodo no debe escribir). Es un
    `ValueError` a propósito: el receptor de LibraEdge la manda a revisión manual y el nodo no traba la cola detrás de ella
    (ADR-020 de libraedge)."""


class StockSinRevision(StockDelNodoError):
    """A la base del central le falta la revisión `0008_origen_del_stock` (las columnas `stock_movements.origen` y `origen_id`)."""


class NodoSinRevision(StockDelNodoError):
    """A la base del nodo le falta la revisión `0009_sucursal_del_nodo` (la tabla `nodo_local`): no puede guardar su sucursal."""


class NodoDesconocido(StockDelNodoError):
    """El `node_id` de la operación no está en `node_identity` del central: no se sabe de qué sucursal es."""


class DepositoFueraDeLaSucursal(StockDelNodoError):
    """El depósito del movimiento no existe en el central, no tiene sucursal o es de otra sucursal que la del nodo."""


class SalidaNoSincronizada(StockDelNodoError):
    """La entrada de una transferencia del nodo cuya salida no está en el central (su `stock.movimiento` no se aplicó)."""


class MovimientoRechazadoPorElCentral(StockDelNodoError):
    """El central no acepta el movimiento: producto o variante que no existen, tipo desconocido, cantidad inválida."""


class CompraRechazadaPorElCentral(StockDelNodoError):
    """El central no acepta la recepción de compra: la orden no existe o se canceló, la base no tiene la revisión `0006`."""


class TransferenciaEntreSucursales(StockDelNodoError):
    """Una transferencia del nodo entre depósitos de sucursales distintas. Las transferencias del nodo son sólo dentro de su sucursal."""


class DepositosSinSucursal(StockDelNodoError):
    """Hay depósitos sin sucursal: hay que asignarlos antes de vincular un nodo. `depositos` es `listar_depositos_sin_sucursal(conn)`."""

    def __init__(self, depositos: list[dict]):
        self.depositos = depositos
        nombres = ", ".join(f"{d['nombre']!r} (id {d['id']})" for d in depositos)
        super().__init__(f"Hay depósitos sin sucursal: {nombres}. Asignalos a una sucursal antes de vincular un nodo.")


class DepositoDeSucursalConNodo(Exception):
    """El depósito pertenece a una sucursal que ya trabaja con un nodo offline: su stock lo mueve sólo ese equipo.

    🔴 **No es un `ValueError`**: varios routers pasan los `ValueError` a 422 y esto no es un dato inválido. El producto la convierte en
    409 (`instalar_manejador_409`)."""

    def __init__(self, deposito_id: int, node_id: str, sucursal_id: Any = None):
        self.deposito_id = deposito_id
        self.node_id = node_id
        self.sucursal_id = sucursal_id
        super().__init__(
            f"El depósito {deposito_id} es de una sucursal que trabaja con un equipo sin conexión (nodo {node_id!r}): su stock lo "
            "mueve ese equipo. Hacé el movimiento desde ahí."
        )


# ── El nodo que está aplicando: un `ContextVar` ─────────────────────────────

#: Prendido mientras corre un aplicador del central: lo que se escribe es el nodo moviendo lo suyo, no una decisión del central ni del nodo.
aplicando_del_nodo: contextvars.ContextVar[bool] = contextvars.ContextVar("aplicando_del_nodo", default=False)


@contextlib.contextmanager
def aplicando() -> Iterator[None]:
    """Prende `aplicando_del_nodo` mientras dura el bloque (y lo deja como estaba, también ante una excepción)."""
    token = aplicando_del_nodo.set(True)
    try:
        yield
    finally:
        aplicando_del_nodo.reset(token)


def aplicador_del_nodo(aplicador: Callable[[Any, Any], None]) -> Callable[[Any, Any], None]:
    """El aplicador `(conn, operacion)` corriendo con `aplicando_del_nodo` prendido. `offline.aplicadores()` envuelve todos los suyos así."""

    @functools.wraps(aplicador)
    def envuelto(conn, operacion) -> None:
        with aplicando():
            aplicador(conn, operacion)

    return envuelto


# ── En el nodo: el encolado ─────────────────────────────────────────────────


def sube_como_movimiento(movimiento: Mapping[str, Any]) -> bool:
    """Si una fila del ledger sube como `stock.movimiento`. No sube la que viaja con otra cosa (una venta, una recepción de compra) ni la
    carga inicial; ver el docstring del módulo."""
    if movimiento["reason_code"] == MOTIVO_CARGA_INICIAL:
        return False
    origen = movimiento["source_type"]
    return origen not in _ORIGENES_DE_VENTA and origen != _ORIGEN_DE_RECEPCION


def _texto_o_none(valor) -> str | None:
    return None if valor is None else str(valor)


def payload_stock_movimiento(movimiento: Mapping[str, Any]) -> dict:
    """El payload de `stock.movimiento` para una fila del ledger (la que ve `configurar_al_mover`). Puro: sin base."""
    return {
        "version": VERSION, "movimiento_id": movimiento["id"], "producto_id": movimiento["item_id"],
        "variante_id": movimiento["variant_id"], "deposito_id": movimiento["location_id"],
        "movement_type": movimiento["movement_type"], "reason_code": movimiento["reason_code"],
        "cantidad": _texto(movimiento["quantity_delta"]), "fecha": _texto_o_none(movimiento["occurred_at"]),
        "lote": movimiento["lot_code"], "vence": _texto_o_none(movimiento["expires_at"]),
        "costo": _texto(movimiento["unit_cost"]), "nota": movimiento["note"] or "", "usuario_id": movimiento["created_by"],
        "ref_tipo": movimiento["source_type"], "ref_id": movimiento["source_id"],
    }


def encolar_stock_movimiento(conn, nodo: NodoLocal, movimiento: Mapping[str, Any]):
    """Encola `stock.movimiento` **dentro de la transacción de `conn`** (`commit=False` en las dos llamadas)."""
    return _encolar(conn, nodo, TIPO_STOCK_MOVIMIENTO, AGREGADO_MOVIMIENTO, movimiento["id"],
                    payload_stock_movimiento(movimiento))


def _sucursal_de_deposito(conn, deposito_id: int):
    fila = conn.execute("SELECT branch_id FROM locations WHERE id = ?", (deposito_id,)).fetchone()
    return fila[0] if fila is not None else None


def _exigir_transferencia_en_la_sucursal(conn, movimiento: Mapping[str, Any]) -> None:
    """Una entrada de transferencia tiene que ser en la misma sucursal que su salida: el nodo mueve sólo la suya, y si la entrada se
    rechazara en el central la mercadería quedaría descontada en el origen y sin entrar. Levanta antes de encolar nada: el movimiento entero
    se revierte."""
    if movimiento["movement_type"] != "transfer_in" or movimiento["source_type"] != _ORIGEN_DE_TRANSFERENCIA:
        return
    if movimiento["source_id"] is None:
        return
    fila = conn.execute("SELECT location_id FROM stock_movements WHERE id = ?", (movimiento["source_id"],)).fetchone()
    if fila is None:
        return
    origen, destino = _sucursal_de_deposito(conn, fila[0]), _sucursal_de_deposito(conn, movimiento["location_id"])
    if origen != destino:
        raise TransferenciaEntreSucursales(
            f"El depósito {fila[0]} (sucursal {origen}) y el {movimiento['location_id']} (sucursal {destino}) son de sucursales "
            "distintas: un equipo sin conexión transfiere sólo entre los depósitos de su sucursal."
        )


def movimientos_con_outbox(outbox: Outbox) -> Callable[[Any, dict], None]:
    """El gancho `(conn, movimiento)` para `erp.stock.configurar_al_mover`: en un nodo encola `stock.movimiento` por cada fila que sube
    (`sube_como_movimiento`), dentro de la transacción del movimiento. Fuera de un nodo (`outbox.nodo()` es `None`), o mientras corre un
    aplicador, no hace nada: se puede armar siempre, en las dos puntas."""

    def al_mover(conn, movimiento: dict) -> None:
        nodo = outbox.nodo()
        if nodo is None or aplicando_del_nodo.get() or not sube_como_movimiento(movimiento):
            return
        _exigir_transferencia_en_la_sucursal(conn, movimiento)
        encolar_stock_movimiento(conn, nodo, movimiento)

    return al_mover


def payload_compra_recibida(recepcion: Mapping[str, Any], deposito_id: int) -> dict:
    """El payload de `compra.recibida` para una recepción confirmada (el `_recepcion_dict` de `erp.compras`). Puro: sin base."""
    return {
        "version": VERSION, "recepcion_id": recepcion["id"], "proveedor_id": recepcion["supplier_party_id"],
        "orden_id": recepcion["purchase_order_id"], "remito": recepcion["document_reference"], "deposito_id": deposito_id,
        "fecha": recepcion["received_at"],
        "lineas": [
            {"producto_id": i["item_id"], "cantidad": str(i["quantity"]), "costo": str(i["unit_cost"]),
             "lote": i["lot_code"], "vence": i["expires_at"]}
            for i in recepcion["items"]
        ],
    }


def encolar_compra_recibida(conn, nodo: NodoLocal, recepcion: Mapping[str, Any], deposito_id: int):
    """Encola `compra.recibida` **dentro de la transacción de `conn`**."""
    return _encolar(conn, nodo, TIPO_COMPRA_RECIBIDA, AGREGADO_RECEPCION, recepcion["id"],
                    payload_compra_recibida(recepcion, deposito_id))


def recepcion_con_outbox(outbox: Outbox, al_recibir: Callable[[Any, Mapping[str, Any], int], None] | None = None
                         ) -> Callable[[Any, Mapping[str, Any], int], None]:
    """El `al_recibir` del producto (`OpcionesCompras.al_recibir`, `erp.compras.AlRecibir`) con el outbox sumado, **sin pisarlo**: corre primero
    el del producto y después encola `compra.recibida`. Fuera de un nodo no encola nada."""

    def compuesto(conn, recepcion: Mapping[str, Any], deposito_id: int) -> None:
        if al_recibir is not None:
            al_recibir(conn, recepcion, deposito_id)
        nodo = outbox.nodo()
        if nodo is not None and not aplicando_del_nodo.get():
            encolar_compra_recibida(conn, nodo, recepcion, deposito_id)

    return compuesto


# ── En el nodo: su sucursal y su depósito por defecto ───────────────────────


def tiene_nodo_local(conn) -> bool:
    """Si la base tiene la revisión `0009_sucursal_del_nodo` (la tabla `nodo_local`). Por metadatos, como `tiene_origen`."""
    return _tiene_tabla(conn, TABLA_NODO_LOCAL)


def sucursal_guardada_del_nodo(conn) -> int | None:
    """El id de la sucursal que el nodo guardó en la carga inicial, o `None` (sin la revisión `0009`, sin carga todavía, o un valor que no es un id)."""
    if not tiene_nodo_local(conn):
        return None
    fila = conn.execute(f"SELECT valor FROM {TABLA_NODO_LOCAL} WHERE clave = ?", (CLAVE_SUCURSAL,)).fetchone()
    if fila is None:
        return None
    try:
        return int(str(fila[0]))
    except ValueError:
        logger.warning("nodo_local.%s no es un id de sucursal: %r", CLAVE_SUCURSAL, fila[0])
        return None


def _exigir_nodo_local(conn) -> None:
    if not tiene_nodo_local(conn):
        raise NodoSinRevision(
            "a la base del nodo le falta la revisión 0009_sucursal_del_nodo (la tabla nodo_local): correr `libracommerce-migrar upgrade "
            "--prefijo <producto>` y reintentar la carga inicial"
        )


def guardar_sucursal_del_nodo(conn, sucursal_id) -> None:
    """Deja `sucursal_id` como la sucursal del nodo (la pisa si ya había otra). No commitea: es de quien llama. `NodoSinRevision` si falta la `0009`."""
    sucursal = _id_de_sucursal(sucursal_id)
    _exigir_nodo_local(conn)
    conn.execute(
        f"INSERT INTO {TABLA_NODO_LOCAL} (clave, valor) VALUES (?, ?) ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor",
        (CLAVE_SUCURSAL, str(sucursal)),
    )


def deposito_por_defecto_del_nodo(outbox: Outbox) -> Callable[[Any], int | None]:
    """El resolvedor `(conn) -> int | None` para `db.ganchos_de_movimientos.configurar_deposito_por_defecto`: en un nodo (`outbox.nodo()` no es
    `None`) que ya guardó su sucursal, el depósito activo de esa sucursal; en cualquier otro caso `None` y `get_default_deposito_id` sigue como
    siempre (el central, una instalación común, un nodo que todavía no hizo la carga o sin la `0009`, o cuya sucursal no tiene depósito activo)."""

    def resolver(conn) -> int | None:
        if outbox.nodo() is None:
            return None
        sucursal = sucursal_guardada_del_nodo(conn)
        if sucursal is None:
            return None
        from .catalogo import deposito_activo_de_la_sucursal

        return deposito_activo_de_la_sucursal(conn, sucursal)

    return resolver


# ── En el central: la guarda ────────────────────────────────────────────────


def nodo_activo_del_deposito(conn, deposito_id: int) -> tuple[str, Any] | None:
    """`(node_id, branch_id)` del nodo activo de la sucursal de ese depósito, o `None` (sin sucursal, sin nodo, o una base sin
    `node_identity`: la tabla es de LibraEdge y no existe donde no hay nodos)."""
    if not _tiene_tabla(conn, "node_identity"):
        return None
    fila = conn.execute(
        "SELECT n.node_id, l.branch_id FROM locations l JOIN node_identity n "
        "ON n.branch_id = CAST(l.branch_id AS TEXT) AND n.active = 1 WHERE l.id = ? ORDER BY n.node_id LIMIT 1",
        (deposito_id,),
    ).fetchone()
    return (fila[0], fila[1]) if fila is not None else None


def guarda_del_central(outbox: Outbox) -> Callable[[Any, int], None]:
    """El gancho `(conn, deposito_id)` para `erp.stock.configurar_antes_de_mover`: en el central, `DepositoDeSucursalConNodo` si el depósito es de
    una sucursal con nodo activo. En un nodo (`outbox.nodo()` no es `None`: mueve lo suyo) o mientras corre un aplicador, no hace nada."""

    def antes_de_mover(conn, deposito_id: int) -> None:
        if outbox.nodo() is not None or aplicando_del_nodo.get():
            return
        nodo = nodo_activo_del_deposito(conn, deposito_id)
        if nodo is not None:
            raise DepositoDeSucursalConNodo(deposito_id, nodo[0], nodo[1])

    return antes_de_mover


def configurar(outbox: Outbox) -> None:
    """Arma las dos mitades en el proceso, **una vez**, al armar la app y en las dos puntas (como `altas_en_nodo.configurar`): el encolado
    de `stock.movimiento` (sólo hace algo en un nodo), la guarda (sólo hace algo en el central) y el depósito por defecto del nodo (sólo hace algo en
    un nodo que ya sabe su sucursal). Es global y reemplaza a la anterior."""
    ganchos_de_movimientos.configurar_al_mover(movimientos_con_outbox(outbox))
    ganchos_de_movimientos.configurar_antes_de_mover(guarda_del_central(outbox))
    ganchos_de_movimientos.configurar_deposito_por_defecto(deposito_por_defecto_del_nodo(outbox))


def desconfigurar() -> None:
    """Saca los tres ganchos (para los tests, que comparten el proceso)."""
    ganchos_de_movimientos.configurar_al_mover(None)
    ganchos_de_movimientos.configurar_antes_de_mover(None)
    ganchos_de_movimientos.configurar_deposito_por_defecto(None)


def instalar_manejador_409(app) -> None:
    """Registra en una app FastAPI el manejador que convierte `DepositoDeSucursalConNodo` en **409** con
    `{"detail": ..., "codigo": "deposito_de_sucursal_con_nodo", "deposito_id": ..., "node_id": ...}`."""
    from fastapi.responses import JSONResponse

    @app.exception_handler(DepositoDeSucursalConNodo)
    async def _deposito_de_sucursal_con_nodo(_request, exc: DepositoDeSucursalConNodo):
        return JSONResponse(status_code=409, content={
            "detail": str(exc), "codigo": "deposito_de_sucursal_con_nodo", "deposito_id": exc.deposito_id,
            "node_id": exc.node_id,
        })


# ── Los depósitos sin sucursal ──────────────────────────────────────────────


def listar_depositos_sin_sucursal(conn, *, solo_activos: bool = True) -> list[dict]:
    """Los depósitos sin `branch_id` (`[{"id", "nombre", "activo"}]`): ninguno es de un nodo, así que hay que asignarlos antes de vincular uno."""
    filas = conn.execute(
        "SELECT id, name, active FROM locations WHERE branch_id IS NULL" + (" AND active = 1" if solo_activos else "") + " ORDER BY id"
    ).fetchall()
    return [{"id": f[0], "nombre": f[1], "activo": bool(f[2])} for f in filas]


def depositos_sin_sucursal(conn) -> list[str]:
    """Un problema legible por depósito activo sin sucursal (`"El depósito 'X' no tiene sucursal"`), o `[]`. Es la firma de
    `Aprovisionamiento(validar_registro=...)` de libraedge (`f(conn) -> list[str]`): el registro de un nodo se niega mientras devuelva algo."""
    return [f"El depósito {d['nombre']!r} no tiene sucursal" for d in listar_depositos_sin_sucursal(conn)]


def exigir_depositos_con_sucursal(conn, *, adoptar_si_hay_una: bool = False) -> None:
    """`DepositosSinSucursal` si queda algún depósito activo sin sucursal; es lo que el registro de un nodo llama antes de vincular. Con
    `adoptar_si_hay_una` y **una sola sucursal** (`branches`), antes las asigna con `catalogo.adoptar_sucursal_unica` (que no commitea: es de quien
    llama)."""
    sin = listar_depositos_sin_sucursal(conn)
    if sin and adoptar_si_hay_una and conn.execute("SELECT COUNT(*) FROM branches").fetchone()[0] == 1:
        from .catalogo import adoptar_sucursal_unica

        adoptar_sucursal_unica(conn)
        sin = listar_depositos_sin_sucursal(conn)
    if sin:
        raise DepositosSinSucursal(sin)


# ── En el central: lo que ya está ───────────────────────────────────────────


def tiene_origen(conn) -> bool:
    """Si la base tiene la revisión `0008_origen_del_stock` (las columnas `stock_movements.origen` y `origen_id`). Se sondea por metadatos
    (`PRAGMA table_info`) y no con un `SELECT`: en PostgreSQL uno fallido aborta la transacción."""
    return {"origen", "origen_id"} <= {f[1] for f in conn.execute("PRAGMA table_info(stock_movements)").fetchall()}


def movimiento_del_central(conn, node_id: str, movimiento_id_nodo: int | None) -> dict | None:
    """La fila del ledger del central que subió del nodo `node_id` con ese id local (`{"id", "location_id"}`), o `None` (sin id, todavía no
    subió, o la base no tiene la revisión `0008`)."""
    if movimiento_id_nodo is None or not tiene_origen(conn):
        return None
    fila = conn.execute(
        "SELECT id, location_id FROM stock_movements WHERE origen = ? AND origen_id = ?", (node_id, movimiento_id_nodo),
    ).fetchone()
    return {"id": fila[0], "location_id": fila[1]} if fila is not None else None


def sucursal_del_nodo(conn, node_id: str) -> str:
    """El `branch_id` (texto) del nodo en `node_identity`. `NodoDesconocido` si no está."""
    fila = conn.execute("SELECT branch_id FROM node_identity WHERE node_id = ?", (node_id,)).fetchone() \
        if _tiene_tabla(conn, "node_identity") else None
    if fila is None:
        raise NodoDesconocido(f"el nodo {node_id!r} no está en node_identity del central: no se sabe de qué sucursal es")
    return str(fila[0])


def _exigir_deposito_del_nodo(conn, node_id: str, deposito_id: int) -> None:
    sucursal = sucursal_del_nodo(conn, node_id)
    fila = conn.execute("SELECT branch_id FROM locations WHERE id = ?", (deposito_id,)).fetchone()
    if fila is None:
        raise DepositoFueraDeLaSucursal(f"el depósito {deposito_id} del nodo {node_id!r} no existe en el central")
    if fila[0] is None or str(fila[0]) != sucursal:
        raise DepositoFueraDeLaSucursal(
            f"el depósito {deposito_id} es de la sucursal {fila[0]!r} y el nodo {node_id!r} es de la {sucursal!r}: un nodo mueve "
            "sólo el stock de su sucursal"
        )


def _datos(operacion, tipo: str) -> Mapping[str, Any]:
    _verificar(operacion, tipo)
    datos = operacion.payload
    if datos.get("version") != VERSION:
        raise StockDelNodoError(f"versión {datos.get('version')!r} de los datos de {tipo} desconocida (se entiende {VERSION})")
    return datos


def _decimal(valor, que: str) -> Decimal:
    try:
        numero = Decimal(str(valor))
    except InvalidOperation as exc:
        raise MovimientoRechazadoPorElCentral(f"{que} no es un número: {valor!r}") from exc
    if not numero.is_finite():
        raise MovimientoRechazadoPorElCentral(f"{que} no es un número: {valor!r}")
    return numero


# ── Aplicadores del central ─────────────────────────────────────────────────


def aplicar_stock_movimiento(conn, operacion) -> None:
    """Inserta en el ledger del central la fila que escribió el nodo, **tal cual** (lote, vencimiento, costo), con su `origen`/`origen_id`.

    Idempotente por `(origen, origen_id)`: si la fila ya está, no hace nada. No pasa por `add_movimiento_stock` (ni por la guarda ni por el
    encolado): el depósito pudo darse de baja en el central y el hecho está consumado. Verifica antes de escribir —un `IntegrityError` lo lee el
    receptor como «duplicado»—: la revisión `0008` (`StockSinRevision`), el nodo y que el depósito sea de su sucursal, que el producto y la
    variante existan, el tipo y la cantidad. La entrada de una transferencia se ata a **su** salida del central (`SalidaNoSincronizada` si la
    salida no subió)."""
    datos = _datos(operacion, TIPO_STOCK_MOVIMIENTO)
    if not tiene_origen(conn):
        raise StockSinRevision(
            "Falta la revisión 0008_origen_del_stock del motor: corré `libracommerce-migrar upgrade` (--prefijo del producto) antes de "
            "recibir el stock de un nodo."
        )
    node_id, movimiento_id = operacion.node_id, datos["movimiento_id"]
    if movimiento_del_central(conn, node_id, movimiento_id) is not None:
        return
    _exigir_deposito_del_nodo(conn, node_id, datos["deposito_id"])
    if datos["movement_type"] not in _MOVEMENT_TYPES:
        raise MovimientoRechazadoPorElCentral(f"tipo de movimiento desconocido: {datos['movement_type']!r}")
    cantidad = _decimal(datos["cantidad"], "la cantidad")
    if cantidad == 0:
        raise MovimientoRechazadoPorElCentral("la cantidad de un movimiento no puede ser cero")
    if conn.execute("SELECT 1 FROM catalog_items WHERE id = ?", (datos["producto_id"],)).fetchone() is None:
        raise MovimientoRechazadoPorElCentral(f"el producto {datos['producto_id']} del nodo {node_id!r} no existe en el central")
    variante = datos.get("variante_id")
    if variante is not None and conn.execute("SELECT 1 FROM item_variants WHERE id = ?", (variante,)).fetchone() is None:
        raise MovimientoRechazadoPorElCentral(f"la variante {variante} del nodo {node_id!r} no existe en el central")
    source_type = source_id = None
    if datos.get("ref_tipo") == _ORIGEN_DE_TRANSFERENCIA:
        source_type = _ORIGEN_DE_TRANSFERENCIA
        if datos["movement_type"] == "transfer_in":
            salida = movimiento_del_central(conn, node_id, datos.get("ref_id"))
            if salida is None:
                raise SalidaNoSincronizada(
                    f"la salida {datos.get('ref_id')} de la transferencia del nodo {node_id!r} no está en el central: falta aplicar su "
                    f"{TIPO_STOCK_MOVIMIENTO}"
                )
            source_id = salida["id"]
    conn.execute(
        """INSERT INTO stock_movements
           (item_id, variant_id, location_id, movement_type, quantity_delta, occurred_at, source_type, source_id,
            unit_cost, lot_code, expires_at, note, created_by, reason_code, origen, origen_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (datos["producto_id"], variante, datos["deposito_id"], datos["movement_type"], datos["cantidad"], datos["fecha"],
         source_type, source_id, datos.get("costo"), datos.get("lote"), datos.get("vence"), datos.get("nota") or "",
         datos.get("usuario_id"), datos.get("reason_code"), node_id, movimiento_id),
    )


def aplicar_compra_recibida(conn, operacion) -> None:
    """Recibe en el central la mercadería que recibió el nodo: `recibir_mercaderia` con la `clave_operacion` de la operación (idempotente: un
    reintento contesta `repetida` sin escribir). El proveedor tiene que existir y el depósito ser de la sucursal del nodo; la orden que se
    canceló en la web, o que no existe, es `CompraRechazadaPorElCentral` (revisión manual). El stock entra, el costo se actualiza y la orden
    avanza, igual que si la hubieran recibido en el central. Corre con `aplicando_del_nodo` (lo pone `aplicadores()`), así que la guarda no
    la frena."""
    from .compras import EstadoInvalido, OrdenNoEncontrada, SinRevision, recibir_mercaderia

    datos = _datos(operacion, TIPO_COMPRA_RECIBIDA)
    _exigir_deposito_del_nodo(conn, operacion.node_id, datos["deposito_id"])
    if conn.execute("SELECT 1 FROM parties WHERE id = ?", (datos["proveedor_id"],)).fetchone() is None:
        raise CompraRechazadaPorElCentral(f"el proveedor {datos['proveedor_id']} del nodo {operacion.node_id!r} no existe en el central")
    items = [
        {"item_id": linea["producto_id"], "quantity": linea["cantidad"], "unit_cost": linea["costo"], "lot_code": linea.get("lote"),
         "expires_at": datetime.fromisoformat(linea["vence"]) if linea.get("vence") else None}
        for linea in datos["lineas"]
    ]
    try:
        recibir_mercaderia(
            conn, supplier_party_id=datos["proveedor_id"], deposito_id=datos["deposito_id"], items=items,
            clave_operacion=clave_de_operacion(operacion), occurred_at=datetime.fromisoformat(datos["fecha"]),
            purchase_order_id=datos.get("orden_id"), document_reference=datos.get("remito"),
        )
    except OrdenNoEncontrada as exc:
        raise CompraRechazadaPorElCentral(
            f"la orden de compra {datos.get('orden_id')} del nodo {operacion.node_id!r} no existe en el central") from exc
    except (EstadoInvalido, SinRevision) as exc:
        raise CompraRechazadaPorElCentral(str(exc) or type(exc).__name__) from exc


def aplicadores_de_stock() -> dict[str, Callable[[Any, Any], None]]:
    """`{operation_type: (conn, operacion) -> None}` de los dos tipos de stock. `erp.offline.aplicadores` los suma a los demás (y los envuelve con
    `aplicando_del_nodo`), así que `registrar_en` los registra todos en el mismo `Despachador`."""
    return {
        TIPO_STOCK_MOVIMIENTO: aplicar_stock_movimiento,
        TIPO_COMPRA_RECIBIDA: aplicar_compra_recibida,
    }


# ── La carga inicial ────────────────────────────────────────────────────────


def _id_de_sucursal(branch_id) -> int:
    try:
        return int(str(branch_id))
    except ValueError as exc:
        raise ValueError(f"la sucursal {branch_id!r} del nodo no es el id de una sucursal de este central") from exc


def _cantidad_en_texto(cantidad: Decimal) -> str:
    return format(cantidad.normalize(), "f")


def carga_inicial_del_central(conn, node_id: str, branch_id) -> dict:
    """Las existencias de la sucursal `branch_id` del nodo `node_id`, para que el nodo las traiga al vincularse. JSON puro:
    `{"version", "sucursal_id", "depositos": [{"id", "nombre", "activo"}], "saldos": [{"item_id", "variant_id", "location_id", "lote", "vence", "cantidad"}],
    "corte": {"movimiento_id", "a"}}`.

    Los saldos son los de `erp.lotes.saldos_por_bucket` sobre todos los depósitos de la sucursal (activos o no: las existencias de un depósito
    dado de baja siguen existiendo), con el lote y el vencimiento normalizados y sin los saldos en cero. `corte` dice hasta qué fila del ledger
    del central llegó la foto (`movimiento_id`, el mayor id de esos depósitos) y cuándo se sacó (`a`, UTC): es información, el nodo no la usa para
    decidir. `sucursal_id` es la sucursal del nodo (un entero): el nodo la guarda para saber cuál es su depósito por defecto. Es una lectura:
    no escribe ni commitea."""
    sucursal = _id_de_sucursal(branch_id)
    depositos = [{"id": f[0], "nombre": f[1], "activo": bool(f[2])} for f in conn.execute(
        "SELECT id, name, active FROM locations WHERE branch_id = ? ORDER BY id", (sucursal,)).fetchall()]
    ids = [d["id"] for d in depositos]
    saldos: list[dict] = []
    corte_id = None
    if ids:
        marcadores = ",".join("?" for _ in ids)
        por_bucket = lotes.saldos_por_bucket(conn, f"sm.location_id IN ({marcadores})", ids)
        for (item, deposito, variante, lote, vence), saldo in sorted(
                por_bucket.items(), key=lambda kv: tuple("" if x is None else str(x) for x in kv[0])):
            saldos.append({"item_id": item, "variant_id": variante, "location_id": deposito, "lote": lote, "vence": vence,
                           "cantidad": _cantidad_en_texto(saldo)})
        corte_id = conn.execute(f"SELECT MAX(id) FROM stock_movements WHERE location_id IN ({marcadores})", ids).fetchone()[0]
    logger.info("carga inicial del nodo %s (sucursal %s): %d depósitos, %d saldos", node_id, sucursal, len(depositos), len(saldos))
    return {"version": VERSION_CARGA_INICIAL, "sucursal_id": sucursal, "depositos": depositos, "saldos": saldos,
            "corte": {"movimiento_id": corte_id, "a": hora.ahora_utc().isoformat()}}


#: Los `operation_type` de stock: una de ésas en `manual_review` frena la carga inicial (el central no tiene lo que el nodo ya movió).
_TIPOS_QUE_FRENAN = TIPOS_DE_STOCK
_SIN_CONFIRMAR = ("pending", "sending", "retryable_error")


def _por_que_esperar(conn) -> str | None:
    """Por qué la carga inicial no puede correr ahora, o `None`: hay operaciones sin confirmar, o una de stock en revisión manual."""
    if conn.execute(
        f"SELECT 1 FROM sync_outbox WHERE status IN ({','.join('?' for _ in _SIN_CONFIRMAR)}) LIMIT 1", _SIN_CONFIRMAR
    ).fetchone() is not None:
        return "operaciones_pendientes"
    if conn.execute(
        f"SELECT 1 FROM sync_outbox WHERE status = 'manual_review' AND operation_type IN ({','.join('?' for _ in _TIPOS_QUE_FRENAN)}) LIMIT 1",
        _TIPOS_QUE_FRENAN,
    ).fetchone() is not None:
        return "stock_en_revision"
    return None


def _ultimo_movimiento(conn) -> int:
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM stock_movements").fetchone()[0]


def _existentes(conn, tabla: str, ids: set[int]) -> set[int]:
    """Cuáles de `ids` existen en `tabla` (por `id`), de a 500."""
    hallados: set[int] = set()
    lista = sorted(ids)
    for i in range(0, len(lista), 500):
        trozo = lista[i:i + 500]
        hallados.update(f[0] for f in conn.execute(
            f"SELECT id FROM {tabla} WHERE id IN ({','.join('?' for _ in trozo)})", trozo).fetchall())
    return hallados


def aplicar_carga_inicial(conn, traer: Callable[[], Mapping[str, Any]]) -> dict:
    """Trae las existencias de la sucursal del central y deja el ledger del nodo igual: por cada bucket `(producto, variante, depósito, lote,
    vencimiento)`, un `ajuste` con `reason_code = "carga_inicial"` y delta = saldo del central − saldo local. **Idempotente** (volver a correrla da
    delta 0) y **sin duplicar**: una venta del nodo que ya subió está en las dos puntas y no entra en el delta.

    Es la firma que LibraEdge llama: `(conn, traer) -> dict`. `conn` es la conexión del nodo y **no se commitea acá** (es de quien llama: que
    confirme sólo si `hecha`); `traer()` devuelve lo de `carga_inicial_del_central`. Devuelve `{"hecha": bool, "motivo": str | None, "ajustes": int,
    "buckets": int}`:

    - `hecha = False` y `motivo` (`operaciones_pendientes`, `stock_en_revision`, `el_stock_cambio`, `espejo_incompleto`): no escribió nada y se reintenta
      en el ciclo siguiente. Ver el docstring del módulo.
    - `hecha = True`: `ajustes` es cuántos ajustes escribió y `buckets` cuántos buckets del central miró (0 ajustes = ya estaba al día).

    Si la carga trae `sucursal_id` (un central desde v0.72.0), también la **guarda** en `nodo_local`, en la misma transacción que los ajustes, y es
    lo que hace que el depósito por defecto del nodo sea el de su sucursal (`deposito_por_defecto_del_nodo`). Sin la revisión `0009` levanta
    `NodoSinRevision` **antes de escribir** (el worker de LibraEdge lo muestra como error y reintenta); un central anterior no la manda y no se guarda nada.

    Orden: (1) outbox limpio; (2) saldo local y último id del ledger; (3) `traer()`; (4) que ni el ledger ni el outbox hayan cambiado
    desde (2); (5) que el espejo tenga los productos, variantes y depósitos; (6) los ajustes."""
    if not _tiene_tabla(conn, "sync_outbox"):
        raise ValueError("la carga inicial es de un nodo: esta base no tiene el outbox de LibraEdge")
    espera = _por_que_esperar(conn)
    if espera:
        logger.info("carga inicial: espera (%s)", espera)
        return {"hecha": False, "motivo": espera, "ajustes": 0, "buckets": 0}
    ultimo = _ultimo_movimiento(conn)
    locales = lotes.saldos_por_bucket(conn, "1 = 1", [])
    datos = traer()
    if datos.get("version") != VERSION_CARGA_INICIAL:
        raise ValueError(f"versión {datos.get('version')!r} de la carga inicial desconocida (se entiende {VERSION_CARGA_INICIAL})")
    sucursal = datos.get("sucursal_id")
    if sucursal is None:
        logger.warning("carga inicial: el central no informó la sucursal del nodo (es anterior a v0.72.0): el depósito por defecto sigue siendo el de la instancia")
    else:
        sucursal = _id_de_sucursal(sucursal)
        _exigir_nodo_local(conn)                           # antes de escribir nada
    if _ultimo_movimiento(conn) != ultimo or _por_que_esperar(conn):
        logger.info("carga inicial: el stock del nodo cambió mientras se traía; se reintenta")
        return {"hecha": False, "motivo": "el_stock_cambio", "ajustes": 0, "buckets": 0}
    depositos = {d["id"] for d in datos["depositos"]}
    centrales = {
        (s["item_id"], s["location_id"], s["variant_id"], s["lote"], s["vence"]): _decimal(s["cantidad"], "el saldo")
        for s in datos["saldos"] if s["location_id"] in depositos
    }
    deltas = []
    for clave in sorted(set(centrales) | {k for k in locales if k[1] in depositos}, key=lambda k: tuple("" if x is None else str(x) for x in k)):
        delta = lotes.saldo_limpio(centrales.get(clave, lotes.CERO) - locales.get(clave, lotes.CERO))
        if delta != 0:
            deltas.append((clave, delta))
    faltan = {
        "productos": sorted({k[0] for k, _ in deltas} - _existentes(conn, "catalog_items", {k[0] for k, _ in deltas})),
        "variantes": sorted({k[2] for k, _ in deltas if k[2] is not None}
                            - _existentes(conn, "item_variants", {k[2] for k, _ in deltas if k[2] is not None})),
        "depositos": sorted({k[1] for k, _ in deltas} - _existentes(conn, "locations", {k[1] for k, _ in deltas})),
    }
    if any(faltan.values()):
        logger.warning("carga inicial: el espejo del nodo todavía no tiene %s", faltan)
        return {"hecha": False, "motivo": "espejo_incompleto", "ajustes": 0, "buckets": len(centrales),
                "faltan": {k: v[:20] for k, v in faltan.items() if v}}
    ocurrido = hora.ahora_local().replace(microsecond=0).isoformat()
    for (item, deposito, variante, lote, vence), delta in deltas:
        conn.execute(
            """INSERT INTO stock_movements
               (item_id, variant_id, location_id, movement_type, quantity_delta, occurred_at, lot_code, expires_at, note, reason_code)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (item, variante, deposito, "adjustment", _cantidad_en_texto(delta), ocurrido, lote, vence, "Carga inicial desde el central",
             MOTIVO_CARGA_INICIAL),
        )
    if sucursal is not None:
        guardar_sucursal_del_nodo(conn, sucursal)
    logger.info("carga inicial: %d ajustes sobre %d buckets del central (sucursal del nodo: %s)", len(deltas), len(centrales), sucursal)
    return {"hecha": True, "motivo": None, "ajustes": len(deltas), "buckets": len(centrales)}
