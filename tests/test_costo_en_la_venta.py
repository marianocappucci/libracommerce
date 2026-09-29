"""`sale_items.unit_cost_snapshot` en la venta de mostrador (ADR-016), contra los
DOS motores de base.

🔴 Lo que se protege: `guardar_costo` es OPT-IN. Contalibra y Restolibra comparten
`crear_venta` y hoy dejan el snapshot en NULL; el motor no les cambia nada salvo
que lo pidan. Los tests de "sin opt-in" son el candado de esa promesa."""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from conftest import USUARIO, _usuario
from fastapi import FastAPI
from fastapi.testclient import TestClient

from libracommerce.erp import catalogo, stock, ventas
from libracommerce.web.catalogo_router import build_productos_router
from libracommerce.web.ventas_router import OpcionesVentas, build_ventas_router

HOY = datetime.date.today().isoformat()


def _producto(conn, nombre="Yerba", precio=100.0, costo=60.0):
    pid = catalogo.create_producto(conn, nombre, precio_venta=precio, precio_costo=costo)
    stock.ajustar_stock(conn, pid, 50.0, "inicial", usuario_id=USUARIO["id"], fecha=HOY)
    return pid


def _linea(pid, qty=1, precio=100.0, nombre="Yerba", **extra):
    return {"nombre": nombre, "qty": qty, "precio": precio, "subtotal": qty * precio,
            "producto_id": pid, **extra}


def _vender(abrir, items, **opciones):
    total = round(sum(i["subtotal"] for i in items), 2)
    pagos = [{"medio": "efectivo", "monto": total, "estado": "aprobado"}]
    return ventas.crear_venta_directa(
        abrir, fecha=HOY, items=items, subtotal=total, descuento=0.0, total=total,
        cliente_id=None, cliente_nombre="", usuario_id=USUARIO["id"], observaciones="",
        estado="cobrada", pagos=pagos, stock_habilitado=True, **opciones,
    )


def _snapshots(abrir, venta_id) -> list[Decimal | None]:
    """El `unit_cost_snapshot` de cada línea, en orden. `Decimal` en los dos
    motores (SQLite devuelve float, PostgreSQL `Decimal`)."""
    with abrir() as conn:
        filas = conn.execute(
            "SELECT unit_cost_snapshot FROM sale_items WHERE sale_id = ? ORDER BY id", (venta_id,)
        ).fetchall()
    return [None if f[0] is None else Decimal(str(f[0])) for f in filas]


# ── crear_venta_directa ──────────────────────────────────────────────────


def test_con_guardar_costo_queda_el_costo_vigente(abrir_ventas):
    with abrir_ventas() as conn:
        a = _producto(conn, "Yerba", costo=60.0)
        b = _producto(conn, "Azúcar", costo=42.5)
    vid = _vender(abrir_ventas, [_linea(a, 2), _linea(b, 1, nombre="Azúcar")], guardar_costo=True)
    assert _snapshots(abrir_ventas, vid) == [Decimal("60"), Decimal("42.5")]


def test_el_snapshot_no_se_mueve_si_el_costo_cambia_despues(abrir_ventas):
    """Es el punto de guardarlo: la venta de hoy conserva el costo de hoy."""
    with abrir_ventas() as conn:
        pid = _producto(conn, costo=60.0)
    vid = _vender(abrir_ventas, [_linea(pid)], guardar_costo=True)
    with abrir_ventas() as conn:
        conn.execute("UPDATE catalog_items SET default_cost = 90 WHERE id = ?", (pid,))
        conn.commit()
    assert _snapshots(abrir_ventas, vid) == [Decimal("60")]


def test_sin_guardar_costo_queda_null_como_hoy(abrir_ventas):
    """🔴 El candado para Contalibra y Restolibra: sin pedirlo, ni un byte distinto."""
    with abrir_ventas() as conn:
        pid = _producto(conn, costo=60.0)
    vid = _vender(abrir_ventas, [_linea(pid)])
    assert _snapshots(abrir_ventas, vid) == [None]
    vid = _vender(abrir_ventas, [_linea(pid)], guardar_costo=False)
    assert _snapshots(abrir_ventas, vid) == [None]


def test_producto_sin_costo_queda_null_no_cero(abrir_ventas):
    """`default_cost` es NOT NULL DEFAULT 0: el 0 significa "nadie lo cargó", no
    "costó cero". Guardarlo dejaría un margen de 100% con cara de dato real."""
    with abrir_ventas() as conn:
        pid = _producto(conn, costo=0.0)
    vid = _vender(abrir_ventas, [_linea(pid)], guardar_costo=True)
    assert _snapshots(abrir_ventas, vid) == [None]


