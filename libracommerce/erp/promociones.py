"""Promociones: "llevá N pagá M" (2x1, 3x2) y combos fijos (2026-09-28).

Construcción nueva, pedida por el roadmap de producto de VentaLibra: ningún
producto de la familia tenía promociones por regla, sólo precio por cantidad y
por vigencia (`erp.listas_precio`). Viven en el motor, no en el producto, para que
Contalibra y Restolibra puedan montarlas el día de mañana.

Dos tipos, un solo modelo (`promotions` + `promotion_items`):

- `nxm`: un solo producto, `cantidad` = N unidades del paquete y `paga` = M
  (`paga < cantidad`). 2x1 es `cantidad=2, paga=1`.
- `combo`: dos o más productos distintos, cada uno con su `cantidad`, a un
  `precio` cerrado para el paquete entero.

**La promoción no toca las líneas de la venta**: sigue habiendo una línea por
producto a su precio de lista, y el ahorro viaja en el `descuento` de la venta.
Así el stock, el costo por línea y las devoluciones siguen siendo veraces por
producto. Qué promoción se aplicó y cuánto ahorró queda en `sale_promotions`.

**Cuando compiten**, gana la de mayor ahorro por paquete (empate: la más
vieja), consume las unidades que usa y las demás se calculan con lo que sobra.
Es un criterio simple y predecible, no el óptimo global: no busca la
combinación que más le convenga al cliente.

La tabla la crea `erp.schema.crear_promociones`.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from .listas_precio import a_hora_local

TIPO_NXM = "nxm"
TIPO_COMBO = "combo"
TIPOS = (TIPO_NXM, TIPO_COMBO)


def _dec(valor) -> Decimal:
    return Decimal(str(valor))


def _num(valor) -> float:
    return float(valor) if valor is not None else 0.0


def _validar(tipo: str, items: list[dict], paga, precio, desde: str, hasta: str) -> None:
    """Rebota, con un mensaje que dice qué falta, lo que nunca podría aplicarse."""
    if tipo not in TIPOS:
        raise ValueError(f"tipo de promoción inválido: {tipo!r} (usar 'nxm' o 'combo')")
    if not items:
        raise ValueError("la promoción necesita al menos un producto")
    if any(_dec(i["cantidad"]) <= 0 for i in items):
        raise ValueError("cada producto de la promoción necesita una cantidad mayor a 0")
    ids = [i["producto_id"] for i in items]
    if len(set(ids)) != len(ids):
        raise ValueError("un producto no puede repetirse dentro de la misma promoción")
    if tipo == TIPO_NXM:
        if len(items) != 1:
            raise ValueError("'llevá N pagá M' es sobre un solo producto")
        lleva = _dec(items[0]["cantidad"])
        if lleva < 2:
            raise ValueError("'llevá N pagá M' necesita llevar al menos 2 unidades")
        if paga is None or _dec(paga) <= 0 or _dec(paga) >= lleva:
            raise ValueError("'pagá M' tiene que ser mayor a 0 y menor que las unidades que se llevan")
    else:
        if len(items) < 2:
            raise ValueError("un combo necesita al menos dos productos distintos")
        if precio is None or _dec(precio) <= 0:
            raise ValueError("el combo necesita un precio mayor a 0")
    if desde and hasta and datetime.fromisoformat(hasta) <= datetime.fromisoformat(desde):
        raise ValueError("la vigencia termina antes de empezar")


def _guardar_items(conn, promocion_id: int, items: list[dict]) -> None:
    for i in items:
        conn.execute(
            "INSERT INTO promotion_items (promotion_id, item_id, quantity) VALUES (?, ?, ?)",
            (promocion_id, i["producto_id"], float(_dec(i["cantidad"]))),
        )


def _existen_productos(conn, items: list[dict]) -> None:
    for i in items:
        if conn.execute("SELECT 1 FROM catalog_items WHERE id=?", (i["producto_id"],)).fetchone() is None:
            raise ValueError(f"el producto {i['producto_id']} no existe")


def crear_promocion(conn, *, nombre: str, tipo: str, items: list[dict], paga: float | None = None,
                    precio: float | None = None, desde: str = "", hasta: str = "",
                    activa: bool = True) -> int:
    """Alta. `items` es `[{producto_id, cantidad}]`. No commitea."""
    if not nombre.strip():
        raise ValueError("la promoción necesita un nombre")
    _validar(tipo, items, paga, precio, desde, hasta)
    _existen_productos(conn, items)
    cur = conn.execute(
        "INSERT INTO promotions (name, kind, pay_quantity, combo_price, valid_from, valid_until, "
        "active, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (nombre.strip(), tipo, paga if tipo == TIPO_NXM else None,
         precio if tipo == TIPO_COMBO else None, desde or None, hasta or None,
         1 if activa else 0, datetime.now().isoformat(timespec="seconds")),
    )
    promocion_id = cur.lastrowid
    _guardar_items(conn, promocion_id, items)
    return promocion_id


def actualizar_promocion(conn, promocion_id: int, *, nombre: str, tipo: str, items: list[dict],
                         paga: float | None = None, precio: float | None = None,
                         desde: str = "", hasta: str = "", activa: bool = True) -> bool:
    """Reemplaza la regla entera (incluidos los productos). `False` si no existe."""
    if conn.execute("SELECT 1 FROM promotions WHERE id=?", (promocion_id,)).fetchone() is None:
        return False
    if not nombre.strip():
        raise ValueError("la promoción necesita un nombre")
    _validar(tipo, items, paga, precio, desde, hasta)
    _existen_productos(conn, items)
    conn.execute(
        "UPDATE promotions SET name=?, kind=?, pay_quantity=?, combo_price=?, valid_from=?, "
        "valid_until=?, active=? WHERE id=?",
        (nombre.strip(), tipo, paga if tipo == TIPO_NXM else None,
         precio if tipo == TIPO_COMBO else None, desde or None, hasta or None,
         1 if activa else 0, promocion_id),
    )
    conn.execute("DELETE FROM promotion_items WHERE promotion_id=?", (promocion_id,))
    _guardar_items(conn, promocion_id, items)
    return True


def borrar_promocion(conn, promocion_id: int) -> bool:
    """Baja física. Lo ya vendido conserva su registro (`promotion_id` queda en NULL)."""
    cur = conn.execute("DELETE FROM promotions WHERE id=?", (promocion_id,))
    return cur.rowcount > 0


def _dict(row, items: list[dict]) -> dict:
    return {
        "id": row["id"], "nombre": row["name"], "tipo": row["kind"],
        "paga": _num(row["pay_quantity"]) if row["pay_quantity"] is not None else None,
        "precio": _num(row["combo_price"]) if row["combo_price"] is not None else None,
        "desde": row["valid_from"], "hasta": row["valid_until"],
        "activa": row["active"], "items": items,
    }


def _items_de(conn, promocion_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT pi.item_id, pi.quantity, ci.name FROM promotion_items pi "
        "JOIN catalog_items ci ON ci.id = pi.item_id WHERE pi.promotion_id=? ORDER BY pi.id",
        (promocion_id,),
    ).fetchall()
    return [
        {"producto_id": r["item_id"], "cantidad": _num(r["quantity"]), "nombre": r["name"]}
        for r in rows
    ]


def get_promocion(conn, promocion_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM promotions WHERE id=?", (promocion_id,)).fetchone()
    return _dict(row, _items_de(conn, promocion_id)) if row else None


def listar_promociones(conn, *, solo_activas: bool = False) -> list[dict]:
    where = "WHERE active=1" if solo_activas else ""
    rows = conn.execute(f"SELECT * FROM promotions {where} ORDER BY active DESC, name, id").fetchall()
    return [_dict(r, _items_de(conn, r["id"])) for r in rows]


def _vigente(promo: dict, momento: datetime) -> bool:
    if not promo["activa"]:
        return False
    if promo["desde"] and datetime.fromisoformat(promo["desde"]) > momento:
        return False
    return not (promo["hasta"] and datetime.fromisoformat(promo["hasta"]) < momento)


def calcular(conn, lineas: list[dict], *, en: str = "") -> dict:
    """Qué promociones aplican a un carrito y cuánto ahorran.

    `lineas` es `[{producto_id, qty, precio}]` (lo que viaja en la venta). Las
    líneas sin `producto_id` se ignoran, y varias del mismo producto se juntan
    con el precio promedio ponderado. `en` (ISO, vacío = ahora) admite un
    instante con zona: se pasa a hora local igual que las vigencias de precio.

    Devuelve `{"aplicadas": [{promocion_id, nombre, veces, ahorro}], "ahorro": total}`.
    """
    momento = a_hora_local(datetime.fromisoformat(en)) if en else datetime.now()

    disponible: dict[int, Decimal] = {}
    importe: dict[int, Decimal] = {}
    for linea in lineas:
        pid = linea.get("producto_id")
        qty = _dec(linea["qty"])
        if pid is None or qty <= 0:
            continue
        disponible[pid] = disponible.get(pid, Decimal(0)) + qty
        importe[pid] = importe.get(pid, Decimal(0)) + qty * _dec(linea["precio"])
    precio_de = {pid: importe[pid] / disponible[pid] for pid in disponible}

    candidatas = []
    for promo in listar_promociones(conn, solo_activas=True):
        if not _vigente(promo, momento):
            continue
        if any(i["producto_id"] not in precio_de for i in promo["items"]):
            continue
        de_lista = sum(_dec(i["cantidad"]) * precio_de[i["producto_id"]] for i in promo["items"])
        if promo["tipo"] == TIPO_NXM:
            unidad = precio_de[promo["items"][0]["producto_id"]]
            por_paquete = de_lista - _dec(promo["paga"]) * unidad
        else:
            por_paquete = de_lista - _dec(promo["precio"])
        if por_paquete > 0:
            candidatas.append((por_paquete, promo))
    candidatas.sort(key=lambda c: (-c[0], c[1]["id"]))

    aplicadas = []
    for por_paquete, promo in candidatas:
        veces = min(
            int(disponible[i["producto_id"]] // _dec(i["cantidad"])) for i in promo["items"]
        )
        if veces <= 0:
            continue
        for i in promo["items"]:
            disponible[i["producto_id"]] -= _dec(i["cantidad"]) * veces
        aplicadas.append({
            "promocion_id": promo["id"], "nombre": promo["nombre"], "veces": veces,
            "ahorro": float(round(por_paquete * veces, 2)),
        })
    return {"aplicadas": aplicadas, "ahorro": round(sum(a["ahorro"] for a in aplicadas), 2)}


def registrar_aplicadas(conn, venta_id: int, aplicadas: list[dict]) -> None:
    """Deja en `sale_promotions` qué promoción se aplicó a la venta. No commitea:
    corre con la conexión de la venta, así que si ésta se revierte, esto también."""
    for a in aplicadas:
        conn.execute(
            "INSERT INTO sale_promotions (sale_id, promotion_id, name, times, amount) "
            "VALUES (?, ?, ?, ?, ?)",
            (venta_id, a["promocion_id"], a["nombre"], a["veces"], a["ahorro"]),
        )


def promociones_de_venta(conn, venta_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT promotion_id, name, times, amount FROM sale_promotions WHERE sale_id=? ORDER BY id",
        (venta_id,),
    ).fetchall()
    return [
        {"promocion_id": r["promotion_id"], "nombre": r["name"], "veces": r["times"],
         "ahorro": _num(r["amount"])}
        for r in rows
    ]
