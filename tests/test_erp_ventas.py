"""Los casos de uso de ventas contra los DOS motores de base (P9-M3): la
transacción que cruza LibraCommerce y LibraCore, el modelo de acreditación,
la anulación simétrica, los ganchos y el arqueo del turno."""

from __future__ import annotations

import datetime

from conftest import USUARIO

from libracommerce.erp import Hooks, catalogo, stock, ventas

HOY = datetime.date.today().isoformat()


def _producto(conn, nombre="Yerba", precio=100.0, existencia=10.0):
    pid = catalogo.create_producto(conn, nombre, precio_venta=precio, precio_costo=60.0)
    if existencia:
        stock.ajustar_stock(conn, pid, existencia, "inicial", usuario_id=USUARIO["id"], fecha=HOY)
    return pid


def _venta(abrir, *, pagos=None, items=None, stock_habilitado=True, hooks=None, usuario_id=USUARIO["id"]):
    items = items or [{"nombre": "Suelto", "qty": 2, "precio": 100.0, "subtotal": 200.0, "producto_id": None}]
    pagos = pagos or [{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}]
    total = round(sum(i["subtotal"] for i in items), 2)
    kw = dict(fecha=HOY, items=items, subtotal=total, descuento=0.0, total=total,
              cliente_id=None, cliente_nombre="", usuario_id=usuario_id, observaciones="",
              estado=ventas.estado_segun_pagos(total, pagos), pagos=pagos,
              stock_habilitado=stock_habilitado)
    if hooks is not None:
        kw["hooks"] = hooks
    return ventas.crear_venta_directa(abrir, **kw)


def _caja(conn, venta_numero=None):
    sql = "SELECT tipo, concepto, monto, medio_pago, factura_id FROM caja_movimientos"
    if venta_numero:
        return conn.execute(sql + " WHERE concepto LIKE ? ORDER BY id", (f"%{venta_numero}%",)).fetchall()
    return conn.execute(sql + " ORDER BY id").fetchall()


def _turno(conn, monto_inicial=1000.0):
    from libracore.db.turnos import create_turno

    return create_turno(USUARIO["id"], monto_inicial)


# ── Estado según pagos ───────────────────────────────────────────────────


def test_estado_segun_lo_acreditado():
    assert ventas.estado_segun_pagos(100, [{"monto": 100, "estado": "aprobado"}]) == "cobrada"
    assert ventas.estado_segun_pagos(100, [{"monto": 40, "estado": "aprobado"}]) == "parcial"
    # Un pago pendiente no cuenta: la venta nace pendiente aunque "sume".
    assert ventas.estado_segun_pagos(100, [{"monto": 100, "estado": "pendiente"}]) == "pendiente"
    assert ventas.estado_segun_pagos(100, [
        {"monto": 50, "estado": "aprobado"}, {"monto": 50, "estado": "pendiente"}]) == "parcial"


# ── La venta directa ─────────────────────────────────────────────────────


def test_venta_cobrada_escribe_los_dos_motores(abrir_ventas):
    with abrir_ventas() as conn:
        pid = _producto(conn)
    vid = _venta(abrir_ventas, items=[{"nombre": "Yerba", "qty": 3, "precio": 100.0, "subtotal": 300.0, "producto_id": pid}],
                 pagos=[{"medio": "efectivo", "monto": 300.0, "estado": "aprobado"}])
    with abrir_ventas() as conn:
        v = ventas.obtener_venta(conn, vid)
        assert v["numero"] == "V-00001" and v["estado"] == "cobrada" and v["status"] == "confirmed"
        assert v["total"] == 300.0 and len(v["items"]) == 1 and v["items"][0]["producto_id"] == pid
        assert [p["estado"] for p in v["pagos"]] == ["aprobado"]
        # LibraCommerce: el stock bajó por el ledger.
        assert stock.get_stock_actual(conn, pid) == 7.0
        # LibraCore: un ingreso por el pago, con el concepto que después busca la factura.
        movs = _caja(conn, "V-00001")
        assert len(movs) == 1 and movs[0]["tipo"] == "ingreso" and movs[0]["monto"] == 300.0
        assert movs[0]["concepto"] == "Venta V-00001 — Efectivo"


