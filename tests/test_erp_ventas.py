"""Los casos de uso de ventas contra los DOS motores de base (P9-M3): la
transacción que cruza LibraCommerce y LibraCore, el modelo de acreditación,
la anulación simétrica, los ganchos y el arqueo del turno."""

from __future__ import annotations

import datetime
import sqlite3
from decimal import Decimal

import pytest
from conftest import USUARIO

from libracommerce.erp import Hooks, Insumo, catalogo, stock, ventas

HOY = datetime.date.today().isoformat()


def _producto(conn, nombre="Yerba", precio=100.0, existencia=10.0):
    pid = catalogo.create_producto(conn, nombre, precio_venta=precio, precio_costo=60.0)
    if existencia:
        stock.ajustar_stock(conn, pid, existencia, "inicial", usuario_id=USUARIO["id"], fecha=HOY)
    return pid


def _venta(abrir, *, pagos=None, items=None, stock_habilitado=True, hooks=None,
          usuario_id=USUARIO["id"], **opciones):
    items = items or [{"nombre": "Suelto", "qty": 2, "precio": 100.0, "subtotal": 200.0, "producto_id": None}]
    pagos = pagos or [{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}]
    total = round(sum(i["subtotal"] for i in items), 2)
    kw = dict(fecha=HOY, items=items, subtotal=total, descuento=0.0, total=total,
              cliente_id=None, cliente_nombre="", usuario_id=usuario_id, observaciones="",
              estado=ventas.estado_segun_pagos(total, pagos), pagos=pagos,
              stock_habilitado=stock_habilitado, **opciones)
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


# ── F1: ganchos nuevos (numerador, turno_para, cliente_cc_de) ────────────


def test_numerador_default_es_v_incremental(abrir_ventas):
    with abrir_ventas() as conn:
        assert Hooks().numerador(conn) == "V-00001"


def test_numerador_enganchado_cambia_el_prefijo(abrir_ventas):
    """🔑 mutación (e): si `registrar_venta` ignorara `hooks.numerador`, la
    venta seguiría naciendo `V-00001` y este test da rojo."""
    ganchos = Hooks(numerador=lambda conn: "POS-000123")
    vid = _venta(abrir_ventas, hooks=ganchos)
    with abrir_ventas() as conn:
        v = ventas.obtener_venta(conn, vid)
        assert v["numero"] == "POS-000123"
        # La caja interpola el número en el concepto: sigue el prefijo nuevo.
        assert _caja(conn, "POS-000123")[0]["concepto"] == "Venta POS-000123 — Efectivo"


def test_numerador_enganchado_sobrevive_al_reintento(abrir_ventas):
    """El reintento por número repetido no depende de qué numerador esté
    enganchado: si el enganchado siempre choca, `registrar_venta` vuelve a
    llamarlo en CADA intento (no se cuelga en uno solo) y agota los
    reintentos, igual que con el numerador default."""
    llamadas = []

    def _numerador_fijo(conn):
        llamadas.append(1)
        return "POS-000001"

    ganchos = Hooks(numerador=_numerador_fijo)
    _venta(abrir_ventas, hooks=ganchos)  # ocupa "POS-000001"
    with pytest.raises(sqlite3.IntegrityError):
        _venta(abrir_ventas, hooks=ganchos)  # siempre choca: se agotan los reintentos
    # La primera venta + un intento por cada reintento de la segunda.
    assert len(llamadas) == 1 + ventas.INTENTOS_POR_NUMERO


def test_turno_para_default_es_por_cajero(abrir_ventas):
    with abrir_ventas() as conn:
        tid = _turno(conn, 500.0)
        assert Hooks().turno_para(conn, USUARIO["id"])["id"] == tid
        assert Hooks().turno_para(conn, None) is None


def test_turno_para_enganchado_reemplaza_al_de_cajero(abrir_ventas):
    """VentaLibra: un turno COMPARTIDO, no uno por usuario."""
    otro_usuario_id = USUARIO["id"] + 1
    with abrir_ventas() as conn:
        conn.execute(
            "INSERT INTO usuarios (id, username, nombre, password_hash, role) VALUES (?,?,?,?,?)",
            (otro_usuario_id, "otro", "Otro cajero", "x", "operador"),
        )
        conn.commit()
        # El turno lo abrió USUARIO["id"]; `otro_usuario_id` no tiene turno
        # propio, y con el turno por-cajero (el default) no se le ataría nada.
        tid = _turno(conn, 500.0)
    compartido = Hooks(turno_para=lambda conn, uid: {"id": tid} if uid else None)
    vid = _venta(abrir_ventas, hooks=compartido, usuario_id=otro_usuario_id)
    with abrir_ventas() as conn:
        assert ventas.obtener_venta(conn, vid)["turno_id"] == tid


def test_exigir_turno_sin_turno_levanta_y_no_deja_nada(abrir_ventas):
    with pytest.raises(ventas.SinTurno):
        _venta(abrir_ventas, exigir_turno=True)
    with abrir_ventas() as conn:
        assert ventas.listar_ventas(conn) == []
        assert _caja(conn) == []


def test_exigir_turno_con_turno_registra_normal(abrir_ventas):
    with abrir_ventas() as conn:
        _turno(conn, 500.0)
    vid = _venta(abrir_ventas, exigir_turno=True)
    with abrir_ventas() as conn:
        assert ventas.obtener_venta(conn, vid) is not None


def test_caja_con_turno_apunta_el_turno_id(abrir_ventas):
    """🔑 mutación (a): sin pasar `turno_id` a `create_caja_movimiento`, esto
    da rojo (todos `None`)."""
    with abrir_ventas() as conn:
        tid = _turno(conn, 500.0)
    _venta(abrir_ventas, caja_con_turno=True)
    with abrir_ventas() as conn:
        movs = conn.execute("SELECT turno_id FROM caja_movimientos").fetchall()
        assert [m["turno_id"] for m in movs] == [tid]


def test_sin_caja_con_turno_no_apunta_nada(abrir_ventas):
    """Default `False`: el comportamiento de hoy no cambia."""
    with abrir_ventas() as conn:
        _turno(conn, 500.0)
    _venta(abrir_ventas)
    with abrir_ventas() as conn:
        movs = conn.execute("SELECT turno_id FROM caja_movimientos").fetchall()
        assert [m["turno_id"] for m in movs] == [None]


def test_acreditar_pago_qr_con_turno_toma_el_de_venta_links(abrir_ventas):
    with abrir_ventas() as conn:
        tid = _turno(conn, 500.0)
    vid = _venta(abrir_ventas, pagos=[{"medio": "mercadopago", "monto": 200.0, "estado": "pendiente"}])
    with abrir_ventas() as conn:
        assert ventas.obtener_venta(conn, vid)["turno_id"] == tid
        assert ventas.acreditar_pago_qr(conn, vid, "999", caja_con_turno=True) is True
        conn.commit()
        row = conn.execute("SELECT turno_id FROM caja_movimientos ORDER BY id DESC LIMIT 1").fetchone()
        assert row["turno_id"] == tid


def test_cliente_cc_de_default_es_cliente_id(abrir_ventas):
    assert Hooks().cliente_cc_de(None, {"cliente_id": 5}) == 5
    assert Hooks().cliente_cc_de(None, {"cliente_id": None}) is None


