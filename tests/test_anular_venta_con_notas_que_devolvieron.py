"""Anular una venta cuya nota de crédito ya devolvió la plata, o la dejó a favor del cliente (ADR-064).

🔴 Desde libracore 1.176 (ADR-065 del motor) la nota de una factura **al contado** cobrada mueve la plata al
emitirse: «devolver» saca un egreso de caja y «dejar a favor» abona la cuenta corriente del cliente, los dos con la
marca `nc:factura:<id>:<nota>`. `anular_venta` revertía además cada pago entero: **la plata salía dos veces**.

Los movimientos se siembran con `reintegrar_devolucion`, que es lo que escribe `libracore.reintegro_de_nota.aplicar`.
Con un libracore anterior (sin `devuelto_por_notas`) estos tests se saltean: ahí las notas de una factura al contado no
movían plata y no hay nada que restar.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from conftest import USUARIO
from test_anular_venta_con_factura import HOY, _cliente, _factura, _nota, _venta

from libracommerce.erp import ventas

try:
    from libracore.notas_de_credito import devuelto_por_notas  # noqa: F401
    _HAY_176 = True
except ImportError:  # libracore < 1.176
    _HAY_176 = False

requiere_176 = pytest.mark.skipif(
    not _HAY_176, reason="libracore < 1.176: las notas de una factura al contado no movían plata")

EFECTIVO = {"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}


def _nota_que_movio(conn, fid, *, total, numero, medio=None, cliente_id=None):
    """Una nota con CAE que, al emitirse, devolvió `total` por `medio` o lo dejó a favor de `cliente_id`."""
    from libracore.db.reversiones import MEDIO_CUENTA_CORRIENTE, reintegrar_devolucion
    from libracore.notas_de_credito import referencia_cc_de_nota

    _nota(conn, total=total, numero=numero)
    nota_id = conn.execute("SELECT MAX(id) FROM facturas").fetchone()[0]
    reintegrar_devolucion(
        venta_id=fid, numero=f"F{fid}", fecha=HOY, monto=float(total),
        medio_pago=medio or MEDIO_CUENTA_CORRIENTE, referencia=referencia_cc_de_nota(fid, nota_id),
        cliente_id=cliente_id, usuario_id=USUARIO["id"], conn=conn, factura_id=fid,
        concepto="Devolución de nota" if medio else "Saldo a favor de nota",
    )
    conn.commit()
    return nota_id


def _egresos(conn):
    """`(referencia sin id, medio, monto)` de cada egreso de caja, en orden."""
    return [
        (f[0].split(":")[0], f[1], float(f[2])) for f in conn.execute(
            "SELECT referencia, medio_pago, monto FROM caja_movimientos WHERE tipo='egreso' ORDER BY id").fetchall()
    ]


def _anulaciones(conn):
    return [(m, monto) for ref, m, monto in _egresos(conn) if ref == "anulacion"]


# ── La nota devolvió por caja ──────────────────────────────────────────────

@requiere_176
def test_nota_total_que_devolvio_la_anulacion_no_saca_la_plata_otra_vez(abrir_ventas):
    vid = _venta(abrir_ventas, pagos=[EFECTIVO])
    with abrir_ventas() as conn:
        fid = _factura(conn, vid)
        _nota_que_movio(conn, fid, total=200, numero=1, medio="efectivo")
        assert ventas.anular_venta(conn, vid, usuario_id=USUARIO["id"]) is True
        conn.commit()
        assert _egresos(conn) == [("nc", "efectivo", 200.0)], "sale UNA vez: la de la nota"
        assert ventas.obtener_venta(conn, vid)["status"] == "cancelled"


@requiere_176
def test_nota_parcial_que_devolvio_la_anulacion_saca_solo_el_resto(abrir_ventas):
    vid = _venta(abrir_ventas, pagos=[EFECTIVO])
    with abrir_ventas() as conn:
        fid = _factura(conn, vid)
        _nota_que_movio(conn, fid, total=80, numero=1, medio="efectivo")
        _nota(conn, total=120, numero=2)  # la otra parcial no movió plata (sin destino)
        assert ventas.anular_venta(conn, vid, usuario_id=USUARIO["id"]) is True
        conn.commit()
        assert _egresos(conn) == [("nc", "efectivo", 80.0), ("anulacion", "efectivo", 120.0)]


@requiere_176
def test_varias_notas_que_devolvieron_se_restan_todas(abrir_ventas):
    vid = _venta(abrir_ventas, pagos=[EFECTIVO])
    with abrir_ventas() as conn:
        fid = _factura(conn, vid)
        _nota_que_movio(conn, fid, total=50, numero=1, medio="efectivo")
        _nota_que_movio(conn, fid, total=100, numero=2, medio="efectivo")
        _nota(conn, total=50, numero=3)
        ventas.anular_venta(conn, vid, usuario_id=USUARIO["id"])
        conn.commit()
        assert _anulaciones(conn) == [("efectivo", 50.0)]


@requiere_176
def test_lo_que_no_cupo_en_su_medio_sale_de_los_otros_pagos(abrir_ventas):
    """Pagó 100 en efectivo y 100 con tarjeta; la nota devolvió los 200 por tarjeta: los 100 que no caben en la tarjeta
    salen del efectivo. No queda nada por revertir."""
    vid = _venta(abrir_ventas, pagos=[
        {"medio": "efectivo", "monto": 100.0, "estado": "aprobado"},
        {"medio": "tarjeta_debito", "monto": 100.0, "estado": "aprobado"},
    ])
    with abrir_ventas() as conn:
        fid = _factura(conn, vid)
        _nota_que_movio(conn, fid, total=200, numero=1, medio="tarjeta_debito")
        ventas.anular_venta(conn, vid, usuario_id=USUARIO["id"])
        conn.commit()
        assert _anulaciones(conn) == []


@requiere_176
def test_lo_devuelto_se_descuenta_del_pago_del_mismo_medio_y_no_de_otro(abrir_ventas):
    """La nota devolvió 100 por tarjeta: el efectivo sigue en el cajón y es lo único que la anulación saca (no se
    descuenta del efectivo sólo por haberse cobrado primero)."""
    vid = _venta(abrir_ventas, pagos=[
        {"medio": "efectivo", "monto": 100.0, "estado": "aprobado"},
        {"medio": "tarjeta_debito", "monto": 100.0, "estado": "aprobado"},
    ])
    with abrir_ventas() as conn:
        fid = _factura(conn, vid)
        _nota_que_movio(conn, fid, total=100, numero=1, medio="tarjeta_debito")
        _nota(conn, total=100, numero=2)
        ventas.anular_venta(conn, vid, usuario_id=USUARIO["id"])
        conn.commit()
        assert _anulaciones(conn) == [("efectivo", 100.0)]


@requiere_176
def test_un_egreso_de_la_nota_anulado_no_cuenta(abrir_ventas):
    """Si el movimiento de la nota se anuló, esa plata volvió a la caja: la anulación de la venta la saca como siempre."""
    vid = _venta(abrir_ventas, pagos=[EFECTIVO])
    with abrir_ventas() as conn:
        fid = _factura(conn, vid)
        _nota_que_movio(conn, fid, total=200, numero=1, medio="efectivo")
        conn.execute("UPDATE caja_movimientos SET anulado=1 WHERE referencia LIKE 'nc:factura:%'")
        conn.commit()
        ventas.anular_venta(conn, vid, usuario_id=USUARIO["id"])
        conn.commit()
        assert _anulaciones(conn) == [("efectivo", 200.0)]


# ── La nota dejó a favor del cliente ───────────────────────────────────────

@requiere_176
def test_nota_que_dejo_a_favor_la_anulacion_no_revierte_la_caja_otra_vez(abrir_ventas):
    with abrir_ventas() as conn:
        cid = _cliente(conn)
    vid = _venta(abrir_ventas, pagos=[EFECTIVO], cliente_id=cid)
    with abrir_ventas() as conn:
        fid = _factura(conn, vid)
        _nota_que_movio(conn, fid, total=200, numero=1, cliente_id=cid)
        ventas.anular_venta(conn, vid, usuario_id=USUARIO["id"])
        conn.commit()
        assert _anulaciones(conn) == [], "el cliente ya tiene la plata en su cuenta: no se le devuelve en caja también"
        abonos = conn.execute("SELECT monto FROM cc_pagos WHERE cliente_id=?", (cid,)).fetchall()
        assert [float(a["monto"]) for a in abonos] == [200.0], "UN solo abono: el de la nota"


@requiere_176
def test_a_favor_parcial_la_anulacion_saca_solo_el_resto(abrir_ventas):
    with abrir_ventas() as conn:
        cid = _cliente(conn)
    vid = _venta(abrir_ventas, pagos=[EFECTIVO], cliente_id=cid)
    with abrir_ventas() as conn:
        fid = _factura(conn, vid)
        _nota_que_movio(conn, fid, total=75, numero=1, cliente_id=cid)
        _nota(conn, total=125, numero=2)
        ventas.anular_venta(conn, vid, usuario_id=USUARIO["id"])
        conn.commit()
        assert _anulaciones(conn) == [("efectivo", 125.0)]


@requiere_176
def test_a_favor_sobre_lo_cobrado_en_caja_no_tapa_el_fiado_de_la_misma_venta(abrir_ventas):
    """Pagó 120 en efectivo y 80 quedaron fiados; la nota dejó a favor los 120 que entraron. La anulación no saca esos
    120 de la caja, pero los 80 de deuda sí se acreditan: el abono de la nota no es por ellos."""
    with abrir_ventas() as conn:
        cid = _cliente(conn)
    vid = _venta(abrir_ventas, cliente_id=cid, pagos=[
        {"medio": "efectivo", "monto": 120.0, "estado": "aprobado"},
        {"medio": "cuenta_corriente", "monto": 80.0, "estado": "aprobado"},
    ])
    with abrir_ventas() as conn:
        fid = _factura(conn, vid)
        _nota_que_movio(conn, fid, total=120, numero=1, cliente_id=cid)
        _nota(conn, total=80, numero=2)
        ventas.anular_venta(conn, vid, usuario_id=USUARIO["id"])
        conn.commit()
        assert [m for m, _ in _anulaciones(conn)] == ["cuenta_corriente"], "el efectivo no se revierte otra vez"
        abonos = conn.execute("SELECT monto FROM cc_pagos WHERE cliente_id=? ORDER BY id", (cid,)).fetchall()
        assert [float(a["monto"]) for a in abonos] == [120.0, 80.0], "la deuda fiada sí se acredita"


# ── Lo que no cambia ───────────────────────────────────────────────────────

@requiere_176
def test_sin_notas_que_hayan_movido_plata_la_anulacion_revierte_todo(abrir_ventas):
    """Una nota sin destino (no movió plata) no resta nada: la caja se revierte entera, como siempre."""
    vid = _venta(abrir_ventas, pagos=[EFECTIVO])
    with abrir_ventas() as conn:
        _factura(conn, vid)
        _nota(conn)
        ventas.anular_venta(conn, vid, usuario_id=USUARIO["id"])
        conn.commit()
        assert _egresos(conn) == [("anulacion", "efectivo", 200.0)]


@requiere_176
def test_el_reintegro_de_la_nota_de_otra_factura_no_cuenta(abrir_ventas):
    vid = _venta(abrir_ventas, pagos=[EFECTIVO])
    otra = _venta(abrir_ventas, pagos=[EFECTIVO])
    with abrir_ventas() as conn:
        _factura(conn, vid, numero=11)
        fid_otra = _factura(conn, otra, numero=12)
        _nota_que_movio(conn, fid_otra, total=200, numero=1, medio="efectivo")
        _nota(conn, tipo_original=11, numero_original=11, numero=2)
        ventas.anular_venta(conn, vid, usuario_id=USUARIO["id"])
        conn.commit()
        assert _anulaciones(conn) == [("efectivo", 200.0)]


@requiere_176
def test_con_un_libracore_sin_devuelto_por_notas_la_anulacion_es_la_de_siempre(abrir_ventas, monkeypatch):
    """libracore < 1.176: `devuelto_por_notas` no existe. El import es tolerante y no se resta de caja."""
    from libracore import notas_de_credito

    vid = _venta(abrir_ventas, pagos=[EFECTIVO])
    with abrir_ventas() as conn:
        fid = _factura(conn, vid)
        _nota_que_movio(conn, fid, total=200, numero=1, medio="efectivo")
        monkeypatch.delattr(notas_de_credito, "devuelto_por_notas")
        ventas.anular_venta(conn, vid, usuario_id=USUARIO["id"])
        conn.commit()
        assert _anulaciones(conn) == [("efectivo", 200.0)], "sin la función, todo lo de siempre"


# ── La cuenta, sin base ────────────────────────────────────────────────────

def _pagos(*filas):
    return [{"id": i, "medio": m, "monto": monto} for i, (m, monto) in enumerate(filas, start=1)]


def test_descontar_reparte_por_medio_primero_y_despues_en_orden_de_cobro():
    pagos = _pagos(("efectivo", 100.0), ("tarjeta_debito", 100.0), ("qr", 50.0))
    # 130 devueltos en tarjeta: 100 salen de la tarjeta, los 30 que no caben salen del efectivo (el primero cobrado).
    a_revertir, sin_caja = ventas.descontar_reintegro_de_notas(pagos, {"tarjeta_debito": Decimal("130")}, Decimal(0))
    assert [(p["id"], p["monto"]) for p in a_revertir] == [(1, 70.0), (3, 50.0)]
    assert sin_caja == 0


def test_descontar_lo_dejado_a_favor_sale_de_los_pagos_de_caja_y_lo_que_sobra_es_deuda():
    pagos = _pagos(("efectivo", 100.0), ("cuenta_corriente", 100.0))
    a_revertir, sin_caja = ventas.descontar_reintegro_de_notas(pagos, {}, Decimal("150"))
    assert [(p["medio"], p["monto"]) for p in a_revertir] == [("cuenta_corriente", 100.0)]
    assert sin_caja == Decimal("50"), "lo que no cupo en la caja es deuda ya abonada"


def test_descontar_sin_nada_que_restar_devuelve_los_pagos_tal_cual():
    pagos = _pagos(("efectivo", 100.0), ("cuenta_corriente", 50.0))
    assert ventas.descontar_reintegro_de_notas(pagos, {}, Decimal(0)) == (pagos, Decimal(0))


def test_descontar_un_medio_desconocido_sale_de_cualquier_pago_de_caja():
    pagos = _pagos(("efectivo", 100.0))
    a_revertir, _ = ventas.descontar_reintegro_de_notas(pagos, {None: Decimal("40")}, Decimal(0))
    assert [p["monto"] for p in a_revertir] == [60.0]
