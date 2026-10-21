"""Las ventas de la capa ERP en un nodo offline de LibraEdge: el traductor, el encolado y los aplicadores del central (ADR-067).

LibraEdge no sabe qué es una venta: mueve `OutboxOperation` genéricas del nodo al central. Este módulo es la traducción
de la capa ERP —la misma para RestoLibra, VentaLibra y ContaLibra, que venden por `erp.ventas`— a esas operaciones, y su
vuelta en el central. Lo de cada producto entra por costuras (`Outbox.extra_de_venta`, `al_materializar`), nunca por
`if producto`.

## Las cinco operaciones

Todas con `aggregate_type = "venta"` y `aggregate_id = "<node_id>:venta:<id local>"`: el `node_id` adentro hace que dos
nodos con el mismo id local no se pisen. `schema_version = 1`.

| `operation_type` | Lo encola | El central |
|---|---|---|
| `venta.registrada` | `al_registrar_venta`: toda venta al nacer, **en cualquier estado** | crea la venta, sus pagos, la caja de lo acreditado y el stock que descontó el nodo |
| `venta.pago_acreditado` | `al_acreditar_pago`: lo que `acreditar_pago_qr` acaba de acreditar | `acreditar_pago_qr` sobre la venta del central |
| `venta.anulada` | `al_anular_venta` | `anular_venta` sobre la venta del central |
| `venta.devolucion` | `al_devolver_items` | `devolver_items` sobre la venta del central, con una `clave_operacion` del nodo |
| `venta.facturada` | `al_facturar_con_outbox`: el `PuertoDeVentas.al_facturar` de libracore, cuando el nodo factura | escribe la factura con el mismo número y CAE y la vincula a la venta (ADR-068) |

🔑 **No hay `venta.cobrada`, y es a propósito (decisión 5 del humano).** Una venta que nace cobrada es una
`venta.registrada` con `estado = "cobrada"`; una que nace parcial sube igual, y la completa `venta.pago_acreditado`
cuando entra el resto. Si además se encolara al llegar a `cobrada` (`al_confirmar_venta`), la venta que nace cobrada
subiría dos veces y la segunda chocaría con la primera en el central. Un solo hecho por cosa que pasó: la venta nació,
entró un pago, se anuló, se devolvió.

## En la misma transacción

Los ganchos reciben la conexión de la venta, y el encolado usa `next_sequence(commit=False)` y
`enqueue_operation(commit=False)`: la venta y su operación viven o mueren juntas.

## Idempotencia en el central

`SyncReceiver` ya descarta un `operation_id` repetido. Además, cada aplicador es idempotente por sí mismo:

- `venta.registrada`, por el agregado: la venta del central queda con `source_type = "libraedge:<node_id>"` y
  `source_id = <id local>`; si ya está, no hace nada. Si el **número** ya lo tiene otra venta, es una **colisión de
  numeración** y levanta `ColisionDeNumero` (un `ValueError`: el receptor la rechaza y el nodo la deja en revisión
  manual). No se deja llegar al `INSERT`: el `UNIQUE` de `sales.number` sería un `IntegrityError`, que el receptor lee
  como «duplicado», y la venta se perdería pareciendo un reintento.
- `venta.pago_acreditado`, por la condición: `acreditar_pago_qr` sólo toca lo `pendiente`.
- `venta.anulada`: `anular_venta` devuelve `False` si ya estaba anulada.
- `venta.devolucion`: la `clave_operacion` sale del `operation_id`, y `devolver_items` contesta `repetida` sin escribir.
- `venta.facturada`: la factura, por su clave ante ARCA (`libracore.facturas_del_nodo.aplicar_factura_del_nodo`); el
  vínculo, porque si la venta ya tiene esa factura no hace nada. Si tiene **otra**, `VentaConOtraFactura`.

Una operación de una venta que el central no tiene (su `venta.registrada` quedó en revisión manual) levanta
`ValueError`: queda en revisión manual detrás de ella, en vez de aplicarse sobre nada.

## Numeración por nodo (decisión 4 del humano)

En el nodo, `con_outbox` antepone el prefijo de la sucursal al numerador del producto: `S02-POS-000123`,
`S02-V-00007`. Así una venta del nodo no choca con las del central ni con las de otra sucursal. Si igual choca (dos
nodos con el mismo prefijo, una PC reemplazada que arrancó la numeración de cero), lo atrapa `ColisionDeNumero`.

## Anular una venta facturada sin conexión: prohibido

El nodo factura cuando tiene internet (decisión 3); sin conexión no hay ARCA para la nota de crédito, y anular lo
facturado dejaría al central con una factura vigente de una venta anulada. `con_outbox` lo rechaza con
`hooks.RequiereConexion` si la venta tiene factura y `Outbox.en_linea()` dice que no. **Cómo se sabe:** por el estado
que escribe el proceso del nodo (`libraedge-nodo`) en su archivo de estado: `en_linea_segun_estado(ruta)` lee
`EstadoNodo.en_linea` y exige que la última sincronización buena sea reciente. Sin archivo, o viejo, es «sin
conexión»: ante la duda, no se anula.

✅ **Cerrado (ADR-068):** `en_linea_segun_estado` es la de `libracore.nodo_offline` (ADR-074 del motor, libracore
v1.186.0), re-exportada acá.

## La factura del nodo (ADR-068)

El nodo factura con internet con el ticket de ARCA que le presta el central (ADR-074 del motor, ADR-009 de
libraedge). La factura nace en la base del nodo con su número y CAE; `venta.facturada` la sube. Lo encola
`al_facturar_con_outbox(outbox, abrir_conexion)`, que va en `PuertoDeVentas.al_facturar`: ese gancho corre **después**
de vincular la factura, fuera de la transacción de la venta, así que el encolado abre su propia conexión y confirma.
El payload de la factura es el de `libracore.facturas_del_nodo.payload_factura` (libracore >= ADR-074; se importa
recién al usarlo).

Si `al_facturar` falla (libracore lo registra y no lo reintenta), la factura quedaría en el nodo para siempre.
`reconciliar_ventas_facturadas` lo recupera: busca las ventas del nodo con factura con CAE, ya subidas
(`venta.registrada` en el outbox) y sin `venta.facturada`, y la encola. Corre al arrancar el producto y en cada vuelta
de la facturación al volver (`reconciliador(outbox, abrir_conexion)`). Un duplicado por carrera (dos workers, o la
reconciliación cruzada con `al_facturar`) no rompe nada: el aplicador del central es idempotente.

## Los clientes que se dan de alta en el nodo (ADR-070)

Un cliente dado de alta en el nodo nace con un id provisorio negativo y sube como `cliente.alta` (`erp.offline_clientes`).
`venta.registrada` lleva ese id tal cual en `cliente_id` y el central lo traduce con el mapeo del alta
(`offline_clientes.cliente_del_central`): sin mapeo, la venta se rechaza (`ClienteNoSincronizado`, revisión manual) en vez
de quedar a nombre de otro cliente. El alta sale antes que la venta porque comparten la secuencia del outbox.

## La caja y los turnos (ADR-069)

Los turnos, los movimientos de caja a mano y el cierre del día suben con cinco operaciones más (`turno.abierto`,
`turno.cerrado`, `caja.movimiento`, `caja.movimiento_anulado`, `dia.cerrado`), que viven en `erp.offline_caja`. Acá queda
lo que cruza con la venta: `venta.registrada` lleva el `turno_id_nodo` y el central lo traduce al turno que subió
(`offline_caja.turno_del_central`); si el turno no está, la venta se aplica sin turno, como antes. `Outbox.caja_con_turno`
dice si el producto escribe el turno en los movimientos de caja de la venta (el mismo `caja_con_turno` de
`registrar_venta`): el central lo reproduce igual para que el arqueo del turno dé lo mismo en las dos puntas.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from ..hora import ahora_utc
from .hooks import SIN_GANCHOS, Hooks, RequiereConexion

TIPO_VENTA_REGISTRADA = "venta.registrada"
TIPO_PAGO_ACREDITADO = "venta.pago_acreditado"
TIPO_VENTA_ANULADA = "venta.anulada"
TIPO_VENTA_DEVOLUCION = "venta.devolucion"
TIPO_VENTA_FACTURADA = "venta.facturada"
TIPOS = (TIPO_VENTA_REGISTRADA, TIPO_PAGO_ACREDITADO, TIPO_VENTA_ANULADA, TIPO_VENTA_DEVOLUCION, TIPO_VENTA_FACTURADA)

#: Versión del contrato de los payloads. Si cambia la forma, sube: el central rechaza lo que no entiende en vez de
#: materializar algo a medias.
SCHEMA_VERSION = 1

#: El `source_type` de una venta del central que subió de un nodo: `libraedge:<node_id>`.
ORIGEN_NODO = "libraedge:"

_PREFIJO_VALIDO = re.compile(r"^[A-Z0-9]{1,8}$")


class ColisionDeNumero(ValueError):
    """El número de una venta del nodo ya lo tiene otra venta del central: no es un duplicado, es un choque de
    numeración. Va a revisión manual."""


class VentaNoSincronizada(ValueError):
    """Una operación sobre una venta que el central todavía no tiene (su `venta.registrada` no se aplicó)."""


class VentaConOtraFactura(ValueError):
    """La venta del central ya está vinculada a una factura distinta de la que emitió el nodo. Revisión manual."""


# ── El nodo ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NodoLocal:
    """La identidad de ESTE nodo: su `node_id` (el de `node_identity`) y el prefijo de su sucursal (`S02`), que va
    adelante de cada número de venta. Mayúsculas y dígitos, hasta 8."""

    node_id: str
    prefijo: str

    def __post_init__(self) -> None:
        if not self.node_id or not self.node_id.strip():
            raise ValueError("el node_id del nodo no puede estar vacío")
        if not _PREFIJO_VALIDO.match(self.prefijo or ""):
            raise ValueError(
                f"prefijo de sucursal inválido {self.prefijo!r}: mayúsculas y dígitos, de 1 a 8 (p. ej. S02)"
            )


def nodo_desde_entorno(variable_nodo: str, variable_prefijo: str) -> Callable[[], NodoLocal | None]:
    """Un `Outbox.nodo` que lee la identidad del entorno, como el piloto de RestoLibra (`RESTOLIBRA_EDGE_NODE_ID`).

    Sin `variable_nodo` la instancia no es un nodo (`None`): el central y una instalación común no encolan nada. Con
    el nodo y sin prefijo, `ValueError` al primer uso: un nodo sin prefijo numeraría igual que el central.
    Se lee en cada llamada (es una variable de entorno: barato), así un test lo puede cambiar."""

    def nodo() -> NodoLocal | None:
        node_id = (os.environ.get(variable_nodo) or "").strip()
        if not node_id:
            return None
        return NodoLocal(node_id=node_id, prefijo=(os.environ.get(variable_prefijo) or "").strip())

    return nodo


# Una sola definición en el motor (libracore v1.186.0, ADR-074): libracommerce la re-exporta para los productos que
# ya la importan de acá. Cerrado el pendiente del ADR-068.
from libracore.nodo_offline import en_linea_segun_estado  # noqa: E402


def _sin_conexion() -> bool:
    return False


@dataclass(frozen=True)
class Outbox:
    """Lo que `con_outbox` necesita saber del producto.

    - `nodo`: la identidad de este nodo, o `None` si la instancia no es un nodo (el central, o una instalación común:
      ahí `con_outbox` no cambia nada). Ver `nodo_desde_entorno`.
    - `en_linea`: si el nodo está sincronizando con el central. El default es `False` —sin conexión—, el seguro: sin
      cablearlo, un nodo no anula ninguna venta facturada. Ver `en_linea_segun_estado`.
    - `extra_de_venta`: lo propio del producto que viaja con la venta (`(conn, venta) -> dict | None`), en
      `payload["extra"]`. RestoLibra: el pedido y la mesa. Del otro lado lo lee `al_materializar`.
    - `caja_con_turno`: si los movimientos de caja de la venta llevan el `turno_id` (el `caja_con_turno` que el producto
      le pasa a `registrar_venta` y `acreditar_pago_qr`; VentaLibra lo prende). Viaja en `venta.registrada` y
      `venta.pago_acreditado` (ADR-069). El default, `False`, es el de siempre: el central escribe esa caja sin turno.
    """

    nodo: Callable[[], NodoLocal | None]
    en_linea: Callable[[], bool] = _sin_conexion
    extra_de_venta: Callable[[Any, Mapping[str, Any]], Mapping[str, Any] | None] | None = None
    caja_con_turno: bool = False


# ── Traducción (puro: sin base) ───────────────────────────────────────────


def _texto(valor) -> str | None:
    """Plata y cantidades como texto (no `float` en JSON); `None` pasa como `None`."""
    if valor is None:
        return None
    if isinstance(valor, float):
        return repr(valor)
    return str(valor)


def _fecha(valor) -> str | None:
    # En PostgreSQL `occurred_on` es DATE y llega como `date`; el payload va a JSON.
    return None if valor is None else str(valor)


def _ahora_utc() -> str:
    return ahora_utc().isoformat()


def armar_operacion(nodo: NodoLocal, secuencia: int, tipo: str, venta_id: int, payload: dict, *,
                    ocurrida: str | None = None):
    """La `OutboxOperation` de LibraEdge para `tipo` sobre la venta local `venta_id`.

    `operation_id = "<node_id>:<secuencia>"` (la secuencia es del nodo, única y sin huecos) y
    `aggregate_id = "<node_id>:venta:<venta_id>"`."""
    try:
        from libraedge.domain.sync import OutboxOperation
    except ImportError as exc:  # pragma: no cover - depende del entorno
        raise RuntimeError(
            "Falta la dependencia opcional libraedge para el nodo offline: instalar libracommerce[offline]"
        ) from exc
    if tipo not in TIPOS:
        raise ValueError(f"tipo de operación desconocido: {tipo!r}")
    return OutboxOperation(
        operation_id=f"{nodo.node_id}:{secuencia}", node_id=nodo.node_id, sequence=secuencia,
        operation_type=tipo, aggregate_type="venta", aggregate_id=f"{nodo.node_id}:venta:{venta_id}",
        occurred_at=ocurrida or _ahora_utc(), schema_version=SCHEMA_VERSION, payload=payload,
    )


def payload_venta_registrada(venta: Mapping[str, Any], *, lineas: list[Mapping[str, Any]],
                             movimientos: list[Mapping[str, Any]], promociones: list[Mapping[str, Any]],
                             sucursal_id: int | None, extra: Mapping[str, Any] | None = None,
                             caja_con_turno: bool = False) -> dict:
    """El payload de `venta.registrada`: la venta entera como la dejó el nodo.

    `venta` es `obtener_venta`; `lineas`, las filas de `sale_items` en orden de id (con `unit_cost_snapshot`);
    `movimientos`, las filas del ledger que descontó la venta (receta y lotes ya resueltos: el central las repite tal
    cual en vez de volver a resolverlas); `promociones`, las de `sale_promotions`.

    Los ids de producto, variante, depósito y sucursal viajan tal cual: son datos de referencia que el central publica y
    el nodo espeja con la misma clave. El del **cliente** también, salvo uno dado de alta en el nodo: ése viaja con su id
    provisorio (negativo) y el central lo traduce con el mapeo de su `cliente.alta` (ADR-070, `offline_clientes`).
    `turno_id_nodo` es el del **nodo**: el central lo traduce al turno que subió con `turno.abierto` (ADR-069) y, si no
    está, aplica la venta sin turno. `caja_con_turno` (aditivo: un payload sin él es `False`) dice si la caja de lo
    acreditado lleva ese turno."""
    return {
        "venta_id": venta["id"], "numero": venta["numero"], "fecha": _fecha(venta["fecha"]),
        "estado": venta["estado"], "subtotal": _texto(venta["subtotal"]), "descuento": _texto(venta["descuento"]),
        "total": _texto(venta["total"]), "cliente_id": venta.get("cliente_id"),
        "cliente_nombre": venta.get("cliente_nombre") or "", "usuario_id": venta.get("usuario_id"),
        "observaciones": venta.get("observaciones") or "", "sucursal_id": sucursal_id,
        "turno_id_nodo": venta.get("turno_id"), "caja_con_turno": bool(caja_con_turno),
        "items": [
            {
                "producto_id": li["item_id"], "variante_id": li["variant_id"], "nombre": li["description_snapshot"],
                "qty": _texto(li["quantity"]), "precio": _texto(li["unit_price"]),
                "costo": _texto(li["unit_cost_snapshot"]),
            }
            for li in lineas
        ],
        "pagos": [
            {
                "medio": p["medio"], "monto": _texto(p["monto"]), "referencia": p.get("referencia") or "",
                "estado": p["estado"], "recibido": _texto(p.get("recibido")),
            }
            for p in venta["pagos"]
        ],
        "stock": [
            {
                "producto_id": m["item_id"], "variante_id": m["variant_id"], "deposito_id": m["location_id"],
                "cantidad": _texto(m["quantity_delta"]), "lote": m["lot_code"],
                "vence": _fecha(m["expires_at"]),
            }
            for m in movimientos
        ],
        "promociones": [
            {"promocion_id": p["promotion_id"], "nombre": p["name"], "veces": p["times"], "ahorro": _texto(p["amount"])}
            for p in promociones
        ],
        "extra": dict(extra) if extra else None,
    }


def payload_pago_acreditado(venta: Mapping[str, Any], pagos: list[Mapping[str, Any]], *,
                            caja_con_turno: bool = False) -> dict:
    """El payload de `venta.pago_acreditado`. Cada pago va por su **posición** entre los pagos de la venta (en orden
    de id): el central los escribió en el mismo orden, y el id local no le dice nada. `caja_con_turno` (aditivo) dice si
    la caja de lo que se acredita lleva el turno de la venta (ADR-069)."""
    posiciones = {p["id"]: i for i, p in enumerate(venta["pagos"])}
    return {
        "venta_id": venta["id"], "numero": venta["numero"], "estado": venta["estado"],
        "payment_id": next((p.get("payment_id") for p in pagos if p.get("payment_id")), "") or "",
        "caja_con_turno": bool(caja_con_turno),
        "pagos": [
            {"posicion": posiciones[p["id"]], "medio": p["medio"], "monto": _texto(p["monto"])} for p in pagos
        ],
    }


def payload_venta_anulada(venta: Mapping[str, Any]) -> dict:
    """El payload de `venta.anulada`. Quién anuló no viaja: el gancho `al_anular_venta` recibe la venta, no el usuario
    (`venta["usuario_id"]` es quien la registró), así que el central anula sin usuario."""
    return {"venta_id": venta["id"], "numero": venta["numero"]}


def payload_venta_devolucion(venta: Mapping[str, Any], devolucion: Mapping[str, Any], *,
                             ids_de_lineas: list[int]) -> dict:
    """El payload de `venta.devolucion`. Las líneas van por **posición** (orden de id de `sale_items`), como los pagos."""
    posiciones = {sid: i for i, sid in enumerate(ids_de_lineas)}
    return {
        "venta_id": venta["id"], "numero": venta["numero"], "usuario_id": devolucion.get("usuario_id"),
        "lineas": [
            {"posicion": posiciones[int(sid)], "cantidad": _texto(cant)} for sid, cant in devolucion["lineas"].items()
        ],
        "deposito_id": devolucion["deposito_id"], "medio_pago": devolucion["medio_pago"],
        "importe": _texto(devolucion["importe"]), "clave_operacion_nodo": devolucion.get("clave_operacion"),
    }


# ── Lectura en el nodo ─────────────────────────────────────────────────────

def _payload_factura_de_libracore(factura: Mapping[str, Any]) -> dict:
    try:
        from libracore.facturas_del_nodo import payload_factura
    except ImportError as exc:
        raise RuntimeError(
            "venta.facturada necesita libracore con facturas_del_nodo (ADR-074 del motor)") from exc
    return payload_factura(dict(factura))


def payload_venta_facturada(venta: Mapping[str, Any], factura: Mapping[str, Any], *,
                            payload_factura: Callable[[Mapping[str, Any]], dict] | None = None) -> dict:
    """`{"venta_id", "numero", "factura"}`: la venta local por su id y número, y la factura como la arma
    `libracore.facturas_del_nodo.payload_factura` (número, CAE, ítems, importes, ambiente, emisor, `id_en_el_nodo`,
    `version`). `payload_factura` reemplaza esa función (tests, o un producto con su propia factura)."""
    armar = payload_factura or _payload_factura_de_libracore
    return {"venta_id": venta["id"], "numero": venta["numero"], "factura": armar(factura)}


_SQL_LINEAS = (
    "SELECT id, item_id, variant_id, description_snapshot, quantity, unit_price, unit_cost_snapshot "
    "FROM sale_items WHERE sale_id=? ORDER BY id"
)


def _lineas(conn, venta_id: int) -> list[dict]:
    return [dict(f) for f in conn.execute(_SQL_LINEAS, (venta_id,)).fetchall()]


def _movimientos_vendidos(conn, venta_id: int) -> list[dict]:
    from .ventas import _COND_VENDIDO

    return [dict(f) for f in conn.execute(
        "SELECT item_id, variant_id, location_id, quantity_delta, lot_code, expires_at FROM stock_movements "
        f"WHERE source_id=? AND ({_COND_VENDIDO}) ORDER BY id", (venta_id,),
    ).fetchall()]


def _tiene_tabla(conn, tabla: str) -> bool:
    # Por metadatos, no con un SELECT: en PostgreSQL un SELECT fallido aborta la transacción (mismo criterio que
    # `ventas.tiene_devoluciones_de_servicio`).
    return bool(conn.execute(f"PRAGMA table_info({tabla})").fetchall())


def _promociones(conn, venta_id: int) -> list[dict]:
    if not _tiene_tabla(conn, "sale_promotions"):
        return []
    return [dict(f) for f in conn.execute(
        "SELECT promotion_id, name, times, amount FROM sale_promotions WHERE sale_id=? ORDER BY id", (venta_id,),
    ).fetchall()]


def _sucursal(conn, venta_id: int) -> int | None:
    fila = conn.execute("SELECT branch_id FROM sales WHERE id=?", (venta_id,)).fetchone()
    return fila[0] if fila else None


# ── Encolado (en la transacción del llamador) ─────────────────────────────


def encolar(conn, nodo: NodoLocal, tipo: str, venta_id: int, payload: dict):
    """Encola la operación **dentro de la transacción de `conn`** (`commit=False` en las dos llamadas). Devuelve la
    `OutboxOperation` encolada."""
    from libraedge.db.repository import NodeRepository

    repo = NodeRepository(conn)
    secuencia = repo.next_sequence(commit=False)
    operacion = armar_operacion(nodo, secuencia, tipo, venta_id, payload)
    return repo.enqueue_operation(operacion, commit=False)


def encolar_venta_registrada(conn, nodo: NodoLocal, venta: Mapping[str, Any], *,
                             extra: Mapping[str, Any] | None = None, caja_con_turno: bool = False):
    vid = venta["id"]
    payload = payload_venta_registrada(
        venta, lineas=_lineas(conn, vid), movimientos=_movimientos_vendidos(conn, vid),
        promociones=_promociones(conn, vid), sucursal_id=_sucursal(conn, vid), extra=extra,
        caja_con_turno=caja_con_turno,
    )
    return encolar(conn, nodo, TIPO_VENTA_REGISTRADA, vid, payload)


def encolar_pago_acreditado(conn, nodo: NodoLocal, venta: Mapping[str, Any], pagos: list[Mapping[str, Any]], *,
                            caja_con_turno: bool = False):
    return encolar(conn, nodo, TIPO_PAGO_ACREDITADO, venta["id"],
                   payload_pago_acreditado(venta, pagos, caja_con_turno=caja_con_turno))


def encolar_venta_anulada(conn, nodo: NodoLocal, venta: Mapping[str, Any]):
    return encolar(conn, nodo, TIPO_VENTA_ANULADA, venta["id"], payload_venta_anulada(venta))


def encolar_venta_devolucion(conn, nodo: NodoLocal, venta: Mapping[str, Any], devolucion: Mapping[str, Any]):
    ids = [li["id"] for li in _lineas(conn, venta["id"])]
    return encolar(conn, nodo, TIPO_VENTA_DEVOLUCION, venta["id"],
                   payload_venta_devolucion(venta, devolucion, ids_de_lineas=ids))


def encolar_venta_facturada(conn, nodo: NodoLocal, venta: Mapping[str, Any], factura: Mapping[str, Any], *,
                            payload_factura: Callable[[Mapping[str, Any]], dict] | None = None):
    return encolar(conn, nodo, TIPO_VENTA_FACTURADA, venta["id"],
                   payload_venta_facturada(venta, factura, payload_factura=payload_factura))


def _agregado(nodo: NodoLocal, venta_id) -> str:
    return f"{nodo.node_id}:venta:{venta_id}"


def _agregados_en_el_outbox(conn, nodo: NodoLocal, tipo: str) -> set[str]:
    """Los `aggregate_id` de las operaciones `tipo` de este nodo en el outbox, en cualquier estado (pendiente,
    enviada, confirmada o en revisión): lo que ya se encoló una vez no se vuelve a encolar."""
    return {f[0] for f in conn.execute(
        "SELECT aggregate_id FROM sync_outbox WHERE node_id=? AND operation_type=?", (nodo.node_id, tipo),
    ).fetchall()}


def _factura_ya_encolada(conn, nodo: NodoLocal, venta_id) -> bool:
    return conn.execute(
        "SELECT 1 FROM sync_outbox WHERE node_id=? AND operation_type=? AND aggregate_id=?",
        (nodo.node_id, TIPO_VENTA_FACTURADA, _agregado(nodo, venta_id)),
    ).fetchone() is not None


def reconciliar_ventas_facturadas(conn, outbox: Outbox, *,
                                  payload_factura: Callable[[Mapping[str, Any]], dict] | None = None) -> list[int]:
    """Encola `venta.facturada` para cada venta del nodo con factura **con CAE** que ya subió (`venta.registrada`
    en el outbox) y todavía no tiene su `venta.facturada` (en ningún estado). Devuelve los ids de las ventas
    encoladas. Idempotente: una segunda pasada no encuentra nada.

    Encola en la transacción de `conn` (no confirma). Una venta que nunca subió no se toca: su factura sin la venta
    iría a revisión manual en el central. Una factura sin CAE tampoco: todavía no es un comprobante. Fuera de un nodo
    no hace nada."""
    nodo = outbox.nodo()
    if nodo is None:
        return []
    from .ventas import obtener_venta

    subidas = _agregados_en_el_outbox(conn, nodo, TIPO_VENTA_REGISTRADA)
    facturadas = _agregados_en_el_outbox(conn, nodo, TIPO_VENTA_FACTURADA)
    filas = conn.execute(
        "SELECT vl.venta_id, vl.factura_id FROM venta_links vl JOIN facturas f ON f.id = vl.factura_id"
        " WHERE vl.factura_id IS NOT NULL AND f.cae IS NOT NULL AND f.cae <> '' ORDER BY vl.venta_id"
    ).fetchall()
    encoladas = []
    for venta_id, factura_id in ((f[0], f[1]) for f in filas):
        agregado = _agregado(nodo, venta_id)
        if agregado not in subidas or agregado in facturadas:
            continue
        venta = obtener_venta(conn, venta_id)
        factura = conn.execute("SELECT * FROM facturas WHERE id=?", (factura_id,)).fetchone()
        if venta is None or factura is None:
            continue
        encolar_venta_facturada(conn, nodo, venta, dict(factura), payload_factura=payload_factura)
        facturadas.add(agregado)
        encoladas.append(venta_id)
    return encoladas


def reconciliador(outbox: Outbox, abrir_conexion: Callable[[], Any], *,
                  payload_factura: Callable[[Mapping[str, Any]], dict] | None = None) -> Callable[[], list[int]]:
    """`reconciliar_ventas_facturadas` en su propia transacción, para llamar sin argumentos: al arrancar el producto
    y en cada vuelta de la facturación al volver. Fuera de un nodo no abre conexión."""

    def reconciliar() -> list[int]:
        if outbox.nodo() is None:
            return []
        with abrir_conexion() as conn:
            encoladas = reconciliar_ventas_facturadas(conn, outbox, payload_factura=payload_factura)
            conn.commit()
        return encoladas

    return reconciliar


def al_facturar_con_outbox(outbox: Outbox, abrir_conexion: Callable[[], Any], *,
                           base: Callable[[dict, dict], None] | None = None,
                           payload_factura: Callable[[Mapping[str, Any]], dict] | None = None,
                           ) -> Callable[[dict, dict], None]:
    """El `PuertoDeVentas.al_facturar(venta, factura)` de libracore con `venta.facturada` encolada (ADR-068).

    Corre `base` (lo que el producto ya hacía al facturar) y, en un nodo, encola la factura **en una transacción
    propia**: libracore llama a este gancho después de vincular la factura, fuera de la transacción de la venta.
    `abrir_conexion()` devuelve un context manager de conexión (`libracore.db.core.get_connection`). Fuera de un nodo
    no encola nada. Un error acá lo registra libracore y no se propaga (la factura ya tiene su CAE)."""

    def al_facturar(venta: dict, factura: dict) -> None:
        if base is not None:
            base(venta, factura)
        nodo = outbox.nodo()
        if nodo is None:
            return
        with abrir_conexion() as conn:
            if not _factura_ya_encolada(conn, nodo, venta["id"]):
                encolar_venta_facturada(conn, nodo, venta, factura, payload_factura=payload_factura)
            conn.commit()

    return al_facturar


# ── Composición de ganchos ─────────────────────────────────────────────────


def con_outbox(hooks: Hooks, outbox: Outbox) -> Hooks:
    """Los ganchos del producto con el outbox de LibraEdge sumado, **sin pisar** los que ya tiene.

    Cada gancho compuesto corre primero el del producto y después encola (si el del producto levanta, no se encola
    nada, y de todos modos la transacción entera se revierte). El numerador antepone el prefijo de la sucursal al del
    producto. **Fuera de un nodo** (`outbox.nodo()` es `None`, el central o una instalación común) todo se comporta
    exactamente como `hooks`: se puede armar siempre así, en las dos puntas, con la misma línea.

    `al_confirmar_venta` no se toca: la venta que llega a `cobrada` ya subió como `venta.registrada` o
    `venta.pago_acreditado` (ver el docstring del módulo)."""

    def al_registrar_venta(conn, venta):
        hooks.al_registrar_venta(conn, venta)
        nodo = outbox.nodo()
        if nodo is not None:
            extra = outbox.extra_de_venta(conn, venta) if outbox.extra_de_venta is not None else None
            encolar_venta_registrada(conn, nodo, venta, extra=extra, caja_con_turno=outbox.caja_con_turno)

    def al_acreditar_pago(conn, venta, pagos):
        hooks.al_acreditar_pago(conn, venta, pagos)
        nodo = outbox.nodo()
        if nodo is not None and pagos:
            encolar_pago_acreditado(conn, nodo, venta, list(pagos), caja_con_turno=outbox.caja_con_turno)

    def al_anular_venta(conn, venta):
        nodo = outbox.nodo()
        if nodo is not None and venta.get("factura_id") and not outbox.en_linea():
            raise RequiereConexion(
                f"La venta {venta['numero']} está facturada y este equipo no tiene conexión con el central: no se "
                "puede anular sin conexión. Esperá a que vuelva la conexión y anulala con su nota de crédito."
            )
        hooks.al_anular_venta(conn, venta)
        if nodo is not None:
            encolar_venta_anulada(conn, nodo, venta)

    def al_devolver_items(conn, venta, devolucion):
        hooks.al_devolver_items(conn, venta, devolucion)
        nodo = outbox.nodo()
        if nodo is not None:
            encolar_venta_devolucion(conn, nodo, venta, devolucion)

    def numerador(conn):
        base = hooks.numerador(conn)
        nodo = outbox.nodo()
        return f"{nodo.prefijo}-{base}" if nodo is not None else base

    return replace(
        hooks, al_registrar_venta=al_registrar_venta, al_acreditar_pago=al_acreditar_pago,
        al_anular_venta=al_anular_venta, al_devolver_items=al_devolver_items, numerador=numerador,
    )


# ── Aplicadores del central ────────────────────────────────────────────────


def _verificar(operacion, tipo: str) -> None:
    if operacion.operation_type != tipo:
        raise ValueError(f"se esperaba {tipo}, llegó {operacion.operation_type!r}")
    if operacion.schema_version != SCHEMA_VERSION:
        raise ValueError(f"schema {operacion.schema_version} de {tipo} desconocido (se entiende {SCHEMA_VERSION})")


def origen_de(node_id: str) -> str:
    """El `sales.source_type` de una venta que subió del nodo `node_id`."""
    return f"{ORIGEN_NODO}{node_id}"


def venta_del_central(conn, node_id: str, venta_id_nodo: int) -> int | None:
    """El id en el central de la venta `venta_id_nodo` del nodo `node_id`, o `None` si no subió."""
    fila = conn.execute(
        "SELECT id FROM sales WHERE source_type=? AND source_id=?", (origen_de(node_id), venta_id_nodo),
    ).fetchone()
    return fila[0] if fila else None


def _venta_existente(conn, operacion) -> int:
    datos = operacion.payload
    vid = venta_del_central(conn, operacion.node_id, datos["venta_id"])
    if vid is None:
        raise VentaNoSincronizada(
            f"la venta {datos.get('numero')!r} (id {datos['venta_id']}) del nodo {operacion.node_id} no está en el "
            f"central: falta aplicar su {TIPO_VENTA_REGISTRADA}"
        )
    return vid


def _num(valor) -> float:
    return float(Decimal(str(valor)))


def aplicar_venta_registrada(conn, operacion, *, hooks: Hooks = SIN_GANCHOS,
                             al_materializar: Callable[[Any, int, Any], None] | None = None) -> None:
    """Crea en el central la venta que nació en el nodo, con las mismas piezas que `registrar_venta`.

    No pasa por `registrar_venta`: el número ya lo puso el nodo (con su prefijo), el turno viene traducido, y el stock se
    repite fila por fila desde el ledger del nodo en vez de volver a resolver recetas y lotes acá. Sí usa
    `crear_venta`, `agregar_pago` (el fiado entra al libro de clientes igual que en el nodo) y
    `create_caja_movimiento` para lo acreditado.

    **El turno (ADR-069).** `turno_id_nodo` se traduce al turno del central que subió con `turno.abierto`
    (`offline_caja.turno_del_central`): la venta queda atada a él (`venta_links`) y, si el nodo escribía el turno en la
    caja de la venta (`caja_con_turno` del payload), también sus movimientos. Si el turno no está —la venta sin turno, o un
    turno que todavía no subió—, la venta se aplica sin turno, como antes: no se rechaza por eso. Al final corre
    `hooks.al_registrar_venta`,
    `hooks.al_confirmar_venta` si nació cobrada y `al_materializar(conn, venta_id, operacion)`, lo propio del
    producto con `payload["extra"]`."""
    _verificar(operacion, TIPO_VENTA_REGISTRADA)
    from libracore import medios_pago
    from libracore import pagos as acreditacion
    from libracore.db.caja import create_caja_movimiento

    from . import promociones as _promociones
    from .offline_caja import turno_del_central
    from .offline_clientes import cliente_del_central
    from .stock import add_movimiento_stock
    from .ventas import CONCEPTO_VENTA, agregar_pago, crear_venta, obtener_venta, vincular_venta_turno

    datos = operacion.payload
    if venta_del_central(conn, operacion.node_id, datos["venta_id"]) is not None:
        return
    numero = datos["numero"]
    if conn.execute("SELECT 1 FROM sales WHERE number=?", (numero,)).fetchone() is not None:
        raise ColisionDeNumero(
            f"el número de venta {numero!r} del nodo {operacion.node_id} ya existe en el central: colisión de "
            "numeración, no un duplicado"
        )
    usuario_id = datos.get("usuario_id")
    fecha = datos["fecha"]
    turno = turno_del_central(conn, operacion.node_id, datos.get("turno_id_nodo"))
    turno_id_caja = turno["id"] if turno is not None and datos.get("caja_con_turno") else None
    items = [
        {"producto_id": it["producto_id"], "variante_id": it["variante_id"], "nombre": it["nombre"],
         "qty": _num(it["qty"]), "precio": _num(it["precio"])}
        for it in datos["items"]
    ]
    # El cliente: uno provisorio (negativo) es el que el nodo dio de alta y el central tradujo con su `cliente.alta`
    # (ADR-070); si ese alta no se aplicó, la venta se rechaza en vez de quedar a nombre de otro cliente.
    cliente_id = cliente_del_central(conn, operacion.node_id, datos.get("cliente_id"))
    vid = crear_venta(
        conn, numero=numero, fecha=fecha, items=items, subtotal=_num(datos["subtotal"]),
        descuento=_num(datos["descuento"]), total=_num(datos["total"]), cliente_id=cliente_id,
        cliente_nombre=datos.get("cliente_nombre") or "", usuario_id=usuario_id,
        observaciones=datos.get("observaciones") or "", estado=datos["estado"],
        sucursal_id=datos.get("sucursal_id"),
    )
    conn.execute("UPDATE sales SET source_type=?, source_id=? WHERE id=?",
                 (origen_de(operacion.node_id), datos["venta_id"], vid))
    # El costo que estampó el nodo (ADR-016), línea por línea en el mismo orden; sin costo queda NULL, como allá.
    ids = [f[0] for f in conn.execute("SELECT id FROM sale_items WHERE sale_id=? ORDER BY id", (vid,)).fetchall()]
    for sid, it in zip(ids, datos["items"], strict=True):
        if it.get("costo") is not None:
            conn.execute("UPDATE sale_items SET unit_cost_snapshot=? WHERE id=?", (it["costo"], sid))
    if datos.get("promociones"):
        _promociones.registrar_aplicadas(conn, vid, [{**p, "ahorro": _num(p["ahorro"])} for p in datos["promociones"]])
    for p in datos["pagos"]:
        estado_del_pago = acreditacion.estado_de(p)
        agregar_pago(conn, vid, p["medio"], _num(p["monto"]), p.get("referencia") or "",
                     estado=estado_del_pago.value,
                     recibido=_num(p["recibido"]) if p.get("recibido") is not None else None)
        if estado_del_pago not in acreditacion.ACREDITAN:
            continue
        create_caja_movimiento(
            fecha=fecha, tipo="ingreso", concepto=CONCEPTO_VENTA.format(numero=numero) + medios_pago.label(p["medio"]),
            monto=_num(p["monto"]), referencia=p.get("referencia") or "", medio_pago=p["medio"],
            usuario_id=usuario_id, turno_id=turno_id_caja, conn=conn,
        )
    if turno is not None:
        vincular_venta_turno(conn, vid, turno["id"])
    for m in datos["stock"]:
        # Lo que el nodo ya vendió, en el depósito y el lote de donde salió: es un hecho consumado, así que va
        # `sobre_existencias` (el depósito pudo darse de baja en el central mientras tanto).
        add_movimiento_stock(
            conn, producto_id=m["producto_id"], tipo="venta", cantidad=_num(m["cantidad"]),
            referencia=f"Venta ID {vid} (nodo {operacion.node_id})", fecha=fecha, venta_id=vid,
            usuario_id=usuario_id, deposito_id=m["deposito_id"], variant_id=m["variante_id"],
            lot_code=m.get("lote"), expires_at=m.get("vence"), sobre_existencias=True,
        )
    if hooks.al_registrar_venta is not SIN_GANCHOS.al_registrar_venta:
        hooks.al_registrar_venta(conn, obtener_venta(conn, vid))
    if datos["estado"] == "cobrada":
        hooks.al_confirmar_venta(conn, obtener_venta(conn, vid))
    if al_materializar is not None:
        al_materializar(conn, vid, operacion)


def aplicar_pago_acreditado(conn, operacion, *, hooks: Hooks = SIN_GANCHOS) -> None:
    """Acredita en el central lo que se acreditó en el nodo: `acreditar_pago_qr` sobre la venta del central (que acredita
    TODO lo pendiente, igual que en el nodo) y verifica que lo acreditado sea lo mismo. Si no coincide, `ValueError`:
    las dos puntas se separaron y eso lo mira una persona."""
    _verificar(operacion, TIPO_PAGO_ACREDITADO)
    from libracore import pagos as acreditacion

    from .ventas import acreditar_pago_qr, obtener_venta

    datos = operacion.payload
    vid = _venta_existente(conn, operacion)
    acreditar_pago_qr(conn, vid, datos.get("payment_id") or "", usuario_id=None, hooks=hooks,
                      caja_con_turno=bool(datos.get("caja_con_turno")))
    venta = obtener_venta(conn, vid)
    for p in datos["pagos"]:
        posicion = p["posicion"]
        if posicion >= len(venta["pagos"]):
            raise ValueError(f"la venta {venta['numero']} del central no tiene el pago {posicion} que acreditó el nodo")
        local = venta["pagos"][posicion]
        if (local["medio"] != p["medio"] or abs(_num(local["monto"]) - _num(p["monto"])) > 0.005
                or acreditacion.estado_de(local) not in acreditacion.ACREDITAN):
            raise ValueError(
                f"el pago {posicion} de la venta {venta['numero']} no coincide con el del nodo {operacion.node_id}"
            )
    if venta["estado"] != datos["estado"]:
        raise ValueError(
            f"la venta {venta['numero']} quedó {venta['estado']!r} en el central y {datos['estado']!r} en el nodo"
        )


def aplicar_venta_anulada(conn, operacion, *, hooks: Hooks = SIN_GANCHOS) -> None:
    """`anular_venta` sobre la venta del central: repone el stock (las filas que repitió `venta.registrada`), revierte la
    caja y la cuenta corriente con `hooks.cliente_cc_de` del central. Si en el central la venta ya tiene factura con
    CAE (se facturó al volver), `VentaConFacturaCAE` la manda a revisión manual: ahí hace falta la nota de crédito."""
    _verificar(operacion, TIPO_VENTA_ANULADA)
    from .ventas import anular_venta

    vid = _venta_existente(conn, operacion)
    anular_venta(conn, vid, usuario_id=None, hooks=hooks)


def clave_de_operacion(operacion) -> str:
    """La `clave_operacion` (ADR-041) de una devolución que subió del nodo: sale del `operation_id`, así que una
    reaplicación contesta `repetida`. Hasta 64 caracteres; si no entra, un hash."""
    clave = f"nodo:{operacion.operation_id}"
    if len(clave) <= 64 and "[" not in clave and "]" not in clave:
        return clave
    return "nodo:" + hashlib.sha256(operacion.operation_id.encode()).hexdigest()[:40]


def aplicar_venta_devolucion(conn, operacion, *, hooks: Hooks = SIN_GANCHOS) -> None:
    """`devolver_items` sobre la venta del central, con las líneas traducidas por posición y la clave de la operación.
    Si el importe que calcula el central no es el del nodo, `ValueError`: las dos puntas se separaron."""
    _verificar(operacion, TIPO_VENTA_DEVOLUCION)
    from .ventas import devolver_items

    datos = operacion.payload
    vid = _venta_existente(conn, operacion)
    ids = [f[0] for f in conn.execute("SELECT id FROM sale_items WHERE sale_id=? ORDER BY id", (vid,)).fetchall()]
    devoluciones: dict[int, float] = {}
    for li in datos["lineas"]:
        if li["posicion"] >= len(ids):
            raise ValueError(f"la venta {datos['numero']} del central no tiene la línea {li['posicion']} del nodo")
        devoluciones[ids[li["posicion"]]] = _num(li["cantidad"])
    resultado = devolver_items(
        conn, vid, devoluciones, datos["deposito_id"], medio_pago=datos["medio_pago"],
        usuario_id=datos.get("usuario_id"), hooks=hooks, clave_operacion=clave_de_operacion(operacion),
    )
    if abs(_num(resultado["importe"]) - _num(datos["importe"])) > 0.005:
        raise ValueError(
            f"la devolución de la venta {datos['numero']} reintegra {resultado['importe']} en el central y "
            f"{datos['importe']} en el nodo {operacion.node_id}"
        )


def _aplicar_factura_de_libracore(payload: dict, *, conn) -> tuple[int, bool]:
    try:
        from libracore.facturas_del_nodo import aplicar_factura_del_nodo
    except ImportError as exc:
        # Un `ValueError`: va a revisión manual y se reintenta cuando el central tenga el motor al día.
        raise ValueError(
            "el central no puede aplicar venta.facturada: falta libracore con facturas_del_nodo (ADR-074 del motor)"
        ) from exc
    return aplicar_factura_del_nodo(payload, conn=conn)


AplicarFactura = Callable[..., tuple[int, bool]]


def aplicar_venta_facturada(conn, operacion, *, aplicar_factura: AplicarFactura | None = None) -> None:
    """Escribe en el central la factura que emitió el nodo y la vincula a la venta que subió (ADR-068).

    La factura la escribe `aplicar_factura(payload, conn=conn) -> (factura_id, creada)`, por defecto
    `libracore.facturas_del_nodo.aplicar_factura_del_nodo`: con el mismo número y CAE, idempotente por la clave ante
    ARCA (`FacturaEnConflicto`, un `ValueError`, si el central tiene ese número con otro total u otro CAE). Después
    `vincular_factura` y `vincular_cobros_de_venta` (los movimientos de caja que creó `venta.registrada`).
    Idempotente: si la venta ya tiene esa factura, sólo se reasegura el vínculo de los cobros (no toca lo ya
    vinculado). Si tiene **otra**, `VentaConOtraFactura`. Sin la venta, `VentaNoSincronizada`."""
    _verificar(operacion, TIPO_VENTA_FACTURADA)
    from .ventas import obtener_venta, vincular_cobros_de_venta, vincular_factura

    datos = operacion.payload
    if not isinstance(datos.get("factura"), dict):
        raise ValueError(f"venta.facturada sin factura (venta {datos.get('numero')!r} del nodo {operacion.node_id})")
    vid = _venta_existente(conn, operacion)
    factura_id, _creada = (aplicar_factura or _aplicar_factura_de_libracore)(datos["factura"], conn=conn)
    venta = obtener_venta(conn, vid)
    actual = venta.get("factura_id")
    if actual and int(actual) != int(factura_id):
        raise VentaConOtraFactura(
            f"la venta {venta['numero']} del central ya tiene la factura {actual} y el nodo {operacion.node_id} la "
            f"facturó con otra ({factura_id})"
        )
    if not actual:
        vincular_factura(conn, vid, factura_id)
    vincular_cobros_de_venta(conn, venta["numero"], factura_id)


Aplicador = Callable[[Any, Any], None]


def aplicadores(hooks: Hooks = SIN_GANCHOS, *,
                al_materializar: Callable[[Any, int, Any], None] | None = None,
                aplicar_factura: AplicarFactura | None = None) -> dict[str, Aplicador]:
    """`{operation_type: (conn, operacion) -> None}`, para registrar en el despachador de LibraEdge (uno por tipo) o
    juntar con `despachar` en un solo `operation_handler`. `hooks` son los del central (`cliente_cc_de` para la
    cuenta corriente, y lo que el producto haga al nacer o anularse una venta). `aplicar_factura` reemplaza
    `libracore.facturas_del_nodo.aplicar_factura_del_nodo` en `venta.facturada`. Suma los cinco de caja y turnos
    (`offline_caja.aplicadores_de_caja`, ADR-069) y el alta de clientes (`offline_clientes`, ADR-070), que no dependen
    de los ganchos de venta."""
    from .offline_caja import aplicadores_de_caja
    from .offline_clientes import aplicadores_de_clientes

    return {
        **aplicadores_de_caja(),
        **aplicadores_de_clientes(),
        TIPO_VENTA_REGISTRADA: lambda conn, op: aplicar_venta_registrada(
            conn, op, hooks=hooks, al_materializar=al_materializar),
        TIPO_PAGO_ACREDITADO: lambda conn, op: aplicar_pago_acreditado(conn, op, hooks=hooks),
        TIPO_VENTA_ANULADA: lambda conn, op: aplicar_venta_anulada(conn, op, hooks=hooks),
        TIPO_VENTA_DEVOLUCION: lambda conn, op: aplicar_venta_devolucion(conn, op, hooks=hooks),
        TIPO_VENTA_FACTURADA: lambda conn, op: aplicar_venta_facturada(conn, op, aplicar_factura=aplicar_factura),
    }


def registrar_en(despachador, hooks: Hooks = SIN_GANCHOS, *,
                 al_materializar: Callable[[Any, int, Any], None] | None = None,
                 aplicar_factura: AplicarFactura | None = None):
    """Registra los once tipos —los cinco de venta, los cinco de caja y `cliente.alta`—, en `SCHEMA_VERSION`, en un
    `libraedge.sync.despacho.Despachador` (v0.7.0,
    ADR-007 de libraedge) y lo devuelve. Un producto suma los suyos al mismo despachador; registrar dos veces un tipo
    falla al arrancar (es la regla del `Despachador`)::

        despachador = registrar_en(Despachador(), GANCHOS)
        router = create_sync_router(get_connection, despachador=despachador)
    """
    for tipo, aplicador in aplicadores(hooks, al_materializar=al_materializar,
                                       aplicar_factura=aplicar_factura).items():
        despachador.registrar(tipo, SCHEMA_VERSION, aplicador)
    return despachador


def despachar(por_tipo: Mapping[str, Aplicador]) -> Aplicador:
    """Un solo `operation_handler` `(conn, operacion)` que despacha por `operation_type`, para un central con la API de
    un manejador por router (libraedge < 0.7, o el `operation_handler` que se conserva); con v0.7 se prefiere
    `registrar_en`. Un tipo que no está **se rechaza** (`ValueError`, revisión manual): marcarlo
    aplicado sin aplicarlo sería perder la operación en silencio."""

    def manejar(conn, operacion) -> None:
        aplicador = por_tipo.get(operacion.operation_type)
        if aplicador is None:
            raise ValueError(f"tipo de operación desconocido en el central: {operacion.operation_type!r}")
        aplicador(conn, operacion)

    return manejar