def test_cliente_cc_de_enganchado_traduce_el_cliente(abrir_ventas):
    """VentaLibra: el `cliente_id` de la venta (un `party_id`) no es el
    `clients.id` real al que se le acredita la cuenta corriente.

    🔑 mutación (c): si `anular_venta` usara `venta["cliente_id"]` (5) en vez
    de `hooks.cliente_cc_de` (que lo traduce a 55), el crédito cae en el
    cliente equivocado y este test da rojo."""
    with abrir_ventas() as conn:
        conn.execute("INSERT INTO clients (id, name, cuit_dni) VALUES (55, 'Cliente CC', '20111111112')")
        conn.execute("INSERT INTO parties (id, party_type, display_name) VALUES (5, 'customer', 'Party 5')")
        conn.commit()
    ganchos = Hooks(cliente_cc_de=lambda conn, venta: 55 if venta["cliente_id"] == 5 else None)
    vid = ventas.crear_venta_directa(
        abrir_ventas, fecha=HOY, items=[{"nombre": "X", "qty": 1, "precio": 100.0, "subtotal": 100.0}],
        subtotal=100.0, descuento=0.0, total=100.0, cliente_id=5, cliente_nombre="Party 5",
        usuario_id=USUARIO["id"], observaciones="", estado="cobrada",
        pagos=[{"medio": "cuenta_corriente", "monto": 100.0, "estado": "aprobado"}],
        stock_habilitado=False, hooks=ganchos,
    )
    with abrir_ventas() as conn:
        ventas.anular_venta(conn, vid, hooks=ganchos)
        conn.commit()
        acreditado_55 = conn.execute("SELECT monto FROM cc_pagos WHERE cliente_id=55").fetchall()
        assert [float(r["monto"]) for r in acreditado_55] == [100.0]
        assert conn.execute("SELECT COUNT(*) FROM cc_pagos WHERE cliente_id=5").fetchone()[0] == 0


# ── F1: vuelto (recibido) ─────────────────────────────────────────────────


def _agregar_columna_recibido(conn):
    """Sólo para los tests del vuelto: la columna la agrega la migración
    `0010` de LibraCore (otro trabajo, en paralelo), no este módulo."""
    conn.execute("ALTER TABLE ventas_pagos ADD COLUMN recibido NUMERIC")
    conn.commit()


def test_recibido_se_guarda_solo_si_viene_un_valor(abrir_ventas):
    with abrir_ventas() as conn:
        _agregar_columna_recibido(conn)
    vid = _venta(abrir_ventas, pagos=[
        {"medio": "efectivo", "monto": 200.0, "estado": "aprobado", "recibido": 300.0},
    ])
    with abrir_ventas() as conn:
        pago = ventas.obtener_venta(conn, vid)["pagos"][0]
        assert float(pago["recibido"]) == 300.0


def test_recibido_sin_columna_no_rompe_el_insert_de_siempre(abrir_ventas):
    """Contalibra y Restolibra: sin la migración 0010, ni la columna ni el
    campo. El INSERT de siempre sigue andando igual."""
    vid = _venta(abrir_ventas)  # pago sin "recibido", tabla sin la columna
    with abrir_ventas() as conn:
        pago = ventas.obtener_venta(conn, vid)["pagos"][0]
        assert "recibido" not in pago


# ── F1: variantes ──────────────────────────────────────────────────────────


def test_variante_viaja_a_sale_items_y_al_stock(abrir_ventas):
    with abrir_ventas() as conn:
        pid = _producto(conn, existencia=10.0)
        vidr = conn.execute(
            "INSERT INTO item_variants (item_id, sku, name) VALUES (?, 'SKU-1', 'Chico')", (pid,)
        ).lastrowid
        conn.commit()
    vid = _venta(abrir_ventas, items=[
        {"nombre": "Yerba Chica", "qty": 2, "precio": 100.0, "subtotal": 200.0,
         "producto_id": pid, "variante_id": vidr},
    ], pagos=[{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}])
    with abrir_ventas() as conn:
        v = ventas.obtener_venta(conn, vid)
        assert v["items"][0]["variante_id"] == vidr
        mov = conn.execute(
            "SELECT variant_id FROM stock_movements WHERE source_id=?", (vid,)
        ).fetchone()
        assert mov["variant_id"] == vidr
        assert stock.get_stock_actual(conn, pid) == 8.0


def test_anulacion_repone_por_la_misma_variante(abrir_ventas):
    with abrir_ventas() as conn:
        pid = _producto(conn, existencia=10.0)
        v1 = conn.execute(
            "INSERT INTO item_variants (item_id, sku, name) VALUES (?, 'SKU-A', 'A')", (pid,)
        ).lastrowid
        v2 = conn.execute(
            "INSERT INTO item_variants (item_id, sku, name) VALUES (?, 'SKU-B', 'B')", (pid,)
        ).lastrowid
        conn.commit()
    vid = _venta(abrir_ventas, items=[
        {"nombre": "A", "qty": 3, "precio": 50.0, "subtotal": 150.0, "producto_id": pid, "variante_id": v1},
        {"nombre": "B", "qty": 2, "precio": 50.0, "subtotal": 100.0, "producto_id": pid, "variante_id": v2},
    ], pagos=[{"medio": "efectivo", "monto": 250.0, "estado": "aprobado"}])
    with abrir_ventas() as conn:
        ventas.anular_venta(conn, vid)
        conn.commit()
        assert stock.get_stock_actual(conn, pid) == 10.0
        repos = conn.execute(
            "SELECT variant_id, quantity_delta FROM stock_movements "
            "WHERE source_id=? AND reason_code='anulacion' ORDER BY variant_id", (vid,)
        ).fetchall()
        assert [(r["variant_id"], float(r["quantity_delta"])) for r in repos] == [(v1, 3.0), (v2, 2.0)]


# ── F1: anular_venta tolerante a filas viejas de VentaLibra ────────────────


def _venta_vieja(conn, *, pid: int, cantidad: float = 4.0, precio: float = 100.0):
    """Simula lo que escribe HOY `libracommerce.usecases.sales.confirm_sale`
    —el camino viejo que usa VentaLibra—: `reason_code` NULL,
    `source_type='sale'`, `movement_type='sale'`. No pasa por `erp.ventas`
    a propósito: es la forma vieja del ledger, no la nueva."""
    from libracommerce.erp.catalogo import get_default_deposito_id

    deposito_id = get_default_deposito_id(conn)
    numero = ventas.siguiente_numero(conn)
    total = cantidad * precio
    cur = conn.execute(
        "INSERT INTO sales (number, occurred_on, status, status_detail, total, subtotal, source_type) "
        "VALUES (?,?,?,?,?,?,'pos')",
        (numero, HOY, "confirmed", None, total, total),
    )
    vid = cur.lastrowid
    conn.execute(
        "INSERT INTO sale_items (sale_id, kind, item_id, description_snapshot, quantity, unit_price) "
        "VALUES (?, 'product', ?, 'Viejo', ?, ?)",
        (vid, pid, cantidad, precio),
    )
    conn.execute(
        "INSERT INTO stock_movements (item_id, location_id, movement_type, quantity_delta, "
        "occurred_at, source_type, source_id) VALUES (?,?,?,?,?,?,?)",
        (pid, deposito_id, "sale", -cantidad, HOY, "sale", vid),
    )
    conn.execute(
        "INSERT INTO ventas_pagos (venta_id, medio, monto, referencia, estado) VALUES (?,?,?,?,?)",
        (vid, "efectivo", total, "", "aprobado"),
    )
    return vid, deposito_id


