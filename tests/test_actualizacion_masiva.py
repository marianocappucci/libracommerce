"""`erp.actualizacion_masiva`: el cálculo del margen, sin la capa HTTP.

El caso que manda es el producto sin costo previo (recién dado de alta,
`precio_costo=0`): no hay margen del que partir, así que el precio de venta
no se toca -- calcular `0 * algo` daría un venta_nueva de 0, que vaciaría el
precio de un producto que sí se vende.
"""

from __future__ import annotations

import pytest

from libracommerce.erp import actualizacion_masiva as am
from libracommerce.erp import catalogo as erp_catalogo


@pytest.fixture
def conn(abrir):
    with abrir() as c:
        yield c


def _crear(conn, nombre, codigo, venta, costo):
    return erp_catalogo.create_producto(conn, nombre=nombre, codigo=codigo, precio_venta=venta, precio_costo=costo)


def test_manteniene_el_margen_al_recalcular_la_venta(conn):
    _crear(conn, "Yerba", "111", venta=1500, costo=1000)  # margen 1.5x
    resultado = am.calcular(conn, [{"codigo": "111", "costo": 1200}])
    assert len(resultado.actualizaciones) == 1
    linea = resultado.actualizaciones[0]
    assert linea.venta_nueva == 1800.0
    assert linea.margen_calculado is True


def test_sin_costo_previo_no_toca_la_venta(conn):
    """Producto recién creado con costo en 0: no hay margen posible."""
    _crear(conn, "Fideos", "222", venta=800, costo=0)
    resultado = am.calcular(conn, [{"codigo": "222", "costo": 500}])
    linea = resultado.actualizaciones[0]
    assert linea.venta_nueva == 800.0  # sin cambios
    assert linea.margen_calculado is False


def test_un_codigo_sin_producto_va_a_no_encontrados(conn):
    resultado = am.calcular(conn, [{"codigo": "999", "costo": 100}])
    assert resultado.actualizaciones == []
    assert resultado.no_encontrados[0].codigo == "999"


def test_un_codigo_repetido_en_la_planilla_se_procesa_una_sola_vez(conn):
    _crear(conn, "Arroz", "333", venta=1000, costo=800)
    resultado = am.calcular(conn, [
        {"codigo": "333", "costo": 900},
        {"codigo": "333", "costo": 950},  # se ignora: ya se vio ese código
    ])
    assert len(resultado.actualizaciones) == 1
    assert resultado.actualizaciones[0].costo_nuevo == 900.0


def test_calcular_no_escribe_nada(conn):
    pid = _crear(conn, "Aceite", "444", venta=2000, costo=1500)
    am.calcular(conn, [{"codigo": "444", "costo": 1600}])
    assert erp_catalogo.get_producto(conn, pid)["precio_costo"] == 1500.0


def test_aplicar_escribe_lo_que_calculo_calcular(conn):
    pid = _crear(conn, "Aceite", "555", venta=2000, costo=1500)
    resultado = am.calcular(conn, [{"codigo": "555", "costo": 1650}])
    aplicadas = am.aplicar(conn, resultado.actualizaciones)
    assert aplicadas == 1
    actualizado = erp_catalogo.get_producto(conn, pid)
    assert actualizado["precio_costo"] == 1650.0
    assert actualizado["precio_venta"] == pytest.approx(2200.0)


def test_aplicar_no_pisa_el_nombre_ni_el_codigo(conn):
    """`aplicar` sólo cambia costo y venta -- el resto del producto queda igual."""
    pid = _crear(conn, "Sal", "666", venta=500, costo=300)
    resultado = am.calcular(conn, [{"codigo": "666", "costo": 350}])
    am.aplicar(conn, resultado.actualizaciones)
    actualizado = erp_catalogo.get_producto(conn, pid)
    assert actualizado["nombre"] == "Sal"
    assert actualizado["codigo"] == "666"
