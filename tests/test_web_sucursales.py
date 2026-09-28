"""Sucursales (`build_sucursales_router`), portado de LibraDesk al motor —
ver `wiki/analyses/jerarquia-sucursal-deposito-libracommerce.md`.

La guarda de baja (`erp.catalogo._verificar_baja_de_sucursal`) es la pieza
que importa: es una porta literal de `_verificar_baja_de_sucursal` de
LibraDesk, con su corrección del 2026-08-16 (mira EXISTENCIAS, no sólo
depósitos `active=1`). Estos tests son, en gran parte, los mismos casos que
`libradesk/tests/test_sucursales.py` ejercita contra la capa de producto.
"""

from __future__ import annotations

import pytest
from conftest import _usuario  # noqa: F401  (y la fixture `abrir`, que pytest carga sola)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from libracommerce.erp import catalogo
from libracommerce.web.catalogo_router import (
    OpcionesStock,
    build_depositos_router,
    build_productos_router,
    build_stock_router,
    build_sucursales_router,
)


def _app(abrir) -> TestClient:
    app = FastAPI()
    app.include_router(build_productos_router(conexion=abrir, usuario_actual=_usuario))
    app.include_router(build_depositos_router(conexion=abrir, usuario_actual=_usuario))
    # `por_deposito=True`: sin esto el ajuste ignora `deposito_id` y siempre
    # escribe en el depósito default (Contalibra/Restolibra, sin sucursales).
    app.include_router(
        build_stock_router(conexion=abrir, usuario_actual=_usuario, opciones=OpcionesStock(por_deposito=True))
    )
    app.include_router(build_sucursales_router(conexion=abrir, usuario_actual=_usuario))
    return TestClient(app)


@pytest.fixture
def client(abrir):
    return _app(abrir)