def test_linea_de_servicio_queda_null(abrir_ventas):
    with abrir_ventas() as conn:
        pid = _producto(conn, costo=60.0)
    envio = {"nombre": "Envío", "qty": 1, "precio": 50.0, "subtotal": 50.0, "producto_id": None}
    vid = _vender(abrir_ventas, [_linea(pid), envio], guardar_costo=True)
    assert _snapshots(abrir_ventas, vid) == [Decimal("60"), None]


def test_linea_con_variante_toma_el_costo_del_producto(abrir_ventas):
    """El costo vive en `catalog_items.default_cost`: `item_variants` no tiene columna de costo."""
    with abrir_ventas() as conn:
        pid = _producto(conn, costo=60.0)
        conn.execute("INSERT INTO item_variants (item_id, sku, name) VALUES (?,?,?)", (pid, "Y-500", "500 g"))
        vid_variante = conn.execute("SELECT id FROM item_variants WHERE sku = 'Y-500'").fetchone()[0]
        conn.commit()
    vid = _vender(abrir_ventas, [_linea(pid, variante_id=vid_variante)], guardar_costo=True)
    assert _snapshots(abrir_ventas, vid) == [Decimal("60")]


def test_producto_inexistente_sigue_levantando_con_guardar_costo(abrir_ventas):
    """Leer el costo de un producto que no existe no puede tapar el 422 de siempre."""
    with pytest.raises(ventas.ProductoInexistente):
        _vender(abrir_ventas, [_linea(999999)], guardar_costo=True)
    with abrir_ventas() as conn:
        assert ventas.listar_ventas(conn) == []


# ── El reporte de margen lee el snapshot ─────────────────────────────────


def _margen(abrir):
    """`erp.margen` llega con ADR-015 (`origin/main`); si esta rama todavía no lo
    tiene, el test se saltea en vez de romperse por un import."""
    margen = pytest.importorskip("libracommerce.erp.margen")
    with abrir() as conn:
        return margen.reporte_margen(conn)["productos"]


def test_el_margen_usa_el_snapshot_y_no_marca_costo_estimado(abrir_ventas):
    with abrir_ventas() as conn:
        pid = _producto(conn, costo=60.0)
    _vender(abrir_ventas, [_linea(pid, 2)], guardar_costo=True)
    # El costo de hoy sube: la venta ya hecha no debe enterarse.
    with abrir_ventas() as conn:
        conn.execute("UPDATE catalog_items SET default_cost = 90 WHERE id = ?", (pid,))
        conn.commit()
    (fila,) = _margen(abrir_ventas)
    assert fila["costo"] == 120.0 and fila["margen"] == 80.0
    assert fila["costo_estimado"] is False and fila["sin_costo"] is False


def test_el_margen_sin_snapshot_sigue_estimando(abrir_ventas):
    """El contraste: sin opt-in el margen usa el costo de hoy y lo dice."""
    with abrir_ventas() as conn:
        pid = _producto(conn, costo=60.0)
    _vender(abrir_ventas, [_linea(pid, 2)])
    (fila,) = _margen(abrir_ventas)
    assert fila["costo"] == 120.0 and fila["costo_estimado"] is True


# ── El router: OpcionesVentas.guardar_costo ──────────────────────────────


def _client(abrir, opciones=None) -> TestClient:
    app = FastAPI()
    app.include_router(build_productos_router(conexion=abrir, usuario_actual=_usuario))
    app.include_router(build_ventas_router(conexion=abrir, usuario_actual=_usuario, opciones=opciones))
    return TestClient(app)


def _post_venta(client, pid):
    resp = client.post("/api/ventas", json={
        "fecha": HOY, "items": [{"nombre": "Yerba", "qty": 1, "precio": 100.0, "producto_id": pid}],
        "pagos": [{"medio": "efectivo", "monto": 100.0}],
    })
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def test_router_con_la_opcion_guarda_el_costo(abrir_ventas):
    with abrir_ventas() as conn:
        pid = _producto(conn, costo=60.0)
    vid = _post_venta(_client(abrir_ventas, OpcionesVentas(guardar_costo=True)), pid)
    assert _snapshots(abrir_ventas, vid) == [Decimal("60")]


def test_router_sin_la_opcion_no_guarda_nada(abrir_ventas):
    """El default de `OpcionesVentas` es el de hoy: el snapshot queda NULL."""
    with abrir_ventas() as conn:
        pid = _producto(conn, costo=60.0)
    assert OpcionesVentas().guardar_costo is False
    vid = _post_venta(_client(abrir_ventas), pid)
    assert _snapshots(abrir_ventas, vid) == [None]
