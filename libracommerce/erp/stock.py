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
from typing import Any

from .catalogo import get_default_deposito_id
from .hooks import SIN_GANCHOS, Hooks

# Tipo del producto -> movement_type semántico del motor. El tipo original se
# guarda aparte en `reason_code`, así que este mapeo puede ser muchos-a-uno.
_TIPO_A_MOVEMENT_TYPE = {
    "venta": "sale",
    "anulacion": "return",
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
}


def _tipo_de_row(movement_type: str, reason_code: str | None) -> str:
    return reason_code or _MOVEMENT_TYPE_A_TIPO.get(movement_type, movement_type)


def add_movimiento_stock(conn, producto_id: int, tipo: str, cantidad: float,
                         referencia: str = "", fecha: str = "",
                         venta_id: int | None = None,
                         usuario_id: int | None = None,
                         deposito_id: int | None = None):
    """Agrega un movimiento. cantidad positiva = entrada, negativa = salida.

    Un movimiento de cantidad 0 se ignora: `stock_movements` tiene
    `CHECK (quantity_delta <> 0)` y una fila en cero no aporta nada al ledger.
    """
    if not cantidad:
        return
    if tipo not in _TIPO_A_MOVEMENT_TYPE:
        raise ValueError(f"tipo de movimiento desconocido: {tipo!r}")
    # `fecha` llega como 'YYYY-MM-DD'; `occurred_at` es un timestamp ISO. Se
    # normaliza siempre a la forma canónica completa para que todos los
    # movimientos ordenen igual entre sí.
    _fecha = _datetime.fromisoformat(fecha or _date.today().isoformat()).isoformat()
    _deposito = deposito_id or get_default_deposito_id(conn)
    conn.execute(
        """INSERT INTO stock_movements
           (item_id, location_id, movement_type, quantity_delta, occurred_at,
            source_type, source_id, note, created_by, reason_code)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (producto_id, _deposito, _TIPO_A_MOVEMENT_TYPE[tipo], cantidad, _fecha,
         "venta" if venta_id else None, venta_id, referencia, usuario_id, tipo),
    )


def get_stock_actual(conn, producto_id: int) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(quantity_delta),0) FROM stock_movements WHERE item_id=?",
        (producto_id,),
    ).fetchone()
    return float(row[0])


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
                  usuario_id: int | None = None, fecha: str = ""):
    """Un movimiento de ajuste que lleva el stock al valor indicado."""
    actual = get_stock_actual(conn, producto_id)
    delta = round(stock_nuevo - actual, 4)
    if delta == 0:
        return
    add_movimiento_stock(
        conn, producto_id=producto_id, tipo="ajuste",
        cantidad=delta, referencia=referencia,
        usuario_id=usuario_id, fecha=fecha,
    )


def _es_servicio(conn, producto_id: int) -> bool:
    row = conn.execute("SELECT item_type FROM catalog_items WHERE id=?", (producto_id,)).fetchone()
    return bool(row) and row[0] == "service"


def descontar_stock_venta(conn, venta_id: int, items: list, fecha: str = "",
                          usuario_id: int | None = None,
                          hooks: Hooks = SIN_GANCHOS):
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
    """
    for item in items:
        pid = item.get("producto_id")
        if not pid:
            continue
        if _es_servicio(conn, pid):
            continue
        qty = abs(float(item.get("qty", 0)))
        insumos = hooks.resolver_receta(pid, item)
        if insumos:
            for insumo in insumos:
                add_movimiento_stock(
                    conn, producto_id=insumo.item_id, tipo="venta",
                    cantidad=-(float(insumo.cantidad) * qty),
                    referencia=f"Venta ID {venta_id} (receta)",
                    venta_id=venta_id, usuario_id=usuario_id, fecha=fecha,
                )
        else:
            add_movimiento_stock(
                conn, producto_id=pid, tipo="venta",
                cantidad=-qty,
                referencia=f"Venta ID {venta_id}",
                venta_id=venta_id, usuario_id=usuario_id, fecha=fecha,
            )
