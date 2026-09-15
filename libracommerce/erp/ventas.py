"""Ventas del punto de venta: la orquestación que cruza los dos motores (P9-M3).

Es lo que Contalibra (P7) y Restolibra (P8) tenían en `app/db_ventas.py` y en
las tres funciones de `app/db_turnos.py` que leen `sales` — el módulo más
entrelazado de las dos migraciones, y por eso el que más valía traer: una venta
escribe en la misma transacción

- **LibraCommerce**: `sales` (encabezado), `sale_items` (líneas) y
  `stock_movements` (el descuento, vía `erp.stock`, receta-aware por gancho);
- **LibraCore**: `ventas_pagos` (una línea por medio), `caja_movimientos` (un
  ingreso por pago **acreditado**), `turnos_caja` (la vinculación al turno
  abierto del cajero) y `cc_pagos` (la deuda del cliente, al anular una venta
  fiada).

Las dos copias de los productos se diffearon antes de unificarlas. Lo que
difería y cómo quedó:

- `_factura_display`: Contalibra imprime el **tipo legible** (`FACTURA C
  0005-00000011`), Restolibra el numérico (`11 0005-...`). Queda el legible, que
  Contalibra corrigió el 2026-08-20 y Restolibra nunca recibió.
- `vincular_cobros_de_venta`: Contalibra buscaba `Venta {numero} — %`,
  Restolibra `Venta {numero} %` porque el cobro de una mesa mete el pedido en el
  medio (`Venta V-00007 (pedido P-0009) — efectivo`). Queda el de Restolibra,
  que cubre los dos.
- `acreditar_pago_qr` cerraba el pedido del salón en Restolibra con un `UPDATE
  pedidos`. Eso es el gancho `al_confirmar_venta`: el motor lo llama cuando la
  venta llega a `cobrada` —al nacer cobrada o al acreditarse—, con la misma
  conexión, y el producto hace lo suyo.
- `get_venta` de Contalibra devolvía `status` (el crudo) y `factura_display`;
  el de Restolibra no. Quedan los dos: `venta_facturacion` decide por `status`
  y el detalle de la SPA muestra `factura_display`.

## Lo que este módulo NO decide

- **Ni abre conexiones ni commitea** — regla de la capa. La única excepción,
  dicha con su nombre, es `crear_venta_directa`, que recibe la *fábrica* de
  conexiones porque el reintento por número repetido necesita una transacción
  nueva por intento.
- **Dónde vive la tabla `venta_links`.** Los vínculos a dominios ajenos
  (`factura_id`, `remito_id`, `turno_id`, `mp_order_id`, `mp_payment_id`) no van
  en `sales`; hoy la tabla la declara cada producto en su `schema_propio`, con
  el mismo DDL. M5 la trae al motor; mientras tanto el consumidor la provee
  (ver `tests/conftest.py`, que es lo que hace un producto).
- **La FK de `ventas_pagos`.** El schema de LibraCore la crea contra `ventas`;
  los productos la repuntan a `sales` en su `init_db`. `repuntar_fk_ventas_pagos`
  es esa función traída acá para que el motor se pueda probar contra los dos
  motores de base; los productos la siguen llamando desde su `init_db` hasta M5.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager
from decimal import Decimal
from typing import Any

from .hooks import SIN_GANCHOS, Hooks
from .stock import add_movimiento_stock, descontar_stock_venta

logger = logging.getLogger(__name__)

#: Una fábrica de conexiones como la que pasa un producto: un context manager
#: que devuelve la conexión abierta (`libracore.db.core.get_connection`).
Conexion = Callable[[], AbstractContextManager[Any]]

#: Los estados de cobranza que ve la pantalla. `parcial` es estado de COBRANZA
#: (parcialmente pagada), no de la venta, que está confirmada igual: por eso no
#: entra en el enum del motor y se preserva en `sales.status_detail`. Mismo
#: mecanismo para `devuelta`/`devuelta_parcial` (`devolver_items`): la venta
#: sigue `confirmed`, la nuance vive en `status_detail` — que guarda un solo
#: valor a la vez y no compone ("cobrada" y "devuelta_parcial" no coexisten;
#: la más reciente pisa a la anterior).
ESTADOS = ("cobrada", "parcial", "pendiente", "anulada", "devuelta", "devuelta_parcial")
_ESTADO_A_STATUS = {
    "cobrada": "confirmed",
    "parcial": "confirmed",
    "pendiente": "draft",
    "anulada": "cancelled",
}
_STATUS_A_ESTADO = {"confirmed": "cobrada", "draft": "pendiente", "cancelled": "anulada"}

#: El prefijo con el que `registrar_venta` y `acreditar_pago_qr` nombran sus
#: movimientos de caja. Vive acá arriba para que las funciones que escriben el
#: concepto y la que lo busca (`vincular_cobros_de_venta`) se rompan juntas.
CONCEPTO_VENTA = "Venta {numero} — "

#: El patrón con el que se buscan esos movimientos. 🔑 `Venta {numero} %` y no
#: `Venta {numero} — %`: un producto puede meter algo entre el número y el
#: guion (Restolibra: `Venta V-00007 (pedido P-0009) — efectivo`), y el espacio
#: después del número evita que `V-0000` matchee a `V-00007`. Los números son
#: `V-%05d`, sin comodines de LIKE adentro.
PATRON_COBROS_DE_VENTA = "Venta {numero} %"

#: Cuántas veces se reintenta una venta cuyo número chocó con otra simultánea.
INTENTOS_POR_NUMERO = 10


class SinTurno(Exception):
    """No hay un turno de caja abierto y la opción `exigir_turno` lo exige.

    La venta no se llega a registrar: nada queda escrito a medio camino."""


class ProductoInexistente(ValueError):
    """Un ítem de la venta trae un `producto_id` que no existe en el catálogo
    (violación de FK en `sale_items.item_id`).

    No es un conflicto de otra venta simultánea —eso es `sales.number`—, así
    que `crear_venta_directa` no la reintenta: es un dato del pedido que nunca
    va a dejar de fallar, insistir 10 veces sólo demora el 422 que corresponde
    de entrada. Ver `_es_conflicto_de_numero`."""


def estado_de_row(status: str, status_detail: str | None) -> str:
    return status_detail or _STATUS_A_ESTADO.get(status, status)


def estado_segun_pagos(total: float, pagos: list[dict]) -> str:
    """`cobrada`, `parcial` o `pendiente` según lo **acreditado**, no la suma.

    Con la suma, una venta cuyo único pago espera el QR nacería cobrada —el
    defecto que el modelo de acreditación vino a cerrar en los dos productos—.
    """
    from libracore import pagos as acreditacion

    acreditado = acreditacion.acreditado(pagos)
    if acreditado >= Decimal(str(total)):
        return "cobrada"
    if acreditado > 0:
        return "parcial"
    return "pendiente"


# ── Escritura ────────────────────────────────────────────────────────────


def siguiente_numero(conn) -> str:
    """`V-00001`, `V-00002`, ... Calculado dentro de la transacción del caller
    (ya con el write-lock tomado) para no chocar con otro cobro concurrente."""
    row = conn.execute("SELECT number FROM sales ORDER BY id DESC LIMIT 1").fetchone()
    if row:
        try:
            n = int(row["number"].split("-")[-1]) + 1
        except (ValueError, IndexError):
            n = 1
    else:
        n = 1
    return f"V-{n:05d}"


def crear_venta(conn, *, numero: str, fecha: str, items: list, subtotal: float,
                descuento: float, total: float, cliente_id: int | None,
                cliente_nombre: str, usuario_id: int | None,
                observaciones: str = "", estado: str = "cobrada") -> int:
    """Inserta el encabezado en `sales` y las líneas en `sale_items`.

    Una línea con `producto_id` es de tipo 'product'; una ad-hoc (texto libre,
    sin producto del catálogo — el "Envío" de un delivery) es 'service' sin
    `item_id`: la misma regla que enforcean el dominio y el `CHECK` de
    `sale_items`.
    """
    cur = conn.execute(
        """INSERT INTO sales
           (number, occurred_on, status, status_detail, customer_party_id,
            customer_name_snapshot, created_by, notes, source_type,
            subtotal, discount_total, tax_total, total)
           VALUES (?,?,?,?,?,?,?,?,'pos',?,?,0,?)""",
        (numero, fecha, _ESTADO_A_STATUS.get(estado, "draft"), estado,
         cliente_id, cliente_nombre, usuario_id, observaciones,
         subtotal, descuento, total),
    )
    venta_id = cur.lastrowid
    for it in items:
        producto_id = it.get("producto_id")
        conn.execute(
            """INSERT INTO sale_items
               (sale_id, kind, item_id, variant_id, description_snapshot, quantity, unit_price)
               VALUES (?,?,?,?,?,?,?)""",
            (venta_id, "product" if producto_id else "service", producto_id,
             it.get("variante_id"), it.get("nombre", ""), it.get("qty", 0), it.get("precio", 0)),
        )
    return venta_id


def agregar_pago(conn, venta_id: int, medio: str, monto: float, referencia: str = "",
                 *, estado: str, recibido: float | None = None) -> None:
    """Una línea de pago de una venta.

    🔴 **`estado` es obligatorio y va por nombre.** La columna tiene default
    `'aprobado'` en la base —lo necesita el backfill de las filas viejas—, así
    que un `INSERT` que la omitiera contaría como plata que entró sin que nadie
    lo haya decidido. Acá el estado se declara, o no se escribe la fila. Los
    valores son los de `libracore.pagos.EstadoAcreditacion`.

    `recibido` es cuánto entregó el cliente (el vuelto sale de la resta con
    `monto`), y va a la columna `ventas_pagos.recibido` que agrega la
    migración `0010` de LibraCore. 🔴 **Con `recibido=None` el `INSERT` es
    carácter por carácter el de siempre** — Contalibra y Restolibra, que no
    mandan este campo y pueden no tener todavía la columna, no ven ninguna
    diferencia.
    """
    from libracore import pagos as acreditacion

    # Valida contra el vocabulario del motor antes de tocar la base: el error
    # dice qué estado se intentó poner, en vez del `CheckViolation` de psycopg.
    estado = acreditacion.estado_de({"estado": estado}).value
    if recibido is None:
        conn.execute(
            "INSERT INTO ventas_pagos (venta_id, medio, monto, referencia, estado) "
            "VALUES (?,?,?,?,?)",
            (venta_id, medio, monto, referencia, estado),
        )
    else:
        conn.execute(
            "INSERT INTO ventas_pagos (venta_id, medio, monto, referencia, estado, recibido) "
            "VALUES (?,?,?,?,?,?)",
            (venta_id, medio, monto, referencia, estado, recibido),
        )


def vincular_venta_turno(conn, venta_id: int, turno_id: int) -> None:
    """El turno de una venta vive en `venta_links`, no en `sales`."""
    conn.execute(
        """INSERT INTO venta_links (venta_id, turno_id) VALUES (?, ?)
           ON CONFLICT(venta_id) DO UPDATE SET turno_id=excluded.turno_id""",
        (venta_id, turno_id),
    )


def registrar_venta(conn, *, fecha: str, items: list, subtotal: float, descuento: float,
                    total: float, cliente_id: int | None, cliente_nombre: str,
                    usuario_id: int | None, observaciones: str, estado: str,
                    pagos: list[dict], stock_habilitado: bool,
                    hooks: Hooks = SIN_GANCHOS, exigir_turno: bool = False,
                    caja_con_turno: bool = False) -> int:
    """Una venta de mostrador completa, dentro de la transacción de `conn`:
    número, encabezado, líneas, pagos, caja, stock, turno y el gancho.

    🔴 **La caja se escribe al ACREDITAR, no al declarar.** Cada línea de
    `pagos` trae su `estado` (sin él, `agregar_pago` levanta): un pago
    `pendiente` —el del QR que todavía nadie escaneó— queda registrado como
    línea y **no toca la caja**. Lo acredita `acreditar_pago_qr` cuando
    MercadoPago dice que entró. Escribirlo acá inflaba el arqueo con plata que
    no entró, y el error aparecía horas después, al cerrar el turno.

    `exigir_turno=True` levanta `SinTurno` si `hooks.turno_para` no encuentra
    uno abierto para `usuario_id`, ANTES de escribir nada — default `False`
    (el comportamiento de hoy: una venta sin cajero con turno igual se
    registra, sólo que no queda atada a ninguno). `caja_con_turno=True` hace
    que los movimientos de caja de esta venta lleven el `turno_id`: sin esto
    (el default) el arqueo por turno que suma `caja_movimientos` da cero.

    No commitea: es del caller (`crear_venta_directa`, o el cobro de un pedido
    en Restolibra, que arma la venta con estas mismas piezas).
    """
    from libracore import medios_pago
    from libracore import pagos as acreditacion
    from libracore.db.caja import create_caja_movimiento

    turno = hooks.turno_para(conn, usuario_id)
    if exigir_turno and turno is None:
        raise SinTurno("No hay un turno de caja abierto: no se puede registrar la venta.")
    turno_id = turno["id"] if (caja_con_turno and turno) else None

    numero = hooks.numerador(conn)
    venta_id = crear_venta(
        conn, numero=numero, fecha=fecha, items=items, subtotal=subtotal,
        descuento=descuento, total=total, cliente_id=cliente_id,
        cliente_nombre=cliente_nombre, usuario_id=usuario_id,
        observaciones=observaciones, estado=estado,
    )
    for p in pagos:
        estado_del_pago = acreditacion.estado_de(p)
        agregar_pago(conn, venta_id, p["medio"], p["monto"], p.get("referencia", ""),
                     estado=estado_del_pago.value, recibido=p.get("recibido"))
        if estado_del_pago not in acreditacion.ACREDITAN:
            continue
        create_caja_movimiento(
            fecha=fecha, tipo="ingreso",
            concepto=CONCEPTO_VENTA.format(numero=numero) + medios_pago.label(p["medio"]),
            monto=p["monto"], referencia=p.get("referencia", ""),
            medio_pago=p["medio"], usuario_id=usuario_id, turno_id=turno_id, conn=conn,
        )

    if stock_habilitado:
        descontar_stock_venta(conn, venta_id, items, fecha=fecha,
                              usuario_id=usuario_id, hooks=hooks)

    if turno:
        vincular_venta_turno(conn, venta_id, turno["id"])

    if estado == "cobrada":
        hooks.al_confirmar_venta(conn, obtener_venta(conn, venta_id))
    return venta_id


#: Lo que nombra la violación de unicidad de `sales.number` en cada motor —la
#: ÚNICA que justifica reintentar, porque es la que puede desaparecer sola en
#: una transacción nueva (otra venta se quedó con ese número). SQLite nombra
#: la columna a secas (`UNIQUE constraint failed: sales.number`); lo que junta
#: la traducción de psycopg (`libracore/db/_postgres.py::_errores_como_sqlite3`)
#: es la clase de la excepción —ahí las dos jerarquías convergen en
#: `sqlite3.IntegrityError`, perdiendo el nombre concreto de PostgreSQL— y el
#: `str(e)` original, que sí conserva el nombre del constraint. Una columna
#: `UNIQUE` sin nombre explícito (como ésta) recibe el default de PostgreSQL:
#: `<tabla>_<columna>_key`.
_MARCAS_CONFLICTO_DE_NUMERO = ("sales.number", "sales_number_key")


def _es_conflicto_de_numero(exc: sqlite3.IntegrityError) -> bool:
    """Si esta `IntegrityError` es la unicidad de `sales.number` chocando con
    otra venta concurrente, y no otra violación de integridad (una FK, por
    ejemplo) que un reintento nunca va a curar."""
    msg = str(exc)
    return any(marca in msg for marca in _MARCAS_CONFLICTO_DE_NUMERO)


def _productos_faltantes(conexion: Conexion, items: list[dict]) -> list[int]:
    """Los `producto_id` de `items` que no existen en `catalog_items`.

    Se pregunta con una conexión NUEVA y no con la que acaba de fallar: en
    PostgreSQL esa transacción quedó abortada (cualquier `execute` sobre ella
    muere con *current transaction is aborted*), y en los dos motores el
    `rollback()` del caller ya la cerró de todos modos.
    """
    ids = sorted({it["producto_id"] for it in items if it.get("producto_id") is not None})
    if not ids:
        return []
    marcadores = ",".join("?" for _ in ids)
    with conexion() as conn:
        existentes = {
            r[0] for r in conn.execute(
                f"SELECT id FROM catalog_items WHERE id IN ({marcadores})", ids
            ).fetchall()
        }
    return [i for i in ids if i not in existentes]


def crear_venta_directa(conexion: Conexion, *, fecha: str, items: list, subtotal: float,
                        descuento: float, total: float, cliente_id: int | None,
                        cliente_nombre: str, usuario_id: int | None, observaciones: str,
                        estado: str, pagos: list[dict], stock_habilitado: bool,
                        hooks: Hooks = SIN_GANCHOS, exigir_turno: bool = False,
                        caja_con_turno: bool = False,
                        intentos: int = INTENTOS_POR_NUMERO) -> int:
    """`registrar_venta` con su transacción y el reintento por número repetido.

    Es la **única** función de la capa que recibe la fábrica de conexiones en
    vez de una conexión, y está dicho con su nombre: si dos ventas concurrentes
    chocan en el mismo número (`UNIQUE` en `sales.number`) hay que reintentar en
    una transacción **nueva** —en PostgreSQL la anterior quedó abortada—. Cada
    intento fallido reduce la contención en al menos uno (el que ganó ese round
    ya commiteó), así que la cantidad de reintentos está acotada por los
    submits realmente simultáneos: en la práctica 1, el doble click.

    El reintento no depende de qué numerador esté enganchado: `registrar_venta`
    vuelve a llamar a `hooks.numerador(conn)` en cada intento, así que un
    prefijo distinto de `V-` (VentaLibra: `POS-`) reintenta igual.
    """
    for intento in range(intentos):
        with conexion() as conn:
            try:
                venta_id = registrar_venta(
                    conn, fecha=fecha, items=items, subtotal=subtotal, descuento=descuento,
                    total=total, cliente_id=cliente_id, cliente_nombre=cliente_nombre,
                    usuario_id=usuario_id, observaciones=observaciones, estado=estado,
                    pagos=pagos, stock_habilitado=stock_habilitado, hooks=hooks,
                    exigir_turno=exigir_turno, caja_con_turno=caja_con_turno,
                )
                conn.commit()
                return venta_id
            except sqlite3.IntegrityError as e:
                conn.rollback()
                if _es_conflicto_de_numero(e):
                    if intento < intentos - 1:
                        continue
                    raise
                # No es el número: no se cura reintentando (una FK que no
                # existe sigue sin existir). El único caso que hoy puede
                # llegar acá es `sale_items.item_id` contra un producto
                # borrado o inventado — se identifica cuál, para que el 422
                # diga qué producto falta en vez de un 409 genérico.
                faltantes = _productos_faltantes(conexion, items)
                if faltantes:
                    raise ProductoInexistente(
                        "No existe el producto "
                        + ", ".join(str(i) for i in faltantes)
                        + ": revisá el catálogo antes de reintentar la venta."
                    ) from e
                raise
            except Exception:
                conn.rollback()
                raise
    raise RuntimeError("No se pudo generar un número de venta único")


# ── Lectura ──────────────────────────────────────────────────────────────

_VENTA_COLUMNAS = """
    s.id, s.number AS numero, s.occurred_on AS fecha, s.status, s.status_detail,
    s.customer_party_id AS cliente_id, s.customer_name_snapshot AS cliente_nombre,
    s.created_by AS usuario_id, s.notes AS observaciones, s.created_at,
    s.subtotal, s.discount_total AS descuento, s.total,
    vl.factura_id, vl.remito_id, vl.turno_id, vl.mp_order_id, vl.mp_payment_id
