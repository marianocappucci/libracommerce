"""Conciliar el saldo «sin lote» negativo heredado de antes de A-4 (ADR-060, 2026-10-14), contra los dos motores.

Antes de A-4 (2026-10-01) las salidas descontaban del bucket «sin lote» aunque la mercadería estuviera en un lote: el «sin lote» quedó NEGATIVO y los lotes
sobreestimados, y `dar_de_baja_lote` se niega (409) a mermar mientras sea así. `conciliacion_sin_lote` propone un reparto FEFO del negativo entre los
lotes con saldo y `conciliar_sin_lote` lo aplica: un PAR aditivo de ajustes por tramo, el stock total no cambia, nunca un `UPDATE`.

Lo que se protege:

- la vista previa es **lectura pura** y reparte en orden FEFO (vencidos primero) sin pasarse de lo que cada lote tiene;
- aplicar deja el «sin lote» en 0 (o lo que no cubren los lotes: un faltante real, que NO se toca), con el **stock total igual** y las filas anteriores
  del ledger intactas;
- es **idempotente** por `clave_operacion`, también cuando ya no queda nada por conciliar;
- los errores (un tramo mayor que su lote, una suma mayor que el negativo, datos inválidos) **no escriben nada**;
- después de conciliar todo, `dar_de_baja_lote` **ya no da 409**.
"""

from __future__ import annotations

import datetime
import threading
import uuid

import pytest
from conftest import USUARIO, url_postgres
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from libracore.db import core

from libracommerce import migrar
from libracommerce.erp import catalogo, stock, vencimientos
from libracommerce.web.vencimientos_router import build_vencimientos_escritura_router, build_vencimientos_router

HOY = datetime.date(2026, 9, 30)
PREVIA = "2026-09-01"
VENCIDO = "2026-09-10"
L1_VENCE = "2026-10-05"
L2_VENCE = "2026-11-01"


def _liberar():
    core._db_path = None
    core._database_url = None


@pytest.fixture(params=["sqlite", "postgres"])
def abrir_conc(request, tmp_path):
    from conftest import _schema_de_producto

    destino = str(tmp_path / "conc.db") if request.param == "sqlite" else url_postgres()
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
    monkeypatch.setattr(core, "_ar_now", lambda: "2026-09-20 12:00:00")
    monkeypatch.setattr(vencimientos, "hoy_argentina", lambda ahora=None: HOY)


# ── Helpers ──────────────────────────────────────────────────────────────


def _k() -> str:
    return uuid.uuid4().hex


def _producto(abrir, nombre="Yogur", *, lotes_=(("L1", L1_VENCE, 6), ("L2", L2_VENCE, 4)), salidas=7, marcado=True,
              deposito=None, variante=None, tipo=None):
    """Un producto con sus lotes `(código, vence, cantidad)` y `salidas` unidades vendidas «sin lote» como las escribía el motor antes de A-4 (el «sin
    lote» queda en `-salidas` y los lotes sobreestimados)."""
    with abrir() as conn:
        pid = catalogo.create_producto(conn, nombre, precio_venta=100.0, precio_costo=60.0)
        if marcado:
            vencimientos.marcar_vence(conn, pid, True)
        for lote, vence, cantidad in lotes_:
            stock.add_movimiento_stock(conn, pid, "entrada", cantidad, "carga", fecha=PREVIA, lot_code=lote, expires_at=vence,
                                       deposito_id=deposito, variant_id=variante)
        if salidas:
            stock.add_movimiento_stock(conn, pid, "venta", -salidas, "Venta ID 1", fecha=PREVIA, deposito_id=deposito, variant_id=variante)
        conn.commit()
    return pid


def _deposito(abrir):
    with abrir() as conn:
        return catalogo.get_default_deposito_id(conn)


def _saldos(abrir, pid, deposito=None, variante=None):
    """`{lote: saldo}` del producto en ese depósito y variante; `None` es el «sin lote»."""
    with abrir() as conn:
        return {f["lote"]: f["saldo"] for f in vencimientos.lotes_de(conn, pid, hoy=HOY, variante_id=variante)
                if deposito is None or f["deposito_id"] == deposito}


