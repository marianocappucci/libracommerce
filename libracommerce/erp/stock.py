"""Movimientos de stock sobre `stock_movements`: la orquestación que Contalibra y
Restolibra tenían duplicada en `app/db_stock.py` (P9-M1, 2026-09-06).

Las dos copias diferían en dos cosas, y las dos entran acá como variación
declarada y no como `if producto`:

1. **El vocabulario de `tipo`.** Restolibra tenía `merma` y `produccion` además
   de `entrada`/`salida`/`ajuste`/`venta`/`anulacion`/transferencias. Acá el
   vocabulario es la **unión**: un producto que nunca manda `merma` no la ve.
   El tipo original se preserva en `stock_movements.reason_code` (migración
   0003) y es el que se devuelve al leer; `movement_type` queda como el tipo
   semántico del motor. La vuelta `movement_type → tipo` sólo aplica a filas
   sin `reason_code` (las que genere el motor por su cuenta, ej. una recepción
   de compra): `waste` vuelve como `merma`, que es lo que significa.
2. **El descuento por venta.** Restolibra descuenta los insumos de la receta
   en vez del plato; Contalibra descuenta el producto. Eso entra por el gancho
   `resolver_receta` de `erp.hooks.Hooks`: con el default (ninguna receta) el
   comportamiento es exactamente el de Contalibra.

Cada función recibe la conexión; el ledger sigue 100 % aditivo (nunca UPDATE ni
DELETE sobre un movimiento, el stock siempre se calcula sumando).
"""

from __future__ import annotations

from datetime import date as _date
from datetime import datetime as _datetime
from decimal import Decimal
from typing import Any

from . import lotes
from .catalogo import get_default_deposito_id
from .hooks import SIN_GANCHOS, Hooks
from .lotes import MAX_LARGO_LOTE, normalizar_lote, normalizar_vencimiento  # noqa: F401  (se reexportan)

# Tipo del producto -> movement_type semántico del motor. El tipo original se
# guarda aparte en `reason_code`, así que este mapeo puede ser muchos-a-uno.
_TIPO_A_MOVEMENT_TYPE = {
    "venta": "sale",
    "anulacion": "return",
    # Reposición por una devolución PARCIAL (`erp.ventas.devolver_items`), no
    # por la anulación de la venta entera. Mismo `movement_type` que
    # `anulacion` (las dos son un `return`); lo que las distingue es el
    # `reason_code`, que es justo lo que `anular_venta` necesita para no
    # reponer dos veces lo que una devolución ya repuso (ver su docstring).
    "devolucion": "return",
    "ajuste": "adjustment",
    "entrada": "adjustment",
    "salida": "adjustment",
    "merma": "waste",
    "produccion": "adjustment",
    "transferencia_salida": "transfer_out",
    "transferencia_entrada": "transfer_in",
}

# Vuelta: sólo para movimientos sin `reason_code`.
_MOVEMENT_TYPE_A_TIPO = {
    "sale": "venta",
    "return": "anulacion",
    "adjustment": "ajuste",
    "transfer_out": "transferencia_salida",
    "transfer_in": "transferencia_entrada",
    "purchase": "entrada",
    "waste": "merma",
}

#: Los tipos que un producto puede escribir, en el orden en que la UI los lista.
TIPOS = tuple(_TIPO_A_MOVEMENT_TYPE)

#: Etiquetas de los tipos que se muestran en pantalla (unión de los dos productos).
TIPO_LABELS = {
    "entrada": "Entrada",
    "salida": "Salida",
    "ajuste": "Ajuste",
    "venta": "Venta",
    "merma": "Merma",
    "produccion": "Producción",
    "devolucion": "Devolución",
}


def _tipo_de_row(movement_type: str, reason_code: str | None) -> str:
    return reason_code or _MOVEMENT_TYPE_A_TIPO.get(movement_type, movement_type)


# `MAX_LARGO_LOTE`, `normalizar_lote` y `normalizar_vencimiento` viven ahora en `erp.lotes` (A-4 PR-2: `erp.lotes` no
# puede importar este módulo y los dos las necesitan); se reexportan acá con el mismo nombre.


