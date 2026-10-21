"""La caja y los turnos de un nodo offline: ganchos que encolan y aplicadores del central (ADR-069).

Misma estructura que `test_erp_offline.py`: DOS bases —el nodo y el central— y el `SyncReceiver` de LibraEdge de verdad
con el `Despachador` (`registrar_en`), en las tres combinaciones de motores (`puntas`). Los ids del nodo y los del central
se hacen **distintos a propósito** (el central ya tiene un turno y un movimiento propios de otro día) para que
una traducción que copie el id del nodo no pase por casualidad.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from conftest import USUARIO

# Las piezas de las dos bases son las mismas que las del test de las ventas.
from test_erp_offline import HOY, NODO, _pendientes, _stock, _subir
from test_erp_offline import puntas as _puntas_de_ventas  # noqa: F401

from libracommerce.erp import SIN_GANCHOS, offline, offline_caja, ventas

libraedge = pytest.importorskip("libraedge")

@pytest.fixture
def puntas(_puntas_de_ventas):  # noqa: F811
    """Las dos bases de `test_erp_offline.py` más las tablas del cierre diario, que el `init_db` de un producto crea
    con `cierre_diario.crear_tablas` (no están en `init_core_schema`)."""
    from libracore.db import cierre_diario

    for base in _puntas_de_ventas:
        with base.abrir() as conn:
            cierre_diario.crear_tablas(conn)
            conn.commit()
    return _puntas_de_ventas


OUTBOX = offline.Outbox(nodo=lambda: NODO, caja_con_turno=True)
OUTBOX_FUERA_DE_UN_NODO = offline.Outbox(nodo=lambda: None)


def _ganchos(outbox=OUTBOX, base=None):
    return offline_caja.caja_con_outbox(base, outbox)


def _hooks(turno_para=True):
    """Los ganchos de venta de un producto con turnos: la venta toma el turno abierto de su cajero."""
    from libracore.db.turnos import get_turno_activo

    base = replace(SIN_GANCHOS, turno_para=lambda conn, uid: get_turno_activo(uid, conn=conn)) if turno_para \
        else SIN_GANCHOS
    return offline.con_outbox(base, OUTBOX)


# ── Lo que hace una persona en la caja del nodo (con los ganchos, como los routers) ───────────────────


def _abrir_turno(base, ganchos, monto=50.0, notas="apertura"):
    from libracore.db import turnos

    with base.abrir() as conn:
        tid = turnos.create_turno(USUARIO["id"], monto, notas, 1, conn=conn, ganchos=ganchos)
        conn.commit()
    return tid


def _movimiento(base, ganchos, tid, tipo, monto, referencia="", medio="efectivo", factura_id=None):
    from libracore.db import caja

    with base.abrir() as conn:
        mid = caja.create_caja_movimiento(
            HOY, tipo, f"{tipo} a mano", monto, referencia, factura_id, USUARIO["id"], 1, medio, tid, conn=conn,
            ganchos=ganchos)
        conn.commit()
    return mid


def _anular(base, ganchos, mid):
    from libracore.db import caja

    with base.abrir() as conn:
        caja.anular_caja_movimiento(mid, conn=conn, ganchos=ganchos)
        conn.commit()


def _cerrar_turno(base, ganchos, tid, declarado):
    from libracore.db import turnos

    with base.abrir() as conn:
        turnos.cerrar_turno_caja(tid, declarado, "cierre", conn=conn, ganchos=ganchos)
        conn.commit()


def _cerrar_dia(base, ganchos, tid):
    from libracore.db import cierre_diario, turnos

    with base.abrir() as conn:
        fecha = turnos.get_turno(tid, conn=conn)["apertura"][:10]
        cierre = cierre_diario.cerrar_dia(USUARIO["id"], None, fecha, "fin del día", conn=conn, ganchos=ganchos)
        conn.commit()
    return cierre


def _vender_en_el_nodo(base, hooks, monto=200.0):
    pagos = [{"medio": "efectivo", "monto": monto, "estado": "aprobado"}]
    items = [{"nombre": "Yerba", "qty": 2.0, "precio": monto / 2, "subtotal": monto, "producto_id": 1}]
    return ventas.crear_venta_directa(
        base.fabrica(), fecha=HOY, items=items, subtotal=monto, descuento=0.0, total=monto, cliente_id=None,
        cliente_nombre="", usuario_id=USUARIO["id"], observaciones="", estado="cobrada", pagos=pagos,
        stock_habilitado=True, hooks=hooks, caja_con_turno=True,
    )


# ── El central con su propia caja, de otro día, para que los ids no coincidan ──────────────────────────


def _caja_propia_del_central(central, *, con_cierre=False):
    """Un turno cerrado de otro día y un movimiento propios (y, si se pide, un cierre diario): corre los ids del central."""
    from libracore.db import caja, cierre_diario, turnos

    with central.abrir() as conn:
        tid = turnos.create_turno(USUARIO["id"], 10.0, "", 1, conn=conn, apertura="2026-01-02 08:00:00")
        caja.create_caja_movimiento("2026-01-02", "ingreso", "propio", 5.0, "", None, USUARIO["id"], 1, "efectivo",
                                    tid, conn=conn)
        turnos.cerrar_turno_caja(tid, 15.0, "", conn=conn, cierre="2026-01-02 20:00:00")
        if con_cierre:
            cierre_diario.cerrar_dia(USUARIO["id"], None, "2026-01-02", "", conn=conn)
        conn.commit()
    return tid


def _filas(base, sql, params=()):
    with base.abrir() as conn:
        return [dict(f) for f in conn.execute(sql, params).fetchall()]


def _tipos(operaciones):
    return [o.operation_type for o in operaciones]


# ── El encolado en el nodo ─────────────────────────────────────────────────


def test_los_ganchos_encolan_en_la_misma_transaccion(puntas):
    """Si el cambio no se confirma, la operación tampoco queda; si se confirma, quedan las dos."""
    from libracore.db import turnos

    nodo, _ = puntas
    ganchos = _ganchos()
    with nodo.abrir() as conn:
        turnos.create_turno(USUARIO["id"], 50.0, "", 1, conn=conn, ganchos=ganchos)
        conn.rollback()
    assert _pendientes(nodo) == []
    assert _filas(nodo, "SELECT id FROM turnos_caja") == []

    tid = _abrir_turno(nodo, ganchos)
    (op,) = _pendientes(nodo)
    assert op.operation_type == "turno.abierto" and op.aggregate_type == "turno"
    assert op.aggregate_id == f"nodo-s02:turno:{tid}" and op.operation_id == "nodo-s02:1"
    assert op.schema_version == 1 and op.payload["version"] == 1
    assert op.payload["turno_id"] == tid and op.payload["monto_inicial"] == 50.0 and op.payload["caja_id"] == 1


def test_un_gancho_del_producto_que_falla_se_lleva_el_cambio_y_la_operacion(puntas):
    from libracore.ganchos_de_caja import GanchosDeCaja

    def falla(conn, datos):
        raise RuntimeError("no se puede abrir")

    nodo, _ = puntas
    with pytest.raises(RuntimeError, match="no se puede abrir"):
        _abrir_turno(nodo, _ganchos(base=GanchosDeCaja(al_abrir_turno=falla)))
    assert _pendientes(nodo) == []
    assert _filas(nodo, "SELECT id FROM turnos_caja") == []


def test_el_gancho_del_producto_corre_antes_que_el_encolado(puntas):
    from libracore.ganchos_de_caja import GanchosDeCaja

    visto = []
    nodo, _ = puntas
    _abrir_turno(nodo, _ganchos(base=GanchosDeCaja(al_abrir_turno=lambda conn, datos: visto.append(datos["turno_id"]))))
    assert visto == [1] and _tipos(_pendientes(nodo)) == ["turno.abierto"]


def test_fuera_de_un_nodo_no_encola_nada_y_corre_el_gancho_del_producto(puntas):
    from libracore.ganchos_de_caja import GanchosDeCaja

    visto = []
    nodo, _ = puntas
    ganchos = _ganchos(OUTBOX_FUERA_DE_UN_NODO, GanchosDeCaja(al_abrir_turno=lambda c, d: visto.append(d)))
    _abrir_turno(nodo, ganchos)
    assert len(visto) == 1 and _pendientes(nodo) == []


def test_los_movimientos_de_una_venta_no_suben_como_movimientos(puntas):
    """El central ya los reproduce al aplicar la venta: sólo suben los que anota una persona con los ganchos."""
    nodo, _ = puntas
    ganchos = _ganchos()
    tid = _abrir_turno(nodo, ganchos)
    _vender_en_el_nodo(nodo, _hooks())
    mid = _movimiento(nodo, ganchos, tid, "egreso", 10.0)
    assert _tipos(_pendientes(nodo)) == ["turno.abierto", "venta.registrada", "caja.movimiento"]
    assert _pendientes(nodo)[2].payload["movimiento_id"] == mid
    # La caja del nodo tiene los dos movimientos (el de la venta y el manual), pero sólo uno subió.
    assert len(_filas(nodo, "SELECT id FROM caja_movimientos")) == 2


# ── El recorrido completo, nodo → central ──────────────────────────────────


def _recorrido(nodo, central, declarado=268.0):
    """Abre un turno, anota un ingreso, vende, anota un egreso y otro que anula, cierra el turno y el día."""
    ganchos = _ganchos()
    tid = _abrir_turno(nodo, ganchos)
    ingreso = _movimiento(nodo, ganchos, tid, "ingreso", 30.0, "ING-1")
    vid = _vender_en_el_nodo(nodo, _hooks())
    egreso = _movimiento(nodo, ganchos, tid, "egreso", 10.0, "EGR-1")
    anulado = _movimiento(nodo, ganchos, tid, "egreso", 5.0)  # sin referencia: la idempotencia no puede ser la de ella
    _anular(nodo, ganchos, anulado)
    _cerrar_turno(nodo, ganchos, tid, declarado)
    cierre = _cerrar_dia(nodo, ganchos, tid)
    return {"turno": tid, "ingreso": ingreso, "venta": vid, "egreso": egreso, "anulado": anulado, "cierre": cierre}


def test_el_recorrido_completo_sube_en_orden_y_el_central_queda_igual(puntas):
    nodo, central = puntas
    _caja_propia_del_central(central)
    hecho = _recorrido(nodo, central)

    ops = _pendientes(nodo)
    assert _tipos(ops) == ["turno.abierto", "caja.movimiento", "venta.registrada", "caja.movimiento", "caja.movimiento",
                           "caja.movimiento_anulado", "turno.cerrado", "dia.cerrado"]
    assert [o.sequence for o in ops] == list(range(1, 9))  # una sola secuencia por nodo, sin huecos
    assert ops[2].payload["turno_id_nodo"] == hecho["turno"] and ops[2].payload["caja_con_turno"] is True

    assert [r.result for r in _subir(central, ops)] == ["accepted"] * 8

    (turno,) = _filas(central, "SELECT * FROM turnos_caja WHERE origen=?", ("nodo-s02",))
    assert turno["origen_id"] == hecho["turno"] and turno["id"] != hecho["turno"]  # id propio del central
    nodo_turno = _filas(nodo, "SELECT * FROM turnos_caja")[0]
    assert (turno["apertura"], turno["cierre"], turno["estado"]) == (nodo_turno["apertura"], nodo_turno["cierre"],
                                                                     "cerrado")
    assert float(turno["monto_esperado_cierre"]) == float(nodo_turno["monto_esperado_cierre"]) == 270.0
    assert float(turno["monto_declarado_cierre"]) == 268.0 and turno["caja_id"] == 1

    movs = _filas(central, "SELECT * FROM caja_movimientos WHERE origen=? ORDER BY origen_id", ("nodo-s02",))
    assert [(m["origen_id"], m["tipo"], float(m["monto"]), m["anulado"], m["turno_id"]) for m in movs] == [
        (hecho["ingreso"], "ingreso", 30.0, 0, turno["id"]),
        (hecho["egreso"], "egreso", 10.0, 0, turno["id"]),
        (hecho["anulado"], "egreso", 5.0, 1, turno["id"]),
    ]
    # Sin duplicados: los 3 manuales + el cobro de la venta (que reprodujo `venta.registrada`) + el propio del central.
    assert len(_filas(central, "SELECT id FROM caja_movimientos")) == 5
    assert len(_filas(central, "SELECT id FROM caja_movimientos WHERE concepto LIKE '%S02-V-00001%'")) == 1

    # La venta quedó en el turno del central, y su cobro también (la caja de la venta lleva el turno).
    with central.abrir() as conn:
        (vid,) = [f[0] for f in conn.execute("SELECT id FROM sales WHERE source_id=?", (hecho["venta"],)).fetchall()]
        assert ventas.obtener_venta(conn, vid)["turno_id"] == turno["id"]
    cobro = _filas(central, "SELECT turno_id, caja_id FROM caja_movimientos WHERE concepto LIKE '%S02-V-00001%'")
    assert cobro == [{"turno_id": turno["id"], "caja_id": 1}]

    (cierre,) = _filas(central, "SELECT * FROM cierres_diarios WHERE origen=?", ("nodo-s02",))
    assert cierre["origen_id"] == hecho["cierre"]["id"] and cierre["fecha"] == nodo_turno["apertura"][:10]
    assert float(cierre["monto_esperado_total"]) == 270.0 and float(cierre["monto_declarado_total"]) == 268.0
    assert _stock(central) == _stock(nodo) == 8.0

    # Todo coincide: el central recalcula 270 y no hay nada que revisar.
    with central.abrir() as conn:
        assert offline_caja.arqueos_a_revisar(conn) == []


def test_aplicar_dos_veces_la_misma_operacion_es_una_fila(puntas):
    nodo, central = puntas
    _recorrido(nodo, central)
    ops = _pendientes(nodo)
    assert [r.result for r in _subir(central, ops)] == ["accepted"] * 8
    antes = {t: len(_filas(central, f"SELECT id FROM {t}")) for t in ("turnos_caja", "caja_movimientos",
                                                                       "cierres_diarios")}
    assert [r.result for r in _subir(central, ops)] == ["duplicate"] * 8  # el receptor, por `operation_id`

    # Y los aplicadores solos, sin el receptor: idempotentes por `(origen, origen_id)`.
    aplicadores = offline_caja.aplicadores_de_caja()
    with central.abrir() as conn:
        for op in ops:
            if op.operation_type in aplicadores:
                aplicadores[op.operation_type](conn, op)
        conn.commit()
    assert {t: len(_filas(central, f"SELECT id FROM {t}")) for t in antes} == antes


def test_la_venta_se_aplica_al_turno_del_central_o_sin_turno_si_no_subio(puntas):
    nodo, central = puntas
    ganchos = _ganchos()
    tid = _abrir_turno(nodo, ganchos)
    _vender_en_el_nodo(nodo, _hooks(), 100.0)
    abierto, venta = _pendientes(nodo)

    # Subió sólo la venta: su turno no está en el central; la venta no se rechaza, se aplica sin turno (como antes).
    assert [r.result for r in _subir(central, [venta])] == ["accepted"]
    assert _filas(central, "SELECT turno_id FROM venta_links") == []
    assert _filas(central, "SELECT turno_id FROM caja_movimientos") == [{"turno_id": None}]
    assert venta.payload["turno_id_nodo"] == tid


def test_la_venta_sin_turno_en_el_nodo_sigue_igual(puntas):
    nodo, central = puntas
    _vender_en_el_nodo(nodo, _hooks(turno_para=False), 100.0)
    (venta,) = _pendientes(nodo)
    assert venta.payload["turno_id_nodo"] is None
    assert [r.result for r in _subir(central, [venta])] == ["accepted"]
    assert _filas(central, "SELECT turno_id FROM venta_links") == []


def test_sin_caja_con_turno_la_caja_de_la_venta_va_sin_turno_pero_la_venta_queda_en_el_turno(puntas):
    nodo, central = puntas
    ganchos = _ganchos()
    _abrir_turno(nodo, ganchos)
    from libracore.db.turnos import get_turno_activo

    hooks = offline.con_outbox(replace(SIN_GANCHOS, turno_para=lambda c, uid: get_turno_activo(uid, conn=c)),
                               offline.Outbox(nodo=lambda: NODO))
    pagos = [{"medio": "efectivo", "monto": 100.0, "estado": "aprobado"}]
    items = [{"nombre": "Yerba", "qty": 1.0, "precio": 100.0, "subtotal": 100.0, "producto_id": 1}]
    ventas.crear_venta_directa(
        nodo.fabrica(), fecha=HOY, items=items, subtotal=100.0, descuento=0.0, total=100.0, cliente_id=None,
        cliente_nombre="", usuario_id=USUARIO["id"], observaciones="", estado="cobrada", pagos=pagos,
        stock_habilitado=True, hooks=hooks)
    ops = _pendientes(nodo)
    assert ops[1].payload["caja_con_turno"] is False
    assert [r.result for r in _subir(central, ops)] == ["accepted", "accepted"]
    (turno,) = _filas(central, "SELECT id FROM turnos_caja")
    assert _filas(central, "SELECT turno_id FROM venta_links") == [{"turno_id": turno["id"]}]
    assert _filas(central, "SELECT turno_id FROM caja_movimientos") == [{"turno_id": None}]


def test_el_pago_acreditado_despues_lleva_el_turno_de_la_venta(puntas):
    nodo, central = puntas
    ganchos = _ganchos()
    _abrir_turno(nodo, ganchos)
    hooks = _hooks()
    pagos = [{"medio": "efectivo", "monto": 100.0, "estado": "aprobado"},
             {"medio": "mercadopago_qr", "monto": 100.0, "estado": "pendiente"}]
    items = [{"nombre": "Yerba", "qty": 2.0, "precio": 100.0, "subtotal": 200.0, "producto_id": 1}]
    vid = ventas.crear_venta_directa(
        nodo.fabrica(), fecha=HOY, items=items, subtotal=200.0, descuento=0.0, total=200.0, cliente_id=None,
        cliente_nombre="", usuario_id=USUARIO["id"], observaciones="", estado="parcial", pagos=pagos,
        stock_habilitado=True, hooks=hooks, caja_con_turno=True)
    with nodo.abrir() as conn:
        assert ventas.acreditar_pago_qr(conn, vid, "MP-1", hooks=hooks, caja_con_turno=True) is True
        conn.commit()
    ops = _pendientes(nodo)
    assert _tipos(ops) == ["turno.abierto", "venta.registrada", "venta.pago_acreditado"]
    assert ops[2].payload["caja_con_turno"] is True
    assert [r.result for r in _subir(central, ops)] == ["accepted"] * 3
    (turno,) = _filas(central, "SELECT id FROM turnos_caja")
    assert _filas(central, "SELECT turno_id FROM caja_movimientos ORDER BY id") == [
        {"turno_id": turno["id"]}, {"turno_id": turno["id"]}]


# ── Lo que falta o choca en el central: rechazo, no reintento ───────────────


def test_un_movimiento_cuyo_turno_no_subio_se_rechaza_y_no_deja_nada(puntas):
    nodo, central = puntas
    ganchos = _ganchos()
    tid = _abrir_turno(nodo, ganchos)
    _movimiento(nodo, ganchos, tid, "ingreso", 30.0)
    _, movimiento = _pendientes(nodo)

    (resultado,) = _subir(central, [movimiento])
    assert resultado.result == "rejected" and "turno" in resultado.error and "no está en el central" in resultado.error
    assert _filas(central, "SELECT id FROM caja_movimientos") == []
    with pytest.raises(offline_caja.TurnoNoSincronizado):
        with central.abrir() as conn:
            offline_caja.aplicar_caja_movimiento(conn, movimiento)


def test_la_anulacion_de_un_movimiento_que_no_subio_se_rechaza(puntas):
    nodo, central = puntas
    ganchos = _ganchos()
    tid = _abrir_turno(nodo, ganchos)
    mid = _movimiento(nodo, ganchos, tid, "egreso", 5.0)
    _anular(nodo, ganchos, mid)
    anulacion = _pendientes(nodo)[2]
    (resultado,) = _subir(central, [anulacion])
    assert resultado.result == "rejected" and "no está en el central" in resultado.error


def test_el_cierre_de_un_turno_que_no_subio_lo_crea_con_los_datos_de_la_apertura(puntas):
    """`turno.cerrado` trae todo lo de la apertura: un turno abierto antes de actualizar el nodo se cierra igual."""
    nodo, central = puntas
    ganchos = _ganchos()
    tid = _abrir_turno(nodo, ganchos)
    _cerrar_turno(nodo, ganchos, tid, 50.0)
    cerrado = _pendientes(nodo)[1]
    assert [r.result for r in _subir(central, [cerrado])] == ["accepted"]
    (turno,) = _filas(central, "SELECT * FROM turnos_caja")
    assert (turno["origen"], turno["origen_id"], turno["estado"]) == ("nodo-s02", tid, "cerrado")
    assert turno["apertura"] == _filas(nodo, "SELECT apertura FROM turnos_caja")[0]["apertura"]


def test_el_dia_cerrado_con_un_turno_que_no_subio_se_rechaza(puntas):
    nodo, central = puntas
    ganchos = _ganchos()
    tid = _abrir_turno(nodo, ganchos)
    _cerrar_turno(nodo, ganchos, tid, 50.0)
    _cerrar_dia(nodo, ganchos, tid)
    dia = _pendientes(nodo)[2]
    (resultado,) = _subir(central, [dia])
    assert resultado.result == "rejected" and "no está en el central" in resultado.error
    assert _filas(central, "SELECT id FROM cierres_diarios") == []


def test_el_dia_cerrado_con_un_turno_abierto_en_el_central_se_rechaza(puntas):
    nodo, central = puntas
    ganchos = _ganchos()
    tid = _abrir_turno(nodo, ganchos)
    _cerrar_turno(nodo, ganchos, tid, 50.0)
    _cerrar_dia(nodo, ganchos, tid)
    abierto, cerrado, dia = _pendientes(nodo)
    # El turno del nodo subió pero el central todavía lo tiene abierto, y además otro turno del día sigue abierto.
    assert [r.result for r in _subir(central, [abierto])] == ["accepted"]
    from libracore.db import turnos
    with central.abrir() as conn:
        turnos.create_turno(USUARIO["id"], 1.0, "", 1, conn=conn)
        conn.commit()
    (resultado,) = _subir(central, [cerrado, dia])[1:]
    assert resultado.result == "rejected"


def test_abrir_un_turno_en_un_dia_ya_cerrado_del_central_se_rechaza(puntas):
    """El motor levanta `RuntimeError`, que el receptor leería como falla de transporte y trabaría la cola: se traduce."""
    nodo, central = puntas
    tid = _abrir_turno(nodo, _ganchos())
    (abierto,) = _pendientes(nodo)
    from libracore.db import cierre_diario
    with central.abrir() as conn:
        cierre_diario.cerrar_dia(USUARIO["id"], None, abierto.payload["apertura"][:10], "", conn=conn)
        conn.commit()
    (resultado,) = _subir(central, [abierto])
    assert resultado.result == "rejected" and "ya está cerrado" in resultado.error
    assert tid == abierto.payload["turno_id"] and _filas(central, "SELECT id FROM turnos_caja") == []


def test_la_caja_que_no_existe_en_el_central_se_rechaza(puntas):
    nodo, central = puntas
    _abrir_turno(nodo, _ganchos())
    (abierto,) = _pendientes(nodo)
    otra = replace(abierto, payload={**abierto.payload, "caja_id": 99})
    (resultado,) = _subir(central, [otra])
    assert resultado.result == "rejected" and "caja 99" in resultado.error


def test_una_referencia_que_el_central_ya_tiene_se_rechaza_en_vez_de_perder_el_movimiento(puntas):
    from libracore.db import caja

    nodo, central = puntas
    ganchos = _ganchos()
    tid = _abrir_turno(nodo, ganchos)
    _movimiento(nodo, ganchos, tid, "ingreso", 30.0, "REF-REPETIDA")
    abierto, movimiento = _pendientes(nodo)
    with central.abrir() as conn:
        caja.create_caja_movimiento(HOY, "ingreso", "otro", 1.0, "REF-REPETIDA", None, USUARIO["id"], 1, "efectivo",
                                    None, conn=conn)
        conn.commit()
    resultados = _subir(central, [abierto, movimiento])
    assert [r.result for r in resultados] == ["accepted", "rejected"]
    assert "ya tiene en otro movimiento" in resultados[1].error


def test_factura_y_pago_de_cc_del_nodo_no_se_copian_al_central(puntas):
    """Son ids de otra base: copiarlos ataría el movimiento a otra factura."""
    nodo, central = puntas
    ganchos = _ganchos()
    tid = _abrir_turno(nodo, ganchos)
    _movimiento(nodo, ganchos, tid, "egreso", 3.0)
    abierto, movimiento = _pendientes(nodo)
    con_ids = replace(movimiento, payload={**movimiento.payload, "factura_id": 999, "cc_pago_id": 888})
    assert [r.result for r in _subir(central, [abierto, con_ids])] == ["accepted", "accepted"]
    (m,) = _filas(central, "SELECT factura_id, cc_pago_id FROM caja_movimientos")
    assert m == {"factura_id": None, "cc_pago_id": None}


# ── El arqueo del nodo manda; el central lo marca ───────────────────────────


def test_un_arqueo_distinto_del_recalculado_se_guarda_y_se_marca_sin_rechazar(puntas):
    """El ingreso del nodo no subió (quedó en revisión): el central recalcula 50 y el nodo esperaba 80."""
    nodo, central = puntas
    ganchos = _ganchos()
    tid = _abrir_turno(nodo, ganchos)
    _movimiento(nodo, ganchos, tid, "ingreso", 30.0)
    _cerrar_turno(nodo, ganchos, tid, 80.0)
    abierto, _ingreso, cerrado = _pendientes(nodo)

    assert [r.result for r in _subir(central, [abierto, cerrado])] == ["accepted", "accepted"]
    (turno,) = _filas(central, "SELECT * FROM turnos_caja")
    assert float(turno["monto_esperado_cierre"]) == 80.0 and float(turno["monto_declarado_cierre"]) == 80.0
    assert turno["estado"] == "cerrado" and offline_caja.MARCA_ARQUEO in turno["notas"]
    assert "esperaba 80.00" in turno["notas"] and "recalcula 50.00" in turno["notas"]
    with central.abrir() as conn:
        (aviso,) = offline_caja.arqueos_a_revisar(conn)
    assert (aviso["tabla"], aviso["id"], aviso["origen_id"]) == ("turnos_caja", turno["id"], tid)


def test_un_cierre_diario_con_otro_numero_no_se_marca(puntas):
    """Cada base numera sus cierres: si el central ya cerraba días antes del nodo, el número difiere y no es un problema."""
    nodo, central = puntas
    _caja_propia_del_central(central, con_cierre=True)  # el central ya tiene su cierre número 1 de otro día
    ganchos = _ganchos()
    tid = _abrir_turno(nodo, ganchos)
    _cerrar_turno(nodo, ganchos, tid, 50.0)
    _cerrar_dia(nodo, ganchos, tid)
    assert [r.result for r in _subir(central, _pendientes(nodo))] == ["accepted"] * 3
    (cierre,) = _filas(central, "SELECT * FROM cierres_diarios WHERE origen IS NOT NULL")
    assert cierre["numero"] == 2 and offline_caja.MARCA_ARQUEO not in (cierre["notas"] or "")
    with central.abrir() as conn:
        assert offline_caja.arqueos_a_revisar(conn) == []


def test_un_cierre_diario_con_otros_totales_se_marca(puntas):
    """El central tiene un turno propio ese mismo día en la sucursal: su foto suma más que la del nodo."""
    from libracore.db import turnos

    nodo, central = puntas
    ganchos = _ganchos()
    tid = _abrir_turno(nodo, ganchos)
    _cerrar_turno(nodo, ganchos, tid, 50.0)
    _cerrar_dia(nodo, ganchos, tid)
    with central.abrir() as conn:
        propio = turnos.create_turno(USUARIO["id"], 7.0, "", 1, conn=conn)
        turnos.cerrar_turno_caja(propio, 7.0, "", conn=conn)
        conn.commit()
    assert [r.result for r in _subir(central, _pendientes(nodo))] == ["accepted"] * 3
    (cierre,) = _filas(central, "SELECT * FROM cierres_diarios WHERE origen IS NOT NULL")
    assert float(cierre["monto_esperado_total"]) == 57.0 and "monto_esperado_total 57.00" in cierre["notas"]
    with central.abrir() as conn:
        assert [a["tabla"] for a in offline_caja.arqueos_a_revisar(conn)] == ["cierres_diarios"]


# ── Piezas puras ───────────────────────────────────────────────────────────


def test_registrar_en_registra_los_diez_tipos():
    from libraedge.sync.despacho import Despachador

    despachador = offline.registrar_en(Despachador())
    assert despachador.tipos() == tuple(sorted((*offline.TIPOS, *offline_caja.TIPOS_DE_CAJA)))
    assert len(despachador) == 10


def test_una_version_de_datos_desconocida_se_rechaza():
    from libraedge.domain.sync import OutboxOperation

    op = OutboxOperation(operation_id="n:1", node_id="n", sequence=1, operation_type="turno.abierto",
                         aggregate_type="turno", aggregate_id="n:turno:1", occurred_at="x", schema_version=1,
                         payload={"version": 2})
    with pytest.raises(ValueError, match="versión 2"):
        offline_caja.aplicar_turno_abierto(None, op)


def test_un_error_de_caja_es_un_value_error():
    """Es lo que hace que el receptor mande la operación a revisión y el nodo no trabe la cola (ADR-020)."""
    for clase in (offline_caja.TurnoNoSincronizado, offline_caja.MovimientoNoSincronizado,
                  offline_caja.CajaNoSincronizada, offline_caja.CajaRechazadaPorElCentral):
        assert issubclass(clase, ValueError)


def test_el_cierre_de_turno_de_erp_ventas_sube_desde_el_nodo(puntas):
    """`erp.ventas.cerrar_turno` (el de ContaLibra y RestoLibra, que arquea contra las ventas) corre `al_cerrar_turno`
    con los ganchos (v0.69.1): sin eso el cierre del turno no subía."""
    from libracommerce.erp import ventas

    nodo, _ = puntas
    ganchos = _ganchos()
    tid = _abrir_turno(nodo, ganchos)
    with nodo.abrir() as conn:
        assert ventas.cerrar_turno(conn, tid, 50.0, "fin", ganchos=ganchos) is True
        conn.commit()
    assert [op.operation_type for op in _pendientes(nodo)] == ["turno.abierto", "turno.cerrado"]
    with nodo.abrir() as conn:
        assert ventas.cerrar_turno(conn, 999999, 1.0, ganchos=ganchos) is False
        conn.rollback()