def _ledger(abrir):
    """Todo el ledger sin `id` ni `created_at`, en orden de escritura."""
    with abrir() as conn:
        return [tuple(f) for f in conn.execute(
            "SELECT item_id, variant_id, location_id, movement_type, reason_code, quantity_delta, occurred_at, lot_code, expires_at, note, created_by "
            "FROM stock_movements ORDER BY id")]


def _total(abrir, pid):
    with abrir() as conn:
        return stock.get_stock_actual(conn, pid)


def _vista(abrir, pid, deposito=1, variante=None):
    with abrir() as conn:
        return vencimientos.conciliacion_sin_lote(conn, pid, deposito, variante, hoy=HOY)


def _conciliar(abrir, pid, reparto, deposito=1, **kw):
    kw.setdefault("clave_operacion", _k())
    kw.setdefault("usuario_id", USUARIO["id"])
    with abrir() as conn:
        r = vencimientos.conciliar_sin_lote(conn, pid, deposito, reparto, **kw)
        conn.commit()
    return r


def _t(lote, vence, cantidad):
    return {"lote": lote, "vence": vence, "cantidad": cantidad}


# ═══════════════════════════════════════════════════ La vista previa


def test_la_vista_previa_reparte_fefo_entre_los_lotes_con_saldo(abrir_conc):
    pid = _producto(abrir_conc)                                   # L1 6, L2 4, sin lote −7
    antes = _ledger(abrir_conc)
    v = _vista(abrir_conc, pid)
    assert (v["saldo_sin_lote"], v["a_conciliar"]) == (-7, 7)
    assert v["propuesta"] == [{"lote": "L1", "vence": L1_VENCE, "cantidad": 6}, {"lote": "L2", "vence": L2_VENCE, "cantidad": 1}]
    assert (v["cubierto"], v["sin_cubrir"]) == (7, 0)
    assert [(x["lote"], x["saldo"], x["estado"]) for x in v["lotes"]] == [("L1", 6, "vigente"), ("L2", 4, "vigente")]
    assert (v["producto_id"], v["nombre"], v["unidad"], v["vence"], v["deposito_id"], v["variante_id"]) == (pid, "Yogur", "u", True, 1, None)
    assert _ledger(abrir_conc) == antes, "la vista previa escribió"


def test_los_lotes_vencidos_se_reparten_primero(abrir_conc):
    pid = _producto(abrir_conc, lotes_=(("NUEVO", L2_VENCE, 5), ("VIEJO", VENCIDO, 3)), salidas=4)
    v = _vista(abrir_conc, pid)
    assert v["propuesta"] == [{"lote": "VIEJO", "vence": VENCIDO, "cantidad": 3}, {"lote": "NUEVO", "vence": L2_VENCE, "cantidad": 1}]
    assert [x["estado"] for x in v["lotes"]] == ["vencido", "vigente"]


def test_con_lotes_que_no_alcanzan_dice_cuanto_queda_sin_cubrir(abrir_conc):
    pid = _producto(abrir_conc, lotes_=(("L1", L1_VENCE, 2),), salidas=7)         # sin lote −7, pero el único lote tiene 2
    v = _vista(abrir_conc, pid)
    assert (v["a_conciliar"], v["cubierto"], v["sin_cubrir"]) == (7, 2, 5)
    assert v["propuesta"] == [{"lote": "L1", "vence": L1_VENCE, "cantidad": 2}]


def test_sin_saldo_negativo_no_hay_nada_que_proponer(abrir_conc):
    pid = _producto(abrir_conc, salidas=0)
    v = _vista(abrir_conc, pid)
    assert (v["saldo_sin_lote"], v["a_conciliar"], v["propuesta"], v["sin_cubrir"]) == (0, 0, [], 0)


def test_la_vista_previa_valida_producto_deposito_y_variante(abrir_conc):
    pid = _producto(abrir_conc)
    otro = _producto(abrir_conc, "Leche", salidas=0)
    with abrir_conc() as conn:
        variante_ajena = catalogo.create_variante(conn, otro, "O-1", "Chico")["id"]
        conn.commit()
        with pytest.raises(vencimientos.ProductoNoEncontrado):
            vencimientos.conciliacion_sin_lote(conn, 9999, 1)
        with pytest.raises(ValueError, match="depósito"):
            vencimientos.conciliacion_sin_lote(conn, pid, 9999)
        with pytest.raises(ValueError, match="variante"):
            vencimientos.conciliacion_sin_lote(conn, pid, 1, variante_ajena)


