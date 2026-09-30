"""FEFO en la venta y lote en la anulación (A-4 PR-2, ADR-018, 2026-09-30), contra los dos motores.

La venta de un producto **marcado** (`tracks_expiry = 1`) ya no sale del bucket «sin lote»: sale de los lotes en orden
FEFO, una fila `sale` por lote consumido; la anulación devuelve cada cantidad al lote del que salió. Un producto sin
marcar y un marcado sin lotes escriben el ledger de siempre: eso lo fija `tests/test_ledger_sin_marcar.py` (que no se
toca salvo los `test_a4_pr2_*`, invertidos a propósito) y acá sólo se suma lo que hace falta para la parte nueva.

Qué se mide:

- el **orden** (vencido primero, con fecha antes que sin fecha, sin lote último, desempate por código) y el **reparto**
  (una fila por lote, exacto al borde, faltante en UNA fila sin lote, cantidades fraccionarias sin restos);
- las **dimensiones** del bucket: variante y depósito;
- la **receta** (FEFO sobre el insumo marcado; el plato y el insumo sin marcar no cambian);
- la **anulación** (al lote de origen, faltante incluido, lote mermado después, doble anulación);
- la **concurrencia** en PostgreSQL (el bloqueo por producto, en orden de id);
- lo que **no** cambia: márgenes y reposición, la sonda de opt-in, una base sin la revisión `0002`;
- los **avisos** y la **planificación** (`avisos_de_venta`, `planificar_salida`): lectura pura, sin escribir.
"""

from __future__ import annotations

import datetime
import threading
import time
from decimal import Decimal

import pytest
from conftest import USUARIO, _schema_de_producto, url_postgres
from libracore.db import core

from libracommerce import migrar
from libracommerce.erp import Hooks, Insumo, catalogo, lotes, margen, reposicion, stock, vencimientos, ventas

HOY = datetime.date(2026, 9, 30)
PREVIA = "2026-09-01"
FECHA_VENTA = "2026-09-10"
FECHA_HOY = "2026-09-20"

#: Vencimientos de los lotes de estas pruebas (con `HOY` = 2026-09-30).
VENCIDO = "2026-09-10"       # hace 20 días
VENCE_HOY = "2026-09-30"
EN_15 = "2026-10-15"         # el borde cerrado de la ventana de 15 días
EN_16 = "2026-10-16"         # el primero que queda afuera
LEJOS = "2026-12-01"


# ── Fixtures ─────────────────────────────────────────────────────────────


def _liberar():
    core._db_path = None
    core._database_url = None


@pytest.fixture(params=["sqlite", "postgres"])
def abrir_fefo(request, tmp_path):
    """Un `conexion()` como el de un producto (LibraCore + LibraCommerce, depósito principal y revisión `0002`)."""
    destino = str(tmp_path / "fefo.db") if request.param == "sqlite" else url_postgres()
    core.configure(destino)
    conn = core.get_connection()
    try:
        if request.param == "postgres":
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
            conn.commit()
        _schema_de_producto(conn)
    finally:
        conn.close()
    _liberar()
    migrar.upgrade(destino)
    core.configure(destino)
    yield core.get_connection
    _liberar()


@pytest.fixture(autouse=True)
def hoy_fijo(monkeypatch):
    monkeypatch.setattr(core, "_ar_now", lambda: f"{FECHA_HOY} 12:00:00")
    monkeypatch.setattr(vencimientos, "hoy_argentina", lambda ahora=None: HOY)


# ── Helpers ──────────────────────────────────────────────────────────────

L1_L2 = (("L1", "2026-10-05", 6), ("L2", "2026-11-01", 4))


def _producto(abrir, nombre="Yogur", *, lotes_=L1_L2, sin_lote=0, marcado=True, deposito=None, variante=None,
              unidad="u", fraccion=None):
    """Un producto con sus lotes `(código, vence, cantidad)` y `sin_lote` unidades sin lote en `deposito`."""
    with abrir() as conn:
        pid = catalogo.create_producto(conn, nombre, precio_venta=100.0, precio_costo=60.0, unidad=unidad,
                                       permite_fraccion=fraccion)
        if marcado:
            vencimientos.marcar_vence(conn, pid, True)
        for lote, vence, cantidad in lotes_:
            stock.add_movimiento_stock(conn, pid, "entrada", cantidad, "carga", fecha=PREVIA, lot_code=lote,
                                       expires_at=vence, deposito_id=deposito, variant_id=variante)
        if sin_lote:
            stock.add_movimiento_stock(conn, pid, "entrada", sin_lote, "carga", fecha=PREVIA, deposito_id=deposito,
                                       variant_id=variante)
        conn.commit()
    return pid


def _deposito_nuevo(abrir, nombre="Norte"):
    with abrir() as conn:
        d = catalogo.create_deposito(conn, nombre)
        conn.commit()
        return d


def _principal(abrir):
    with abrir() as conn:
        return catalogo.get_default_deposito_id(conn)


def _linea(pid, qty, *, precio=100.0, **extra):
    return {"nombre": "Yogur", "qty": qty, "precio": precio, "subtotal": round(qty * precio, 2),
            "producto_id": pid, **extra}


def _vender(abrir, items, *, deposito=None, hooks=None, fecha=FECHA_VENTA):
    total = round(sum(i["subtotal"] for i in items), 2)
    pagos = [{"medio": "efectivo", "monto": total, "estado": "aprobado"}]
    kw = {"hooks": hooks} if hooks is not None else {}
    return ventas.crear_venta_directa(
        abrir, fecha=fecha, items=items, subtotal=total, descuento=0.0, total=total, cliente_id=None,
        cliente_nombre="", usuario_id=USUARIO["id"], observaciones="",
        estado=ventas.estado_segun_pagos(total, pagos), pagos=pagos, stock_habilitado=True, deposito_id=deposito, **kw,
    )


def _anular(abrir, vid):
    with abrir() as conn:
        r = ventas.anular_venta(conn, vid, usuario_id=USUARIO["id"])
        conn.commit()
    return r


def _filas(abrir, vid, tipo="venta"):
    """Las filas de ledger de una venta con ese `reason_code`: `(producto, depósito, variante, cantidad, lote, vence)`."""
    with abrir() as conn:
        return [
            (f["item_id"], f["location_id"], f["variant_id"], float(f["quantity_delta"]), f["lot_code"], f["expires_at"])
            for f in conn.execute(
                "SELECT item_id, location_id, variant_id, quantity_delta, lot_code, expires_at FROM stock_movements "
                "WHERE source_id = ? AND reason_code = ? ORDER BY id", (vid, tipo))
        ]


def _tramos(abrir, vid, tipo="venta"):
    """Lo mismo, sin producto, depósito ni variante: `(cantidad, lote, vence)`."""
    return [(c, lo, ve) for _, _, _, c, lo, ve in _filas(abrir, vid, tipo)]


_TODAS = object()


def _saldos(abrir, pid, deposito=None, variante=_TODAS):
    """`{lote: saldo}` de un producto (el bucket sin lote es la clave `None`); `variante=None` es el producto sin
    variantes, y sin pasarla se mira todo."""
    with abrir() as conn:
        return {f["lote"]: f["saldo"] for f in vencimientos.lotes_de(conn, pid, hoy=HOY)
                if (deposito is None or f["deposito_id"] == deposito)
                and (variante is _TODAS or f["variante_id"] == variante)}


