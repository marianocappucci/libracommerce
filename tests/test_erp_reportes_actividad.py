"""Los reportes y la línea de tiempo sobre las ventas del motor (P9-M4), contra
los dos motores de base: lo que Contalibra y Restolibra tenían copiado en
`db_reportes.py` y `db_logs.py`."""

from __future__ import annotations

import datetime

from conftest import USUARIO

from libracommerce.erp import actividad, catalogo, reportes, stock, ventas

HOY = datetime.date.today().isoformat()
AYER = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()


def _producto(conn, nombre, precio, existencia, minimo=0.0):
    pid = catalogo.create_producto(conn, nombre, precio_venta=precio, precio_costo=precio / 2,
                                   stock_minimo=minimo)
    if existencia:
        stock.ajustar_stock(conn, pid, existencia, "inicial", usuario_id=USUARIO["id"], fecha=HOY)
    return pid


def _venta(abrir, items, pagos, fecha=HOY):
    total = round(sum(i["subtotal"] for i in items), 2)
    return ventas.crear_venta_directa(
        abrir, fecha=fecha, items=items, subtotal=total, descuento=0.0, total=total,
        cliente_id=None, cliente_nombre="", usuario_id=USUARIO["id"], observaciones="",
        estado=ventas.estado_segun_pagos(total, pagos), pagos=pagos, stock_habilitado=True,
    )


def _escenario(abrir):
    """Dos ventas de hoy (yerba x3 en efectivo, azúcar x1 + yerba x1 con
    tarjeta) y una de ayer; el azúcar queda bajo el mínimo."""
    with abrir() as conn:
        yerba = _producto(conn, "Yerba", 100.0, 10.0)
        azucar = _producto(conn, "Azúcar", 50.0, 2.0, minimo=5.0)
    v1 = _venta(abrir, [{"nombre": "Yerba", "qty": 3, "precio": 100.0, "subtotal": 300.0, "producto_id": yerba}],
                [{"medio": "efectivo", "monto": 300.0, "estado": "aprobado"}])
    v2 = _venta(abrir, [{"nombre": "Azúcar", "qty": 1, "precio": 50.0, "subtotal": 50.0, "producto_id": azucar},
                        {"nombre": "Yerba", "qty": 1, "precio": 100.0, "subtotal": 100.0, "producto_id": yerba}],
                [{"medio": "tarjeta", "monto": 150.0, "estado": "aprobado"}])
    v3 = _venta(abrir, [{"nombre": "Yerba", "qty": 1, "precio": 100.0, "subtotal": 100.0, "producto_id": yerba}],
                [{"medio": "efectivo", "monto": 100.0, "estado": "aprobado"}], fecha=AYER)
    return yerba, azucar, (v1, v2, v3)


# ── Reportes ─────────────────────────────────────────────────────────────


def test_reportes_sobre_las_ventas_del_motor(abrir_ventas):
    yerba, azucar, _ = _escenario(abrir_ventas)
    with abrir_ventas() as conn:
        por_dia = reportes.reporte_ventas(conn, HOY, HOY)
        assert [(r["periodo"], r["cantidad"], float(r["total"])) for r in por_dia] == [(HOY, 2, 450.0)]
        # Sin rango entran las tres; por mes las agrupa en uno o dos períodos.
        assert sum(r["cantidad"] for r in reportes.reporte_ventas(conn)) == 3
        assert sum(r["cantidad"] for r in reportes.reporte_ventas(conn, agrupacion="mes")) == 3

        medios = {r["medio"]: (r["operaciones"], float(r["total"])) for r in reportes.reporte_medios_pago(conn, HOY, HOY)}
        assert medios == {"efectivo": (1, 300.0), "tarjeta": (1, 150.0)}

        top = reportes.reporte_productos_top(conn, HOY, HOY)
        assert [(r["nombre"], float(r["cantidad"]), float(r["total"])) for r in top] == [("Yerba", 4.0, 400.0), ("Azúcar", 1.0, 50.0)]
        assert len(reportes.reporte_productos_top(conn, limit=1)) == 1

        bajo = reportes.reporte_stock_bajo(conn)
        assert [(r["id"], r["nombre"], float(r["stock_minimo"]), float(r["stock_actual"])) for r in bajo] == [(azucar, "Azúcar", 5.0, 1.0)]

        resumen = reportes.reporte_resumen(conn, HOY, HOY)
        assert resumen["ventas_cantidad"] == 2 and float(resumen["ventas_total"]) == 450.0
        assert resumen["facturas_cantidad"] == 0
        # El saldo de caja son los dos ingresos de hoy: la venta de ayer no entra.
        assert float(resumen["caja_saldo"]) == 450.0