# ═══════════════════════════════════════════════════ Aplicar


def test_conciliar_todo_deja_el_sin_lote_en_cero_con_el_stock_total_igual_y_filas_nuevas(abrir_conc):
    pid = _producto(abrir_conc)
    total = _total(abrir_conc, pid)
    antes = _ledger(abrir_conc)
    r = _conciliar(abrir_conc, pid, _vista(abrir_conc, pid)["propuesta"], nota="inventario de octubre", fecha="2026-09-25")
    assert (r["cantidad"], r["saldo_sin_lote"], r["sin_cubrir"], r["repetida"]) == (7, 0, 0, False)
    assert r["tramos"] == [_t("L1", L1_VENCE, 6), _t("L2", L2_VENCE, 1)]
    assert _saldos(abrir_conc, pid) == {"L2": 3}                         # L1 quedó en 0 y el sin lote también (los saldos en 0 no figuran)
    assert _total(abrir_conc, pid) == total                              # el stock total no cambia
    nuevas = _ledger(abrir_conc)
    assert nuevas[:len(antes)] == antes, "se modificó o reordenó una fila ya escrita"
    pares = nuevas[len(antes):]
    assert len(pares) == 4                                               # 2 tramos × (+sin lote, −lote)
    assert [(p[3], p[4], p[5], p[7], p[8]) for p in pares] == [
        ("adjustment", "ajuste", 6.0, None, None), ("adjustment", "ajuste", -6.0, "L1", L1_VENCE),
        ("adjustment", "ajuste", 1.0, None, None), ("adjustment", "ajuste", -1.0, "L2", L2_VENCE)]
    assert {p[6] for p in pares} == {"2026-09-25T00:00:00"} and {p[10] for p in pares} == {USUARIO["id"]}   # misma fecha y mismo usuario
    referencia = r["referencia"]
    assert referencia.startswith("inventario de octubre [conciliación ") and referencia.endswith("]")
    assert all(p[9].startswith(referencia) for p in pares) and all("[op:" in p[9] for p in pares)


def test_sin_nota_la_referencia_es_conciliacion_sin_lote(abrir_conc):
    pid = _producto(abrir_conc)
    r = _conciliar(abrir_conc, pid, [_t("L1", L1_VENCE, 2)])
    assert r["referencia"].startswith("Conciliación sin lote [conciliación ")


def test_conciliar_solo_una_parte_deja_el_resto_negativo_porque_es_un_faltante_real(abrir_conc):
    pid = _producto(abrir_conc)                                          # sin lote −7
    r = _conciliar(abrir_conc, pid, [_t("L1", L1_VENCE, 4)])
    assert (r["cantidad"], r["saldo_sin_lote"], r["sin_cubrir"]) == (4, -3, 3)
    assert _saldos(abrir_conc, pid) == {"L1": 2, "L2": 4, None: -3}


def test_lo_que_los_lotes_no_cubren_no_se_toca(abrir_conc):
    pid = _producto(abrir_conc, lotes_=(("L1", L1_VENCE, 2),), salidas=7)
    r = _conciliar(abrir_conc, pid, _vista(abrir_conc, pid)["propuesta"])
    assert (r["saldo_sin_lote"], r["sin_cubrir"]) == (-5, 5)
    assert _saldos(abrir_conc, pid) == {None: -5}                         # L1 quedó en 0; el −5 es un faltante que corrige un conteo físico


def test_un_lote_con_solo_vencimiento_o_solo_codigo(abrir_conc):
    pid = _producto(abrir_conc, lotes_=((None, L1_VENCE, 3), ("SINFECHA", None, 3)), salidas=5)
    r = _conciliar(abrir_conc, pid, [_t(None, L1_VENCE, 3), _t("SINFECHA", None, 2)])
    assert r["saldo_sin_lote"] == 0
    assert _saldos(abrir_conc, pid) == {"SINFECHA": 1}


