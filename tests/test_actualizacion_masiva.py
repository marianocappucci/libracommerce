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


# ── Concurrencia: `aplicar` relee el producto DENTRO del candado (ADR-027) ──


def test_aplicar_no_pisa_un_minimo_editado_entre_la_relectura_y_la_escritura(abrir, monkeypatch):
    """`update_producto` reescribe todos los campos con lo que se le pasa, y `aplicar` los saca de una relectura. Con la relectura FUERA del candado, un mínimo que otro
    hilo confirma después de ella se pisa con el valor de antes (lost update). Barrera determinista: el hilo de la masiva, apenas relee el producto, avisa y espera a que
    el otro hilo edite el mínimo de 5 a 9 y confirme; ese otro escribe sólo el mínimo (toma el candado del producto y confirma, como lo haría cualquier escritura del
    mínimo). Sin el arreglo, el otro termina enseguida y la masiva escribe el 5 de antes: mínimo final 5. Con el arreglo, la relectura ocurre con el candado tomado: el
    otro espera (la espera de la masiva se agota, no hay forma de que haya editado) y recién cuando la masiva confirma escribe su 9. El mínimo final es 9 y el precio, el nuevo."""
    import threading

    from libracommerce.erp.reposicion import _bloquear_producto

    with abrir() as conn:
        pid = erp_catalogo.create_producto(conn, nombre="Yerba", codigo="111", precio_venta=1500, precio_costo=1000, stock_minimo=5)
        resultado = am.calcular(conn, [{"codigo": "111", "costo": 1200}])
    leido, editado = threading.Event(), threading.Event()
    salida: dict[str, str] = {}
    original = erp_catalogo.get_producto

    def releer_y_esperar(conn, producto_id):
        producto = original(conn, producto_id)
        if producto_id == pid and not leido.is_set():
            leido.set()
            editado.wait(timeout=3)   # sin candado, el otro hilo edita ahora; con él, está esperando y el plazo se agota
        return producto

    monkeypatch.setattr(erp_catalogo, "get_producto", releer_y_esperar)

    def editar_el_minimo():
        try:
            assert leido.wait(timeout=30)
            with abrir() as conn:
                _bloquear_producto(conn, pid)
                conn.execute("UPDATE catalog_items SET min_stock = ? WHERE id = ?", ("9", pid))
                conn.commit()
            salida["minimo"] = "confirmado"
        except Exception as exc:  # noqa: BLE001 - se informa abajo
            salida["minimo"] = f"error: {exc!r}"
        finally:
            editado.set()

    hilo = threading.Thread(target=editar_el_minimo)
    hilo.start()
    with abrir() as conn:
        assert am.aplicar(conn, resultado.actualizaciones) == 1
    hilo.join(timeout=60)
    assert salida == {"minimo": "confirmado"}, salida
    with abrir() as conn:
        final = erp_catalogo.get_producto(conn, pid)
    assert final["stock_minimo"] == 9.0, f"la masiva pisó el mínimo editado con el valor de antes: {final['stock_minimo']}"
    assert (final["precio_costo"], final["precio_venta"]) == (1200.0, pytest.approx(1800.0))
