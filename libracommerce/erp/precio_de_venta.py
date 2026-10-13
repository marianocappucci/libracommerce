"""El precio de cada línea de una venta lo decide el servidor, no el cliente (ADR-049).

`POST /api/ventas` guardaba el `precio` que mandaba el punto de venta (ADR-048, «No cubre»): el motor ofrecía la cuenta
bien hecha (`listas_precio.precios_por_producto`) y el POS la pedía, pero nada impedía que un cliente armado a mano
—o una pantalla vieja con una lista que cambió— cobrara a otro precio. Con `OpcionesVentas.validar_precios` prendida,
`web.ventas_router.crear` llama a `validar_precios` ANTES de escribir nada y rechaza la venta si alguna línea no cuesta
lo que el servidor calcula.

La regla es **la del POS, repetida en el servidor**:

- Hay una lista predeterminada activa: el precio de la línea es el de `precios_por_producto` (el quiebre cuenta por
  producto, ADR-048), al momento de ahora y **con la sucursal que se pida** (`sucursal_id`, ADR-058; sin ella, el precio
  general, que es lo que cotizaba el POS cuando todavía no sabía de sucursales). Un precio propio de la sucursal gana
  sobre el general; sin precio propio, rige el general. Si la lista no tiene precio para ese producto, rige el precio
  plano (`catalog_items.default_sale_price`), igual que en la pantalla.
- No hay lista predeterminada activa: el precio plano. La variante no tiene precio propio (ADR-045).
- Una línea con `etiqueta` (el EAN-13 crudo de la balanza) se valida contra lo que dice la etiqueta, con el formato del
  comercio (`catalogo.get_formato_balanza`, la misma `commerce_settings` que usa `escanear`): **por importe**, el precio
  es el de la etiqueta y la cantidad es 1; **por peso**, la cantidad es el peso de la etiqueta y el precio, el de la
  lista (o el plano) por unidad de peso.

La tolerancia es de un centavo: el POS manda el precio como número y el servidor lo compara con el suyo.
"""

from __future__ import annotations

from datetime import UTC, datetime

from libracommerce.db.repository import repositorio_de
from libracommerce.domain.catalog import ItemCodeType
from libracommerce.domain.scale import ScaleValueKind, parse_scale_barcode

from . import catalogo
from . import listas_precio as lp

#: Cuánto puede diferir el precio que manda el cliente del que calcula el servidor. Un centavo: es la unidad de la plata.
TOLERANCIA = 0.01

#: Cuánto puede diferir la cantidad de una línea de balanza por peso de la que dice la etiqueta. Los gramos de la etiqueta
#: son enteros (`divisor` 1000 por defecto) y la cantidad viaja como número: sólo absorbe el redondeo del float.
_TOLERANCIA_DE_PESO = 1e-6


class ValidacionDePrecio(ValueError):
    """Un dato del pedido que reintentar no cura: el router lo traduce a 422. (Un precio distinto del vigente NO es
    esto: es un 409 con los precios esperados, para que el POS re-precie.)"""


class LineaSinProducto(ValidacionDePrecio):
    """Una línea sin `producto_id` (ítem libre): no hay de dónde sacar su precio."""


class EtiquetaInvalida(ValidacionDePrecio):
    """La `etiqueta` de balanza de una línea no se puede leer, no es de ese producto o no coincide con su cantidad."""


def _precios_planos(conn, producto_ids: set[int]) -> dict[int, float]:
    """`{producto_id: precio plano}` de los productos que existen. Los que no existen no figuran: el 422 de
    «No existe el producto» lo da `crear_venta_directa`, como siempre."""
    if not producto_ids:
        return {}
    ids = sorted(producto_ids)
    marcadores = ",".join("?" for _ in ids)
    filas = conn.execute(
        f"SELECT id, default_sale_price FROM catalog_items WHERE id IN ({marcadores})", ids).fetchall()
    return {f[0]: float(f[1]) for f in filas}


