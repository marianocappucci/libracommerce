"""Los clientes que se dan de alta en un nodo offline de LibraEdge: el encolado y el aplicador del central (ADR-070).

Fase 2 de LibraEdge. Un nodo puede dar de alta un cliente sin ver al central, y la decisión del humano (2026-10-21) es que
**el central asigna su id y reescribe las referencias**. El motor de abajo (`libracore` >= 1.189, ADR-077) guarda al cliente
con un **id provisorio negativo** y avisa a un gancho dentro de la transacción del alta; `libraedge` >= 0.18 (ADR-021)
tiene el id provisorio, el mapeo `(nodo, tabla, id_nodo) -> id_central` y el reapuntado cuando el id definitivo baja por el
espejo. Este módulo es lo que une las puntas, igual que `erp.offline` con las ventas y `erp.offline_caja` con la caja:

| `operation_type` | `aggregate_type` / `aggregate_id` | Lo encola | El central |
|---|---|---|---|
| `cliente.alta` | `cliente` / `<node_id>:cliente:<id provisorio>` | el gancho `al_crear_cliente` de `libracore.altas_en_nodo` | busca al cliente (si ya existe, lo usa) o lo crea; anota el mapeo |

## El contrato de `cliente.alta`

`schema_version = 1` (el de las ventas) y el payload que arma `libracore.altas_en_nodo.datos_de_cliente`:
`{"version": 1, "cliente_id": <id provisorio, negativo>, "name", "address", "cuit_dni", "email", "phone",
"iva_condition", "external_ref", "activo", y las columnas de la revisión 0002 si la instancia las tiene}`.

El central, **en la transacción del receptor**:

1. Si ya anotó el mapeo de ese `(nodo, id provisorio)`, no hace nada **siempre que el cliente mapeado sea el mismo** (mismo
   `external_ref`, o mismo CUIT/DNI, o —si ninguno trae documento— mismo nombre): el outbox entrega al menos una vez. Si no
   lo es, el nodo reusó un id provisorio (una PC reemplazada con el mismo `node_id` que arrancó la secuencia de cero) y las
   ventas a ese id irían a otro cliente: `ClienteDelNodoError`, revisión manual. (El receptor ya descarta un
   `operation_id` repetido; esto cubre al id repetido en una operación distinta.)
2. Si no, **busca al cliente**: por `external_ref` si viene, y si no por CUIT/DNI (`get_client_by_cuit`, que normaliza
   guiones y prefiere al activo). Si lo encuentra, **usa ése** y no toca sus datos: el central es la autoridad de los
   clientes. Si no hay CUIT ni `external_ref`, **no deduplica**: dos «Juan Pérez» sin documento son dos clientes (el nodo
   no puede saber que son el mismo, y fusionar por nombre sería peor que duplicar).
3. Si no existe, lo crea con `libracore.db.clients.crear_cliente(..., conn=)` (con su `parties` espejo, el libro y la
   auditoría, como cualquier alta) y **con el id de su secuencia**.
4. Anota el mapeo `clients` **y** `parties` (el cliente y su party tienen el mismo id: ADR de libracore sobre
   `_espejar_party`). El de `parties` es el que usa `venta.registrada`, que referencia `sales.customer_party_id`.

Ese mapeo es también lo que le dice al nodo el id definitivo: `libraedge_mapeo` baja por el espejo y el nodo reapunta lo
que tenía a nombre del provisorio y borra su fila (ADR-021 de libraedge).

## Qué se traduce

`cliente_del_central(conn, node_id, id)` es el único punto que traduce: `None` y un id no negativo pasan sin tocar (el nodo
vendió a un cliente que ya existía en el central y espejó), uno negativo se busca en el mapeo y, si no está,
`ClienteNoSincronizado`. Lo usa `venta.registrada` (`cliente_id`). Las otras operaciones no llevan un cliente: la
devolución, el pago acreditado y la anulación trabajan sobre la venta del central, que ya quedó a nombre del traducido. Un
producto cuyo `extra_de_venta` lleve un id de cliente lo traduce con esta misma función en su `al_materializar`.

## Qué pasa cuando algo falta (rechazo, no reintento)

Todo lo que no se puede aplicar es un `ValueError` (`ClienteDelNodoError`): el receptor lo manda a **revisión manual** y el
nodo la deja en `manual_review` **sin trabar la cola** (ADR-020 de libraedge). Lo que dependía de un alta rechazada —la
venta a ese cliente— se rechaza a su vez (`ClienteNoSincronizado`) en vez de quedar a nombre de nadie, o peor, de otro.
Al resolver el alta, `reintentar` las que dependían, **en el orden en que se crearon** (ADR-020, punto 3).

## Cómo lo cablea un producto

```python
OUTBOX = Outbox(nodo=nodo_local, en_linea=en_linea)                           # el mismo de las ventas
altas_en_nodo.configurar(offline_clientes.altas_en_nodo(OUTBOX))              # una vez, al armar la app (nodo y central)
```

y en el `Aprovisionamiento` del producto (central): `altas_en_nodo=[AltaEnNodo("clients", referencias=
[Referencia(*r) for r in REFERENCIAS_DE_CLIENTES]), "parties"]`. En el central no hay nada más que hacer:
`registrar_en(Despachador(), GANCHOS)` registra el tipo junto con los de venta y los de caja.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from libracore.altas_en_nodo import COLUMNAS_DE_CLIENTE, VERSION, AltasEnNodo

from .offline import NodoLocal, Outbox, _verificar
from .offline_caja import _encolar

logger = logging.getLogger(__name__)

TIPO_CLIENTE_ALTA = "cliente.alta"
TIPOS_DE_CLIENTES = (TIPO_CLIENTE_ALTA,)

#: El `aggregate_type` de la operación (el `aggregate_id` es `<node_id>:cliente:<id provisorio>`).
AGREGADO_CLIENTE = "cliente"

#: Las tablas del mapeo. El cliente y su party tienen el mismo id (`libracore.db.clients._espejar_party`): el mapeo de
#: `parties` es el que usa la venta (`sales.customer_party_id`) y el de `clients` el que usa el resto del motor.
TABLA_CLIENTES = "clients"
TABLA_PARTIES = "parties"


class ClienteDelNodoError(ValueError):
    """Un alta de cliente del nodo (o algo que depende de ella) que el central no puede aplicar. Es un `ValueError` a
    propósito: el receptor de LibraEdge la manda a revisión manual y el nodo no traba la cola (ADR-020 de libraedge)."""


class AltaDeClienteInvalida(ClienteDelNodoError):
    """El payload de `cliente.alta` no se entiende o el central no puede escribir al cliente que trae."""


class ClienteNoSincronizado(ClienteDelNodoError):
    """Una operación que lleva un cliente provisorio del nodo cuyo `cliente.alta` no se aplicó en el central."""


# ── En el nodo: el encolado ─────────────────────────────────────────────────


def altas_en_nodo(outbox: Outbox) -> AltasEnNodo:
    """La `AltasEnNodo` de `libracore` para este producto: la identidad del nodo es la del `outbox`, el id provisorio sale de
    `libraedge.mapeo` y el gancho encola `cliente.alta` **en la transacción del alta**.

    Se arma igual en el nodo y en el central: fuera de un nodo (`outbox.nodo()` es `None`) el motor no pide ids
    provisorios ni avisa, y no hace falta que libraedge esté instalado."""

    def nodo() -> str | None:
        local = outbox.nodo()
        return local.node_id if local is not None else None

    def id_provisorio(conn) -> int:
        try:
            from libraedge.mapeo import siguiente_id_provisorio
        except ImportError as exc:  # pragma: no cover - depende del entorno
            raise RuntimeError(
                "Falta libraedge >= 0.18 para dar de alta clientes en un nodo: instalar libracommerce[offline]"
            ) from exc
        return siguiente_id_provisorio(conn)

    def al_crear_cliente(conn, datos: dict) -> None:
        local = outbox.nodo()
        if local is not None:
            encolar_cliente_alta(conn, local, datos)

    return AltasEnNodo(nodo=nodo, id_provisorio=id_provisorio, al_crear_cliente=al_crear_cliente)


def encolar_cliente_alta(conn, nodo: NodoLocal, datos: dict):
    """Encola `cliente.alta` **dentro de la transacción de `conn`**. `datos` es el de `libracore.altas_en_nodo`."""
    return _encolar(conn, nodo, TIPO_CLIENTE_ALTA, AGREGADO_CLIENTE, datos["cliente_id"], datos)


# ── En el central: traducir y aplicar ───────────────────────────────────────


def cliente_del_central(conn, node_id: str, cliente_id, tabla: str = TABLA_PARTIES):
    """El id en el central de un cliente del nodo `node_id` (por defecto, de su party: lo que referencia una venta).

    `None` y un id no negativo pasan igual: el nodo uso un cliente que ya era del central. Uno negativo se busca en el
    mapeo; si el alta no se aplicó, `ClienteNoSincronizado` (revisión manual: la venta no queda a nombre de nadie)."""
    if cliente_id is None or int(cliente_id) >= 0:
        return cliente_id
    from libraedge.mapeo import SinMapeo, traducir_id

    try:
        return traducir_id(conn, node_id, tabla, int(cliente_id))
    except SinMapeo as exc:
        raise ClienteNoSincronizado(
            f"el cliente provisorio {cliente_id} del nodo {node_id} no está en el central: falta aplicar su "
            f"{TIPO_CLIENTE_ALTA} (quedó en revisión manual o todavía no subió)"
        ) from exc


def _tiene_tabla(conn, tabla: str) -> bool:
    # Por metadatos, no con un SELECT: en PostgreSQL un SELECT fallido aborta la transacción.
    return bool(conn.execute(f"PRAGMA table_info({tabla})").fetchall())


def _datos(operacion) -> dict:
    _verificar(operacion, TIPO_CLIENTE_ALTA)
    datos = operacion.payload
    if datos.get("version") != VERSION:
        raise AltaDeClienteInvalida(
            f"versión {datos.get('version')!r} de los datos de {TIPO_CLIENTE_ALTA} desconocida (se entiende {VERSION})")
    id_nodo = datos.get("cliente_id")
    if isinstance(id_nodo, bool) or not isinstance(id_nodo, int) or id_nodo >= 0:
        raise AltaDeClienteInvalida(
            f"el cliente_id {id_nodo!r} del alta del nodo {operacion.node_id} no es un id provisorio (un entero "
            "negativo): el nodo sólo sube altas con id provisorio")
    if not (datos.get("name") or "").strip():
        raise AltaDeClienteInvalida(f"el alta del cliente {id_nodo} del nodo {operacion.node_id} no trae nombre")
    return datos


def _normalizado(cuit) -> str:
    return (cuit or "").replace("-", "").strip()


def _es_el_mismo(cliente: dict, datos: dict) -> bool:
    """Si el cliente del central es al que se refiere el alta: por `external_ref`, por CUIT/DNI o, si ninguna de las dos
    claves está en los dos lados, por nombre (sin mayúsculas ni espacios de más)."""
    if cliente.get("external_ref") and datos.get("external_ref"):
        return cliente["external_ref"] == datos["external_ref"]
    if _normalizado(cliente.get("cuit_dni")) and _normalizado(datos.get("cuit_dni")):
        return _normalizado(cliente["cuit_dni"]) == _normalizado(datos["cuit_dni"])
    return (cliente.get("name") or "").strip().lower() == (datos.get("name") or "").strip().lower()


def _cliente_existente(conn, datos: dict) -> dict | None:
    """El cliente del central al que corresponde el alta: por `external_ref` y, si no, por CUIT/DNI. `None` si no hay
    ninguno (y si el alta no trae ninguna de las dos claves: no se deduplica por nombre)."""
    from libracore.db.clients import get_client_by_cuit

    if datos.get("external_ref"):
        fila = conn.execute("SELECT * FROM clients WHERE external_ref = ?", (datos["external_ref"],)).fetchone()
        if fila is not None:
            return dict(fila)
    return get_client_by_cuit(datos.get("cuit_dni") or "", conn=conn)


def aplicar_cliente_alta(conn, operacion) -> None:
    """Aplica `cliente.alta` en el central: usa al cliente que ya tiene (mismo `external_ref` o CUIT/DNI) o lo crea con su
    propio id, y anota el mapeo `(nodo, id provisorio) -> id` de `clients` y de `parties`. Todo en la transacción del
    receptor: o queda el cliente y su mapeo, o ninguno. Idempotente."""
    datos = _datos(operacion)
    from libracore.db.clients import crear_cliente
    from libraedge.mapeo import MapeoEnConflicto, anotar_mapeo, traducir_id

    node_id, id_nodo = operacion.node_id, datos["cliente_id"]
    id_central = traducir_id(conn, node_id, TABLA_CLIENTES, id_nodo, exigir=False)
    if id_central is not None:
        actual = conn.execute("SELECT * FROM clients WHERE id = ?", (id_central,)).fetchone()
        if actual is None:
            raise ClienteNoSincronizado(
                f"el alta del cliente {id_nodo} del nodo {node_id} ya estaba mapeada al {id_central} y ese cliente ya "
                "no existe en el central")
        if not _es_el_mismo(dict(actual), datos):
            raise ClienteDelNodoError(
                f"el nodo {node_id} reusó el id provisorio {id_nodo}: ya estaba mapeado al cliente {id_central} del "
                f"central ({actual['name']!r}) y ahora llega el alta de {datos['name']!r}. Una venta a ese id iría a "
                "otro cliente: revisar a mano")
    else:
        existente = _cliente_existente(conn, datos)
        if existente is not None:
            id_central = existente["id"]
            logger.info("cliente.alta del nodo %s: el cliente provisorio %s es el %s del central (ya existía)",
                        node_id, id_nodo, id_central)
        else:
            try:
                id_central = crear_cliente({c: datos[c] for c in COLUMNAS_DE_CLIENTE if c in datos}, conn=conn)
            except ValueError as exc:
                raise AltaDeClienteInvalida(
                    f"el central no puede crear al cliente {id_nodo} del nodo {node_id}: {exc}") from exc
    tablas = [TABLA_CLIENTES]
    if _tiene_tabla(conn, TABLA_PARTIES):
        if conn.execute("SELECT 1 FROM parties WHERE id = ?", (id_central,)).fetchone() is None:
            raise AltaDeClienteInvalida(
                f"el cliente {id_central} del central no tiene su party espejo (parties.id = clients.id): correr "
                "`libracore.db.clients.sincronizar_parties_de_clientes()` y reintentar")
        tablas.append(TABLA_PARTIES)
    try:
        for tabla in tablas:
            anotar_mapeo(conn, node_id, tabla, id_nodo, id_central)
    except MapeoEnConflicto as exc:
        raise ClienteDelNodoError(str(exc)) from exc


def aplicadores_de_clientes() -> dict[str, Callable[[Any, Any], None]]:
    """`{operation_type: (conn, operacion) -> None}` de `cliente.alta`. `erp.offline.aplicadores` lo suma a los de venta
    y caja, así que `registrar_en` los registra todos en el mismo `Despachador`."""
    return {TIPO_CLIENTE_ALTA: aplicar_cliente_alta}


__all__ = [
    "AGREGADO_CLIENTE", "TABLA_CLIENTES", "TABLA_PARTIES", "TIPOS_DE_CLIENTES", "TIPO_CLIENTE_ALTA",
    "AltaDeClienteInvalida", "ClienteDelNodoError", "ClienteNoSincronizado", "altas_en_nodo", "aplicadores_de_clientes",
    "aplicar_cliente_alta", "cliente_del_central", "encolar_cliente_alta",
]
