"""Margen y rotación por producto y por período (ADR-015), contra los dos motores.

Lo que se mide acá es lo que decide si el número es creíble: qué NO cuenta como venta
(anulada, pendiente de cobro), cómo se resta una devolución (que no toca `sale_items`),
de dónde sale el costo (el snapshot de la línea, o el de hoy avisando que es el de hoy) y
cómo se reparte el descuento de la venta."""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from conftest import USUARIO
from fastapi import FastAPI
from fastapi.testclient import TestClient

from libracommerce.db.repository import SqliteCommerceRepository
from libracommerce.domain.catalog import CatalogItemType
from libracommerce.domain.sales import Sale, SaleItem
from libracommerce.erp import catalogo, margen, stock, ventas
from libracommerce.usecases.sales import confirm_sale, return_sale_items
from libracommerce.web.margen_router import build_margen_router

D1, D2, D3 = "2026-09-01", "2026-09-02", "2026-09-03"


def _producto(conn, nombre, precio, costo):
    pid = catalogo.create_producto(conn, nombre, precio_venta=precio, precio_costo=costo)
    stock.ajustar_stock(conn, pid, 1000.0, "inicial", usuario_id=USUARIO["id"], fecha=D1)
    return pid


def _venta(abrir, items, fecha=D1, descuento=0.0, acreditado=True):
    """Una venta de mostrador por el mismo camino que `POST /api/ventas`. `items`: `(producto_id, nombre, qty, precio)`."""
    lineas = [{"nombre": n, "qty": q, "precio": p, "subtotal": round(q * p, 2), "producto_id": pid}
              for pid, n, q, p in items]
    subtotal = round(sum(li["subtotal"] for li in lineas), 2)
    total = round(subtotal - descuento, 2)
    pagos = [{"medio": "efectivo", "monto": total, "estado": "aprobado" if acreditado else "pendiente"}]
    return ventas.crear_venta_directa(
        abrir, fecha=fecha, items=lineas, subtotal=subtotal, descuento=descuento, total=total,
        cliente_id=None, cliente_nombre="", usuario_id=USUARIO["id"], observaciones="",
        estado=ventas.estado_segun_pagos(total, pagos), pagos=pagos, stock_habilitado=True,
    )


def _reporte(abrir, **kw):
    with abrir() as conn:
        return margen.reporte_margen(conn, **kw)


def _por_nombre(reporte):
    return {p["nombre"]: p for p in reporte["productos"]}


def _linea_de(abrir, venta_id):
    with abrir() as conn:
        fila = conn.execute("SELECT id FROM sale_items WHERE sale_id=? ORDER BY id LIMIT 1", (venta_id,)).fetchone()
        deposito = conn.execute("SELECT location_id FROM stock_movements WHERE source_id=? LIMIT 1",
                                (venta_id,)).fetchone()
    return fila["id"], deposito["location_id"]


# ── El cálculo ───────────────────────────────────────────────────────────