def test_cada_variante_y_cada_deposito_se_concilian_por_separado(abrir_conc):
    with abrir_conc() as conn:
        norte = catalogo.create_deposito(conn, "Norte")
        conn.commit()
    pid = _producto(abrir_conc)                                          # principal: sin lote −7
    with abrir_conc() as conn:
        stock.add_movimiento_stock(conn, pid, "entrada", 5, "carga", fecha=PREVIA, lot_code="N1", expires_at=L1_VENCE, deposito_id=norte)
        stock.add_movimiento_stock(conn, pid, "venta", -2, "Venta", fecha=PREVIA, deposito_id=norte)
        conn.commit()
    r = _conciliar(abrir_conc, pid, [_t("N1", L1_VENCE, 2)], deposito=norte)
    assert r["saldo_sin_lote"] == 0
    assert _saldos(abrir_conc, pid, deposito=norte) == {"N1": 3}
    assert _saldos(abrir_conc, pid, deposito=_deposito(abrir_conc)) == {"L1": 6, "L2": 4, None: -7}   # el principal, intacto
    with abrir_conc() as conn, pytest.raises(vencimientos.SaldoInsuficiente):     # N1 no existe en el principal
        vencimientos.conciliar_sin_lote(conn, pid, _deposito(abrir_conc), [_t("N1", L1_VENCE, 1)], clave_operacion=_k())


def test_la_variante_se_concilia_en_su_propio_bucket(abrir_conc):
    with abrir_conc() as conn:
        pid = catalogo.create_producto(conn, "Remera", precio_venta=100.0, precio_costo=60.0)
        vencimientos.marcar_vence(conn, pid, True)
        roja = catalogo.create_variante(conn, pid, "R-1", "Roja")["id"]
        azul = catalogo.create_variante(conn, pid, "R-2", "Azul")["id"]
        for v in (roja, azul):
            stock.add_movimiento_stock(conn, pid, "entrada", 5, "carga", fecha=PREVIA, lot_code="L1", expires_at=L1_VENCE, variant_id=v)
            stock.add_movimiento_stock(conn, pid, "venta", -3, "Venta", fecha=PREVIA, variant_id=v)
        conn.commit()
    assert _vista(abrir_conc, pid, 1, roja)["a_conciliar"] == 3
    assert _vista(abrir_conc, pid, 1, None)["a_conciliar"] == 0           # el producto sin variante no tiene nada
    _conciliar(abrir_conc, pid, [_t("L1", L1_VENCE, 3)], variante_id=roja)
    assert _saldos(abrir_conc, pid, variante=roja) == {"L1": 2}
    assert _saldos(abrir_conc, pid, variante=azul) == {"L1": 5, None: -3}  # la otra variante, intacta


# ═══════════════════════════════════════════════════ Idempotencia


def test_un_reintento_con_la_misma_clave_no_escribe_y_devuelve_lo_de_la_primera_vez(abrir_conc):
    pid = _producto(abrir_conc)
    clave = _k()
    reparto = _vista(abrir_conc, pid)["propuesta"]
    primera = _conciliar(abrir_conc, pid, reparto, clave_operacion=clave, nota="octubre")
    despues = _ledger(abrir_conc)
    repetida = _conciliar(abrir_conc, pid, list(reversed(reparto)), clave_operacion=clave, nota="octubre")      # el orden de los tramos no importa
    assert repetida["repetida"] is True
    assert {k: repetida[k] for k in repetida if k != "repetida"} == {k: primera[k] for k in primera if k != "repetida"}
    assert repetida["saldo_sin_lote"] == 0
    assert _ledger(abrir_conc) == despues, "el reintento escribió"


def test_el_reintento_funciona_aunque_ya_no_quede_nada_por_conciliar_y_aunque_hayan_pasado_cosas(abrir_conc):
    pid = _producto(abrir_conc)
    clave = _k()
    reparto = _vista(abrir_conc, pid)["propuesta"]
    _conciliar(abrir_conc, pid, reparto, clave_operacion=clave)
    with abrir_conc() as conn:                                            # después de conciliar, el producto se vende y se desmarca
        stock.add_movimiento_stock(conn, pid, "entrada", 3, "carga", fecha=PREVIA, lot_code="L9", expires_at=L2_VENCE)
        vencimientos.marcar_vence(conn, pid, False)
        conn.commit()
    r = _conciliar(abrir_conc, pid, reparto, clave_operacion=clave)
    assert r["repetida"] is True and r["saldo_sin_lote"] == 0            # el saldo «tras la primera vez», no el de hoy