def test_anular_repone_filas_viejas_de_ventalibra(abrir_ventas):
    """🔑 mutación (b): si `_COND_VENDIDO` volviera a exigir sólo
    `reason_code='venta'`, la fila vieja (`reason_code` NULL) no matchea, no
    se repone nada y este test da rojo."""
    with abrir_ventas() as conn:
        pid = _producto(conn, existencia=10.0)
    with abrir_ventas() as conn:
        vid, _dep = _venta_vieja(conn, pid=pid, cantidad=4.0, precio=100.0)
        conn.commit()
    with abrir_ventas() as conn:
        assert stock.get_stock_actual(conn, pid) == 6.0
        assert ventas.anular_venta(conn, vid) is True
        conn.commit()
        assert stock.get_stock_actual(conn, pid) == 10.0
        assert ventas.obtener_venta(conn, vid)["status"] == "cancelled"
        # No se hizo ningún UPDATE sobre el ledger: sigue habiendo sólo dos
        # filas por esta venta (la vieja y la reposición nueva).
        n = conn.execute("SELECT COUNT(*) FROM stock_movements WHERE source_id=?", (vid,)).fetchone()[0]
        assert n == 2


def test_anular_con_devolucion_parcial_levanta_y_no_mueve_nada(abrir_ventas):
    """🔑 Sin esta guarda, anular una venta con devolución parcial dobla el
    reintegro: revierte el pago ORIGINAL completo encima de lo que la
    devolución ya le dio de vuelta al cliente. Mutación: sacar la guarda —
    ver el reporte para la plata exacta que queda mal."""
    with abrir_ventas() as conn:
        pid = _producto(conn, existencia=10.0)
    vid = _venta(abrir_ventas, items=[
        {"nombre": "Yerba", "qty": 4, "precio": 100.0, "subtotal": 400.0, "producto_id": pid},
    ], pagos=[{"medio": "efectivo", "monto": 400.0, "estado": "aprobado"}])
    with abrir_ventas() as conn:
        item_id_linea = conn.execute(
            "SELECT id FROM sale_items WHERE sale_id=?", (vid,)
        ).fetchone()["id"]
        deposito_id = conn.execute(
            "SELECT location_id FROM stock_movements WHERE source_id=?", (vid,)
        ).fetchone()["location_id"]
        ventas.devolver_items(conn, vid, {item_id_linea: 1.0}, deposito_id)  # $100 ya vueltos
        conn.commit()

    with abrir_ventas() as conn:
        n_stock_antes = conn.execute(
            "SELECT COUNT(*) FROM stock_movements WHERE source_id=?", (vid,)
        ).fetchone()[0]
        status_antes = ventas.obtener_venta(conn, vid)["status"]

        levanto = False
        try:
            ventas.anular_venta(conn, vid)
        except ventas.VentaConDevoluciones:
            levanto = True
            conn.rollback()
        else:
            conn.commit()
        assert levanto, "anular_venta tiene que rechazar una venta con devolución parcial"

        n_stock_despues = conn.execute(
            "SELECT COUNT(*) FROM stock_movements WHERE source_id=?", (vid,)
        ).fetchone()[0]
        status_despues = ventas.obtener_venta(conn, vid)["status"]
        assert n_stock_despues == n_stock_antes  # no se repuso stock de nuevo
        assert status_despues == status_antes  # sigue confirmada, no anulada

        ingresos = float(conn.execute(
            "SELECT COALESCE(SUM(monto),0) FROM caja_movimientos WHERE tipo='ingreso'"
        ).fetchone()[0])
        egresos = float(conn.execute(
            "SELECT COALESCE(SUM(monto),0) FROM caja_movimientos WHERE tipo='egreso'"
        ).fetchone()[0])
        # La única plata devuelta hasta acá es la de la devolución parcial
        # ($100 de $400 cobrados). Nunca puede superar lo cobrado.
        assert egresos == 100.0
        assert egresos <= ingresos


def test_anular_venta_devuelta_del_todo_levanta(abrir_ventas):
    with abrir_ventas() as conn:
        pid = _producto(conn, existencia=10.0)
    vid = _venta(abrir_ventas, items=[
        {"nombre": "Yerba", "qty": 2, "precio": 100.0, "subtotal": 200.0, "producto_id": pid},
    ], pagos=[{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}])
    with abrir_ventas() as conn:
        item_id_linea = conn.execute(
            "SELECT id FROM sale_items WHERE sale_id=?", (vid,)
        ).fetchone()["id"]
        deposito_id = conn.execute(
            "SELECT location_id FROM stock_movements WHERE source_id=?", (vid,)
        ).fetchone()["location_id"]
        ventas.devolver_items(conn, vid, {item_id_linea: 2.0}, deposito_id)
        conn.commit()
        assert ventas.obtener_venta(conn, vid)["estado"] == "devuelta"


# ── F4: depósito por venta (VentaLibra multisucursal) ──────────────────────


def test_venta_con_deposito_descuenta_de_ese_deposito_no_del_default(abrir_ventas):
    """🔑 mutación (a): si `registrar_venta`/`descontar_stock_venta` ignorara
    `deposito_id` (siempre el default), el movimiento de esta venta seguiría
    cayendo en el depósito principal y este test da rojo."""
    with abrir_ventas() as conn:
        pid = _producto(conn, existencia=10.0)
        default_id = catalogo.get_default_deposito_id(conn)
        sucursal_b = catalogo.create_deposito(conn, "Sucursal B")
        conn.commit()
    vid = _venta(abrir_ventas, items=[
        {"nombre": "Yerba", "qty": 3, "precio": 100.0, "subtotal": 300.0, "producto_id": pid},
    ], pagos=[{"medio": "efectivo", "monto": 300.0, "estado": "aprobado"}],
        deposito_id=sucursal_b)
    with abrir_ventas() as conn:
        mov = conn.execute(
            "SELECT location_id, quantity_delta FROM stock_movements WHERE source_id=?", (vid,)
        ).fetchone()
        assert mov["location_id"] == sucursal_b
        assert float(mov["quantity_delta"]) == -3.0
        # El total del producto (todos los depósitos) bajó igual que siempre.
        assert stock.get_stock_actual(conn, pid) == 7.0
        # Pero el default no vio NINGÚN movimiento de esta venta.
        en_default = conn.execute(
            "SELECT COUNT(*) FROM stock_movements WHERE source_id=? AND location_id=?",
            (vid, default_id),
        ).fetchone()[0]
        assert en_default == 0
        stock_b = catalogo.get_stock_por_deposito(conn, sucursal_b)
        assert stock_b[0]["id"] == pid and stock_b[0]["stock_actual"] == -3.0


def test_venta_sin_deposito_descuenta_del_default(abrir_ventas):
    """El comportamiento de hoy — Contalibra y Restolibra no mandan
    `deposito_id` — no cambia. 🔑 mutación (b): si el default se resolviera
    distinto (por ejemplo, siempre el primero por id en vez de
    `is_default`/orden), esto da rojo con más de un depósito en danza."""
    with abrir_ventas() as conn:
        pid = _producto(conn, existencia=10.0)
        default_id = catalogo.get_default_deposito_id(conn)
        catalogo.create_deposito(conn, "Sucursal B")  # existe, pero nadie la pide
        conn.commit()
    vid = _venta(abrir_ventas, items=[
        {"nombre": "Yerba", "qty": 2, "precio": 100.0, "subtotal": 200.0, "producto_id": pid},
    ], pagos=[{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}])
    with abrir_ventas() as conn:
        mov = conn.execute(
            "SELECT location_id FROM stock_movements WHERE source_id=?", (vid,)
        ).fetchone()
        assert mov["location_id"] == default_id
        assert stock.get_stock_actual(conn, pid) == 8.0


