"""Anular una venta cuya factura tiene CAE de ARCA (ADR-032).

🔴 Anular **no llega a ARCA**: con CAE y sin nota de crédito la factura sigue vigente allá mientras el stock vuelve y la
caja se revierte. La anulación exige la nota **antes**; con la nota emitida anula, y **no acredita otra vez** la cuenta
corriente si la nota ya lo hizo (con las dos, el saldo del cliente quedaba en −total).

La nota la emite el motor (`libracore.notas_de_credito`, `POST /api/facturas/{id}/nota-credito`); acá se la siembra
como queda en la base: una fila de `facturas` de tipo nota que apunta a la factura (`cbte_asoc_*`), y el abono con su
marca en `cc_pagos.referencia`.
"""

from __future__ import annotations

import datetime

import pytest
from conftest import USUARIO

from libracommerce.erp import stock, ventas

HOY = datetime.date.today().isoformat()
CAE = "75123456789012"


def _venta(abrir, *, pagos, cliente_id=None, producto_id=None):
    items = [{"nombre": "Yerba", "qty": 2, "precio": 100.0, "subtotal": 200.0, "producto_id": producto_id}]
    total = 200.0
    return ventas.crear_venta_directa(
        abrir, fecha=HOY, items=items, subtotal=total, descuento=0.0, total=total, cliente_id=cliente_id,
        cliente_nombre="Cliente" if cliente_id else "", usuario_id=USUARIO["id"], observaciones="",
        estado=ventas.estado_segun_pagos(total, pagos), pagos=pagos, stock_habilitado=producto_id is not None)


def _factura(conn, vid, *, cae=CAE, tipo=11, numero=11):
    conn.execute(
        "INSERT INTO facturas (tipo, punto_venta, numero, fecha, cliente_cuit, cliente_razon, cliente_iva_cond, "
        "items, subtotal, iva_amount, total, ambiente, cae) VALUES (?, 5, ?, ?, '', 'CF', 5, '[]', 200, 0, 200, "
        "'homologacion', ?)", (tipo, numero, HOY, cae))
    fid = conn.execute("SELECT MAX(id) FROM facturas").fetchone()[0]
    ventas.vincular_factura(conn, vid, fid)
    conn.commit()
    return fid


def _nota(conn, *, tipo_original=11, numero_original=11, cae=CAE, tipo_nota=13, numero=1):
    conn.execute(
        "INSERT INTO facturas (tipo, punto_venta, numero, fecha, cliente_cuit, cliente_razon, cliente_iva_cond, "
        "items, subtotal, iva_amount, total, ambiente, cae, cbte_asoc_tipo, cbte_asoc_pv, cbte_asoc_nro) "
        "VALUES (?, 5, ?, ?, '', 'CF', 5, '[]', 200, 0, 200, 'homologacion', ?, ?, 5, ?)",
        (tipo_nota, numero, HOY, cae, tipo_original, numero_original))
    conn.commit()


def _estado(conn, vid):
    return {
        "venta": ventas.obtener_venta(conn, vid)["status"],
        "caja": conn.execute("SELECT COUNT(*) FROM caja_movimientos").fetchone()[0],
        "cc": conn.execute("SELECT COUNT(*) FROM cc_pagos").fetchone()[0],
        "stock_mov": conn.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0],
    }


# ── Sin nota: no se anula, y no queda nada a medias ────────────────────────

def test_una_venta_con_factura_con_cae_y_sin_nota_no_se_anula(abrir_ventas):
    vid = _venta(abrir_ventas, pagos=[{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}])
    with abrir_ventas() as conn:
        fid = _factura(conn, vid)
        antes = _estado(conn, vid)
        with pytest.raises(ventas.VentaConFacturaCAE) as e:
            ventas.anular_venta(conn, vid, usuario_id=USUARIO["id"])
        assert e.value.factura_id == fid
        assert f"#{fid}" in str(e.value) and "nota de crédito" in str(e.value)
        conn.rollback()
        assert _estado(conn, vid) == antes, "ni stock, ni caja, ni cuenta corriente, ni estado"
        assert antes["venta"] != "cancelled"