def test_los_numeros_son_correlativos(abrir_ventas):
    _venta(abrir_ventas)
    _venta(abrir_ventas)
    with abrir_ventas() as conn:
        assert [v["numero"] for v in ventas.listar_ventas(conn)] == ["V-00002", "V-00001"]


def test_sin_stock_habilitado_no_hay_movimiento(abrir_ventas):
    with abrir_ventas() as conn:
        pid = _producto(conn)
    _venta(abrir_ventas, items=[{"nombre": "Yerba", "qty": 1, "precio": 100.0, "subtotal": 100.0, "producto_id": pid}],
           pagos=[{"medio": "efectivo", "monto": 100.0, "estado": "aprobado"}], stock_habilitado=False)
    with abrir_ventas() as conn:
        assert stock.get_stock_actual(conn, pid) == 10.0


def test_pago_pendiente_no_toca_la_caja(abrir_ventas):
    """🔴 El modelo de acreditación: la caja se escribe al acreditar, no al declarar."""
    vid = _venta(abrir_ventas, pagos=[{"medio": "mercadopago", "monto": 200.0, "estado": "pendiente"}])
    with abrir_ventas() as conn:
        v = ventas.obtener_venta(conn, vid)
        assert v["estado"] == "pendiente" and v["status"] == "draft"
        assert _caja(conn, v["numero"]) == []


def test_pago_sin_estado_levanta_y_no_deja_nada(abrir_ventas):
    import pytest
    from libracore.pagos import PagoSinEstado

    with pytest.raises(PagoSinEstado):
        _venta(abrir_ventas, pagos=[{"medio": "efectivo", "monto": 200.0}])
    with abrir_ventas() as conn:
        assert ventas.listar_ventas(conn) == []
        assert _caja(conn) == []


def test_reintenta_si_el_numero_choca(abrir_ventas, monkeypatch):
    """Dos ventas simultáneas calculan el mismo número: la segunda pierde el
    UNIQUE y reintenta con uno fresco, en una transacción nueva."""
    original = ventas.siguiente_numero
    llamadas = []

    def _repetido(conn):
        llamadas.append(1)
        # La primera vez miente y devuelve el número que ya existe.
        return "V-00001" if len(llamadas) == 1 else original(conn)

    _venta(abrir_ventas)
    monkeypatch.setattr(ventas, "siguiente_numero", _repetido)
    vid = _venta(abrir_ventas)
    with abrir_ventas() as conn:
        assert ventas.obtener_venta(conn, vid)["numero"] == "V-00002"
        assert len(ventas.listar_ventas(conn)) == 2
        assert len(_caja(conn)) == 2  # el intento fallido no dejó su ingreso


# ── Ganchos ──────────────────────────────────────────────────────────────


def test_al_confirmar_corre_en_la_misma_transaccion(abrir_ventas):
    vistas = []

    def _gancho(conn, venta):
        # La misma conexión: ve la venta antes del commit.
        assert conn.execute("SELECT COUNT(*) FROM sales WHERE id=?", (venta["id"],)).fetchone()[0] == 1
        vistas.append(venta["estado"])

    _venta(abrir_ventas, hooks=Hooks(al_confirmar_venta=_gancho))
    assert vistas == ["cobrada"]
    # Una venta que nace pendiente no confirma nada todavía.
    _venta(abrir_ventas, hooks=Hooks(al_confirmar_venta=_gancho),
           pagos=[{"medio": "mercadopago", "monto": 200.0, "estado": "pendiente"}])
    assert vistas == ["cobrada"]


def test_un_gancho_que_falla_revierte_la_venta_entera(abrir_ventas):
    import pytest

    def _rompe(conn, venta):
        raise RuntimeError("el pedido no existe")

    with pytest.raises(RuntimeError):
        _venta(abrir_ventas, hooks=Hooks(al_confirmar_venta=_rompe))
    with abrir_ventas() as conn:
        assert ventas.listar_ventas(conn) == []
        assert _caja(conn) == []