def test_deposito_inexistente_da_depositoinexistente_sin_reintentar(abrir_ventas):
    """🔑 mutación (c): sin la validación, un `deposito_id` inventado no
    rebota acá — o revienta más abajo como `IntegrityError` (la FK de
    `stock_movements.location_id`), que ningún `except` de
    `web/ventas_router.py::crear` atrapa como 422."""
    with abrir_ventas() as conn:
        pid = _producto(conn, existencia=10.0)
    with pytest.raises(ventas.DepositoInexistente) as exc_info:
        _venta(abrir_ventas, items=[
            {"nombre": "Yerba", "qty": 1, "precio": 100.0, "subtotal": 100.0, "producto_id": pid},
        ], pagos=[{"medio": "efectivo", "monto": 100.0, "estado": "aprobado"}],
            deposito_id=999999)
    assert "999999" in str(exc_info.value)
    # Nada quedó escrito: ni la venta, ni la caja, ni el stock se tocó.
    with abrir_ventas() as conn:
        assert ventas.listar_ventas(conn) == []
        assert _caja(conn) == []
        assert stock.get_stock_actual(conn, pid) == 10.0


def test_deposito_inactivo_tambien_es_depositoinexistente(abrir_ventas):
    """"Válido/activo": un depósito que existe pero está desactivado tampoco
    es un destino aceptable para una venta nueva."""
    with abrir_ventas() as conn:
        sucursal_b = catalogo.create_deposito(conn, "Sucursal B")
        catalogo.update_deposito(conn, sucursal_b, "Sucursal B", "", 0)
        conn.commit()
    with pytest.raises(ventas.DepositoInexistente):
        _venta(abrir_ventas, deposito_id=sucursal_b)


def test_anular_venta_con_deposito_repone_en_ese_deposito(abrir_ventas):
    """`anular_venta` repone fila por fila del ledger (el `location_id` sale
    de la fila que descontó, no de un parámetro nuevo): confirma que sigue
    reponiendo en el depósito del que salió, no en el default."""
    with abrir_ventas() as conn:
        pid = _producto(conn, existencia=10.0)
        default_id = catalogo.get_default_deposito_id(conn)
        sucursal_b = catalogo.create_deposito(conn, "Sucursal B")
        conn.commit()
    vid = _venta(abrir_ventas, items=[
        {"nombre": "Yerba", "qty": 4, "precio": 100.0, "subtotal": 400.0, "producto_id": pid},
    ], pagos=[{"medio": "efectivo", "monto": 400.0, "estado": "aprobado"}],
        deposito_id=sucursal_b)
    with abrir_ventas() as conn:
        assert catalogo.get_stock_por_deposito(conn, sucursal_b)[0]["stock_actual"] == -4.0
        assert ventas.anular_venta(conn, vid, usuario_id=USUARIO["id"]) is True
        conn.commit()
        repos = conn.execute(
            "SELECT location_id, quantity_delta FROM stock_movements "
            "WHERE source_id=? AND reason_code='anulacion'", (vid,)
        ).fetchall()
        assert [(r["location_id"], float(r["quantity_delta"])) for r in repos] == [(sucursal_b, 4.0)]
        # El default nunca se tocó en todo este flujo (venta + anulación).
        en_default = conn.execute(
            "SELECT COUNT(*) FROM stock_movements WHERE source_id=? AND location_id=?",
            (vid, default_id),
        ).fetchone()[0]
        assert en_default == 0
        assert stock.get_stock_actual(conn, pid) == 10.0  # el total volvió al punto de partida


# ── Huecos de VentaLibra (v0.16.2): el 409 que confunde y la referencia pisada ──


def test_producto_inexistente_da_productoinexistente_sin_reintentar(abrir_ventas):
    """🔴 Punto 1: un `producto_id` que no existe viola la FK de `sale_items`,
    NO la unicidad de `sales.number` — no se reintenta, y el error nombra el
    producto que falta."""
    with pytest.raises(ventas.ProductoInexistente) as exc_info:
        _venta(abrir_ventas, items=[
            {"nombre": "Fantasma", "qty": 1, "precio": 50.0, "subtotal": 50.0, "producto_id": 999999},
        ], pagos=[{"medio": "efectivo", "monto": 50.0, "estado": "aprobado"}])
    assert "999999" in str(exc_info.value)
    with abrir_ventas() as conn:
        assert ventas.listar_ventas(conn) == []
        assert _caja(conn) == []


def test_conflicto_de_numero_real_sigue_reintentando(abrir_ventas, monkeypatch):
    """Regresión explícita de `_es_conflicto_de_numero`: una violación de
    `sales.number` real (no una FK) se sigue reintentando como hasta ahora."""
    original = ventas.siguiente_numero
    llamadas = []

    def _repetido(conn):
        llamadas.append(1)
        return "V-00001" if len(llamadas) == 1 else original(conn)

    _venta(abrir_ventas)
    monkeypatch.setattr(ventas, "siguiente_numero", _repetido)
    vid = _venta(abrir_ventas)
    with abrir_ventas() as conn:
        assert ventas.obtener_venta(conn, vid)["numero"] == "V-00002"


def test_acreditar_pago_qr_no_pisa_una_referencia_cargada_a_mano(abrir_ventas):
    """🔴 Punto 5: si el mostrador ya cargó una referencia sobre el pago QR
    pendiente, acreditarlo no la debe pisar — mismo criterio que
    `sellar_referencia_mp` (`referencia IS NULL OR referencia=''`)."""
    vid = _venta(abrir_ventas, pagos=[
        {"medio": "mercadopago", "monto": 200.0, "estado": "pendiente", "referencia": "manual-123"},
    ])
    with abrir_ventas() as conn:
        assert ventas.acreditar_pago_qr(conn, vid, "555") is True
        conn.commit()
        pago = ventas.obtener_venta(conn, vid)["pagos"][0]
        assert pago["estado"] == "aprobado"
        assert pago["referencia"] == "manual-123"


# ── F3: anular no revierte lo que nunca se cobró (pago QR pendiente) ───────


def test_anular_con_pago_pendiente_solo_revierte_lo_acreditado(abrir_ventas):
    """🔑 El defecto: anular una venta con el QR pendiente no puede sacar de
    la caja plata que nunca entró — sólo se revierte el pago en efectivo, y
    el pendiente queda en un estado terminal (`vencido`)."""
    with abrir_ventas() as conn:
        pid = _producto(conn, existencia=10.0)
    vid = _venta(abrir_ventas, items=[
        {"nombre": "Yerba", "qty": 4, "precio": 100.0, "subtotal": 400.0, "producto_id": pid},
    ], pagos=[
        {"medio": "efectivo", "monto": 200.0, "estado": "aprobado"},
        {"medio": "mercadopago", "monto": 200.0, "estado": "pendiente"},
    ])
    with abrir_ventas() as conn:
        assert ventas.obtener_venta(conn, vid)["estado"] == "parcial"  # sólo entró el efectivo
        assert ventas.anular_venta(conn, vid, usuario_id=USUARIO["id"]) is True
        conn.commit()
        movs = _caja(conn, "V-00001")
        # Un ingreso (el efectivo, al crear la venta) y UN SOLO egreso: el
        # del efectivo. El QR pendiente nunca escribió un ingreso, así que no
        # genera ningún egreso.
        assert [m["tipo"] for m in movs] == ["ingreso", "egreso"]
        assert movs[0]["monto"] == 200.0 and movs[0]["medio_pago"] == "efectivo"
        assert movs[1]["monto"] == 200.0 and movs[1]["medio_pago"] == "efectivo"

        pagos = ventas.obtener_venta(conn, vid)["pagos"]
        estados = {p["medio"]: p["estado"] for p in pagos}
        assert estados == {"efectivo": "aprobado", "mercadopago": "vencido"}

        # El pago QR quedó en un estado terminal: acreditarlo después no
        # revive la venta ni toca la caja de nuevo.
        assert ventas.acreditar_pago_qr(conn, vid, "555", usuario_id=USUARIO["id"]) is False
        conn.commit()
        assert ventas.obtener_venta(conn, vid)["status"] == "cancelled"
        assert len(_caja(conn, "V-00001")) == 2  # nada nuevo


