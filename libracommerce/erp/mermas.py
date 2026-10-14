"""Mermas por período, valorizadas: sólo lectura (ADR-059, 2026-10-14).

Cuánto se perdió, en unidades y en plata. Se junta lo que ya está en el ledger: cada movimiento de tipo `merma` (`movement_type = 'waste'`, cantidad
negativa) con el costo unitario que se estampó al mermar (`stock_movements.unit_cost`, ver `erp.stock.add_movimiento_stock`). No escribe nada ni toca el
schema.

🔴 **Qué cuenta como merma.** Todo `waste` negativo, venga por donde venga: el modo «Merma» del ajuste de stock, la baja de un lote por vencimiento
(`erp.vencimientos.dar_de_baja_lote`) y la devolución de un perecedero, que va a merma (`erp.ventas.devolver_items`, ADR-018: ahí la mercadería volvió
y se tiró, así que es plata perdida igual). No se separan por origen.

🔴 **El importe es un piso, no un total.** Una merma vale `|cantidad| × unit_cost`. Si el producto no tenía costo cargado al mermar, o la merma es anterior a
ADR-059 (no se reconstruye: el costo de entonces no está en ningún lado), `unit_cost` es NULL y esa merma **no suma al importe**: se cuenta aparte
(`movimientos_sin_costo`, `cantidad_sin_costo`) para que la pantalla avise que el importe no está completo en vez de mostrarlo como si lo estuviera.
Un 0 nunca se guarda como costo (significa «nadie lo cargó»).

**El rango es por día** (`desde` y `hasta` inclusive, como los demás reportes), sobre `occurred_at`. **Los importes se calculan exactos (`Decimal`) y se
redondean a 2 decimales al final**; los campos se llaman con `costo` a propósito (`costo_total`, `movimientos_sin_costo`, `cantidad_sin_costo`): es la clave
que un producto filtra para quien no puede ver costos (VentaLibra, `SinCostos`). El motor no decide quién ve el reporte.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from .stock import IMPORTE_DECIMALES

CERO = Decimal("0")


def _redondear(valor: Decimal) -> float:
    return float(valor.quantize(Decimal(1).scaleb(-IMPORTE_DECIMALES), rounding=ROUND_HALF_UP))


def _num(valor: Decimal):
    """Un `int` si es entero, un `float` si no (`5` y no `5.0`)."""
    valor = round(valor, 10)
    return int(valor) if valor == valor.to_integral_value() else float(valor)


def _fecha(nombre: str, texto: str) -> str:
    try:
        return datetime.date.fromisoformat(str(texto)[:10]).isoformat()
    except ValueError:
        raise ValueError(f"{nombre} tiene que ser una fecha AAAA-MM-DD: {texto!r}") from None


@dataclass
class _Acum:
    cantidad: Decimal = CERO
    costo_total: Decimal = CERO
    movimientos: int = 0
    movimientos_sin_costo: int = 0
    cantidad_sin_costo: Decimal = CERO
    datos: dict = field(default_factory=dict)

    def sumar(self, cantidad: Decimal, costo: Decimal | None) -> None:
        self.cantidad += cantidad
        self.movimientos += 1
        if costo is None:
            self.movimientos_sin_costo += 1
            self.cantidad_sin_costo += cantidad
        else:
            self.costo_total += cantidad * costo

    def fila(self) -> dict:
        return {"cantidad": _num(self.cantidad), "costo_total": _redondear(self.costo_total), "movimientos": self.movimientos,
                "movimientos_sin_costo": self.movimientos_sin_costo, "cantidad_sin_costo": _num(self.cantidad_sin_costo),
                "sin_costo": self.movimientos_sin_costo > 0}


def reporte_mermas(conn, desde: str, hasta: str, *, deposito_id: int | None = None, producto_id: int | None = None) -> dict:
    """Las mermas de `desde` a `hasta` (ambos inclusive, `AAAA-MM-DD`): `{resumen, productos}`.

    `productos`: uno por producto con mermas en el rango, con `producto_id`, `codigo`, `nombre`, `unidad`, `cantidad` (unidades mermadas), `costo_total`
    (la suma de `|cantidad| × costo` de las mermas **que tienen costo**), `movimientos`, `movimientos_sin_costo` y `cantidad_sin_costo` (lo que se mermó sin costo
    y por eso no está en `costo_total`) y `sin_costo` (si tiene alguna). Ordenados por `costo_total` descendente, después por `cantidad` y por nombre.
    `resumen`: lo mismo sumado, más `productos` y `productos_sin_costo`.

    `deposito_id` deja sólo las mermas de ese depósito y `producto_id` las de ese producto. `ValueError` con una fecha ilegible, `desde` posterior a `hasta`, o
    un depósito que no existe. No commitea (no escribe)."""
    desde, hasta = _fecha("desde", desde), _fecha("hasta", hasta)
    if desde > hasta:
        raise ValueError(f"desde ({desde}) no puede ser posterior a hasta ({hasta})")
    if deposito_id is not None and not conn.execute("SELECT 1 FROM locations WHERE id = ?", (deposito_id,)).fetchall():
        raise ValueError(f"el depósito {deposito_id} no existe")
    donde = ["sm.movement_type = 'waste'", "sm.quantity_delta < 0", "substr(sm.occurred_at, 1, 10) >= ?",
             "substr(sm.occurred_at, 1, 10) <= ?"]
    params: list = [desde, hasta]
    if deposito_id is not None:
        donde.append("sm.location_id = ?")
        params.append(deposito_id)
    if producto_id is not None:
        donde.append("sm.item_id = ?")
        params.append(producto_id)
    filas = conn.execute(
        f"""SELECT sm.item_id, sm.quantity_delta, sm.unit_cost, ci.name, ci.unit_code, ic.code AS codigo
            FROM stock_movements sm
            JOIN catalog_items ci ON ci.id = sm.item_id
            LEFT JOIN item_codes ic ON ic.item_id = ci.id AND ic.is_primary = 1
            WHERE {' AND '.join(donde)}""",
        params,
    ).fetchall()
    por_producto: dict[int, _Acum] = {}
    total = _Acum()
    for f in filas:
        cantidad = abs(Decimal(str(f["quantity_delta"])))
        costo = Decimal(str(f["unit_cost"])) if f["unit_cost"] is not None else None
        acum = por_producto.setdefault(f["item_id"], _Acum(datos={"codigo": f["codigo"], "nombre": f["name"], "unidad": f["unit_code"]}))
        acum.sumar(cantidad, costo)
        total.sumar(cantidad, costo)
    productos = [{"producto_id": item_id, **a.datos, **a.fila()} for item_id, a in por_producto.items()]
    productos.sort(key=lambda p: (-p["costo_total"], -p["cantidad"], p["nombre"].casefold(), p["producto_id"]))
    resumen = {**total.fila(), "productos": len(productos), "productos_sin_costo": sum(1 for p in productos if p["sin_costo"])}
    return {"resumen": resumen, "productos": productos}