def _ledger(abrir):
    with abrir() as conn:
        return [tuple(f) for f in conn.execute(
            "SELECT item_id, variant_id, location_id, movement_type, quantity_delta, occurred_at, source_type, "
            "source_id, lot_code, expires_at, note, created_by, reason_code FROM stock_movements ORDER BY id")]


def _receta(plato, insumos):
    def resolver(item_id, item):
        return [Insumo(item_id=i, cantidad=Decimal(c)) for i, c in insumos] if item_id == plato else None
    return Hooks(resolver_receta=resolver)


# ═══════════════════════════════════════════════════ El orden FEFO ═══════


def test_un_lote_vencido_sale_primero_y_se_vende_con_aviso(abrir_fefo):
    pid = _producto(abrir_fefo, lotes_=(("NUEVO", LEJOS, 5), ("VIEJO", VENCIDO, 5)))
    vid = _vender(abrir_fefo, [_linea(pid, 2)])
    assert _tramos(abrir_fefo, vid) == [(-2.0, "VIEJO", VENCIDO)]
    with abrir_fefo() as conn:
        (aviso,) = lotes.avisos_de_venta(conn, vid, hoy=HOY)
    assert (aviso["tipo"], aviso["lote"], aviso["dias_para_vencer"], aviso["cantidad"]) == ("lote_vencido", "VIEJO", -20, 2)


def test_con_fecha_antes_que_sin_fecha_y_sin_lote_ultimo(abrir_fefo):
    pid = _producto(abrir_fefo, lotes_=(("SINFECHA", None, 3), ("CONFECHA", LEJOS, 3)), sin_lote=3)
    vid = _vender(abrir_fefo, [_linea(pid, 9)])
    assert _tramos(abrir_fefo, vid) == [(-3.0, "CONFECHA", LEJOS), (-3.0, "SINFECHA", None), (-3.0, None, None)]
    assert _saldos(abrir_fefo, pid) == {}


def test_el_sin_lote_es_lo_ultimo_aunque_haya_mucho(abrir_fefo):
    pid = _producto(abrir_fefo, lotes_=(("L1", LEJOS, 2),), sin_lote=50)
    vid = _vender(abrir_fefo, [_linea(pid, 4)])
    assert _tramos(abrir_fefo, vid) == [(-2.0, "L1", LEJOS), (-2.0, None, None)]
    assert _saldos(abrir_fefo, pid) == {None: 48}


def test_una_venta_que_cruza_varios_lotes_escribe_una_fila_por_lote(abrir_fefo):
    pid = _producto(abrir_fefo, lotes_=(("L3", "2026-12-01", 5), ("L1", "2026-10-05", 6), ("L2", "2026-11-01", 4)))
    vid = _vender(abrir_fefo, [_linea(pid, 12)])
    assert _tramos(abrir_fefo, vid) == [(-6.0, "L1", "2026-10-05"), (-4.0, "L2", "2026-11-01"),
                                       (-2.0, "L3", "2026-12-01")]
    assert _saldos(abrir_fefo, pid) == {"L3": 3}
    with abrir_fefo() as conn:
        assert stock.get_stock_actual(conn, pid) == 3.0


def test_el_desempate_es_por_codigo_de_lote(abrir_fefo):
    pid = _producto(abrir_fefo, lotes_=(("B", "2026-10-05", 2), ("A", "2026-10-05", 2), ("C", "2026-10-05", 2)))
    vid = _vender(abrir_fefo, [_linea(pid, 5)])
    assert _tramos(abrir_fefo, vid) == [(-2.0, "A", "2026-10-05"), (-2.0, "B", "2026-10-05"), (-1.0, "C", "2026-10-05")]


def test_vender_justo_lo_del_primer_lote_no_toca_al_segundo_ni_escribe_filas_de_mas(abrir_fefo):
    pid = _producto(abrir_fefo)
    vid = _vender(abrir_fefo, [_linea(pid, 6)])
    assert _tramos(abrir_fefo, vid) == [(-6.0, "L1", "2026-10-05")]
    assert _saldos(abrir_fefo, pid) == {"L2": 4}


def test_un_lote_en_cero_no_se_consume_y_un_saldo_negativo_de_un_lote_tampoco(abrir_fefo):
    pid = _producto(abrir_fefo, lotes_=(("L1", "2026-10-05", 4), ("L2", "2026-11-01", 4)))
    with abrir_fefo() as conn:
        stock.add_movimiento_stock(conn, pid, "ajuste", -5, "rotura", fecha=PREVIA, lot_code="L1", expires_at="2026-10-05")
        conn.commit()     # L1 queda en −1
    vid = _vender(abrir_fefo, [_linea(pid, 2)])
    assert _tramos(abrir_fefo, vid) == [(-2.0, "L2", "2026-11-01")]


def test_el_vencimiento_guardado_con_hora_se_ordena_y_se_copia_como_fecha(abrir_fefo):
    """La recepción de compras guarda `AAAA-MM-DDTHH:MM:SS`: es el mismo bucket que `AAAA-MM-DD` y la fila de venta lo
    copia normalizado."""
    pid = _producto(abrir_fefo, lotes_=())
    with abrir_fefo() as conn:
        for lote, vence, cantidad in (("L2", "2026-11-01T00:00:00", 5), ("L1", "2026-10-05T00:00:00", 5)):
            conn.execute(
                "INSERT INTO stock_movements (item_id, location_id, movement_type, quantity_delta, occurred_at, "
                "lot_code, expires_at) VALUES (?, ?, 'purchase', ?, '2026-09-01T09:00:00', ?, ?)",
                (pid, catalogo.get_default_deposito_id(conn), cantidad, lote, vence))
        conn.commit()
    vid = _vender(abrir_fefo, [_linea(pid, 6)])
    assert _tramos(abrir_fefo, vid) == [(-5.0, "L1", "2026-10-05"), (-1.0, "L2", "2026-11-01")]
    assert _saldos(abrir_fefo, pid) == {"L2": 4}


# ═══════════════════════════════════════════════════ El faltante ═════════


def test_un_faltante_va_a_una_sola_fila_sin_lote_y_deja_el_sin_lote_negativo(abrir_fefo):
    pid = _producto(abrir_fefo)
    vid = _vender(abrir_fefo, [_linea(pid, 14)])
    assert _tramos(abrir_fefo, vid) == [(-6.0, "L1", "2026-10-05"), (-4.0, "L2", "2026-11-01"), (-4.0, None, None)]
    assert _saldos(abrir_fefo, pid) == {None: -4}
    with abrir_fefo() as conn:
        avisos = lotes.avisos_de_venta(conn, vid, hoy=HOY)
    # L1 vence en 5 días (por_vencer) y el resto no tenía respaldo: un aviso de cada cosa.
    assert [(a["tipo"], a["lote"], a["vence"], a["cantidad"]) for a in avisos] == [
        ("por_vencer", "L1", "2026-10-05", 6), ("faltante_sin_lote", None, None, 4)]


def test_con_sin_lote_positivo_el_resto_es_una_sola_fila_y_el_faltante_es_lo_que_no_tenia_respaldo(abrir_fefo):
    pid = _producto(abrir_fefo, sin_lote=3)
    vid = _vender(abrir_fefo, [_linea(pid, 15)])
    assert _tramos(abrir_fefo, vid) == [(-6.0, "L1", "2026-10-05"), (-4.0, "L2", "2026-11-01"), (-5.0, None, None)]
    with abrir_fefo() as conn:
        avisos = lotes.avisos_de_venta(conn, vid, hoy=HOY)
    assert [(a["tipo"], a["cantidad"]) for a in avisos if a["tipo"] == "faltante_sin_lote"] == [("faltante_sin_lote", 2)]