def test_anular_venta_toda_pendiente_no_genera_ningun_egreso(abrir_ventas):
    """Venta 100% QR sin acreditar: anularla no debe escribir NINGÚN egreso,
    porque no había ningún ingreso que revertir."""
    with abrir_ventas() as conn:
        pid = _producto(conn, existencia=10.0)
    vid = _venta(abrir_ventas, items=[
        {"nombre": "Yerba", "qty": 2, "precio": 100.0, "subtotal": 200.0, "producto_id": pid},
    ], pagos=[{"medio": "mercadopago", "monto": 200.0, "estado": "pendiente"}])
    with abrir_ventas() as conn:
        assert ventas.obtener_venta(conn, vid)["estado"] == "pendiente"
        assert ventas.anular_venta(conn, vid) is True
        conn.commit()
        assert _caja(conn, "V-00001") == []
        pago = ventas.obtener_venta(conn, vid)["pagos"][0]
        assert pago["estado"] == "vencido"
        assert ventas.obtener_venta(conn, vid)["status"] == "cancelled"


def test_acreditar_pago_qr_no_revive_una_venta_anulada(abrir_ventas):
    """🔑 mutación (g): sin el chequeo `venta['status'] == 'cancelled'` en
    `acreditar_pago_qr`, una fila que siga (o vuelva a estar) `pendiente`
    pese a que la venta ya está anulada haría que acreditar escriba caja y
    la venta reviva a `cobrada`."""
    vid = _venta(abrir_ventas, pagos=[{"medio": "mercadopago", "monto": 200.0, "estado": "pendiente"}])
    with abrir_ventas() as conn:
        ventas.anular_venta(conn, vid)
        # Simula la fila vieja / la carrera: el pago sigue "pendiente" pese a
        # que la venta ya está anulada (por ejemplo, una fila de antes de
        # este fix, o el webhook que llega justo entre medio).
        conn.execute("UPDATE ventas_pagos SET estado='pendiente' WHERE venta_id=?", (vid,))
        conn.commit()
        assert ventas.acreditar_pago_qr(conn, vid, "999", usuario_id=USUARIO["id"]) is False
        conn.commit()
        v = ventas.obtener_venta(conn, vid)
        assert v["status"] == "cancelled"
        assert _caja(conn, v["numero"]) == []


def test_anular_venta_cobrada_normal_sigue_como_hoy(abrir_ventas):
    """Regresión: una venta cobrada normal (todos los pagos acreditados)
    anula exactamente igual que antes de este fix — un egreso por pago."""
    with abrir_ventas() as conn:
        pid = _producto(conn, existencia=10.0)
    vid = _venta(abrir_ventas, items=[
        {"nombre": "Yerba", "qty": 3, "precio": 100.0, "subtotal": 300.0, "producto_id": pid},
    ], pagos=[
        {"medio": "efectivo", "monto": 100.0, "estado": "aprobado"},
        {"medio": "transferencia", "monto": 200.0, "estado": "aprobado"},
    ])
    with abrir_ventas() as conn:
        assert ventas.anular_venta(conn, vid) is True
        conn.commit()
        movs = _caja(conn, "V-00001")
        assert [m["tipo"] for m in movs] == ["ingreso", "ingreso", "egreso", "egreso"]
        egresos = sorted(float(m["monto"]) for m in movs if m["tipo"] == "egreso")
        assert egresos == [100.0, 200.0]


# ── F3: el detalle expone el id de la línea, para poder devolverla ─────────


def test_obtener_venta_expone_el_id_de_cada_linea(abrir_ventas):
    """Sin el `id` de `sale_items` en el detalle, ningún cliente de la API
    puede armar el payload de `POST /{vid}/devolver` (que pide
    `sale_item_id`) sin leer la base directamente."""
    with abrir_ventas() as conn:
        pid = _producto(conn)
    vid = _venta(abrir_ventas, items=[
        {"nombre": "Yerba", "qty": 2, "precio": 100.0, "subtotal": 200.0, "producto_id": pid},
    ], pagos=[{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}])
    with abrir_ventas() as conn:
        item = ventas.obtener_venta(conn, vid)["items"][0]
        linea_real = conn.execute(
            "SELECT id FROM sale_items WHERE sale_id=?", (vid,)
        ).fetchone()["id"]
        assert item["id"] == linea_real
        # El id sirve tal cual para devolver esa línea.
        deposito_id = conn.execute(
            "SELECT location_id FROM stock_movements WHERE source_id=?", (vid,)
        ).fetchone()["location_id"]
        resultado = ventas.devolver_items(conn, vid, {item["id"]: 1.0}, deposito_id)
        assert resultado["importe"] == 100.0


def test_devolver_cuenta_la_devolucion_vieja_del_camino_return_sale_items(abrir_ventas):
    """`usecases.sales.return_sale_items` escribe `source_type='sale_return'`
    con `source_id=sale.id` (la venta), no el de otro documento — verificado
    en el código de ese caso de uso. `devolver_items` sobre una venta VIEJA
    (`status='partially_returned'`, el que deja ESE caso de uso) tiene que
    contar esa devolución para el tope."""
    with abrir_ventas() as conn:
        pid = _producto(conn, existencia=10.0)
    with abrir_ventas() as conn:
        vid, deposito_id = _venta_vieja(conn, pid=pid, cantidad=5.0, precio=100.0)
        item_id_linea = conn.execute("SELECT id FROM sale_items WHERE sale_id=?", (vid,)).fetchone()["id"]
        # 3 ya devueltas por el camino viejo: `reason_code` es la POSICIÓN
        # de la línea (acá "0"), `source_type='sale_return'`, `source_id=vid`.
        conn.execute(
            "INSERT INTO stock_movements (item_id, location_id, movement_type, quantity_delta, "
            "occurred_at, source_type, source_id, reason_code) VALUES (?,?,?,?,?,?,?,?)",
            (pid, deposito_id, "return", 3.0, HOY, "sale_return", vid, "0"),
        )
        conn.execute("UPDATE sales SET status='partially_returned' WHERE id=?", (vid,))
        conn.commit()
        with pytest.raises(ValueError):
            # Quedaban 2 sin devolver (5 - 3); pedir 3 excede el tope.
            ventas.devolver_items(conn, vid, {item_id_linea: 3.0}, deposito_id)
        ventas.devolver_items(conn, vid, {item_id_linea: 2.0}, deposito_id)
        conn.commit()
        assert ventas.obtener_venta(conn, vid)["estado"] == "devuelta"


