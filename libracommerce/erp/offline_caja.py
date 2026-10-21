"""La caja y los turnos de un nodo offline de LibraEdge: el encolado y los aplicadores del central (ADR-069).

Fase 2 de LibraEdge. Un nodo abre y cierra turnos, anota ingresos y egresos, anula movimientos y cierra el día **sin ver
al central**; la decisión del humano (2026-10-21) es que todo eso sube. El motor de caja (`libracore` >= 1.188, ADR-076)
da los cinco puntos de extensión (`GanchosDeCaja`) y la columna `origen`/`origen_id` para reconocer una fila que vino de
un nodo; este módulo es lo mismo que `erp.offline` hace con las ventas: los ganchos encolan en el outbox del nodo, y en
el central los aplicadores reproducen el hecho con las funciones del motor.

## Las cinco operaciones

Todas con `schema_version = 1` (el mismo `SCHEMA_VERSION` de las ventas) y el `payload` que arma `libracore.ganchos_de_caja`
(`version: 1`, ids **del nodo**, montos `float`): este módulo no lo toca ni lo reinterpreta.

| `operation_type` | `aggregate_type` / `aggregate_id` | Lo encola | El central |
|---|---|---|---|
| `turno.abierto` | `turno` / `<node_id>:turno:<id>` | `al_abrir_turno` | `create_turno` con la `apertura` del nodo |
| `turno.cerrado` | `turno` / `<node_id>:turno:<id>` | `al_cerrar_turno` | `cerrar_turno_caja` con el `cierre`, lo declarado y el **esperado del nodo** |
| `caja.movimiento` | `caja_movimiento` / `<node_id>:caja_movimiento:<id>` | `al_registrar_movimiento` | `create_caja_movimiento` en el turno del central |
| `caja.movimiento_anulado` | `caja_movimiento` / el mismo | `al_anular_movimiento` | `anular_caja_movimiento` |
| `dia.cerrado` | `cierre_diario` / `<node_id>:cierre_diario:<id>` | `al_cerrar_dia` | `cerrar_dia` sobre los turnos ya subidos |

La secuencia del outbox es **una sola por nodo**, la misma de las ventas: el orden `turno.abierto` → ventas y movimientos de
ese turno → `turno.cerrado` → `dia.cerrado` es el orden en que ocurrieron, y LibraEdge >= 0.17 (ADR-020) sube en ese
orden y corta la tanda ante un fallo de transporte.

## En la misma transacción

Los ganchos reciben la conexión del cambio (es el contrato de `GanchosDeCaja`) y el encolado usa
`next_sequence(commit=False)` y `enqueue_operation(commit=False)`: el turno, el movimiento o el cierre y su operación
viven o mueren juntos. Un gancho que falla frena el cambio: una caja que se abre en el nodo y no sube no es «a medias»,
es una caja que el central no ve.

## Qué sube y qué no (🔑 sin duplicados)

Sólo suben las acciones **que decide una persona en la caja**: los ganchos se pasan por llamada a los routers de caja
(`build_caja_router`, `build_turnos_router`, `build_cierre_diario_router`) o a las funciones de `libracore.db`. **No se
registran globalmente**, y este motor no se los pasa a nada: `registrar_venta`, `anular_venta`, `devolver_items` y
`acreditar_pago_qr` llaman a `create_caja_movimiento` sin `ganchos=`. Los movimientos que nacen de una venta los
reproduce el central al aplicar la venta (`venta.registrada`, `venta.pago_acreditado`, `venta.anulada`,
`venta.devolucion`); si además subieran como `caja.movimiento`, quedarían duplicados.

## Idempotencia en el central

`SyncReceiver` ya descarta un `operation_id` repetido. Además cada aplicador lo es por sí mismo, con la pregunta que
dejó el motor para eso (`get_turno_de_origen`, `get_movimiento_de_origen`, `get_cierre_de_origen`) y el índice único
parcial de `(origen, origen_id)` que vuelve **duro** lo que el aplicador consulta: aplicar dos veces la misma operación
es una fila, no dos. `origen` es el `node_id` del nodo (así lo define ADR-076 del motor, distinto del
`libraedge:<node_id>` del `source_type` de las ventas) y `origen_id` el id de la fila en la base del nodo.

## Cómo se traducen los ids

- **`turno_id`**: al del central por `get_turno_de_origen(node_id, turno_id_nodo)`.
- **`caja_id` y `usuario_id`**: tal cual. Las cajas y los usuarios son datos de referencia que el central publica y el
  nodo espeja con la misma clave (`cajas` y `usuarios` están en las tablas de referencia del aprovisionamiento). Si la
  caja no existe en el central, `CajaNoSincronizada`; el usuario lo ata la clave foránea.
- **`factura_id` y `cc_pago_id` de un movimiento**: **no se traducen: el central los deja en `NULL`.** Son ids de otra
  base (la factura sube con su propia operación, `venta.facturada`, y su id en el central es otro): copiarlos ataría el
  movimiento a *otra* factura y, siendo un ingreso con factura, `create_caja_movimiento` lo llevaría al libro de
  clientes. Perder el vínculo es el mal menor; el concepto y la referencia viajan.

## Qué pasa cuando algo no está (rechazo, no reintento)

Un aplicador que levanta `ValueError` manda la operación a **revisión manual** y el nodo la deja en `manual_review` **sin
trabar la cola** (ADR-020 de libraedge). Cualquier otra excepción es un fallo de transporte para el nodo y **corta la
tanda**: detrás de una operación que no se puede aplicar nunca no saldría nada, y el nodo quedaría «fuera de línea». Por
eso todo lo que falta o choca es un `ValueError`:

- un movimiento, un cierre de turno o una anulación **cuyo turno o movimiento no subió** (su `turno.abierto` quedó en
  revisión): `TurnoNoSincronizado`, `MovimientoNoSincronizado`. Al resolver la primera, `reintentar` las que dependían
  de ella, en el orden en que se crearon;
- la caja que no existe en el central: `CajaNoSincronizada`;
- lo que el motor de caja del central no acepta —el día de la sucursal ya cerrado para abrir un turno, turnos abiertos
  al cerrar el día, el día ya cerrado—: `CajaRechazadaPorElCentral`. El motor levanta `RuntimeError`, que el receptor
  no sabe leer: se traduce acá.

`turno.cerrado` se basta solo: su payload trae todo lo de la apertura, así que si el turno no está en el central
(porque se abrió antes de actualizar el nodo, o su `turno.abierto` está en revisión) lo crea y lo cierra.

## El arqueo del nodo manda; el central lo recalcula y lo marca

El esperado de un turno es **el que vio el cajero en el nodo**: el central lo guarda tal cual (`monto_esperado=` de
`cerrar_turno_caja`) y no lo rechaza. Pero lo recalcula con los movimientos que tiene (`get_resumen_turno_caja`) y, si
no coincide, deja la marca `MARCA_ARQUEO` al final de las `notas` del turno y un `logger.warning`; lo mismo con los
totales de un cierre diario. `arqueos_a_revisar(conn)` los lista. Una diferencia no es un error de la
operación sino una pregunta para una persona: pasa cuando falta subir algo (un movimiento en revisión, los egresos
de una anulación o una devolución del nodo, que hoy no llevan el turno, o un cobro de factura que no sube como movimiento).

## Cómo lo cablea un producto

```python
OUTBOX = Outbox(nodo=nodo_local, en_linea=en_linea, caja_con_turno=True)       # el mismo en el nodo y en el central
GANCHOS_DE_CAJA = caja_con_outbox(GanchosDeCaja(), OUTBOX)                       # fuera de un nodo, GanchosDeCaja() tal cual
build_caja_router(usuario_actual=..., ganchos_de_caja=GANCHOS_DE_CAJA)
build_turnos_router(usuario_actual=..., ganchos_de_caja=GANCHOS_DE_CAJA)
build_cierre_diario_router(usuario_actual=..., ganchos_de_caja=GANCHOS_DE_CAJA)
```

En el central no hay nada más que hacer: `registrar_en(Despachador(), GANCHOS)` registra los diez tipos (los cinco de venta
y estos cinco). Un producto que abre turnos llamando a `libracore.db.turnos` directo (VentaLibra) pasa
`ganchos=GANCHOS_DE_CAJA` ahí. `reabrir_dia` no tiene gancho: el nodo no debe montar `autorizar_reabrir`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from libracore.ganchos_de_caja import SIN_GANCHOS as SIN_GANCHOS_DE_CAJA
from libracore.ganchos_de_caja import VERSION, GanchosDeCaja

from .offline import SCHEMA_VERSION, NodoLocal, Outbox, _ahora_utc, _verificar

logger = logging.getLogger(__name__)

TIPO_TURNO_ABIERTO = "turno.abierto"
TIPO_TURNO_CERRADO = "turno.cerrado"
TIPO_MOVIMIENTO = "caja.movimiento"
TIPO_MOVIMIENTO_ANULADO = "caja.movimiento_anulado"
TIPO_DIA_CERRADO = "dia.cerrado"
TIPOS_DE_CAJA = (
    TIPO_TURNO_ABIERTO, TIPO_TURNO_CERRADO, TIPO_MOVIMIENTO, TIPO_MOVIMIENTO_ANULADO, TIPO_DIA_CERRADO,
)

#: Los `aggregate_type` de las operaciones de caja (el `aggregate_id` es `<node_id>:<tipo>:<id local>`).
AGREGADO_TURNO = "turno"
AGREGADO_MOVIMIENTO = "caja_movimiento"
AGREGADO_CIERRE = "cierre_diario"

#: Lo que el central agrega a las `notas` de un turno o de un cierre cuyo arqueo no coincide con el del nodo.
MARCA_ARQUEO = "[REVISAR ARQUEO]"

#: Diferencia de plata que se toma por distinta (un centavo de redondeo no lo es).
_TOLERANCIA = 0.005


class CajaDelNodoError(ValueError):
    """Una operación de caja del nodo que el central no puede aplicar. Es un `ValueError` a propósito: el receptor de
    LibraEdge la manda a revisión manual y el nodo no traba la cola detrás de ella (ADR-020 de libraedge)."""


class TurnoNoSincronizado(CajaDelNodoError):
    """El turno del nodo no está en el central (su `turno.abierto` no se aplicó)."""


class MovimientoNoSincronizado(CajaDelNodoError):
    """El movimiento del nodo no está en el central (su `caja.movimiento` no se aplicó)."""


class CajaNoSincronizada(CajaDelNodoError):
    """La caja del nodo no existe en el central: las cajas se espejan con la misma clave, y ésta no llegó."""


class CajaRechazadaPorElCentral(CajaDelNodoError):
    """El motor de caja del central no acepta la operación (día cerrado, turnos abiertos, referencia repetida)."""


# ── En el nodo: el encolado ────────────────────────────────────────────────


def _encolar(conn, nodo: NodoLocal, tipo: str, agregado: str, id_local: int, payload: Mapping[str, Any]):
    """Encola la operación **dentro de la transacción de `conn`** (`commit=False` en las dos llamadas)."""
    try:
        from libraedge.db.repository import NodeRepository
        from libraedge.domain.sync import OutboxOperation
    except ImportError as exc:  # pragma: no cover - depende del entorno
        raise RuntimeError(
            "Falta la dependencia opcional libraedge para el nodo offline: instalar libracommerce[offline]"
        ) from exc
    repo = NodeRepository(conn)
    secuencia = repo.next_sequence(commit=False)
    operacion = OutboxOperation(
        operation_id=f"{nodo.node_id}:{secuencia}", node_id=nodo.node_id, sequence=secuencia, operation_type=tipo,
        aggregate_type=agregado, aggregate_id=f"{nodo.node_id}:{agregado}:{id_local}", occurred_at=_ahora_utc(),
        schema_version=SCHEMA_VERSION, payload=dict(payload),
    )
    return repo.enqueue_operation(operacion, commit=False)


def caja_con_outbox(ganchos: GanchosDeCaja | None, outbox: Outbox) -> GanchosDeCaja:
    """Los `GanchosDeCaja` del producto con el outbox de LibraEdge sumado, **sin pisar** los que ya tiene.

    Cada gancho compuesto corre primero el del producto y después encola (si el del producto levanta, no se encola nada,
    y de todos modos la transacción entera se revierte). **Fuera de un nodo** (`outbox.nodo()` es `None`, el central o
    una instalación común) todo se comporta exactamente como `ganchos`: se puede armar siempre así, en las dos puntas.
    `ganchos=None` es `GanchosDeCaja()` (ningún gancho del producto).

    Se pasa a los routers de caja (`ganchos_de_caja=`) o a `libracore.db.turnos`/`caja`/`cierre_diario` (`ganchos=`)."""
    base = ganchos if ganchos is not None else SIN_GANCHOS_DE_CAJA

    def componer(gancho: Callable[[Any, dict], None], tipo: str, agregado: str, clave: str):
        def compuesto(conn, datos: dict) -> None:
            gancho(conn, datos)
            nodo = outbox.nodo()
            if nodo is not None:
                _encolar(conn, nodo, tipo, agregado, datos[clave], datos)

        return compuesto

    return replace(
        base,
        al_abrir_turno=componer(base.al_abrir_turno, TIPO_TURNO_ABIERTO, AGREGADO_TURNO, "turno_id"),
        al_cerrar_turno=componer(base.al_cerrar_turno, TIPO_TURNO_CERRADO, AGREGADO_TURNO, "turno_id"),
        al_registrar_movimiento=componer(
            base.al_registrar_movimiento, TIPO_MOVIMIENTO, AGREGADO_MOVIMIENTO, "movimiento_id"),
        al_anular_movimiento=componer(
            base.al_anular_movimiento, TIPO_MOVIMIENTO_ANULADO, AGREGADO_MOVIMIENTO, "movimiento_id"),
        al_cerrar_dia=componer(base.al_cerrar_dia, TIPO_DIA_CERRADO, AGREGADO_CIERRE, "cierre_id"),
    )


# ── En el central: lo que ya está ──────────────────────────────────────────


def turno_del_central(conn, node_id: str, turno_id_nodo: int | None) -> dict | None:
    """El turno del central que subió del nodo `node_id` con ese id local, o `None` (sin id, o todavía no subió)."""
    if turno_id_nodo is None:
        return None
    from libracore.db.turnos import get_turno_de_origen

    return get_turno_de_origen(node_id, turno_id_nodo, conn=conn)


def _turno_o_error(conn, operacion, turno_id_nodo: int | None) -> dict | None:
    turno = turno_del_central(conn, operacion.node_id, turno_id_nodo)
    if turno_id_nodo is not None and turno is None:
        raise TurnoNoSincronizado(
            f"el turno {turno_id_nodo} del nodo {operacion.node_id} no está en el central: falta aplicar su "
            f"{TIPO_TURNO_ABIERTO}"
        )
    return turno


def _datos(operacion, tipo: str) -> Mapping[str, Any]:
    _verificar(operacion, tipo)
    datos = operacion.payload
    if datos.get("version") != VERSION:
        raise ValueError(f"versión {datos.get('version')!r} de los datos de {tipo} desconocida (se entiende {VERSION})")
    return datos


def _verificar_caja(conn, operacion, caja_id: int | None) -> None:
    if caja_id is None:
        return
    if conn.execute("SELECT 1 FROM cajas WHERE id=?", (caja_id,)).fetchone() is None:
        raise CajaNoSincronizada(
            f"la caja {caja_id} del nodo {operacion.node_id} no existe en el central: las cajas se espejan con la "
            "misma clave y ésta no llegó"
        )


def _distinto(a, b) -> bool:
    return abs(float(a or 0.0) - float(b or 0.0)) > _TOLERANCIA


def _marcar(conn, tabla: str, fila_id: int, texto: str) -> None:
    """Agrega `MARCA_ARQUEO texto` al final de las `notas` de la fila (`turnos_caja` o `cierres_diarios`)."""
    assert tabla in ("turnos_caja", "cierres_diarios")
    fila = conn.execute(f"SELECT notas FROM {tabla} WHERE id=?", (fila_id,)).fetchone()
    previas = (fila[0] or "") if fila else ""
    conn.execute(f"UPDATE {tabla} SET notas=? WHERE id=?", (f"{previas}\n{MARCA_ARQUEO} {texto}".strip(), fila_id))
    logger.warning("caja del nodo: %s %s a revisar: %s", tabla, fila_id, texto)


def arqueos_a_revisar(conn) -> list[dict]:
    """Los turnos y los cierres diarios del central que subieron de un nodo con un arqueo que no coincidía con el
    recalculado (`{"tabla", "id", "origen", "origen_id", "notas"}`)."""
    patron = f"%{MARCA_ARQUEO}%"
    filas = conn.execute(
        "SELECT 'turnos_caja' AS tabla, id, origen, origen_id, notas FROM turnos_caja WHERE notas LIKE ? "
        "UNION ALL "
        "SELECT 'cierres_diarios' AS tabla, id, origen, origen_id, notas FROM cierres_diarios WHERE notas LIKE ? "
        "ORDER BY 1, 2",
        (patron, patron),
    ).fetchall()
    return [{"tabla": f[0], "id": f[1], "origen": f[2], "origen_id": f[3], "notas": f[4]} for f in filas]


def _rechazo_del_motor(exc: RuntimeError) -> CajaRechazadaPorElCentral:
    return CajaRechazadaPorElCentral(str(exc) or type(exc).__name__)


# ── Aplicadores del central ────────────────────────────────────────────────


def _asegurar_turno(conn, operacion, datos: Mapping[str, Any]) -> dict:
    """El turno del central para los `datos` de un turno del nodo: el que ya subió, o el que se crea ahora."""
    from libracore.db import cierre_diario
    from libracore.db.turnos import create_turno, get_turno

    existente = turno_del_central(conn, operacion.node_id, datos["turno_id"])
    if existente is not None:
        return existente
    _verificar_caja(conn, operacion, datos.get("caja_id"))
    try:
        tid = create_turno(
            datos["usuario_id"], datos["monto_inicial"], datos.get("notas") or "", datos.get("caja_id"), conn=conn,
            apertura=datos["apertura"], origen=operacion.node_id, origen_id=datos["turno_id"],
        )
    except cierre_diario.DiaCerradoError as exc:
        raise _rechazo_del_motor(exc) from exc
    return get_turno(tid, conn=conn)


def aplicar_turno_abierto(conn, operacion) -> None:
    """Abre en el central el turno que abrió el nodo, con su `apertura`, su caja y su cajero. Idempotente por
    `(origen, origen_id)`: si el turno ya está (abierto o cerrado), no hace nada."""
    _asegurar_turno(conn, operacion, _datos(operacion, TIPO_TURNO_ABIERTO))


def aplicar_turno_cerrado(conn, operacion) -> None:
    """Cierra en el central el turno del nodo con **lo que declaró y esperaba el nodo** y la hora de cierre del nodo.

    Si el turno no está en el central, lo crea con los datos de la apertura que trae el payload. Idempotente: un turno
    ya cerrado no se toca. Después recalcula el esperado con los movimientos que tiene el central; si no coincide con el
    del nodo, guarda el del nodo y deja la `MARCA_ARQUEO` en las `notas` del turno (no rechaza)."""
    from libracore.db.turnos import cerrar_turno_caja, get_resumen_turno_caja

    datos = _datos(operacion, TIPO_TURNO_CERRADO)
    turno = _asegurar_turno(conn, operacion, datos)
    if turno["estado"] == "cerrado":
        return
    cerrado = cerrar_turno_caja(
        turno["id"], datos["monto_declarado"], datos.get("notas") or "", conn=conn, cierre=datos["cierre"],
        monto_esperado=datos["monto_esperado"],
    )
    recalculado = round(
        float(cerrado["monto_inicial"]) + float(get_resumen_turno_caja(turno["id"], conn=conn)["efectivo_ventas"]), 2)
    if _distinto(recalculado, datos["monto_esperado"]):
        _marcar(
            conn, "turnos_caja", turno["id"],
            f"el nodo {operacion.node_id} esperaba {float(datos['monto_esperado']):.2f} en su turno "
            f"{datos['turno_id']} y el central recalcula {recalculado:.2f} con los movimientos que tiene; se guardó "
            "lo del nodo",
        )


def aplicar_caja_movimiento(conn, operacion) -> None:
    """Anota en el central el ingreso o egreso que anotó el nodo, en el turno del central (`TurnoNoSincronizado` si su
    turno no subió) y con su `origen`. Idempotente por `(origen, origen_id)`.

    `factura_id` y `cc_pago_id` no se traducen (son ids del nodo): quedan en `NULL`. Si `create_caja_movimiento` lo
    descarta por tener una referencia que ya existía (su idempotencia por referencia), no hubo fila nueva: se rechaza
    en vez de dar por aplicado un movimiento que no está."""
    from libracore.db.caja import create_caja_movimiento, get_movimiento_de_origen

    datos = _datos(operacion, TIPO_MOVIMIENTO)
    if get_movimiento_de_origen(operacion.node_id, datos["movimiento_id"], conn=conn) is not None:
        return
    turno = _turno_o_error(conn, operacion, datos.get("turno_id"))
    _verificar_caja(conn, operacion, datos.get("caja_id"))
    mov_id = create_caja_movimiento(
        datos["fecha"], datos["tipo"], datos["concepto"], datos["monto"], datos.get("referencia") or "",
        usuario_id=datos.get("usuario_id"), caja_id=datos.get("caja_id"), medio_pago=datos.get("medio_pago") or "",
        turno_id=turno["id"] if turno else None, conn=conn, origen=operacion.node_id,
        origen_id=datos["movimiento_id"],
    )
    propio = get_movimiento_de_origen(operacion.node_id, datos["movimiento_id"], conn=conn)
    if propio is None or propio["id"] != mov_id:
        raise CajaRechazadaPorElCentral(
            f"el movimiento {datos['movimiento_id']} del nodo {operacion.node_id} tiene la referencia "
            f"{datos.get('referencia')!r}, que el central ya tiene en otro movimiento: no se duplica"
        )


def aplicar_caja_movimiento_anulado(conn, operacion) -> None:
    """Anula en el central el movimiento que anuló el nodo. Idempotente: uno ya anulado no se toca. Sin el movimiento,
    `MovimientoNoSincronizado` (revisión manual detrás de su `caja.movimiento`)."""
    from libracore.db.caja import anular_caja_movimiento, get_movimiento_de_origen

    datos = _datos(operacion, TIPO_MOVIMIENTO_ANULADO)
    mov = get_movimiento_de_origen(operacion.node_id, datos["movimiento_id"], conn=conn)
    if mov is None:
        raise MovimientoNoSincronizado(
            f"el movimiento {datos['movimiento_id']} del nodo {operacion.node_id} no está en el central: falta aplicar "
            f"su {TIPO_MOVIMIENTO}"
        )
    if mov["anulado"]:
        return
    anular_caja_movimiento(mov["id"], conn=conn)


def aplicar_dia_cerrado(conn, operacion) -> None:
    """Cierra en el central el día de la sucursal con los turnos que subieron del nodo (`TurnoNoSincronizado` si falta
    alguno o sigue abierto). Idempotente por `(origen, origen_id)`.

    El cierre del central es la foto de **sus** turnos, que ya llevan lo declarado y esperado por el nodo, así que los
    totales coinciden si no falta nada; si no coinciden (falta un turno, o el central tiene otro propio ese día en la
    sucursal), se queda con lo del central y deja la `MARCA_ARQUEO` en las `notas` del cierre. El número correlativo lo
    asigna cada base y puede diferir sin que sea un problema: sólo se registra. Que el día ya esté cerrado o que haya turnos abiertos de otro
    origen es `CajaRechazadaPorElCentral`."""
    from libracore.db import cierre_diario

    datos = _datos(operacion, TIPO_DIA_CERRADO)
    if cierre_diario.get_cierre_de_origen(operacion.node_id, datos["cierre_id"], conn=conn) is not None:
        return
    for t in datos["turnos"]:
        turno = _turno_o_error(conn, operacion, t["turno_id"])
        if turno["estado"] != "cerrado":
            raise TurnoNoSincronizado(
                f"el turno {t['turno_id']} del nodo {operacion.node_id} sigue abierto en el central: falta aplicar su "
                f"{TIPO_TURNO_CERRADO}"
            )
    try:
        cierre = cierre_diario.cerrar_dia(
            datos["usuario_id"], datos.get("sucursal_id"), datos["fecha"], datos.get("notas") or "", conn=conn,
            origen=operacion.node_id, origen_id=datos["cierre_id"],
        )
    except (cierre_diario.TurnosAbiertosError, cierre_diario.DiaYaCerradoError) as exc:
        raise _rechazo_del_motor(exc) from exc
    diferencias = []
    for campo in ("monto_esperado_total", "monto_declarado_total", "diferencia_total"):
        if _distinto(cierre[campo], datos[campo]):
            diferencias.append(f"{campo} {float(cierre[campo]):.2f} en el central y {float(datos[campo]):.2f} en el nodo")
    if cierre["numero"] != datos["numero"]:
        # Cada base numera sus cierres: si el central ya cerraba días antes de tener nodo, el número difiere siempre.
        # Es información (el ticket del nodo dice otro número), no una pregunta para una persona: no se marca.
        logger.info("caja del nodo: el cierre %s del nodo %s es el número %s en el central (%s en el nodo)",
                    datos["cierre_id"], operacion.node_id, cierre["numero"], datos["numero"])
    if diferencias:
        _marcar(conn, "cierres_diarios", cierre["id"],
                f"el cierre {datos['cierre_id']} del nodo {operacion.node_id} no coincide: " + "; ".join(diferencias))


def aplicadores_de_caja() -> dict[str, Callable[[Any, Any], None]]:
    """`{operation_type: (conn, operacion) -> None}` de los cinco tipos de caja. `erp.offline.aplicadores` los suma a los
    de venta, así que `registrar_en` los registra todos en el mismo `Despachador`."""
    return {
        TIPO_TURNO_ABIERTO: aplicar_turno_abierto,
        TIPO_TURNO_CERRADO: aplicar_turno_cerrado,
        TIPO_MOVIMIENTO: aplicar_caja_movimiento,
        TIPO_MOVIMIENTO_ANULADO: aplicar_caja_movimiento_anulado,
        TIPO_DIA_CERRADO: aplicar_dia_cerrado,
    }