def test_un_marcado_sin_ningun_stock_vende_una_fila_sin_lote_como_siempre(abrir_fefo):
    pid = _producto(abrir_fefo, lotes_=())
    vid = _vender(abrir_fefo, [_linea(pid, 3)])
    assert _tramos(abrir_fefo, vid) == [(-3.0, None, None)]
    with abrir_fefo() as conn:
        assert [a["tipo"] for a in lotes.avisos_de_venta(conn, vid, hoy=HOY)] == ["faltante_sin_lote"]


def test_cantidades_fraccionarias_no_dejan_restos(abrir_fefo):
    """0,1 + 0,2 en `float` no es 0,3: los saldos y lo pedido se limpian a 10 decimales, así que vender exactamente lo
    que hay consume los lotes enteros y no deja ni un resto ni una fila de faltante."""
    pid = _producto(abrir_fefo, "Queso", lotes_=(("L1", "2026-10-05", 0.1), ("L2", "2026-11-01", 0.2),
                                                  ("L3", "2026-12-01", 0.7)), unidad="kg", fraccion=True)
    vid = _vender(abrir_fefo, [_linea(pid, 0.1 + 0.2)])
    assert _tramos(abrir_fefo, vid) == [(-0.1, "L1", "2026-10-05"), (-0.2, "L2", "2026-11-01")]
    vid2 = _vender(abrir_fefo, [_linea(pid, 0.35)])
    assert _tramos(abrir_fefo, vid2) == [(-0.35, "L3", "2026-12-01")]
    assert _saldos(abrir_fefo, pid) == {"L3": 0.35}
    vid3 = _vender(abrir_fefo, [_linea(pid, 0.35)])
    assert _tramos(abrir_fefo, vid3) == [(-0.35, "L3", "2026-12-01")]
    assert _saldos(abrir_fefo, pid) == {}


def test_cantidad_pedida_con_ruido_de_float_en_varios_lotes(abrir_fefo):
    pid = _producto(abrir_fefo, "Jamón", lotes_=(("L1", "2026-10-05", 0.7), ("L2", "2026-11-01", 0.4)),
                    unidad="kg", fraccion=True)
    vid = _vender(abrir_fefo, [_linea(pid, 1.1)])
    assert _tramos(abrir_fefo, vid) == [(-0.7, "L1", "2026-10-05"), (-0.4, "L2", "2026-11-01")]
    assert _saldos(abrir_fefo, pid) == {}


def test_plan_fefo_es_una_lectura_y_una_cantidad_nula_no_planifica_nada(abrir_fefo):
    pid = _producto(abrir_fefo)
    dep = _principal(abrir_fefo)
    antes = _ledger(abrir_fefo)
    with abrir_fefo() as conn:
        assert lotes.plan_fefo(conn, pid, dep, None, 0) == []
        tramos = lotes.plan_fefo(conn, pid, dep, None, 7)
    assert [(t.lote, t.vence, t.cantidad, t.faltante) for t in tramos] == [
        ("L1", "2026-10-05", Decimal("6"), Decimal("0")), ("L2", "2026-11-01", Decimal("1"), Decimal("0"))]
    assert _ledger(abrir_fefo) == antes


# ═══════════════════════════ Una cantidad positiva nunca se pierde (ni se vuelve cero) ═══════════════════════════

#: Cantidades que el redondeo a 10 decimales (o un `float`) podría comerse: minúsculas, con ruido, enormes, fraccionarias.
CANTIDADES = [5e-13, 4e-11, 1e-10, 5e-11, 1.4e-10, 1e-5, 0.1 + 0.2, 0.1 * 3, 0.333, 0.3333333333333333, 1.00000000004,
              6.000000000049, 10.0000000001, 2.675, 0.7, 1.1, 123456.789012345, 1e9 + 0.5, 987654321.123456789]


def _suma_de_filas(abrir, vid):
    filas = _filas(abrir, vid)
    assert all(f[3] < 0 for f in filas), filas
    return sum((Decimal(str(-f[3])) for f in filas), Decimal(0))


def test_una_cantidad_minuscula_de_un_marcado_escribe_una_fila_sin_lote_con_esa_cantidad(abrir_fefo):
    """Regresión (revisión de Codex): con `qty=4e-11` el plan quedaba vacío y la venta no escribía NINGÚN movimiento."""
    pid = _producto(abrir_fefo)
    vid = _vender(abrir_fefo, [_linea(pid, 4e-11)])
    assert _tramos(abrir_fefo, vid) == [(-4e-11, None, None)]
    with abrir_fefo() as conn:
        assert stock.get_stock_actual(conn, pid) == pytest.approx(10 - 4e-11, abs=1e-12)


def test_una_cantidad_minuscula_sin_lotes_ni_stock_tambien_escribe(abrir_fefo):
    pid = _producto(abrir_fefo, lotes_=())
    vid = _vender(abrir_fefo, [_linea(pid, 4e-11)])
    assert _tramos(abrir_fefo, vid) == [(-4e-11, None, None)]


def test_la_suma_de_las_filas_escritas_es_siempre_la_cantidad_vendida(abrir_fefo):
    """El barrido: para cada cantidad, una venta FEFO sobre lotes fraccionarios (0,1 + 0,2 + 0,7 y un poco sin lote)
    escribe filas cuya suma es la cantidad vendida, sin perder ni una cifra, y nunca deja un lote con código en negativo."""
    for n, q in enumerate(CANTIDADES):
        pid = _producto(abrir_fefo, f"Q{n}", lotes_=(("L1", "2026-10-05", 0.1), ("L2", "2026-11-01", 0.2),
                                                        ("L3", "2026-12-01", 0.7)), sin_lote=0.05, unidad="kg",
                        fraccion=True)
        with abrir_fefo() as conn:
            stock.descontar_stock_venta(conn, 1000 + n, [{"producto_id": pid, "qty": q}], fecha=FECHA_VENTA)
            conn.commit()
        suma = _suma_de_filas(abrir_fefo, 1000 + n)
        assert abs(suma - Decimal(str(q))) <= Decimal("1e-12") * max(Decimal(1), Decimal(str(q))), (q, suma)
        assert all(saldo >= 0 for lote, saldo in _saldos(abrir_fefo, pid).items() if lote is not None), q


def test_el_plan_suma_exactamente_la_cantidad_pedida(abrir_fefo):
    pid = _producto(abrir_fefo, lotes_=(("L1", "2026-10-05", 0.1), ("L2", "2026-11-01", 0.2)), unidad="kg", fraccion=True)
    dep = _principal(abrir_fefo)
    with abrir_fefo() as conn:
        for q in CANTIDADES:
            tramos = lotes.plan_fefo(conn, pid, dep, None, q)
            exacta = Decimal(str(q))
            assert abs(sum(t.cantidad for t in tramos) - exacta) <= lotes.UMBRAL_DE_RUIDO * max(Decimal(1), exacta), q
            assert q < 1e-12 or sum(t.cantidad for t in tramos) != 0, q
            assert all(t.cantidad > 0 for t in tramos), q
        for q in (0.0, -1.0):
            assert lotes.plan_fefo(conn, pid, dep, None, q) == []