# ── F2 (correcciones sobre la revisión): tope por clave y estados admitidos ─


def test_tope_de_devolucion_es_por_item_y_variante_no_por_linea(abrir_ventas):
    """🔑 mutación: comparar `disponible` contra la cantidad de UNA sola línea
    (en vez de la suma de todas las líneas con esa clave) bloquea una
    devolución legítima — A y B son el mismo producto en dos líneas
    distintas (A=2, B=3): devolver los 3 de B no le debe tocar el cupo a A."""
    with abrir_ventas() as conn:
        pid = _producto(conn, existencia=10.0)
    vid = _venta(abrir_ventas, items=[
        {"nombre": "Yerba (línea A)", "qty": 2, "precio": 100.0, "subtotal": 200.0, "producto_id": pid},
        {"nombre": "Yerba (línea B)", "qty": 3, "precio": 100.0, "subtotal": 300.0, "producto_id": pid},
    ], pagos=[{"medio": "efectivo", "monto": 500.0, "estado": "aprobado"}])
    with abrir_ventas() as conn:
        lineas = conn.execute(
            "SELECT id FROM sale_items WHERE sale_id=? ORDER BY id", (vid,)
        ).fetchall()
        linea_a, linea_b = lineas[0]["id"], lineas[1]["id"]
        deposito_id = conn.execute(
            "SELECT location_id FROM stock_movements WHERE source_id=? LIMIT 1", (vid,)
        ).fetchone()["location_id"]
        ventas.devolver_items(conn, vid, {linea_b: 3.0}, deposito_id)
        conn.commit()
        # A sigue intacta: sus 2 propias tienen que poder devolverse.
        ventas.devolver_items(conn, vid, {linea_a: 2.0}, deposito_id)
        conn.commit()
        assert ventas.obtener_venta(conn, vid)["estado"] == "devuelta"


def test_tope_de_devolucion_neteo_dentro_de_la_misma_llamada(abrir_ventas):
    """🔑 mutación: sin descontar lo pedido DENTRO de esta misma llamada a
    medida que se procesa cada línea, dos líneas del mismo ítem pedidas
    juntas se comparan cada una contra el mismo acumulado (`ya_devuelto`, que
    no cambia durante el `for`) y pueden sumar más que lo vendido."""
    with abrir_ventas() as conn:
        pid = _producto(conn, existencia=10.0)
    vid = _venta(abrir_ventas, items=[
        {"nombre": "Yerba (línea A)", "qty": 2, "precio": 100.0, "subtotal": 200.0, "producto_id": pid},
        {"nombre": "Yerba (línea B)", "qty": 3, "precio": 100.0, "subtotal": 300.0, "producto_id": pid},
    ], pagos=[{"medio": "efectivo", "monto": 500.0, "estado": "aprobado"}])
    with abrir_ventas() as conn:
        lineas = conn.execute(
            "SELECT id FROM sale_items WHERE sale_id=? ORDER BY id", (vid,)
        ).fetchall()
        linea_a, linea_b = lineas[0]["id"], lineas[1]["id"]
        deposito_id = conn.execute(
            "SELECT location_id FROM stock_movements WHERE source_id=? LIMIT 1", (vid,)
        ).fetchone()["location_id"]
        # Vendido total = 5. Pedir A:2 + B:4 en la MISMA llamada excede el
        # total, aunque cada uno por separado (contra el `ya_devuelto` del
        # ledger, que arranca en 0) pareciera entrar.
        with pytest.raises(ValueError):
            ventas.devolver_items(conn, vid, {linea_a: 2.0, linea_b: 4.0}, deposito_id)
        conn.rollback()
        # El total sigue siendo devolvible de a partes, ni más ni menos: 5.
        ventas.devolver_items(conn, vid, {linea_a: 2.0, linea_b: 3.0}, deposito_id)
        conn.commit()
        assert ventas.obtener_venta(conn, vid)["estado"] == "devuelta"


def test_devolver_venta_pendiente_levanta(abrir_ventas):
    """🔴 Punto 3: una venta con el QR sin acreditar no tiene plata adentro
    todavía. Reintegrar algo ahí sería devolver dinero que nunca entró."""
    with abrir_ventas() as conn:
        pid = _producto(conn, existencia=10.0)
    vid = _venta(abrir_ventas, items=[
        {"nombre": "Yerba", "qty": 1, "precio": 100.0, "subtotal": 100.0, "producto_id": pid},
    ], pagos=[{"medio": "mercadopago", "monto": 100.0, "estado": "pendiente"}])
    with abrir_ventas() as conn:
        item_id_linea = conn.execute(
            "SELECT id FROM sale_items WHERE sale_id=?", (vid,)
        ).fetchone()["id"]
        deposito_id = conn.execute(
            "SELECT location_id FROM stock_movements WHERE source_id=?", (vid,)
        ).fetchone()["location_id"]
        assert ventas.obtener_venta(conn, vid)["estado"] == "pendiente"
        with pytest.raises(ValueError):
            ventas.devolver_items(conn, vid, {item_id_linea: 1.0}, deposito_id)


def test_devolver_venta_parcial_de_cobranza_levanta(abrir_ventas):
    """Cobrada a medias (no confundir con `devuelta_parcial`, que es de
    devolución): la plata que falta cobrar no está en el cajón."""
    with abrir_ventas() as conn:
        pid = _producto(conn, existencia=10.0)
    vid = _venta(abrir_ventas, items=[
        {"nombre": "Yerba", "qty": 2, "precio": 100.0, "subtotal": 200.0, "producto_id": pid},
    ], pagos=[{"medio": "efectivo", "monto": 100.0, "estado": "aprobado"}])  # cobra la mitad
    with abrir_ventas() as conn:
        item_id_linea = conn.execute(
            "SELECT id FROM sale_items WHERE sale_id=?", (vid,)
        ).fetchone()["id"]
        deposito_id = conn.execute(
            "SELECT location_id FROM stock_movements WHERE source_id=?", (vid,)
        ).fetchone()["location_id"]
        assert ventas.obtener_venta(conn, vid)["estado"] == "parcial"
        with pytest.raises(ValueError):
            ventas.devolver_items(conn, vid, {item_id_linea: 1.0}, deposito_id)


def test_acreditar_pago_qr_no_toca_una_venta_ya_devuelta(abrir_ventas):
    """🔴 Punto 4: con el punto 3 en pie, una venta con devolución no puede
    tener pagos pendientes (era `cobrada`, todo `aprobado`, para poder
    devolverse), así que `acreditar_pago_qr` no encuentra nada y no pisa el
    `status_detail` que dejó `devolver_items`. Se fija igual, explícito."""
    with abrir_ventas() as conn:
        pid = _producto(conn, existencia=10.0)
    vid = _venta(abrir_ventas, items=[
        {"nombre": "Yerba", "qty": 2, "precio": 100.0, "subtotal": 200.0, "producto_id": pid},
    ], pagos=[{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}])
    with abrir_ventas() as conn:
        item_id_linea = conn.execute(
            "SELECT id FROM sale_items WHERE sale_id=?", (vid,)
        ).fetchone()["id"]
        deposito_id = conn.execute(
            "SELECT location_id FROM stock_movements WHERE source_id=?", (vid,)
        ).fetchone()["location_id"]
        ventas.devolver_items(conn, vid, {item_id_linea: 1.0}, deposito_id)
        conn.commit()
        assert ventas.obtener_venta(conn, vid)["estado"] == "devuelta_parcial"
        assert ventas.acreditar_pago_qr(conn, vid, "999") is False
        assert ventas.obtener_venta(conn, vid)["estado"] == "devuelta_parcial"
        with pytest.raises(ventas.VentaConDevoluciones):
            ventas.anular_venta(conn, vid)
        conn.rollback()
        # No se tocó la caja de nuevo: sigue habiendo el ingreso y el egreso
        # de la devolución, nada más.
        assert len(_caja(conn, "V-00001")) == 2