def test_margen_por_producto_y_por_periodo(abrir_ventas):
    abrir = abrir_ventas
    with abrir() as conn:
        yerba = _producto(conn, "Yerba", 100.0, 60.0)
        azucar = _producto(conn, "Azúcar", 50.0, 40.0)
    _venta(abrir, [(yerba, "Yerba", 3, 100.0), (azucar, "Azúcar", 2, 50.0)], fecha=D1)
    _venta(abrir, [(yerba, "Yerba", 1, 100.0)], fecha=D2)

    r = _reporte(abrir, desde=D1, hasta=D3)
    p = _por_nombre(r)
    # Yerba: 4 u a $100 con costo $60 -> ingreso 400, costo 240, margen 160 (40%).
    assert p["Yerba"]["producto_id"] == yerba
    assert (p["Yerba"]["unidades"], p["Yerba"]["ingreso"], p["Yerba"]["costo"], p["Yerba"]["margen"],
            p["Yerba"]["margen_pct"]) == (4.0, 400.0, 240.0, 160.0, 40.0)
    # Azúcar: 2 u a $50 con costo $40 -> 100 / 80 / 20 (20%).
    assert (p["Azúcar"]["ingreso"], p["Azúcar"]["costo"], p["Azúcar"]["margen"], p["Azúcar"]["margen_pct"]) == (
        100.0, 80.0, 20.0, 20.0)
    # La rotación: 3 días de rango (1, 2 y 3 de septiembre).
    assert r["resumen"]["dias"] == 3
    assert p["Yerba"]["unidades_por_dia"] == round(4 / 3, 2)

    assert [(x["periodo"], x["unidades"], x["ingreso"], x["costo"], x["margen"]) for x in r["periodos"]] == [
        (D1, 5.0, 400.0, 260.0, 140.0), (D2, 1.0, 100.0, 60.0, 40.0)]
    assert r["resumen"]["ingreso"] == 500.0 and r["resumen"]["costo"] == 320.0 and r["resumen"]["margen"] == 180.0
    assert r["resumen"]["margen_pct"] == 36.0 and r["resumen"]["productos"] == 2

    # Por mes es un solo período; por semana también (1 al 2 de septiembre de 2026 caen en la misma).
    assert [x["periodo"] for x in _reporte(abrir, desde=D1, hasta=D3, agrupacion="mes")["periodos"]] == ["2026-09"]
    assert len(_reporte(abrir, desde=D1, hasta=D3, agrupacion="semana")["periodos"]) == 1


def test_el_rango_deja_afuera_lo_que_no_es_del_periodo(abrir_ventas):
    abrir = abrir_ventas
    with abrir() as conn:
        yerba = _producto(conn, "Yerba", 100.0, 60.0)
    _venta(abrir, [(yerba, "Yerba", 1, 100.0)], fecha=D1)
    _venta(abrir, [(yerba, "Yerba", 5, 100.0)], fecha=D3)
    assert _por_nombre(_reporte(abrir, desde=D1, hasta=D2))["Yerba"]["unidades"] == 1.0
    assert _por_nombre(_reporte(abrir, desde=D3, hasta=D3))["Yerba"]["unidades"] == 5.0
    # Sin rango entran las dos, y sin rango cerrado no hay "por día".
    sin_rango = _reporte(abrir)
    assert _por_nombre(sin_rango)["Yerba"]["unidades"] == 6.0
    assert sin_rango["resumen"]["dias"] is None and _por_nombre(sin_rango)["Yerba"]["unidades_por_dia"] is None


def test_una_venta_sin_fecha_no_se_pierde_ni_rompe_el_agrupado(abrir_ventas):
    abrir = abrir_ventas
    with abrir() as conn:
        yerba = _producto(conn, "Yerba", 100.0, 60.0)
    venta = _venta(abrir, [(yerba, "Yerba", 2, 100.0)])
    with abrir() as conn:
        conn.execute("UPDATE sales SET occurred_on=NULL WHERE id=?", (venta,))
        conn.commit()
    for agrupacion in ("dia", "semana", "mes"):
        r = _reporte(abrir, agrupacion=agrupacion)
        assert [(x["periodo"], x["unidades"]) for x in r["periodos"]] == [(margen.SIN_FECHA, 2.0)]
    # Con un rango de fechas no entra: no se sabe de cuándo es.
    assert _reporte(abrir, desde=D1, hasta=D3)["productos"] == []