def test_planificar_salida_y_avisos_no_pierden_una_cantidad_minuscula(abrir_fefo):
    pid = _producto(abrir_fefo)
    with abrir_fefo() as conn:
        plan = lotes.planificar_salida(conn, [_linea(pid, 4e-11)], hoy=HOY)
    assert [(s["lote"], s["cantidad"], s["faltante"]) for s in plan["salidas"]] == [(None, 4e-11, 4e-11)]
    vid = _vender(abrir_fefo, [_linea(pid, 4e-11)])
    with abrir_fefo() as conn:
        # El saldo «sin lote» que queda (−4e-11) está por debajo de la precisión con que se miden los saldos (10
        # decimales): el aviso posterior no lo ve; lo que importa es que la fila se escribió (primer test).
        assert lotes.avisos_de_venta(conn, vid, hoy=HOY) == []


# ═══════════════════════════════════════════════════ Varias líneas y productos ══


def test_varios_productos_en_una_venta_cada_uno_por_su_fefo_y_en_el_orden_de_las_lineas(abrir_fefo):
    a = _producto(abrir_fefo, "Yogur")
    b = _producto(abrir_fefo, "Leche", lotes_=(("LE1", "2026-10-01", 3),))
    sin = _producto(abrir_fefo, "Pan", lotes_=(), sin_lote=10, marcado=False)
    vid = _vender(abrir_fefo, [_linea(b, 2), _linea(sin, 1), _linea(a, 7)])
    assert _filas(abrir_fefo, vid) == [
        (b, _principal(abrir_fefo), None, -2.0, "LE1", "2026-10-01"),
        (sin, _principal(abrir_fefo), None, -1.0, None, None),
        (a, _principal(abrir_fefo), None, -6.0, "L1", "2026-10-05"),
        (a, _principal(abrir_fefo), None, -1.0, "L2", "2026-11-01"),
    ]


def test_varias_lineas_del_mismo_producto_consumen_en_secuencia(abrir_fefo):
    pid = _producto(abrir_fefo)
    vid = _vender(abrir_fefo, [_linea(pid, 4), _linea(pid, 4), _linea(pid, 1)])
    assert _tramos(abrir_fefo, vid) == [(-4.0, "L1", "2026-10-05"), (-2.0, "L1", "2026-10-05"),
                                       (-2.0, "L2", "2026-11-01"), (-1.0, "L2", "2026-11-01")]
    assert _saldos(abrir_fefo, pid) == {"L2": 1}


def test_una_variante_consume_solo_los_buckets_de_esa_variante(abrir_fefo):
    pid = _producto(abrir_fefo, lotes_=(("L1", "2026-10-05", 5),))     # los lotes del producto sin variante
    with abrir_fefo() as conn:
        v1 = catalogo.create_variante(conn, pid, "Y-1", "Frutilla")["id"]
        stock.add_movimiento_stock(conn, pid, "entrada", 3, "carga", fecha=PREVIA, lot_code="V1A",
                                   expires_at="2026-12-01", variant_id=v1)
        stock.add_movimiento_stock(conn, pid, "entrada", 3, "carga", fecha=PREVIA, lot_code="V1B",
                                   expires_at="2026-09-20", variant_id=v1)
        conn.commit()
    vid = _vender(abrir_fefo, [_linea(pid, 4, variante_id=v1)])
    # V1B vence antes; el lote L1 (2026-10-05) es de OTRA variante (ninguna) y no se toca.
    assert _filas(abrir_fefo, vid) == [
        (pid, _principal(abrir_fefo), v1, -3.0, "V1B", "2026-09-20"),
        (pid, _principal(abrir_fefo), v1, -1.0, "V1A", "2026-12-01"),
    ]
    assert _saldos(abrir_fefo, pid, variante=None) == {"L1": 5}
    vid2 = _vender(abrir_fefo, [_linea(pid, 2)])
    assert _tramos(abrir_fefo, vid2) == [(-2.0, "L1", "2026-10-05")]


def test_una_venta_solo_consume_los_buckets_del_deposito_de_la_venta(abrir_fefo):
    principal = _principal(abrir_fefo)
    norte = _deposito_nuevo(abrir_fefo)
    pid = _producto(abrir_fefo, lotes_=(("P1", "2026-11-01", 5),))
    with abrir_fefo() as conn:     # Norte tiene un lote que vence ANTES que el del principal
        stock.add_movimiento_stock(conn, pid, "entrada", 2, "carga", fecha=PREVIA, lot_code="N1",
                                   expires_at="2026-10-01", deposito_id=norte)
        conn.commit()
    vid = _vender(abrir_fefo, [_linea(pid, 3)], deposito=principal)
    assert _filas(abrir_fefo, vid) == [(pid, principal, None, -3.0, "P1", "2026-11-01")]
    vid2 = _vender(abrir_fefo, [_linea(pid, 3)], deposito=norte)
    # Norte sólo tiene 2 en N1: el resto es faltante SIN LOTE en Norte, y el principal no se toca.
    assert _filas(abrir_fefo, vid2) == [(pid, norte, None, -2.0, "N1", "2026-10-01"), (pid, norte, None, -1.0, None, None)]
    assert _saldos(abrir_fefo, pid, deposito=principal) == {"P1": 2}


def test_sin_deposito_la_venta_usa_el_principal(abrir_fefo):
    principal = _principal(abrir_fefo)
    pid = _producto(abrir_fefo)
    vid = _vender(abrir_fefo, [_linea(pid, 7)])
    assert [f[1] for f in _filas(abrir_fefo, vid)] == [principal, principal]


def test_un_servicio_y_una_linea_libre_no_escriben_y_un_producto_sin_marcar_sale_como_siempre(abrir_fefo):
    pid = _producto(abrir_fefo)
    with abrir_fefo() as conn:
        servicio = catalogo.create_producto(conn, "Flete", precio_venta=500.0, tipo="servicio")
        conn.commit()
    vid = _vender(abrir_fefo, [_linea(servicio, 1), {"nombre": "Envío", "qty": 1, "precio": 3.0, "subtotal": 3.0,
                                                    "producto_id": None}, _linea(pid, 1)])
    assert [(f[0], f[4]) for f in _filas(abrir_fefo, vid)] == [(pid, "L1")]


# ═══════════════════════════════════════════════════ Receta ══════════════


def test_receta_fefo_sobre_el_insumo_marcado_y_el_plato_y_el_insumo_sin_marcar_no_cambian(abrir_fefo):
    marcado = _producto(abrir_fefo, "Pan")
    comun = _producto(abrir_fefo, "Sal", lotes_=(), sin_lote=50, marcado=False)
    plato = _producto(abrir_fefo, "Tostado", lotes_=(), marcado=False)
    principal = _principal(abrir_fefo)
    vid = _vender(abrir_fefo, [_linea(plato, 4)], hooks=_receta(plato, [(marcado, "2"), (comun, "0.25")]))
    assert _filas(abrir_fefo, vid) == [
        (marcado, principal, None, -6.0, "L1", "2026-10-05"),
        (marcado, principal, None, -2.0, "L2", "2026-11-01"),
        (comun, principal, None, -1.0, None, None),
    ]
    with abrir_fefo() as conn:
        assert stock.get_stock_actual(conn, plato) == 0.0     # el plato no descuenta nada
        notas = {f[0] for f in conn.execute("SELECT note FROM stock_movements WHERE source_id = ?", (vid,))}
    assert notas == {f"Venta ID {vid} (receta)"}


