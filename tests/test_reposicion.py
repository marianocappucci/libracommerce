"""Reposición sugerida —qué pedir— (ADR-017), contra los dos motores.

Lo que se mide acá es lo que decide si la sugerencia se puede seguir: la fórmula con números que se hacen a mano,
qué NO cuenta como venta (el mismo criterio que el margen), qué cuenta como «en camino», que cada sucursal se
mire con sus depósitos y sus ventas (y no con las del vecino), que no se sugiera 2,3 unidades de algo entero, y cómo
se trata un producto que estuvo sin stock."""

from __future__ import annotations

import datetime
import inspect
from decimal import Decimal

import pytest
import test_vencimientos as _vto
from conftest import USUARIO
from fastapi import FastAPI
from fastapi.testclient import TestClient

from libracommerce.db.repository import SqliteCommerceRepository, repositorio_de
from libracommerce.domain.catalog import CatalogItemType
from libracommerce.domain.entities import Party, PartyType
from libracommerce.domain.sales import Sale, SaleItem
from libracommerce.erp import catalogo, compras, reposicion, stock, vencimientos, ventas
from libracommerce.usecases.sales import confirm_sale
from libracommerce.web.reposicion_router import build_reposicion_router

#: «Hoy» de las pruebas de la función: la ventana de 30 días es del 1 al 30 de septiembre de 2026.
HOY = datetime.date(2026, 9, 30)
#: Antes de la ventana: de ahí sale el stock inicial, para que no haya días sin stock que no sean del caso.
PREVIA = "2026-08-15"


def _producto(conn, nombre, *, inicial=None, minimo=0.0, unidad="u", fraccion=None, categoria="", deposito=None):
    pid = catalogo.create_producto(conn, nombre, precio_venta=100.0, precio_costo=60.0, stock_minimo=minimo,
                                   unidad=unidad, permite_fraccion=fraccion, categoria=categoria)
    if inicial is not None:
        stock.ajustar_stock(conn, pid, inicial, "inicial", usuario_id=USUARIO["id"], fecha=PREVIA,
                            deposito_id=deposito)
    return pid


def _venta(abrir, items, fecha, deposito_id=None, acreditado=True):
    """Una venta de mostrador por el mismo camino que `POST /api/ventas`. `items`: `(producto_id, nombre, qty, precio)`."""
    lineas = [{"nombre": n, "qty": q, "precio": p, "subtotal": round(q * p, 2), "producto_id": pid}
              for pid, n, q, p in items]
    total = round(sum(li["subtotal"] for li in lineas), 2)
    pagos = [{"medio": "efectivo", "monto": total, "estado": "aprobado" if acreditado else "pendiente"}]
    return ventas.crear_venta_directa(
        abrir, fecha=fecha, items=lineas, subtotal=total, descuento=0.0, total=total,
        cliente_id=None, cliente_nombre="", usuario_id=USUARIO["id"], observaciones="",
        estado=ventas.estado_segun_pagos(total, pagos), pagos=pagos, stock_habilitado=True, deposito_id=deposito_id,
    )


def _reporte(abrir, **kw):
    kw.setdefault("hoy", HOY)
    with abrir() as conn:
        return reposicion.sugerencia_reposicion(conn, **kw)


def _por_nombre(filas):
    return {f["nombre"]: f for f in filas}


def _proveedor(abrir) -> int:
    with abrir() as conn:
        return repositorio_de(conn).save_party(Party(None, PartyType.ORGANIZATION, "Distribuidora SA")).id


def _orden(abrir, proveedor, lineas, *, branch_id=None, estado=None) -> int:
    """Una orden de compra con sus líneas `(producto_id, cantidad)`; `estado` la deja en ese `status` si se pide."""
    with abrir() as conn:
        oid = compras.crear_orden(conn, supplier_party_id=proveedor, branch_id=branch_id)["id"]
        for pid, cantidad in lineas:
            compras.agregar_linea_orden(conn, oid, item_id=pid, quantity_ordered=Decimal(str(cantidad)),
                                        unit_cost=Decimal("60"))
        if estado:
            conn.execute("UPDATE purchase_orders SET status=? WHERE id=?", (estado, oid))
        conn.commit()
    return oid


def _recibir(abrir, proveedor, orden_id, lineas, deposito):
    """Confirma una recepción de `lineas` `(producto_id, cantidad)` contra la orden, en el depósito."""
    with abrir() as conn:
        rid = compras.crear_recepcion(conn, supplier_party_id=proveedor, purchase_order_id=orden_id)["id"]
        for pid, cantidad in lineas:
            compras.agregar_linea_recepcion(conn, rid, item_id=pid, quantity=Decimal(str(cantidad)),
                                            unit_cost=Decimal("60"))
        compras.confirmar_recepcion(conn, rid, location_id=deposito, occurred_at=datetime.datetime(2026, 8, 20, 9))
        conn.commit()


def _yerba_de_referencia(abrir, *, minimo=0.0):
    """Yerba con 40 antes de la ventana y 30 vendidas (10 el 10 y 20 el 20): queda stock 10 y rota 1 por día."""
    with abrir() as conn:
        yerba = _producto(conn, "Yerba", inicial=40.0, minimo=minimo)
    _venta(abrir, [(yerba, "Yerba", 10, 100.0)], "2026-09-10")
    _venta(abrir, [(yerba, "Yerba", 20, 100.0)], "2026-09-20")
    return yerba


# ── La fórmula ───────────────────────────────────────────────────────────


def test_formula_rotacion_cobertura_en_camino_y_piso_de_min_stock(abrir_ventas):
    abrir = abrir_ventas
    yerba = _yerba_de_referencia(abrir)
    proveedor = _proveedor(abrir)

    # Sin nada en camino: rota 30 / 30 = 1 por día, hay 10 (cobertura de 10 días) y hay que cubrir 15 + 3 = 18:
    # 1 × 18 − 10 = 8.
    [fila] = _reporte(abrir)
    assert (fila["producto_id"], fila["stock"], fila["unidades_vendidas"], fila["rotacion_diaria"]) == (
        yerba, 10.0, 30.0, 1.0)
    assert (fila["cobertura_dias"], fila["en_camino"], fila["sugerido"], fila["motivo"]) == (10.0, 0.0, 8, "por_rotacion")
    assert fila["sin_ventas"] is False and fila["posible_quiebre"] is False and fila["dias_con_stock"] == 30

    # Con 5 en una orden abierta: 1 × 18 − 10 − 5 = 3.
    _orden(abrir, proveedor, [(yerba, 5)])
    [fila] = _reporte(abrir)
    assert (fila["en_camino"], fila["sugerido"]) == (5.0, 3)

    # Los parámetros mueven la cuenta: 10 días de cobertura y 2 de plazo son 12 (12 − 10 − 5 < 0: nada que pedir), y con
    # una rotación de 15 días sólo entra la venta del 20 (20 unidades, 20 × 18 / 15 = 24): 24 − 10 − 5 = 9.
    assert _reporte(abrir, dias_cobertura=10, plazo_entrega_dias=2) == []
    [fila] = _reporte(abrir, dias_rotacion=15)
    assert (fila["unidades_vendidas"], fila["rotacion_diaria"], fila["sugerido"]) == (20.0, 1.333, 9)