def test_la_misma_clave_con_otro_reparto_u_otra_operacion_es_409(abrir_conc):
    pid = _producto(abrir_conc)
    clave = _k()
    _conciliar(abrir_conc, pid, [_t("L1", L1_VENCE, 3)], clave_operacion=clave)
    antes = _ledger(abrir_conc)
    with abrir_conc() as conn:
        for reparto in ([_t("L1", L1_VENCE, 2)], [_t("L2", L2_VENCE, 3)], [_t("L1", L1_VENCE, 3), _t("L2", L2_VENCE, 1)]):
            with pytest.raises(vencimientos.ClaveDeOperacionReusada):
                vencimientos.conciliar_sin_lote(conn, pid, 1, reparto, clave_operacion=clave)
    with abrir_conc() as conn:
        norte = catalogo.create_deposito(conn, "Norte")
        conn.commit()
        with pytest.raises(vencimientos.ClaveDeOperacionReusada):             # otro depósito
            vencimientos.conciliar_sin_lote(conn, pid, norte, [_t("L1", L1_VENCE, 3)], clave_operacion=clave)
    assert _ledger(abrir_conc) == antes


def test_una_clave_de_asignar_no_se_puede_reusar_para_conciliar_ni_al_reves(abrir_conc):
    pid = _producto(abrir_conc)
    with abrir_conc() as conn:
        stock.add_movimiento_stock(conn, pid, "entrada", 10, "carga", fecha=PREVIA)                # sin lote queda en +3
        conn.commit()
    clave_asignar = _k()
    with abrir_conc() as conn:
        vencimientos.asignar_vencimiento_a_saldo(conn, pid, 1, "A1", "2026-12-01", 1, clave_operacion=clave_asignar)
        conn.commit()
    with abrir_conc() as conn, pytest.raises(vencimientos.ClaveDeOperacionReusada):
        vencimientos.conciliar_sin_lote(conn, pid, 1, [_t("L1", L1_VENCE, 1)], clave_operacion=clave_asignar)


def test_la_misma_clave_sobre_otro_producto_es_otra_operacion(abrir_conc):
    a, b = _producto(abrir_conc, "A"), _producto(abrir_conc, "B")
    clave = _k()
    assert _conciliar(abrir_conc, a, [_t("L1", L1_VENCE, 1)], clave_operacion=clave)["repetida"] is False
    assert _conciliar(abrir_conc, b, [_t("L1", L1_VENCE, 1)], clave_operacion=clave)["repetida"] is False


# ═══════════════════════════════════════════════════ Errores: ninguno escribe


def _no_escribe(abrir, pid, excepcion, reparto, **kw):
    antes = _ledger(abrir)
    with abrir() as conn, pytest.raises(excepcion) as e:
        vencimientos.conciliar_sin_lote(conn, pid, kw.pop("deposito", 1), reparto, clave_operacion=_k(), **kw)
    assert _ledger(abrir) == antes, "una conciliación que falló escribió algo"
    return e.value


def test_un_tramo_mayor_que_el_saldo_de_su_lote_es_error_y_no_escribe(abrir_conc):
    pid = _producto(abrir_conc)                                          # L1 tiene 6, el negativo es 7
    e = _no_escribe(abrir_conc, pid, vencimientos.SaldoInsuficiente, [_t("L2", L2_VENCE, 1), _t("L1", L1_VENCE, 6.5)])
    assert "L1" in str(e) and "6" in str(e)
    _no_escribe(abrir_conc, pid, vencimientos.SaldoInsuficiente, [_t("NOEXISTE", L1_VENCE, 1)])     # un lote que no existe tiene saldo 0


def test_una_suma_mayor_que_el_negativo_es_error_y_no_escribe(abrir_conc):
    pid = _producto(abrir_conc, salidas=3)                               # el negativo es 3; L1 (6) y L2 (4) alcanzan para más
    e = _no_escribe(abrir_conc, pid, vencimientos.ReglaDeNegocio, [_t("L1", L1_VENCE, 2), _t("L2", L2_VENCE, 2)])
    assert "suma 4" in str(e) and "3" in str(e)
    _conciliar(abrir_conc, pid, [_t("L1", L1_VENCE, 3)])                 # justo el negativo, sí


