"""Promociones (2026-09-28): "llevá N pagá M" y combos, el cálculo del ahorro, el
CRUD, y su aplicación al registrar una venta (`OpcionesVentas.promociones`).
Contra los dos motores (`abrir_ventas`)."""

from __future__ import annotations

import datetime
from datetime import timezone

import pytest
from conftest import USUARIO, _usuario
from fastapi import FastAPI
from fastapi.testclient import TestClient

from libracommerce.web.catalogo_router import build_productos_router
from libracommerce.web.listas_router import (
    build_listas_precio_router,
    build_precios_vigentes_router,
    build_quiebres_router,
)
from libracommerce.web.promociones_router import (
    build_promociones_calculo_router,
    build_promociones_router,
)
from libracommerce.web.ventas_router import OpcionesVentas, build_ventas_router

HOY = datetime.date.today().isoformat()


@pytest.fixture(autouse=True)
def _admin():
    USUARIO["role"] = "admin"
    yield
    USUARIO.pop("role", None)


def _app(abrir, opciones=None) -> TestClient:
    app = FastAPI()
    app.include_router(build_productos_router(conexion=abrir, usuario_actual=_usuario))
    app.include_router(build_listas_precio_router(conexion=abrir))
    app.include_router(build_quiebres_router(conexion=abrir))
    app.include_router(build_precios_vigentes_router(conexion=abrir))
    app.include_router(build_promociones_router(conexion=abrir))
    app.include_router(build_promociones_calculo_router(conexion=abrir))
    app.include_router(build_ventas_router(conexion=abrir, usuario_actual=_usuario, opciones=opciones))
    return TestClient(app)


@pytest.fixture
def client(abrir_ventas):
    return _app(abrir_ventas)