def test_anular_venta_vieja_partially_returned_levanta(abrir_ventas):
    """El viejo `usecases.sales.return_sale_items` deja `sales.status`
    directo en `partially_returned` — no toca `status_detail`, que es de
    esta capa. La guarda lo tiene que ver igual."""
    vid = _venta(abrir_ventas)
    with abrir_ventas() as conn:
        conn.execute("UPDATE sales SET status='partially_returned' WHERE id=?", (vid,))
        conn.commit()
        with pytest.raises(ventas.VentaConDevoluciones):
            ventas.anular_venta(conn, vid)


def test_anular_venta_vieja_returned_levanta(abrir_ventas):
    vid = _venta(abrir_ventas)
    with abrir_ventas() as conn:
        conn.execute("UPDATE sales SET status='returned' WHERE id=?", (vid,))
        conn.commit()
        with pytest.raises(ventas.VentaConDevoluciones):
            ventas.anular_venta(conn, vid)


def test_anular_repone_fila_por_fila_no_agrupa_ni_con_receta(abrir_ventas):
    """🔑 mutación: si `anular_venta` volviera a agrupar por (ítem, variante,
    depósito) antes de reponer, dos platos que descuentan el MISMO insumo
    (Restolibra, vía `resolver_receta`) colapsarían en una sola fila de
    `anulacion` en vez de dos — le cambiaría el ledger a un producto que hoy
    no se toca."""
    with abrir_ventas() as conn:
        insumo_id = _producto(conn, nombre="Insumo", existencia=20.0)
        plato_a = _producto(conn, nombre="Plato A", existencia=0.0)
        plato_b = _producto(conn, nombre="Plato B", existencia=0.0)

    def receta(item_id, item):
        return [Insumo(item_id=insumo_id, cantidad=Decimal("1"))]

    ganchos = Hooks(resolver_receta=receta)
    vid = _venta(abrir_ventas, hooks=ganchos, items=[
        {"nombre": "Plato A", "qty": 2, "precio": 50.0, "subtotal": 100.0, "producto_id": plato_a},
        {"nombre": "Plato B", "qty": 3, "precio": 50.0, "subtotal": 150.0, "producto_id": plato_b},
    ], pagos=[{"medio": "efectivo", "monto": 250.0, "estado": "aprobado"}])
    with abrir_ventas() as conn:
        # Dos movimientos de VENTA sobre el MISMO insumo (uno por plato).
        n_venta = conn.execute(
            "SELECT COUNT(*) FROM stock_movements WHERE source_id=? AND reason_code='venta'", (vid,)
        ).fetchone()[0]
        assert n_venta == 2
        ventas.anular_venta(conn, vid)
        conn.commit()
        n_anulacion = conn.execute(
            "SELECT COUNT(*) FROM stock_movements WHERE source_id=? AND reason_code='anulacion'", (vid,)
        ).fetchone()[0]
        # Una reposición por CADA movimiento de venta, sin agrupar por insumo.
        assert n_anulacion == 2
        assert stock.get_stock_actual(conn, insumo_id) == 20.0  # 20 - 2 - 3 + 2 + 3


# ── F1: devolución parcial ──────────────────────────────────────────────


def test_devolver_items_reintegra_y_marca_parcial(abrir_ventas):
    with abrir_ventas() as conn:
        pid = _producto(conn, existencia=10.0)
    vid = _venta(abrir_ventas, items=[
        {"nombre": "Yerba", "qty": 4, "precio": 100.0, "subtotal": 400.0, "producto_id": pid},
    ], pagos=[{"medio": "efectivo", "monto": 400.0, "estado": "aprobado"}])
    with abrir_ventas() as conn:
        item_id_linea = conn.execute(
            "SELECT id FROM sale_items WHERE sale_id=?", (vid,)
        ).fetchone()["id"]
        deposito_id = conn.execute(
            "SELECT location_id FROM stock_movements WHERE source_id=?", (vid,)
        ).fetchone()["location_id"]
        resultado = ventas.devolver_items(
            conn, vid, {item_id_linea: 1.0}, deposito_id, usuario_id=USUARIO["id"]
        )
        conn.commit()
        assert resultado["importe"] == 100.0
        assert resultado["venta"]["estado"] == "devuelta_parcial"
        assert stock.get_stock_actual(conn, pid) == 7.0  # 10 - 4 + 1
        egreso = _caja(conn)[-1]
        assert egreso["tipo"] == "egreso" and float(egreso["monto"]) == 100.0


def test_devolver_items_completo_marca_devuelta_y_no_admite_otra(abrir_ventas):
    with abrir_ventas() as conn:
        pid = _producto(conn, existencia=10.0)
    vid = _venta(abrir_ventas, items=[
        {"nombre": "Yerba", "qty": 2, "precio": 100.0, "subtotal": 200.0, "producto_id": pid},
    ], pagos=[{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}])
    with abrir_ventas() as conn:
        item_id_linea = conn.execute(
            "SELECT id FROM sale_items WHERE sale_id=?", (vid,)
        ).fetchone()["id"]
        deposito_id = conn.execute(
            "SELECT location_id FROM stock_movements WHERE source_id=?", (vid,)
        ).fetchone()["location_id"]
        resultado = ventas.devolver_items(conn, vid, {item_id_linea: 2.0}, deposito_id)
        conn.commit()
        assert resultado["venta"]["estado"] == "devuelta"
        with pytest.raises(ValueError):
            ventas.devolver_items(conn, vid, {item_id_linea: 1.0}, deposito_id)


def test_devolver_items_topea_lo_ya_devuelto(abrir_ventas):
    """🔑 mutación (d): sin el chequeo de `disponible`, esto no levanta."""
    with abrir_ventas() as conn:
        pid = _producto(conn, existencia=10.0)
    vid = _venta(abrir_ventas, items=[
        {"nombre": "Yerba", "qty": 3, "precio": 100.0, "subtotal": 300.0, "producto_id": pid},
    ], pagos=[{"medio": "efectivo", "monto": 300.0, "estado": "aprobado"}])
    with abrir_ventas() as conn:
        item_id_linea = conn.execute(
            "SELECT id FROM sale_items WHERE sale_id=?", (vid,)
        ).fetchone()["id"]
        deposito_id = conn.execute(
            "SELECT location_id FROM stock_movements WHERE source_id=?", (vid,)
        ).fetchone()["location_id"]
        ventas.devolver_items(conn, vid, {item_id_linea: 2.0}, deposito_id)
        conn.commit()
        with pytest.raises(ValueError):
            # Quedaba 1 sin devolver; pide 2.
            ventas.devolver_items(conn, vid, {item_id_linea: 2.0}, deposito_id)