def test_sin_saldo_negativo_no_hay_nada_que_conciliar(abrir_conc):
    pid = _producto(abrir_conc, salidas=0)
    assert "nada que conciliar" in str(_no_escribe(abrir_conc, pid, vencimientos.ReglaDeNegocio, [_t("L1", L1_VENCE, 1)]))
    pid2 = _producto(abrir_conc, "Otro")
    _conciliar(abrir_conc, pid2, [_t("L1", L1_VENCE, 6), _t("L2", L2_VENCE, 1)])
    _no_escribe(abrir_conc, pid2, vencimientos.ReglaDeNegocio, [_t("L2", L2_VENCE, 1)])      # una segunda conciliación con otra clave: ya no queda nada


def test_solo_un_producto_marcado_con_inventario(abrir_conc):
    sin_marcar = _producto(abrir_conc, "Sin marcar", marcado=False)
    _no_escribe(abrir_conc, sin_marcar, vencimientos.ReglaDeNegocio, [_t("L1", L1_VENCE, 1)])
    with abrir_conc() as conn:
        servicio = catalogo.create_producto(conn, "Envío", precio_venta=100.0, precio_costo=0, tipo="servicio")
        conn.commit()
    _no_escribe(abrir_conc, servicio, vencimientos.ReglaDeNegocio, [_t("L1", L1_VENCE, 1)])
    with abrir_conc() as conn:         # un marcado que después pasó a servicio
        viejo = catalogo.create_producto(conn, "Antes producto", precio_venta=100.0, precio_costo=0)
        vencimientos.marcar_vence(conn, viejo, True)
        conn.execute("UPDATE catalog_items SET item_type = 'service' WHERE id = ?", (viejo,))
        conn.commit()
    assert "servicio" in str(_no_escribe(abrir_conc, viejo, vencimientos.ReglaDeNegocio, [_t("L1", L1_VENCE, 1)]))
    _no_escribe(abrir_conc, 9999, vencimientos.ProductoNoEncontrado, [_t("L1", L1_VENCE, 1)])


@pytest.mark.parametrize("reparto", [
    [], None, "L1", [None], [{"lote": "L1", "vence": L1_VENCE}], [{"lote": "L1", "vence": L1_VENCE, "cantidad": 0}],
    [{"lote": "L1", "vence": L1_VENCE, "cantidad": -1}], [{"lote": "L1", "vence": L1_VENCE, "cantidad": "mucho"}],
    [{"lote": "L1", "vence": L1_VENCE, "cantidad": True}], [{"cantidad": 1}], [{"lote": None, "vence": None, "cantidad": 1}],
    [{"lote": "", "vence": L1_VENCE, "cantidad": 1}], [{"lote": "L1", "vence": "ayer", "cantidad": 1}],
    [{"lote": "L1", "vence": L1_VENCE, "cantidad": 1}, {"lote": "L1", "vence": L1_VENCE, "cantidad": 1}],
])
def test_un_reparto_mal_formado_es_value_error_y_no_escribe(abrir_conc, reparto):
    pid = _producto(abrir_conc)
    _no_escribe(abrir_conc, pid, ValueError, reparto)


def test_depósito_variante_y_clave_invalidos_son_value_error_y_no_escriben(abrir_conc):
    pid = _producto(abrir_conc)
    otro = _producto(abrir_conc, "Leche", salidas=0)
    with abrir_conc() as conn:
        ajena = catalogo.create_variante(conn, otro, "O-1", "Chico")["id"]
        conn.commit()
    _no_escribe(abrir_conc, pid, ValueError, [_t("L1", L1_VENCE, 1)], deposito=9999)
    _no_escribe(abrir_conc, pid, ValueError, [_t("L1", L1_VENCE, 1)], variante_id=ajena)
    antes = _ledger(abrir_conc)
    for clave in ("", "  ", "a[b]", "x" * 65, None):
        with abrir_conc() as conn, pytest.raises(ValueError):
            vencimientos.conciliar_sin_lote(conn, pid, 1, [_t("L1", L1_VENCE, 1)], clave_operacion=clave)
    assert _ledger(abrir_conc) == antes


# ═══════════════════════════════════════════════════ El cierre: la baja del lote vuelve a andar


def test_antes_de_conciliar_la_baja_da_409_y_despues_ya_no(abrir_conc):
    pid = _producto(abrir_conc)
    with abrir_conc() as conn, pytest.raises(vencimientos.ReglaDeNegocio, match="salidas sin lote sin conciliar"):
        vencimientos.dar_de_baja_lote(conn, pid, 1, "L1", L1_VENCE, 1, clave_operacion=_k())
    _conciliar(abrir_conc, pid, _vista(abrir_conc, pid)["propuesta"])
    with abrir_conc() as conn:
        r = vencimientos.dar_de_baja_lote(conn, pid, 1, "L2", L2_VENCE, 2, clave_operacion=_k())
        conn.commit()
    assert r["saldo_restante"] == 1