def test_el_minimo_es_un_piso(abrir_ventas):
    abrir = abrir_ventas
    yerba = _yerba_de_referencia(abrir, minimo=20.0)
    proveedor = _proveedor(abrir)
    _orden(abrir, proveedor, [(yerba, 5)])

    # Hay 10 + 5 en camino = 15 < 20. Por rotación serían 3; para llegar al mínimo faltan 5: gana el piso, y
    # el motivo dice que son las dos cosas.
    [fila] = _reporte(abrir)
    assert (fila["sugerido"], fila["motivo"], fila["stock_minimo"]) == (5, "ambos", 20.0)

    # Con un mínimo más alto el piso manda solo lo que le falta (40 − 15), aunque por rotación fueran 3.
    with abrir() as conn:
        conn.execute("UPDATE catalog_items SET min_stock=40 WHERE id=?", (yerba,))
        conn.commit()
    assert _reporte(abrir)[0]["sugerido"] == 25

    # Con el mínimo cubierto (14 < 15 = 10 + 5) manda la rotación y el mínimo no aparece en el motivo.
    with abrir() as conn:
        conn.execute("UPDATE catalog_items SET min_stock=14 WHERE id=?", (yerba,))
        conn.commit()
    [fila] = _reporte(abrir)
    assert (fila["sugerido"], fila["motivo"]) == (3, "por_rotacion")


def test_un_producto_sin_minimo_no_tiene_piso(abrir_ventas):
    abrir = abrir_ventas
    with abrir() as conn:
        _producto(conn, "Sin mínimo", inicial=0.0, minimo=0.0)
        _producto(conn, "Con mínimo", inicial=2.0, minimo=10.0)
    filas = _reporte(abrir, solo_a_pedir=False)
    p = _por_nombre(filas)
    # `min_stock` en 0 es «sin mínimo»: con stock 0 y sin ventas no hay nada que pedir.
    assert (p["Sin mínimo"]["sugerido"], p["Sin mínimo"]["motivo"]) == (0, None)
    assert (p["Con mínimo"]["sugerido"], p["Con mínimo"]["motivo"]) == (8, "bajo_minimo")
    assert [f["nombre"] for f in _reporte(abrir)] == ["Con mínimo"]


def test_un_producto_sin_ventas_no_tiene_rotacion_ni_cobertura(abrir_ventas):
    abrir = abrir_ventas
    with abrir() as conn:
        _producto(conn, "Quieto", inicial=5.0, minimo=8.0)
        _producto(conn, "Quieto sin mínimo", inicial=5.0)
    p = _por_nombre(_reporte(abrir, solo_a_pedir=False))
    quieto = p["Quieto"]
    assert (quieto["sin_ventas"], quieto["unidades_vendidas"], quieto["rotacion_diaria"], quieto["cobertura_dias"]) == (
        True, 0.0, 0.0, None)
    assert (quieto["sugerido"], quieto["motivo"]) == (3, "bajo_minimo")  # sólo el piso
    assert p["Quieto sin mínimo"]["sugerido"] == 0 and p["Quieto sin mínimo"]["cobertura_dias"] is None
    # Los que no tienen rotación van al final, aunque tengan algo que pedir.
    with abrir() as conn:
        rota = _producto(conn, "Rota", inicial=5.0)
    _venta(abrir, [(rota, "Rota", 4, 100.0)], "2026-09-10")
    assert [f["nombre"] for f in _reporte(abrir, solo_a_pedir=False)][0] == "Rota"


def test_un_producto_con_todo_cubierto_no_aparece_salvo_que_se_pida(abrir_ventas):
    abrir = abrir_ventas
    with abrir() as conn:
        holgado = _producto(conn, "Holgado", inicial=500.0)
    _venta(abrir, [(holgado, "Holgado", 3, 100.0)], "2026-09-10")
    assert _reporte(abrir) == []
    [fila] = _reporte(abrir, solo_a_pedir=False)
    assert (fila["nombre"], fila["sugerido"], fila["motivo"]) == ("Holgado", 0, None)
    assert fila["cobertura_dias"] == 4970.0  # 497 / (3/30): un número, no un aviso


# ── Qué cuenta como venta (el criterio del margen) ───────────────────────


def test_una_venta_anulada_o_pendiente_de_cobro_no_es_rotacion(abrir_ventas):
    abrir = abrir_ventas
    with abrir() as conn:
        yerba = _producto(conn, "Yerba", inicial=1000.0)
    _venta(abrir, [(yerba, "Yerba", 3, 100.0)], "2026-09-10")
    anulada = _venta(abrir, [(yerba, "Yerba", 10, 100.0)], "2026-09-11")
    _venta(abrir, [(yerba, "Yerba", 100, 100.0)], "2026-09-12", acreditado=False)  # el QR que nadie escaneó
    with abrir() as conn:
        assert ventas.anular_venta(conn, anulada, usuario_id=USUARIO["id"]) is True
        conn.commit()
    [fila] = _reporte(abrir, solo_a_pedir=False)
    assert fila["unidades_vendidas"] == 3.0 and fila["rotacion_diaria"] == 0.1


def test_una_devolucion_resta_de_la_rotacion(abrir_ventas):
    abrir = abrir_ventas
    with abrir() as conn:
        yerba = _producto(conn, "Yerba", inicial=1000.0)
    venta = _venta(abrir, [(yerba, "Yerba", 10, 100.0)], "2026-09-10")
    with abrir() as conn:
        linea = conn.execute("SELECT id FROM sale_items WHERE sale_id=?", (venta,)).fetchone()["id"]
        deposito = conn.execute("SELECT location_id FROM stock_movements WHERE source_id=? LIMIT 1",
                                (venta,)).fetchone()["location_id"]
        ventas.devolver_items(conn, venta, {linea: 4}, deposito_id=deposito, usuario_id=USUARIO["id"])
        conn.commit()
    [fila] = _reporte(abrir, solo_a_pedir=False)
    assert fila["unidades_vendidas"] == 6.0 and fila["rotacion_diaria"] == 0.2


def test_lo_vendido_fuera_de_la_ventana_no_cuenta(abrir_ventas):
    abrir = abrir_ventas
    with abrir() as conn:
        yerba = _producto(conn, "Yerba", inicial=1000.0)
    _venta(abrir, [(yerba, "Yerba", 7, 100.0)], "2026-08-31")  # un día antes de la ventana de 30
    _venta(abrir, [(yerba, "Yerba", 3, 100.0)], "2026-09-01")  # el primer día
    _venta(abrir, [(yerba, "Yerba", 2, 100.0)], "2026-09-30 18:30:00")  # hoy, con hora
    assert _reporte(abrir, solo_a_pedir=False)[0]["unidades_vendidas"] == 5.0
    assert _reporte(abrir, solo_a_pedir=False, dias_rotacion=1)[0]["unidades_vendidas"] == 2.0
    assert _reporte(abrir, solo_a_pedir=False, dias_rotacion=31)[0]["unidades_vendidas"] == 12.0


# ── En camino ────────────────────────────────────────────────────────────


def test_una_orden_abierta_resta_y_una_recibida_entera_no(abrir_ventas):
    abrir = abrir_ventas
    yerba = _yerba_de_referencia(abrir)  # stock 10, rota 1 por día: pide 8
    proveedor = _proveedor(abrir)
    with abrir() as conn:
        deposito = catalogo.get_default_deposito_id(conn)

    # Recibida entera: el stock ya lo tiene (10 + 6), no queda nada en camino.
    entera = _orden(abrir, proveedor, [(yerba, 6)])
    _recibir(abrir, proveedor, entera, [(yerba, 6)], deposito)
    with abrir() as conn:
        assert conn.execute("SELECT status FROM purchase_orders WHERE id=?", (entera,)).fetchone()["status"] == "received"
    [fila] = _reporte(abrir)
    assert (fila["stock"], fila["en_camino"], fila["sugerido"]) == (16.0, 0.0, 2)  # 18 − 16

    # Una abierta de 10 con 4 recibidas: en camino es lo que falta (6), y las 4 ya están en el stock (20).
    parcial = _orden(abrir, proveedor, [(yerba, 10)])
    _recibir(abrir, proveedor, parcial, [(yerba, 4)], deposito)
    [fila] = _reporte(abrir, solo_a_pedir=False)
    assert (fila["stock"], fila["en_camino"], fila["sugerido"]) == (20.0, 6.0, 0)

    # Una cancelada no viene; una enviada sí; y una línea recibida de más no resta de las demás.
    _orden(abrir, proveedor, [(yerba, 50)], estado="cancelled")
    _orden(abrir, proveedor, [(yerba, 3)], estado="sent")
    with abrir() as conn:
        conn.execute("UPDATE purchase_order_items SET quantity_received=15 WHERE purchase_order_id=?", (entera,))
        conn.execute("UPDATE purchase_orders SET status='partial' WHERE id=?", (entera,))
        conn.commit()
    assert _reporte(abrir, solo_a_pedir=False)[0]["en_camino"] == 9.0  # 6 + 3; la sobre-recibida aporta 0, no −9