def test_una_venta_anulada_o_pendiente_de_cobro_no_es_una_venta(abrir_ventas):
    abrir = abrir_ventas
    with abrir() as conn:
        yerba = _producto(conn, "Yerba", 100.0, 60.0)
    _venta(abrir, [(yerba, "Yerba", 1, 100.0)])
    anulada = _venta(abrir, [(yerba, "Yerba", 10, 100.0)])
    _venta(abrir, [(yerba, "Yerba", 100, 100.0)], acreditado=False)  # el QR que nadie escaneó: draft
    with abrir() as conn:
        assert ventas.anular_venta(conn, anulada, usuario_id=USUARIO["id"]) is True
        conn.commit()
        estados = {r["number"]: r["status"] for r in conn.execute("SELECT number, status FROM sales").fetchall()}
    assert sorted(estados.values()) == ["cancelled", "confirmed", "draft"]

    r = _reporte(abrir, desde=D1, hasta=D3)
    assert _por_nombre(r)["Yerba"]["unidades"] == 1.0 and r["resumen"]["ingreso"] == 100.0


def test_una_devolucion_parcial_resta_unidades_ingreso_y_costo(abrir_ventas):
    abrir = abrir_ventas
    with abrir() as conn:
        yerba = _producto(conn, "Yerba", 100.0, 60.0)
    venta = _venta(abrir, [(yerba, "Yerba", 4, 100.0)])
    linea, deposito = _linea_de(abrir, venta)
    with abrir() as conn:
        ventas.devolver_items(conn, venta, {linea: 1}, deposito_id=deposito, usuario_id=USUARIO["id"])
        conn.commit()
        # La venta sigue `confirmed`: por eso hay que leer el ledger.
        assert conn.execute("SELECT status FROM sales WHERE id=?", (venta,)).fetchone()["status"] == "confirmed"

    p = _por_nombre(_reporte(abrir, desde=D1, hasta=D3))["Yerba"]
    assert (p["unidades"], p["ingreso"], p["costo"], p["margen"]) == (3.0, 300.0, 180.0, 120.0)

    # Devolver el resto: no vendió nada, no aparece (ni en productos ni en períodos).
    with abrir() as conn:
        ventas.devolver_items(conn, venta, {linea: 3}, deposito_id=deposito, usuario_id=USUARIO["id"])
        conn.commit()
    r = _reporte(abrir, desde=D1, hasta=D3)
    assert r["productos"] == [] and r["periodos"] == [] and r["resumen"]["ingreso"] == 0.0
    assert r["resumen"]["margen_pct"] is None  # sin ingreso no hay porcentaje


def test_la_devolucion_del_camino_viejo_tambien_se_resta(abrir_ventas):
    """`usecases.sales.return_sale_items` deja `sales.status` en `partially_returned` y el ledger con
    `source_type='sale_return'`: la venta sigue siendo una venta, menos lo devuelto. Y esa venta trae el
    snapshot de costo del dominio."""
    abrir = abrir_ventas
    with abrir() as conn:
        yerba = _producto(conn, "Yerba", 100.0, 999.0)  # el costo de hoy NO es el de la venta
        repo = SqliteCommerceRepository(conn)
        linea = SaleItem(kind=CatalogItemType.PRODUCT, item_id=yerba, description_snapshot="Yerba",
                         quantity=Decimal("4"), unit_price=Decimal("100"), unit_cost_snapshot=Decimal("60"))
        venta = Sale(id=None, number="D-1", items=(linea,), subtotal=Decimal("400"), total=Decimal("400"),
                     occurred_on=D1)
        deposito = catalogo.get_default_deposito_id(conn)
        ahora = datetime.datetime(2026, 9, 1, 12, 0)
        confirmada = confirm_sale(repo, venta, deposito, ahora)
        devuelta, _importe = return_sale_items(repo, confirmada, {0: Decimal("1")}, deposito, ahora)
        assert devuelta.status.value == "partially_returned"
        conn.commit()

    p = _por_nombre(_reporte(abrir, desde=D1, hasta=D3))["Yerba"]
    assert (p["unidades"], p["ingreso"], p["costo"]) == (3.0, 300.0, 180.0)
    assert p["costo_estimado"] is False  # el costo es el de la venta (60), no el de hoy (999)