def test_una_conciliacion_parcial_mantiene_la_guarda_de_la_baja(abrir_conc):
    pid = _producto(abrir_conc)
    _conciliar(abrir_conc, pid, [_t("L1", L1_VENCE, 4)])                 # queda −3
    with abrir_conc() as conn, pytest.raises(vencimientos.ReglaDeNegocio, match="salidas sin lote sin conciliar"):
        vencimientos.dar_de_baja_lote(conn, pid, 1, "L2", L2_VENCE, 1, clave_operacion=_k())


def test_la_conciliacion_cambia_el_reporte_de_proximos_a_vencer(abrir_conc):
    """Los lotes dejan de estar sobreestimados y el producto sale de «salidas sin lote»."""
    pid = _producto(abrir_conc)
    with abrir_conc() as conn:
        antes = vencimientos.proximos_a_vencer(conn, dias=60, hoy=HOY)
    assert [r["situacion"] for r in antes["sin_lote"]] == ["salidas_sin_lote"] and antes["resumen"]["unidades_por_vencer"] == 10
    _conciliar(abrir_conc, pid, _vista(abrir_conc, pid)["propuesta"])
    with abrir_conc() as conn:
        despues = vencimientos.proximos_a_vencer(conn, dias=60, hoy=HOY)
    assert despues["sin_lote"] == [] and despues["resumen"]["unidades_por_vencer"] == 3


# ═══════════════════════════════════════════════════ Concurrencia (PostgreSQL)