# ── Sucursales y depósitos ───────────────────────────────────────────────


def _dos_sucursales(abrir):
    with abrir() as conn:
        a = catalogo.create_sucursal(conn, "Centro")
        b = catalogo.create_sucursal(conn, "Norte")
        return a, b, catalogo.get_deposito_de_venta(conn, a), catalogo.get_deposito_de_venta(conn, b)


def test_cada_sucursal_se_mira_con_sus_ventas_y_su_stock(abrir_ventas):
    abrir = abrir_ventas
    a, b, dep_a, dep_b = _dos_sucursales(abrir)
    with abrir() as conn:
        principal = catalogo.get_default_deposito_id(conn)  # un depósito sin sucursal
        yerba = _producto(conn, "Yerba")
        for deposito, inicial in ((dep_a, 30.0), (dep_b, 50.0), (principal, 7.0)):
            stock.ajustar_stock(conn, yerba, inicial, "inicial", fecha=PREVIA, deposito_id=deposito)
    _venta(abrir, [(yerba, "Yerba", 25, 100.0)], "2026-09-10", deposito_id=dep_a)
    _venta(abrir, [(yerba, "Yerba", 3, 100.0)], "2026-09-11", deposito_id=dep_b)
    _venta(abrir, [(yerba, "Yerba", 5, 100.0)], "2026-09-12", deposito_id=principal)
    with abrir() as conn:
        # La venta de mostrador no guarda la sucursal (`sales.branch_id` en NULL): la sucursal es la de su depósito.
        assert [r[0] for r in conn.execute("SELECT DISTINCT branch_id FROM sales").fetchall()] == [None]

    # Centro: 25 vendidas (0,83 por día → 15 en 18 días) y le quedan 5: pide 10.
    [centro] = _reporte(abrir, sucursal_id=a)
    assert (centro["stock"], centro["unidades_vendidas"], centro["sugerido"]) == (5.0, 25.0, 10)
    # Norte vendió 3 y le sobra: no aparece.
    assert _reporte(abrir, sucursal_id=b) == []
    norte = _reporte(abrir, sucursal_id=b, solo_a_pedir=False)[0]
    assert (norte["stock"], norte["unidades_vendidas"], norte["sugerido"]) == (47.0, 3.0, 0)
    # La instancia entera suma todo (5 + 47 + 2 en stock, 33 vendidas) y ahí no falta nada: por eso se calcula por sucursal.
    [total] = _reporte(abrir, solo_a_pedir=False)
    assert (total["stock"], total["unidades_vendidas"], total["sugerido"]) == (54.0, 33.0, 0)


def test_la_sucursal_de_la_venta_manda_sobre_la_de_su_deposito(abrir_ventas):
    """Una venta del dominio (`save_sale`) sí trae `sales.branch_id`: si dice otra sucursal que la del depósito, gana."""
    abrir = abrir_ventas
    a, b, dep_a, _dep_b = _dos_sucursales(abrir)
    with abrir() as conn:
        yerba = _producto(conn, "Yerba", inicial=100.0, deposito=dep_a)
    venta = _venta(abrir, [(yerba, "Yerba", 6, 100.0)], "2026-09-10", deposito_id=dep_a)
    assert _reporte(abrir, sucursal_id=a, solo_a_pedir=False)[0]["unidades_vendidas"] == 6.0
    with abrir() as conn:
        conn.execute("UPDATE sales SET branch_id=? WHERE id=?", (b, venta))
        conn.commit()
    assert _reporte(abrir, sucursal_id=a, solo_a_pedir=False)[0]["unidades_vendidas"] == 0.0
    assert _reporte(abrir, sucursal_id=b, solo_a_pedir=False)[0]["unidades_vendidas"] == 6.0
    assert _reporte(abrir, solo_a_pedir=False)[0]["unidades_vendidas"] == 6.0  # el total las cuenta a todas


def test_la_venta_del_camino_del_dominio_tambien_es_de_la_sucursal_de_su_deposito(abrir_ventas):
    """`usecases.sales.confirm_sale` anota el ledger con `source_type='sale'` (no `venta`): la sucursal también sale de ahí
    cuando la venta no trae `branch_id`."""
    abrir = abrir_ventas
    a, b, dep_a, _dep_b = _dos_sucursales(abrir)
    with abrir() as conn:
        yerba = _producto(conn, "Yerba", inicial=100.0, deposito=dep_a)
        linea = SaleItem(kind=CatalogItemType.PRODUCT, item_id=yerba, description_snapshot="Yerba",
                         quantity=Decimal("4"), unit_price=Decimal("100"))
        venta = Sale(id=None, number="D-1", items=(linea,), subtotal=Decimal("400"), total=Decimal("400"),
                     occurred_on="2026-09-10")
        confirm_sale(SqliteCommerceRepository(conn), venta, dep_a, datetime.datetime(2026, 9, 10, 12, 0))
        conn.commit()
        assert conn.execute("SELECT branch_id FROM sales").fetchone()["branch_id"] is None
        assert conn.execute("SELECT DISTINCT source_type FROM stock_movements WHERE movement_type='sale'"
                            ).fetchone()["source_type"] == "sale"
    assert _reporte(abrir, sucursal_id=a, solo_a_pedir=False)[0]["unidades_vendidas"] == 4.0
    assert _reporte(abrir, sucursal_id=b, solo_a_pedir=False)[0]["unidades_vendidas"] == 0.0


def test_el_mismo_producto_en_dos_depositos_de_una_sucursal_suma(abrir_ventas):
    abrir = abrir_ventas
    a, _b, dep_a, _dep_b = _dos_sucursales(abrir)
    with abrir() as conn:
        segundo = catalogo.create_deposito(conn, "Trastienda Centro", branch_id=a)
        yerba = _producto(conn, "Yerba")
        stock.ajustar_stock(conn, yerba, 20.0, "inicial", fecha=PREVIA, deposito_id=dep_a)
        stock.ajustar_stock(conn, yerba, 10.0, "inicial", fecha=PREVIA, deposito_id=segundo)
    _venta(abrir, [(yerba, "Yerba", 12, 100.0)], "2026-09-10", deposito_id=dep_a)
    _venta(abrir, [(yerba, "Yerba", 18, 100.0)], "2026-09-20", deposito_id=segundo)
    # 30 vendidas entre los dos depósitos, y quedan 8 y −8: el stock de la sucursal es la suma, 0.
    [fila] = _reporte(abrir, sucursal_id=a)
    assert (fila["stock"], fila["unidades_vendidas"]) == (0.0, 30.0)
    # Un depósito desactivado ya no cuenta como stock de la sucursal.
    with abrir() as conn:
        stock.ajustar_stock(conn, yerba, 9.0, "recuento", fecha="2026-09-25", deposito_id=segundo)
        assert _reporte_en(conn, a)["stock"] == 17.0  # 8 + 9
        conn.execute("UPDATE locations SET active=0 WHERE id=?", (segundo,))
        assert _reporte_en(conn, a)["stock"] == 8.0


def _reporte_en(conn, sucursal_id):
    return reposicion.sugerencia_reposicion(conn, hoy=HOY, sucursal_id=sucursal_id, solo_a_pedir=False)[0]