def test_puerto_de_reportes_abre_la_conexion_del_producto(abrir_ventas):
    from libracore.reportes import PuertoDeReportes

    _escenario(abrir_ventas)
    puerto = reportes.puerto_de_reportes(abrir_ventas)
    assert isinstance(puerto, PuertoDeReportes)
    assert puerto.resumen(HOY, HOY)["ventas_cantidad"] == 2
    assert [r["nombre"] for r in puerto.productos_top(HOY, HOY, limit=1)] == ["Yerba"]
    assert puerto.ventas(HOY, HOY, "dia")[0]["cantidad"] == 2
    assert len(puerto.medios_pago(HOY, HOY)) == 2
    assert [r["nombre"] for r in puerto.stock_bajo()] == ["Azúcar"]


def test_las_ventas_anuladas_y_pendientes_no_cuentan_si_el_producto_lo_pide(abrir_ventas):
    """Sin `solo_confirmadas` (Contalibra, Restolibra) los reportes cuentan todo, como siempre; con él (VentaLibra, que anula y
    cobra por QR) una venta anulada o pendiente de cobro no es una venta."""
    yerba, _azucar, (v1, v2, _v3) = _escenario(abrir_ventas)
    pendiente = _venta(abrir_ventas, [{"nombre": "Yerba", "qty": 2, "precio": 100.0, "subtotal": 200.0, "producto_id": yerba}],
                       [{"medio": "mercadopago", "monto": 200.0, "estado": "pendiente"}])
    with abrir_ventas() as conn:
        ventas.anular_venta(conn, v2["id"] if isinstance(v2, dict) else v2)
        # Sin la opción: las tres de hoy (una anulada, una pendiente) cuentan.
        assert reportes.reporte_resumen(conn, HOY, HOY)["ventas_cantidad"] == 3
        assert sum(r["cantidad"] for r in reportes.reporte_ventas(conn, HOY, HOY)) == 3
        # Con la opción: sólo la confirmada.
        resumen = reportes.reporte_resumen(conn, HOY, HOY, solo_confirmadas=True)
        assert resumen["ventas_cantidad"] == 1 and float(resumen["ventas_total"]) == 300.0
        assert [(r["cantidad"], float(r["total"])) for r in reportes.reporte_ventas(conn, HOY, HOY, solo_confirmadas=True)] == [(1, 300.0)]
        assert {r["medio"] for r in reportes.reporte_medios_pago(conn, HOY, HOY, solo_confirmadas=True)} == {"efectivo"}
        top = reportes.reporte_productos_top(conn, HOY, HOY, solo_confirmadas=True)
        assert [(r["nombre"], float(r["cantidad"])) for r in top] == [("Yerba", 3.0)]
    puerto = reportes.puerto_de_reportes(abrir_ventas, solo_confirmadas=True)
    assert puerto.resumen(HOY, HOY)["ventas_cantidad"] == 1 and puerto.ventas(HOY, HOY, "dia")[0]["cantidad"] == 1
    assert pendiente  # la venta pendiente existe: sólo que no cuenta


def test_el_saldo_de_caja_del_resumen_no_cuenta_lo_anulado_ni_el_fiado_si_se_pide(abrir_ventas):
    yerba, _azucar, _ = _escenario(abrir_ventas)  # 450 de ingresos hoy
    _venta(abrir_ventas, [{"nombre": "Yerba", "qty": 5, "precio": 100.0, "subtotal": 500.0, "producto_id": yerba}],
           [{"medio": "cuenta_corriente", "monto": 500.0, "estado": "aprobado"}])
    with abrir_ventas() as conn:
        con_fiado = float(reportes.reporte_resumen(conn, HOY, HOY)["caja_saldo"])
        sin_fiado = float(reportes.reporte_resumen(conn, HOY, HOY, sin_fiado=True)["caja_saldo"])
        assert (con_fiado, sin_fiado) == (950.0, 450.0)
        # Un movimiento anulado no cuenta en ningún caso (antes este resumen lo sumaba).
        conn.execute("UPDATE caja_movimientos SET anulado = 1 WHERE medio_pago = 'efectivo' AND monto = 300")
        assert float(reportes.reporte_resumen(conn, HOY, HOY, sin_fiado=True)["caja_saldo"]) == 150.0
    assert float(reportes.puerto_de_reportes(abrir_ventas, sin_fiado=True).resumen(HOY, HOY)["caja_saldo"]) == 150.0