def add_movimiento_stock(conn, producto_id: int, tipo: str, cantidad: float,
                         referencia: str = "", fecha: str = "",
                         venta_id: int | None = None,
                         usuario_id: int | None = None,
                         deposito_id: int | None = None,
                         variant_id: int | None = None,
                         lot_code: str | None = None,
                         expires_at: _date | _datetime | str | None = None):
    """Agrega un movimiento. cantidad positiva = entrada, negativa = salida.

    Un movimiento de cantidad 0 se ignora: `stock_movements` tiene
    `CHECK (quantity_delta <> 0)` y una fila en cero no aporta nada al ledger.

    `variant_id` viaja al ledger tal cual: `None` (el default, y lo único que
    manda hoy Contalibra/Restolibra) es "este ítem no tiene variantes".

    `lot_code` y `expires_at` (ADR-018, A-1) son ADITIVOS: el lote es una dimensión del ledger, no una tabla. Con
    los dos en `None` (el default, y lo único que mandan hoy los tres productos) el `INSERT` es carácter por
    carácter el de siempre. `lot_code` se recorta y no puede quedar vacío (`normalizar_lote`); `expires_at` se
    guarda como `'AAAA-MM-DD'` (`normalizar_vencimiento`). Un valor inválido es un `ValueError` **antes** de
    escribir nada. No cambian el stock total: sólo lo parten en el ledger.
    """
    if not cantidad:
        return
    if tipo not in _TIPO_A_MOVEMENT_TYPE:
        raise ValueError(f"tipo de movimiento desconocido: {tipo!r}")
    _lote = normalizar_lote(lot_code) if lot_code is not None else None
    _vence = normalizar_vencimiento(expires_at) if expires_at is not None else None
    # Import diferido: `erp.vencimientos` importa este módulo, así que un import de arriba sería un ciclo. Se usa SU
    # `hoy_argentina` para que «hoy» sea uno solo en todo el motor (ADR-044; mismo patrón que `lotes._hoy`).
    from .vencimientos import hoy_argentina

    # `fecha` llega como 'YYYY-MM-DD'; `occurred_at` es un timestamp ISO. Se
    # normaliza siempre a la forma canónica completa para que todos los
    # movimientos ordenen igual entre sí. Sin `fecha`, el día de Argentina.
    _fecha = _datetime.fromisoformat(fecha or hoy_argentina().isoformat()).isoformat()
    # `is None` y no `or`: un `deposito_id=0` no es un id real (los ids de
    # `locations` son seriales, arrancan en 1), pero `or` lo confundiría en
    # silencio con "no vino ninguno" y lo mandaría al default. Verificado:
    # ningún test ni caller del motor usa 0 como "sin depósito" (búsqueda en
    # `tests/` y `libracommerce/`, 2026-09-15).
    _deposito = deposito_id if deposito_id is not None else get_default_deposito_id(conn)
    if _lote is not None or _vence is not None:
        conn.execute(
            """INSERT INTO stock_movements
               (item_id, variant_id, location_id, movement_type, quantity_delta, occurred_at,
                source_type, source_id, note, created_by, reason_code, lot_code, expires_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (producto_id, variant_id, _deposito, _TIPO_A_MOVEMENT_TYPE[tipo], cantidad, _fecha,
             "venta" if venta_id else None, venta_id, referencia, usuario_id, tipo, _lote, _vence),
        )
        return
    conn.execute(
        """INSERT INTO stock_movements
           (item_id, variant_id, location_id, movement_type, quantity_delta, occurred_at,
            source_type, source_id, note, created_by, reason_code)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (producto_id, variant_id, _deposito, _TIPO_A_MOVEMENT_TYPE[tipo], cantidad, _fecha,
         "venta" if venta_id else None, venta_id, referencia, usuario_id, tipo),
    )


def get_stock_actual(conn, producto_id: int, deposito_id: int | None = None,
                     variant_id: int | None = None) -> float:
    """El stock de un producto. Sin `deposito_id` ni `variant_id`, el total de todos los depósitos (lo de siempre);
    con ellos, el de ese depósito y/o esa variante."""
    sql = "SELECT COALESCE(SUM(quantity_delta),0) FROM stock_movements WHERE item_id=?"
    params: list[Any] = [producto_id]
    if deposito_id is not None:
        sql += " AND location_id=?"
        params.append(deposito_id)
    if variant_id is not None:
        sql += " AND variant_id=?"
        params.append(variant_id)
    return float(conn.execute(sql, tuple(params)).fetchone()[0])


