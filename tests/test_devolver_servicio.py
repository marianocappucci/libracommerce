"""Devolver un servicio del catálogo no escribe stock (ADR-054), contra los dos motores.

Una línea con un servicio DEL CATÁLOGO queda en `sale_items` como `kind='product'` y la venta no escribe stock (`_es_servicio`). Hasta v0.58
`devolver_items` la trataba como producto y escribía `+q` de `devolucion` sobre un ítem que no tiene inventario. Ahora reintegra el importe
prorrateado, baja el tope por (ítem, variante), cuenta para el estado de la venta y respeta la `clave_operacion`, pero **no toca `stock_movements`**:
lo devuelto queda en `sale_service_returns` (revisión `0007`).

Lo que fija: el ledger vacío y el servicio sin stock; el tope y el estado (`devuelta_parcial` / `devuelta`) en una venta de sólo servicio y en una
mixta (servicio + producto), donde el estado final sólo llega a `devuelta` cuando volvió TODO; la idempotencia por clave; que una venta con un
servicio devuelto no se anule; que el margen reste el servicio devuelto; el 503 de una base sin la revisión (y que un producto se siga devolviendo
ahí); el router; y la revisión `0007` (datos previos, idempotencia y bajada).
"""

from __future__ import annotations

import pytest
import test_ledger_sin_marcar as _led
import test_vencimientos as _vto
from alembic import command
from conftest import _usuario
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_ledger_sin_marcar import (
    FECHA_VENTA,
    _depositos,
    _devolver,
    _ledger,
    _linea,
    _lineas,
    _marca,
    _producto,
    _stock,
    _vender,
)
from test_vencimientos import _con_conexion, _crear_base_al_dia_de_0001, catalogo, migrar, stock

from libracommerce.erp import claves, margen, ventas
from libracommerce.web.ventas_router import OpcionesVentas, build_ventas_router

# Las fixtures de la red de seguridad del ledger (una base por motor con los dos schemas, la cadena entera y la fecha fija de `devolver_items`).
abrir_ledger = _led.abrir_ledger
hoy_fijo = _led.hoy_fijo
destino = _vto.destino


def _servicio(abrir, nombre="Flete", precio=500.0):
    with abrir() as conn:
        pid = catalogo.create_producto(conn, nombre, precio_venta=precio, tipo="servicio")
        conn.commit()
    return pid


def _devueltos(abrir, vid) -> list[tuple]:
    """Lo registrado en `sale_service_returns` de la venta: `(item, variante, depósito, cantidad, nota)`."""
    with abrir() as conn:
        return [(f[0], f[1], f[2], float(f[3]), f[4]) for f in conn.execute(
            "SELECT item_id, variant_id, location_id, quantity, note FROM sale_service_returns WHERE sale_id = ? ORDER BY id", (vid,))]


def _estado(abrir, vid) -> str:
    with abrir() as conn:
        return ventas.obtener_venta(conn, vid)["estado"]


# ═════════════════════════════════════════════════ Una venta de sólo servicio ══


def test_devolver_un_servicio_reintegra_el_importe_baja_el_tope_y_no_escribe_stock(abrir_ledger):
    servicio = _servicio(abrir_ledger)
    dep, _ = _depositos(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(servicio, 2, precio=500.0)])
    (linea,) = _lineas(abrir_ledger, vid)
    marca = _marca(abrir_ledger)

    primera = _devolver(abrir_ledger, vid, {linea: 1.0}, dep, usuario_id=7)

    assert primera["importe"] == 500.0 and primera["repetida"] is False
    assert primera["venta"]["estado"] == "devuelta_parcial"           # 1 de 2: queda la mitad
    with abrir_ledger() as conn:
        assert _ledger(conn, marca) == []                              # NADA en el ledger
    assert _stock(abrir_ledger, servicio) == 0.0                       # y el servicio sigue sin stock
    assert _devueltos(abrir_ledger, vid) == [(servicio, None, dep, 1.0, f"Devolución venta ID {vid}")]

    segunda = _devolver(abrir_ledger, vid, {linea: 1.0}, dep, usuario_id=7)
    assert segunda["importe"] == 500.0
    assert segunda["venta"]["estado"] == "devuelta"                    # el rastro de la primera cuenta: ya volvió todo
    with abrir_ledger() as conn:
        assert _ledger(conn, marca) == []
    assert _stock(abrir_ledger, servicio) == 0.0
    assert [d[3] for d in _devueltos(abrir_ledger, vid)] == [1.0, 1.0]