def test_receta_con_un_plato_marcado_descuenta_los_insumos_y_no_el_plato(abrir_fefo):
    insumo = _producto(abrir_fefo, "Harina")
    plato = _producto(abrir_fefo, "Pizza")      # marcado, con lotes: si hay receta no se descuenta por él
    vid = _vender(abrir_fefo, [_linea(plato, 2)], hooks=_receta(plato, [(insumo, "1")]))
    assert {f[0] for f in _filas(abrir_fefo, vid)} == {insumo}
    assert _saldos(abrir_fefo, plato) == {"L1": 6, "L2": 4}


# ═══════════════════════════════════════════════════ Anulación ═══════════


def test_la_anulacion_repone_cada_cantidad_al_lote_de_origen_incluido_el_faltante(abrir_fefo):
    pid = _producto(abrir_fefo)
    antes = _saldos(abrir_fefo, pid)
    vid = _vender(abrir_fefo, [_linea(pid, 14)])
    assert _anular(abrir_fefo, vid) is True
    assert _tramos(abrir_fefo, vid, "anulacion") == [(6.0, "L1", "2026-10-05"), (4.0, "L2", "2026-11-01"),
                                                    (4.0, None, None)]
    assert _saldos(abrir_fefo, pid) == antes
    with abrir_fefo() as conn:
        assert stock.get_stock_actual(conn, pid) == 10.0


def test_la_anulacion_con_receta_repone_el_lote_del_insumo(abrir_fefo):
    insumo = _producto(abrir_fefo, "Pan")
    plato = _producto(abrir_fefo, "Tostado", lotes_=(), marcado=False)
    vid = _vender(abrir_fefo, [_linea(plato, 4)], hooks=_receta(plato, [(insumo, "2")]))
    assert _saldos(abrir_fefo, insumo) == {"L2": 2}
    assert _anular(abrir_fefo, vid) is True
    assert _tramos(abrir_fefo, vid, "anulacion") == [(6.0, "L1", "2026-10-05"), (2.0, "L2", "2026-11-01")]
    assert _saldos(abrir_fefo, insumo) == {"L1": 6, "L2": 4}


def test_la_anulacion_repone_a_un_lote_que_se_dio_de_baja_despues_de_la_venta(abrir_fefo):
    """Documentado: la anulación repone SIEMPRE al lote de origen, aunque después de la venta ese lote se haya dado de
    baja (merma). La merma descontó lo que había entonces; lo que vuelve por la anulación es mercadería que estaba
    vendida y vuelve: el lote reaparece con esa cantidad."""
    pid = _producto(abrir_fefo)
    principal = _principal(abrir_fefo)
    vid = _vender(abrir_fefo, [_linea(pid, 3)])         # L1: 6 → 3
    with abrir_fefo() as conn:
        vencimientos.dar_de_baja_lote(conn, pid, principal, "L1", "2026-10-05", 3, clave_operacion="baja-1")
        conn.commit()
    assert _saldos(abrir_fefo, pid) == {"L2": 4}
    assert _anular(abrir_fefo, vid) is True
    assert _tramos(abrir_fefo, vid, "anulacion") == [(3.0, "L1", "2026-10-05")]
    assert _saldos(abrir_fefo, pid) == {"L1": 3, "L2": 4}


def test_anular_dos_veces_no_escribe_la_segunda(abrir_fefo):
    pid = _producto(abrir_fefo)
    vid = _vender(abrir_fefo, [_linea(pid, 8)])
    assert _anular(abrir_fefo, vid) is True
    despues = _ledger(abrir_fefo)
    assert _anular(abrir_fefo, vid) is False
    assert _ledger(abrir_fefo) == despues


def test_la_anulacion_no_mira_la_marca_lo_que_dice_el_ledger_manda(abrir_fefo):
    """Desmarcar entre la venta y la anulación no cambia a dónde vuelve; y una venta hecha sin lote (antes de marcar el
    producto) repone sin lote aunque después se lo marque."""
    pid = _producto(abrir_fefo)
    vid = _vender(abrir_fefo, [_linea(pid, 3)])
    with abrir_fefo() as conn:
        vencimientos.marcar_vence(conn, pid, False)
        conn.commit()
    assert _anular(abrir_fefo, vid) is True
    assert _tramos(abrir_fefo, vid, "anulacion") == [(3.0, "L1", "2026-10-05")]

    comun = _producto(abrir_fefo, "Pan", lotes_=(), sin_lote=10, marcado=False)
    vid2 = _vender(abrir_fefo, [_linea(comun, 2)])
    with abrir_fefo() as conn:
        vencimientos.marcar_vence(conn, comun, True)
        conn.commit()
    assert _anular(abrir_fefo, vid2) is True
    assert _tramos(abrir_fefo, vid2, "anulacion") == [(2.0, None, None)]


def test_la_anulacion_de_una_venta_sin_lotes_es_la_de_hoy(abrir_fefo):
    """Sin marcar: las filas de reposición son exactamente las de siempre (el ledger completo, sin lote)."""
    pid = _producto(abrir_fefo, "Pan", lotes_=(), sin_lote=10, marcado=False)
    vid = _vender(abrir_fefo, [_linea(pid, 2), _linea(pid, 3)])
    principal = _principal(abrir_fefo)
    assert _anular(abrir_fefo, vid) is True
    assert _ledger(abrir_fefo)[-2:] == [
        (pid, None, principal, "return", 2, f"{FECHA_HOY}T00:00:00", "venta", vid, None, None,
         f"Anulación venta ID {vid}", USUARIO["id"], "anulacion"),
        (pid, None, principal, "return", 3, f"{FECHA_HOY}T00:00:00", "venta", vid, None, None,
         f"Anulación venta ID {vid}", USUARIO["id"], "anulacion"),
    ]


def test_la_anulacion_repone_en_el_orden_de_escritura(abrir_fefo):
    pid = _producto(abrir_fefo)
    vid = _vender(abrir_fefo, [_linea(pid, 7)])
    _anular(abrir_fefo, vid)
    assert _tramos(abrir_fefo, vid, "anulacion") == [(-c, lo, ve) for c, lo, ve in _tramos(abrir_fefo, vid)]


# ═══════════════════════════════════════════════════ La sonda y una base sin 0002 ══


class _Grabadora:
    """Una conexión que anota cada SQL que se ejecuta y delega todo lo demás."""

    def __init__(self, conn, registro):
        self._conn = conn
        self._registro = registro

    def execute(self, sql, params=()):
        self._registro.append(" ".join(sql.split()))
        return self._conn.execute(sql, params)

    def __getattr__(self, nombre):
        return getattr(self._conn, nombre)


def test_la_sonda_es_una_sola_consulta_y_un_producto_sin_marcar_no_se_bloquea(abrir_fefo):
    comun = _producto(abrir_fefo, "Pan", lotes_=(), sin_lote=10, marcado=False)
    otro = _producto(abrir_fefo, "Sal", lotes_=(), sin_lote=10, marcado=False)
    registro: list[str] = []
    with abrir_fefo() as conn:
        stock.descontar_stock_venta(_Grabadora(conn, registro), 1, [_linea(comun, 2), _linea(otro, 1)],
                                    fecha=FECHA_VENTA)
    sondas = [s for s in registro if "tracks_expiry" in s and s.startswith("SELECT")]
    assert len(sondas) == 1 and "tracks_expiry = 1" in sondas[0]
    assert not [s for s in registro if s.startswith("UPDATE")], "un producto sin marcar no se bloquea"
    assert not [s for s in registro if "GROUP BY" in s], "un producto sin marcar no lee saldos"