def get_stock_por_deposito(conn) -> dict[int, dict[int, float]]:
    """`{producto_id: {deposito_id: stock}}` con los depósitos que tienen movimientos. Lo usa `GET /api/stock` de
    un producto con sucursales (`OpcionesStock.por_deposito`)."""
    rows = conn.execute(
        "SELECT item_id, location_id, COALESCE(SUM(quantity_delta),0) FROM stock_movements "
        "GROUP BY item_id, location_id"
    ).fetchall()
    salida: dict[int, dict[int, float]] = {}
    for item_id, location_id, cantidad in rows:
        salida.setdefault(item_id, {})[location_id] = float(cantidad)
    return salida


def get_stock_todos(conn) -> list[dict]:
    """Todos los productos activos con su stock actual (los servicios incluidos:
    quien lista decide qué muestra; ver `alertas_de_stock`)."""
    rows = conn.execute("""
        SELECT ci.id, ic.code AS codigo, ci.name, ci.unit_code, ci.min_stock, ci.active,
               COALESCE(cat.name, '') AS categoria,
               COALESCE(SUM(sm.quantity_delta), 0) AS stock_actual
        FROM catalog_items ci
        LEFT JOIN categories cat ON cat.id = ci.category_id
        LEFT JOIN item_codes ic ON ic.item_id = ci.id AND ic.is_primary = 1
        LEFT JOIN stock_movements sm ON sm.item_id = ci.id
        WHERE ci.active = 1
        -- `ic.code` y `cat.name` van en el GROUP BY porque son de OTRAS tablas:
        -- PostgreSQL sólo deja omitir del grupo las columnas de la tabla cuya
        -- clave primaria se agrupa.
        GROUP BY ci.id, ic.code, cat.name
        ORDER BY ci.name
    """).fetchall()
    return [
        {
            "id": r["id"], "codigo": r["codigo"], "nombre": r["name"],
            "unidad": r["unit_code"], "categoria": r["categoria"],
            "stock_minimo": float(r["min_stock"]), "activo": r["active"],
            "stock_actual": float(r["stock_actual"]),
        }
        for r in rows
    ]


def alertas_de_stock(productos: list[dict]) -> list[dict]:
    """Los que están en o bajo su mínimo, cuando tienen mínimo."""
    return [p for p in productos if p["stock_minimo"] > 0 and p["stock_actual"] <= p["stock_minimo"]]


def get_movimientos_stock(conn, producto_id: int | None = None,
                          desde: str = "", hasta: str = "",
                          limit: int = 200) -> list[dict]:
    where: list[str] = []
    params: list[Any] = []
    if producto_id:
        where.append("sm.item_id = ?")
        params.append(producto_id)
    # `occurred_at` es un timestamp ISO; los filtros son por fecha. Se compara
    # el prefijo de 10 caracteres, si no un `<= '2026-07-01'` dejaría afuera
    # los movimientos de ese mismo día.
    if desde:
        where.append("substr(sm.occurred_at, 1, 10) >= ?")
        params.append(desde)
    if hasta:
        where.append("substr(sm.occurred_at, 1, 10) <= ?")
        params.append(hasta)
    sql = """SELECT sm.id, sm.item_id AS producto_id, sm.movement_type, sm.reason_code,
                    sm.quantity_delta, sm.note, sm.source_id AS venta_id,
                    sm.created_by AS usuario_id, sm.location_id AS deposito_id,
                    sm.created_at,
                    substr(sm.occurred_at, 1, 10) AS fecha,
                    ci.name AS producto_nombre, ci.unit_code AS unidad
             FROM stock_movements sm
             JOIN catalog_items ci ON ci.id = sm.item_id"""
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY sm.occurred_at DESC, sm.id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "id": r["id"], "producto_id": r["producto_id"],
            "tipo": _tipo_de_row(r["movement_type"], r["reason_code"]),
            "cantidad": float(r["quantity_delta"]), "referencia": r["note"],
            "venta_id": r["venta_id"], "usuario_id": r["usuario_id"],
            "fecha": r["fecha"], "deposito_id": r["deposito_id"],
            "created_at": r["created_at"],
            "producto_nombre": r["producto_nombre"], "unidad": r["unidad"],
        }
        for r in rows
    ]