def test_el_tope_de_un_servicio_baja_con_lo_ya_devuelto(abrir_ledger):
    """Sin el rastro de lo devuelto (el ledger no lo tiene) se podía devolver la misma línea una y otra vez, cobrando de más."""
    servicio = _servicio(abrir_ledger)
    dep, _ = _depositos(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(servicio, 3, precio=500.0)])
    (linea,) = _lineas(abrir_ledger, vid)
    _devolver(abrir_ledger, vid, {linea: 2.0}, dep, usuario_id=7)

    with pytest.raises(ValueError, match="quedan 1.0 sin devolver"):
        _devolver(abrir_ledger, vid, {linea: 2.0}, dep, usuario_id=7)
    assert [d[3] for d in _devueltos(abrir_ledger, vid)] == [2.0]      # el rechazo no escribió nada
    assert _devolver(abrir_ledger, vid, {linea: 1.0}, dep, usuario_id=7)["venta"]["estado"] == "devuelta"
    with pytest.raises(ValueError, match="estado 'devuelta'"):         # y ya con todo devuelto, la venta no admite más
        _devolver(abrir_ledger, vid, {linea: 1.0}, dep, usuario_id=7)


def test_devolver_de_a_poco_un_servicio_suma_exacto_lo_cobrado(abrir_ledger):
    """El reintegro prorrateado se calcula sobre lo ya devuelto: tres devoluciones de a 1 de un servicio de 3 suman lo cobrado, sin deriva."""
    servicio = _servicio(abrir_ledger, precio=100.0)
    dep, _ = _depositos(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(servicio, 3, precio=100.0)])
    (linea,) = _lineas(abrir_ledger, vid)
    importes = [_devolver(abrir_ledger, vid, {linea: 1.0}, dep, usuario_id=7)["importe"] for _ in range(3)]
    assert importes == [100.0, 100.0, 100.0]
    assert _estado(abrir_ledger, vid) == "devuelta"


# ═════════════════════════════════════════════════════ Una venta mixta ═══════


def test_venta_mixta_el_estado_cuenta_los_dos_y_solo_el_producto_toca_el_stock(abrir_ledger):
    """`vendido_total` = 2 (servicio) + 3 (producto) = 5. Devolver sólo el servicio es parcial; sólo al volver también el producto queda `devuelta`.
    El producto reponer su stock; el servicio, no."""
    servicio = _servicio(abrir_ledger)
    producto = _producto(abrir_ledger, "sin_marcar", "Yogur", 10)
    dep, _ = _depositos(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(servicio, 2, precio=500.0), _linea(producto, 3, precio=100.0)])
    l_servicio, l_producto = _lineas(abrir_ledger, vid)
    assert _stock(abrir_ledger, producto) == 7.0

    marca = _marca(abrir_ledger)
    solo_servicio = _devolver(abrir_ledger, vid, {l_servicio: 2.0}, dep, usuario_id=7)
    assert solo_servicio["importe"] == 1000.0 and solo_servicio["venta"]["estado"] == "devuelta_parcial"
    with abrir_ledger() as conn:
        assert _ledger(conn, marca) == []

    todo = _devolver(abrir_ledger, vid, {l_producto: 3.0}, dep, usuario_id=7)
    assert todo["importe"] == 300.0
    assert todo["venta"]["estado"] == "devuelta"                       # 2 (servicio, de antes) + 3 (producto) = 5 = vendido_total
    with abrir_ledger() as conn:
        filas = _ledger(conn, marca)
    assert [(f[0], f[1], f[3]) for f in filas] == [("return", 3.0, producto)]       # una sola fila: la del producto
    assert (_stock(abrir_ledger, producto), _stock(abrir_ledger, servicio)) == (10.0, 0.0)