# ── Actividad ────────────────────────────────────────────────────────────


def test_actividad_mezcla_las_partes_de_los_dos_motores(abrir_ventas):
    yerba, azucar, (v1, v2, v3) = _escenario(abrir_ventas)
    with abrir_ventas() as conn:
        filas = actividad.get_actividad_log(conn)
        tipos = {f["tipo"] for f in filas}
        # Ventas y stock de este motor; caja de LibraCore. Todo en una lista.
        assert tipos == {"venta", "stock", "caja"}
        assert actividad.get_actividad_count(conn) == len(filas)

        ventas_ = [f for f in filas if f["tipo"] == "venta"]
        assert {f["ref_id"] for f in ventas_} == {v1, v2, v3}
        assert all(f["ref_tabla"] == "ventas" and f["usuario"] == "Cajero" for f in ventas_)
        assert any(f["descripcion"] == "Venta V-00001 (cobrada)" and float(f["monto"]) == 300.0 for f in ventas_)

        stock_ = [f for f in filas if f["tipo"] == "stock"]
        # Dos ajustes iniciales y cuatro descuentos por venta.
        assert len(stock_) == 6 and all(f["ref_tabla"] == "movimientos_stock" for f in stock_)
        # El texto de la cantidad lo arma la base: SQLite escribe `-3.0` y
        # PostgreSQL `-3` (el `real` se imprime sin decimales enteros).
        assert any(f["descripcion"].startswith("venta Yerba (-3") and " u)" in f["descripcion"] for f in stock_)

        # Los filtros son los de LibraCore, sobre las partes de acá.
        assert {f["tipo"] for f in actividad.get_actividad_log(conn, tipos=["venta"])} == {"venta"}
        assert actividad.get_actividad_count(conn, tipos=["venta"], desde=HOY, hasta=HOY) == 2
        assert actividad.get_actividad_count(conn, usuario_id=USUARIO["id"], tipos=["venta"]) == 3
        assert actividad.get_actividad_count(conn, usuario_id=999) == 0
        assert len(actividad.get_actividad_log(conn, limit=2)) == 2


def test_las_partes_del_producto_son_las_dos_propias_mas_el_core():
    from libracore.db import logs

    partes = actividad.partes_de_comercio()
    # Ventas y stock de este motor, en su lugar de siempre; todo lo demás es
    # exactamente `logs.PARTES_CORE` del libracore instalado — sin un número
    # mágico, para que una parte nueva del core (como los cierres diarios)
    # entre sola sin romper este test.
    assert partes[0] is actividad.PARTE_VENTAS_COMERCIO and partes[2] is actividad.PARTE_STOCK_COMERCIO
    assert set(partes) - {actividad.PARTE_VENTAS_COMERCIO, actividad.PARTE_STOCK_COMERCIO} == set(logs.PARTES_CORE)
    assert len(partes) == len(logs.PARTES_CORE) + 2
    assert logs.PARTE_VENTAS not in partes and logs.PARTE_STOCK not in partes


def test_el_cierre_diario_entra_a_la_actividad(abrir_ventas):
    """Desde LibraCore v1.98.0 `logs.PARTES_CORE` suma `PARTE_CIERRES_DIARIOS`
    (mismo criterio que la caja o los turnos): `partes_de_comercio()` la hereda
    sola, sin nombrarla, y por eso tiene que aparecer en la línea de tiempo de
    un producto de este motor."""
    from libracore.db import cierre_diario as db_cierre_diario

    _escenario(abrir_ventas)
    with abrir_ventas() as conn:
        db_cierre_diario.crear_tablas(conn)
        conn.execute(
            "INSERT INTO cierres_diarios "
            "(sucursal_id, numero, fecha, usuario_id, monto_esperado_total, "
            " monto_declarado_total, diferencia_total) "
            "VALUES (NULL, 1, ?, ?, 1000, 900, -100)",
            (HOY, USUARIO["id"]),
        )
        conn.commit()

    with abrir_ventas() as conn:
        filas = actividad.get_actividad_log(conn, tipos=["cierre_diario"])
        assert len(filas) == 1
        fila = filas[0]
        assert fila["ref_tabla"] == "cierres_diarios"
        assert "Cierre diario #1" in fila["descripcion"]
        assert fila["usuario"] == "Cajero"

        assert "cierre_diario" in {f["tipo"] for f in actividad.get_actividad_log(conn)}