def ajustar_stock(conn, producto_id: int, stock_nuevo: float, referencia: str,
                  usuario_id: int | None = None, fecha: str = "",
                  deposito_id: int | None = None, variant_id: int | None = None,
                  lot_code: str | None = None, expires_at: _date | _datetime | str | None = None):
    """Un movimiento de ajuste que lleva el stock al valor indicado. Con `deposito_id`/`variant_id` el valor es el
    de ese depósito y esa variante (y el movimiento va ahí); sin ellos, el total y el depósito por defecto.

    🔑 **Productos marcados (`tracks_expiry = 1`, ADR-018, A-4 PR-3).** Un producto sin marcar, y un marcado sin lotes,
    escriben EXACTAMENTE la fila de siempre. Un marcado CON lotes:

    - **con `lot_code` y/o `expires_at`** es el conteo de ESE bucket: `stock_nuevo` es lo que hay en el lote, y el delta
      se mide contra el saldo del bucket `(lote, vencimiento)` en el depósito que escribe (no contra el total); una fila
      `ajuste` con ese lote y vencimiento. Un lote que no existe se crea (el saldo de un bucket ausente es 0);
    - **sin lote**, `stock_nuevo` sigue siendo el total (como hoy) y un delta **negativo** sale por FEFO (una fila
      `ajuste` por lote consumido, con el mismo plan y las mismas reglas que la venta: vencidos primero, «sin lote»
      último, el resto que ningún bucket respalda va a una fila «sin lote», que queda negativa como hoy); un delta
      **positivo** entra en el bucket «sin lote» (no se inventa un lote).

    **Variantes:** un marcado ajustado SIN variante cuando tiene stock por variante en ese depósito es un
    `lotes.VarianteRequerida` (`ValueError`; 422 por HTTP): el total que se compara incluye las variantes y el FEFO sólo
    planifica la variante NULL. Con `variant_id` explícito, sin stock en variantes, o sin marcar, nada cambia. (Con lote
    el bucket ya es de una variante: no aplica.)

    Pasar `lot_code` o `expires_at` de un producto que NO está marcado es un `ValueError` (no hay lote que contar; antes
    de A-4 el parámetro no existía, así que nadie lo manda hoy). Antes de leer saldos el producto marcado se toma
    (`lotes.tomar_productos`): dos ajustes, o un ajuste y una venta, del mismo producto se serializan en PostgreSQL.

    🔴 **Límite preexistente, conservado a propósito:** sin `deposito_id` el valor que se compara es el **total de todos
    los depósitos** pero la fila se escribe en el depósito por defecto. Para un marcado con el stock repartido el FEFO
    corre sobre el depósito donde efectivamente se escribe (el por defecto), no sobre donde está el resto: un ajuste
    negativo puede dejar el «sin lote» del depósito por defecto en negativo aunque otro depósito tenga lotes. Cambiarlo
    alteraría el camino de los productos sin marcar; quien ajusta un producto con varios depósitos tiene que pasar
    `deposito_id`."""
    con_lote = lot_code is not None or expires_at is not None
    marcado = bool(lotes.ids_marcados(conn, [producto_id]))
    if not marcado:
        if con_lote:
            raise ValueError(
                "lot_code y expires_at sólo se pueden usar con un producto que vence (marcado): este no lo está"
            )
        return _ajustar_total(conn, producto_id, stock_nuevo, referencia, usuario_id, fecha, deposito_id, variant_id)
    # El producto se toma ANTES de leer saldos (y se relee lo que haya): el conteo y el FEFO ven el ledger ya serializado.
    lotes.tomar_productos(conn, [producto_id])
    if con_lote:
        lote = normalizar_lote(lot_code) if lot_code is not None else None
        vence = normalizar_vencimiento(expires_at) if expires_at is not None else None
        deposito = deposito_id if deposito_id is not None else get_default_deposito_id(conn)
        saldo = lotes.saldo_del_bucket(conn, producto_id, deposito, variant_id, lote, vence)
        delta = round(float(Decimal(str(stock_nuevo)) - saldo), 4)
        if delta == 0:
            return
        return add_movimiento_stock(
            conn, producto_id=producto_id, tipo="ajuste", cantidad=delta, referencia=referencia,
            usuario_id=usuario_id, fecha=fecha, deposito_id=deposito_id, variant_id=variant_id,
            lot_code=lote, expires_at=vence,
        )
    # Sin variante, el total que se compara incluye las variantes pero el FEFO sólo planifica la variante NULL: si hay stock
    # por variante en ese depósito se pide la variante (lotes.VarianteRequerida), en vez de bajar el bucket equivocado.
    lotes.exigir_variante(conn, producto_id, deposito_id if deposito_id is not None else get_default_deposito_id(conn),
                          variant_id)
    actual = get_stock_actual(conn, producto_id, deposito_id, variant_id)
    delta = round(stock_nuevo - actual, 4)
    if delta < 0:
        return _salida_por_lote(conn, producto_id=producto_id, cantidad=delta, referencia=referencia, venta_id=None,
                                usuario_id=usuario_id, fecha=fecha, deposito_id=deposito_id, variant_id=variant_id,
                                tipo="ajuste")
    return _ajustar_total(conn, producto_id, stock_nuevo, referencia, usuario_id, fecha, deposito_id, variant_id,
                          actual=actual)