@pytest.mark.parametrize("hora", [" 13:00:00", "T13:00:00"])
def test_una_venta_con_hora_entra_en_el_dia_que_se_pide(abrir_ventas, hora):
    """`POST /api/ventas` acepta `fecha` como texto libre: `2026-09-02 13:00:00` es del 2 de septiembre, y
    `hasta=2026-09-02` no la puede dejar afuera (un `<=` contra el texto la dejaba: `'... 13:00:00' > '2026-09-02'`).
    Lo mismo para la devolución de esa venta: se resta con el mismo filtro."""
    abrir = abrir_ventas
    with abrir() as conn:
        yerba = _producto(conn, "Yerba", 100.0, 60.0)
    con_hora = _venta(abrir, [(yerba, "Yerba", 4, 100.0)], fecha=D2 + hora)
    _venta(abrir, [(yerba, "Yerba", 1, 100.0)], fecha=D1)
    _venta(abrir, [(yerba, "Yerba", 8, 100.0)], fecha=D3 + hora)  # es del 3: afuera de ..D2
    linea, deposito = _linea_de(abrir, con_hora)
    with abrir() as conn:
        ventas.devolver_items(conn, con_hora, {linea: 1}, deposito_id=deposito, usuario_id=USUARIO["id"])
        conn.commit()

    r = _reporte(abrir, desde=D1, hasta=D2)
    assert _por_nombre(r)["Yerba"]["unidades"] == 4.0  # 1 del 1 + (4 - 1 devuelta) del 2
    assert [x["periodo"] for x in r["periodos"]] == [D1, D2]
    # Un solo día, el de la venta con hora, con `desde` y `hasta` iguales.
    assert _por_nombre(_reporte(abrir, desde=D2, hasta=D2))["Yerba"]["unidades"] == 3.0
    # Y `desde` con la hora de la venta en el mismo día no la deja afuera: se compara por día.
    assert _por_nombre(_reporte(abrir, desde=D3 + " 20:00:00", hasta=D3))["Yerba"]["unidades"] == 8.0


def test_una_devolucion_se_reparte_entre_las_lineas_de_la_misma_clave(abrir_ventas):
    """Límite conocido (ADR-015): el ledger de `devolver_items` NO trae la línea (`source_id` es la venta y
    `reason_code='devolucion'`), así que con dos líneas del mismo producto a distinto precio no se puede saber
    de cuál volvió: se reparte por cantidad. Devolver la de $100 de una venta de $100 + $200 deja $150, no $200.
    Si algún día el ledger guarda la línea, este test es el que hay que dar vuelta."""
    abrir = abrir_ventas
    with abrir() as conn:
        yerba = _producto(conn, "Yerba", 100.0, 60.0)
    venta = _venta(abrir, [(yerba, "Yerba", 1, 100.0), (yerba, "Yerba", 1, 200.0)])
    with abrir() as conn:
        primera = conn.execute("SELECT id FROM sale_items WHERE sale_id=? ORDER BY id", (venta,)).fetchone()["id"]
        _, deposito = _linea_de(abrir, venta)
        ventas.devolver_items(conn, venta, {primera: 1}, deposito_id=deposito, usuario_id=USUARIO["id"])
        conn.commit()
        filas = conn.execute(
            "SELECT * FROM stock_movements WHERE source_id=? AND reason_code='devolucion'", (venta,)
        ).fetchall()
        # El ledger no dice de qué línea: ninguna columna lo lleva.
        assert len(filas) == 1 and "sale_item_id" not in filas[0].keys()
        assert filas[0]["source_type"] == "venta" and filas[0]["note"] == f"Devolución venta ID {venta}"

    p = _por_nombre(_reporte(abrir, desde=D1, hasta=D3))["Yerba"]
    assert (p["unidades"], p["ingreso"]) == (1.0, 150.0)