def _crear_sucursal(client, nombre="Casa Central", **extra):
    payload = {"nombre": nombre, "codigo": extra.pop("codigo", ""), "direccion": extra.pop("direccion", "")}
    payload.update(extra)
    resp = client.post("/api/sucursales", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _depositos_de(abrir, sid):
    with abrir() as conn:
        filas = conn.execute(
            "SELECT id, active, name FROM locations WHERE branch_id=? ORDER BY id", (sid,)
        ).fetchall()
    return [{"id": f[0], "activo": bool(f[1]), "nombre": f[2]} for f in filas]


def _crear_deposito(client, nombre, branch_id=None):
    resp = client.post("/api/depositos", json={"nombre": nombre, "branch_id": branch_id})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── CRUD básico ────────────────────────────────────────────────────────


def test_crear_y_listar_sucursal(client):
    s = _crear_sucursal(client, "Local Centro", codigo="LC", direccion="Av. Siempreviva 742")
    assert s["codigo"] == "LC"
    assert s["direccion"] == "Av. Siempreviva 742"
    nombres = [x["nombre"] for x in client.get("/api/sucursales").json()]
    assert "Local Centro" in nombres


def test_nombre_obligatorio(client):
    assert client.post("/api/sucursales", json={"nombre": "  "}).status_code == 422


def test_obtener_sucursal_404(client):
    assert client.get("/api/sucursales/999").status_code == 404


def test_editar_sucursal(client):
    s = _crear_sucursal(client, "Vieja")
    resp = client.put(f"/api/sucursales/{s['id']}", json={"nombre": "Nueva", "codigo": "N1", "direccion": "X"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["nombre"] == "Nueva"


def test_editar_sucursal_inexistente_404(client):
    assert client.put("/api/sucursales/999", json={"nombre": "X"}).status_code == 404


# ── El vínculo con depósitos (`locations.branch_id`) ──────────────────


def test_deposito_creado_con_branch_id_cuenta_en_la_sucursal(client):
    s = _crear_sucursal(client, "Con depósito")
    _crear_deposito(client, "Depósito de la sucursal", branch_id=s["id"])
    listado = client.get("/api/sucursales").json()
    encontrada = next(x for x in listado if x["id"] == s["id"])
    assert encontrada["depositos"] == 2  # el que trae la sucursal + el que se agregó


def test_crear_deposito_con_sucursal_inexistente_es_422(client):
    resp = client.post("/api/depositos", json={"nombre": "Huérfano", "branch_id": 999})
    assert resp.status_code == 422


def test_crear_deposito_con_sucursal_dada_de_baja_es_422(client):
    s = _crear_sucursal(client, "De baja")
    client.put(f"/api/sucursales/{s['id']}", json={"nombre": "De baja", "activa": False})
    resp = client.post("/api/depositos", json={"nombre": "Tarde", "branch_id": s["id"]})
    assert resp.status_code == 422


def test_deposito_sin_branch_id_sigue_funcionando(client):
    """Un producto sin sucursales (Contalibra) nunca manda `branch_id`: el
    depósito se crea igual que siempre, con `branch_id=None`."""
    d = _crear_deposito(client, "Depósito suelto")
    assert d is not None


# ── La guarda de baja: el porqué de esta migración ────────────────────


def test_no_se_puede_desactivar_la_sucursal_por_defecto(client):
    s = _crear_sucursal(client, "Default")
    client.post(f"/api/sucursales/{s['id']}/set-default")
    resp = client.put(f"/api/sucursales/{s['id']}", json={"nombre": "Default", "activa": False})
    assert resp.status_code == 422
    assert "por defecto" in resp.json()["detail"]


def test_no_se_puede_marcar_default_una_sucursal_inactiva(client):
    s = _crear_sucursal(client, "Inactiva")
    client.put(f"/api/sucursales/{s['id']}", json={"nombre": "Inactiva", "activa": False})
    resp = client.post(f"/api/sucursales/{s['id']}/set-default")
    assert resp.status_code == 422


def test_se_puede_desactivar_una_sucursal_sin_existencias(client, abrir):
    s = _crear_sucursal(client, "Vacía")
    resp = client.put(f"/api/sucursales/{s['id']}", json={"nombre": "Vacía", "activa": False})
    assert resp.status_code == 200
    assert resp.json()["activa"] is False
    assert not any(d["activo"] for d in _depositos_de(abrir, s["id"]))


def test_la_baja_de_una_sucursal_da_de_baja_sus_depositos_vacios(client, abrir):
    """Un depósito activo pero vacío no bloquea la baja (ADR-013): el chequeo que importa es el de existencias,
    y la baja arrastra a todos los depósitos de la sucursal."""
    s = _crear_sucursal(client, "Con depósitos")
    _crear_deposito(client, "Segundo", branch_id=s["id"])
    resp = client.put(f"/api/sucursales/{s['id']}", json={"nombre": "X", "activa": False})
    assert resp.status_code == 200, resp.text
    depositos = _depositos_de(abrir, s["id"])
    assert len(depositos) == 2
    assert not any(d["activo"] for d in depositos)


def test_no_se_puede_desactivar_una_sucursal_con_existencias_en_deposito_inactivo(client):
    """La corrección del 2026-08-16 de LibraDesk: un depósito DESACTIVADO con
    stock adentro también tiene que bloquear la baja de la sucursal — si no,
    esas existencias quedan invisibles sin que nadie las haya movido."""
    s = _crear_sucursal(client, "Con historia")
    d = _crear_deposito(client, "Depósito con stock", branch_id=s["id"])
    p = client.post("/api/productos", json={"nombre": "Yerba 1kg", "precio_venta": 1500.0, "precio_costo": 900.0}).json()
    resp_ajuste = client.post(
        f"/api/stock/{p['id']}/ajuste", json={"modo": "absoluto", "cantidad": 10, "deposito_id": d["id"]}
    )
    assert resp_ajuste.status_code == 200, resp_ajuste.text
    # Se desactiva el depósito (nada lo impide: no es el default).
    assert client.put(f"/api/depositos/{d['id']}", json={"nombre": "Depósito con stock", "activo": False}).status_code == 200
    # La sucursal sigue bloqueada: quedan existencias con `<> 0` en un depósito suyo.
    resp = client.put(f"/api/sucursales/{s['id']}", json={"nombre": "X", "activa": False})
    assert resp.status_code == 422
    assert "existencias" in resp.json()["detail"]


def test_transferir_el_stock_y_desactivar_el_deposito_libera_la_baja(client):
    """El camino feliz que el mensaje de error sugiere: transferir a otro
    depósito (de otra sucursal) deja el saldo del depósito bloqueante en
    cero, y ahí sí se puede dar de baja la sucursal."""
    s = _crear_sucursal(client, "Se cierra")
    d = _crear_deposito(client, "Depósito que se vacía", branch_id=s["id"])
    otro = _crear_deposito(client, "Depósito de otra sucursal")
    p = client.post("/api/productos", json={"nombre": "Yerba 1kg", "precio_venta": 1500.0, "precio_costo": 900.0}).json()
    client.post(f"/api/stock/{p['id']}/ajuste", json={"modo": "absoluto", "cantidad": 10, "deposito_id": d["id"]})
    resp_transf = client.post("/api/depositos/transferir", json={
        "producto_id": p["id"], "origen_id": d["id"], "destino_id": otro["id"], "cantidad": 10})
    assert resp_transf.status_code == 200, resp_transf.text
    assert client.put(f"/api/depositos/{d['id']}", json={"nombre": "Vacío", "activo": False}).status_code == 200
    resp = client.put(f"/api/sucursales/{s['id']}", json={"nombre": "X", "activa": False})
    assert resp.status_code == 200


# ── Toda sucursal declara al menos un depósito (Fase 0.5, ADR-013) ────────


def test_crear_sucursal_crea_su_primer_deposito_y_lo_predetermina(client, abrir):
    s = _crear_sucursal(client, "Local Centro")
    depositos = _depositos_de(abrir, s["id"])
    assert [d["nombre"] for d in depositos] == ["Depósito Local Centro"]
    assert s["deposito_predeterminado_id"] == depositos[0]["id"]
    assert client.get("/api/sucursales").json()[0]["depositos"] >= 1


def test_crear_sucursal_con_nombre_de_deposito(client, abrir):
    s = _crear_sucursal(client, "Norte", deposito="Trastienda")
    assert [d["nombre"] for d in _depositos_de(abrir, s["id"])] == ["Trastienda"]


def test_un_deposito_nuevo_no_le_saca_el_predeterminado_a_la_sucursal(client):
    s = _crear_sucursal(client, "Sur")
    _crear_deposito(client, "Extra", branch_id=s["id"])
    assert client.get(f"/api/sucursales/{s['id']}").json()["deposito_predeterminado_id"] == s["deposito_predeterminado_id"]


def test_no_se_puede_desactivar_el_ultimo_deposito_activo_de_una_sucursal(client):
    s = _crear_sucursal(client, "Una sola")
    did = s["deposito_predeterminado_id"]
    resp = client.put(f"/api/depositos/{did}", json={"nombre": "X", "activo": False})
    assert resp.status_code == 422
    assert "único depósito activo" in resp.json()["detail"]


def test_no_se_puede_eliminar_el_ultimo_deposito_de_una_sucursal(client):
    s = _crear_sucursal(client, "Una sola")
    resp = client.delete(f"/api/depositos/{s['deposito_predeterminado_id']}")
    assert resp.status_code == 422
    assert "único depósito activo" in resp.json()["detail"]


def test_desactivar_el_predeterminado_pasa_el_predeterminado_a_otro_activo(client):
    s = _crear_sucursal(client, "Dos")
    otro = _crear_deposito(client, "Otro", branch_id=s["id"])
    resp = client.put(f"/api/depositos/{s['deposito_predeterminado_id']}", json={"nombre": "X", "activo": False})
    assert resp.status_code == 200, resp.text
    assert client.get(f"/api/sucursales/{s['id']}").json()["deposito_predeterminado_id"] == otro["id"]


def test_eliminar_el_predeterminado_pasa_el_predeterminado_a_otro_activo(client):
    s = _crear_sucursal(client, "Dos")
    otro = _crear_deposito(client, "Otro", branch_id=s["id"])
    assert client.delete(f"/api/depositos/{s['deposito_predeterminado_id']}").status_code == 200
    assert client.get(f"/api/sucursales/{s['id']}").json()["deposito_predeterminado_id"] == otro["id"]


def test_un_deposito_sin_sucursal_no_tiene_la_guarda(client):
    suelto = _crear_deposito(client, "Suelto")
    assert client.put(f"/api/depositos/{suelto['id']}", json={"nombre": "Suelto", "activo": False}).status_code == 200


def test_no_se_puede_dar_de_baja_la_sucursal_con_el_deposito_por_defecto_de_la_instancia(client):
    s = _crear_sucursal(client, "Con el default")
    assert client.post(f"/api/depositos/{s['deposito_predeterminado_id']}/set-default").status_code == 200
    resp = client.put(f"/api/sucursales/{s['id']}", json={"nombre": "X", "activa": False})
    assert resp.status_code == 422
    assert "por defecto de la instancia" in resp.json()["detail"]


def test_reactivar_la_sucursal_reactiva_su_deposito_predeterminado(client, abrir):
    s = _crear_sucursal(client, "Vuelve")
    assert client.put(f"/api/sucursales/{s['id']}", json={"nombre": "Vuelve", "activa": False}).status_code == 200
    resp = client.put(f"/api/sucursales/{s['id']}", json={"nombre": "Vuelve", "activa": True})
    assert resp.status_code == 200, resp.text
    activos = [d for d in _depositos_de(abrir, s["id"]) if d["activo"]]
    assert [d["id"] for d in activos] == [s["deposito_predeterminado_id"]]
    assert resp.json()["deposito_predeterminado_id"] == s["deposito_predeterminado_id"]


def test_marcar_otro_deposito_como_predeterminado(client):
    s = _crear_sucursal(client, "Elige")
    otro = _crear_deposito(client, "Otro", branch_id=s["id"])
    resp = client.post(f"/api/sucursales/{s['id']}/deposito-predeterminado", json={"deposito_id": otro["id"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["deposito_predeterminado_id"] == otro["id"]


def test_el_predeterminado_tiene_que_ser_un_deposito_activo_de_esa_sucursal(client):
    a = _crear_sucursal(client, "A")
    b = _crear_sucursal(client, "B")
    ajeno = client.post(
        f"/api/sucursales/{a['id']}/deposito-predeterminado", json={"deposito_id": b["deposito_predeterminado_id"]}
    )
    assert ajeno.status_code == 422
    inactivo = _crear_deposito(client, "Inactivo", branch_id=a["id"])
    client.put(f"/api/depositos/{inactivo['id']}", json={"nombre": "Inactivo", "activo": False})
    resp = client.post(f"/api/sucursales/{a['id']}/deposito-predeterminado", json={"deposito_id": inactivo["id"]})
    assert resp.status_code == 422


def test_deposito_predeterminado_de_una_sucursal_404(client):
    resp = client.post("/api/sucursales/999/deposito-predeterminado", json={"deposito_id": 1})
    assert resp.status_code == 404


def test_deposito_de_venta_repara_una_sucursal_de_antes_de_la_invariante(client, abrir):
    """Una sucursal creada sin `default_location_id` (datos de antes de la Fase 0.5, o un producto que
    migró) igual resuelve su depósito de venta: el activo de menor id."""
    s = _crear_sucursal(client, "Vieja")
    with abrir() as conn:
        conn.execute("UPDATE branches SET default_location_id=NULL WHERE id=?", (s["id"],))
    with abrir() as conn:
        assert catalogo.get_deposito_de_venta(conn, s["id"]) == s["deposito_predeterminado_id"]


def test_deposito_de_venta_de_una_sucursal_inexistente_es_none(abrir):
    with abrir() as conn:
        assert catalogo.get_deposito_de_venta(conn, 999) is None