def test_venta_mixta_en_una_sola_llamada_y_en_el_orden_inverso(abrir_ledger):
    """Producto primero y servicio después da lo mismo, y los dos en una llamada también."""
    servicio = _servicio(abrir_ledger)
    producto = _producto(abrir_ledger, "sin_marcar", "Yogur", 10)
    dep, _ = _depositos(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(servicio, 2, precio=500.0), _linea(producto, 3, precio=100.0)])
    l_servicio, l_producto = _lineas(abrir_ledger, vid)

    parcial = _devolver(abrir_ledger, vid, {l_producto: 3.0}, dep, usuario_id=7)
    assert parcial["venta"]["estado"] == "devuelta_parcial"            # quedan los 2 del servicio
    resto = _devolver(abrir_ledger, vid, {l_servicio: 2.0}, dep, usuario_id=7)
    assert resto["venta"]["estado"] == "devuelta"

    vid2 = _vender(abrir_ledger, [_linea(servicio, 2, precio=500.0), _linea(producto, 3, precio=100.0)])
    l_servicio2, l_producto2 = _lineas(abrir_ledger, vid2)
    junto = _devolver(abrir_ledger, vid2, {l_servicio2: 2.0, l_producto2: 3.0}, dep, usuario_id=7)
    assert junto["importe"] == 1300.0 and junto["venta"]["estado"] == "devuelta"


# ════════════════════════════════════════════════ clave_operacion, anulación, margen ══


def test_una_devolucion_de_servicio_con_clave_no_se_repite(abrir_ledger):
    servicio = _servicio(abrir_ledger)
    producto = _producto(abrir_ledger, "sin_marcar", "Yogur", 10)
    dep, norte = _depositos(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(servicio, 2, precio=500.0), _linea(producto, 3, precio=100.0)])
    l_servicio, l_producto = _lineas(abrir_ledger, vid)
    pedido = {l_servicio: 1.0, l_producto: 1.0}

    primera = _devolver(abrir_ledger, vid, pedido, dep, usuario_id=7, clave_operacion="intento-1")
    assert primera["repetida"] is False and primera["importe"] == 600.0
    marca = _marca(abrir_ledger)
    filas = _devueltos(abrir_ledger, vid)

    repetida = _devolver(abrir_ledger, vid, pedido, dep, usuario_id=7, clave_operacion="intento-1")
    assert repetida["repetida"] is True and repetida["importe"] == 600.0
    assert _marca(abrir_ledger) == marca and _devueltos(abrir_ledger, vid) == filas      # un reintento no escribe nada
    assert filas[0][4].endswith("[op:intento-1]")

    with pytest.raises(claves.ClaveReusada):                                              # la misma clave con otras cantidades
        _devolver(abrir_ledger, vid, {l_servicio: 2.0, l_producto: 1.0}, dep, usuario_id=7, clave_operacion="intento-1")
    with pytest.raises(claves.ClaveReusada):                                              # u otro depósito
        _devolver(abrir_ledger, vid, pedido, norte, usuario_id=7, clave_operacion="intento-1")
    assert _devueltos(abrir_ledger, vid) == filas


def test_el_reintento_de_una_devolucion_de_solo_servicio_con_clave_no_reintegra_dos_veces(abrir_ledger):
    """Sin el rastro del servicio, la clave no encontraba nada que repetir (el ledger está vacío) y el reintento reintegraba otra vez."""
    servicio = _servicio(abrir_ledger)
    dep, _ = _depositos(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(servicio, 2, precio=500.0)])
    (linea,) = _lineas(abrir_ledger, vid)
    primera = _devolver(abrir_ledger, vid, {linea: 1.0}, dep, usuario_id=7, clave_operacion="solo-servicio")
    segunda = _devolver(abrir_ledger, vid, {linea: 1.0}, dep, usuario_id=7, clave_operacion="solo-servicio")
    assert (primera["repetida"], segunda["repetida"], segunda["importe"]) == (False, True, 500.0)
    assert len(_devueltos(abrir_ledger, vid)) == 1
    with abrir_ledger() as conn:
        egresos = conn.execute("SELECT COUNT(*) FROM caja_movimientos WHERE referencia LIKE ?", (f"devolucion:venta:{vid}:%",)).fetchone()[0]
    assert egresos == 1