def test_una_linea_devuelta_entera_no_arrastra_sus_avisos_de_costo(abrir_ventas):
    """Lo que se devolvió todo no aporta nada al margen: que su costo sea estimado o falte no puede avisar nada en
    el resumen ni en el período, donde el resto de las líneas tiene costo de verdad."""
    abrir = abrir_ventas
    with abrir() as conn:
        sin_costo = _producto(conn, "Sin costo", 100.0, 0.0)
        estimado = _producto(conn, "Estimado", 100.0, 70.0)
        firme = _producto(conn, "Firme", 100.0, 60.0)
    venta = _venta(abrir, [(sin_costo, "Sin costo", 2, 100.0), (estimado, "Estimado", 1, 100.0),
                           (firme, "Firme", 3, 100.0)])
    with abrir() as conn:
        conn.execute("UPDATE sale_items SET unit_cost_snapshot=? WHERE sale_id=? AND item_id=?", (60, venta, firme))
        conn.commit()
    with abrir() as conn:
        lineas = {f["item_id"]: f["id"] for f in conn.execute("SELECT id, item_id FROM sale_items WHERE sale_id=?",
                                                              (venta,)).fetchall()}
        _, deposito = _linea_de(abrir, venta)
        ventas.devolver_items(conn, venta, {lineas[sin_costo]: 2, lineas[estimado]: 1},
                              deposito_id=deposito, usuario_id=USUARIO["id"])
        conn.commit()

    r = _reporte(abrir, desde=D1, hasta=D3)
    assert [p["nombre"] for p in r["productos"]] == ["Firme"]
    assert (r["resumen"]["ingreso"], r["resumen"]["costo"]) == (300.0, 180.0)
    assert r["resumen"]["sin_costo"] is False and r["resumen"]["costo_estimado"] is False
    assert r["resumen"]["productos_sin_costo"] == 0 and r["resumen"]["productos_costo_estimado"] == 0
    assert [(x["periodo"], x["sin_costo"], x["costo_estimado"]) for x in r["periodos"]] == [(D1, False, False)]
    assert _reporte(abrir, desde=D1, hasta=D3, producto_id=sin_costo)["resumen"]["sin_costo"] is False


def test_costo_del_snapshot_o_el_de_hoy_avisando(abrir_ventas):
    abrir = abrir_ventas
    with abrir() as conn:
        con_snapshot = _producto(conn, "Con snapshot", 100.0, 60.0)
        sin_snapshot = _producto(conn, "Sin snapshot", 100.0, 60.0)
    v = _venta(abrir, [(con_snapshot, "Con snapshot", 2, 100.0), (sin_snapshot, "Sin snapshot", 2, 100.0)])
    with abrir() as conn:
        # `crear_venta` no escribe el snapshot: se lo pone a una sola línea, como lo haría el dominio.
        conn.execute("UPDATE sale_items SET unit_cost_snapshot=? WHERE sale_id=? AND item_id=?", (50, v, con_snapshot))
        # Y el costo de hoy sube después de la venta.
        conn.execute("UPDATE catalog_items SET default_cost=? WHERE id IN (?, ?)", (80, con_snapshot, sin_snapshot))
        conn.commit()

    p = _por_nombre(_reporte(abrir, desde=D1, hasta=D3))
    assert p["Con snapshot"]["costo"] == 100.0 and p["Con snapshot"]["costo_estimado"] is False  # 2 x 50
    assert p["Sin snapshot"]["costo"] == 160.0 and p["Sin snapshot"]["costo_estimado"] is True  # 2 x 80, el de hoy
    r = _reporte(abrir, desde=D1, hasta=D3)
    assert r["resumen"]["productos_costo_estimado"] == 1 and r["resumen"]["productos_sin_costo"] == 0