def test_en_postgres_dos_conciliaciones_simultaneas_no_concilian_dos_veces(abrir_conc):
    if not core.is_postgres():
        pytest.skip("sólo PostgreSQL toma el bloqueo por fila")
    pid = _producto(abrir_conc)
    resultados: list = []
    errores: list = []

    def correr():
        c = core.get_connection()
        try:
            try:
                resultados.append(vencimientos.conciliar_sin_lote(c, pid, 1, [_t("L1", L1_VENCE, 6), _t("L2", L2_VENCE, 1)], clave_operacion=_k()))
                c.commit()
            except vencimientos.ReglaDeNegocio as e:
                c.rollback()
                errores.append(e)
        finally:
            c.close()

    hilos = [threading.Thread(target=correr) for _ in range(2)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(30)
    assert not any(h.is_alive() for h in hilos)
    assert len(resultados) == 1 and len(errores) == 1 and "nada que conciliar" in str(errores[0])
    assert _saldos(abrir_conc, pid) == {"L2": 3}                          # se concilió una sola vez


# ═══════════════════════════════════════════════════ El router


def _libre():
    """Un gate que deja pasar."""


def _app(abrir, **kw):
    kw.setdefault("dependencias_marcar", [Depends(_libre)])
    kw.setdefault("dependencias_movimientos", [Depends(_libre)])
    app = FastAPI()
    app.include_router(build_vencimientos_router(conexion=abrir, dependencias_conciliacion=kw["dependencias_movimientos"]))
    app.include_router(build_vencimientos_escritura_router(conexion=abrir, usuario_actual=lambda: USUARIO, **kw))
    return TestClient(app)


def test_get_conciliacion_y_post_conciliar(abrir_conc):
    pid = _producto(abrir_conc)
    c = _app(abrir_conc)
    v = c.get("/api/vencimientos/conciliacion", params={"producto_id": pid, "deposito_id": 1})
    assert v.status_code == 200, v.text
    assert v.json()["a_conciliar"] == 7 and v.json()["propuesta"][0] == {"lote": "L1", "vence": L1_VENCE, "cantidad": 6}
    body = {"producto_id": pid, "deposito_id": 1, "reparto": v.json()["propuesta"], "clave_operacion": "uuid-1", "nota": "octubre"}
    r = c.post("/api/vencimientos/conciliar", json=body)
    assert r.status_code == 200, r.text
    assert (r.json()["cantidad"], r.json()["saldo_sin_lote"], r.json()["repetida"]) == (7, 0, False)
    r2 = c.post("/api/vencimientos/conciliar", json=body)                  # el reintento
    assert r2.status_code == 200 and r2.json()["repetida"] is True and r2.json()["referencia"] == r.json()["referencia"]
    assert c.post("/api/vencimientos/conciliar", json={**body, "reparto": [_t("L1", L1_VENCE, 1)]}).status_code == 409    # misma clave, otro reparto
    assert c.get("/api/vencimientos/conciliacion", params={"producto_id": pid, "deposito_id": 1}).json()["a_conciliar"] == 0
    # El created_by es el usuario de la sesión.
    with abrir_conc() as conn:
        usuarios = {f[0] for f in conn.execute("SELECT created_by FROM stock_movements WHERE note LIKE '%conciliación%'")}
    assert usuarios == {USUARIO["id"]}


def test_los_errores_del_router(abrir_conc):
    pid = _producto(abrir_conc)
    c = _app(abrir_conc)
    ok = {"producto_id": pid, "deposito_id": 1, "reparto": [_t("L1", L1_VENCE, 1)], "clave_operacion": _k()}
    assert c.post("/api/vencimientos/conciliar", json={**ok, "producto_id": 9999}).status_code == 404
    assert c.post("/api/vencimientos/conciliar", json={**ok, "reparto": [_t("L1", L1_VENCE, 99)]}).status_code == 409      # el lote no alcanza
    assert c.post("/api/vencimientos/conciliar", json={**ok, "reparto": [_t("L1", L1_VENCE, 6), _t("L2", L2_VENCE, 2)]}).status_code == 409   # suma de más
    for malo in ({"reparto": []}, {"reparto": [{"lote": "L1", "vence": L1_VENCE, "cantidad": 0}]}, {"reparto": [{"cantidad": 1}]},
                 {"clave_operacion": ""}, {"deposito_id": 9999}, {"deposito_id": True}, {"producto_id": True},
                 {"reparto": [_t("L1", L1_VENCE, 1), _t("L1", L1_VENCE, 1)]}):
        assert c.post("/api/vencimientos/conciliar", json={**ok, **malo}).status_code == 422, malo
    assert c.get("/api/vencimientos/conciliacion", params={"producto_id": 9999, "deposito_id": 1}).status_code == 404
    assert c.get("/api/vencimientos/conciliacion", params={"producto_id": pid, "deposito_id": 9999}).status_code == 422
    assert c.get("/api/vencimientos/conciliacion", params={"producto_id": pid}).status_code == 422
    assert _saldos(abrir_conc, pid) == {"L1": 6, "L2": 4, None: -7}      # nada de lo anterior escribió


def test_el_gate_de_los_movimientos_cubre_la_conciliacion(abrir_conc):
    pid = _producto(abrir_conc)

    def rol(*permitidos):
        def gate(x_rol: str = Header("")):
            if x_rol not in permitidos:
                raise HTTPException(403, "sin permiso")
        return Depends(gate)

    c = _app(abrir_conc, dependencias_marcar=[rol("encargado")], dependencias_movimientos=[rol("encargado", "deposito")])
    ok = {"producto_id": pid, "deposito_id": 1, "reparto": [_t("L1", L1_VENCE, 1)], "clave_operacion": _k()}
    params = {"producto_id": pid, "deposito_id": 1}
    assert c.get("/api/vencimientos/conciliacion", params=params, headers={"x-rol": "cajero"}).status_code == 403
    assert c.post("/api/vencimientos/conciliar", json=ok, headers={"x-rol": "cajero"}).status_code == 403
    assert c.get("/api/vencimientos/conciliacion", params=params, headers={"x-rol": "deposito"}).status_code == 200
    assert c.post("/api/vencimientos/conciliar", json=ok, headers={"x-rol": "deposito"}).status_code == 200


def test_el_router_de_lectura_sigue_siendo_solo_de_lectura_y_sin_el_gate_opcional_no_pide_nada_extra(abrir_conc):
    lectura = build_vencimientos_router(conexion=abrir_conc)
    assert {m for r in lectura.routes for m in r.methods} == {"GET"}
    pid = _producto(abrir_conc)
    app = FastAPI()
    app.include_router(lectura)
    assert TestClient(app).get("/api/vencimientos/conciliacion", params={"producto_id": pid, "deposito_id": 1}).status_code == 200