def test_una_venta_con_un_servicio_devuelto_no_se_anula(abrir_ledger):
    servicio = _servicio(abrir_ledger)
    dep, _ = _depositos(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(servicio, 2, precio=500.0)])
    (linea,) = _lineas(abrir_ledger, vid)
    _devolver(abrir_ledger, vid, {linea: 1.0}, dep, usuario_id=7)
    with abrir_ledger() as conn, pytest.raises(ventas.VentaConDevoluciones):
        ventas.anular_venta(conn, vid)


def test_el_margen_resta_el_servicio_devuelto(abrir_ledger):
    """El margen lee lo devuelto del ledger; el servicio no está ahí, así que lo lee de `sale_service_returns`."""
    servicio = _servicio(abrir_ledger, precio=500.0)
    dep, _ = _depositos(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(servicio, 2, precio=500.0)])
    (linea,) = _lineas(abrir_ledger, vid)

    def reporte():
        with abrir_ledger() as conn:
            return {p["producto_id"]: (p["unidades"], p["ingreso"]) for p in margen.reporte_margen(conn, FECHA_VENTA, FECHA_VENTA)["productos"]}

    assert reporte() == {servicio: (2.0, 1000.0)}
    _devolver(abrir_ledger, vid, {linea: 1.0}, dep, usuario_id=7)
    assert reporte() == {servicio: (1.0, 500.0)}
    _devolver(abrir_ledger, vid, {linea: 1.0}, dep, usuario_id=7)
    assert reporte() == {}                                              # devuelto entero: no vendió nada


# ═════════════════════════════════════════════ Una base sin la revisión 0007 ═══


def _sin_la_0007(abrir):
    with abrir() as conn:
        conn.execute("DROP TABLE sale_service_returns")
        conn.commit()


def test_sin_la_0007_devolver_un_servicio_pide_la_revision_y_no_escribe_nada(abrir_ledger):
    servicio = _servicio(abrir_ledger)
    producto = _producto(abrir_ledger, "sin_marcar", "Yogur", 10)
    dep, _ = _depositos(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(servicio, 2, precio=500.0), _linea(producto, 3, precio=100.0)])
    l_servicio, l_producto = _lineas(abrir_ledger, vid)
    _sin_la_0007(abrir_ledger)
    marca = _marca(abrir_ledger)
    with pytest.raises(ventas.SinRevision, match="0007_devoluciones_de_servicio"):
        _devolver(abrir_ledger, vid, {l_producto: 1.0, l_servicio: 1.0}, dep, usuario_id=7)
    assert _marca(abrir_ledger) == marca                                # ni el producto de la misma llamada: se pidió la revisión ANTES de escribir


def test_sin_la_0007_un_producto_se_sigue_devolviendo_igual_aunque_la_venta_tenga_un_servicio(abrir_ledger):
    servicio = _servicio(abrir_ledger)
    producto = _producto(abrir_ledger, "sin_marcar", "Yogur", 10)
    dep, _ = _depositos(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(servicio, 2, precio=500.0), _linea(producto, 3, precio=100.0)])
    _, l_producto = _lineas(abrir_ledger, vid)
    _sin_la_0007(abrir_ledger)
    resultado = _devolver(abrir_ledger, vid, {l_producto: 1.0}, dep, usuario_id=7)
    assert resultado["importe"] == 100.0 and resultado["venta"]["estado"] == "devuelta_parcial"
    assert _stock(abrir_ledger, producto) == 8.0


# ═══════════════════════════════════════════════════════════════ El router ═══


def _client(abrir) -> TestClient:
    app = FastAPI()
    app.include_router(build_ventas_router(conexion=abrir, usuario_actual=_usuario, opciones=OpcionesVentas()))
    return TestClient(app)


def test_post_devolver_un_servicio_es_200_con_el_estado_y_sin_tocar_el_ledger(abrir_ledger):
    servicio = _servicio(abrir_ledger)
    dep, _ = _depositos(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(servicio, 2, precio=500.0)])
    (linea,) = _lineas(abrir_ledger, vid)
    marca = _marca(abrir_ledger)
    r = _client(abrir_ledger).post(f"/api/ventas/{vid}/devolver", json={
        "lineas": [{"sale_item_id": linea, "cantidad": 2}], "deposito_id": dep, "clave_operacion": "http-1"})
    assert r.status_code == 200, r.text
    assert r.json()["estado"] == "devuelta" and r.json()["repetida"] is False
    assert _marca(abrir_ledger) == marca


