"""ADR-038: quien sólo ve sus turnos (`OpcionesVentas.solo_sus_turnos`) lista, abre, anula y devuelve sólo las ventas de
los turnos de caja que abrió; una ajena es 404, como una que no existe. Sin la opción, todos ven todas (lo de hoy), y
`build_guarda_de_venta` lleva la misma regla a las rutas con id de venta de otros routers."""

from __future__ import annotations

import datetime

import pytest
from conftest import USUARIO, _usuario
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from libracommerce.erp import ventas
from libracommerce.web.catalogo_router import build_productos_router, build_stock_router
from libracommerce.web.ventas_router import OpcionesVentas, build_guarda_de_venta, build_ventas_router

HOY = datetime.date.today().isoformat()
CAJERO_ID = USUARIO["id"]
OTRO_ID = 8


def _es_cajero(user: dict) -> bool:
    return user.get("role") == "cajero"


def _app(abrir, opciones=None) -> TestClient:
    """Sin `solo_admin`, como lo monta VentaLibra: el cajero anula y devuelve."""
    app = FastAPI()
    app.include_router(build_productos_router(conexion=abrir, usuario_actual=_usuario))
    app.include_router(build_stock_router(conexion=abrir, usuario_actual=_usuario))
    app.include_router(build_ventas_router(conexion=abrir, usuario_actual=_usuario, opciones=opciones))
    return TestClient(app)


def _como(usuario_id: int, rol: str) -> None:
    USUARIO["id"] = usuario_id
    USUARIO["role"] = rol


@pytest.fixture(autouse=True)
def _restaurar_usuario():
    yield
    USUARIO["id"] = CAJERO_ID
    USUARIO.pop("role", None)


def _abrir_turno(abrir, usuario_id: int) -> int:
    from libracore.db.turnos import create_turno

    with abrir() as conn:
        tid = create_turno(usuario_id, 0.0)
        conn.commit()
    return tid


def _vender(client, producto_id: int | None = None) -> dict:
    item = {"nombre": "Yerba", "qty": 2, "precio": 100.0}
    if producto_id:
        item["producto_id"] = producto_id
    r = client.post("/api/ventas", json={"fecha": HOY, "items": [item], "pagos": [{"medio": "efectivo", "monto": 200.0}]})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def escenario(abrir_ventas):
    """Una venta sin turno, una del cajero (7) en su turno y una de otro usuario (8) en el suyo."""
    with abrir_ventas() as conn:
        conn.execute("INSERT INTO usuarios (id, username, nombre, password_hash, role) VALUES (?,?,?,?,?)",
                     (OTRO_ID, "otro", "Otro cajero", "x", "cajero"))
        conn.commit()
    client = _app(abrir_ventas, OpcionesVentas(solo_sus_turnos=_es_cajero))
    _como(CAJERO_ID, "admin")
    pid = client.post("/api/productos", json={"nombre": "Yerba", "precio_venta": 100.0}).json()["id"]
    client.post(f"/api/stock/{pid}/ajuste", json={"modo": "absoluto", "cantidad": 50})
    sin_turno = _vender(client, pid)
    turno_propio = _abrir_turno(abrir_ventas, CAJERO_ID)
    propia = _vender(client, pid)
    _como(OTRO_ID, "cajero")
    _abrir_turno(abrir_ventas, OTRO_ID)
    ajena = _vender(client, pid)
    assert propia["turno_id"] == turno_propio and ajena["turno_id"] not in (None, turno_propio)
    assert sin_turno["turno_id"] is None
    return {"client": client, "abrir": abrir_ventas, "pid": pid, "sin_turno": sin_turno, "propia": propia, "ajena": ajena}


def _ids(client) -> set[int]:
    r = client.get("/api/ventas")
    assert r.status_code == 200, r.text
    return {v["id"] for v in r.json()}


def test_el_cajero_lista_solo_las_ventas_de_sus_turnos(escenario):
    client = escenario["client"]
    _como(CAJERO_ID, "cajero")
    assert _ids(client) == {escenario["propia"]["id"]}
    _como(OTRO_ID, "cajero")
    assert _ids(client) == {escenario["ajena"]["id"]}
    # Los filtros de siempre se combinan con el de turno, no lo reemplazan.
    assert client.get("/api/ventas", params={"q": escenario["propia"]["numero"]}).json() == []


def test_quien_no_esta_limitado_lista_todas(escenario):
    _como(CAJERO_ID, "encargado")
    todas = {escenario[k]["id"] for k in ("sin_turno", "propia", "ajena")}
    assert _ids(escenario["client"]) == todas


