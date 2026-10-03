"""`build_actualizacion_precios_router`: sube la planilla de un proveedor,
recalcula precios de venta manteniendo el margen, y aplica -- o sólo
adelanta la vista previa. La lógica de cálculo (el margen, el producto sin
costo previo) ya la prueba `tests/test_actualizacion_masiva.py` sobre
`erp.actualizacion_masiva`; acá se prueba el contrato HTTP: el parseo del
`.xlsx`, los mensajes de error y que preview/aplicar vean exactamente lo
mismo.
"""

from __future__ import annotations

import sqlite3
from io import BytesIO

import pytest
from conftest import USUARIO, _usuario  # noqa: F401  (fixture `abrir` la carga pytest sola)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook

from libracommerce.erp import catalogo as erp_catalogo
from libracommerce.web.planillas_router import build_actualizacion_precios_router

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _planilla(filas: list[tuple], encabezados=("Código", "Costo")) -> bytes:
    wb = Workbook()
    hoja = wb.active
    hoja.append(list(encabezados))
    for fila in filas:
        hoja.append(list(fila))
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _archivo(contenido: bytes, nombre: str = "precios.xlsx"):
    return {"archivo": (nombre, contenido, _XLSX)}


@pytest.fixture
def client(abrir):
    app = FastAPI()
    app.include_router(build_actualizacion_precios_router(conexion=abrir, usuario_actual=_usuario))
    return TestClient(app)


@pytest.fixture
def producto(abrir):
    """Yerba, código de barra 7791234567890, costo 1000, venta 1500 (margen 1.5x)."""
    with abrir() as conn:
        pid = erp_catalogo.create_producto(conn, nombre="Yerba", codigo="7791234567890",
                                           precio_venta=1500, precio_costo=1000)
    return pid


def test_preview_calcula_sin_escribir_nada(client, abrir, producto):
    r = client.post("/api/actualizacion-masiva/precios/preview",
                    files=_archivo(_planilla([("7791234567890", 1200)])))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["actualizaciones"] == [{
        "producto_id": producto, "codigo": "7791234567890", "nombre": "Yerba",
        "costo_actual": 1000.0, "costo_nuevo": 1200.0,
        "venta_actual": 1500.0, "venta_nueva": 1800.0, "margen_calculado": True,
    }]
    assert body["no_encontrados"] == []
    with abrir() as conn:
        # Nada se escribió: el producto sigue con sus precios originales.
        assert erp_catalogo.get_producto(conn, producto)["precio_costo"] == 1000.0


def test_aplicar_escribe_el_producto(client, abrir, producto):
    r = client.post("/api/actualizacion-masiva/precios/aplicar",
                    files=_archivo(_planilla([("7791234567890", 1200)])))
    assert r.status_code == 200, r.text
    with abrir() as conn:
        actualizado = erp_catalogo.get_producto(conn, producto)
    assert actualizado["precio_costo"] == 1200.0
    assert actualizado["precio_venta"] == 1800.0


def test_un_codigo_que_no_matchea_nada_se_lista_aparte(client, producto):
    r = client.post("/api/actualizacion-masiva/precios/preview",
                    files=_archivo(_planilla([("0000000000000", 500)])))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["actualizaciones"] == []
    assert body["no_encontrados"] == [{"codigo": "0000000000000", "motivo": "Ningún producto activo tiene este código."}]


def test_sin_las_columnas_necesarias_da_422_con_el_mensaje(client):
    r = client.post("/api/actualizacion-masiva/precios/preview",
                    files=_archivo(_planilla([], encabezados=("Producto", "Cantidad"))))
    assert r.status_code == 422
    assert "columnas necesarias" in r.json()["detail"]


def test_un_costo_no_numerico_da_422_nombrando_la_fila(client):
    r = client.post("/api/actualizacion-masiva/precios/preview",
                    files=_archivo(_planilla([("7791234567890", "gratis")])))
    assert r.status_code == 422
    assert "Fila 2" in r.json()["detail"]


def test_un_archivo_que_no_es_excel_da_422(client):
    r = client.post("/api/actualizacion-masiva/precios/preview",
                    files=_archivo(b"esto no es un xlsx"))
    assert r.status_code == 422
    assert "Excel" in r.json()["detail"]


def test_encabezados_alternativos_matchean(client, producto):
    """EAN / Precio, en vez de Código / Costo."""
    r = client.post("/api/actualizacion-masiva/precios/preview",
                    files=_archivo(_planilla([("7791234567890", 1100)], encabezados=("EAN", "Precio"))))
    assert r.status_code == 200, r.text
    assert r.json()["actualizaciones"][0]["costo_nuevo"] == 1100.0


def _instantanea(abrir) -> dict:
    """El contenido de TODAS las tablas, para afirmar que un 422 no escribió nada en ninguna (el mismo criterio de `test_web_booleanos.py`)."""
    with abrir() as conn:
        if isinstance(conn, sqlite3.Connection):
            tablas = [f[0] for f in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
        else:
            tablas = [f[0] for f in conn.execute("SELECT table_name FROM information_schema.tables "
                                                 "WHERE table_schema='public' AND table_type='BASE TABLE'").fetchall()]
        return {t: sorted(repr(tuple(f)) for f in conn.execute(f"SELECT * FROM {t}").fetchall()) for t in sorted(tablas)}


def test_una_celda_verdadero_o_falso_no_es_un_costo(client, abrir, producto):
    """`float(True)` es `1.0`: una celda VERDADERO en la columna de costo se convertía en un costo de 1 (ADR-027); FALSO ya caía en «mayor que cero» pero con otro motivo."""
    antes = _instantanea(abrir)
    for celda in (True, False):
        for ruta in ("preview", "aplicar"):
            r = client.post(f"/api/actualizacion-masiva/precios/{ruta}", files=_archivo(_planilla([("7791234567890", celda)])))
            assert r.status_code == 422, (celda, ruta, r.text)
            assert "Fila 2" in r.json()["detail"] and "no es un número" in r.json()["detail"]
    with abrir() as conn:
        assert erp_catalogo.get_producto(conn, producto)["precio_costo"] == 1000.0
    assert _instantanea(abrir) == antes                                                # y no se escribió nada, en ninguna tabla
    # Un número, o un texto numérico, sigue valiendo.
    for celda in (1200, "1200"):
        r = client.post("/api/actualizacion-masiva/precios/preview", files=_archivo(_planilla([("7791234567890", celda)])))
        assert r.status_code == 200, r.text
        assert r.json()["actualizaciones"][0]["costo_nuevo"] == 1200.0
