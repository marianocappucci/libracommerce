"""Los CSV del motor no dejan pasar fórmulas (CSV injection): margen, reposición y vencimientos.

Un nombre de producto, un código, un lote o un depósito cargados por el personal que empiezan con `=`, `+`, `-` o `@`
(o con tab o retorno de carro) salen con un `'` adelante. Los números —también los negativos— y los textos normales no
se tocan. El de vencimientos vive en `tests/test_vencimientos.py` (necesita su propia base).
"""

from __future__ import annotations

import csv
import io

import pytest
from conftest import USUARIO
from fastapi import FastAPI
from fastapi.testclient import TestClient

from libracommerce.erp import catalogo, stock, ventas
from libracommerce.web.csv_seguro import celda_segura
from libracommerce.web.margen_router import build_margen_router
from libracommerce.web.reposicion_router import build_reposicion_router

PELIGROSOS = ["=1+1", "+SUMA(1;1)", "-2+3", "@SUMA(1)", "=HYPERLINK(\"http://x\",\"a\")", "\t=1", "\r=1"]


@pytest.mark.parametrize("texto", PELIGROSOS)
def test_un_texto_que_una_planilla_leeria_como_formula_sale_con_comilla(texto):
    assert celda_segura(texto) == "'" + texto


@pytest.mark.parametrize("valor", [
    "Yerba", "Yerba - 500g", "5-6", "a=b", "x@y.com", "'ya", " =con espacio", "", "0", "e",
    0, 1, -2, -3.5, 2.5, 0.0, None, True, False,
])
def test_los_textos_normales_los_numeros_y_none_no_se_tocan(valor):
    assert celda_segura(valor) == valor and type(celda_segura(valor)) is type(valor)


def test_un_numero_negativo_es_un_numero_no_un_texto():
    assert celda_segura(-7) == -7 and celda_segura(-0.25) == -0.25
    assert celda_segura("-7") == "'-7"   # pero el TEXTO «-7» sí, porque una planilla lo evaluaría


def _filas(respuesta) -> list[dict]:
    return list(csv.DictReader(io.StringIO(respuesta.text)))


def _venta(abrir, pid, nombre, cantidad, precio, fecha):
    linea = {"nombre": nombre, "qty": cantidad, "precio": precio, "subtotal": round(cantidad * precio, 2),
             "producto_id": pid}
    total = linea["subtotal"]
    pagos = [{"medio": "efectivo", "monto": total, "estado": "aprobado"}]
    ventas.crear_venta_directa(
        abrir, fecha=fecha, items=[linea], subtotal=total, descuento=0.0, total=total, cliente_id=None,
        cliente_nombre="", usuario_id=USUARIO["id"], observaciones="",
        estado=ventas.estado_segun_pagos(total, pagos), pagos=pagos, stock_habilitado=True,
    )


def test_el_export_de_margen_neutraliza_el_nombre_y_no_toca_los_numeros_negativos(abrir_ventas):
    abrir = abrir_ventas
    with abrir() as conn:
        malo = catalogo.create_producto(conn, "=HYPERLINK(\"http://x\",\"a\")", precio_venta=50.0, precio_costo=80.0)
        normal = catalogo.create_producto(conn, "Yerba - 500g", precio_venta=100.0, precio_costo=60.0)
        for pid in (malo, normal):
            stock.ajustar_stock(conn, pid, 100.0, "inicial", fecha="2026-08-01")
    _venta(abrir, malo, "x", 2, 50.0, "2026-09-10")      # vendido por debajo del costo: margen negativo
    _venta(abrir, normal, "Yerba - 500g", 1, 100.0, "2026-09-10")
    c = TestClient(_app(build_margen_router(conexion=abrir)))
    r = c.get("/api/reportes/margen/export/productos", params={"desde": "2026-09-01", "hasta": "2026-09-30"})
    assert r.status_code == 200
    filas = {f["producto_id"]: f for f in _filas(r)}
    assert filas[str(malo)]["nombre"] == "'=HYPERLINK(\"http://x\",\"a\")"
    assert float(filas[str(malo)]["margen"]) < 0 and filas[str(malo)]["margen"].startswith("-"), "el margen negativo es número"
    assert not filas[str(malo)]["margen"].startswith("'")
    assert filas[str(normal)]["nombre"] == "Yerba - 500g"
    assert not any(v.startswith("'") for k, v in filas[str(normal)].items())
    r = c.get("/api/reportes/margen/export/periodos", params={"desde": "2026-09-01", "hasta": "2026-09-30"})
    assert r.status_code == 200 and all(not v.startswith("'") for f in _filas(r) for v in f.values())


def test_el_export_de_reposicion_neutraliza_nombre_codigo_y_categoria_y_no_los_numeros_negativos(abrir_ventas):
    abrir = abrir_ventas
    with abrir() as conn:
        pid = catalogo.create_producto(conn, "+SUMA(1;1)", codigo="@cod", precio_venta=100.0, precio_costo=60.0,
                                       stock_minimo=5.0, categoria="-Lácteos")
        stock.add_movimiento_stock(conn, pid, "salida", -3, "sin cargar", fecha="2026-08-01")  # stock −3
        sano = catalogo.create_producto(conn, "Yerba", codigo="Y-1", precio_venta=1.0, precio_costo=1.0,
                                        stock_minimo=5.0)
    c = TestClient(_app(build_reposicion_router(conexion=abrir)))
    r = c.get("/api/reportes/reposicion/export", params={"solo_a_pedir": "false"})
    assert r.status_code == 200
    filas = {f["producto_id"]: f for f in _filas(r)}
    f = filas[str(pid)]
    assert (f["nombre"], f["codigo"], f["categoria"]) == ("'+SUMA(1;1)", "'@cod", "'-Lácteos")
    assert f["stock"] == "-3.0", "el stock negativo es un número y no se prefija"
    assert (filas[str(sano)]["nombre"], filas[str(sano)]["codigo"]) == ("Yerba", "Y-1")


def _app(router) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def test_hay_un_solo_punto_por_el_que_pasa_todo_csv_del_motor():
    """Si aparece otro export que arme su propio `csv.writer`, este test lo avisa: tiene que usar `_csv`."""
    from pathlib import Path

    web = Path(__file__).resolve().parents[1] / "libracommerce"
    quienes = sorted(p.relative_to(web).as_posix() for p in web.rglob("*.py")
                     if "csv.writer" in p.read_text(encoding="utf-8") or "csv.DictWriter" in p.read_text(encoding="utf-8"))
    assert quienes == ["web/margen_router.py"], quienes
    assert "celda_segura" in (web / "web" / "margen_router.py").read_text(encoding="utf-8")