def test_una_nota_sin_cae_no_alcanza(abrir_ventas):
    """Una nota que ARCA no autorizó no revierte la factura ante ARCA."""
    vid = _venta(abrir_ventas, pagos=[{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}])
    with abrir_ventas() as conn:
        _factura(conn, vid)
        for cae in ("", "PENDIENTE", None):
            conn.execute("DELETE FROM facturas WHERE cbte_asoc_nro=11")
            _nota(conn, cae=cae, numero=2)
            with pytest.raises(ventas.VentaConFacturaCAE):
                ventas.anular_venta(conn, vid)
            conn.rollback()


def test_la_nota_de_otra_factura_no_cuenta(abrir_ventas):
    """La nota tiene que apuntar a ESTA factura: tipo, punto de venta y número."""
    vid = _venta(abrir_ventas, pagos=[{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}])
    with abrir_ventas() as conn:
        _factura(conn, vid)
        _nota(conn, numero_original=99)  # de otra factura
        _nota(conn, tipo_original=1, numero=2)  # mismo número, otro tipo
        with pytest.raises(ventas.VentaConFacturaCAE):
            ventas.anular_venta(conn, vid)


# ── Con nota: se anula ─────────────────────────────────────────────────────

def test_con_la_nota_emitida_se_anula_y_repone_stock_y_caja(abrir_ventas):
    with abrir_ventas() as conn:
        from libracommerce.erp import catalogo
        pid = catalogo.create_producto(conn, "Yerba", precio_venta=100.0, precio_costo=60.0)
        stock.ajustar_stock(conn, pid, 10, "inicial", usuario_id=USUARIO["id"], fecha=HOY)
    vid = _venta(abrir_ventas, producto_id=pid,
                 pagos=[{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}])
    with abrir_ventas() as conn:
        _factura(conn, vid)
        _nota(conn)
        assert stock.get_stock_actual(conn, pid) == 8.0
        assert ventas.anular_venta(conn, vid, usuario_id=USUARIO["id"]) is True
        conn.commit()
        assert stock.get_stock_actual(conn, pid) == 10.0
        assert ventas.obtener_venta(conn, vid)["status"] == "cancelled"
        egresos = conn.execute("SELECT monto FROM caja_movimientos WHERE tipo='egreso'").fetchall()
        assert [float(m["monto"]) for m in egresos] == [200.0], "la caja se revierte igual: la nota no la toca"


def _cliente(conn):
    conn.execute("INSERT INTO clients (id, name, cuit_dni) VALUES (5, 'Cliente', '20111111112')")
    conn.execute("INSERT INTO parties (id, party_type, display_name) VALUES (5, 'customer', 'Cliente')")
    conn.commit()
    return 5


def test_si_la_nota_ya_abono_la_cuenta_corriente_la_anulacion_no_acredita_otra_vez(abrir_ventas):
    """🔴 El caso que dejaba el saldo del cliente en −total."""
    from libracore.db.cuenta_corriente import create_cc_pago
    from libracore.notas_de_credito import referencia_cc_de_nota

    with abrir_ventas() as conn:
        cid = _cliente(conn)
    vid = _venta(abrir_ventas, cliente_id=cid,
                 pagos=[{"medio": "cuenta_corriente", "monto": 200.0, "estado": "aprobado"}])
    with abrir_ventas() as conn:
        fid = _factura(conn, vid)
        _nota(conn)
        # El abono tal como lo deja `POST /api/facturas/{id}/nota-credito`.
        create_cc_pago(cliente_id=cid, monto=200.0, fecha=HOY, concepto="NC", referencia=referencia_cc_de_nota(fid),
                       medio_pago="Cuenta Corriente", caja_id=None, usuario_id=USUARIO["id"], conn=conn)
        conn.commit()
        assert ventas.anular_venta(conn, vid, usuario_id=USUARIO["id"]) is True
        conn.commit()
        abonos = conn.execute("SELECT monto FROM cc_pagos WHERE cliente_id=?", (cid,)).fetchall()
        assert [float(a["monto"]) for a in abonos] == [200.0], "UN solo abono: el de la nota"