def _ajustar_total(conn, producto_id, stock_nuevo, referencia, usuario_id, fecha, deposito_id, variant_id,
                   actual=None):
    """El ajuste de siempre: una sola fila `ajuste` sin lote por la diferencia contra el stock (total, o el del depósito
    y la variante). `actual` ya leído, si quien llama lo tenía."""
    if actual is None:
        actual = get_stock_actual(conn, producto_id, deposito_id, variant_id)
    delta = round(stock_nuevo - actual, 4)
    if delta == 0:
        return
    add_movimiento_stock(
        conn, producto_id=producto_id, tipo="ajuste",
        cantidad=delta, referencia=referencia,
        usuario_id=usuario_id, fecha=fecha,
        deposito_id=deposito_id, variant_id=variant_id,
    )


def salida_manual(conn, producto_id: int, tipo: str, cantidad: float, referencia: str, usuario_id: int | None = None,
                  fecha: str = "", deposito_id: int | None = None, variant_id: int | None = None):
    """Una salida manual de stock (`salida`, `merma`, ...: lo que el endpoint de ajuste escribe en esos modos) de
    `cantidad` (positiva; el signo lo pone esta función).

    🔑 **Productos marcados (`tracks_expiry = 1`, ADR-018, A-4 PR-3): FEFO.** Un producto marcado no resta del bucket «sin
    lote»: sale por los lotes en el mismo orden y con las mismas reglas que la venta (`_salida_por_lote`: una fila por lote
    consumido con su `lot_code` y `expires_at`, vencidos primero, «sin lote» último, lo que ningún bucket respalda va a una
    fila «sin lote»), conservando el `movement_type` y el `reason_code` de `tipo` y la misma `referencia` en cada fila. Se
    toma el producto antes de leer saldos (`lotes.tomar_productos`). **Un producto sin marcar, y un marcado sin lotes,
    escriben la llamada de siempre** a `add_movimiento_stock` (mismos argumentos, `INSERT` de 11 columnas).

    Es el punto de entrada para CUALQUIER salida manual de un producto que puede estar marcado: llamar a
    `add_movimiento_stock` con una cantidad negativa y sin lote resta del bucket «sin lote» y deja sobreestimado el saldo
    de los lotes. Un marcado SIN variante con stock por variante en ese depósito es `lotes.VarianteRequerida` (ver
    `ajustar_stock`)."""
    cantidad = -abs(cantidad)
    if lotes.ids_marcados(conn, [producto_id]):
        lotes.tomar_productos(conn, [producto_id])
        lotes.exigir_variante(conn, producto_id,
                              deposito_id if deposito_id is not None else get_default_deposito_id(conn), variant_id)
        return _salida_por_lote(conn, producto_id=producto_id, cantidad=cantidad, referencia=referencia, venta_id=None,
                                usuario_id=usuario_id, fecha=fecha, deposito_id=deposito_id, variant_id=variant_id,
                                tipo=tipo)
    return add_movimiento_stock(conn, producto_id, tipo, cantidad, referencia, usuario_id=usuario_id, fecha=fecha,
                                deposito_id=deposito_id, variant_id=variant_id)


