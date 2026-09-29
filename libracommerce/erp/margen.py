"""Margen y rotación por producto y por período: sólo lectura (2026-09-29).

Roadmap de producto de VentaLibra, tanda 1 (ADR-015). Lo que ya hay en
`erp.reportes` cuenta plata vendida (`reporte_productos_top`: cantidad y total
por producto); nadie restaba el costo. Acá se junta lo que ya está guardado en
`sale_items` —cantidad, precio, descuento de línea, `unit_cost_snapshot`— y se
devuelve ingreso, costo, margen ($ y %) y unidades, por producto y por período.
No escribe nada ni toca el schema.

🔴 **Qué cuenta como venta.** `sales.status` en `confirmed`, `partially_returned` y
`returned`: una anulada (`cancelled`) y una pendiente de cobro (`draft`, el QR sin
acreditar) no son ventas —el mismo criterio que `reportes.puerto_de_reportes(
solo_confirmadas=True)`—. Las devoluciones no viven en `sale_items`: la venta sigue
`confirmed` (`erp.ventas.devolver_items` sólo mueve `status_detail`, stock y caja),
así que lo devuelto se resta leyendo el ledger de stock (`_devuelto_por_clave`), por
las dos formas que tiene el motor de escribirlo (la nueva y la de `usecases.sales.
return_sale_items`). Una línea devuelta a medias cuenta la parte que se quedó el
cliente: unidades, ingreso y costo, en la misma proporción. Una línea devuelta entera
no cuenta nada, tampoco sus avisos de costo (`costo_estimado`, `sin_costo`).

🔴 **Límite conocido: la devolución se reparte por (ítem, variante), no por línea.** El
ledger de `devolver_items` no dice de qué línea volvió lo devuelto (`source_id` es la venta,
`reason_code='devolucion'`, sin `sale_item_id`; el propio motor trata las líneas de una
clave como un pozo común), así que con dos líneas del mismo producto y variante a distinto
precio (o distinto snapshot de costo) lo devuelto se prorratea por cantidad: devolver la de
$100 de una venta de $100 + $200 deja $150 de ingreso, no $200. Con el mismo precio y costo
en las líneas —lo habitual— el resultado es exacto. Sólo el camino viejo (`sale_return`) guarda
la posición de la línea, en `reason_code`; no se usa para no tener dos criterios en el mismo
reporte. Arreglarlo de verdad es guardar la línea en el ledger (escritura, decisión aparte).

**El rango es por día.** `sales.occurred_on` es texto libre y puede traer hora: `desde` se
compara por su fecha y `hasta` con la cota exclusiva del día siguiente (`_filtro_de_ventas`).

🔴 **De dónde sale el costo.** `sale_items.unit_cost_snapshot` cuando existe. Pero
`erp.ventas.crear_venta` —el camino de `POST /api/ventas`— **no lo escribe**: sólo lo
llena `db.repository.save_sale` con un `SaleItem` del dominio. Sin snapshot se usa el
`default_cost` ACTUAL del producto, y el resultado lo dice (`costo_estimado`): es el
costo de hoy, no el de aquella venta. Si tampoco hay costo (`None` o `0`), la línea
cuenta con costo 0 y queda marcada (`sin_costo`): el margen de ese producto está
inflado, y la pantalla tiene que avisarlo en vez de mostrar un 100% como si fuera real.

**El descuento.** El de línea (`discount_amount`) se resta de la línea. El de la
venta (`sales.discount_total`, donde viajan el descuento manual y el ahorro de las
promociones: `crear_venta` no escribe descuentos de línea) se reparte entre las líneas
en proporción a lo que valen, **descontando primero lo que ya explican los descuentos de
línea** para no restarlo dos veces. Así el ingreso de una línea es lo que se cobró por
ella, no el precio de lista. No se le quita el IVA: `crear_venta` deja `tax_amount` en 0
y el precio es el que se cargó.

Alcance: líneas `product` con `item_id` (los servicios y las líneas ad-hoc no tienen
costo ni producto: entran sólo para repartir el descuento de la venta). Se agrupa por
producto, no por variante. Se agrega en Python y no en SQL: el reparto del descuento y
el neto de devoluciones no se dejan escribir como un `GROUP BY` que corra igual en
SQLite y en PostgreSQL, y el período se arma con `datetime` en vez de `strftime`.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal

#: Los `sales.status` que son una venta. Ver el docstring del módulo.
_STATUS_DE_VENTA = ("confirmed", "partially_returned", "returned")

AGRUPACIONES = ("dia", "semana", "mes")
#: Por qué columnas se ordena la tabla de productos.
ORDENES = ("margen", "margen_pct", "ingreso", "costo", "unidades", "unidades_por_dia", "nombre")
SENTIDOS = ("asc", "desc")

#: Un período sin fecha (`occurred_on` vacío: una venta vieja mal cargada).
SIN_FECHA = "sin fecha"

_CERO = Decimal("0")


def _dec(valor) -> Decimal:
    """`Decimal` desde lo que devuelva el motor de base (int/float en SQLite,
    `Decimal` en PostgreSQL, texto en una fila vieja)."""
    return Decimal(str(valor)) if valor is not None else _CERO


@dataclass
class _Acum:
    unidades: Decimal = _CERO
    ingreso: Decimal = _CERO
    costo: Decimal = _CERO
    #: Alguna línea usó el costo de hoy porque la venta no guardó el suyo.
    costo_estimado: bool = False
    #: Alguna línea no tiene costo de ningún lado: su margen es 100%, y es mentira.
    sin_costo: bool = False


def _redondear(valor: Decimal, decimales: int = 2) -> float:
    return float(round(valor, decimales))


def _margen_pct(margen: Decimal, ingreso: Decimal) -> float | None:
    """`None` cuando no hubo ingreso: dividir por cero no es un 0%."""
    return _redondear(margen / ingreso * 100) if ingreso > 0 else None


def _periodo(occurred_on, agrupacion: str) -> str:
    """El período de una venta, con las mismas claves que `reportes.reporte_ventas`
    (`%Y-%m-%d`, `%Y-W%W`, `%Y-%m`) pero armado en Python."""
    texto = str(occurred_on or "")[:10]
    if agrupacion == "mes":
        return texto[:7] or SIN_FECHA
    if agrupacion == "semana":
        try:
            return datetime.date.fromisoformat(texto).strftime("%Y-W%W")
        except ValueError:
            return SIN_FECHA
    return texto or SIN_FECHA


def _dia(texto: str) -> datetime.date | None:
    """La parte fecha de `texto` (`2026-09-29` o `2026-09-29 13:00:00`), o `None` si no es una fecha."""
    try:
        return datetime.date.fromisoformat(texto[:10])
    except ValueError:
        return None


def _dias(desde: str, hasta: str) -> int | None:
    """Cuántos días abarca el rango, ambos extremos incluidos; `None` si alguno falta o no es
    una fecha (sin rango cerrado no hay "por día" que calcular)."""
    try:
        d = datetime.date.fromisoformat(desde[:10])
        h = datetime.date.fromisoformat(hasta[:10])
    except ValueError:
        return None
    return (h - d).days + 1 if h >= d else None


def _filtro_de_ventas(desde: str, hasta: str) -> tuple[str, list]:
    """El `WHERE` de las ventas del rango, por **día**: `sales.occurred_on` es texto libre (`POST /api/ventas`
    acepta `fecha` con hora, `2026-09-29 13:00:00`), y un `<= '2026-09-29'` contra el texto deja afuera todo lo
    del 29 que traiga hora. Por eso `desde` se compara por su parte fecha y `hasta` como cota exclusiva del día
    siguiente (`< '2026-09-30'`): sólo comparaciones de texto, el mismo SQL en SQLite y en PostgreSQL. Un extremo
    que no es una fecha se usa tal cual, como antes. Lo usan las ventas y el ledger de devoluciones
    (`_devuelto_por_clave`), así que las dos miran el mismo rango."""
    donde = ["s.status IN (" + ",".join("?" for _ in _STATUS_DE_VENTA) + ")"]
    params: list = list(_STATUS_DE_VENTA)
    if desde:
        donde.append("s.occurred_on >= ?")
        params.append(dia.isoformat() if (dia := _dia(desde)) else desde)
    if hasta:
        if dia := _dia(hasta):
            donde.append("s.occurred_on < ?")
            params.append((dia + datetime.timedelta(days=1)).isoformat())
        else:
            donde.append("s.occurred_on <= ?")
            params.append(hasta)
    return " AND ".join(donde), params


def _devuelto_por_clave(conn, desde: str, hasta: str) -> dict[tuple, Decimal]:
    """Lo devuelto por (venta, ítem, variante), del ledger de stock.

    Las dos formas en que el motor anota una devolución (mismo criterio que
    `erp.ventas._COND_DEVUELTO`, pero atadas a `source_type` porque acá se junta con
    `sales` por `source_id`, que en otros orígenes es el id de otra tabla): la nueva
    (`devolver_items`: `reason_code='devolucion'`, `source_type='venta'`) y la de
    `usecases.sales.return_sale_items` (`source_type='sale_return'`, `movement_type='return'`)."""
    donde, params = _filtro_de_ventas(desde, hasta)
    filas = conn.execute(
        f"""SELECT sm.source_id AS venta_id, sm.item_id, sm.variant_id,
                   SUM(sm.quantity_delta) AS devuelto
            FROM stock_movements sm
            JOIN sales s ON s.id = sm.source_id
            WHERE ((sm.source_type = 'venta' AND sm.reason_code = 'devolucion')
                   OR (sm.source_type = 'sale_return' AND sm.movement_type = 'return'))
              AND {donde}
            GROUP BY sm.source_id, sm.item_id, sm.variant_id""",
        params,
    ).fetchall()
    return {(f["venta_id"], f["item_id"], f["variant_id"]): _dec(f["devuelto"]) for f in filas}


def _lineas_netas(conn, desde: str, hasta: str):
    """Cada línea de producto de las ventas del rango, ya neta de descuentos y
    devoluciones: `(occurred_on, item_id, nombre, unidades, ingreso, costo, estimado, sin_costo)`."""
    donde, params = _filtro_de_ventas(desde, hasta)
    filas = conn.execute(
        f"""SELECT s.id AS venta_id, s.occurred_on, s.discount_total,
                   si.kind, si.item_id, si.variant_id, si.description_snapshot,
                   si.quantity, si.unit_price, si.discount_amount, si.unit_cost_snapshot,
                   ci.name AS nombre, ci.default_cost
            FROM sales s
            JOIN sale_items si ON si.sale_id = s.id
            LEFT JOIN catalog_items ci ON ci.id = si.item_id
            WHERE {donde}
            ORDER BY s.id, si.id""",
        params,
    ).fetchall()
    devuelto = _devuelto_por_clave(conn, desde, hasta)

    por_venta: dict[int, list] = {}
    for f in filas:
        por_venta.setdefault(f["venta_id"], []).append(f)

    for venta_id, lineas in por_venta.items():
        # Lo que vale cada línea después de su propio descuento, y lo que del descuento
        # de la venta queda por repartir (ver "El descuento" en el docstring del módulo).
        valores = [_dec(f["quantity"]) * _dec(f["unit_price"]) - _dec(f["discount_amount"]) for f in lineas]
        en_lineas = sum((_dec(f["discount_amount"]) for f in lineas), _CERO)
        de_la_venta = max(_dec(lineas[0]["discount_total"]) - en_lineas, _CERO)
        base = sum(valores, _CERO)

        # Cuánto de cada (ítem, variante) se quedó el cliente: lo devuelto se reparte
        # entre las líneas de esa clave en la misma proporción, con tope en lo vendido.
        vendido: dict[tuple, Decimal] = {}
        for f in lineas:
            if f["item_id"] is not None:
                clave = (f["item_id"], f["variant_id"])
                vendido[clave] = vendido.get(clave, _CERO) + _dec(f["quantity"])

        for f, valor in zip(lineas, valores, strict=True):
            if f["kind"] != "product" or f["item_id"] is None:
                continue
            clave = (f["item_id"], f["variant_id"])
            de_esa_clave = vendido[clave]
            devuelta = min(devuelto.get((venta_id, *clave), _CERO), de_esa_clave)
            queda = (de_esa_clave - devuelta) / de_esa_clave if de_esa_clave > 0 else _CERO
            if queda <= 0:
                # Devuelta entera: no aporta unidades, ingreso ni costo, y tampoco puede avisar de un costo
                # estimado o faltante en el producto, el período o el resumen.
                continue

            cantidad = _dec(f["quantity"])
            ingreso = valor - (de_la_venta * valor / base if base > 0 else _CERO)

            if f["unit_cost_snapshot"] is not None:
                costo_unitario, estimado = _dec(f["unit_cost_snapshot"]), False
            else:
                costo_unitario, estimado = _dec(f["default_cost"]), True
            sin_costo = costo_unitario <= 0
            # Sin costo de ningún lado no hay nada "estimado": es un dato que falta.
            estimado = estimado and not sin_costo

            yield (
                f["occurred_on"], f["item_id"], f["nombre"] or f["description_snapshot"],
                cantidad * queda, ingreso * queda, costo_unitario * cantidad * queda,
                estimado, sin_costo,
            )


def _sumar(acum: _Acum, unidades, ingreso, costo, estimado, sin_costo) -> None:
    acum.unidades += unidades
    acum.ingreso += ingreso
    acum.costo += costo
    acum.costo_estimado = acum.costo_estimado or estimado
    acum.sin_costo = acum.sin_costo or sin_costo


def _fila(acum: _Acum) -> dict:
    margen = acum.ingreso - acum.costo
    return {
        "unidades": _redondear(acum.unidades, 3),
        "ingreso": _redondear(acum.ingreso),
        "costo": _redondear(acum.costo),
        "margen": _redondear(margen),
        "margen_pct": _margen_pct(margen, acum.ingreso),
        "costo_estimado": acum.costo_estimado,
        "sin_costo": acum.sin_costo,
    }


def _ordenar(productos: list[dict], orden: str, sentido: str) -> list[dict]:
    """Por la columna pedida; los que no tienen valor (`margen_pct` sin ingreso, o
    `unidades_por_dia` sin rango) van siempre al final, sea cual sea el sentido. El
    desempate es el nombre, para que dos consultas seguidas den el mismo orden."""
    productos = sorted(productos, key=lambda p: (p["nombre"].casefold(), p["producto_id"]))
    if orden == "nombre":
        return sorted(productos, key=lambda p: p["nombre"].casefold(), reverse=sentido == "desc")
    con_valor = [p for p in productos if p[orden] is not None]
    sin_valor = [p for p in productos if p[orden] is None]
    return sorted(con_valor, key=lambda p: p[orden], reverse=sentido == "desc") + sin_valor


def reporte_margen(conn, desde: str = "", hasta: str = "", agrupacion: str = "dia",
                   producto_id: int | None = None, orden: str = "margen",
                   sentido: str = "desc") -> dict:
    """Margen y rotación del rango: `{resumen, productos, periodos}`.

    `productos`: uno por producto vendido, con `unidades` (la rotación del rango),
    `unidades_por_dia` (sólo con `desde` y `hasta`), ingreso, costo, margen y `margen_pct`,
    ordenados por `orden`/`sentido` (`ORDENES`/`SENTIDOS`). `periodos`: lo mismo por día,
    semana o mes (`agrupacion`), del más viejo al más nuevo. `producto_id` deja sólo ese
    producto, en las tres partes (sus unidades por período son su rotación). Un producto
    del que se devolvió todo no aparece: no vendió nada.

    Levanta `ValueError` con una `agrupacion`, un `orden` o un `sentido` desconocidos.
    No commitea (no escribe)."""
    if agrupacion not in AGRUPACIONES:
        raise ValueError(f"agrupación desconocida: {agrupacion!r} (válidas: {', '.join(AGRUPACIONES)})")
    if orden not in ORDENES:
        raise ValueError(f"orden desconocido: {orden!r} (válidos: {', '.join(ORDENES)})")
    if sentido not in SENTIDOS:
        raise ValueError(f"sentido desconocido: {sentido!r} (válidos: {', '.join(SENTIDOS)})")

    por_producto: dict[int, _Acum] = {}
    nombres: dict[int, str] = {}
    por_periodo: dict[str, _Acum] = {}
    total = _Acum()
    for occurred_on, item_id, nombre, unidades, ingreso, costo, estimado, sin_costo in _lineas_netas(conn, desde, hasta):
        if producto_id is not None and item_id != producto_id:
            continue
        nombres[item_id] = nombre
        args = (unidades, ingreso, costo, estimado, sin_costo)
        _sumar(por_producto.setdefault(item_id, _Acum()), *args)
        _sumar(por_periodo.setdefault(_periodo(occurred_on, agrupacion), _Acum()), *args)
        _sumar(total, *args)

    dias = _dias(desde, hasta)
    productos = []
    for item_id, acum in por_producto.items():
        if acum.unidades <= 0:
            continue
        fila = _fila(acum)
        productos.append({
            "producto_id": item_id, "nombre": nombres[item_id], **fila,
            "unidades_por_dia": _redondear(acum.unidades / dias) if dias else None,
        })
    periodos = [
        {"periodo": clave, **_fila(acum)}
        for clave, acum in sorted(por_periodo.items()) if acum.unidades > 0
    ]
    resumen = {
        **_fila(total),
        "productos": len(productos),
        "productos_costo_estimado": sum(1 for p in productos if p["costo_estimado"]),
        "productos_sin_costo": sum(1 for p in productos if p["sin_costo"]),
        "dias": dias,
    }
    return {"resumen": resumen, "productos": _ordenar(productos, orden, sentido), "periodos": periodos}
