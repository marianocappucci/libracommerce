"""La cuenta del reintegro de una devolución, sola (`domain.sales.reintegro_prorrateado`, ADR-040).

Antes la ejercitaban los tests de `usecases.sales.return_sale_items` (ADR-043), que se retiró en ADR-053 por no tener ningún
llamador. La cuenta sigue viva —`erp.ventas.devolver_items` la usa— y estos tests son los que la cubren a nivel dominio, con los
mismos números. Los de `devolver_items` (HTTP, caja, stock) están en `test_erp_ventas.py`."""
from __future__ import annotations

from decimal import Decimal

import pytest

from libracommerce.domain.sales import LineaReintegro, reintegro_prorrateado

D = Decimal


def _devolver_de_a_una(lineas, descuento_venta, pedidos) -> list[Decimal]:
    """`pedidos`: `[(clave, cantidad), ...]`, cada uno con lo ya devuelto por los anteriores."""
    ya: dict = {}
    importes = []
    for clave, cantidad in pedidos:
        importes.append(reintegro_prorrateado(lineas, D(descuento_venta), dict(ya), {clave: D(cantidad)}))
        ya[clave] = ya.get(clave, D(0)) + D(cantidad)
    return importes


def test_promocion_2x1_reintegra_lo_pagado_y_no_el_precio_de_lista():
    """🔴 El caso medido en la demo: 2 x $1.600 con 2x1 (descuento $1.600) cobran $1.600. Devolver 1 reintegra $800."""
    lineas = [LineaReintegro("p", D(2), D(1600))]
    assert reintegro_prorrateado(lineas, D(1600), {}, {"p": D(1)}) == D(800)
    assert _devolver_de_a_una(lineas, 1600, [("p", 1), ("p", 1)]) == [D(800), D(800)]


def test_descuento_de_la_linea_se_prorratea_en_la_devolucion():
    """3 x $100 con $30 de descuento en la línea cobran $270: cada unidad vale $90."""
    lineas = [LineaReintegro("p", D(3), D(100), D(30))]
    assert reintegro_prorrateado(lineas, D(0), {}, {"p": D(1)}) == D(90)


def test_descuento_general_se_reparte_entre_las_lineas():
    """Dos líneas ($1.000 y $500) con 10% general ($150): se cobra $1.350. La primera vale 900 y la segunda 450."""
    lineas = [LineaReintegro("a", D(2), D(500)), LineaReintegro("b", D(1), D(500))]
    importes = _devolver_de_a_una(lineas, 150, [("a", 2), ("b", 1)])
    assert importes == [D(900), D(450)]
    assert sum(importes, D(0)) == D(1350)


def test_descuento_en_la_linea_y_en_el_total_es_el_mismo_dinero():
    """El descuento puede estar en la línea Y en `discount_total`: no se descuenta dos veces. 2 x $100 con $20 en la línea y
    $50 de descuento de la venta (los $20 incluidos) cobran $150, no $130."""
    lineas = [LineaReintegro("p", D(2), D(100), D(20))]
    assert _devolver_de_a_una(lineas, 50, [("p", 1), ("p", 1)]) == [D(75), D(75)]


def test_las_devoluciones_sucesivas_no_derivan_centavos():
    """3 x $100 con $10 de descuento cobran $290. Redondear cada devolución por separado daba 96,67 x 3 = 290,01; la diferencia de
    acumulados da 96,67 + 96,66 + 96,67 = 290,00."""
    lineas = [LineaReintegro("p", D(3), D(100))]
    importes = _devolver_de_a_una(lineas, 10, [("p", 1), ("p", 1), ("p", 1)])
    assert importes == [D("96.67"), D("96.66"), D("96.67")]
    assert sum(importes, D(0)) == D("290.00")


def test_una_devolucion_parcial_con_descuento_y_el_resto_despues_suma_lo_cobrado():
    """5 x $100 con $33 de descuento cobran $467: 2 unidades reintegran $186,80 y las 3 restantes, el resto exacto."""
    lineas = [LineaReintegro("p", D(5), D(100))]
    importes = _devolver_de_a_una(lineas, 33, [("p", 2), ("p", 3)])
    assert importes[0] == D("186.80")
    assert sum(importes, D(0)) == D("467.00")


def test_la_linea_de_servicio_entra_en_el_prorrateo_pero_no_se_devuelve():
    """Yerba 2 x $100 y un Envío de $100 con $30 de descuento general: se cobra $270 y el descuento se reparte también sobre el
    envío. Devolver las 2 yerbas reintegra $180; los $90 del envío no vuelven."""
    lineas = [LineaReintegro("yerba", D(2), D(100)), LineaReintegro(None, D(1), D(100))]
    assert _devolver_de_a_una(lineas, 30, [("yerba", 1), ("yerba", 1)]) == [D(90), D(90)]


def test_sin_descuentos_el_importe_es_cantidad_por_precio():
    assert reintegro_prorrateado([LineaReintegro("p", D(7), D("33.33"))], D(0), {}, {"p": D(3)}) == D("99.99")


def test_dos_lineas_del_mismo_producto_a_distinto_precio_se_valuan_cada_una_si_la_clave_las_distingue():
    """Con la posición de la línea como clave cada una vale lo suyo; con la clave `(ítem, variante)` (el ledger del ERP no dice de qué
    línea volvió lo devuelto) se promedian: es el límite que documenta ADR-040."""
    lineas_por_posicion = [LineaReintegro(0, D(1), D(100)), LineaReintegro(1, D(1), D(300))]
    assert _devolver_de_a_una(lineas_por_posicion, 0, [(1, 1), (0, 1)]) == [D(300), D(100)]
    lineas_por_item = [LineaReintegro("p", D(1), D(100)), LineaReintegro("p", D(1), D(300))]
    assert _devolver_de_a_una(lineas_por_item, 0, [("p", 1), ("p", 1)]) == [D(200), D(200)]


def test_no_se_reintegra_de_mas_que_lo_cobrado():
    lineas = [LineaReintegro("p", D(2), D(100))]
    with pytest.raises(ValueError, match="superaría lo cobrado"):
        reintegro_prorrateado(lineas, D(0), {"p": D(2)}, {"p": D(1)})


def test_una_venta_sin_valor_no_reintegra_nada():
    assert reintegro_prorrateado([LineaReintegro("p", D(1), D(0))], D(0), {}, {"p": D(1)}) == D(0)