def test_sin_productos_que_descuenten_no_hay_ni_sonda(abrir_fefo):
    registro: list[str] = []
    with abrir_fefo() as conn:
        stock.descontar_stock_venta(_Grabadora(conn, registro), 1, [{"nombre": "Envío", "qty": 1, "producto_id": None}])
    assert not [s for s in registro if "tracks_expiry" in s or "table_info" in s.lower()]


def test_los_productos_marcados_se_toman_en_orden_ascendente_de_id(abrir_fefo):
    a = _producto(abrir_fefo, "A")
    b = _producto(abrir_fefo, "B")
    c = _producto(abrir_fefo, "C")
    registro: list = []
    with abrir_fefo() as conn:
        g = _Grabadora(conn, registro)
        original = g.execute

        def anotando(sql, params=()):
            if sql.startswith("UPDATE catalog_items SET tracks_expiry = tracks_expiry"):
                registro.append(("LOCK", params[0]))
            return original(sql, params)

        g.execute = anotando
        stock.descontar_stock_venta(g, 1, [_linea(c, 1), _linea(a, 1), _linea(b, 1), _linea(a, 1)], fecha=FECHA_VENTA)
    assert [p for k, p in (r for r in registro if isinstance(r, tuple))] == sorted([a, b, c])


@pytest.fixture
def abrir_sin_0002(request, tmp_path):
    """Un producto que NO corrió `libracommerce-migrar upgrade`: sin `catalog_items.tracks_expiry`."""
    destino = str(tmp_path / "sin0002.db")
    if request.param == "postgres":
        destino = url_postgres()
    core.configure(destino)
    conn = core.get_connection()
    try:
        if request.param == "postgres":
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
            conn.commit()
        _schema_de_producto(conn)
    finally:
        conn.close()
    yield core.get_connection
    _liberar()


@pytest.mark.parametrize("abrir_sin_0002", ["sqlite", "postgres"], indirect=True)
def test_una_base_sin_la_revision_0002_vende_y_anula_sin_romper_la_transaccion(abrir_sin_0002):
    abrir = abrir_sin_0002
    with abrir() as conn:
        assert lotes.tiene_marca(conn) is False
        assert lotes.ids_marcados(conn, [1, 2]) == set()
        pid = catalogo.create_producto(conn, "Pan", precio_venta=100.0, precio_costo=60.0)
        stock.add_movimiento_stock(conn, pid, "entrada", 10, "carga", fecha=PREVIA)
        conn.commit()
    vid = _vender(abrir, [_linea(pid, 3)])
    assert _tramos(abrir, vid) == [(-3.0, None, None)]
    with abrir() as conn:
        # La transacción sigue viva después de la sonda (en PostgreSQL un SELECT fallido la habría abortado).
        stock.descontar_stock_venta(conn, vid + 100, [_linea(pid, 1)], fecha=FECHA_VENTA)
        assert lotes.avisos_de_venta(conn, vid, hoy=HOY) == []
        assert lotes.planificar_salida(conn, [_linea(pid, 1)], hoy=HOY)["salidas"] == []
        assert stock.get_stock_actual(conn, pid) == 6.0
        conn.commit()
    assert _anular(abrir, vid) is True
    with abrir() as conn:
        assert stock.get_stock_actual(conn, pid) == 9.0


# ═══════════════════════════════════════════════════ Reexportes ══════════


def test_los_nombres_que_se_movieron_siguen_donde_estaban():
    assert stock.normalizar_lote is lotes.normalizar_lote
    assert stock.normalizar_vencimiento is lotes.normalizar_vencimiento
    assert stock.MAX_LARGO_LOTE == lotes.MAX_LARGO_LOTE == 64
    assert vencimientos._saldos is lotes.saldos_por_bucket
    assert vencimientos.DIAS_AVISO == lotes.DIAS_AVISO and vencimientos.MAX_DIAS_AVISO == lotes.MAX_DIAS_AVISO


# ═══════════════════════════════════════════════════ Márgenes y reposición ══


def test_margen_y_reposicion_dan_lo_mismo_con_una_venta_fefo_que_con_un_producto_sin_marcar(abrir_fefo):
    """Margen y reposición leen las líneas de la venta y el ledger por `source_id` y `movement_type`, no el lote: un
    marcado con lotes vendido por FEFO rinde el mismo neto que un producto sin marcar equivalente."""
    fefo = _producto(abrir_fefo, "Yogur")                                          # 6 + 4, marcado
    comun = _producto(abrir_fefo, "Yogur común", lotes_=(), sin_lote=10, marcado=False)
    for qty, fecha in ((4, "2026-09-10"), (3, "2026-09-20")):
        _vender(abrir_fefo, [_linea(fefo, qty), _linea(comun, qty)], fecha=fecha)
    v = _vender(abrir_fefo, [_linea(fefo, 1), _linea(comun, 1)], fecha="2026-09-22")
    _anular(abrir_fefo, v)
    with abrir_fefo() as conn:
        rep = margen.reporte_margen(conn, "2026-09-01", "2026-09-30")
        por = {p["nombre"]: p for p in rep["productos"]}
        a, b = por["Yogur"], por["Yogur común"]
        assert {k: a[k] for k in a if k not in ("producto_id", "nombre")} == {
            k: b[k] for k in b if k not in ("producto_id", "nombre")}
        assert a["unidades"] == 7
        sug = {f["nombre"]: f for f in reposicion.sugerencia_reposicion(conn, hoy=HOY, solo_a_pedir=False)}
        x, y = sug["Yogur"], sug["Yogur común"]
        assert {k: x[k] for k in x if k not in ("producto_id", "nombre", "codigo")} == {
            k: y[k] for k in y if k not in ("producto_id", "nombre", "codigo")}
        assert x["stock"] == 3 and x["unidades_vendidas"] == 7


# ═══════════════════════════════════════════════════ Avisos ══════════════


def test_avisos_la_ventana_es_cerrada_y_lo_que_vence_hoy_es_por_vencer(abrir_fefo):
    pid = _producto(abrir_fefo, lotes_=(("V", VENCIDO, 1), ("H", VENCE_HOY, 1), ("B", EN_15, 1), ("F", EN_16, 1),
                                        ("L", LEJOS, 1)))
    vid = _vender(abrir_fefo, [_linea(pid, 5)])
    with abrir_fefo() as conn:
        avisos = lotes.avisos_de_venta(conn, vid, hoy=HOY)
    assert [(a["tipo"], a["lote"], a["dias_para_vencer"]) for a in avisos] == [
        ("lote_vencido", "V", -20), ("por_vencer", "H", 0), ("por_vencer", "B", 15)]
    assert all(a["producto_id"] == pid and a["nombre"] == "Yogur" and a["deposito_id"] == _principal(abrir_fefo)
               for a in avisos)
    with abrir_fefo() as conn:
        chicos = lotes.avisos_de_venta(conn, vid, hoy=HOY, dias=3)
    assert [a["lote"] for a in chicos] == ["V", "H"]


def test_avisos_suman_las_lineas_del_mismo_lote_y_calculan_hoy_por_defecto(abrir_fefo):
    pid = _producto(abrir_fefo, lotes_=(("V", VENCIDO, 10),))
    vid = _vender(abrir_fefo, [_linea(pid, 2), _linea(pid, 3)])
    with abrir_fefo() as conn:
        (aviso,) = lotes.avisos_de_venta(conn, vid)       # `hoy_argentina` fijado en HOY por la fixture
    assert (aviso["lote"], aviso["cantidad"], aviso["dias_para_vencer"]) == ("V", 5, -20)