def test_un_producto_sin_costo_se_marca_en_vez_de_mostrar_100_por_ciento_como_real(abrir_ventas):
    abrir = abrir_ventas
    with abrir() as conn:
        sin_costo = _producto(conn, "Sin costo", 100.0, 0.0)
        con_costo = _producto(conn, "Con costo", 100.0, 60.0)
    _venta(abrir, [(sin_costo, "Sin costo", 1, 100.0), (con_costo, "Con costo", 1, 100.0)])
    r = _reporte(abrir, desde=D1, hasta=D3)
    p = _por_nombre(r)
    assert p["Sin costo"]["sin_costo"] is True and p["Sin costo"]["costo_estimado"] is False
    assert p["Sin costo"]["margen_pct"] == 100.0  # el número está, pero la marca dice que no vale
    assert p["Con costo"]["sin_costo"] is False
    assert r["resumen"]["productos_sin_costo"] == 1 and r["resumen"]["sin_costo"] is True


def test_el_descuento_de_la_venta_se_reparte_entre_las_lineas(abrir_ventas):
    """Una venta de $300 (yerba) + $100 (azúcar) con $40 de descuento: cada línea cede lo que le toca
    (30 y 10), y el margen sale de lo que se cobró, no del precio de lista."""
    abrir = abrir_ventas
    with abrir() as conn:
        yerba = _producto(conn, "Yerba", 100.0, 60.0)
        azucar = _producto(conn, "Azúcar", 100.0, 40.0)
    _venta(abrir, [(yerba, "Yerba", 3, 100.0), (azucar, "Azúcar", 1, 100.0)], descuento=40.0)
    p = _por_nombre(_reporte(abrir, desde=D1, hasta=D3))
    assert (p["Yerba"]["ingreso"], p["Yerba"]["costo"], p["Yerba"]["margen"]) == (270.0, 180.0, 90.0)
    assert (p["Azúcar"]["ingreso"], p["Azúcar"]["costo"], p["Azúcar"]["margen"]) == (90.0, 40.0, 50.0)


def test_una_linea_de_servicio_toma_su_parte_del_descuento_pero_no_es_un_producto(abrir_ventas):
    abrir = abrir_ventas
    with abrir() as conn:
        yerba = _producto(conn, "Yerba", 100.0, 60.0)
    # Un "Envío" ad-hoc: sin producto. $100 de yerba + $100 de envío, $20 de descuento -> la yerba cede 10.
    _venta(abrir, [(yerba, "Yerba", 1, 100.0), (None, "Envío", 1, 100.0)], descuento=20.0)
    r = _reporte(abrir, desde=D1, hasta=D3)
    assert [p["nombre"] for p in r["productos"]] == ["Yerba"]
    assert r["productos"][0]["ingreso"] == 90.0


def test_el_descuento_de_linea_y_el_de_cabecera_no_se_restan_dos_veces(abrir_ventas):
    """El dominio puede dejar el descuento en la línea Y en `discount_total` de la venta (es el mismo
    dinero): lo que ya explica la línea no se vuelve a repartir."""
    abrir = abrir_ventas
    with abrir() as conn:
        yerba = _producto(conn, "Yerba", 100.0, 60.0)
        repo = SqliteCommerceRepository(conn)
        linea = SaleItem(kind=CatalogItemType.PRODUCT, item_id=yerba, description_snapshot="Yerba",
                         quantity=Decimal("2"), unit_price=Decimal("100"), discount_amount=Decimal("10"),
                         unit_cost_snapshot=Decimal("60"))
        venta = Sale(id=None, number="D-2", items=(linea,), subtotal=Decimal("200"), discount_total=Decimal("10"),
                     total=Decimal("190"), occurred_on=D1)
        confirm_sale(repo, venta, catalogo.get_default_deposito_id(conn), datetime.datetime(2026, 9, 1, 12, 0))
        conn.commit()
    p = _por_nombre(_reporte(abrir, desde=D1, hasta=D3))["Yerba"]
    assert p["ingreso"] == 190.0  # 200 - 10, no 200 - 10 - 10