def test_sin_la_opcion_el_cajero_ve_todas(escenario):
    """El default: Contalibra y Restolibra no la prenden y no cambian."""
    client = _app(escenario["abrir"])
    _como(CAJERO_ID, "cajero")
    assert len(_ids(client)) == 3
    assert client.get(f"/api/ventas/{escenario['ajena']['id']}").status_code == 200


def test_el_detalle_de_una_venta_ajena_es_404_como_una_inexistente(escenario):
    client = escenario["client"]
    _como(CAJERO_ID, "cajero")
    assert client.get(f"/api/ventas/{escenario['propia']['id']}").status_code == 200
    for vid in (escenario["ajena"]["id"], escenario["sin_turno"]["id"], 999):
        r = client.get(f"/api/ventas/{vid}")
        assert r.status_code == 404 and r.json() == {"detail": "Venta no encontrada"}


def test_el_turno_cerrado_sigue_siendo_suyo(escenario):
    """«Sus turnos», no «su turno abierto»: la venta de ayer la puede devolver quien la cobró."""
    with escenario["abrir"]() as conn:
        conn.execute("UPDATE turnos_caja SET estado='cerrado' WHERE usuario_id=?", (CAJERO_ID,))
        conn.commit()
    _como(CAJERO_ID, "cajero")
    assert _ids(escenario["client"]) == {escenario["propia"]["id"]}


def test_no_anula_ni_devuelve_una_venta_ajena(escenario):
    client, ajena = escenario["client"], escenario["ajena"]
    with escenario["abrir"]() as conn:
        linea = conn.execute("SELECT id FROM sale_items WHERE sale_id=?", (ajena["id"],)).fetchone()["id"]
        deposito = conn.execute("SELECT location_id FROM stock_movements WHERE source_id=?",
                                (ajena["id"],)).fetchone()["location_id"]
    _como(CAJERO_ID, "cajero")
    devolucion = {"lineas": [{"sale_item_id": linea, "cantidad": 1}], "deposito_id": deposito}
    assert client.post(f"/api/ventas/{ajena['id']}/devolver", json=devolucion).status_code == 404
    assert client.post(f"/api/ventas/{ajena['id']}/anular").status_code == 404
    with escenario["abrir"]() as conn:
        assert ventas.obtener_venta(conn, ajena["id"])["estado"] == "cobrada"
    # La suya sí.
    assert client.post(f"/api/ventas/{escenario['propia']['id']}/anular").status_code == 200


def test_la_guarda_corta_las_rutas_de_otros_routers(escenario):
    """`build_guarda_de_venta` en otro router, con otro nombre de parámetro (el ticket de VentaLibra usa `sale_id`)."""
    otro = APIRouter()

    @otro.get("/api/ventas/{sale_id}/ticket")
    def ticket(sale_id: int):
        return {"ticket": sale_id}

    @otro.get("/api/pos/estado")
    def estado():
        return {"ok": True}

    app = FastAPI()
    app.include_router(otro, dependencies=[Depends(build_guarda_de_venta(
        solo_sus_turnos=_es_cajero, conexion=escenario["abrir"], usuario_actual=_usuario, parametro="sale_id"))])
    client = TestClient(app)
    _como(CAJERO_ID, "cajero")
    assert client.get(f"/api/ventas/{escenario['propia']['id']}/ticket").status_code == 200
    assert client.get(f"/api/ventas/{escenario['ajena']['id']}/ticket").status_code == 404
    assert client.get("/api/ventas/abc/ticket").status_code == 422  # la validación de la ruta, como sin la guarda
    assert client.get("/api/pos/estado").status_code == 200  # sin el parámetro no consulta nada
    _como(CAJERO_ID, "encargado")
    assert client.get(f"/api/ventas/{escenario['ajena']['id']}/ticket").status_code == 200


def test_es_de_sus_turnos(escenario):
    with escenario["abrir"]() as conn:
        assert ventas.es_de_sus_turnos(conn, escenario["propia"]["id"], CAJERO_ID)
        assert not ventas.es_de_sus_turnos(conn, escenario["ajena"]["id"], CAJERO_ID)
        assert not ventas.es_de_sus_turnos(conn, escenario["sin_turno"]["id"], CAJERO_ID)
        assert not ventas.es_de_sus_turnos(conn, escenario["propia"]["id"], None)
        assert not ventas.es_de_sus_turnos(conn, 999, CAJERO_ID)