def test_en_camino_por_sucursal_incluye_las_ordenes_sin_sucursal(abrir_ventas):
    abrir = abrir_ventas
    a, b, _dep_a, _dep_b = _dos_sucursales(abrir)
    with abrir() as conn:
        yerba = _producto(conn, "Yerba", inicial=1.0)
    proveedor = _proveedor(abrir)
    _orden(abrir, proveedor, [(yerba, 4)], branch_id=a)
    _orden(abrir, proveedor, [(yerba, 9)], branch_id=b)
    _orden(abrir, proveedor, [(yerba, 2)])  # sin sucursal: no se sabe a dónde va
    en_camino = {
        sucursal: (f["en_camino"], f["en_camino_sin_sucursal"])
        for sucursal in (a, b, None)
        for f in _reporte(abrir, sucursal_id=sucursal, solo_a_pedir=False)
    }
    assert en_camino == {a: (6.0, 2.0), b: (11.0, 2.0), None: (15.0, 2.0)}


def test_una_sucursal_que_no_existe_es_un_error(abrir_ventas):
    with abrir_ventas() as conn, pytest.raises(ValueError, match="no existe"):
        reposicion.sugerencia_reposicion(conn, sucursal_id=999)


# ── La unidad ────────────────────────────────────────────────────────────


def test_una_unidad_entera_se_pide_entera_y_una_fraccionable_no(abrir_ventas):
    abrir = abrir_ventas
    with abrir() as conn:
        entero = _producto(conn, "Fideos", inicial=13.0, unidad="u")
        fraccionable = _producto(conn, "Queso", inicial=12.0, unidad="kg", fraccion=True)
    _venta(abrir, [(entero, "Fideos", 12, 100.0), (fraccionable, "Queso", 11.5, 100.0)], "2026-09-25")
    # 10 días de rotación y 2 (1 + 1) de horizonte. Fideos: 12 × 2 / 10 = 2,4 − 1 en stock = 1,4 → 2 unidades.
    # Queso: 11,5 × 2 / 10 = 2,3 − 0,5 en stock = 1,8 kg, que se pide como 1,8 y no como 2.
    filas = _por_nombre(_reporte(abrir, dias_rotacion=10, dias_cobertura=1, plazo_entrega_dias=1))
    assert filas["Fideos"]["sugerido"] == 2 and isinstance(filas["Fideos"]["sugerido"], int)
    assert filas["Queso"]["sugerido"] == 1.8


def _unidad_fina(conn, nombre, *, inicial, minimo=0.0, escala=6):
    """Un producto en una unidad fraccionable de `escala` decimales, con `inicial` cargado por el ledger directo
    (`ajustar_stock` redondea el delta a 4 decimales y no sirve para cantidades más finas)."""
    pid = catalogo.create_producto(conn, nombre, precio_venta=100000.0, precio_costo=1.0, stock_minimo=minimo,
                                   unidad="mg", permite_fraccion=True)
    conn.execute("UPDATE units SET decimal_scale=? WHERE code='mg'", (escala,))
    if inicial:
        stock.add_movimiento_stock(conn, pid, "entrada", inicial, "inicial", fecha=PREVIA)
    return pid


def test_el_saldo_no_se_redondea_antes_de_la_cuenta(abrir_ventas):
    """Una unidad de 6 decimales con stock 0,00001 y mínimo 0,00001: el mínimo está cubierto. Redondear el saldo a 4
    decimales antes de comparar lo dejaba en 0 y pedía 0,00001."""
    abrir = abrir_ventas
    with abrir() as conn:
        _unidad_fina(conn, "Fino", inicial=0.00001, minimo=0.00001)
    assert _reporte(abrir) == []
    [fila] = _reporte(abrir, solo_a_pedir=False)
    assert (fila["stock"], fila["sugerido"], fila["motivo"]) == (0.00001, 0, None)
    # Y un mínimo apenas mayor sí falta, por lo que falta (0,00002 − 0,00001).
    with abrir() as conn:
        conn.execute("UPDATE catalog_items SET min_stock=0.00003")
    assert _reporte(abrir)[0]["sugerido"] == 0.00002


def test_todas_las_cantidades_se_informan_con_la_escala_de_la_unidad(abrir_ventas):
    """Con una unidad de 6 decimales un saldo real de 0,0004 no puede salir como 0,0: `stock`, `en_camino`, el mínimo, lo
    vendido y la rotación se formatean con la escala de la unidad, igual que `sugerido`."""
    abrir = abrir_ventas
    with abrir() as conn:
        fino = _unidad_fina(conn, "Fino", inicial=0.0004, minimo=0.0002)
    proveedor = _proveedor(abrir)
    _orden(abrir, proveedor, [(fino, 0.00025)])
    _venta(abrir, [(fino, "Fino", 0.00003, 100000.0)], "2026-09-10")  # 3,00 de venta
    [fila] = _reporte(abrir, solo_a_pedir=False)
    assert fila["stock"] == 0.00037  # 0,0004 − 0,00003
    assert fila["en_camino"] == 0.00025 and fila["en_camino_sin_sucursal"] == 0.00025
    assert fila["stock_minimo"] == 0.0002 and fila["unidades_vendidas"] == 0.00003
    assert fila["rotacion_diaria"] == 0.000001  # 0,00003 / 30
    # Una unidad de la escala de siempre (3 decimales) se informa como antes.
    with abrir() as conn:
        conn.execute("UPDATE units SET decimal_scale=3 WHERE code='mg'")
    assert _reporte(abrir, solo_a_pedir=False)[0]["stock"] == 0.0


def test_una_unidad_fraccionable_se_redondea_a_su_escala_decimal(abrir_ventas):
    abrir = abrir_ventas
    with abrir() as conn:
        queso = _producto(conn, "Queso", inicial=12.0, unidad="kg", fraccion=True)
        conn.execute("UPDATE units SET decimal_scale=1 WHERE code='kg'")
    _venta(abrir, [(queso, "Queso", 11.4, 100.0)], "2026-09-25")
    # 11,4 × 2 / 10 = 2,28 − 0,6 en stock = 1,68 kg: con un decimal, 1,7 (hacia arriba, no 1,68 ni 1,6).
    assert _reporte(abrir, dias_rotacion=10, dias_cobertura=1, plazo_entrega_dias=1)[0]["sugerido"] == 1.7


def test_el_piso_tambien_se_redondea_a_la_unidad(abrir_ventas):
    abrir = abrir_ventas
    with abrir() as conn:
        _producto(conn, "Fideos", inicial=1.0, minimo=3.5, unidad="u")
        _producto(conn, "Queso", inicial=1.0, minimo=3.5, unidad="kg", fraccion=True)
    p = _por_nombre(_reporte(abrir))
    assert p["Fideos"]["sugerido"] == 3  # faltan 2,5: se pide 3
    assert p["Queso"]["sugerido"] == 2.5


# ── El sesgo por quiebres ────────────────────────────────────────────────


def test_los_dias_sin_stock_y_sin_ventas_no_cuentan_para_la_rotacion(abrir_ventas):
    """Se cargan 40 el 16 (del 1 al 15 no había nada: 15 días sin stock) y se venden 30 el 20. Dividir por 30 daría
    1 por día; con los 15 días en que había stock es 30 / 15 = 2 por día, y eso es lo que pide."""
    abrir = abrir_ventas
    with abrir() as conn:
        yerba = catalogo.create_producto(conn, "Yerba", precio_venta=100.0, precio_costo=60.0)
        stock.ajustar_stock(conn, yerba, 40.0, "ingreso", fecha="2026-09-16")
    _venta(abrir, [(yerba, "Yerba", 30, 100.0)], "2026-09-20")
    [fila] = _reporte(abrir)
    assert (fila["dias_con_stock"], fila["rotacion_diaria"], fila["posible_quiebre"]) == (15, 2.0, True)
    assert (fila["stock"], fila["cobertura_dias"], fila["sugerido"]) == (10.0, 5.0, 26)  # 2 × 18 − 10