def test_pedir_un_producto_deja_sus_tres_partes_sobre_ese_producto(abrir_ventas):
    abrir = abrir_ventas
    with abrir() as conn:
        yerba = _producto(conn, "Yerba", 100.0, 60.0)
        azucar = _producto(conn, "Azúcar", 50.0, 40.0)
    _venta(abrir, [(yerba, "Yerba", 3, 100.0), (azucar, "Azúcar", 2, 50.0)], fecha=D1)
    _venta(abrir, [(yerba, "Yerba", 1, 100.0)], fecha=D2)
    r = _reporte(abrir, desde=D1, hasta=D3, producto_id=yerba)
    assert [p["nombre"] for p in r["productos"]] == ["Yerba"]
    assert [(x["periodo"], x["unidades"]) for x in r["periodos"]] == [(D1, 3.0), (D2, 1.0)]  # su rotación
    assert r["resumen"]["ingreso"] == 400.0


def test_el_orden_es_el_pedido_y_lo_que_no_tiene_valor_queda_al_final(abrir_ventas):
    abrir = abrir_ventas
    with abrir() as conn:
        a = _producto(conn, "A", 100.0, 90.0)  # margen 10 (10%)
        b = _producto(conn, "B", 100.0, 20.0)  # margen 80 (80%)
        c = _producto(conn, "C", 10.0, 1.0)  # margen 9 (90%)
    _venta(abrir, [(a, "A", 1, 100.0), (b, "B", 1, 100.0), (c, "C", 1, 10.0)])

    def nombres(**kw):
        return [p["nombre"] for p in _reporte(abrir, desde=D1, hasta=D3, **kw)["productos"]]

    assert nombres() == ["B", "A", "C"]  # margen $ descendente (default)
    assert nombres(orden="margen", sentido="asc") == ["C", "A", "B"]
    assert nombres(orden="margen_pct") == ["C", "B", "A"]
    assert nombres(orden="nombre", sentido="desc") == ["C", "B", "A"]
    assert nombres(orden="ingreso") == ["A", "B", "C"]  # empate A/B: por nombre

    # Sin ingreso el % no existe: va al final aunque se pida ascendente.
    with abrir() as conn:
        conn.execute("UPDATE sale_items SET unit_price=0 WHERE item_id=?", (a,))
        conn.commit()
    assert nombres(orden="margen_pct", sentido="asc")[-1] == "A"
    assert nombres(orden="margen_pct", sentido="desc")[-1] == "A"


@pytest.mark.parametrize("kw", [{"orden": "inventado"}, {"sentido": "arriba"}, {"agrupacion": "siglo"}])
def test_un_parametro_desconocido_es_un_error_y_no_un_default_silencioso(abrir_ventas, kw):
    with abrir_ventas() as conn, pytest.raises(ValueError):
        margen.reporte_margen(conn, **kw)


def test_solo_lee(abrir_ventas):
    abrir = abrir_ventas
    with abrir() as conn:
        yerba = _producto(conn, "Yerba", 100.0, 60.0)
    _venta(abrir, [(yerba, "Yerba", 1, 100.0)])
    tablas = ("sales", "sale_items", "stock_movements", "catalog_items")
    with abrir() as conn:
        antes = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tablas}
        snapshot = conn.execute("SELECT unit_cost_snapshot FROM sale_items").fetchone()[0]
        margen.reporte_margen(conn, D1, D3)
        assert {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tablas} == antes
        assert conn.execute("SELECT unit_cost_snapshot FROM sale_items").fetchone()[0] == snapshot


# ── El contrato HTTP ─────────────────────────────────────────────────────


def _cliente(abrir) -> TestClient:
    app = FastAPI()
    app.include_router(build_margen_router(conexion=abrir))
    return TestClient(app)