def test_si_la_nota_no_abono_la_cuenta_corriente_la_anulacion_si_acredita(abrir_ventas):
    """El control del de arriba: sin la marca de la nota, la anulación acredita como siempre."""
    with abrir_ventas() as conn:
        cid = _cliente(conn)
    vid = _venta(abrir_ventas, cliente_id=cid,
                 pagos=[{"medio": "cuenta_corriente", "monto": 200.0, "estado": "aprobado"}])
    with abrir_ventas() as conn:
        _factura(conn, vid)
        _nota(conn)
        ventas.anular_venta(conn, vid, usuario_id=USUARIO["id"])
        conn.commit()
        abonos = conn.execute("SELECT monto FROM cc_pagos WHERE cliente_id=?", (cid,)).fetchall()
        assert [float(a["monto"]) for a in abonos] == [200.0]


def test_el_abono_de_otra_factura_no_cuenta(abrir_ventas):
    from libracore.db.cuenta_corriente import create_cc_pago
    from libracore.notas_de_credito import referencia_cc_de_nota

    with abrir_ventas() as conn:
        cid = _cliente(conn)
    vid = _venta(abrir_ventas, cliente_id=cid,
                 pagos=[{"medio": "cuenta_corriente", "monto": 200.0, "estado": "aprobado"}])
    with abrir_ventas() as conn:
        fid = _factura(conn, vid)
        _nota(conn)
        create_cc_pago(cliente_id=cid, monto=200.0, fecha=HOY, concepto="NC de otra", usuario_id=USUARIO["id"],
                       referencia=referencia_cc_de_nota(fid + 1000), medio_pago="Cuenta Corriente", caja_id=None,
                       conn=conn)
        conn.commit()
        ventas.anular_venta(conn, vid, usuario_id=USUARIO["id"])
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM cc_pagos WHERE cliente_id=?", (cid,)).fetchone()[0] == 2


# ── Lo que no cambia ───────────────────────────────────────────────────────

@pytest.mark.parametrize("cae", ["", None, "PENDIENTE"])
def test_una_factura_sin_cae_se_anula_como_siempre(abrir_ventas, cae):
    """Sin ARCA, o con una factura que ARCA no autorizó: no hay nada vigente allá que dejar."""
    vid = _venta(abrir_ventas, pagos=[{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}])
    with abrir_ventas() as conn:
        _factura(conn, vid, cae=cae)
        assert ventas.anular_venta(conn, vid) is True


def test_una_venta_sin_factura_se_anula_como_siempre(abrir_ventas):
    vid = _venta(abrir_ventas, pagos=[{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}])
    with abrir_ventas() as conn:
        assert ventas.anular_venta(conn, vid) is True


def test_anular_dos_veces_sigue_siendo_un_no_op_aun_con_factura_con_cae(abrir_ventas):
    vid = _venta(abrir_ventas, pagos=[{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}])
    with abrir_ventas() as conn:
        _factura(conn, vid)
        _nota(conn)
        assert ventas.anular_venta(conn, vid) is True
        assert ventas.anular_venta(conn, vid) is False


def test_el_detalle_de_la_venta_trae_el_cae_para_que_la_pantalla_avise(abrir_ventas):
    vid = _venta(abrir_ventas, pagos=[{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}])
    sin = _venta(abrir_ventas, pagos=[{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}])
    with abrir_ventas() as conn:
        _factura(conn, vid)
        assert ventas.obtener_venta(conn, vid)["factura_cae"] == CAE
        assert ventas.obtener_venta(conn, sin)["factura_cae"] is None
        _factura(conn, sin, cae="PENDIENTE", numero=12)
        assert ventas.obtener_venta(conn, sin)["factura_cae"] is None, "PENDIENTE no es un CAE"