def entrada_manual_con_lote(conn, producto_id: int, cantidad: float, referencia: str, *, lot_code=None,
                            expires_at=None, usuario_id: int | None = None, fecha: str = "",
                            deposito_id: int | None = None, variant_id: int | None = None):
    """Una entrada manual (`entrada`, `+cantidad`, UNA fila) al bucket `(lot_code, expires_at)` de un producto marcado: la
    misma fila que escribe `erp.vencimientos.registrar_entrada_con_lote`, pero sin su idempotencia por `clave_operacion`
    ni su validación de escala (que son de esa operación) y con la `referencia` que arme quien llama (el modo `entrada` del
    endpoint de ajuste, que suma una conversión de unidad de compra). `ValueError` si el producto no está marcado (no hay
    lote que cargar) o el lote o el vencimiento no son válidos, antes de escribir. Sumar no bloquea ni toca otros
    buckets. La entrada SIN lote no pasa por acá: sigue entrando «sin lote» (no sobreestima ningún lote)."""
    if not lotes.ids_marcados(conn, [producto_id]):
        raise ValueError(
            "lot_code y expires_at sólo se pueden usar con un producto que vence (marcado): este no lo está"
        )
    return add_movimiento_stock(conn, producto_id, "entrada", cantidad, referencia, usuario_id=usuario_id, fecha=fecha,
                                deposito_id=deposito_id, variant_id=variant_id, lot_code=lot_code, expires_at=expires_at)


def _es_servicio(conn, producto_id: int) -> bool:
    row = conn.execute("SELECT item_type FROM catalog_items WHERE id=?", (producto_id,)).fetchone()
    return bool(row) and row[0] == "service"


def _salida_por_lote(conn, *, producto_id: int, cantidad: float, referencia: str, venta_id: int | None,
                     usuario_id: int | None, fecha: str, deposito_id: int | None, variant_id: int | None = None,
                     tipo: str = "venta"):
    """La salida de `cantidad` (negativa) de un producto **marcado** (`tracks_expiry=1`, ya tomado con
    `lotes.tomar_productos`): una fila por bucket en orden FEFO (`lotes.plan_fefo`), cada una con el `lot_code` y el
    `expires_at` de su bucket. Un marcado sin lotes da un único tramo «sin lote» y entonces la llamada es, argumento por
    argumento, la de siempre (incluida la cantidad original en `float`, sin pasar por `Decimal`).

    `tipo` es el de las filas: `venta` (la venta, con su `venta_id`) o `ajuste` (el ajuste negativo de
    `ajustar_stock`, sin `venta_id`; A-4 PR-3)."""
    if not cantidad:
        return add_movimiento_stock(conn, producto_id=producto_id, tipo=tipo, cantidad=cantidad,
                                    referencia=referencia, venta_id=venta_id, usuario_id=usuario_id, fecha=fecha,
                                    variant_id=variant_id, deposito_id=deposito_id)
    deposito = deposito_id if deposito_id is not None else get_default_deposito_id(conn)
    tramos = lotes.plan_fefo(conn, producto_id, deposito, variant_id, Decimal(str(abs(cantidad))))
    for t in tramos:
        if t.sin_lote:
            add_movimiento_stock(
                conn, producto_id=producto_id, tipo=tipo,
                cantidad=cantidad if len(tramos) == 1 else -float(t.cantidad),
                referencia=referencia, venta_id=venta_id, usuario_id=usuario_id, fecha=fecha,
                variant_id=variant_id, deposito_id=deposito_id,
            )
        else:
            add_movimiento_stock(
                conn, producto_id=producto_id, tipo=tipo, cantidad=-float(t.cantidad),
                referencia=referencia, venta_id=venta_id, usuario_id=usuario_id, fecha=fecha,
                variant_id=variant_id, deposito_id=deposito_id, lot_code=t.lote, expires_at=t.vence,
            )