def _producto(client, nombre, precio=100.0):
    r = client.post("/api/productos", json={"nombre": nombre, "precio_venta": precio, "precio_costo": 1.0})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _nxm(client, pid, lleva=2, paga=1, **extra):
    body = {"nombre": f"{lleva}x{paga}", "tipo": "nxm", "paga": paga,
            "items": [{"producto_id": pid, "cantidad": lleva}], **extra}
    r = client.post("/api/promociones", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _combo(client, pids, precio, nombre="Combo", **extra):
    body = {"nombre": nombre, "tipo": "combo", "precio": precio,
            "items": [{"producto_id": p, "cantidad": 1} for p in pids], **extra}
    r = client.post("/api/promociones", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _calcular(client, lineas, **extra):
    r = client.post("/api/promociones/calcular", json={"items": lineas, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _linea(pid, qty, precio=100.0):
    return {"producto_id": pid, "qty": qty, "precio": precio}


# ── llevá N pagá M ───────────────────────────────────────────────────────

def test_2x1_ahorra_una_unidad_por_cada_par(client):
    pid = _producto(client, "Alfajor")
    _nxm(client, pid, lleva=2, paga=1)
    assert _calcular(client, [_linea(pid, 1)])["ahorro"] == 0
    r = _calcular(client, [_linea(pid, 2)])
    assert r["ahorro"] == 100.0
    assert r["aplicadas"][0]["veces"] == 1
    # 5 unidades = 2 pares completos y una suelta que se paga entera.
    assert _calcular(client, [_linea(pid, 5)])["ahorro"] == 200.0


def test_3x2_usa_el_precio_de_lista_de_la_linea(client):
    pid = _producto(client, "Gaseosa")
    _nxm(client, pid, lleva=3, paga=2)
    assert _calcular(client, [_linea(pid, 3, precio=250.0)])["ahorro"] == 250.0


def test_la_cantidad_fraccionaria_no_arma_un_paquete_incompleto(client):
    pid = _producto(client, "Queso")
    _nxm(client, pid, lleva=2, paga=1)
    assert _calcular(client, [_linea(pid, 1.5)])["ahorro"] == 0
    assert _calcular(client, [_linea(pid, 2.5)])["ahorro"] == 100.0


def test_varias_lineas_del_mismo_producto_se_juntan(client):
    pid = _producto(client, "Alfajor")
    _nxm(client, pid)
    assert _calcular(client, [_linea(pid, 1), _linea(pid, 1)])["ahorro"] == 100.0


def test_una_linea_sin_producto_no_participa(client):
    pid = _producto(client, "Alfajor")
    _nxm(client, pid)
    r = _calcular(client, [{"qty": 2, "precio": 100.0}, _linea(pid, 1)])
    assert r == {"aplicadas": [], "ahorro": 0}


# ── combos ───────────────────────────────────────────────────────────────

def test_combo_ahorra_la_diferencia_contra_la_suma_de_lista(client):
    a, b = _producto(client, "Hamburguesa", 500), _producto(client, "Papas", 300)
    _combo(client, [a, b], precio=650.0)
    r = _calcular(client, [_linea(a, 1, 500.0), _linea(b, 1, 300.0)])
    assert r["ahorro"] == 150.0
    assert _calcular(client, [_linea(a, 1, 500.0)])["ahorro"] == 0


def test_combo_dos_veces_con_lo_que_alcanza(client):
    a, b = _producto(client, "A", 500), _producto(client, "B", 300)
    _combo(client, [a, b], precio=650.0)
    r = _calcular(client, [_linea(a, 3, 500.0), _linea(b, 2, 300.0)])
    assert r["aplicadas"][0]["veces"] == 2
    assert r["ahorro"] == 300.0


def test_un_combo_que_no_ahorra_no_se_aplica(client):
    a, b = _producto(client, "A", 100), _producto(client, "B", 100)
    _combo(client, [a, b], precio=250.0)
    assert _calcular(client, [_linea(a, 1), _linea(b, 1)])["ahorro"] == 0


def test_cuando_compiten_gana_la_de_mayor_ahorro_y_consume_las_unidades(client):
    a, b = _producto(client, "A", 100), _producto(client, "B", 100)
    _nxm(client, a, lleva=2, paga=1)            # ahorra 100 por paquete, usa 2 A
    _combo(client, [a, b], precio=120.0)         # ahorra 80 por paquete
    r = _calcular(client, [_linea(a, 2), _linea(b, 1)])
    # El 2x1 se lleva las dos A; el combo ya no tiene A con qué armarse.
    assert [x["nombre"] for x in r["aplicadas"]] == ["2x1"]
    assert r["ahorro"] == 100.0


# ── vigencia ─────────────────────────────────────────────────────────────

def test_una_promocion_inactiva_o_fuera_de_fecha_no_aplica(client):
    pid = _producto(client, "Alfajor")
    _nxm(client, pid, activa=False)
    assert _calcular(client, [_linea(pid, 2)])["ahorro"] == 0
    _nxm(client, pid, desde="2099-01-01T00:00:00")
    assert _calcular(client, [_linea(pid, 2)])["ahorro"] == 0
    _nxm(client, pid, hasta="2000-01-01T00:00:00")
    assert _calcular(client, [_linea(pid, 2)])["ahorro"] == 0


def test_la_vigencia_se_compara_en_hora_local_y_no_en_utc(client):
    pid = _producto(client, "Alfajor")
    _nxm(client, pid, desde="2026-09-28T18:00:00", hasta="2026-09-28T20:00:00")
    # 19:00 en Argentina son las 22:00Z: adentro de la ventana.
    dentro = _calcular(client, [_linea(pid, 2)], en="2026-09-28T22:00:00Z")
    assert dentro["ahorro"] == 100.0
    # 15:00 en Argentina son las 18:00Z: si se leyera como hora local, entraría.
    fuera = _calcular(client, [_linea(pid, 2)], en="2026-09-28T18:00:00Z")
    assert fuera["ahorro"] == 0


def test_el_precio_vigente_tambien_convierte_el_instante_con_zona(client):
    """El arreglo de zona horaria que arrastraba la tanda anterior: `en` en UTC."""
    pid = _producto(client, "Alfajor", 100.0)
    lista = client.post("/api/listas-precio", json={"nombre": "Default"}).json()
    client.post(f"/api/listas-precio/{lista['id']}/set-default")
    client.post(f"/api/listas-precio/{lista['id']}/items/{pid}/precio-vigente", json={
        "monto": 70.0, "desde": "2026-09-28T18:00:00", "hasta": "2026-09-28T20:00:00"})

    def precio(en):
        r = client.get(f"/api/listas-precio/{lista['id']}/precio", params={"producto_id": pid, "en": en})
        return r.json()["precio"]

    assert precio("2026-09-28T22:00:00Z") == 70.0   # 19:00 local
    assert precio("2026-09-28T18:00:00Z") != 70.0   # 15:00 local


# ── CRUD y validaciones ──────────────────────────────────────────────────

@pytest.mark.parametrize("body, motivo", [
    ({"tipo": "nxm", "paga": 2, "items": [{"producto_id": 1, "cantidad": 2}]}, "menor"),
    ({"tipo": "nxm", "paga": 1, "items": [{"producto_id": 1, "cantidad": 1}]}, "al menos 2"),
    ({"tipo": "nxm", "paga": 1, "items": [{"producto_id": 1, "cantidad": 2},
                                          {"producto_id": 2, "cantidad": 1}]}, "un solo producto"),
    ({"tipo": "combo", "precio": 10, "items": [{"producto_id": 1, "cantidad": 1}]}, "dos productos"),
    ({"tipo": "combo", "items": [{"producto_id": 1, "cantidad": 1},
                                 {"producto_id": 2, "cantidad": 1}]}, "precio"),
    ({"tipo": "combo", "precio": 10, "items": [{"producto_id": 1, "cantidad": 1},
                                               {"producto_id": 1, "cantidad": 1}]}, "repetirse"),
])
def test_una_promocion_imposible_se_rechaza_con_422(client, body, motivo):
    _producto(client, "A")
    _producto(client, "B")
    r = client.post("/api/promociones", json={"nombre": "x", **body})
    assert r.status_code == 422 and motivo in r.json()["detail"]


def test_producto_inexistente_y_vigencia_invertida_dan_422(client):
    pid = _producto(client, "A")
    r = client.post("/api/promociones", json={
        "nombre": "x", "tipo": "nxm", "paga": 1, "items": [{"producto_id": 9999, "cantidad": 2}]})
    assert r.status_code == 422
    r = client.post("/api/promociones", json={
        "nombre": "x", "tipo": "nxm", "paga": 1, "items": [{"producto_id": pid, "cantidad": 2}],
        "desde": "2026-10-01T00:00:00", "hasta": "2026-09-01T00:00:00"})
    assert r.status_code == 422


def test_listar_obtener_actualizar_y_borrar(client):
    pid = _producto(client, "Alfajor")
    promo = _nxm(client, pid)
    assert promo["items"][0]["nombre"] == "Alfajor"
    assert [p["id"] for p in client.get("/api/promociones").json()] == [promo["id"]]

    r = client.put(f"/api/promociones/{promo['id']}", json={
        "nombre": "3x2", "tipo": "nxm", "paga": 2, "items": [{"producto_id": pid, "cantidad": 3}]})
    assert r.status_code == 200 and r.json()["nombre"] == "3x2"
    assert r.json()["items"][0]["cantidad"] == 3

    assert client.get("/api/promociones", params={"solo_activas": True}).json()
    client.put(f"/api/promociones/{promo['id']}", json={
        "nombre": "3x2", "tipo": "nxm", "paga": 2, "activa": False,
        "items": [{"producto_id": pid, "cantidad": 3}]})
    assert client.get("/api/promociones", params={"solo_activas": True}).json() == []

    assert client.delete(f"/api/promociones/{promo['id']}").status_code == 200
    assert client.get(f"/api/promociones/{promo['id']}").status_code == 404
    assert client.delete(f"/api/promociones/{promo['id']}").status_code == 404
    assert client.put(f"/api/promociones/{promo['id']}", json={
        "nombre": "x", "tipo": "nxm", "paga": 1, "items": [{"producto_id": pid, "cantidad": 2}],
    }).status_code == 404


# ── en la venta ──────────────────────────────────────────────────────────

def _vender(client, pid, qty, precio=100.0, **extra):
    total = qty * precio
    body = {
        "fecha": HOY, "items": [{"nombre": "Alfajor", "qty": qty, "precio": precio, "producto_id": pid}],
        "pagos": [{"medio": "efectivo", "monto": total}], **extra,
    }
    return client.post("/api/ventas", json=body)


def test_sin_la_opcion_la_venta_no_cambia(abrir_ventas):
    client = _app(abrir_ventas)  # opciones por defecto: promociones=False
    pid = _producto(client, "Alfajor")
    _nxm(client, pid)
    r = _vender(client, pid, 2)
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 200.0
    assert "promociones" not in r.json()


def test_con_la_opcion_el_ahorro_va_al_descuento_y_queda_registrado(abrir_ventas):
    client = _app(abrir_ventas, OpcionesVentas(promociones=True))
    pid = _producto(client, "Alfajor")
    promo = _nxm(client, pid)
    # El cajero cobra lo que ve en pantalla (subtotal 200 − 100 de la promo).
    r = client.post("/api/ventas", json={
        "fecha": HOY, "items": [{"nombre": "Alfajor", "qty": 2, "precio": 100.0, "producto_id": pid}],
        "pagos": [{"medio": "efectivo", "monto": 100.0}]})
    assert r.status_code == 200, r.text
    venta = r.json()
    assert venta["subtotal"] == 200.0 and venta["descuento"] == 100.0 and venta["total"] == 100.0
    assert venta["promociones"] == [
        {"promocion_id": promo["id"], "nombre": "2x1", "veces": 1, "ahorro": 100.0}]
    assert client.get(f"/api/ventas/{venta['id']}").json()["promociones"] == venta["promociones"]


def test_el_descuento_manual_y_el_de_la_promo_se_suman_con_tope_en_el_subtotal(abrir_ventas):
    client = _app(abrir_ventas, OpcionesVentas(promociones=True))
    pid = _producto(client, "Alfajor")
    _nxm(client, pid)
    r = client.post("/api/ventas", json={
        "fecha": HOY, "descuento": 30.0,
        "items": [{"nombre": "Alfajor", "qty": 2, "precio": 100.0, "producto_id": pid}],
        "pagos": [{"medio": "efectivo", "monto": 70.0}]})
    assert r.status_code == 200, r.text
    assert r.json()["descuento"] == 130.0 and r.json()["total"] == 70.0

    r = client.post("/api/ventas", json={
        "fecha": HOY, "descuento": 500.0,
        "items": [{"nombre": "Alfajor", "qty": 2, "precio": 100.0, "producto_id": pid}],
        "pagos": [{"medio": "efectivo", "monto": 1.0}]})
    assert r.json()["descuento"] == 200.0 and r.json()["total"] == 0.0


def test_el_servidor_no_confia_en_un_carrito_sin_promo_aplicable(abrir_ventas):
    client = _app(abrir_ventas, OpcionesVentas(promociones=True))
    pid = _producto(client, "Alfajor")
    _nxm(client, pid)
    r = _vender(client, pid, 1)
    assert r.status_code == 200, r.text
    assert r.json()["descuento"] == 0 and r.json()["promociones"] == []


def test_borrar_la_promocion_no_reescribe_lo_ya_vendido(abrir_ventas):
    client = _app(abrir_ventas, OpcionesVentas(promociones=True))
    pid = _producto(client, "Alfajor")
    promo = _nxm(client, pid)
    venta = client.post("/api/ventas", json={
        "fecha": HOY, "items": [{"nombre": "Alfajor", "qty": 2, "precio": 100.0, "producto_id": pid}],
        "pagos": [{"medio": "efectivo", "monto": 100.0}]}).json()
    assert client.delete(f"/api/promociones/{promo['id']}").status_code == 200
    registrada = client.get(f"/api/ventas/{venta['id']}").json()["promociones"]
    assert registrada == [{"promocion_id": None, "nombre": "2x1", "veces": 1, "ahorro": 100.0}]


def test_la_promocion_se_deshace_junto_con_la_venta_si_esta_falla(abrir_ventas):
    """Corre con la conexión de la venta: un producto inexistente la rechaza y no
    deja `sale_promotions` colgando."""
    client = _app(abrir_ventas, OpcionesVentas(promociones=True))
    pid = _producto(client, "Alfajor")
    _nxm(client, pid)
    r = client.post("/api/ventas", json={
        "fecha": HOY,
        "items": [{"nombre": "Alfajor", "qty": 2, "precio": 100.0, "producto_id": pid},
                  {"nombre": "Fantasma", "qty": 1, "precio": 10.0, "producto_id": 999999}],
        "pagos": [{"medio": "efectivo", "monto": 110.0}]})
    assert r.status_code == 422
    with abrir_ventas() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sale_promotions").fetchone()[0] == 0


# ── v0.25.1: los dos hallazgos de la revisión de Codex sobre #110 ────────

@pytest.mark.parametrize("cota", ["desde", "hasta"])
def test_una_sola_cota_mal_escrita_se_rechaza_y_no_rompe_el_calculo(client, cota):
    """Antes sólo se parseaban si venían las dos: `desde="mañana"` solo se guardaba y después
    `calcular` (que parsea todas las activas en cada venta) reventaba con un 500."""
    pid = _producto(client, "Alfajor")
    r = client.post("/api/promociones", json={
        "nombre": "x", "tipo": "nxm", "paga": 1, "items": [{"producto_id": pid, "cantidad": 2}],
        cota: "mañana"})
    assert r.status_code == 422 and cota in r.json()["detail"]
    assert client.get("/api/promociones").json() == []
    assert _calcular(client, [_linea(pid, 2)])["ahorro"] == 0


def test_una_cota_mal_escrita_tambien_se_rechaza_al_actualizar(client):
    pid = _producto(client, "Alfajor")
    promo = _nxm(client, pid)
    r = client.put(f"/api/promociones/{promo['id']}", json={
        "nombre": "x", "tipo": "nxm", "paga": 1, "items": [{"producto_id": pid, "cantidad": 2}],
        "hasta": "2026-13-45"})
    assert r.status_code == 422
    assert _calcular(client, [_linea(pid, 2)])["ahorro"] == 100.0


def test_sin_en_ahora_es_la_hora_de_argentina_aunque_el_proceso_este_en_utc(client, monkeypatch):
    """Un servidor en UTC a las 22:00Z (19:00 en Argentina) está DENTRO de una promo de 18 a 20 hs."""
    from libracommerce.erp import promociones as modulo

    class _RelojEnUtc(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            instante = datetime.datetime(2026, 9, 28, 22, 0, 0, tzinfo=datetime.UTC)
            return instante.astimezone(tz) if tz else instante.replace(tzinfo=None)

    monkeypatch.setattr(modulo, "datetime", _RelojEnUtc)
    pid = _producto(client, "Alfajor")
    _nxm(client, pid, desde="2026-09-28T18:00:00", hasta="2026-09-28T20:00:00")
    assert _calcular(client, [_linea(pid, 2)])["ahorro"] == 100.0