def test_devolver_a_cuenta_corriente_exige_cliente(abrir_ventas):
    with abrir_ventas() as conn:
        pid = _producto(conn, existencia=10.0)
    vid = _venta(abrir_ventas, items=[
        {"nombre": "Yerba", "qty": 1, "precio": 100.0, "subtotal": 100.0, "producto_id": pid},
    ], pagos=[{"medio": "efectivo", "monto": 100.0, "estado": "aprobado"}])
    with abrir_ventas() as conn:
        item_id_linea = conn.execute(
            "SELECT id FROM sale_items WHERE sale_id=?", (vid,)
        ).fetchone()["id"]
        deposito_id = conn.execute(
            "SELECT location_id FROM stock_movements WHERE source_id=?", (vid,)
        ).fetchone()["location_id"]
        with pytest.raises(ValueError):
            ventas.devolver_items(
                conn, vid, {item_id_linea: 1.0}, deposito_id, medio_pago="cuenta_corriente"
            )


# ── F4 (correcciones sobre la revisión): deposito_id también en devolver_items ──


def test_devolver_a_deposito_inexistente_da_depositoinexistente_sin_escribir_nada(abrir_ventas):
    """🔑 mutación: sin `_validar_deposito` en `devolver_items`, esto no
    rebota acá con un error de dominio — o revienta más abajo como
    `IntegrityError` (la FK de `stock_movements.location_id`), que ningún
    `except` de `web/ventas_router.py::devolver` atrapaba como 422 antes de
    este fix (hallazgo del 2026-09-15, tarea F4)."""
    with abrir_ventas() as conn:
        pid = _producto(conn, existencia=10.0)
    vid = _venta(abrir_ventas, items=[
        {"nombre": "Yerba", "qty": 4, "precio": 100.0, "subtotal": 400.0, "producto_id": pid},
    ], pagos=[{"medio": "efectivo", "monto": 400.0, "estado": "aprobado"}])
    with abrir_ventas() as conn:
        item_id_linea = conn.execute(
            "SELECT id FROM sale_items WHERE sale_id=?", (vid,)
        ).fetchone()["id"]
        n_stock_antes = conn.execute(
            "SELECT COUNT(*) FROM stock_movements WHERE source_id=?", (vid,)
        ).fetchone()[0]
        n_caja_antes = len(_caja(conn, "V-00001"))
        with pytest.raises(ventas.DepositoInexistente) as exc_info:
            ventas.devolver_items(conn, vid, {item_id_linea: 1.0}, 999999)
        assert "999999" in str(exc_info.value)
        # Nada quedó escrito: ni el stock, ni la caja, ni el estado de la venta.
        n_stock_despues = conn.execute(
            "SELECT COUNT(*) FROM stock_movements WHERE source_id=?", (vid,)
        ).fetchone()[0]
        assert n_stock_despues == n_stock_antes
        assert len(_caja(conn, "V-00001")) == n_caja_antes
        assert ventas.obtener_venta(conn, vid)["estado"] == "cobrada"
        assert stock.get_stock_actual(conn, pid) == 6.0


def test_devolver_a_deposito_inactivo_tambien_es_depositoinexistente(abrir_ventas):
    """"Válido/activo": mismo criterio que en `registrar_venta` — un depósito
    que existe pero está desactivado tampoco es un destino aceptable."""
    with abrir_ventas() as conn:
        pid = _producto(conn, existencia=10.0)
        sucursal_b = catalogo.create_deposito(conn, "Sucursal B")
        catalogo.update_deposito(conn, sucursal_b, "Sucursal B", "", 0)
        conn.commit()
    vid = _venta(abrir_ventas, items=[
        {"nombre": "Yerba", "qty": 1, "precio": 100.0, "subtotal": 100.0, "producto_id": pid},
    ], pagos=[{"medio": "efectivo", "monto": 100.0, "estado": "aprobado"}])
    with abrir_ventas() as conn:
        item_id_linea = conn.execute(
            "SELECT id FROM sale_items WHERE sale_id=?", (vid,)
        ).fetchone()["id"]
        with pytest.raises(ventas.DepositoInexistente):
            ventas.devolver_items(conn, vid, {item_id_linea: 1.0}, sucursal_b)


def test_devolver_linea_de_servicio_levanta(abrir_ventas):
    vid = _venta(
        abrir_ventas,
        items=[{"nombre": "Envío", "qty": 1, "precio": 50.0, "subtotal": 50.0, "producto_id": None}],
        pagos=[{"medio": "efectivo", "monto": 50.0, "estado": "aprobado"}],
        stock_habilitado=False,
    )
    with abrir_ventas() as conn:
        item_id_linea = conn.execute(
            "SELECT id FROM sale_items WHERE sale_id=?", (vid,)
        ).fetchone()["id"]
        with pytest.raises(ValueError):
            ventas.devolver_items(conn, vid, {item_id_linea: 1.0}, deposito_id=1)


def test_devolver_venta_anulada_levanta(abrir_ventas):
    with abrir_ventas() as conn:
        pid = _producto(conn, existencia=10.0)
    vid = _venta(abrir_ventas, items=[
        {"nombre": "Yerba", "qty": 1, "precio": 100.0, "subtotal": 100.0, "producto_id": pid},
    ], pagos=[{"medio": "efectivo", "monto": 100.0, "estado": "aprobado"}])
    with abrir_ventas() as conn:
        item_id_linea = conn.execute(
            "SELECT id FROM sale_items WHERE sale_id=?", (vid,)
        ).fetchone()["id"]
        deposito_id = conn.execute(
            "SELECT location_id FROM stock_movements WHERE source_id=?", (vid,)
        ).fetchone()["location_id"]
        ventas.anular_venta(conn, vid)
        conn.commit()
        with pytest.raises(ValueError):
            ventas.devolver_items(conn, vid, {item_id_linea: 1.0}, deposito_id)


def test_devolver_items_descuenta_lo_ya_devuelto_por_el_camino_viejo(abrir_ventas):
    """El viejo `return_sale_items` de VentaLibra escribía
    `source_type='sale_return'` con `reason_code` = la POSICIÓN de la línea,
    no un tipo. Una devolución nueva sobre la misma venta tiene que descontar
    lo que ya volvió por ese camino."""
    with abrir_ventas() as conn:
        pid = _producto(conn, existencia=10.0)
    vid = _venta(abrir_ventas, items=[
        {"nombre": "Yerba", "qty": 5, "precio": 100.0, "subtotal": 500.0, "producto_id": pid},
    ], pagos=[{"medio": "efectivo", "monto": 500.0, "estado": "aprobado"}])
    with abrir_ventas() as conn:
        item_id_linea = conn.execute(
            "SELECT id FROM sale_items WHERE sale_id=?", (vid,)
        ).fetchone()["id"]
        deposito_id = conn.execute(
            "SELECT location_id FROM stock_movements WHERE source_id=?", (vid,)
        ).fetchone()["location_id"]
        # 3 ya devueltas por el camino viejo.
        conn.execute(
            "INSERT INTO stock_movements (item_id, location_id, movement_type, quantity_delta, "
            "occurred_at, source_type, source_id, reason_code) VALUES (?,?,?,?,?,?,?,?)",
            (pid, deposito_id, "return", 3.0, HOY, "sale_return", vid, "0"),
        )
        conn.commit()
        with pytest.raises(ValueError):
            # Quedaban 2 sin devolver; pedir 3 excede el tope.
            ventas.devolver_items(conn, vid, {item_id_linea: 3.0}, deposito_id)
        ventas.devolver_items(conn, vid, {item_id_linea: 2.0}, deposito_id)
        conn.commit()
        assert ventas.obtener_venta(conn, vid)["estado"] == "devuelta"