def descontar_stock_venta(conn, venta_id: int, items: list, fecha: str = "",
                          usuario_id: int | None = None,
                          hooks: Hooks = SIN_GANCHOS,
                          deposito_id: int | None = None):
    """Descuenta stock por cada ítem de la venta con `producto_id` que sea de
    tipo 'producto' — un servicio nunca genera movimiento: no tiene inventario.

    🔑 **Corre dentro de la transacción de la venta** (`conn` es la de
    `crear_venta_directa`/`cobrar_pedido`): un error acá aborta el cobro
    completo, no se pierde en silencio.

    Con `hooks.resolver_receta` un producto puede decir que un ítem se
    descuenta por sus insumos (Restolibra: receta × cantidad, con los
    modificadores del pedido ya aplicados por el gancho) en vez de por el
    propio ítem. `None` significa "no tiene receta": se descuenta el ítem, que
    es el comportamiento de Contalibra y el default.

    `deposito_id` es ADITIVO (F4, VentaLibra multisucursal): `None` (el
    default) deja que `add_movimiento_stock` resuelva el depósito por
    defecto, exactamente como hoy. Con un valor, se descuenta de ESE depósito
    — el ítem, o sus insumos si tiene receta.

    🔑 **FEFO para los productos marcados (ADR-018, A-4 PR-2).** Un producto (o un insumo de receta) con
    `catalog_items.tracks_expiry = 1` no sale del bucket «sin lote»: sale de los lotes en orden FEFO, **una fila `sale`
    por lote consumido** con su `lot_code` y `expires_at` (ver `erp.lotes`: con fecha por vencimiento, con código sin
    fecha, «sin lote» último; el faltante va a una fila más del «sin lote»). Antes de leer los saldos se toman los
    productos marcados (`lotes.tomar_productos`, en orden de id), así dos ventas simultáneas no consumen dos veces el
    mismo lote. **Un producto sin marcar, y uno marcado sin lotes, escriben EXACTAMENTE las filas de siempre**; y si
    ningún producto de la venta está marcado (una sola consulta lo dice) el código es el de antes. Los insumos sin
    marcar y los platos de una receta no cambian.
    """
    # Primero se resuelve qué descuenta cada línea (servicio, receta) y se sondea UNA vez si algún producto que va a
    # salir está marcado (`tracks_expiry = 1`): sin ninguno, el resto es EXACTAMENTE el código de siempre.
    lineas = []
    for item in items:
        pid = item.get("producto_id")
        if not pid:
            continue
        if _es_servicio(conn, pid):
            continue
        lineas.append((item, pid, hooks.resolver_receta(pid, item)))
    marcados = lotes.ids_marcados(conn, _ids_que_salen(lineas))
    if marcados:
        # FEFO: se toman los productos marcados (en orden de id) ANTES de leer saldos; `plan_fefo` relee después.
        lotes.tomar_productos(conn, marcados)
    for item, pid, insumos in lineas:
        qty = abs(float(item.get("qty", 0)))
        if insumos:
            # La receta se resuelve en OTROS ítems (los insumos): la variante
            # del plato vendido no tiene sentido acá, así que no viaja.
            for insumo in insumos:
                cantidad = -(float(insumo.cantidad) * qty)
                if insumo.item_id in marcados:
                    _salida_por_lote(
                        conn, producto_id=insumo.item_id, cantidad=cantidad,
                        referencia=f"Venta ID {venta_id} (receta)",
                        venta_id=venta_id, usuario_id=usuario_id, fecha=fecha,
                        deposito_id=deposito_id,
                    )
                    continue
                add_movimiento_stock(
                    conn, producto_id=insumo.item_id, tipo="venta",
                    cantidad=cantidad,
                    referencia=f"Venta ID {venta_id} (receta)",
                    venta_id=venta_id, usuario_id=usuario_id, fecha=fecha,
                    deposito_id=deposito_id,
                )
        elif pid in marcados:
            _salida_por_lote(
                conn, producto_id=pid, cantidad=-qty,
                referencia=f"Venta ID {venta_id}",
                venta_id=venta_id, usuario_id=usuario_id, fecha=fecha,
                variant_id=item.get("variante_id"),
                deposito_id=deposito_id,
            )
        else:
            add_movimiento_stock(
                conn, producto_id=pid, tipo="venta",
                cantidad=-qty,
                referencia=f"Venta ID {venta_id}",
                venta_id=venta_id, usuario_id=usuario_id, fecha=fecha,
                variant_id=item.get("variante_id"),
                deposito_id=deposito_id,
            )


def _ids_que_salen(lineas) -> list[int]:
    """Los productos que van a escribir una fila: el ítem si no tiene receta, y sus insumos si la tiene."""
    return [i.item_id for _, _, insumos in lineas if insumos for i in insumos] + [
        pid for _, pid, insumos in lineas if not insumos
    ]