def test_get_devuelve_resumen_productos_y_periodos(abrir_ventas):
    abrir = abrir_ventas
    with abrir() as conn:
        yerba = _producto(conn, "Yerba", 100.0, 60.0)
    _venta(abrir, [(yerba, "Yerba", 3, 100.0)])
    r = _cliente(abrir).get("/api/reportes/margen", params={"desde": D1, "hasta": D3, "agrupacion": "mes"})
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert (cuerpo["desde"], cuerpo["hasta"], cuerpo["agrupacion"], cuerpo["orden"], cuerpo["sentido"]) == (
        D1, D3, "mes", "margen", "desc")
    assert cuerpo["productos"][0]["margen"] == 120.0 and cuerpo["periodos"][0]["periodo"] == "2026-09"
    assert cuerpo["resumen"]["unidades"] == 3.0


def test_sin_fechas_es_el_mes_en_curso(abrir_ventas):
    abrir = abrir_ventas
    with abrir() as conn:
        yerba = _producto(conn, "Yerba", 100.0, 60.0)
    hoy = datetime.date.today()
    _venta(abrir, [(yerba, "Yerba", 1, 100.0)], fecha=hoy.isoformat())
    _venta(abrir, [(yerba, "Yerba", 7, 100.0)], fecha=D1)  # de otro mes (2026-09 no es el mes de "hoy" si hoy no es septiembre)
    cuerpo = _cliente(abrir).get("/api/reportes/margen").json()
    assert cuerpo["desde"] == hoy.replace(day=1).isoformat() and cuerpo["hasta"] == hoy.isoformat()
    esperado = 8.0 if hoy.strftime("%Y-%m") == "2026-09" else 1.0
    assert cuerpo["resumen"]["unidades"] == esperado


def test_parametros_invalidos_son_422(abrir_ventas):
    c = _cliente(abrir_ventas)
    for params in ({"orden": "inventado"}, {"sentido": "arriba"}, {"agrupacion": "siglo"}):
        assert c.get("/api/reportes/margen", params=params).status_code == 422
    assert c.get("/api/reportes/margen/export/productos", params={"orden": "inventado"}).status_code == 422
    assert c.get("/api/reportes/margen/export/periodos", params={"agrupacion": "siglo"}).status_code == 422


def test_export_csv_de_productos_y_de_periodos(abrir_ventas):
    abrir = abrir_ventas
    with abrir() as conn:
        con_costo = _producto(conn, "Yerba", 100.0, 60.0)
        sin_costo = _producto(conn, "Sin costo", 100.0, 0.0)
    _venta(abrir, [(con_costo, "Yerba", 3, 100.0), (sin_costo, "Sin costo", 1, 100.0)])
    c = _cliente(abrir)

    r = c.get("/api/reportes/margen/export/productos", params={"desde": D1, "hasta": D3, "orden": "nombre", "sentido": "asc"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert f'filename="margen_productos_{D1}_{D3}.csv"' in r.headers["content-disposition"]
    lineas = r.text.splitlines()
    assert lineas[0] == ("producto_id,nombre,unidades,unidades_por_dia,ingreso,costo,margen,margen_pct,"
                         "costo_estimado,sin_costo")
    # Ordenado por nombre ascendente, y los booleanos legibles.
    assert lineas[1] == f"{sin_costo},Sin costo,1.0,0.33,100.0,0.0,100.0,100.0,no,si"
    assert lineas[2] == f"{con_costo},Yerba,3.0,1.0,300.0,180.0,120.0,40.0,si,no"

    r = c.get("/api/reportes/margen/export/periodos", params={"desde": D1, "hasta": D3})
    assert f'filename="margen_periodos_{D1}_{D3}.csv"' in r.headers["content-disposition"]
    lineas = r.text.splitlines()
    assert lineas[0] == "periodo,unidades,ingreso,costo,margen,margen_pct,costo_estimado,sin_costo"
    assert lineas[1] == f"{D1},4.0,400.0,180.0,220.0,55.0,si,si"


def test_el_router_no_expone_nada_que_escriba(abrir_ventas):
    router = build_margen_router(conexion=abrir_ventas)
    assert {m for r in router.routes for m in r.methods} == {"GET"}