# ── Acreditación del QR ──────────────────────────────────────────────────


def test_acreditar_pago_qr_es_idempotente_y_confirma(abrir_ventas):
    confirmadas = []
    ganchos = Hooks(al_confirmar_venta=lambda conn, v: confirmadas.append(v["id"]))
    vid = _venta(abrir_ventas, pagos=[
        {"medio": "efectivo", "monto": 100.0, "estado": "aprobado"},
        {"medio": "mercadopago", "monto": 100.0, "estado": "pendiente"},
    ])
    with abrir_ventas() as conn:
        assert ventas.obtener_venta(conn, vid)["estado"] == "parcial"
        assert len(_caja(conn, "V-00001")) == 1
        assert ventas.acreditar_pago_qr(conn, vid, "555", usuario_id=USUARIO["id"], hooks=ganchos) is True
        # La segunda pasada (el poll y el webhook llegan los dos) no escribe nada.
        assert ventas.acreditar_pago_qr(conn, vid, "555", hooks=ganchos) is False
        conn.commit()
        v = ventas.obtener_venta(conn, vid)
        assert v["estado"] == "cobrada" and v["status"] == "confirmed"
        assert [p["estado"] for p in v["pagos"]] == ["aprobado", "aprobado"]
        assert v["pagos"][1]["referencia"] == "MP#555"
        movs = _caja(conn, "V-00001")
        assert len(movs) == 2 and movs[1]["medio_pago"] == "mercadopago" and movs[1]["monto"] == 100.0
    assert confirmadas == [vid]


def test_acreditar_sin_pendientes_no_hace_nada(abrir_ventas):
    vid = _venta(abrir_ventas)
    with abrir_ventas() as conn:
        assert ventas.acreditar_pago_qr(conn, vid, "1") is False
        assert ventas.acreditar_pago_qr(conn, 999, "1") is False


def test_sellar_referencia_solo_los_electronicos_vacios(abrir_ventas):
    vid = _venta(abrir_ventas, pagos=[
        {"medio": "efectivo", "monto": 100.0, "estado": "aprobado"},
        {"medio": "mercadopago", "monto": 50.0, "estado": "aprobado"},
        {"medio": "mercadopago", "monto": 50.0, "estado": "aprobado", "referencia": "MP#viejo"},
    ])
    with abrir_ventas() as conn:
        ventas.sellar_referencia_mp(conn, vid, "777")
        conn.commit()
        refs = [p["referencia"] for p in ventas.obtener_venta(conn, vid)["pagos"]]
        assert refs == ["", "MP#777", "MP#viejo"]


# ── Links ────────────────────────────────────────────────────────────────


def test_links_a_otros_contextos(abrir_ventas):
    vid = _venta(abrir_ventas)
    with abrir_ventas() as conn:
        ventas.set_orden_mp(conn, vid, "orden-1")
        ventas.set_pago_mp(conn, vid, "pago-1")
        conn.commit()
        v = ventas.obtener_venta(conn, vid)
        assert v["mp_order_id"] == "orden-1" and v["mp_payment_id"] == "pago-1"
        assert ventas.obtener_venta_por_orden_mp(conn, "orden-1")["id"] == vid
        assert ventas.obtener_venta_por_orden_mp(conn, "nada") is None
        assert ventas.obtener_venta(conn, 999) is None


