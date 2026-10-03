"""Actualización masiva de precios desde la planilla de precios de un
proveedor (roadmap de producto de VentaLibra, ver
wiki/analyses/ventalibra-gaps-despensa.md).

Cada fila de la planilla trae un código (de barra u otro ya cargado en el
producto) y un costo nuevo. El precio de venta **se recalcula solo**,
manteniendo el margen que el producto ya tenía
(`costo_nuevo * (venta_actual / costo_actual)`) — decisión del humano
(2026-09-28): no todos los proveedores mandan el precio de venta sugerido, y
lo que importa conservar es el margen que el dueño ya venía aplicando, no uno
que la planilla eventualmente traiga.

Reutiliza `catalogo.get_producto_by_codigo`/`update_producto`: para el motor,
una actualización masiva es una fila de `PUT /api/productos/{id}` por
producto — no un camino paralelo con su propia noción de "producto".

`calcular` no escribe nada: es la base tanto de la vista previa como de
`aplicar`, para que las dos vean EXACTAMENTE lo mismo (el cliente nunca manda
los precios ya calculados — sólo la planilla — así que no hay forma de que la
vista previa muestre un margen y se aplique otro).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import catalogo
from .reposicion import _bloquear_producto


@dataclass(frozen=True)
class LineaActualizada:
    item_id: int
    codigo: str
    nombre: str
    costo_actual: float
    costo_nuevo: float
    venta_actual: float
    venta_nueva: float
    #: `False` cuando `costo_actual` era 0: no hay margen del que partir, así
    #: que el precio de venta no se toca (se muestra igual a `venta_actual`).
    margen_calculado: bool


@dataclass(frozen=True)
class LineaNoEncontrada:
    codigo: str
    motivo: str


@dataclass
class ResultadoActualizacion:
    actualizaciones: list[LineaActualizada] = field(default_factory=list)
    no_encontrados: list[LineaNoEncontrada] = field(default_factory=list)


def calcular(conn, filas: list[dict[str, Any]]) -> ResultadoActualizacion:
    """`filas`: `[{"codigo": str, "costo": float}, ...]`, ya parseadas y
    validadas (números positivos) por quien lee la planilla. Una fila con un
    código repetido se procesa una sola vez, la primera."""
    resultado = ResultadoActualizacion()
    vistos: set[str] = set()
    for f in filas:
        codigo = str(f["codigo"]).strip()
        if not codigo or codigo in vistos:
            continue
        vistos.add(codigo)
        producto = catalogo.get_producto_by_codigo(conn, codigo)
        if not producto:
            resultado.no_encontrados.append(
                LineaNoEncontrada(codigo=codigo, motivo="Ningún producto activo tiene este código.")
            )
            continue
        costo_actual = float(producto["precio_costo"])
        venta_actual = float(producto["precio_venta"])
        costo_nuevo = float(f["costo"])
        if costo_actual > 0:
            venta_nueva = round(costo_nuevo * (venta_actual / costo_actual), 2)
            margen_calculado = True
        else:
            venta_nueva = venta_actual
            margen_calculado = False
        resultado.actualizaciones.append(LineaActualizada(
            item_id=producto["id"], codigo=codigo, nombre=producto["nombre"],
            costo_actual=costo_actual, costo_nuevo=costo_nuevo,
            venta_actual=venta_actual, venta_nueva=venta_nueva,
            margen_calculado=margen_calculado,
        ))
    return resultado


def aplicar(conn, actualizaciones: list[LineaActualizada]) -> int:
    """Escribe cada línea con `catalogo.update_producto` -- lo mismo que
    editar el producto a mano, una vez por línea. Devuelve cuántas se
    aplicaron (un producto borrado entre el cálculo y acá se salta, no
    revienta el resto de la tanda).

    **Relee el producto DENTRO del candado** (ADR-027): `update_producto` reescribe todos los campos del producto (también el mínimo global) con lo que se le
    pasa, y acá eso sale de una relectura. Si la relectura fuera antes del candado, una edición que otro confirma entre ella y la escritura se pisaría con el valor
    de antes (lost update). Por eso se toma `_bloquear_producto` ANTES de leer; `update_producto` lo vuelve a tomar (la misma transacción, no espera) y el
    repositorio confirma al guardar, que es lo que lo suelta: releer y escribir quedan en una sola sección crítica, línea por línea. Siempre producto primero, el
    orden de `delete_producto`, `fijar_parametros`, `fijar_minimo_sucursal` y `update_producto`; la tanda no retiene el candado de una línea al pasar a la
    siguiente (cada línea confirma al guardar)."""
    aplicadas = 0
    for linea in actualizaciones:
        _bloquear_producto(conn, linea.item_id)   # ANTES de leer lo que se va a volver a escribir
        producto = catalogo.get_producto(conn, linea.item_id)
        if not producto:
            continue
        catalogo.update_producto(
            conn, pid=linea.item_id, nombre=producto["nombre"], codigo=producto["codigo"],
            descripcion=producto["descripcion"], precio_venta=linea.venta_nueva,
            precio_costo=linea.costo_nuevo, unidad=producto["unidad"],
            categoria=producto["categoria"] or "", activo=producto["activo"],
            stock_minimo=producto["stock_minimo"], estacion=producto["estacion"],
            vendible=producto["vendible"], tipo=producto["tipo"],
            permite_fraccion=producto["permite_fraccion"],
        )
        aplicadas += 1
    return aplicadas