"""

_VENTA_FROM = """
    FROM sales s
    LEFT JOIN venta_links vl ON vl.venta_id = s.id
"""


def _venta_dict(row, items: list, pagos: list) -> dict:
    return {
        "id": row["id"], "numero": row["numero"], "fecha": row["fecha"],
        "cliente_id": row["cliente_id"], "cliente_nombre": row["cliente_nombre"],
        "items": items,
        "subtotal": float(row["subtotal"]), "descuento": float(row["descuento"]),
        "total": float(row["total"]),
        "estado": estado_de_row(row["status"], row["status_detail"]),
        # El crudo además del legible: `estado` puede venir pisado por
        # `status_detail`, así que no sirve para decidir. Lo necesita la
        # facturación, que no debe emitir sobre una venta anulada.
        "status": row["status"],
        "factura_id": row["factura_id"], "remito_id": row["remito_id"],
        "usuario_id": row["usuario_id"], "observaciones": row["observaciones"],
        "created_at": row["created_at"], "turno_id": row["turno_id"],
        "mp_order_id": row["mp_order_id"] or "", "mp_payment_id": row["mp_payment_id"] or "",
        "pagos": pagos,
    }


def factura_display(tipo, punto_venta, numero) -> str | None:
    """El comprobante como lo lee una persona: `FACTURA C 0005-00000011`.

    Los labels salen de LibraCore y no de una tabla nueva acá: Contalibra ya
    tenía cuatro copias del mismo diccionario cuando lo corrigió.
    """
    if not tipo or not numero:
        return None
    # Adentro para no arrastrar la pila del PDF en cada consulta de ventas.
    from libracore.pdf_generator import _TIPO_LABELS

    etiqueta = _TIPO_LABELS.get(tipo, str(tipo))
    return f"{etiqueta} {str(punto_venta or 0).zfill(4)}-{str(numero).zfill(8)}"


def _items_de(conn, venta_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT id, item_id, variant_id, description_snapshot, quantity, unit_price
           FROM sale_items WHERE sale_id=? ORDER BY id""",
        (venta_id,),
    ).fetchall()
    return [
        {
            # El id de `sale_items`: sin él, ningún cliente de la API puede
            # armar el payload de `POST /{vid}/devolver` (que pide
            # `sale_item_id`) sin leer la base directamente.
            "id": r["id"],
            "producto_id": r["item_id"], "variante_id": r["variant_id"],
            "nombre": r["description_snapshot"],
            "qty": float(r["quantity"]), "precio": float(r["unit_price"]),
            "subtotal": round(float(r["quantity"]) * float(r["unit_price"]), 2),
        }
        for r in rows
    ]