def _esperado_por_etiqueta(conn, linea: dict, esperado: float) -> float:
    """El precio que corresponde a una línea que trae etiqueta de balanza (`esperado` es el de lista o el plano).

    Raises:
        EtiquetaInvalida: la balanza está apagada, la etiqueta no es del formato, no es del producto de la línea, o la
            cantidad de la línea no es la que la etiqueta dice.
    """
    nombre = linea["nombre"]
    fmt = catalogo.get_formato_balanza(conn)
    leida = parse_scale_barcode(linea["etiqueta"], fmt) if fmt is not None else None
    if leida is None:
        raise EtiquetaInvalida(
            f"La etiqueta de balanza de «{nombre}» no se puede leer: no es del formato de la balanza del comercio "
            "(o la balanza no está configurada). Volvé a escanearla.")
    producto = repositorio_de(conn).find_item_by_code(leida.item_code, code_type=ItemCodeType.SCALE)
    if producto is None or producto.id != linea["producto_id"]:
        raise EtiquetaInvalida(
            f"La etiqueta de balanza de «{nombre}» es de otro producto: la línea y la etiqueta no coinciden.")
    valor = float(leida.value)
    if leida.kind is ScaleValueKind.AMOUNT:
        if abs(linea["qty"] - 1) > _TOLERANCIA_DE_PESO:
            raise EtiquetaInvalida(
                f"«{nombre}» viene de una etiqueta por importe: cada etiqueta es un paquete, la cantidad es 1. "
                "Escaneá otra etiqueta para llevar más.")
        return valor
    if abs(linea["qty"] - valor) > _TOLERANCIA_DE_PESO:
        raise EtiquetaInvalida(
            f"La cantidad de «{nombre}» ({linea['qty']:g}) no es la que dice la etiqueta ({valor:g}). "
            "Escaneá la etiqueta de nuevo.")
    return esperado


def validar_precios(conn, lineas: list[dict], *, en: str = "", sucursal_id: int | None = None) -> list[dict]:
    """Compara el precio de cada línea con el que calcula el servidor y devuelve las que no coinciden.

    `lineas` es `[{indice, nombre, producto_id, variante_id, qty, precio, etiqueta?}]`; `indice` es la posición de la
    línea en el pedido original (lo que el POS usa para saber a cuál corregir). `en` es el momento de la cotización (ISO;
    vacío = ahora). `sucursal_id` es la sucursal por la que se cotiza (ADR-058): con ella rige el precio propio de esa
    sucursal en la lista si lo hay y, si no, el general; `None` (el default) es el precio general de siempre. Devuelve `[]` si todas coinciden, o `[{indice, producto_id, variante_id, enviado, esperado}]`.
    **Lectura pura**: no escribe nada.

    Un producto que no existe no se valida (no hay precio que esperar): lo rechaza `crear_venta_directa` con su 422.

    Raises:
        LineaSinProducto: hay una línea sin `producto_id`.
        EtiquetaInvalida: la etiqueta de balanza de una línea no vale (ver `_esperado_por_etiqueta`).
        ValueError: una variante que no es del producto de su línea.
    """
    for linea in lineas:
        if linea["producto_id"] is None:
            raise LineaSinProducto(
                f"«{linea['nombre']}» no es un producto del catálogo: cada línea de la venta tiene que llevar su "
                "`producto_id` para que el servidor pueda validar el precio.")
    planos = _precios_planos(conn, {linea["producto_id"] for linea in lineas})
    conocidas = [linea for linea in lineas if linea["producto_id"] in planos]

    de_lista: dict[int, float | None] = {}
    lista_id = lp.lista_predeterminada_activa(conn)
    if lista_id is not None and conocidas:
        cotizadas = lp.precios_por_producto(
            conn, lista_id,
            [{"producto_id": c["producto_id"], "variante_id": c["variante_id"], "cantidad": c["qty"]} for c in conocidas],
            # La misma sucursal por la que cotiza el POS (ADR-049: lo que el POS cotiza, el servidor lo valida; ADR-058: ahora con sucursal).
            sucursal_id=sucursal_id, en=en or datetime.now(UTC).isoformat(),
        )
        de_lista = {linea["indice"]: cotizada["precio"] for linea, cotizada in zip(conocidas, cotizadas, strict=True)}

    distintos = []
    for linea in conocidas:
        esperado = de_lista.get(linea["indice"])
        if esperado is None:
            esperado = planos[linea["producto_id"]]
        if linea.get("etiqueta"):
            esperado = _esperado_por_etiqueta(conn, linea, esperado)
        if abs(linea["precio"] - esperado) > TOLERANCIA + 1e-9:
            distintos.append({
                "indice": linea["indice"], "producto_id": linea["producto_id"], "variante_id": linea["variante_id"],
                "enviado": linea["precio"], "esperado": esperado,
            })
    return distintos