def test_un_dia_con_ventas_nunca_se_excluye_aunque_el_saldo_no_sea_positivo(abrir_ventas):
    """El saldo del ledger dice ≤ 0 pero se vendió: había producto (el inventario está mal cargado). Ese día cuenta,
    y el stock negativo se toma como 0 en la cuenta, no como deuda."""
    abrir = abrir_ventas
    with abrir() as conn:
        yerba = _producto(conn, "Yerba", inicial=10.0)
    for dia in (2, 4, 6, 8, 10, 12):
        _venta(abrir, [(yerba, "Yerba", 2, 100.0)], f"2026-09-{dia:02d}")
    [fila] = _reporte(abrir)
    # 12 vendidas y 10 cargadas: stock −2. Los días 11 y 13 al 30 (19) no tenían saldo positivo ni ventas; los 6 de venta
    # cuentan aunque el 12 haya dejado el saldo en −2. Rotación 12 / 11; 12 × 18 / 11 = 19,64 → 20 (no 22).
    assert (fila["stock"], fila["dias_con_stock"], fila["sugerido"]) == (-2.0, 11, 20)
    assert fila["posible_quiebre"] is True


def test_con_pocos_dias_de_stock_la_muestra_no_baja_de_siete(abrir_ventas):
    """Un producto que sólo tuvo stock 2 días y vendió 2 no rota 1 por día sino 2 / 7: un solo día con stock no puede
    disparar la rotación."""
    abrir = abrir_ventas
    with abrir() as conn:
        yerba = catalogo.create_producto(conn, "Yerba", precio_venta=100.0, precio_costo=60.0)
        stock.ajustar_stock(conn, yerba, 2.0, "ingreso", fecha="2026-09-29")
    _venta(abrir, [(yerba, "Yerba", 2, 100.0)], "2026-09-30")
    [fila] = _reporte(abrir)
    assert (fila["dias_con_stock"], fila["rotacion_diaria"]) == (2, round(2 / 7, 3))
    assert fila["sugerido"] == 6  # 2 × 18 / 7 = 5,14 → 6 (stock 0)


def test_un_producto_agotado_con_ventas_es_posible_quiebre(abrir_ventas):
    abrir = abrir_ventas
    with abrir() as conn:
        agotado = _producto(conn, "Agotado", inicial=6.0)
        holgado = _producto(conn, "Holgado", inicial=500.0)
    _venta(abrir, [(agotado, "Agotado", 6, 100.0), (holgado, "Holgado", 6, 100.0)], "2026-09-10")
    p = _por_nombre(_reporte(abrir, solo_a_pedir=False))
    assert p["Agotado"]["stock"] == 0.0 and p["Agotado"]["posible_quiebre"] is True
    assert p["Holgado"]["posible_quiebre"] is False
    # Le faltan los 20 días en que no tuvo (del 11 al 30): la rotación es 6 / 10 y hay que cubrir 18 días: 11 unidades.
    assert (p["Agotado"]["dias_con_stock"], p["Agotado"]["sugerido"]) == (10, 11)


# ── El resto ─────────────────────────────────────────────────────────────


def test_el_orden_es_por_urgencia(abrir_ventas):
    abrir = abrir_ventas
    with abrir() as conn:
        urgente = _producto(conn, "Urgente", inicial=40.0)
        alfa = _producto(conn, "Alfa", inicial=50.0)
        beta = _producto(conn, "Beta", inicial=100.0)
        _producto(conn, "Quieto", inicial=5.0, minimo=100.0)
    # Cobertura = stock / rotación diaria: Urgente 10 / 1 = 10 días, Alfa 20 / 1 = 20, Beta 40 / 2 = 20.
    _venta(abrir, [(urgente, "Urgente", 30, 100.0), (alfa, "Alfa", 30, 100.0), (beta, "Beta", 60, 100.0)], "2026-09-10")
    # Con 18 días de horizonte sólo hace falta pedir Urgente (18 − 10) y lo que empuja su mínimo, Quieto: el que no rota va al final.
    assert [(f["nombre"], f["cobertura_dias"]) for f in _reporte(abrir)] == [("Urgente", 10.0), ("Quieto", None)]
    # Con 48 (45 de cobertura y 3 de plazo) piden todos: menor cobertura primero y, a igual cobertura (Alfa y Beta, 20 días),
    # el que más hay que pedir (Beta, 56, antes que Alfa, 28: por eso no sale por orden alfabético).
    filas = _reporte(abrir, dias_cobertura=45)  # 48 días de horizonte
    assert [(f["nombre"], f["cobertura_dias"], f["sugerido"]) for f in filas] == [
        ("Urgente", 10.0, 38), ("Beta", 20.0, 56), ("Alfa", 20.0, 28), ("Quieto", None, 95)]


def test_filtra_por_categoria_y_por_producto(abrir_ventas):
    abrir = abrir_ventas
    with abrir() as conn:
        a = _producto(conn, "A", inicial=1.0, minimo=5.0, categoria="Almacén")
        _producto(conn, "B", inicial=1.0, minimo=5.0, categoria="Limpieza")
        inactivo = _producto(conn, "Inactivo", inicial=1.0, minimo=5.0)
        servicio = catalogo.create_producto(conn, "Flete", tipo="servicio", stock_minimo=5.0)
        conn.execute("UPDATE catalog_items SET active=0 WHERE id=?", (inactivo,))
        catalogo.create_variante(conn, a, "A-1", "A chica")
        catalogo.create_variante(conn, a, "A-2", "A grande")
    assert sorted(f["nombre"] for f in _reporte(abrir)) == ["A", "B"]  # ni el inactivo ni el servicio
    assert [f["nombre"] for f in _reporte(abrir, categoria="limpieza")] == ["B"]
    assert [f["nombre"] for f in _reporte(abrir, producto_id=a)] == ["A"]
    assert _reporte(abrir, producto_id=servicio) == []
    assert _por_nombre(_reporte(abrir))["A"]["variantes"] == 2 and _por_nombre(_reporte(abrir))["B"]["variantes"] == 0


@pytest.mark.parametrize("kw", [
    {"dias_rotacion": 0}, {"dias_rotacion": -3}, {"dias_rotacion": reposicion.MAX_DIAS_ROTACION + 1},
    {"dias_cobertura": 0}, {"dias_cobertura": reposicion.MAX_DIAS_COBERTURA + 1},
    {"plazo_entrega_dias": 0}, {"plazo_entrega_dias": reposicion.MAX_PLAZO_ENTREGA_DIAS + 1},
    {"dias_rotacion": 2.5}, {"dias_cobertura": "15"}, {"plazo_entrega_dias": True},
])
def test_un_parametro_fuera_de_rango_es_un_error(abrir_ventas, kw):
    with abrir_ventas() as conn, pytest.raises(ValueError):
        reposicion.sugerencia_reposicion(conn, **kw)


def test_los_parametros_por_defecto_son_los_del_adr():
    """ADR-017: 30 días de rotación, 15 de cobertura y 3 de plazo; topes de 365, 365 y 180."""
    defectos = {n: p.default for n, p in inspect.signature(reposicion.sugerencia_reposicion).parameters.items()}
    assert (defectos["dias_rotacion"], defectos["dias_cobertura"], defectos["plazo_entrega_dias"]) == (30, 15, 3)
    assert defectos["solo_a_pedir"] is True and defectos["sucursal_id"] is None
    assert (reposicion.MAX_DIAS_ROTACION, reposicion.MAX_DIAS_COBERTURA, reposicion.MAX_PLAZO_ENTREGA_DIAS) == (
        365, 365, 180)