def listar_ventas(conn, *, desde: str = "", hasta: str = "", q: str = "",
                  tab: str = "todas", limit: int = 100, offset: int = 0) -> list[dict]:
    """El listado del POS. `tab` es `todas`, `sin_facturar` o `facturadas`.

    La búsqueda no distingue mayúsculas en ningún motor: con `LIKE` a secas
    PostgreSQL sí las distingue y `v-0001` no encontraba `V-00001` (hallazgo
    de M2 sobre el autocompletado, cerrado acá para todo el módulo).
    """
    where, params = [], []
    if desde:
        where.append("s.occurred_on >= ?")
        params.append(desde)
    if hasta:
        where.append("s.occurred_on <= ?")
        params.append(hasta)
    if q:
        where.append("(LOWER(s.number) LIKE ? OR LOWER(s.customer_name_snapshot) LIKE ?)")
        params += [f"%{q.lower()}%", f"%{q.lower()}%"]
    if tab == "sin_facturar":
        where.append("vl.factura_id IS NULL AND s.status != 'cancelled'")
    elif tab == "facturadas":
        where.append("vl.factura_id IS NOT NULL")
    sql = (
        "SELECT " + _VENTA_COLUMNAS
        + ", f.tipo AS fac_tipo, f.punto_venta AS fac_pv, f.numero AS fac_numero"
        + _VENTA_FROM
        + " LEFT JOIN facturas f ON f.id = vl.factura_id"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY s.occurred_on DESC, s.id DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    rows = conn.execute(sql, params).fetchall()

    result = []
    for r in rows:
        pagos = [
            {"medio": p["medio"], "monto": float(p["monto"])}
            for p in conn.execute(
                "SELECT medio, monto FROM ventas_pagos WHERE venta_id=? ORDER BY id",
                (r["id"],),
            ).fetchall()
        ]
        d = _venta_dict(r, _items_de(conn, r["id"]), pagos)
        # Los tres alias del JOIN se preservan: no están en el contrato de la
        # SPA, pero la forma del dict la consumía algún caller.
        d["fac_tipo"] = r["fac_tipo"]
        d["fac_pv"] = r["fac_pv"]
        d["fac_numero"] = r["fac_numero"]
        d["factura_display"] = factura_display(r["fac_tipo"], r["fac_pv"], r["fac_numero"])
        result.append(d)
    return result


def obtener_venta(conn, vid: int) -> dict | None:
    row = conn.execute(
        "SELECT " + _VENTA_COLUMNAS + _VENTA_FROM + " WHERE s.id=?", (vid,)
    ).fetchone()
    if not row:
        return None
    pagos = [
        dict(p) for p in conn.execute(
            "SELECT * FROM ventas_pagos WHERE venta_id=? ORDER BY id", (vid,)
        ).fetchall()
    ]
    d = _venta_dict(row, _items_de(conn, vid), pagos)
    # El detalle también nombra el comprobante: cuando sólo lo armaba el
    # listado, el bloque "Factura generada" del detalle era código muerto.
    fac = conn.execute(
        "SELECT tipo, punto_venta, numero FROM facturas WHERE id=?",
        (d["factura_id"],),
    ).fetchone() if d["factura_id"] else None
    d["factura_display"] = factura_display(
        fac["tipo"], fac["punto_venta"], fac["numero"]
    ) if fac else None
    return d


def obtener_venta_por_orden_mp(conn, mp_order_id: str) -> dict | None:
    row = conn.execute(
        "SELECT " + _VENTA_COLUMNAS + _VENTA_FROM + " WHERE vl.mp_order_id=?",
        (mp_order_id,),
    ).fetchone()
    if not row:
        return None
    return _venta_dict(row, _items_de(conn, row["id"]), [])


# ── Anulación y devolución ─────────────────────────────────────────────────

#: Un movimiento que descontó stock por ESTA venta, sea por el camino nuevo
#: (`erp.stock.descontar_stock_venta`, `reason_code='venta'`) o por el viejo
#: `libracommerce.usecases.sales.confirm_sale` que usa VentaLibra hoy
#: (`reason_code` NULL, `source_type='sale'`, `movement_type='sale'`). Las dos
#: formas son mutuamente excluyentes por `reason_code`/`movement_type`: una
#: fila nunca matchea las dos, así que nunca se cuenta dos veces.
_COND_VENDIDO = (
    "reason_code='venta' "
    "OR (reason_code IS NULL AND source_type='sale' AND movement_type='sale')"
)
#: Un movimiento que YA repuso stock de esta venta por una devolución PARCIAL
#: —no por la anulación entera—: el nuevo `devolver_items`
#: (`reason_code='devolucion'`) o el viejo
#: `libracommerce.usecases.sales.return_sale_items`
#: (`source_type='sale_return'`, `movement_type='return'`).
_COND_DEVUELTO = (
    "reason_code='devolucion' "
    "OR (source_type='sale_return' AND movement_type='return')"
)


def _movimientos_de_venta(conn, vid: int, condicion_sql: str) -> list:
    return conn.execute(
        f"SELECT item_id, variant_id, location_id, quantity_delta FROM stock_movements "
        f"WHERE source_id=? AND ({condicion_sql})",
        (vid,),
    ).fetchall()


def _acumular(movimientos, *, con_deposito: bool) -> dict[tuple, float]:
    """Suma `quantity_delta` por (item, variante[, depósito])."""
    acumulado: dict[tuple, float] = {}
    for m in movimientos:
        clave = ((m["item_id"], m["variant_id"], m["location_id"]) if con_deposito
                 else (m["item_id"], m["variant_id"]))
        acumulado[clave] = acumulado.get(clave, 0.0) + float(m["quantity_delta"])
    return acumulado


class VentaConDevoluciones(ValueError):
    """La venta ya tuvo una devolución —total o parcial, por el camino nuevo
    (`devolver_items`, que lo marca en `status_detail`) o el viejo
    (`usecases.sales.return_sale_items`, que deja `sales.status` en
    `partially_returned`/`returned`)—: no se puede anular.

    Mismo criterio que el motor viejo: `usecases.sales.cancel_sale` sólo
    anula una venta `CONFIRMED` y levanta para cualquier otro estado. Acá se
    replica esa regla en vez de intentar "reconciliar" el dinero de una
    anulación contra el de una devolución previa —son dos reversiones del
    mismo pago, y sumarlas devuelve más plata de la que entró.
    """


def anular_venta(conn, vid: int, usuario_id: int | None = None,
                 hooks: Hooks = SIN_GANCHOS, *, caja_con_turno: bool = False) -> bool:
    """Anula una venta: repone el stock que se había descontado (los insumos de
    la receta si los hubo — el ledger ya guardó qué se descontó de verdad, así
    que la reversión es simétrica sin volver a resolver nada), revierte con un
    egreso cada movimiento de caja de sus pagos **acreditados** y, si tenía un
    pago a cuenta corriente, acredita la deuda del cliente
    (`hooks.cliente_cc_de`). Después llama a `hooks.al_anular_venta` con la
    misma conexión.

    🔴 **Sólo se revierte lo que entró de verdad.** `libracore.pagos` decide
    qué pago cuenta (`ACREDITAN`, hoy sólo `aprobado`) — igual que
    `registrar_venta` decide qué pago escribe caja al declarar. Un pago
    `pendiente` (el QR que nadie escaneó) nunca escribió un ingreso, así que
    revertirlo sacaría de la caja plata que nunca entró. Ese pago pasa a
    `vencido`: es el estado terminal de "se declaró y no llegó a acreditarse"
    (`EstadoAcreditacion.VENCIDO`, ya usado por LibraClub para el jugador que
    nunca volvió de MercadoPago) — ni `aprobado` (no entró) ni `rechazado`
    (MercadoPago no lo rechazó, la venta se cerró antes de que se resolviera).
    Al quedar en un estado que no es `pendiente`, `acreditar_pago_qr` no lo
    vuelve a tocar si el cliente paga después: ver esa función.

    🔴 **Una venta con devoluciones no se anula** — levanta
    `VentaConDevoluciones` (ver esa clase). El chequeo mira `sales.status`
    (el camino viejo), `status_detail` (el nuevo) y el ledger mismo
    (`_COND_DEVUELTO`, por si una fila vieja quedó con el estado
    desalineado), y va ANTES de tocar stock, caja o estado: nada queda a
    medio camino.

    🔑 **Tolerante a las filas viejas de VentaLibra** (ver `_COND_VENDIDO`):
    busca lo vendido por las dos formas del ledger, nueva y vieja, sin que una
    fila cuente dos veces. Repone **movimiento por movimiento, sin agrupar**
    —igual que el código de antes de esta capa—: dos platos que descuentan el
    mismo insumo (Restolibra, vía `resolver_receta`) siguen generando DOS
    filas de reposición, no una sola sumada. Con la guarda de arriba esto en
    la práctica repone siempre el 100% de lo vendido —no hay devoluciones que
    restar—, pero se deja así (y no agrupado) para no cambiarle a Contalibra
    ni a Restolibra la forma del ledger que ya ven sus pantallas de
    movimientos. Nunca hace `UPDATE` sobre `stock_movements`: el ledger es
    aditivo, siempre.

    Devuelve `False` si ya estaba anulada —no-op, para no revertir dos veces
    si se reintenta la acción—. Levanta `ValueError` si no existe. No
    commitea.
    """
    from libracore import pagos as acreditacion
    from libracore.db.core import _ar_now
    from libracore.db.reversiones import revertir_cobro_venta

    venta = obtener_venta(conn, vid)
    if not venta:
        raise ValueError("Venta inexistente")
    if venta["status"] == "cancelled":
        return False

    if (
        venta["status"] in ("returned", "partially_returned")
        or venta["estado"] in ("devuelta", "devuelta_parcial")
        or _movimientos_de_venta(conn, vid, _COND_DEVUELTO)
    ):
        raise VentaConDevoluciones(
            "La venta tiene devoluciones: no se puede anular; devolvé el "
            "resto de las líneas."
        )

    fecha = _ar_now().split(" ")[0]

    for m in _movimientos_de_venta(conn, vid, _COND_VENDIDO):
        add_movimiento_stock(
            conn, producto_id=m["item_id"], tipo="anulacion",
            cantidad=-m["quantity_delta"], referencia=f"Anulación venta ID {vid}",
            venta_id=vid, usuario_id=usuario_id, fecha=fecha,
            deposito_id=m["location_id"], variant_id=m["variant_id"],
        )

    pagos = [
        dict(p) for p in conn.execute(
            "SELECT id, medio, monto, estado FROM ventas_pagos WHERE venta_id=?", (vid,)
        ).fetchall()
    ]
    acreditados = [p for p in pagos if acreditacion.estado_de(p) in acreditacion.ACREDITAN]
    pendientes_ids = [
        p["id"] for p in pagos
        if acreditacion.estado_de(p) is acreditacion.EstadoAcreditacion.PENDIENTE
    ]
    if pendientes_ids:
        marcadores = ",".join("?" for _ in pendientes_ids)
        conn.execute(
            f"UPDATE ventas_pagos SET estado=? WHERE id IN ({marcadores})",
            (acreditacion.EstadoAcreditacion.VENCIDO.value, *pendientes_ids),
        )

    turno = hooks.turno_para(conn, usuario_id) if caja_con_turno else None
    revertir_cobro_venta(
        venta_id=vid, numero=venta["numero"], fecha=fecha, pagos=acreditados,
        cliente_id=hooks.cliente_cc_de(conn, venta), usuario_id=usuario_id, conn=conn,
        turno_id=(turno["id"] if turno else None),
    )

    conn.execute(
        "UPDATE sales SET status='cancelled', status_detail='anulada' WHERE id=?", (vid,)
    )
    hooks.al_anular_venta(conn, obtener_venta(conn, vid))
    return True


def _referencia_devolucion(vid: int, devoluciones: dict[int, float]) -> str:
    """Idempotente por el detalle de líneas: dos devoluciones distintas de la
    misma venta son dos reintegros, pero repetir la misma no paga dos veces."""
    detalle = ",".join(f"{i}x{c}" for i, c in sorted(devoluciones.items()))
    return f"devolucion:venta:{vid}:{detalle}"


#: Los `estado` de venta que admiten una devolución (nueva o siguiente
#: parcial). `partially_returned` es el `sales.status` CRUDO del camino
#: viejo —no pasa por `status_detail`, así que no aparece nunca en `estado`—
#: y por eso se chequea aparte, contra `venta["status"]`.
_ESTADOS_QUE_ADMITEN_DEVOLUCION = ("cobrada", "devuelta_parcial")


def devolver_items(conn, vid: int, devoluciones: dict[int, float], deposito_id: int,
                   medio_pago: str = "efectivo", usuario_id: int | None = None,
                   hooks: Hooks = SIN_GANCHOS, *, caja_con_turno: bool = False) -> dict:
    """Devuelve algunas líneas de una venta confirmada y reintegra su importe.

    `devoluciones` mapea el **id de `sale_items`** (no la posición: acá, a
    diferencia del `Sale` del dominio, la línea sí tiene un id estable) a la
    cantidad que vuelve. Una línea de servicio no se devuelve: anular la
    venta entera.

    🔑 **El tope es por (ítem, variante), no por línea**: `disponible` se
    calcula sumando la cantidad vendida de TODAS las líneas con esa clave,
    restando lo ya devuelto (`_COND_DEVUELTO`, nuevo o viejo) y lo que ya se
    pidió para esa clave DENTRO de esta misma llamada. Comparar contra la
    cantidad de una sola línea era el bug: con dos líneas del mismo ítem
    (A=2, B=3), devolver 3 de B dejaba a A con `disponible=-1` aunque A
    seguía intacta; y sin descontar lo pedido en la misma llamada, dos líneas
    iguales pedidas juntas se comparaban cada una contra el mismo acumulado
    y podían sumar más que lo vendido.

    Sólo se puede devolver una venta **cobrada** (`estado == 'cobrada'`) o ya
    parcialmente devuelta (`'devuelta_parcial'`), o una vieja con
    `sales.status == 'partially_returned'`. Una venta `pendiente` o
    `parcial` (de cobranza, no de devolución) todavía no tiene toda la plata
    adentro: reintegrar algo ahí devolvería dinero que nunca entró.

    Repone stock con `tipo="devolucion"` (nunca `UPDATE`, el ledger es
    aditivo) y reintegra el importe proporcional con
    `libracore.db.reversiones.reintegrar_devolucion`, por el medio de pago que
    se indique —no tiene por qué ser el que cobró la venta—. Si el reintegro
    es a cuenta corriente, `hooks.cliente_cc_de` tiene que resolver un
    cliente, o levanta `ValueError`.

    Marca `sales.status_detail` en `devuelta` (se devolvió todo) o
    `devuelta_parcial`; `status` queda en `confirmed`, igual que hoy con
    `parcial`/`cobrada` — es la misma idea de nuance, ver `ESTADOS`. No
    commitea.

    🔴 **No es receta-aware**: repone el ítem vendido, nunca sus insumos. Es
    una limitación real para un producto con `resolver_receta` (Restolibra);
    no lo pidió esta fase, que es la de VentaLibra, sin recetas.
    """
    from libracore.db.caja import MEDIO_CUENTA_CORRIENTE
    from libracore.db.core import _ar_now
    from libracore.db.reversiones import reintegrar_devolucion

    venta = obtener_venta(conn, vid)
    if not venta:
        raise ValueError("Venta inexistente")
    if venta["status"] == "cancelled":
        raise ValueError("La venta está anulada: no se le puede devolver nada")
    if not (
        venta["estado"] in _ESTADOS_QUE_ADMITEN_DEVOLUCION
        or venta["status"] == "partially_returned"
    ):
        raise ValueError(
            f"no se puede devolver una venta en estado '{venta['estado']}': "
            "sólo una venta cobrada (o ya parcialmente devuelta) admite devoluciones"
        )
    if not devoluciones:
        raise ValueError("No se indicó ninguna línea a devolver")

    lineas = {
        r["id"]: r for r in conn.execute(
            "SELECT id, kind, item_id, variant_id, quantity, unit_price "
            "FROM sale_items WHERE sale_id=?", (vid,),
        ).fetchall()
    }
    # Vendido por clave: la SUMA de todas las líneas con ese (ítem, variante),
    # no la de una sola — dos líneas del mismo producto comparten el pozo.
    vendido_por_clave: dict[tuple, float] = {}
    for li in lineas.values():
        if li["kind"] == "product" and li["item_id"] is not None:
            clave = (li["item_id"], li["variant_id"])
            vendido_por_clave[clave] = vendido_por_clave.get(clave, 0.0) + float(li["quantity"])
    ya_devuelto = _acumular(_movimientos_de_venta(conn, vid, _COND_DEVUELTO), con_deposito=False)
    fecha = _ar_now().split(" ")[0]

    pedido_por_clave: dict[tuple, float] = {}
    importe = 0.0
    for sale_item_id, cantidad in devoluciones.items():
        cantidad = float(cantidad)
        if cantidad <= 0:
            raise ValueError("la cantidad a devolver debe ser mayor que cero")
        linea = lineas.get(sale_item_id)
        if linea is None:
            raise ValueError(f"la venta no tiene la línea {sale_item_id}")
        if linea["kind"] != "product" or linea["item_id"] is None:
            raise ValueError(
                f"no se puede devolver una línea de servicio ({sale_item_id}): "
                "anular la venta entera"
            )
        clave = (linea["item_id"], linea["variant_id"])
        disponible = round(
            vendido_por_clave.get(clave, 0.0)
            - ya_devuelto.get(clave, 0.0)
            - pedido_por_clave.get(clave, 0.0),
            6,
        )
        if cantidad > disponible:
            raise ValueError(
                f"no se puede devolver {cantidad} de la línea {sale_item_id}: "
                f"quedan {disponible} sin devolver"
            )
        pedido_por_clave[clave] = pedido_por_clave.get(clave, 0.0) + cantidad
        add_movimiento_stock(
            conn, producto_id=linea["item_id"], tipo="devolucion", cantidad=cantidad,
            referencia=f"Devolución venta ID {vid}", venta_id=vid, usuario_id=usuario_id,
            fecha=fecha, deposito_id=deposito_id, variant_id=linea["variant_id"],
        )
        importe += cantidad * float(linea["unit_price"])

    importe = round(importe, 2)
    cliente_id = hooks.cliente_cc_de(conn, venta)
    if medio_pago == MEDIO_CUENTA_CORRIENTE and cliente_id is None:
        raise ValueError(
            "para devolver a cuenta corriente la venta tiene que tener cliente"
        )

    turno = hooks.turno_para(conn, usuario_id) if caja_con_turno else None
    reintegrar_devolucion(
        venta_id=vid, numero=venta["numero"], fecha=fecha, monto=importe,
        medio_pago=medio_pago, referencia=_referencia_devolucion(vid, devoluciones),
        cliente_id=cliente_id, usuario_id=usuario_id,
        turno_id=(turno["id"] if turno else None), conn=conn,
    )

    vendido_total = sum(float(li["quantity"]) for li in lineas.values() if li["kind"] == "product")
    devuelto_total = sum(ya_devuelto.values()) + sum(float(c) for c in devoluciones.values())
    status_detail = "devuelta" if devuelto_total >= vendido_total else "devuelta_parcial"
    conn.execute("UPDATE sales SET status_detail=? WHERE id=?", (status_detail, vid))

    return {"importe": importe, "venta": obtener_venta(conn, vid)}


# ── Links a otros contextos (facturación, remitos, MercadoPago) ───────────


def _upsert_link(conn, vid: int, campo: str, valor) -> None:
    conn.execute(
        f"""INSERT INTO venta_links (venta_id, {campo}) VALUES (?, ?)
            ON CONFLICT(venta_id) DO UPDATE SET {campo}=excluded.{campo}""",
        (vid, valor),
    )


def vincular_factura(conn, vid: int, factura_id: int) -> None:
    _upsert_link(conn, vid, "factura_id", factura_id)


def vincular_remito(conn, vid: int, remito_id: int) -> None:
    _upsert_link(conn, vid, "remito_id", remito_id)


def set_orden_mp(conn, vid: int, mp_order_id: str) -> None:
    _upsert_link(conn, vid, "mp_order_id", mp_order_id)


def set_pago_mp(conn, vid: int, mp_payment_id: str) -> None:
    _upsert_link(conn, vid, "mp_payment_id", mp_payment_id)


def sellar_referencia_mp(conn, vid: int, payment_id: str) -> None:
    """Pone `MP#<payment_id>` como referencia de los pagos electrónicos de la
    venta que no tenían ninguna. El criterio de "electrónico" es del motor."""
    from libracore import medios_pago

    conn.execute(
        f"""UPDATE ventas_pagos SET referencia=?
           WHERE venta_id=? AND {medios_pago.sql_es_electronico("medio")}
           AND (referencia IS NULL OR referencia='')""",
        (f"MP#{payment_id}", vid),
    )


def vincular_cobros_de_venta(conn, numero: str, factura_id: int) -> int:
    """Marca los movimientos de caja de una venta como el cobro de su factura.

    Una factura figura cobrada cuando existen `caja_movimientos` con su
    `factura_id`. Los de una venta se crean **antes** de que la factura exista
    y sin ese campo, así que Comprobantes mostraba "Sin cobrar" una factura cuya
    plata ya estaba en la caja (Contalibra, 2026-08-20, con 8 así).

    🔴 **No registra un cobro nuevo, vincula el que ya está.** Registrarlo de
    nuevo contaría el ingreso dos veces. Devuelve cuántos vinculó; cero es un
    resultado válido (venta en cuenta corriente, o cobrada por QR y todavía sin
    acreditar).
    """
    cur = conn.execute(
        "UPDATE caja_movimientos SET factura_id=? "
        "WHERE factura_id IS NULL AND tipo='ingreso' AND concepto LIKE ?",
        (factura_id, PATRON_COBROS_DE_VENTA.format(numero=numero)),
    )
    return cur.rowcount


# ── Acreditación del QR ──────────────────────────────────────────────────


def acreditar_pago_qr(conn, venta_id: int, payment_id: str,
                      usuario_id: int | None = None,
                      hooks: Hooks = SIN_GANCHOS, *,
                      caja_con_turno: bool = False) -> bool:
    """La plata del QR entró: se acreditan los pagos pendientes de la venta.

    Es el punto de acreditación, la contracara de que `registrar_venta` no
    escriba la caja al declarar. En la transacción de `conn`: pasa a `APROBADO`
    los pagos pendientes, escribe **ahora** el movimiento de caja de cada uno,
    recalcula el estado de la venta con lo acreditado —con TODOS los pagos: una
    venta mitad efectivo y mitad QR pasa a `cobrada` recién con la segunda
    mitad— y, si quedó cobrada, llama a `hooks.al_confirmar_venta` (Restolibra
    cierra ahí el pedido del salón que esperaba el pago).

    Con `caja_con_turno=True` el movimiento de caja lleva el `turno_id` que
    `venta_links` guardó **al registrar la venta** —no el que esté abierto
    ahora, que puede ser otro: el QR se puede acreditar recién en el turno
    siguiente, y la plata es del turno que hizo la venta, no del que la
    encuentra pagada.

    🔴 **Una venta anulada no revive.** Si `venta.status == 'cancelled'` no
    acredita nada, no toca la caja ni el estado de la venta, y devuelve
    `False` — aunque el pago siga técnicamente `pendiente` (una fila vieja,
    de antes de que `anular_venta` empezara a marcarlo `vencido`). Se deja un
    `logger.warning` con el `venta_id` y el `payment_id`: esa plata sí entró
    en MercadoPago y a alguien hay que avisarle para que la devuelva a mano.

    🔑 **Idempotente por la condición, no por un flag**: sólo toca lo que está
    `PENDIENTE`. El poll de `mp-status` y el webhook pueden llegar los dos, en
    cualquier orden; la segunda pasada no encuentra nada y no escribe nada.
    Devuelve `True` si acreditó algo.
    """
    from libracore import medios_pago
    from libracore import pagos as acreditacion
    from libracore.db.caja import create_caja_movimiento

    venta = conn.execute(
        "SELECT number AS numero, occurred_on AS fecha, total, status FROM sales WHERE id=?",
        (venta_id,),
    ).fetchone()
    if venta is None:
        return False
    if venta["status"] == "cancelled":
        logger.warning(
            "acreditar_pago_qr: venta %s ya está anulada, no se acredita el pago "
            "%s (la plata entró en MercadoPago: hay que devolverla a mano)",
            venta_id, payment_id,
        )
        return False

    pendientes = conn.execute(
        "SELECT id, medio, monto FROM ventas_pagos "
        "WHERE venta_id=? AND estado=? ORDER BY id",
        (venta_id, acreditacion.EstadoAcreditacion.PENDIENTE.value),
    ).fetchall()
    if not pendientes:
        return False

    turno_id = None
    if caja_con_turno:
        fila_turno = conn.execute(
            "SELECT turno_id FROM venta_links WHERE venta_id=?", (venta_id,)
        ).fetchone()
        turno_id = fila_turno["turno_id"] if fila_turno else None

    for p in pendientes:
        # 🔴 La referencia sólo se pone si estaba vacía — mismo criterio que
        # `sellar_referencia_mp`. Antes pisaba SIEMPRE con `MP#<payment_id>`,
        # borrando una referencia que el mostrador ya hubiera cargado a mano
        # sobre ese pago (declarado `cobrar_con_qr` y con una referencia
        # propia). El estado pasa a aprobado igual.
        conn.execute(
            "UPDATE ventas_pagos SET estado=?, "
            "referencia=CASE WHEN referencia IS NULL OR referencia='' THEN ? ELSE referencia END "
            "WHERE id=?",
            (acreditacion.EstadoAcreditacion.APROBADO.value, f"MP#{payment_id}", p["id"]),
        )
        create_caja_movimiento(
            fecha=venta["fecha"], tipo="ingreso",
            concepto=CONCEPTO_VENTA.format(numero=venta["numero"]) + medios_pago.label(p["medio"]),
            monto=p["monto"], referencia=f"MP#{payment_id}",
            medio_pago=p["medio"], usuario_id=usuario_id, turno_id=turno_id, conn=conn,
        )

    todos = conn.execute(
        "SELECT monto, estado FROM ventas_pagos WHERE venta_id=?", (venta_id,)
    ).fetchall()
    nuevo = estado_segun_pagos(
        float(venta["total"]), [{"monto": r["monto"], "estado": r["estado"]} for r in todos]
    )
    conn.execute(
        "UPDATE sales SET status=?, status_detail=? WHERE id=?",
        (_ESTADO_A_STATUS[nuevo], nuevo, venta_id),
    )
    if nuevo == "cobrada":
        hooks.al_confirmar_venta(conn, obtener_venta(conn, venta_id))
    return True


# ── Turnos: lo que del turno de caja depende de dónde viven las ventas ───


def resumen_turno(conn, tid: int) -> dict:
    """Ventas y totales por medio de pago del turno.

    El dominio de turnos es de LibraCore; estas dos funciones se redefinen acá
    porque las de allá leen la tabla `ventas` vieja, y el arqueo daría mal.
    """
    ventas = conn.execute(
        """SELECT s.id, s.number AS numero, s.occurred_on AS fecha,
                  s.customer_name_snapshot AS cliente_nombre, s.total,
                  COALESCE(s.status_detail, s.status) AS estado
           FROM sales s
           JOIN venta_links vl ON vl.venta_id = s.id
           WHERE vl.turno_id=? ORDER BY s.id""",
        (tid,),
    ).fetchall()
    pagos = conn.execute(
        """SELECT vp.medio, SUM(vp.monto) AS total
           FROM ventas_pagos vp
           JOIN sales s ON s.id = vp.venta_id
           JOIN venta_links vl ON vl.venta_id = s.id
           WHERE vl.turno_id=? AND COALESCE(s.status_detail, s.status)='cobrada'
           GROUP BY vp.medio""",
        (tid,),
    ).fetchall()
    return {
        "ventas": [dict(v) for v in ventas],
        "pagos_por_medio": {r["medio"]: r["total"] for r in pagos},
        "total_ventas": sum(r["total"] for r in pagos),
        "efectivo_ventas": next((r["total"] for r in pagos if r["medio"] == "efectivo"), 0.0),
    }


def cerrar_turno(conn, tid: int, monto_declarado: float, notas: str = "") -> bool:
    """Cierra el turno arqueando contra `resumen_turno`. `False` si no existe."""
    from libracore.db.core import _ar_now

    turno = conn.execute("SELECT monto_inicial FROM turnos_caja WHERE id=?", (tid,)).fetchone()
    if not turno:
        return False
    resumen = resumen_turno(conn, tid)
    monto_esperado = round(turno["monto_inicial"] + resumen["efectivo_ventas"], 2)
    conn.execute(
        """UPDATE turnos_caja
           SET estado='cerrado', cierre=?, monto_declarado_cierre=?,
               monto_esperado_cierre=?, notas=?
           WHERE id=?""",
        (_ar_now(), monto_declarado, monto_esperado, notas, tid),
    )
    return True


# ── El schema que este módulo necesita y no declara ──────────────────────


def _pagos_huerfanos(conn) -> int:
    """Filas de `ventas_pagos` sin su venta en `sales`. NO se descartan: son
    registros de dinero. Se avisa, porque quedan como referencias colgadas y eso
    tiene que ser una decisión de alguien, no un efecto silencioso."""
    huerfanas = conn.execute(
        "SELECT COUNT(*) FROM ventas_pagos vp LEFT JOIN sales s ON s.id = vp.venta_id "
        "WHERE s.id IS NULL"
    ).fetchone()[0]
    if huerfanas:
        print(
            f"[ADVERTENCIA] ventas_pagos: {huerfanas} fila(s) referencian una venta que "
            "no está en `sales`. Se conservan tal cual, pero quedan como referencias "
            "colgadas: revisar a mano.",
            flush=True,
        )
    return huerfanas


def repuntar_fk_ventas_pagos(conn) -> bool:
    """Repunta la FK de `ventas_pagos.venta_id` de `ventas(id)` (el schema de
    LibraCore) a `sales(id)` (LibraCommerce), que es donde viven las ventas de
    quien usa este módulo. Idempotente: si ya apunta a `sales` no hace nada, y
    devuelve si tocó algo.

    Contra PostgreSQL son dos `ALTER TABLE`. Contra SQLite es el rebuild de
    siempre —RENAME, CREATE, copiar, DROP—, con el DDL tomado de
    `sqlite_master` y **todas** las columnas: la versión de los productos
    recreaba la tabla con una lista fija y perdía `estado`, la columna que
    LibraCore agregó después. Ninguna tabla cuelga de `ventas_pagos`, así que el
    RENAME no reescribe FKs ajenas.
    """
    if not isinstance(conn, sqlite3.Connection):
        definiciones = conn.execute("""
            SELECT conname, pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conrelid = 'ventas_pagos'::regclass AND contype = 'f'
        """).fetchall()
        if any("REFERENCES sales(" in d[1] for d in definiciones):
            return False
        huerfanas = _pagos_huerfanos(conn)
        for nombre, definicion in definiciones:
            if "venta_id" in definicion:
                conn.execute(f"ALTER TABLE ventas_pagos DROP CONSTRAINT {nombre}")
        sufijo = " NOT VALID" if huerfanas else ""
        conn.execute(
            "ALTER TABLE ventas_pagos ADD CONSTRAINT ventas_pagos_venta_id_fkey "
            f"FOREIGN KEY (venta_id) REFERENCES sales(id) ON DELETE CASCADE{sufijo}"
        )
        conn.commit()
        return True

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='ventas_pagos'"
    ).fetchone()
    if not row or "REFERENCES sales(" in row[0]:
        return False
    _pagos_huerfanos(conn)
    ddl = re.sub(r"REFERENCES\s+ventas\s*\(", "REFERENCES sales(", row[0])
    # El pragma es por conexión y no se puede tocar dentro de una transacción:
    # se apaga, se reconstruye y se vuelve a encender.
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("ALTER TABLE ventas_pagos RENAME TO ventas_pagos_old")
        conn.execute(ddl)
        conn.execute("INSERT INTO ventas_pagos SELECT * FROM ventas_pagos_old")
        conn.execute("DROP TABLE ventas_pagos_old")
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    return True
