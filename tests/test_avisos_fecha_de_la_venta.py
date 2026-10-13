"""Los avisos de vencimiento de una venta reabierta se miden en el día de la venta, y `plan-salida` apagado es 404 (ADR-057), contra los dos motores.

`GET /api/ventas/{id}` calculaba los avisos contra el día de HOY: una venta de hace un mes con un lote que vencía a los 10 días mostraba `lote_vencido` aunque al vender estaba
vigente (un aviso que la venta nunca tuvo). Ahora el detalle usa el día de la venta; el `POST` que la acaba de crear, el día de hoy (no cambia). Y con
`con_avisos_de_vencimiento` apagada, `POST /api/ventas/plan-salida` contestaba 405 porque `GET /{vid}` captura el path: ahora es un 404, como cualquier ruta que no existe.
"""

from __future__ import annotations

import datetime

import test_fefo_devolucion_transferencia_ajuste as _fdta
import test_fefo_venta as _fefo
from test_fefo_devolucion_transferencia_ajuste import _app, _post_venta
from test_fefo_venta import HOY, _producto

from libracommerce.erp import vencimientos
from libracommerce.web.ventas_router import _fecha_de_la_venta

abrir_fefo = _fefo.abrir_fefo
hoy_fijo = _fefo.hoy_fijo
_ = _fdta

#: Cuando se vuelve a abrir la venta: dos meses después.
MAS_ADELANTE = HOY + datetime.timedelta(days=61)


def _pasar_el_tiempo(monkeypatch):
    monkeypatch.setattr(vencimientos, "hoy_argentina", lambda ahora=None: MAS_ADELANTE)


def test_el_detalle_de_una_venta_vieja_no_inventa_lotes_vencidos(abrir_fefo, monkeypatch):
    """Vendida HOY, con un lote que vence en 5 días y otro que vence ese mismo día. Dos meses después ya vencieron los dos, pero el detalle sigue diciendo lo que pasó al vender."""
    pid = _producto(abrir_fefo, lotes_=(("HOY", "2026-09-30", 1), ("PRONTO", "2026-10-05", 2), ("LARGO", "2026-12-31", 20)))
    cliente = _app(abrir_fefo, avisos=True)
    r = _post_venta(cliente, pid, 3, fecha=HOY.isoformat())
    en_la_venta = r.json()["avisos"]
    assert [(a["tipo"], a["lote"], a["dias_para_vencer"]) for a in en_la_venta] == [("por_vencer", "HOY", 0), ("por_vencer", "PRONTO", 5)]

    _pasar_el_tiempo(monkeypatch)
    reabierta = cliente.get(f"/api/ventas/{r.json()['id']}").json()
    assert reabierta["avisos"] == en_la_venta                        # el mismo aviso: no `lote_vencido` con −62 y −57 días


def test_el_post_sigue_midiendo_contra_el_dia_de_hoy(abrir_fefo):
    """La venta que se acaba de crear usa el «hoy» de Argentina aunque el pedido traiga otra `fecha` (una venta cargada con retraso)."""
    pid = _producto(abrir_fefo, lotes_=(("VIEJO", "2026-09-10", 2), ("LARGO", "2026-12-31", 20)))
    r = _post_venta(_app(abrir_fefo, avisos=True), pid, 2, fecha="2026-09-01")
    assert [(a["tipo"], a["dias_para_vencer"]) for a in r.json()["avisos"]] == [("lote_vencido", -20)]


def test_una_venta_sin_avisos_sigue_sin_la_clave(abrir_fefo, monkeypatch):
    pid = _producto(abrir_fefo, lotes_=(("LARGO", "2026-12-31", 20),))
    cliente = _app(abrir_fefo, avisos=True)
    r = _post_venta(cliente, pid, 2, fecha=HOY.isoformat())
    assert "avisos" not in r.json()
    _pasar_el_tiempo(monkeypatch)
    assert "avisos" not in cliente.get(f"/api/ventas/{r.json()['id']}").json()


def test_la_fecha_de_la_venta_se_lee_con_o_sin_hora_y_si_no_se_puede_leer_no_rompe():
    assert _fecha_de_la_venta({"fecha": "2026-09-10"}) == datetime.date(2026, 9, 10)
    assert _fecha_de_la_venta({"fecha": "2026-09-10 12:30:00"}) == datetime.date(2026, 9, 10)
    assert _fecha_de_la_venta({"fecha": "2026-09-10T12:30:00-03:00"}) == datetime.date(2026, 9, 10)
    assert _fecha_de_la_venta({"fecha": ""}) is None and _fecha_de_la_venta({"fecha": "ayer"}) is None and _fecha_de_la_venta({}) is None


def test_plan_salida_con_la_opcion_apagada_es_404_y_prendida_responde(abrir_fefo):
    pid = _producto(abrir_fefo)
    apagado, prendido = _app(abrir_fefo), _app(abrir_fefo, avisos=True)
    cuerpo = {"items": [{"producto_id": pid, "qty": 1}]}
    r = apagado.post("/api/ventas/plan-salida", json=cuerpo)
    assert r.status_code == 404 and r.json() == {"detail": "Not Found"}
    assert apagado.post("/api/ventas/plan-salida", json={"cualquier": "cosa"}).status_code == 404       # ni mira el cuerpo
    assert apagado.post("/api/ventas/plan-salida").status_code == 404
    assert prendido.post("/api/ventas/plan-salida", json=cuerpo).status_code == 200


def test_el_404_de_plan_salida_apagado_no_aparece_en_el_openapi_ni_tapa_otras_rutas(abrir_fefo):
    """Sigue sin figurar en el esquema, y `GET /{vid}`, `POST /{vid}/anular` y `/devolver` siguen siendo lo que eran."""
    apagado = _app(abrir_fefo)
    assert b"plan-salida" not in apagado.get("/openapi.json").content
    assert apagado.get("/api/ventas/999").status_code == 404
    assert apagado.get("/api/ventas/plan-salida").status_code == 422         # un GET sigue cayendo en `/{vid}` (no es un número), como hoy
    assert apagado.post("/api/ventas/999/anular").status_code == 404