def test_solo_lee(abrir_ventas):
    abrir = abrir_ventas
    _yerba_de_referencia(abrir)
    tablas = ("sales", "sale_items", "stock_movements", "catalog_items", "purchase_orders", "purchase_order_items")
    with abrir() as conn:
        antes = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tablas}
        reposicion.sugerencia_reposicion(conn, hoy=HOY, solo_a_pedir=False)
        assert {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tablas} == antes


# ── El contrato HTTP ─────────────────────────────────────────────────────


def _cliente(abrir) -> TestClient:
    app = FastAPI()
    app.include_router(build_reposicion_router(conexion=abrir))
    return TestClient(app)


def _yerba_de_hoy(abrir):
    """Como `_yerba_de_referencia`, pero contra la fecha real: el router usa el «hoy» del servidor."""
    hoy = datetime.date.today()
    with abrir() as conn:
        yerba = catalogo.create_producto(conn, "Yerba", precio_venta=100.0, precio_costo=60.0)
        stock.ajustar_stock(conn, yerba, 40.0, "inicial", fecha=(hoy - datetime.timedelta(days=60)).isoformat())
    _venta(abrir, [(yerba, "Yerba", 10, 100.0)], (hoy - datetime.timedelta(days=20)).isoformat())
    _venta(abrir, [(yerba, "Yerba", 20, 100.0)], (hoy - datetime.timedelta(days=10)).isoformat())
    return yerba


def test_get_devuelve_los_parametros_el_resumen_y_los_productos(abrir_ventas):
    abrir = abrir_ventas
    yerba = _yerba_de_hoy(abrir)
    r = _cliente(abrir).get("/api/reportes/reposicion")
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert (cuerpo["dias_rotacion"], cuerpo["dias_cobertura"], cuerpo["plazo_entrega_dias"], cuerpo["solo_a_pedir"]) == (
        30, 15, 3, True)
    assert cuerpo["resumen"] == {"productos": 1, "a_pedir": 1, "posible_quiebre": 0, "sin_ventas": 0}
    [p] = cuerpo["productos"]
    assert (p["producto_id"], p["stock"], p["rotacion_diaria"], p["cobertura_dias"], p["sugerido"]) == (
        yerba, 10.0, 1.0, 10.0, 8)

    # Con parámetros: en 5 días de cobertura y 1 de plazo (6) no falta nada; `solo_a_pedir=false` lo lista igual.
    r = _cliente(abrir).get("/api/reportes/reposicion",
                            params={"dias_cobertura": 5, "plazo_entrega_dias": 1, "solo_a_pedir": "false"})
    cuerpo = r.json()
    assert cuerpo["resumen"]["a_pedir"] == 0 and cuerpo["productos"][0]["sugerido"] == 0
    assert _cliente(abrir).get("/api/reportes/reposicion", params={"categoria": "Nada"}).json()["productos"] == []


def test_parametros_invalidos_son_422(abrir_ventas):
    c = _cliente(abrir_ventas)
    invalidos = [
        {"dias_rotacion": 0}, {"dias_rotacion": -1}, {"dias_rotacion": reposicion.MAX_DIAS_ROTACION + 1},
        {"dias_cobertura": 0}, {"dias_cobertura": "muchos"}, {"plazo_entrega_dias": 0},
        {"plazo_entrega_dias": reposicion.MAX_PLAZO_ENTREGA_DIAS + 1}, {"dias_rotacion": 2.5},
        {"solo_a_pedir": "quizás"}, {"sucursal_id": 999},
    ]
    for params in invalidos:
        assert c.get("/api/reportes/reposicion", params=params).status_code == 422, params
        assert c.get("/api/reportes/reposicion/export", params=params).status_code == 422, params
    # Los topes son válidos.
    assert c.get("/api/reportes/reposicion", params={"dias_rotacion": reposicion.MAX_DIAS_ROTACION}).status_code == 200


def test_export_csv(abrir_ventas):
    abrir = abrir_ventas
    _yerba_de_hoy(abrir)
    with abrir() as conn:
        quieto = _producto(conn, "Quieto", inicial=5.0, minimo=8.0)
    proveedor = _proveedor(abrir)
    _orden(abrir, proveedor, [(_id_de(abrir, "Yerba"), 5)])
    r = _cliente(abrir).get("/api/reportes/reposicion/export")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert f'filename="reposicion_{datetime.date.today().isoformat()}.csv"' in r.headers["content-disposition"]
    lineas = r.text.splitlines()
    assert lineas[0] == ("producto_id,codigo,nombre,categoria,unidad,stock,vencido,en_camino,en_camino_sin_sucursal,"
                         "stock_minimo,unidades_vendidas,dias_con_stock,rotacion_diaria,cobertura_dias,sugerido,"
                         "motivo,sin_ventas,posible_quiebre,variantes,plazo_entrega_dias,plazo_propio,stock_maximo,limitado_por_maximo,proveedor_id,proveedor,factor_estacional,stock_minimo_propio,por_vencer")
    # Yerba: hay 10, rota 1 por día y vienen 5 (sin sucursal): 18 − 10 − 5 = 3. Quieto: sin rotación, va al final y sólo lo
    # empuja el mínimo (8 − 5 = 3). Los booleanos, legibles, y el `None` de la cobertura, vacío.
    assert lineas[1] == f"{_id_de(abrir, 'Yerba')},,Yerba,,u,10.0,0.0,5.0,5.0,0.0,30.0,30,1.0,10.0,3,por_rotacion,no,no,0,3,no,,no,,,,no,0.0"
    assert lineas[2] == f"{quieto},,Quieto,,u,5.0,0.0,0.0,0.0,8.0,0.0,30,0.0,,3,bajo_minimo,si,no,0,3,no,,no,,,,no,0.0"
    assert len(lineas) == 3


def _id_de(abrir, nombre):
    with abrir() as conn:
        return conn.execute("SELECT id FROM catalog_items WHERE name=?", (nombre,)).fetchone()["id"]


def test_el_router_no_expone_nada_que_escriba(abrir_ventas):
    router = build_reposicion_router(conexion=abrir_ventas)
    assert {m for r in router.routes for m in r.methods} == {"GET"}


# ── Reposición v2: lo vencido no es stock ────────────────────────────────

# La base de `tests/test_vencimientos.py`: los dos schemas de un producto y la revisión 0002 aplicada.
destino = _vto.destino
abrir_vto_ventas = _vto.abrir_vto_ventas


def _lote(abrir, pid, lote, vence, cantidad, *, deposito=None):
    """Marca el producto como perecedero y le carga `cantidad` en un lote que vence el `vence`."""
    with abrir() as conn:
        deposito = deposito or catalogo.get_default_deposito_id(conn)
        vencimientos.marcar_vence(conn, pid, True)
        vencimientos.registrar_entrada_con_lote(conn, pid, deposito, lote, vence, cantidad,
                                                clave_operacion=f"t-{pid}-{lote}-{deposito}", fecha=PREVIA)
        conn.commit()


def test_lo_vencido_no_cuenta_como_stock(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)                       # stock 10, rota 1 por día: 18 − 10 = 8
    _lote(abrir, yerba, "V1", "2026-09-01", 6)                # +6 vencidos: stock 16, utilizable 10
    fila = _por_nombre(_reporte(abrir))["Yerba"]
    assert fila["stock"] == 16 and fila["vencido"] == 6
    assert fila["sugerido"] == 8                              # 18 − (16 − 6); sin descontar daría 2
    v1 = _por_nombre(_reporte(abrir, descontar_vencido=False))["Yerba"]
    assert v1["sugerido"] == 2 and v1["vencido"] == 0