def test_vincular_cobros_de_venta_ata_la_caja_a_la_factura(abrir_ventas):
    _venta(abrir_ventas)  # V-00001
    vid = _venta(abrir_ventas, pagos=[  # V-00002, dos ingresos
        {"medio": "efectivo", "monto": 100.0, "estado": "aprobado"},
        {"medio": "transferencia", "monto": 100.0, "estado": "aprobado"},
    ])
    with abrir_ventas() as conn:
        # Un producto puede meter el pedido entre el número y el guion (Restolibra):
        # el patrón lo tiene que cubrir igual.
        conn.execute(
            "INSERT INTO caja_movimientos (fecha, tipo, concepto, monto, medio_pago) "
            "VALUES (?,?,?,?,?)", (HOY, "ingreso", "Venta V-00002 (pedido P-0001) — efectivo", 1.0, "efectivo"))
        conn.execute(
            "INSERT INTO facturas (tipo, punto_venta, numero, fecha, cliente_cuit, cliente_razon, "
            "cliente_iva_cond, items, subtotal, iva_amount, total, ambiente) VALUES (11, 5, 11, ?, '', 'CF', 5, '[]', 200, 0, 200, 'homologacion')",
            (HOY,))
        factura_id = conn.execute("SELECT MAX(id) FROM facturas").fetchone()[0]
        assert ventas.vincular_cobros_de_venta(conn, "V-00002", factura_id) == 3
        # `V-0000` no matchea a `V-00002`, y V-00001 queda sin tocar.
        assert ventas.vincular_cobros_de_venta(conn, "V-0000", factura_id) == 0
        ventas.vincular_factura(conn, vid, factura_id)
        conn.commit()
        assert [m["factura_id"] for m in _caja(conn, "V-00001")] == [None]
        v = ventas.obtener_venta(conn, vid)
        assert v["factura_id"] == factura_id and v["factura_display"] == "FACTURA C 0005-00000011"
        fila = ventas.listar_ventas(conn, tab="facturadas")
        assert [x["id"] for x in fila] == [vid] and fila[0]["factura_display"] == "FACTURA C 0005-00000011"
        assert [x["numero"] for x in ventas.listar_ventas(conn, tab="sin_facturar")] == ["V-00001"]


# ── Listado ──────────────────────────────────────────────────────────────


def test_listado_busca_sin_distinguir_mayusculas(abrir_ventas):
    _venta(abrir_ventas)
    with abrir_ventas() as conn:
        conn.execute("UPDATE sales SET customer_name_snapshot='Pérez' WHERE id=1")
        conn.commit()
        assert len(ventas.listar_ventas(conn, q="v-00001")) == 1
        assert len(ventas.listar_ventas(conn, q="pérez")) == 1
        assert ventas.listar_ventas(conn, q="zzz") == []
        assert ventas.listar_ventas(conn, desde="2999-01-01") == []
        assert len(ventas.listar_ventas(conn, hasta="2999-01-01")) == 1


# ── Anulación ────────────────────────────────────────────────────────────


def test_anular_repone_stock_revierte_caja_y_avisa(abrir_ventas):
    import pytest

    anuladas = []
    ganchos = Hooks(al_anular_venta=lambda conn, v: anuladas.append(v["estado"]))
    with abrir_ventas() as conn:
        pid = _producto(conn)
    vid = _venta(abrir_ventas, items=[{"nombre": "Yerba", "qty": 4, "precio": 100.0, "subtotal": 400.0, "producto_id": pid}],
                 pagos=[{"medio": "efectivo", "monto": 400.0, "estado": "aprobado"}])
    with abrir_ventas() as conn:
        assert stock.get_stock_actual(conn, pid) == 6.0
        assert ventas.anular_venta(conn, vid, usuario_id=USUARIO["id"], hooks=ganchos) is True
        assert ventas.anular_venta(conn, vid, hooks=ganchos) is False  # no revierte dos veces
        conn.commit()
        v = ventas.obtener_venta(conn, vid)
        assert v["estado"] == "anulada" and v["status"] == "cancelled"
        assert stock.get_stock_actual(conn, pid) == 10.0
        movs = _caja(conn, "V-00001")
        assert [m["tipo"] for m in movs] == ["ingreso", "egreso"] and movs[1]["monto"] == 400.0
        with pytest.raises(ValueError):
            ventas.anular_venta(conn, 999)
    assert anuladas == ["anulada"]