def test_post_devolver_un_servicio_sin_la_0007_es_503_con_el_comando(abrir_ledger):
    servicio = _servicio(abrir_ledger)
    dep, _ = _depositos(abrir_ledger)
    vid = _vender(abrir_ledger, [_linea(servicio, 2, precio=500.0)])
    (linea,) = _lineas(abrir_ledger, vid)
    _sin_la_0007(abrir_ledger)
    r = _client(abrir_ledger).post(f"/api/ventas/{vid}/devolver", json={"lineas": [{"sale_item_id": linea, "cantidad": 1}], "deposito_id": dep})
    assert r.status_code == 503 and "libracommerce-migrar upgrade" in r.json()["detail"]
    assert _estado(abrir_ledger, vid) == "cobrada"                     # el rollback dejó la venta como estaba


# ═════════════════════════════════════════════════════ La revisión 0007 ════════

_ANTERIOR = "0006_clave_operacion_recepciones"
_COLUMNAS = {"id", "sale_id", "item_id", "variant_id", "location_id", "quantity", "note", "created_by", "occurred_at"}


def test_la_revision_0007_corre_sobre_datos_previos_sin_tocar_una_fila_y_es_idempotente(destino):
    _crear_base_al_dia_de_0001(destino)
    migrar.upgrade(destino, _ANTERIOR)

    def poblar(conn):
        yerba = catalogo.create_producto(conn, "Yerba", precio_venta=100.0, precio_costo=60.0)
        stock.ajustar_stock(conn, yerba, 40.0, "inicial", fecha="2026-09-01")
        conn.commit()
        assert not conn.execute("PRAGMA table_info(sale_service_returns)").fetchall()
        return _ledger(conn)

    antes = _con_conexion(destino, poblar)
    migrar.upgrade(destino)

    def verificar(conn):
        assert {f[1] for f in conn.execute("PRAGMA table_info(sale_service_returns)").fetchall()} == _COLUMNAS
        assert conn.execute("SELECT COUNT(*) FROM sale_service_returns").fetchone()[0] == 0          # nace vacía
        assert _ledger(conn) == antes
        return [f[0] for f in conn.execute("SELECT version_num FROM alembic_version_libracommerce").fetchall()]

    assert _con_conexion(destino, verificar) == [_vto._CABEZA]
    migrar.upgrade(destino)
    assert _con_conexion(destino, verificar) == [_vto._CABEZA]


def test_la_revision_0007_es_idempotente_por_introspeccion_aunque_la_tabla_ya_exista(destino):
    _crear_base_al_dia_de_0001(destino)
    migrar.upgrade(destino)
    migrar.stamp(destino, _ANTERIOR)
    migrar.upgrade(destino)                                            # la tabla ya está: no falla ni la pisa
    assert _con_conexion(destino, lambda c: [f[0] for f in c.execute(
        "SELECT version_num FROM alembic_version_libracommerce").fetchall()]) == [_vto._CABEZA]


def test_la_revision_0007_baja_la_tabla_sin_tocar_el_ledger(destino):
    _crear_base_al_dia_de_0001(destino)
    migrar.upgrade(destino)

    def poblar(conn):
        yerba = catalogo.create_producto(conn, "Yerba", precio_venta=100.0, precio_costo=60.0)
        stock.ajustar_stock(conn, yerba, 40.0, "inicial", fecha="2026-09-01")
        conn.commit()
        return _ledger(conn)

    antes = _con_conexion(destino, poblar)
    command.downgrade(migrar.configuracion(destino), _ANTERIOR)

    def verificar(conn):
        assert not conn.execute("PRAGMA table_info(sale_service_returns)").fetchall()
        assert _ledger(conn) == antes
        assert [f[0] for f in conn.execute("SELECT version_num FROM alembic_version_libracommerce").fetchall()] == [_ANTERIOR]

    _con_conexion(destino, verificar)
