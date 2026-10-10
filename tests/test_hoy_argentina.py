"""«Hoy» es el día de Argentina en todo el motor (ADR-044): la fecha por default de un ajuste de stock, de un movimiento y de una
transferencia, y el período por default del margen, salen de `vencimientos.hoy_argentina`, no de la fecha del sistema. Un servidor o un
CI en UTC corría el día entre las 21 y las 24. Se fija `hoy_argentina` en un día que no es el de hoy y se mira qué fecha quedó escrita."""
from __future__ import annotations

import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_web_booleanos import mundo  # noqa: F401  (fixture)

from libracommerce.erp import catalogo, stock, vencimientos
from libracommerce.web import margen_router
from libracommerce.web.margen_router import build_margen_router

DIA = datetime.date(2024, 3, 15)


@pytest.fixture
def dia_fijo(monkeypatch):
    monkeypatch.setattr(vencimientos, "hoy_argentina", lambda ahora=None: DIA)


def _ultimo_movimiento(abrir, pid) -> str:
    with abrir() as conn:
        return str(conn.execute("SELECT occurred_at FROM stock_movements WHERE item_id = ? ORDER BY id DESC LIMIT 1",
                                (pid,)).fetchone()[0])


def test_el_ajuste_sin_fecha_queda_con_el_dia_de_argentina(mundo, dia_fijo):  # noqa: F811
    w = mundo
    r = w.client.post(f"/api/stock/{w.p2}/ajuste", json={"modo": "entrada", "cantidad": 3, "deposito_id": w.dep1})
    assert r.status_code == 200, r.text
    assert _ultimo_movimiento(w.abrir, w.p2).startswith(DIA.isoformat())


def test_el_movimiento_y_la_transferencia_sin_fecha_quedan_con_el_dia_de_argentina(mundo, dia_fijo):  # noqa: F811
    w = mundo
    with w.abrir() as conn:
        stock.add_movimiento_stock(conn, w.p2, "ajuste", 5, deposito_id=w.dep1)
        conn.commit()
    assert _ultimo_movimiento(w.abrir, w.p2).startswith(DIA.isoformat())
    with w.abrir() as conn:
        catalogo.transferir_stock(conn, w.p1, w.dep1, w.dep2, 2)
        conn.commit()
    assert _ultimo_movimiento(w.abrir, w.p1).startswith(DIA.isoformat())


def test_el_margen_sin_fechas_va_del_primero_del_mes_al_dia_de_argentina(dia_fijo):
    assert margen_router._fechas_default("", "") == ("2024-03-01", "2024-03-15")
    assert margen_router._fechas_default("2024-01-01", "") == ("2024-01-01", "2024-03-15")


def test_el_router_de_margen_usa_ese_periodo(abrir_ventas, dia_fijo):
    c = TestClient(_app(build_margen_router(conexion=abrir_ventas)))
    r = c.get("/api/reportes/margen")
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert (cuerpo.get("desde"), cuerpo.get("hasta")) == ("2024-03-01", "2024-03-15"), cuerpo


def _app(router) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app