def test_un_lote_que_vence_hoy_o_despues_todavia_es_stock(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    _lote(abrir, yerba, "H", HOY.isoformat(), 3)
    _lote(abrir, yerba, "F", "2026-12-01", 2)
    fila = _por_nombre(_reporte(abrir))["Yerba"]
    assert fila["vencido"] == 0 and fila["stock"] == 15 and fila["sugerido"] == 3


def test_un_producto_sin_marcar_no_cambia(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    _lote(abrir, yerba, "V1", "2026-09-01", 6)
    with abrir() as conn:                                      # lote vencido, pero el producto ya no está marcado
        vencimientos.marcar_vence(conn, yerba, False)
        conn.commit()
    fila = _por_nombre(_reporte(abrir))["Yerba"]
    assert fila["vencido"] == 0 and fila["sugerido"] == 2


def test_lo_vencido_mueve_la_cobertura(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    _lote(abrir, yerba, "V1", "2026-09-01", 10)               # stock real 20: 10 útil + 10 vencido
    con = _por_nombre(_reporte(abrir, solo_a_pedir=False))["Yerba"]
    sin = _por_nombre(_reporte(abrir, solo_a_pedir=False, descontar_vencido=False))["Yerba"]
    assert con["cobertura_dias"] == 10.0 and sin["cobertura_dias"] == 20.0


def test_con_todo_el_stock_vencido_la_cobertura_es_cero_y_se_pide(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    with abrir() as conn:
        yerba = _producto(conn, "Yerba")
    _lote(abrir, yerba, "V1", "2026-09-01", 50)               # 50 en el depósito, todo vencido
    _venta(abrir, [(yerba, "Yerba", 20, 100.0)], "2026-09-20", deposito_id=None)   # sale del lote (FEFO): quedan 30
    fila = _por_nombre(_reporte(abrir))["Yerba"]
    assert fila["vencido"] == 30 and fila["stock"] == 30
    assert fila["sugerido"] > 0 and fila["cobertura_dias"] == 0.0


def test_lo_vencido_se_descuenta_en_la_sucursal_que_lo_tiene(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    a, b, dep_a, dep_b = _dos_sucursales(abrir)
    with abrir() as conn:
        yerba = _producto(conn, "Yerba")
    _lote(abrir, yerba, "VA", "2026-09-01", 9, deposito=dep_a)     # vencido en Centro
    _lote(abrir, yerba, "OK", "2026-12-01", 9, deposito=dep_b)     # vigente en Norte
    en_a = _por_nombre(_reporte(abrir, sucursal_id=a, solo_a_pedir=False))["Yerba"]
    en_b = _por_nombre(_reporte(abrir, sucursal_id=b, solo_a_pedir=False))["Yerba"]
    assert (en_a["stock"], en_a["vencido"]) == (9, 9)
    assert (en_b["stock"], en_b["vencido"]) == (9, 0)
    assert _por_nombre(_reporte(abrir, solo_a_pedir=False))["Yerba"]["vencido"] == 9


def test_una_base_sin_la_revision_no_falla_y_no_descuenta(abrir_ventas):
    abrir = abrir_ventas                                       # sin la revisión 0002
    _yerba_de_referencia(abrir)
    fila = _por_nombre(_reporte(abrir))["Yerba"]
    assert fila["vencido"] == 0 and fila["sugerido"] == 8


def test_el_router_acepta_y_devuelve_descontar_vencido(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    _yerba_de_hoy(abrir)
    c = _cliente(abrir)
    assert c.get("/api/reportes/reposicion").json()["descontar_vencido"] is True
    cuerpo = c.get("/api/reportes/reposicion", params={"descontar_vencido": "false"}).json()
    assert cuerpo["descontar_vencido"] is False and "vencido" in cuerpo["productos"][0]
    assert c.get("/api/reportes/reposicion/export", params={"descontar_vencido": "false"}).status_code == 200


def test_lo_vencido_se_consulta_solo_de_los_productos_marcados_y_los_depositos_que_se_miran(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    a, b, dep_a, dep_b = _dos_sucursales(abrir)
    with abrir() as conn:
        yerba = _producto(conn, "Yerba")
        otro = _producto(conn, "Otro")
    _lote(abrir, yerba, "VA", "2026-09-01", 9, deposito=dep_a)
    _lote(abrir, otro, "VO", "2026-09-01", 4, deposito=dep_b)
    with abrir() as conn:
        antes = reposicion.saldos_por_bucket
        pedidos = []

        def espia(c, donde, params, depositos=None):
            pedidos.append((donde, list(params)))
            return antes(c, donde, params, depositos)

        reposicion.saldos_por_bucket = espia
        try:
            fila = _por_nombre(reposicion.sugerencia_reposicion(conn, hoy=HOY, producto_id=yerba, sucursal_id=a,
                                                                solo_a_pedir=False))["Yerba"]
        finally:
            reposicion.saldos_por_bucket = antes
    assert fila["vencido"] == 9
    [(donde, params)] = pedidos
    assert "sm.item_id IN" in donde and "sm.location_id IN" in donde
    assert params == [yerba, dep_a]


# ── Reposición v2: plazo y techo propios del producto (ADR-020) ───────────


def _fijar(abrir, pid, plazo=None, techo=None):
    with abrir() as conn:
        r = reposicion.fijar_parametros(conn, pid, plazo_entrega_dias=plazo, stock_maximo=techo)
        conn.commit()
    return r


def test_el_plazo_propio_reemplaza_al_general_solo_en_ese_producto(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)                       # rota 1 por día, stock 10; plazo general 3: 18 − 10 = 8
    with abrir() as conn:
        otra = _producto(conn, "Otra", inicial=40.0)
    _venta(abrir, [(otra, "Otra", 30, 100.0)], "2026-09-10")  # rota 1 por día, le quedan 10: también 8 con el general
    _fijar(abrir, yerba, plazo=13)                            # 15 + 13 = 28 − 10 = 18
    filas = _por_nombre(_reporte(abrir))
    assert filas["Yerba"]["sugerido"] == 18 and filas["Yerba"]["plazo_entrega_dias"] == 13 and filas["Yerba"]["plazo_propio"]
    assert filas["Otra"]["sugerido"] == 8 and filas["Otra"]["plazo_entrega_dias"] == 3 and not filas["Otra"]["plazo_propio"]


def test_el_techo_recorta_la_sugerencia_y_lo_dice(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)                       # sugeriría 8 con stock 10
    _fijar(abrir, yerba, techo=14)                            # cabe 14 − 10 = 4
    fila = _por_nombre(_reporte(abrir))["Yerba"]
    assert fila["sugerido"] == 4 and fila["limitado_por_maximo"] and fila["stock_maximo"] == 14
    _fijar(abrir, yerba, techo=100)                           # el techo no molesta: 8
    fila = _por_nombre(_reporte(abrir))["Yerba"]
    assert fila["sugerido"] == 8 and not fila["limitado_por_maximo"]


def test_el_techo_cuenta_lo_que_ya_viene_en_camino(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    _orden(abrir, _proveedor(abrir), [(yerba, 3)])            # 10 + 3 en camino: sugeriría 5
    _fijar(abrir, yerba, techo=15)                            # 15 − 13 = 2
    assert _por_nombre(_reporte(abrir))["Yerba"]["sugerido"] == 2


def test_con_el_stock_en_el_techo_no_se_sugiere_nada(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    _fijar(abrir, yerba, techo=10)                            # ya está en el techo
    assert "Yerba" not in _por_nombre(_reporte(abrir))
    fila = _por_nombre(_reporte(abrir, solo_a_pedir=False))["Yerba"]
    assert fila["sugerido"] == 0 and fila["motivo"] is None and fila["limitado_por_maximo"]


def test_el_techo_se_redondea_hacia_abajo_a_la_unidad(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    _fijar(abrir, yerba, techo=14.9)                          # entera: caben 4,9 → 4
    assert _por_nombre(_reporte(abrir))["Yerba"]["sugerido"] == 4


def test_el_techo_manda_sobre_el_piso_del_minimo(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    with abrir() as conn:
        sal = _producto(conn, "Sal", inicial=2.0, minimo=10.0)    # piso: pide 8
    _fijar(abrir, sal, techo=10)
    assert _por_nombre(_reporte(abrir))["Sal"]["sugerido"] == 8   # 10 − 2: justo el techo
    with abrir() as conn:                                          # un mínimo mayor que el techo ya cargado: gana el techo
        conn.execute("UPDATE catalog_items SET min_stock = 50 WHERE id = ?", (sal,))
        conn.commit()
    assert _por_nombre(_reporte(abrir))["Sal"]["sugerido"] == 8


def test_borrar_los_parametros_vuelve_a_la_cuenta_general(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    _fijar(abrir, yerba, plazo=13, techo=14)
    r = _fijar(abrir, yerba)
    assert r["plazo_entrega_dias"] is None and r["stock_maximo"] is None
    assert _por_nombre(_reporte(abrir))["Yerba"]["sugerido"] == 8


@pytest.mark.parametrize("plazo, techo", [(0, None), (181, None), (True, None), (3.5, None), (None, 0), (None, -1),
                                          (None, "x"), (None, float("nan")), (None, True)])
def test_los_valores_invalidos_se_rechazan_antes_de_escribir(abrir_vto_ventas, plazo, techo):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    with abrir() as conn, pytest.raises(ValueError):
        reposicion.fijar_parametros(conn, yerba, plazo_entrega_dias=plazo, stock_maximo=techo)
    with abrir() as conn:
        assert reposicion.parametros_de(conn, yerba)["plazo_entrega_dias"] is None


def test_el_techo_no_puede_ser_menor_que_el_minimo(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    with abrir() as conn:
        sal = _producto(conn, "Sal", minimo=10.0)
    with abrir() as conn, pytest.raises(ValueError, match="menor que el stock mínimo"):
        reposicion.fijar_parametros(conn, sal, plazo_entrega_dias=None, stock_maximo=5)
    _fijar(abrir, sal, techo=10)                              # igual al mínimo: válido


def test_un_producto_que_no_existe_es_un_error(abrir_vto_ventas):
    with abrir_vto_ventas() as conn, pytest.raises(reposicion.ProductoNoEncontrado):
        reposicion.parametros_de(conn, 9999)


def test_una_base_sin_la_revision_no_puede_leer_ni_escribir_parametros(abrir_ventas):
    with abrir_ventas() as conn:
        assert not reposicion.tiene_parametros(conn)
        with pytest.raises(reposicion.SinRevision):
            reposicion.parametros_de(conn, 1)
        with pytest.raises(reposicion.SinRevision):
            reposicion.fijar_parametros(conn, 1, plazo_entrega_dias=3, stock_maximo=None)


def _cliente_parametros(abrir, *, leer=None) -> TestClient:
    from fastapi import Depends

    from libracommerce.web.reposicion_router import build_reposicion_parametros_router

    def _permitir():
        return None

    app = FastAPI()
    app.include_router(build_reposicion_parametros_router(
        conexion=abrir, dependencias_escribir=[Depends(_permitir)], dependencias_leer=leer))
    return TestClient(app)


def test_el_router_de_parametros_lee_y_escribe(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    c = _cliente_parametros(abrir)
    assert c.get(f"/api/productos/{yerba}/reposicion").json()["stock_maximo"] is None
    r = c.put(f"/api/productos/{yerba}/reposicion", json={"plazo_entrega_dias": 7, "stock_maximo": 40})
    assert r.status_code == 200 and r.json()["plazo_entrega_dias"] == 7 and r.json()["stock_maximo"] == 40
    assert c.get(f"/api/productos/{yerba}/reposicion").json()["plazo_entrega_dias"] == 7
    assert c.put(f"/api/productos/{yerba}/reposicion", json={"plazo_entrega_dias": 0, "stock_maximo": None}).status_code == 422
    assert c.put(f"/api/productos/{yerba}/reposicion", json={"plazo_entrega_dias": 7}).status_code == 422   # falta uno
    assert c.put(f"/api/productos/{yerba}/reposicion", json={"plazo_entrega_dias": 7, "stock_maximo": 1, "x": 1}).status_code == 422
    assert c.put("/api/productos/9999/reposicion", json={"plazo_entrega_dias": None, "stock_maximo": None}).status_code == 404
    assert c.get(f"/api/productos/{yerba}/reposicion").json()["plazo_entrega_dias"] == 7   # lo inválido no escribió nada


def test_el_router_de_parametros_sin_la_revision_responde_503(abrir_ventas):
    c = _cliente_parametros(abrir_ventas)
    assert c.get("/api/productos/1/reposicion").status_code == 503


def test_el_router_de_parametros_no_se_monta_sin_autorizacion_para_escribir(abrir_vto_ventas):
    from libracommerce.web.reposicion_router import build_reposicion_parametros_router

    for vacio in (None, [], ()):
        with pytest.raises(ValueError, match="dependencias_escribir"):
            build_reposicion_parametros_router(conexion=abrir_vto_ventas, dependencias_escribir=vacio)


def test_el_techo_se_informa_tal_como_se_guardo_sin_redondearlo_a_la_escala_de_informe(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    _fijar(abrir, yerba, techo=14.9999)                       # entera: caben 4,9999 → 4; el techo se informa entero, no como 15.0
    fila = _por_nombre(_reporte(abrir))["Yerba"]
    assert fila["stock_maximo"] == 14.9999 and fila["sugerido"] == 4


def test_editar_el_producto_no_puede_subir_el_minimo_por_encima_del_techo(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    with abrir() as conn:
        sal = _producto(conn, "Sal", minimo=5.0)

    def editar(minimo):
        with abrir() as conn:
            catalogo.update_producto(conn, sal, "Sal", "", "", 100.0, 60.0, "u", "", 1, stock_minimo=minimo)
            conn.commit()

    _fijar(abrir, sal, techo=10)
    editar(10.0)                                              # igual al techo: válido
    with pytest.raises(ValueError, match="no puede ser mayor que el stock máximo"):
        editar(11.0)
    with abrir() as conn:
        assert float(conn.execute("SELECT min_stock FROM catalog_items WHERE id=?", (sal,)).fetchone()["min_stock"]) == 10.0
    _fijar(abrir, sal)                                        # sin techo, el mínimo es libre
    editar(50.0)


def test_editar_un_producto_sin_techo_ni_revision_no_cambia(abrir_ventas):
    abrir = abrir_ventas                                       # sin la revisión 0003
    with abrir() as conn:
        sal = _producto(conn, "Sal", minimo=5.0)
        catalogo.update_producto(conn, sal, "Sal", "", "", 100.0, 60.0, "u", "", 1, stock_minimo=99.0)
        conn.commit()


def test_un_producto_con_el_minimo_ya_por_encima_del_techo_puede_seguir_editandose(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    with abrir() as conn:
        sal = _producto(conn, "Sal", minimo=5.0)
    _fijar(abrir, sal, techo=10)
    with abrir() as conn:                                      # el dato de antes de la guarda: mínimo 50, techo 10
        conn.execute("UPDATE catalog_items SET min_stock = 50 WHERE id = ?", (sal,))
        conn.commit()

    def editar(minimo, precio=100.0):
        with abrir() as conn:
            catalogo.update_producto(conn, sal, "Sal", "", "", precio, 60.0, "u", "", 1, stock_minimo=minimo)
            conn.commit()

    editar(50.0, precio=120.0)                                 # un cambio ajeno al mínimo (o el reenvío de la actualización masiva)
    editar(30.0)                                               # bajarlo hacia el techo
    with pytest.raises(ValueError, match="no puede ser mayor"):
        editar(40.0)                                           # subirlo, no