def test_avisos_de_una_venta_sin_lotes_o_de_un_producto_sin_marcar_es_una_lista_vacia(abrir_fefo):
    comun = _producto(abrir_fefo, "Pan", lotes_=(), sin_lote=10, marcado=False)
    sin = _producto(abrir_fefo, "Sal", lotes_=(), sin_lote=10)       # marcado, sin lotes, con stock
    vid = _vender(abrir_fefo, [_linea(comun, 2), _linea(sin, 2)])
    with abrir_fefo() as conn:
        assert lotes.avisos_de_venta(conn, vid, hoy=HOY) == []
        assert lotes.avisos_de_venta(conn, 99999, hoy=HOY) == []


def test_un_producto_sin_marcar_que_quedo_negativo_no_avisa_faltante(abrir_fefo):
    comun = _producto(abrir_fefo, "Pan", lotes_=(), sin_lote=1, marcado=False)
    vid = _vender(abrir_fefo, [_linea(comun, 5)])
    with abrir_fefo() as conn:
        assert lotes.avisos_de_venta(conn, vid, hoy=HOY) == []


def test_el_aviso_de_faltante_es_el_de_esa_venta_aunque_otra_venta_lo_empeore_despues(abrir_fefo):
    pid = _producto(abrir_fefo, lotes_=(("L1", LEJOS, 2),))
    v1 = _vender(abrir_fefo, [_linea(pid, 3)])        # faltan 1
    v2 = _vender(abrir_fefo, [_linea(pid, 5)])        # faltan 5 más
    with abrir_fefo() as conn:
        (a1,) = lotes.avisos_de_venta(conn, v1, hoy=HOY)
        a2 = [a for a in lotes.avisos_de_venta(conn, v2, hoy=HOY) if a["tipo"] == "faltante_sin_lote"]
    assert a1["cantidad"] == 1 and [a["cantidad"] for a in a2] == [5]


def test_avisos_no_cuentan_la_anulacion_y_dias_invalidos_son_value_error(abrir_fefo):
    pid = _producto(abrir_fefo, lotes_=(("V", VENCIDO, 5),))
    vid = _vender(abrir_fefo, [_linea(pid, 2)])
    _anular(abrir_fefo, vid)
    with abrir_fefo() as conn:
        assert [a["cantidad"] for a in lotes.avisos_de_venta(conn, vid, hoy=HOY)] == [2]
        for malo in (0, -1, 366, 2.5, "15", True):
            with pytest.raises(ValueError):
                lotes.avisos_de_venta(conn, vid, hoy=HOY, dias=malo)
            with pytest.raises(ValueError):
                lotes.planificar_salida(conn, [], hoy=HOY, dias=malo)
        lotes.avisos_de_venta(conn, vid, hoy=HOY, dias=365)


# ═══════════════════════════════════════════════════ planificar_salida ═══


def test_planificar_salida_dice_lo_que_la_venta_real_escribe_y_no_escribe_nada(abrir_fefo):
    pid = _producto(abrir_fefo, lotes_=(("V", VENCIDO, 3), ("L1", "2026-10-05", 6), ("L2", "2026-11-01", 4)))
    otro = _producto(abrir_fefo, "Pan", lotes_=(), sin_lote=5, marcado=False)
    items = [_linea(pid, 8), _linea(otro, 1), _linea(pid, 3)]
    antes = _ledger(abrir_fefo)
    registro: list[str] = []
    with abrir_fefo() as conn:
        plan = lotes.planificar_salida(_Grabadora(conn, registro), items, hoy=HOY)
    assert _ledger(abrir_fefo) == antes
    assert not [s for s in registro if s.split()[0] in ("INSERT", "UPDATE", "DELETE")], registro
    assert plan["hoy"] == "2026-09-30" and plan["dias"] == 15
    assert [(s["linea"], s["producto_id"], s["lote"], s["cantidad"], s["estado"], s["dias_para_vencer"])
            for s in plan["salidas"]] == [
        (0, pid, "V", 3, "vencido", -20), (0, pid, "L1", 5, "vigente", 5), (2, pid, "L1", 1, "vigente", 5),
        (2, pid, "L2", 2, "vigente", 32)]
    assert [(a["tipo"], a["lote"], a["cantidad"]) for a in plan["avisos"]] == [
        ("lote_vencido", "V", 3), ("por_vencer", "L1", 6)]
    vid = _vender(abrir_fefo, items)
    real = [(f[3], f[4]) for f in _filas(abrir_fefo, vid) if f[0] == pid]
    assert [(-c, lo) for c, lo in real] == [(s["cantidad"], s["lote"]) for s in plan["salidas"]]
    with abrir_fefo() as conn:
        assert lotes.avisos_de_venta(conn, vid, hoy=HOY) == plan["avisos"]


def test_planificar_salida_faltante_sin_lote_sin_marcar_y_deposito_por_defecto(abrir_fefo):
    principal = _principal(abrir_fefo)
    norte = _deposito_nuevo(abrir_fefo)
    pid = _producto(abrir_fefo)
    comun = _producto(abrir_fefo, "Pan", lotes_=(), sin_lote=5, marcado=False)
    with abrir_fefo() as conn:
        plan = lotes.planificar_salida(conn, [_linea(pid, 12), _linea(comun, 1)], hoy=HOY)
        en_norte = lotes.planificar_salida(conn, [_linea(pid, 1)], norte, hoy=HOY)
    assert {s["producto_id"] for s in plan["salidas"]} == {pid}      # el sin marcar no elige lote: no figura
    assert [(s["lote"], s["cantidad"], s["estado"], s["faltante"], s["deposito_id"]) for s in plan["salidas"]] == [
        ("L1", 6, "vigente", 0, principal), ("L2", 4, "vigente", 0, principal), (None, 2, "sin_lote", 2, principal)]
    assert [(a["tipo"], a["cantidad"]) for a in plan["avisos"]] == [("por_vencer", 6), ("faltante_sin_lote", 2)]
    assert [(s["lote"], s["faltante"], s["deposito_id"]) for s in en_norte["salidas"]] == [(None, 1, norte)]


def test_planificar_salida_con_receta_y_variante(abrir_fefo):
    insumo = _producto(abrir_fefo, "Pan")
    plato = _producto(abrir_fefo, "Tostado", lotes_=(), marcado=False)
    con_variantes = _producto(abrir_fefo, "Remera", lotes_=())
    with abrir_fefo() as conn:
        v = catalogo.create_variante(conn, con_variantes, "R-1", "Roja")["id"]
        stock.add_movimiento_stock(conn, con_variantes, "entrada", 2, "carga", fecha=PREVIA, lot_code="R",
                                   expires_at=LEJOS, variant_id=v)
        conn.commit()
        plan = lotes.planificar_salida(
            conn, [_linea(plato, 4), _linea(con_variantes, 1, variante_id=v), _linea(con_variantes, 1)], hoy=HOY,
            hooks=_receta(plato, [(insumo, "2")]))
    assert [(s["producto_id"], s["variante_id"], s["lote"], s["cantidad"]) for s in plan["salidas"]] == [
        (insumo, None, "L1", 6), (insumo, None, "L2", 2), (con_variantes, v, "R", 1), (con_variantes, None, None, 1)]


