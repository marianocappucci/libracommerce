"""Carga de vencimientos (ADR-018, nota del 2026-09-30), contra los dos motores.

Dos piezas del motor de la etapa, las dos aditivas y sin tocar el camino de ventas:

- **`registrar_entrada_con_lote`** y `POST /api/vencimientos/entrada`: una entrada manual de stock NUEVO con lote y
  vencimiento (no mueve el «sin lote»: suma stock real), con la misma idempotencia por `(producto, clave)` que
  asignar y dar de baja;
- **la marca `vence` en el producto** (`OpcionesCatalogo.con_vencimientos`, opt-in): apagada, las respuestas de
  productos son byte a byte las de siempre; prendida, listado, escaneo, alta y edición la devuelven y el alta y la
  edición la pueden cambiar, con un gancho de autorización que decide antes de escribir.

Las fixtures y los helpers son los de `tests/test_vencimientos.py`.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
import test_vencimientos as _vto
from conftest import USUARIO
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_vencimientos import (
    HOY,
    PREVIA,
    _app,
    _asignar,
    _baja,
    _dias,
    _dos_productos_a_la_vez,
    _dos_reintentos_a_la_vez,
    _entrada,
    _hoy_fijo,
    _k,
    _libre,
    _lotes,
    _producto,
    _proximos,
    _todo_el_ledger,
)

from libracommerce.erp import catalogo, stock, vencimientos
from libracommerce.web.catalogo_router import OpcionesCatalogo, build_productos_router
from libracommerce.web.vencimientos_router import build_vencimientos_escritura_router

# Las fixtures de `tests/test_vencimientos.py` (una base por motor, con la revisión 0002 aplicada).
abrir_vto = _vto.abrir_vto
destino = _vto.destino


def _con_lote(conn, *args, **kw):
    kw.setdefault("clave_operacion", _k())
    return vencimientos.registrar_entrada_con_lote(conn, *args, **kw)


def _sin_id(filas):
    return [tuple(f) for f in filas]


def _buckets(abrir, pid, **kw):
    return [(f["lote"], f["vence"], f["saldo"]) for f in _lotes(abrir, pid, **kw)]


# ═══════════════════════════════════════════════════ La entrada con lote (motor)


def test_la_entrada_con_lote_suma_stock_real_en_una_sola_fila_y_no_toca_el_sin_lote(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn, "Yogur")
        deposito = catalogo.get_default_deposito_id(conn)
        _entrada(conn, pid, 6)                                        # sin lote
        _entrada(conn, pid, 10, lote="L1", vence=_dias(5))
        antes = _sin_id(_todo_el_ledger(conn))
        total = stock.get_stock_actual(conn, pid)
        r = _con_lote(conn, pid, deposito, "  L2  ", _dias(9), Decimal("4"), usuario_id=USUARIO["id"], nota="Conteo",
                      clave_operacion="clave-1")
        despues = _sin_id(_todo_el_ledger(conn))
        assert stock.get_stock_actual(conn, pid) == total + 4
    assert r == {"producto_id": pid, "deposito_id": deposito, "variante_id": None, "lote": "L2", "vence": _dias(9),
                 "cantidad": 4, "referencia": "Conteo", "saldo_lote": 4, "repetida": False}
    # Una sola fila nueva (no un par), positiva, con el lote y el vencimiento normalizados; las viejas, intactas.
    assert despues[:len(antes)] == antes and len(despues) == len(antes) + 1
    with abrir_vto() as conn:
        fila = conn.execute(
            "SELECT item_id, variant_id, location_id, movement_type, reason_code, quantity_delta, lot_code, "
            "expires_at, created_by, note FROM stock_movements ORDER BY id DESC LIMIT 1").fetchone()
    assert (fila[0], fila[1], fila[2], fila[3], fila[4]) == (pid, None, deposito, "adjustment", "entrada")
    assert (float(fila[5]), fila[6], fila[7], fila[8]) == (4.0, "L2", _dias(9), USUARIO["id"])
    assert fila[9] == f"Conteo: lote L2, vence {_dias(9)} [op:clave-1]"
    # El «sin lote» no cambió, el lote nuevo aparece y el resto de los lotes sigue igual.
    assert _buckets(abrir_vto, pid) == [("L1", _dias(5), 10), ("L2", _dias(9), 4), (None, None, 6)]


def test_la_entrada_con_lote_aparece_en_el_reporte_de_proximos_a_vencer(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn, "Yogur")
        deposito = catalogo.get_default_deposito_id(conn)
        _con_lote(conn, pid, deposito, "L7", _dias(3), 5)
    reporte = _proximos(abrir_vto)
    assert [(r["lote"], r["vence"], r["saldo"], r["estado"]) for r in reporte["lotes"]] == [
        ("L7", _dias(3), 5, "por_vencer")]
    assert reporte["sin_lote"] == []        # no inventó un saldo sin lote


def test_mismo_codigo_y_misma_fecha_suman_al_bucket_y_otra_fecha_es_otro_bucket(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        a = _con_lote(conn, pid, deposito, "L1", _dias(5), 4)
        b = _con_lote(conn, pid, deposito, "L1", _dias(5), 3)       # mismo par: suma
        c = _con_lote(conn, pid, deposito, "L1", _dias(20), 2)      # mismo código, otra fecha: otro bucket
        d = _con_lote(conn, pid, deposito, "L1", _dias(5) + "T10:30:00", 1)   # la misma fecha escrita de otra forma
    assert (a["saldo_lote"], b["saldo_lote"], c["saldo_lote"], d["saldo_lote"]) == (4, 7, 2, 8)
    assert _buckets(abrir_vto, pid) == [("L1", _dias(5), 8), ("L1", _dias(20), 2)]


def test_la_entrada_con_lote_respeta_deposito_y_variante(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn)
        v1 = catalogo.create_variante(conn, pid, "Y-1", "Frutilla")["id"]
        v2 = catalogo.create_variante(conn, pid, "Y-2", "Durazno")["id"]
        d1 = catalogo.get_default_deposito_id(conn)
        d2 = catalogo.create_deposito(conn, "Cámara")
        _con_lote(conn, pid, d1, "L1", _dias(5), 4, variante_id=v1)
        _con_lote(conn, pid, d1, "L1", _dias(5), 2, variante_id=v2)
        r = _con_lote(conn, pid, d2, "L1", _dias(5), 7, variante_id=v1)
        assert r["saldo_lote"] == 7 and r["variante_id"] == v1
        assert stock.get_stock_actual(conn, pid, d1, v1) == 4
        assert stock.get_stock_actual(conn, pid, d2, v1) == 7
    assert sorted((f["deposito_id"], f["variante_id"], f["saldo"]) for f in _lotes(abrir_vto, pid)) == sorted([
        (d1, v1, 4), (d1, v2, 2), (d2, v1, 7)])


def test_la_fecha_y_la_referencia_por_defecto(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        r = _con_lote(conn, pid, deposito, "L1", _dias(5), 1, fecha="2026-09-01")
        nota, cuando = conn.execute("SELECT note, occurred_at FROM stock_movements ORDER BY id DESC LIMIT 1").fetchone()
    assert r["referencia"] == "Entrada con lote" and nota.startswith("Entrada con lote: lote L1, vence ")
    assert str(cuando).startswith("2026-09-01")


def test_la_entrada_con_lote_valida_antes_de_escribir(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn)
        b = catalogo.create_producto(conn, "Zapatilla", precio_venta=1.0, precio_costo=1.0)
        vb = catalogo.create_variante(conn, b, "Z-1", "Talle 40")["id"]
        sin_marca = _producto(conn, "Sin marca", vence=False)
        servicio = catalogo.create_producto(conn, "Flete", precio_venta=1.0, tipo="servicio")
        deposito = catalogo.get_default_deposito_id(conn)
        antes = _sin_id(_todo_el_ledger(conn))
        clave = _k()
        for malo in (0, -1, "x", True, None, Decimal("NaN"), Decimal("Infinity"), "1e999999"):
            with pytest.raises(ValueError, match="cantidad"):
                _con_lote(conn, pid, deposito, "L1", _dias(5), malo)
        for lote in ("", "   ", None, 7, "L" * 65):
            with pytest.raises(ValueError, match="lot_code"):
                _con_lote(conn, pid, deposito, lote, _dias(5), 1)
        for fecha in ("", "mañana", "2026-13-01", 20261005, None):
            with pytest.raises(ValueError, match="expires_at"):
                _con_lote(conn, pid, deposito, "L1", fecha, 1)
        for clave_mala in ("", "  ", "x" * 65, "a[b", None, 5):
            with pytest.raises(ValueError, match="clave_operacion"):
                _con_lote(conn, pid, deposito, "L1", _dias(5), 1, clave_operacion=clave_mala)
        with pytest.raises(ValueError, match="depósito 999 no existe"):
            _con_lote(conn, pid, 999, "L1", _dias(5), 1)
        with pytest.raises(ValueError, match=f"variante {vb} no existe o no es del producto {pid}"):
            _con_lote(conn, pid, deposito, "L1", _dias(5), 1, variante_id=vb)
        with pytest.raises(vencimientos.ProductoNoEncontrado):
            _con_lote(conn, 9999, deposito, "L1", _dias(5), 1)
        with pytest.raises(vencimientos.ReglaDeNegocio, match="no está marcado"):
            _con_lote(conn, sin_marca, deposito, "L1", _dias(5), 1)
        with pytest.raises(vencimientos.ReglaDeNegocio):
            _con_lote(conn, servicio, deposito, "L1", _dias(5), 1)
        with pytest.raises(TypeError):
            vencimientos.registrar_entrada_con_lote(conn, pid, deposito, "L1", _dias(5), 1)   # la clave es obligatoria
        assert _sin_id(_todo_el_ledger(conn)) == antes
        # Y la clave de una operación rechazada no se gasta.
        assert _con_lote(conn, pid, deposito, "L1", _dias(5), 1, clave_operacion=clave)["repetida"] is False


def test_la_cantidad_respeta_la_escala_de_la_unidad(abrir_vto):
    """Es stock NUEVO: un `2.5` de una unidad entera no se puede corregir después sin otro ajuste."""
    with abrir_vto() as conn:
        unidades = _producto(conn, "Yogur", unidad="u")
        kilos = _producto(conn, "Queso", unidad="kg")
        deposito = catalogo.get_default_deposito_id(conn)
        antes = _sin_id(_todo_el_ledger(conn))
        with pytest.raises(ValueError, match="unidades enteras"):
            _con_lote(conn, unidades, deposito, "L1", _dias(5), Decimal("2.5"))
        with pytest.raises(ValueError, match="unidades enteras"):
            _con_lote(conn, kilos, deposito, "L1", _dias(5), Decimal("2.5"))   # «kg» sin `permite_fraccion`
        assert _sin_id(_todo_el_ledger(conn)) == antes
        assert _con_lote(conn, unidades, deposito, "L1", _dias(5), Decimal("3.0"))["cantidad"] == 3   # entera, vale
        # Con fracciones y sin escala declarada son 3 decimales; con escala, la suya.
        conn.execute("UPDATE units SET allows_fraction = 1 WHERE code = 'kg'")
        assert _con_lote(conn, kilos, deposito, "Q1", _dias(5), Decimal("2.125"))["saldo_lote"] == 2.125
        with pytest.raises(ValueError, match="hasta 3 decimales"):
            _con_lote(conn, kilos, deposito, "Q2", _dias(5), Decimal("0.0001"))
        conn.execute("UPDATE units SET decimal_scale = 1 WHERE code = 'kg'")
        assert _con_lote(conn, kilos, deposito, "Q3", _dias(5), Decimal("0.5"))["saldo_lote"] == 0.5
        with pytest.raises(ValueError, match="hasta 1 decimales"):
            _con_lote(conn, kilos, deposito, "Q4", _dias(5), Decimal("0.25"))


# ═══════════════════════════════════════════════ Idempotencia: `clave_operacion`


def test_un_reintento_devuelve_lo_de_la_primera_vez_sin_escribir(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        clave = _k()
        primera = _con_lote(conn, pid, deposito, "L1", _dias(5), 4, clave_operacion=clave, nota="Conteo")
        despues = _sin_id(_todo_el_ledger(conn))
        _con_lote(conn, pid, deposito, "L1", _dias(5), 6)                  # pasa el tiempo: el lote ahora tiene 10
        con_otra = _sin_id(_todo_el_ledger(conn))
        segunda = _con_lote(conn, pid, deposito, "L1", _dias(5), Decimal("4.0"), clave_operacion=clave,
                            nota="otra nota")
        assert _sin_id(_todo_el_ledger(conn)) == con_otra, "el reintento escribió"
        assert len(con_otra) == len(despues) + 1
    assert primera["repetida"] is False and primera["saldo_lote"] == 4
    assert segunda == {**primera, "repetida": True}, "el resultado repetido no es el de la primera vez"
    assert _buckets(abrir_vto, pid) == [("L1", _dias(5), 10)]


def test_el_reintento_de_una_carga_hecha_no_falla_porque_el_estado_cambio(abrir_vto):
    """La búsqueda de la clave va PRIMERO: lo que depende del estado (producto marcado, unidad) viene después."""
    with abrir_vto() as conn:
        pid = _producto(conn, unidad="kg")
        deposito = catalogo.get_default_deposito_id(conn)
        conn.execute("UPDATE units SET allows_fraction = 1 WHERE code = 'kg'")
        clave = _k()
        primera = _con_lote(conn, pid, deposito, "L1", _dias(5), Decimal("1.5"), clave_operacion=clave)
        vencimientos.marcar_vence(conn, pid, False)
        conn.execute("UPDATE units SET allows_fraction = 0 WHERE code = 'kg'")
        segunda = _con_lote(conn, pid, deposito, "L1", _dias(5), Decimal("1.5"), clave_operacion=clave)
        assert segunda == {**primera, "repetida": True}
        with pytest.raises(vencimientos.ReglaDeNegocio):              # pero una carga NUEVA sí falla
            _con_lote(conn, pid, deposito, "L1", _dias(5), 1)


@pytest.mark.parametrize("cambio", [
    {"cantidad": 5}, {"lote": "L2"}, {"vence": "2026-12-31"}, {"deposito": "otro"}, {"variante": True},
])
def test_la_misma_clave_con_otros_datos_es_409_y_no_escribe(abrir_vto, cambio):
    with abrir_vto() as conn:
        pid = _producto(conn)
        variante = catalogo.create_variante(conn, pid, "Y-1", "Frutilla")["id"]
        d1 = catalogo.get_default_deposito_id(conn)
        d2 = catalogo.create_deposito(conn, "Cámara")
        clave = _k()
        datos = {"deposito": d1, "lote": "L1", "vence": _dias(5), "cantidad": 4, "variante": None}
        _con_lote(conn, pid, d1, "L1", _dias(5), 4, clave_operacion=clave)
        antes = _sin_id(_todo_el_ledger(conn))
        datos.update({k: (d2 if v == "otro" else variante if v is True else v) for k, v in cambio.items()})
        with pytest.raises(vencimientos.ClaveDeOperacionReusada):
            _con_lote(conn, pid, datos["deposito"], datos["lote"], datos["vence"], datos["cantidad"],
                      variante_id=datos["variante"], clave_operacion=clave)
        assert _sin_id(_todo_el_ledger(conn)) == antes


def test_la_clave_no_se_comparte_entre_entrada_asignar_y_baja(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        _entrada(conn, pid, 10)
        _entrada(conn, pid, 10, lote="LB", vence=_dias(3))
        e, a, m = _k(), _k(), _k()
        _con_lote(conn, pid, deposito, "L1", _dias(5), 4, clave_operacion=e)
        _asignar(conn, pid, deposito, "L2", _dias(6), 2, clave_operacion=a)
        _baja(conn, pid, deposito, "LB", _dias(3), 1, clave_operacion=m)
        antes = _sin_id(_todo_el_ledger(conn))
        # La clave de una operación reusada para otra (con datos que coinciden donde se puede) es 409.
        with pytest.raises(vencimientos.ClaveDeOperacionReusada):
            _asignar(conn, pid, deposito, "L1", _dias(5), 4, clave_operacion=e)
        with pytest.raises(vencimientos.ClaveDeOperacionReusada):
            _baja(conn, pid, deposito, "L1", _dias(5), 4, clave_operacion=e)
        with pytest.raises(vencimientos.ClaveDeOperacionReusada):
            _con_lote(conn, pid, deposito, "L2", _dias(6), 2, clave_operacion=a)
        with pytest.raises(vencimientos.ClaveDeOperacionReusada):
            _con_lote(conn, pid, deposito, "LB", _dias(3), 1, clave_operacion=m)
        assert _sin_id(_todo_el_ledger(conn)) == antes
        # Y cada una sigue reintentándose a sí misma.
        assert _con_lote(conn, pid, deposito, "L1", _dias(5), 4, clave_operacion=e)["repetida"] is True
        assert _asignar(conn, pid, deposito, "L2", _dias(6), 2, clave_operacion=a)["repetida"] is True
        assert _baja(conn, pid, deposito, "LB", _dias(3), 1, clave_operacion=m)["repetida"] is True
        assert _sin_id(_todo_el_ledger(conn)) == antes


def test_la_clave_es_por_producto_la_misma_clave_en_otro_producto_es_otra_operacion(abrir_vto):
    with abrir_vto() as conn:
        a, b = _producto(conn, "Yogur"), _producto(conn, "Leche")
        deposito = catalogo.get_default_deposito_id(conn)
        clave = _k()
        ra = _con_lote(conn, a, deposito, "L1", _dias(5), 4, clave_operacion=clave)
        rb = _con_lote(conn, b, deposito, "L1", _dias(5), 4, clave_operacion=clave)     # otra operación: no 409
        assert (ra["repetida"], rb["repetida"]) == (False, False)
        assert len(_todo_el_ledger(conn)) == 2
        assert _con_lote(conn, a, deposito, "L1", _dias(5), 4, clave_operacion=clave) == {**ra, "repetida": True}
        assert _con_lote(conn, b, deposito, "L1", _dias(5), 4, clave_operacion=clave) == {**rb, "repetida": True}
        with pytest.raises(vencimientos.ClaveDeOperacionReusada):
            _con_lote(conn, a, deposito, "L1", _dias(5), 5, clave_operacion=clave)
        assert len(_todo_el_ledger(conn)) == 2


def test_un_texto_libre_no_puede_imitar_la_marca_de_una_entrada(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        _con_lote(conn, pid, deposito, "L1", _dias(5), 4, nota="[op:clave-falsa] y más", clave_operacion="otra")
        assert _con_lote(conn, pid, deposito, "L1", _dias(5), 4, clave_operacion="clave-falsa")["repetida"] is False
        _entrada(conn, pid, 3)
        conn.execute("UPDATE stock_movements SET note = 'a mano [op:clave-manual] sigue'")
        assert _con_lote(conn, pid, deposito, "L1", _dias(5), 4, clave_operacion="clave-manual")["repetida"] is False


def test_en_postgres_dos_reintentos_simultaneos_de_la_entrada_escriben_una_sola_vez(abrir_vto):
    if not vencimientos_es_postgres():
        pytest.skip("sólo PostgreSQL toma el bloqueo por fila")
    with abrir_vto() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        antes = len(_todo_el_ledger(conn))
    clave = _k()
    resultados = _dos_reintentos_a_la_vez(
        abrir_vto, lambda c: _con_lote(c, pid, deposito, "L1", _dias(5), 4, clave_operacion=clave))
    assert sorted(r["repetida"] for r in resultados) == [False, True]
    assert {r["saldo_lote"] for r in resultados} == {4}
    with abrir_vto() as conn:
        assert len(_todo_el_ledger(conn)) == antes + 1
    assert _buckets(abrir_vto, pid) == [("L1", _dias(5), 4)]


def test_en_postgres_la_misma_clave_en_dos_productos_a_la_vez_escribe_una_vez_en_cada_uno(abrir_vto):
    if not vencimientos_es_postgres():
        pytest.skip("sólo PostgreSQL toma el bloqueo por fila")
    with abrir_vto() as conn:
        a, b = _producto(conn, "Yogur"), _producto(conn, "Leche")
        deposito = catalogo.get_default_deposito_id(conn)
    clave = _k()
    ra, rb = _dos_productos_a_la_vez([
        lambda c: _con_lote(c, a, deposito, "L1", _dias(5), 4, clave_operacion=clave),
        lambda c: _con_lote(c, b, deposito, "L1", _dias(5), 4, clave_operacion=clave),
    ])
    assert (ra["repetida"], rb["repetida"]) == (False, False)
    r2a, r2b = _dos_productos_a_la_vez([
        lambda c: _con_lote(c, a, deposito, "L1", _dias(5), 4, clave_operacion=clave),
        lambda c: _con_lote(c, b, deposito, "L1", _dias(5), 4, clave_operacion=clave),
    ])
    assert r2a == {**ra, "repetida": True} and r2b == {**rb, "repetida": True}
    with abrir_vto() as conn:
        assert len(_todo_el_ledger(conn)) == 2


def vencimientos_es_postgres() -> bool:
    from libracore.db import core

    return core.is_postgres()


# ═══════════════════════════════════════════════════ La entrada con lote (HTTP)


def _cuerpo_entrada(pid, deposito, **kw):
    return {"producto_id": pid, "deposito_id": deposito, "lote": "L9", "vence": "2026-11-03", "cantidad": 2.5,
            "nota": "Conteo", "clave_operacion": _k(), **kw}


def test_la_entrada_por_http(abrir_vto, monkeypatch):
    _hoy_fijo(monkeypatch)
    with abrir_vto() as conn:
        pid = _producto(conn, "Queso", unidad="kg")
        conn.execute("UPDATE units SET allows_fraction = 1 WHERE code = 'kg'")
        deposito = catalogo.get_default_deposito_id(conn)
        sin_marca = _producto(conn, "Sin marca", vence=False)
    c = _app(abrir_vto)
    cuerpo = _cuerpo_entrada(pid, deposito)
    r = c.post("/api/vencimientos/entrada", json=cuerpo)
    assert r.status_code == 200, r.text
    assert r.json() == {"producto_id": pid, "deposito_id": deposito, "variante_id": None, "lote": "L9",
                        "vence": "2026-11-03", "cantidad": 2.5, "referencia": "Conteo", "saldo_lote": 2.5,
                        "repetida": False}
    with abrir_vto() as conn:
        fila = conn.execute("SELECT created_by, reason_code, lot_code, expires_at FROM stock_movements").fetchone()
        assert stock.get_stock_actual(conn, pid) == 2.5
    assert tuple(fila) == (USUARIO["id"], "entrada", "L9", "2026-11-03")
    assert c.get(f"/api/vencimientos/productos/{pid}/lotes").json()["lotes"][0]["saldo"] == 2.5
    # 404, 409 y 422: el mismo vocabulario que asignar y merma.
    assert c.post("/api/vencimientos/entrada", json=_cuerpo_entrada(9999, deposito)).status_code == 404
    r = c.post("/api/vencimientos/entrada", json=_cuerpo_entrada(sin_marca, deposito))
    assert r.status_code == 409 and "no está marcado" in r.json()["detail"]
    invalidos = [{"lote": "  "}, {"lote": None}, {"vence": "mañana"}, {"vence": None}, {"cantidad": 0},
                 {"cantidad": -1}, {"cantidad": "x"}, {"deposito_id": 999}, {"variante_id": 9999},
                 {"clave_operacion": ""}, {"clave_operacion": "x" * 65}, {"clave_operacion": None}]
    for cambio in invalidos:
        assert c.post("/api/vencimientos/entrada", json={**cuerpo, "clave_operacion": _k(), **cambio}
                      ).status_code == 422, cambio
    sin_clave = {k: v for k, v in cuerpo.items() if k != "clave_operacion"}
    assert c.post("/api/vencimientos/entrada", json=sin_clave).status_code == 422
    # La escala de la unidad: «u» no admite 2.5.
    with abrir_vto() as conn:
        enteros = _producto(conn, "Yogur", unidad="u")
    r = c.post("/api/vencimientos/entrada", json=_cuerpo_entrada(enteros, deposito))
    assert r.status_code == 422 and "unidades enteras" in r.json()["detail"]
    with abrir_vto() as conn:
        assert stock.get_stock_actual(conn, pid) == 2.5 and stock.get_stock_actual(conn, enteros) == 0


def test_un_reintento_de_la_entrada_por_http_devuelve_lo_anterior_con_repetida(abrir_vto, monkeypatch):
    _hoy_fijo(monkeypatch)
    with abrir_vto() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
    c = _app(abrir_vto)
    cuerpo = _cuerpo_entrada(pid, deposito, cantidad=3)
    primera = c.post("/api/vencimientos/entrada", json=cuerpo)
    with abrir_vto() as conn:
        n = len(_todo_el_ledger(conn))
    segunda = c.post("/api/vencimientos/entrada", json=cuerpo)
    assert (primera.status_code, segunda.status_code) == (200, 200)
    assert primera.json()["repetida"] is False and segunda.json() == {**primera.json(), "repetida": True}
    r = c.post("/api/vencimientos/entrada", json={**cuerpo, "cantidad": 4})
    assert r.status_code == 409 and "otros parámetros" in r.json()["detail"]
    # Ni la clave de un asignar ni la de una merma sirven para otra operación.
    r = c.post("/api/vencimientos/asignar", json={k: v for k, v in cuerpo.items()})
    assert r.status_code == 409
    with abrir_vto() as conn:
        assert len(_todo_el_ledger(conn)) == n
    assert c.post("/api/vencimientos/entrada", json={**cuerpo, "clave_operacion": _k()}).json()["repetida"] is False


def test_la_entrada_respeta_los_gates_de_movimientos_y_exige_identidad(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)

    def _denegar():
        raise HTTPException(403, "sin permiso")

    def _sin_sesion():
        raise HTTPException(401, "sin sesión")

    app = FastAPI()
    app.include_router(build_vencimientos_escritura_router(
        conexion=abrir_vto, usuario_actual=lambda: USUARIO,
        dependencias_marcar=[Depends(_libre)], dependencias_movimientos=[Depends(_denegar)]))
    assert TestClient(app).post("/api/vencimientos/entrada", json=_cuerpo_entrada(pid, deposito)).status_code == 403
    app = FastAPI()
    app.include_router(build_vencimientos_escritura_router(
        conexion=abrir_vto, usuario_actual=_sin_sesion,
        dependencias_marcar=[Depends(_libre)], dependencias_movimientos=[Depends(_libre)]))
    assert TestClient(app).post("/api/vencimientos/entrada", json=_cuerpo_entrada(pid, deposito)).status_code == 401
    with abrir_vto() as conn:
        assert len(_todo_el_ledger(conn)) == 0
    # La factory sigue fallando sin usuario, sin gate de marcar o sin gate de movimientos.
    libre = [Depends(_libre)]
    with pytest.raises(ValueError, match="usuario_actual"):
        build_vencimientos_escritura_router(conexion=abrir_vto, dependencias_marcar=libre,
                                            dependencias_movimientos=libre)
    with pytest.raises(ValueError, match="dependencias_movimientos"):
        build_vencimientos_escritura_router(conexion=abrir_vto, usuario_actual=lambda: USUARIO,
                                            dependencias_marcar=libre)
    with pytest.raises(ValueError, match="dependencias_marcar"):
        build_vencimientos_escritura_router(conexion=abrir_vto, usuario_actual=lambda: USUARIO,
                                            dependencias_movimientos=libre)


def test_sin_la_revision_la_entrada_responde_503_con_el_comando(destino):
    from libracore.db import core
    from test_vencimientos import _crear_base_al_dia_de_0001, _liberar

    _crear_base_al_dia_de_0001(destino)
    core.configure(destino)
    try:
        c = _app(core.get_connection)
        r = c.post("/api/vencimientos/entrada", json=_cuerpo_entrada(1, 1))
        assert r.status_code == 503 and "libracommerce-migrar upgrade" in r.json()["detail"]
    finally:
        _liberar()


# ════════════════════════════════════════ La marca `vence` en el producto (opt-in)


def _productos_app(abrir, *, opciones=None, usuario_actual=lambda: USUARIO):
    app = FastAPI()
    app.include_router(build_productos_router(conexion=abrir, usuario_actual=usuario_actual, opciones=opciones))
    return TestClient(app)


def _alta(c, nombre="Yogur", **extra):
    r = c.post("/api/productos", json={"nombre": nombre, "precio_venta": 100.0, "precio_costo": 60.0, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _edicion(c, pid, **extra):
    return c.put(f"/api/productos/{pid}", json={"nombre": "Yogur", "precio_venta": 100.0, "precio_costo": 60.0,
                                                **extra})


def _marca_en_la_base(abrir, pid) -> bool:
    with abrir() as conn:
        return bool(conn.execute("SELECT tracks_expiry FROM catalog_items WHERE id = ?", (pid,)).fetchone()[0])


def _foto_del_producto(abrir, pid):
    with abrir() as conn:
        return tuple(conn.execute(
            "SELECT name, description, unit_code, default_sale_price, default_cost, min_stock, active, sellable, "
            "tracks_expiry FROM catalog_items WHERE id = ?", (pid,)).fetchone())


def test_con_la_opcion_apagada_las_respuestas_son_las_de_siempre(abrir_vto):
    """Apagada (el default) el router es el de hoy: ni `vence` en las respuestas, ni el campo en el cuerpo, ni se
    resuelve la sesión, y el JSON es idéntico al de una app que no declara la opción."""
    base = _productos_app(abrir_vto)
    apagada = _productos_app(abrir_vto, opciones=OpcionesCatalogo(con_vencimientos=False,
                                                                   autorizar_marcar_vence=lambda u: False))
    alta = _alta(base, "Yogur", codigo="7791234567890")
    assert "vence" not in alta
    pid = alta["id"]
    for ruta in ("/api/productos", "/api/productos?q=yog&incluir_variantes=true", "/api/productos/escanear?code=7791234567890"):
        a, b = base.get(ruta), apagada.get(ruta)
        assert a.status_code == b.status_code == 200 and a.content == b.content, ruta
        assert "vence" not in a.text
    cuerpo = {"nombre": "Yogur entero", "precio_venta": 120.0, "precio_costo": 70.0, "codigo": "7791234567890"}
    a, b = base.put(f"/api/productos/{pid}", json=cuerpo), apagada.put(f"/api/productos/{pid}", json=cuerpo)
    assert a.status_code == b.status_code == 200 and a.content == b.content and "vence" not in a.text
    # El cuerpo con `vence` se ignora: como hoy, un campo de más no hace nada (ni marca, ni 422).
    r = apagada.put(f"/api/productos/{pid}", json={**cuerpo, "vence": True})
    assert r.status_code == 200 and "vence" not in r.text and r.content == a.content
    assert _marca_en_la_base(abrir_vto, pid) is False
    r = apagada.post("/api/productos", json={**cuerpo, "codigo": "", "vence": True})
    assert r.status_code == 200 and _marca_en_la_base(abrir_vto, r.json()["id"]) is False
    nuevo_base = base.post("/api/productos", json={**cuerpo, "codigo": ""}).json()
    nuevo_apagada = apagada.post("/api/productos", json={**cuerpo, "codigo": ""}).json()
    assert {k: v for k, v in nuevo_base.items() if k not in ("id", "created_at")} == \
           {k: v for k, v in nuevo_apagada.items() if k not in ("id", "created_at")}
    # Y el contrato (OpenAPI) es el mismo que el de una app sin la opción.
    assert base.get("/openapi.json").content == apagada.get("/openapi.json").content
    # No se resuelve la sesión: un `usuario_actual` que rechaza no cambia nada con la opción apagada.
    def _sin_sesion():
        raise HTTPException(401, "sin sesión")
    sin_sesion = _productos_app(abrir_vto, usuario_actual=_sin_sesion,
                                opciones=OpcionesCatalogo(autorizar_marcar_vence=lambda u: False))
    assert sin_sesion.put(f"/api/productos/{pid}", json=cuerpo).status_code == 200


def test_con_la_opcion_prendida_listado_escaneo_alta_y_edicion_devuelven_vence(abrir_vto):
    c = _productos_app(abrir_vto, opciones=OpcionesCatalogo(con_vencimientos=True))
    yogur = _alta(c, "Yogur", codigo="7791234567890", vence=True)
    arroz = _alta(c, "Arroz", codigo="7790000000001")
    assert yogur["vence"] is True and arroz["vence"] is False
    assert _marca_en_la_base(abrir_vto, yogur["id"]) is True and _marca_en_la_base(abrir_vto, arroz["id"]) is False
    listado = {p["nombre"]: p["vence"] for p in c.get("/api/productos").json()}
    assert listado == {"Yogur": True, "Arroz": False}
    assert {p["nombre"]: p["vence"] for p in c.get("/api/productos?q=yog&incluir_variantes=true").json()} == {
        "Yogur": True}
    assert c.get("/api/productos/escanear?code=7791234567890").json()["producto"]["vence"] is True
    assert c.get("/api/productos/escanear?code=7790000000001").json()["producto"]["vence"] is False
    # Marcar y desmarcar al editar; la respuesta dice cómo quedó.
    r = _edicion(c, arroz["id"], vence=True)
    assert r.status_code == 200 and r.json()["vence"] is True and _marca_en_la_base(abrir_vto, arroz["id"]) is True
    r = _edicion(c, arroz["id"], vence=False)
    assert r.status_code == 200 and r.json()["vence"] is False and _marca_en_la_base(abrir_vto, arroz["id"]) is False
    # La marca estricta: un texto o un número no son un booleano.
    for malo in ("si", 1, 0, "true"):
        assert _edicion(c, arroz["id"], vence=malo).status_code == 422, malo


def test_el_listado_hace_una_sola_consulta_auxiliar_no_una_por_producto(abrir_vto, monkeypatch):
    c = _productos_app(abrir_vto, opciones=OpcionesCatalogo(con_vencimientos=True))
    for i in range(6):
        _alta(c, f"Producto {i}", vence=(i % 2 == 0))
    llamadas = []
    original = vencimientos.ids_que_vencen
    monkeypatch.setattr(vencimientos, "ids_que_vencen", lambda *a, **kw: llamadas.append(a) or original(*a, **kw))
    listado = c.get("/api/productos").json()
    assert len(llamadas) == 1 and len(listado) == 6
    assert [p["vence"] for p in listado] == [True, False, True, False, True, False]


def test_editar_sin_vence_no_toca_la_marca_y_nunca_se_pierde_al_editar_otros_campos(abrir_vto):
    c = _productos_app(abrir_vto, opciones=OpcionesCatalogo(con_vencimientos=True))
    pid = _alta(c, "Yogur", vence=True)["id"]
    r = c.put(f"/api/productos/{pid}", json={"nombre": "Yogur entero", "precio_venta": 150.0, "precio_costo": 80.0,
                                              "categoria": "Lácteos", "stock_minimo": 3, "activo": True})
    assert r.status_code == 200 and r.json()["vence"] is True and r.json()["nombre"] == "Yogur entero"
    assert _marca_en_la_base(abrir_vto, pid) is True
    assert _edicion(c, pid, vence=None).json()["vence"] is True      # un `null` explícito tampoco la toca
    # Con la opción apagada también sobrevive: el guardado del motor no escribe la columna.
    apagada = _productos_app(abrir_vto)
    assert apagada.put(f"/api/productos/{pid}", json={"nombre": "Yogur", "precio_venta": 1.0}).status_code == 200
    assert _marca_en_la_base(abrir_vto, pid) is True
    # Y una marca igual a la que hay no escribe ni pide permiso.
    llamadas = []
    con_gancho = _productos_app(abrir_vto, opciones=OpcionesCatalogo(
        con_vencimientos=True, autorizar_marcar_vence=lambda u: llamadas.append(u) or False))
    assert _edicion(con_gancho, pid, vence=True).status_code == 200 and llamadas == []


def test_el_gancho_que_deniega_es_403_y_no_se_guarda_nada_de_la_edicion(abrir_vto):
    recibidos = []

    def _no(usuario):
        recibidos.append(usuario)
        return False

    libre = _productos_app(abrir_vto, opciones=OpcionesCatalogo(con_vencimientos=True))
    pid = _alta(libre, "Yogur", vence=False, precio_venta=100.0)["id"]
    c = _productos_app(abrir_vto, opciones=OpcionesCatalogo(con_vencimientos=True, autorizar_marcar_vence=_no))
    antes = _foto_del_producto(abrir_vto, pid)
    r = c.put(f"/api/productos/{pid}", json={"nombre": "Otro nombre", "precio_venta": 999.0, "precio_costo": 1.0,
                                              "categoria": "Nueva", "activo": False, "vence": True})
    assert r.status_code == 403
    assert recibidos == [USUARIO]                          # recibe el usuario de la sesión
    assert _foto_del_producto(abrir_vto, pid) == antes, "la edición quedó a medias"
    with abrir_vto() as conn:
        assert not conn.execute("SELECT 1 FROM categories WHERE name = 'Nueva'").fetchall()
    # Desmarcar también pide permiso.
    marcado = _alta(libre, "Leche", vence=True)["id"]
    antes = _foto_del_producto(abrir_vto, marcado)
    assert c.put(f"/api/productos/{marcado}", json={"nombre": "Leche 2", "precio_venta": 5.0, "vence": False}
                 ).status_code == 403
    assert _foto_del_producto(abrir_vto, marcado) == antes
    # El alta con `vence: true` denegada no crea el producto.
    n = len(libre.get("/api/productos").json())
    assert c.post("/api/productos", json={"nombre": "Nuevo", "precio_venta": 1.0, "vence": True}).status_code == 403
    assert len(libre.get("/api/productos").json()) == n
    # Sin tocar la marca, editar sigue siendo de quien puede editar; y un gancho que aprueba deja marcar.
    assert c.put(f"/api/productos/{pid}", json={"nombre": "Yogur 2", "precio_venta": 110.0}).status_code == 200
    si = _productos_app(abrir_vto, opciones=OpcionesCatalogo(con_vencimientos=True,
                                                              autorizar_marcar_vence=lambda u: u["id"] == 7))
    assert si.put(f"/api/productos/{pid}", json={"nombre": "Yogur 3", "precio_venta": 1.0, "vence": True}
                  ).json()["vence"] is True


def test_un_gancho_que_levanta_su_propia_excepcion_tampoco_deja_nada_escrito(abrir_vto):
    def _prohibido(usuario):
        raise HTTPException(403, "Sólo el encargado marca productos que vencen.")

    c = _productos_app(abrir_vto, opciones=OpcionesCatalogo(con_vencimientos=True, autorizar_marcar_vence=_prohibido))
    pid = _alta(_productos_app(abrir_vto, opciones=OpcionesCatalogo(con_vencimientos=True)), "Yogur")["id"]
    antes = _foto_del_producto(abrir_vto, pid)
    r = c.put(f"/api/productos/{pid}", json={"nombre": "Otro", "precio_venta": 9.0, "vence": True})
    assert r.status_code == 403 and "encargado" in r.json()["detail"]
    assert _foto_del_producto(abrir_vto, pid) == antes


def test_si_el_guardado_falla_la_marca_no_queda(abrir_vto):
    """La marca se escribe DESPUÉS del guardado: si éste falla (un código repetido es un 422 en el guardado del motor,
    que commitea por su cuenta), la marca no queda cambiada."""
    c = _productos_app(abrir_vto, opciones=OpcionesCatalogo(con_vencimientos=True))
    pid = _alta(c, "Yogur")["id"]
    _alta(c, "Leche", codigo="DUP-1")

    def _rechaza(payload, actual):
        raise HTTPException(409, "no se puede")

    r = _edicion(c, pid, vence=True, codigo="DUP-1")          # el código ya es de otro producto
    assert r.status_code == 422
    c_hook = _productos_app(abrir_vto, opciones=OpcionesCatalogo(con_vencimientos=True, validar_producto=_rechaza))
    assert _edicion(c_hook, pid, vence=True).status_code == 409
    assert _marca_en_la_base(abrir_vto, pid) is False


def test_un_servicio_no_se_puede_marcar_ni_en_el_alta_ni_en_la_edicion(abrir_vto):
    c = _productos_app(abrir_vto, opciones=OpcionesCatalogo(con_vencimientos=True))
    n = len(c.get("/api/productos").json())
    assert c.post("/api/productos", json={"nombre": "Flete", "tipo": "servicio", "vence": True}).status_code == 409
    assert len(c.get("/api/productos").json()) == n
    servicio = _alta(c, "Flete", tipo="servicio")
    assert servicio["vence"] is False
    r = c.put(f"/api/productos/{servicio['id']}", json={"nombre": "Flete 2", "tipo": "servicio", "vence": True})
    assert r.status_code == 409 and _foto_del_producto(abrir_vto, servicio["id"])[0] == "Flete"
    # Un producto que pasa a servicio con `vence: true` tampoco.
    pid = _alta(c, "Yogur")["id"]
    assert _edicion(c, pid, tipo="servicio", vence=True).status_code == 409
    assert c.put("/api/productos/9999", json={"nombre": "x", "vence": True}).status_code == 404


def test_una_base_sin_la_revision_0002_no_rompe_y_marcar_dice_que_falta(abrir):
    """`abrir` es la base de la baseline, sin `tracks_expiry`. En PostgreSQL un `SELECT` de la columna que falla aborta
    la transacción: el router sondea por metadatos y el resto del pedido sigue andando."""
    c = _productos_app(abrir, opciones=OpcionesCatalogo(con_vencimientos=True))
    a = _alta(c, "Yogur", codigo="7791234567890")
    b = _alta(c, "Arroz", vence=False)                         # `false` no cambia nada: vale sin la revisión
    assert a["vence"] is False and b["vence"] is False
    assert [p["vence"] for p in c.get("/api/productos").json()] == [False, False]
    assert c.get("/api/productos/escanear?code=7791234567890").json()["producto"]["vence"] is False
    r = c.put(f"/api/productos/{a['id']}", json={"nombre": "Yogur 2", "precio_venta": 1.0, "vence": False})
    assert r.status_code == 200 and r.json()["vence"] is False and r.json()["nombre"] == "Yogur 2"
    # Marcar sí necesita la revisión: 409 claro y no se guarda nada de la edición ni del alta.
    r = c.put(f"/api/productos/{a['id']}", json={"nombre": "Yogur 3", "precio_venta": 1.0, "vence": True})
    assert r.status_code == 409 and "libracommerce-migrar upgrade" in r.json()["detail"]
    with abrir() as conn:
        assert conn.execute("SELECT name FROM catalog_items WHERE id = ?", (a["id"],)).fetchone()[0] == "Yogur 2"
    n = len(c.get("/api/productos").json())
    r = c.post("/api/productos", json={"nombre": "Nuevo", "precio_venta": 1.0, "vence": True})
    assert r.status_code == 409 and len(c.get("/api/productos").json()) == n
    # Las tres respuestas de lectura siguen andando después del 409 (la transacción no quedó abortada).
    assert len(c.get("/api/productos").json()) == n


def test_la_marca_de_un_producto_marcado_se_ve_en_el_reporte_y_admite_la_entrada_con_lote(abrir_vto, monkeypatch):
    """De punta a punta: se da de alta un producto que vence, se le carga un lote y aparece en el aviso."""
    _hoy_fijo(monkeypatch)
    productos = _productos_app(abrir_vto, opciones=OpcionesCatalogo(con_vencimientos=True))
    pid = _alta(productos, "Yogur", vence=True)["id"]
    with abrir_vto() as conn:
        deposito = catalogo.get_default_deposito_id(conn)
    c = _app(abrir_vto)
    assert c.post("/api/vencimientos/entrada", json=_cuerpo_entrada(pid, deposito, cantidad=6, vence=_dias(4))
                  ).status_code == 200
    reporte = c.get("/api/vencimientos").json()
    assert [(r["nombre"], r["lote"], r["saldo"]) for r in reporte["lotes"]] == [("Yogur", "L9", 6)]
    assert [p["vence"] for p in productos.get("/api/productos").json()] == [True]
    assert vencimientos.hoy_argentina() == HOY and datetime.date.fromisoformat(PREVIA) < HOY


# ═══════════════════════════ Revisión de Codex: servicio marcado, depósito inactivo, edición no atómica


def test_un_producto_marcado_no_pasa_a_servicio_quedando_marcado(abrir_vto):
    """Se valida la combinación RESULTANTE (tipo pedido + marca efectiva), cambie o no la marca."""
    c = _productos_app(abrir_vto, opciones=OpcionesCatalogo(con_vencimientos=True))
    pid = _alta(c, "Yogur", vence=True)["id"]
    antes = _foto_del_producto(abrir_vto, pid)
    for extra in ({}, {"vence": True}, {"vence": None}):
        r = c.put(f"/api/productos/{pid}", json={"nombre": "Flete", "precio_venta": 5.0, "tipo": "servicio", **extra})
        assert r.status_code == 409 and "servicio no puede tener vencimiento" in r.json()["detail"], extra
        assert _foto_del_producto(abrir_vto, pid) == antes, "se guardó algo"
    with abrir_vto() as conn:
        assert conn.execute("SELECT item_type FROM catalog_items WHERE id = ?", (pid,)).fetchone()[0] == "product"
    # Desmarcándolo en la misma edición sí vale: queda servicio y sin marca.
    r = c.put(f"/api/productos/{pid}", json={"nombre": "Flete", "precio_venta": 5.0, "tipo": "servicio",
                                              "vence": False})
    assert r.status_code == 200 and r.json()["vence"] is False and r.json()["tipo"] == "servicio"
    assert _marca_en_la_base(abrir_vto, pid) is False
    # Un servicio sin marca se edita sin problema, con o sin `vence: false`.
    assert c.put(f"/api/productos/{pid}", json={"nombre": "Flete 2", "tipo": "servicio"}).status_code == 200
    # El gancho no se consulta si el problema es la combinación.
    llamadas = []
    con_gancho = _productos_app(abrir_vto, opciones=OpcionesCatalogo(
        con_vencimientos=True, autorizar_marcar_vence=lambda u: llamadas.append(u) or True))
    otro = _alta(con_gancho, "Leche", vence=True)["id"]
    llamadas.clear()
    assert con_gancho.put(f"/api/productos/{otro}", json={"nombre": "x", "tipo": "servicio"}).status_code == 409
    assert llamadas == []


def test_desmarcar_un_servicio_vale_y_marcarlo_no(abrir_vto):
    with abrir_vto() as conn:
        servicio = catalogo.create_producto(conn, "Flete", precio_venta=1.0, tipo="servicio")
        conn.execute("UPDATE catalog_items SET tracks_expiry = 1 WHERE id = ?", (servicio,))   # el estado heredado
        assert vencimientos.marcar_vence(conn, servicio, False) == {"producto_id": servicio, "vence": False}
        with pytest.raises(vencimientos.ReglaDeNegocio, match="servicio"):
            vencimientos.marcar_vence(conn, servicio, True)


def test_un_servicio_no_recibe_entradas_ni_asignaciones_aunque_este_marcado(abrir_vto):
    with abrir_vto() as conn:
        servicio = catalogo.create_producto(conn, "Flete", precio_venta=1.0, tipo="servicio")
        conn.execute("UPDATE catalog_items SET tracks_expiry = 1 WHERE id = ?", (servicio,))
        deposito = catalogo.get_default_deposito_id(conn)
        antes = _sin_id(_todo_el_ledger(conn))
        with pytest.raises(vencimientos.ReglaDeNegocio, match="servicio"):
            _con_lote(conn, servicio, deposito, "L1", _dias(5), 1)
        with pytest.raises(vencimientos.ReglaDeNegocio, match="servicio"):
            _asignar(conn, servicio, deposito, "L1", _dias(5), 1)
        assert _sin_id(_todo_el_ledger(conn)) == antes
    c = _app(abrir_vto)
    assert c.post("/api/vencimientos/entrada", json=_cuerpo_entrada(servicio, deposito, cantidad=1)).status_code == 409
    with abrir_vto() as conn:
        assert _sin_id(_todo_el_ledger(conn)) == antes


def test_la_entrada_exige_deposito_activo_pero_leer_asignar_y_mermar_siguen_valiendo(abrir_vto):
    """Stock NUEVO sólo a un depósito activo (`catalogo.validar_deposito`, 422 como en ventas y transferencias); lo que
    ya hay en un depósito dado de baja se sigue pudiendo leer, asignar y dar de baja."""
    with abrir_vto() as conn:
        pid = _producto(conn)
        activo = catalogo.get_default_deposito_id(conn)
        viejo = catalogo.create_deposito(conn, "Cámara vieja")
        _entrada(conn, pid, 10, deposito=viejo)
        _entrada(conn, pid, 6, lote="L1", vence=_dias(5), deposito=viejo)
        hecha = _con_lote(conn, pid, viejo, "L9", _dias(9), 2, clave_operacion="previa")   # cuando estaba activo
        conn.execute("UPDATE locations SET active = 0 WHERE id = ?", (viejo,))
        antes = _sin_id(_todo_el_ledger(conn))
        with pytest.raises(catalogo.DepositoInexistente, match="no existe o no está activo"):
            _con_lote(conn, pid, viejo, "L2", _dias(5), 1)
        assert _sin_id(_todo_el_ledger(conn)) == antes
        # Un reintento de la carga que ya estaba hecha devuelve lo de la primera vez aunque hoy esté inactivo.
        assert _con_lote(conn, pid, viejo, "L9", _dias(9), 2, clave_operacion="previa") == {**hecha, "repetida": True}
        # Lectura, asignar y merma sobre el depósito inactivo siguen andando.
        assert {f["lote"]: f["saldo"] for f in vencimientos.lotes_de(conn, pid, deposito_id=viejo, hoy=HOY)} == {
            None: 10, "L1": 6, "L9": 2}
        assert _asignar(conn, pid, viejo, "A1", _dias(7), 3)["saldo_sin_lote"] == 7
        assert _baja(conn, pid, viejo, "L1", _dias(5), 2)["saldo_restante"] == 4
        assert _con_lote(conn, pid, activo, "L3", _dias(5), 1)["repetida"] is False        # y el activo sigue igual
    c = _app(abrir_vto)
    r = c.post("/api/vencimientos/entrada", json=_cuerpo_entrada(pid, viejo, cantidad=1))
    assert r.status_code == 422 and "no está activo" in r.json()["detail"]
    assert c.get(f"/api/vencimientos/productos/{pid}/lotes", params={"deposito_id": viejo}).status_code == 200


def test_la_edicion_es_atomica_respecto_del_codigo_duplicado(abrir_vto):
    """ADR-028: `update_producto` guarda los campos y reemplaza el código principal en UNA transacción. Antes (limitación preexistente, fijada acá hasta entonces) los campos se
    confirmaban y DESPUÉS fallaba el código repetido: 422 con el nombre y el precio ya guardados. Ahora el 422 no guarda nada, con o sin `vence`, y la marca tampoco cambia."""
    c = _productos_app(abrir_vto, opciones=OpcionesCatalogo(con_vencimientos=True))
    pid = _alta(c, "Yogur")["id"]
    _alta(c, "Leche", codigo="DUP-1")
    antes = _foto_del_producto(abrir_vto, pid)
    r = c.put(f"/api/productos/{pid}", json={"nombre": "Yogur editado", "precio_venta": 77.0, "codigo": "DUP-1",
                                              "vence": True})
    assert r.status_code == 422
    assert _foto_del_producto(abrir_vto, pid) == antes, "un código repetido guardó campos del producto"
    assert _marca_en_la_base(abrir_vto, pid) is False
    # Sin `vence` (y con la opción apagada, el router de siempre) pasa exactamente lo mismo: no es cosa de la marca.
    apagada = _productos_app(abrir_vto)
    r = apagada.put(f"/api/productos/{pid}", json={"nombre": "Yogur otra vez", "precio_venta": 88.0, "codigo": "DUP-1"})
    assert r.status_code == 422 and _foto_del_producto(abrir_vto, pid) == antes


def test_el_codigo_de_otra_edicion_no_se_pisa_entre_el_guardado_y_el_reemplazo(abrir_vto, monkeypatch):
    """La ventana de ADR-027 (4), cerrada en ADR-028, con barrera determinista (hilos, los dos motores). El hilo A edita el producto SIN querer cambiar el código (manda el `111` que
    leyó, como la actualización masiva) y se detiene justo antes del `_set_codigo`, que antes corría DESPUÉS de que el repositorio confirmara y soltara el candado. El hilo B edita el mismo
    producto y cambia el código a `222`. Antes B no esperaba, confirmaba, y el `_set_codigo` de A (que ve `222`, distinto de su `111`) lo reemplazaba: B dejaba su precio y perdía su código
    (final `111`). Ahora el guardado y el reemplazo son una sola transacción con el candado tomado hasta el final: B espera (el plazo de A se agota, no hay forma de que haya terminado), escribe
    después y el final es `222`."""
    import threading

    abrir = abrir_vto
    with abrir() as conn:
        pid = catalogo.create_producto(conn, nombre="Yerba", codigo="111", precio_venta=10, precio_costo=5)
    guardado, otro_listo = threading.Event(), threading.Event()
    real = catalogo._set_codigo

    def set_codigo_tarde(repo, conn, item_id, codigo):
        if threading.current_thread().name == "A":
            guardado.set()
            otro_listo.wait(timeout=5)         # con el guardado atómico, B espera el candado y este plazo se agota: no hay forma de que haya terminado
        return real(repo, conn, item_id, codigo)

    monkeypatch.setattr(catalogo, "_set_codigo", set_codigo_tarde)

    def editar(codigo, precio):
        with abrir() as conn:
            catalogo.update_producto(conn, pid=pid, nombre="Yerba", codigo=codigo, descripcion="", precio_venta=precio,
                                     precio_costo=5, unidad="u", categoria="", activo=1)

    errores: list[str] = []

    def hilo_b():
        try:
            assert guardado.wait(timeout=30)
            editar("222", 12)
        except Exception as exc:  # noqa: BLE001 - se informa abajo
            errores.append(repr(exc))
        finally:
            otro_listo.set()

    b = threading.Thread(target=hilo_b, name="B")
    b.start()
    a = threading.Thread(target=editar, args=("111", 11), name="A")
    a.start()
    a.join(timeout=60)
    b.join(timeout=60)
    assert not errores, errores
    with abrir() as conn:
        final = catalogo.get_producto(conn, pid)
    assert final["codigo"] == "222", f"B dejó su precio ({final['precio_venta']}) y perdió su código: quedó {final['codigo']}"