def test_anular_una_venta_fiada_acredita_la_cuenta_corriente(abrir_ventas):
    with abrir_ventas() as conn:
        # `sales.customer_party_id` referencia `parties`; los productos mantienen
        # un party espejo por cliente, con el mismo id (`sincronizar_parties_de_clientes`).
        conn.execute("INSERT INTO clients (id, name, cuit_dni) VALUES (5, 'Cliente', '20111111112')")
        conn.execute("INSERT INTO parties (id, party_type, display_name) VALUES (5, 'customer', 'Cliente')")
        cid = 5
        conn.commit()
    vid = ventas.crear_venta_directa(
        abrir_ventas, fecha=HOY, items=[{"nombre": "X", "qty": 1, "precio": 100.0, "subtotal": 100.0}],
        subtotal=100.0, descuento=0.0, total=100.0, cliente_id=cid, cliente_nombre="Cliente",
        usuario_id=USUARIO["id"], observaciones="", estado="cobrada",
        pagos=[{"medio": "cuenta_corriente", "monto": 100.0, "estado": "aprobado"}], stock_habilitado=False)
    with abrir_ventas() as conn:
        ventas.anular_venta(conn, vid)
        conn.commit()
        cc = conn.execute("SELECT monto FROM cc_pagos WHERE cliente_id=?", (cid,)).fetchall()
        assert [float(r["monto"]) for r in cc] == [100.0]


# ── Turnos ───────────────────────────────────────────────────────────────


def test_la_venta_se_ata_al_turno_abierto_y_el_arqueo_la_cuenta(abrir_ventas):
    with abrir_ventas() as conn:
        tid = _turno(conn, 1000.0)
    _venta(abrir_ventas, pagos=[{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}])
    _venta(abrir_ventas, pagos=[{"medio": "transferencia", "monto": 200.0, "estado": "aprobado"}])
    _venta(abrir_ventas, pagos=[{"medio": "mercadopago", "monto": 200.0, "estado": "pendiente"}])  # no cobrada
    _venta(abrir_ventas, usuario_id=None)  # sin cajero: sin turno
    with abrir_ventas() as conn:
        assert ventas.obtener_venta(conn, 1)["turno_id"] == tid
        assert ventas.obtener_venta(conn, 4)["turno_id"] is None
        r = ventas.resumen_turno(conn, tid)
        assert [v["numero"] for v in r["ventas"]] == ["V-00001", "V-00002", "V-00003"]
        assert r["pagos_por_medio"] == {"efectivo": 200.0, "transferencia": 200.0}
        assert r["total_ventas"] == 400.0 and r["efectivo_ventas"] == 200.0
        assert ventas.cerrar_turno(conn, tid, 1190.0, "faltan 10") is True
        assert ventas.cerrar_turno(conn, 999, 0) is False
        conn.commit()
        t = conn.execute("SELECT estado, monto_esperado_cierre, monto_declarado_cierre, notas FROM turnos_caja WHERE id=?", (tid,)).fetchone()
        assert t["estado"] == "cerrado" and t["monto_esperado_cierre"] == 1200.0
        assert t["monto_declarado_cierre"] == 1190.0 and t["notas"] == "faltan 10"


# ── El schema que el módulo necesita ─────────────────────────────────────


def test_la_fk_de_ventas_pagos_apunta_a_sales_y_conserva_estado(abrir_ventas):
    """La fixture ya la repuntó (y verificó que la segunda vez no toca nada);
    acá se mira el resultado: la FK está y `estado` sobrevivió al rebuild."""
    with abrir_ventas() as conn:
        cols = {r[1] if isinstance(r, tuple) else r["name"] for r in conn.execute("PRAGMA table_info(ventas_pagos)").fetchall()}
        assert "estado" in cols
        # Un pago contra una venta que no existe rebota: la FK apunta a `sales`.
        import sqlite3

        import pytest

        with pytest.raises(sqlite3.IntegrityError):
            ventas.agregar_pago(conn, 12345, "efectivo", 1.0, estado="aprobado")
        conn.rollback()