def test_planificar_salida_de_una_lista_vacia_o_sin_productos(abrir_fefo):
    with abrir_fefo() as conn:
        assert lotes.planificar_salida(conn, [], hoy=HOY) == {"hoy": "2026-09-30", "dias": 15, "salidas": [],
                                                              "avisos": []}
        assert lotes.planificar_salida(conn, [{"nombre": "Envío", "qty": 1, "producto_id": None}], hoy=HOY)["salidas"] == []


# ═══════════════════════════════════════════════════ Concurrencia (PostgreSQL) ══


def _solo_postgres():
    if not core.is_postgres():
        pytest.skip("sólo PostgreSQL toma el bloqueo por fila")


def _ensanchar_la_ventana(monkeypatch, *, despues_de_leer_saldos=0.0, despues_de_tomar=0.0):
    """Las carreras de estas pruebas duran microsegundos y una corrida sana puede no verlas aunque falte el bloqueo:
    se ensancha la ventana con una pausa justo donde una versión sin bloqueo (o con los bloqueos en otro orden) se
    rompe. Con el código bueno la pausa no cambia el resultado: el segundo hilo espera el bloqueo antes de leer."""
    if despues_de_leer_saldos:
        original = lotes.plan_fefo

        def plan_lento(*a, **k):
            tramos = original(*a, **k)
            time.sleep(despues_de_leer_saldos)
            return tramos

        monkeypatch.setattr(lotes, "plan_fefo", plan_lento)
    if despues_de_tomar:
        from libracore.db import _postgres

        original_execute = _postgres.ConnectionWrapper.execute

        def execute(self, sql, params=None):
            resultado = original_execute(self, sql, params)
            if sql.startswith("UPDATE catalog_items SET tracks_expiry = tracks_expiry"):
                time.sleep(despues_de_tomar)
            return resultado

        monkeypatch.setattr(_postgres.ConnectionWrapper, "execute", execute)


def test_en_postgres_una_venta_espera_a_la_que_tiene_el_producto_y_no_sobreconsume_un_lote(abrir_fefo):
    _solo_postgres()
    pid = _producto(abrir_fefo, lotes_=(("L1", "2026-10-05", 5), ("L2", "2026-11-01", 5)))
    resultado: list = []
    primera = core.get_connection()
    try:
        stock.descontar_stock_venta(primera, 1, [{"producto_id": pid, "qty": 4}], fecha=FECHA_VENTA)   # sin commit

        def segunda():
            c = core.get_connection()
            try:
                stock.descontar_stock_venta(c, 2, [{"producto_id": pid, "qty": 4}], fecha=FECHA_VENTA)
                c.commit()
                resultado.append("pasó")
            finally:
                c.close()

        hilo = threading.Thread(target=segunda)
        hilo.start()
        time.sleep(1.0)
        assert hilo.is_alive() and not resultado, "la segunda venta no esperó a la primera"
        primera.commit()
        hilo.join(10)
    finally:
        primera.close()
    assert resultado == ["pasó"]
    with abrir_fefo() as conn:
        filas = [(f["source_id"], f["lot_code"], float(f["quantity_delta"])) for f in conn.execute(
            "SELECT source_id, lot_code, quantity_delta FROM stock_movements WHERE reason_code = 'venta' ORDER BY id")]
    assert filas == [(1, "L1", -4.0), (2, "L1", -1.0), (2, "L2", -3.0)]
    assert _saldos(abrir_fefo, pid) == {"L2": 2}


def _descontar_en_hilos(trabajos):
    """Corre cada `(venta_id, items)` de `trabajos` en su propia conexión y transacción, todas a la vez (una barrera), con
    `descontar_stock_venta` directo: por `crear_venta_directa` el `INSERT` de `sales.number` ya serializa a las ventas
    simultáneas antes de llegar al stock y taparía lo que se quiere medir acá. Devuelve los errores."""
    barrera = threading.Barrier(len(trabajos))
    errores: list = []

    def correr(venta_id, items):
        c = core.get_connection()
        try:
            barrera.wait(10)
            stock.descontar_stock_venta(c, venta_id, items, fecha=FECHA_VENTA)
            c.commit()
        except Exception as e:  # noqa: BLE001 - se informa en el assert
            c.rollback()
            errores.append(e)
            barrera.abort()
        finally:
            c.close()

    hilos = [threading.Thread(target=correr, args=t) for t in trabajos]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(60)
    assert not any(h.is_alive() for h in hilos), "una venta quedó colgada"
    return errores


def test_en_postgres_ventas_simultaneas_del_mismo_sku_no_sobreconsumen_ni_dejan_un_lote_negativo(abrir_fefo,
                                                                                                 monkeypatch):
    _solo_postgres()
    _ensanchar_la_ventana(monkeypatch, despues_de_leer_saldos=0.2)
    pid = _producto(abrir_fefo, lotes_=(("L1", "2026-10-05", 3), ("L2", "2026-11-01", 3), ("L3", "2026-12-01", 4)))
    assert not _descontar_en_hilos([(n, [{"producto_id": pid, "qty": 2}]) for n in range(1, 6)])
    assert _saldos(abrir_fefo, pid) == {}          # 10 − 5×2: ni un lote negativo ni un resto
    with abrir_fefo() as conn:
        total = conn.execute("SELECT SUM(quantity_delta) FROM stock_movements WHERE reason_code = 'venta'").fetchone()[0]
    assert float(total) == -10.0


def test_en_postgres_dos_ventas_de_productos_distintos_en_orden_cruzado_no_hacen_deadlock(abrir_fefo, monkeypatch):
    _solo_postgres()
    _ensanchar_la_ventana(monkeypatch, despues_de_tomar=0.3)
    a = _producto(abrir_fefo, "A", lotes_=(("A1", "2026-10-05", 50),))
    b = _producto(abrir_fefo, "B", lotes_=(("B1", "2026-10-05", 50),))
    errores = _descontar_en_hilos([(1, [{"producto_id": a, "qty": 1}, {"producto_id": b, "qty": 1}]),
                                   (2, [{"producto_id": b, "qty": 1}, {"producto_id": a, "qty": 1}])])
    assert not errores, errores
    assert _saldos(abrir_fefo, a) == {"A1": 48} and _saldos(abrir_fefo, b) == {"B1": 48}


def test_en_postgres_una_venta_tambien_espera_a_una_merma_del_mismo_producto(abrir_fefo):
    """El bloqueo de la venta es el mismo de asignar y dar de baja, así que los serializa: la merma de un lote no puede
    leer un saldo que una venta concurrente está consumiendo."""
    _solo_postgres()
    pid = _producto(abrir_fefo, lotes_=(("L1", "2026-10-05", 5),))
    principal = _principal(abrir_fefo)
    resultado: list = []
    merma = core.get_connection()
    try:
        vencimientos.dar_de_baja_lote(merma, pid, principal, "L1", "2026-10-05", 2, clave_operacion="m-1")  # sin commit

        def venta():
            c = core.get_connection()
            try:
                stock.descontar_stock_venta(c, 1, [{"producto_id": pid, "qty": 4}], fecha=FECHA_VENTA)
                c.commit()
                resultado.append("pasó")
            finally:
                c.close()

        hilo = threading.Thread(target=venta)
        hilo.start()
        time.sleep(1.0)
        assert hilo.is_alive() and not resultado, "la venta no esperó a la merma"
        merma.commit()
        hilo.join(10)
    finally:
        merma.close()
    assert resultado == ["pasó"]
    # La venta ve el saldo DESPUÉS de la merma: 3 en L1; vende 4 → 3 de L1 y 1 de faltante.
    assert _saldos(abrir_fefo, pid) == {None: -1}
