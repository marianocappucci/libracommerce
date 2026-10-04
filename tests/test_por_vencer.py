"""Reposición v2: descontar lo que vence dentro del horizonte (ADR-025, opt-in con `descontar_por_vencer`).

Las fixtures y los helpers son los de `tests/test_reposicion.py` (una base por motor con la revisión `0002`). La cuenta que se mide: con `r` la rotación diaria proyectada y `H` el horizonte, la pérdida es
`max(0, máx_j (C_j − r × d_j))` sobre los lotes que vencen dentro de `H`, con `d_j = (vence_j − hoy).días + 1` y `C_j` el saldo acumulado por orden de vencimiento. Salvo que se diga otra cosa,
el producto es la Yerba de `_yerba_de_referencia`: 10 «sin lote» en stock, rota 1 por día (`r = 1`), horizonte 15 + 3 = 18, «hoy» el 30 de septiembre de 2026.
"""
from __future__ import annotations

import datetime
from fractions import Fraction

import pytest
import test_reposicion as _rep
from test_reposicion import (
    HOY,
    PREVIA,
    _dos_sucursales,
    _lote,
    _por_nombre,
    _producto,
    _reporte,
    _venta,
    _yerba_de_hoy,
    _yerba_de_referencia,
)

from libracommerce.erp import catalogo, reposicion, vencimientos

destino = _rep.destino
abrir_vto_ventas = _rep.abrir_vto_ventas


def _en(dias: int, base: datetime.date = HOY) -> str:
    """La fecha `dias` después de `base` (por default «hoy» de las pruebas), en ISO."""
    return (base + datetime.timedelta(days=dias)).isoformat()


def _fila(abrir, nombre="Yerba", **kw):
    kw.setdefault("solo_a_pedir", False)
    return _por_nombre(_reporte(abrir, **kw))[nombre]


def _con(abrir, nombre="Yerba", **kw):
    """La fila con la opción prendida."""
    return _fila(abrir, nombre, descontar_por_vencer=True, **kw)


# ── La cuenta, sin base: lo que decide el máximo acumulado y el +1 ───────────


def _lotes(*pares):
    """`[(vence, saldo)]` desde `[(días desde hoy, saldo)]`, ya en el orden de FEFO."""
    return sorted((HOY + datetime.timedelta(days=d), Fraction(s)) for d, s in pares)


def test_un_lote_que_no_alcanza_a_venderse_pierde_la_diferencia():
    # Vence en 5 días (d = 6): hasta entonces se venden 6 de los 20.
    assert reposicion._por_vencer(_lotes((5, 20)), HOY, 18, Fraction(1)) == 14


def test_un_lote_que_si_se_vende_no_pierde_nada():
    assert reposicion._por_vencer(_lotes((5, 6)), HOY, 18, Fraction(1)) == 0           # exactamente lo que se vende: 6 en 6 días
    assert reposicion._por_vencer(_lotes((5, 5)), HOY, 18, Fraction(1)) == 0           # y menos: nunca negativa


def test_con_varios_lotes_es_el_maximo_del_acumulado_y_no_la_suma():
    # A: 10 que vencen en d = 3; B: 10 que vencen en d = 11. Acumulados 10 y 20; se venden 3 y 11: sobran 7 y 9.
    # La pérdida es 9 (lo que sobra al vencer B, con A ya contada adentro): ni 16 (suma de lo que sobra en cada corte) ni 7 (A sola) ni 20 (la suma de saldos).
    assert reposicion._por_vencer(_lotes((2, 10), (10, 10)), HOY, 18, Fraction(1)) == 9


def test_el_maximo_puede_estar_en_el_primer_lote():
    # A: 10 en d = 3 (sobran 7); B: 2 en d = 11: acumulado 12 − 11 = 1. Gana A.
    assert reposicion._por_vencer(_lotes((2, 10), (10, 2)), HOY, 18, Fraction(1)) == 7


def test_dos_lotes_del_mismo_dia_valen_como_uno():
    # Dos lotes del mismo día valen como uno.
    assert reposicion._por_vencer(_lotes((2, 6), (2, 4)), HOY, 18, Fraction(1)) == 7


def test_el_dia_del_vencimiento_todavia_se_vende_y_hoy_cuenta():
    # Vence hoy: d = 1 (se vende lo de hoy). Sin el +1 sería d = 0 y se perdería todo.
    assert reposicion._por_vencer(_lotes((0, 5)), HOY, 18, Fraction(1)) == 4
    # Vence mañana: d = 2.
    assert reposicion._por_vencer(_lotes((1, 5)), HOY, 18, Fraction(1)) == 3


def test_el_borde_del_horizonte_entra_y_un_dia_mas_afuera_no():
    assert reposicion._por_vencer(_lotes((17, 20)), HOY, 18, Fraction(1)) == 2          # d = 18 = H: adentro
    assert reposicion._por_vencer(_lotes((18, 20)), HOY, 18, Fraction(1)) == 0          # d = 19 > H: afuera


def test_un_lote_fuera_del_horizonte_no_suma_al_acumulado_de_los_de_adentro():
    assert reposicion._por_vencer(_lotes((2, 10), (30, 500)), HOY, 18, Fraction(1)) == 7


def test_los_ya_vencidos_no_entran():
    assert reposicion._por_vencer(_lotes((-3, 50), (2, 10)), HOY, 18, Fraction(1)) == 7


def test_sin_rotacion_se_pierde_todo_lo_que_vence_dentro_del_horizonte():
    assert reposicion._por_vencer(_lotes((2, 10), (10, 10), (40, 99)), HOY, 18, Fraction(0)) == 20


def test_la_rotacion_fraccionaria_es_exacta():
    # r = 1/3; d = 4: se venden 4/3 y sobran 10 − 4/3 = 26/3, sin restos de punto flotante ni de Decimal.
    assert reposicion._por_vencer(_lotes((3, 10)), HOY, 13, Fraction(1, 3)) == Fraction(26, 3)


def test_sin_lotes_no_hay_perdida():
    assert reposicion._por_vencer([], HOY, 18, Fraction(1)) == 0


# ── Con la base ──────────────────────────────────────────────────────────


def test_un_lote_que_no_alcanza_a_venderse_se_descuenta_y_cambia_la_sugerencia(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    _lote(abrir, yerba, "A", _en(5), 20)                          # stock 30: 10 sin lote + 20 que vencen en 5 días
    sin = _fila(abrir)
    assert sin["stock"] == 30 and sin["por_vencer"] == 0 and sin["sugerido"] == 0 and sin["cobertura_dias"] == 30.0
    con = _con(abrir)
    # Se venden 6 (d = 6) y sobran 14: utilizable 30 − 14 = 16; 18 − 16 = 2.
    assert (con["stock"], con["vencido"], con["por_vencer"]) == (30, 0, 14.0)
    assert con["sugerido"] == 2 and con["motivo"] == "por_rotacion" and con["cobertura_dias"] == 16.0


def test_un_lote_que_si_se_vende_no_cambia_nada(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    _lote(abrir, yerba, "A", _en(5), 6)
    assert _con(abrir) == dict(_fila(abrir), por_vencer=0.0)


def test_varios_lotes_descuentan_el_maximo_acumulado(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    _lote(abrir, yerba, "A", _en(2), 10)
    _lote(abrir, yerba, "B", _en(10), 10)
    con = _con(abrir)
    assert con["stock"] == 30 and con["por_vencer"] == 9.0
    assert con["sugerido"] == 0 and con["cobertura_dias"] == 21.0                # 30 − 9 = 21 ≥ 18: nada que pedir
    assert _con(abrir, dias_cobertura=30)["sugerido"] == 12                        # horizonte 33, r = 1: la misma pérdida (9), 33 − 21


def test_vence_hoy_todavia_se_vende_lo_de_hoy(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    _lote(abrir, yerba, "H", _en(0), 5)
    assert _con(abrir)["por_vencer"] == 4.0                                        # d = 1: se vende 1 de los 5 (sin el +1 se perderían los 5)


def test_vence_manana_se_venden_dos_dias(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    _lote(abrir, yerba, "M", _en(1), 5)
    assert _con(abrir)["por_vencer"] == 3.0                                        # d = 2
    _lote(abrir, yerba, "H", _en(0), 5)                                            # hoy y mañana juntos: acumulado 10 − 2 = 8 (contra 5 − 1 = 4 de hoy)
    assert _con(abrir)["por_vencer"] == 8.0


def test_un_lote_fuera_del_horizonte_no_cuenta(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    _lote(abrir, yerba, "L", _en(18), 20)                                          # d = 19 > 18
    assert _con(abrir)["por_vencer"] == 0.0
    _lote(abrir, yerba, "J", _en(17), 20)                                          # d = 18 = H: entra
    assert _con(abrir)["por_vencer"] == 2.0                                        # J solo (L, que vence después, no suma): 20 − 18


def test_horizonte_propio_del_producto(abrir_vto_ventas):
    """El plazo propio mueve el horizonte del producto y, con él, qué lotes entran y cuánto se proyecta."""
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    _lote(abrir, yerba, "L", _en(20), 40)                                          # d = 21: afuera con H = 18, adentro con H = 25
    assert _con(abrir)["por_vencer"] == 0.0
    with abrir() as conn:
        reposicion.fijar_parametros(conn, yerba, plazo_entrega_dias=10, stock_maximo=None)
        conn.commit()
    fila = _con(abrir)
    assert fila["plazo_entrega_dias"] == 10 and fila["por_vencer"] == 19.0         # H = 25: r = 1 (25/25), sobran 40 − 21


def test_lo_ya_vencido_no_se_duplica(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    _lote(abrir, yerba, "V", "2026-09-01", 6)                                      # ya vencido
    _lote(abrir, yerba, "A", _en(5), 20)
    con = _con(abrir)
    # stock 36: vencido 6 (cuenta una vez), por vencer 14 (el vencido no entra en el acumulado): utilizable 36 − 6 − 14 = 16.
    assert (con["stock"], con["vencido"], con["por_vencer"], con["cobertura_dias"], con["sugerido"]) == (36, 6, 14.0, 16.0, 2)


def test_sin_descontar_vencido_lo_vencido_tampoco_entra_en_por_vencer(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    _lote(abrir, yerba, "V", "2026-09-01", 6)
    con = _con(abrir, descontar_vencido=False)
    assert (con["vencido"], con["por_vencer"]) == (0, 0.0)
    # Y el interruptor de lo por vencer anda solo: con lotes por vencer y sin descontar lo vencido, sólo se descuenta lo por vencer.
    _lote(abrir, yerba, "A", _en(5), 20)
    con = _con(abrir, descontar_vencido=False)
    assert (con["vencido"], con["por_vencer"], con["cobertura_dias"]) == (0, 14.0, 22.0)    # 36 − 14


def test_un_producto_sin_marcar_no_cambia(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    _lote(abrir, yerba, "A", _en(5), 20)
    with abrir() as conn:
        vencimientos.marcar_vence(conn, yerba, False)
        conn.commit()
    assert _con(abrir) == _fila(abrir) and _con(abrir)["por_vencer"] == 0.0


def test_sin_rotacion_se_pierde_todo_lo_que_vence_dentro_del_horizonte_y_el_minimo_manda(abrir_vto_ventas):
    """El borde dicho en voz alta: un producto sin ventas con un lote por vencer y mínimo > 0 se sugiere reponer."""
    abrir = abrir_vto_ventas
    with abrir() as conn:
        quieto = _producto(conn, "Quieto", minimo=10.0)
    _lote(abrir, quieto, "A", _en(10), 8)                                          # vence dentro del horizonte
    _lote(abrir, quieto, "B", _en(40), 7)                                          # afuera: queda como stock
    sin = _fila(abrir, "Quieto")
    assert sin["sin_ventas"] is True and sin["stock"] == 15 and sin["sugerido"] == 0     # 15 ≥ 10
    con = _con(abrir, "Quieto")
    assert con["por_vencer"] == 8.0 and con["sugerido"] == 3 and con["motivo"] == "bajo_minimo"     # 15 − 8 = 7 < 10: pide 3
    assert con["cobertura_dias"] is None


def test_sin_rotacion_y_sin_minimo_no_se_sugiere_nada(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    with abrir() as conn:
        quieto = _producto(conn, "Quieto")
    _lote(abrir, quieto, "A", _en(10), 8)
    con = _con(abrir, "Quieto")
    assert con["por_vencer"] == 8.0 and con["sugerido"] == 0 and con["motivo"] is None


def test_el_saldo_sin_lote_no_cuenta(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)                                            # 10 sin lote
    _lote(abrir, yerba, "A", _en(2), 4)                                            # d = 3: sobra 1
    con = _con(abrir)
    assert con["stock"] == 14 and con["por_vencer"] == 1.0                         # con lo «sin lote» adentro del acumulado serían 11


def test_con_estacionalidad_usa_la_proyeccion_con_el_factor(abrir_vto_ventas):
    import test_estacionalidad as _est

    abrir = abrir_vto_ventas
    pid = _est._helado(abrir)                                                      # rota 1 por día; hace un año: factor 3
    _lote(abrir, pid, "A", _en(10), 30)                                            # d = 11
    sin_factor = _con(abrir, "Helado")
    assert sin_factor["factor_estacional"] is None and sin_factor["por_vencer"] == 19.0      # 30 − 11 × 1
    con_factor = _con(abrir, "Helado", estacionalidad=True)
    assert con_factor["factor_estacional"] == 3.0 and con_factor["por_vencer"] == 0.0         # se venden 33 ≥ 30


def test_la_opcion_apagada_es_la_cuenta_de_antes(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    _lote(abrir, yerba, "V", "2026-09-01", 6)
    _lote(abrir, yerba, "A", _en(5), 20)
    a = _fila(abrir)
    assert a == _fila(abrir, descontar_por_vencer=False)
    assert a["por_vencer"] == 0.0 and a["vencido"] == 6 and a["cobertura_dias"] == 30.0 and a["sugerido"] == 0     # sólo lo vencido: 36 − 6


def test_sin_marcados_la_opcion_no_cambia_ninguna_fila(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    _yerba_de_referencia(abrir)
    with abrir() as conn:
        _producto(conn, "Sal", inicial=2.0, minimo=10.0)
    assert _reporte(abrir, descontar_por_vencer=True) == _reporte(abrir)
    assert _reporte(abrir, descontar_por_vencer=True, solo_a_pedir=False) == _reporte(abrir, solo_a_pedir=False)


def test_una_base_sin_la_revision_0002_no_falla_ni_descuenta(abrir_ventas):
    abrir = abrir_ventas                                                           # sin la revisión 0002
    _yerba_de_referencia(abrir)
    fila = _por_nombre(_reporte(abrir, descontar_por_vencer=True))["Yerba"]
    assert fila["por_vencer"] == 0.0 and fila["sugerido"] == 8


def test_cada_sucursal_descuenta_lo_que_vence_en_sus_depositos(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    a, b, dep_a, dep_b = _dos_sucursales(abrir)
    with abrir() as conn:
        yerba = _producto(conn, "Yerba")
    _lote(abrir, yerba, "A", _en(5), 20, deposito=dep_a)                           # Centro
    _lote(abrir, yerba, "B", _en(5), 20, deposito=dep_b)                           # Norte: el mismo vencimiento
    # Sin ventas: lo que vence dentro del horizonte se pierde completo, en la sucursal que lo tiene.
    assert _con(abrir, sucursal_id=a)["por_vencer"] == 20.0
    assert _con(abrir, sucursal_id=b)["por_vencer"] == 20.0
    assert _con(abrir)["por_vencer"] == 40.0                                       # toda la instancia: los dos depósitos


def test_la_misma_consulta_de_lotes_sirve_a_lo_vencido_y_a_lo_por_vencer(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    _lote(abrir, yerba, "V", "2026-09-01", 6)
    _lote(abrir, yerba, "A", _en(5), 20)
    antes, pedidos = reposicion.saldos_por_bucket, []

    def espia(c, donde, params, depositos=None):
        pedidos.append(donde)
        return antes(c, donde, params, depositos)

    reposicion.saldos_por_bucket = espia
    try:
        _reporte(abrir, descontar_por_vencer=True)
    finally:
        reposicion.saldos_por_bucket = antes
    assert len(pedidos) == 1


# ── La escala de la unidad y la exactitud ────────────────────────────────


def _exacto(abrir, *, unidad="u", fraccion=None, vendidas=10, cantidad=10, vence_en=3):
    """Un producto que vendió `vendidas` en 30 días y tiene un solo lote de `cantidad` que vence en `vence_en` días (`d = vence_en + 1`); `stock` = `cantidad`."""
    with abrir() as conn:
        pid = _producto(conn, "Exacto", inicial=float(vendidas), unidad=unidad, fraccion=fraccion)
    _venta(abrir, [(pid, "Exacto", vendidas, 100.0)], "2026-09-20")
    _lote(abrir, pid, "A", _en(vence_en), cantidad)
    return pid


def test_un_resto_decimal_no_pide_una_unidad_de_mas(abrir_vto_ventas):
    """1 vendida en 30 días (`r = 1/30`), horizonte 2 + 3 = 5 y un lote de 1 que vence en d = 5: proyectado 1/6, pérdida 1 − 5/30 = 5/6, utilizable 1/6, y `1/6 − 1/6` es exactamente 0.
    Con cocientes de `Decimal` (`1/30 × 5`, `1 − …`) la resta deja un resto de 1e-28 y el techo pide 1 unidad de más (medido al diseñarlo)."""
    abrir = abrir_vto_ventas
    _exacto(abrir, vendidas=1, cantidad=1, vence_en=4)
    fila = _con(abrir, "Exacto", dias_cobertura=2)
    assert fila["stock"] == 1 and fila["por_vencer"] == 1                          # 5/6 informado hacia arriba a la escala de la unidad (entera: 1)
    assert fila["sugerido"] == 0 and fila["motivo"] is None


def test_una_necesidad_fraccionaria_que_da_un_entero_exacto_pide_ese_entero(abrir_vto_ventas):
    """10 vendidas en 30 días (`r = 1/3`), horizonte 10 + 3 = 13 y un lote de 10 que vence en d = 4: proyectado 13/3, pérdida 26/3, utilizable 4/3 y la necesidad `13/3 − 4/3` es exactamente 3."""
    abrir = abrir_vto_ventas
    _exacto(abrir)
    fila = _con(abrir, "Exacto", dias_cobertura=10)
    assert fila["stock"] == 10 and fila["por_vencer"] == 9                         # 26/3 informado hacia arriba, a la escala de la unidad (entera)
    assert fila["sugerido"] == 3 and isinstance(fila["sugerido"], int)             # y la cuenta sigue con la pérdida exacta: con el 9 redondeado saldría 4


def test_una_unidad_fraccionable_informa_y_pide_con_su_escala(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    _exacto(abrir, unidad="kg", fraccion=True)
    fila = _con(abrir, "Exacto", dias_cobertura=11)                                          # horizonte 14: proyectado 14/3, pérdida 26/3, disponible 4/3
    assert fila["por_vencer"] == 8.667 and fila["sugerido"] == 3.334               # 10/3 hacia arriba, a 3 decimales


def test_una_unidad_de_escala_fina_informa_la_perdida_con_su_escala(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    with abrir() as conn:
        fino = _rep._unidad_fina(conn, "Fino", inicial=None)
    _lote(abrir, fino, "A", _en(10), 0.000123)                                     # sin ventas: lo que vence dentro del horizonte se pierde entero
    fila = _con(abrir, "Fino")
    assert fila["stock"] == 0.000123 and fila["por_vencer"] == 0.000123            # con 3 decimales saldría 0,001


def test_por_vencer_de_una_unidad_entera_sale_entero_y_no_con_decimales_de_mas(abrir_vto_ventas):
    """ADR-028: antes `por_vencer` iba con los 3 decimales mínimos del informe y una unidad entera mostraba `8.667` donde `sugerido` mostraba enteros. Ahora va con la escala de la
    unidad (0): hacia arriba, sin tocar la cuenta."""
    abrir = abrir_vto_ventas
    _exacto(abrir)                                                                 # pérdida exacta 26/3 = 8,666...
    fila = _con(abrir, "Exacto", dias_cobertura=10)
    assert fila["por_vencer"] == 9 and isinstance(fila["por_vencer"], int)
    assert isinstance(fila["sugerido"], int) and fila["sugerido"] == 3             # la pérdida exacta: con el 9 redondeado saldría 4
    assert fila["cobertura_dias"] == 4.0                                           # utilizable 10 − 26/3 = 4/3 con la pérdida exacta, ÷ r = 1/3; con el 9 redondeado serían 3
    assert fila["stock"] == 10 and fila["vencido"] == 0


def test_por_vencer_de_una_unidad_fraccionable_va_con_los_tres_decimales_de_kg(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    _exacto(abrir, unidad="kg", fraccion=True)
    fila = _con(abrir, "Exacto", dias_cobertura=10)                                # la misma pérdida exacta, 26/3
    assert fila["por_vencer"] == 8.667 and isinstance(fila["por_vencer"], float)   # 3 decimales, hacia arriba (no 8.666 ni 9)
    assert fila["sugerido"] == 3 and isinstance(fila["sugerido"], int | float)


def test_por_vencer_de_una_unidad_fraccionable_de_un_decimal_va_con_uno(abrir_vto_ventas):
    """Una unidad con `decimal_scale` bajo (1) informa la pérdida con ese decimal y no con los 3 mínimos del informe: 26/3 hacia arriba a 1 decimal es 8,7 (con 3 era 8,667); `sugerido` sigue siendo 3 (la necesidad exacta, 13/3 − 4/3)."""
    abrir = abrir_vto_ventas
    _exacto(abrir, unidad="kg", fraccion=True)
    with abrir() as conn:
        conn.execute("UPDATE units SET decimal_scale=1 WHERE code='kg'")
    fila = _con(abrir, "Exacto", dias_cobertura=10)
    assert fila["por_vencer"] == 8.7 and fila["sugerido"] == 3


@pytest.mark.parametrize("valor, escala, techo, piso", [
    (Fraction(10, 3), 0, 4, 3), (Fraction(10, 3), 3, "3.334", "3.333"), (Fraction(3), 0, 3, 3), (Fraction(-7, 3), 0, -2, -3),
    (Fraction(1, 3), 6, "0.333334", "0.333333"), (Fraction(0), 3, 0, 0),
])
def test_techo_y_piso_de_un_fraction_son_exactos(valor, escala, techo, piso):
    from decimal import Decimal
    assert reposicion._techo(valor, escala) == Decimal(str(techo)) and reposicion._piso(valor, escala) == Decimal(str(piso))
    # y coinciden con los de `Decimal` cuando el valor es decimal exacto
    exacto = Decimal(valor.numerator) / Decimal(valor.denominator)
    if valor.denominator in (1, 2, 4, 5, 8, 10):
        assert reposicion._techo(valor, escala) == reposicion._techo(exacto, escala)


# ── Las órdenes en borrador, el router y el CSV ──────────────────────────


def test_las_ordenes_en_borrador_usan_el_mismo_ajuste_que_se_ve(abrir_vto_ventas):
    from test_proveedor_por_producto import _con_proveedor, _nuevo_tercero

    from libracommerce.erp import reposicion_ordenes

    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    _lote(abrir, yerba, "A", _en(5), 20)
    _con_proveedor(abrir, yerba, _nuevo_tercero(abrir, "Distribuidora Norte"))

    def generar(clave, **kw):
        with abrir() as conn:
            r = reposicion_ordenes.generar_ordenes_borrador(conn, clave_operacion=clave, hoy=HOY, **kw)
            conn.commit()
        return r

    assert generar("sin").get("ordenes") == []                                     # stock 30 ≥ 18: nada que pedir
    r = generar("con", descontar_por_vencer=True)
    assert [float(li["cantidad"]) for o in r["ordenes"] for li in o["lineas"]] == [2.0]       # lo mismo que la fila: sugerido 2
    # La misma clave con y sin la opción son pedidos distintos (la huella incluye el parámetro).
    with pytest.raises(reposicion_ordenes.ClaveReusada):
        generar("con")


def test_el_router_de_ordenes_acepta_la_opcion_en_el_cuerpo(abrir_vto_ventas):
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient
    from test_proveedor_por_producto import _con_proveedor, _nuevo_tercero

    from libracommerce.web.reposicion_router import build_reposicion_ordenes_router

    abrir = abrir_vto_ventas
    yerba = _yerba_de_hoy(abrir)
    hoy = datetime.date.today()
    _lote(abrir, yerba, "A", _en(5, hoy), 20)
    _con_proveedor(abrir, yerba, _nuevo_tercero(abrir, "Distribuidora Norte"))
    app = FastAPI()
    app.include_router(build_reposicion_ordenes_router(conexion=abrir, usuario_actual=lambda: {"id": 7},
                                                       dependencias_escribir=[Depends(lambda: None)]))
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/api/reportes/reposicion/ordenes", json={"clave_operacion": "k1", "descontar_por_vencer": True})
    assert r.status_code == 200, r.text
    assert [float(li["cantidad"]) for o in r.json()["ordenes"] for li in o["lineas"]] == [2.0]
    assert c.post("/api/reportes/reposicion/ordenes", json={"clave_operacion": "k2", "descontar_por_vencer": "quizás"}).status_code == 422


def test_el_router_acepta_la_opcion_la_devuelve_y_rechaza_lo_que_no_es_booleano(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_hoy(abrir)
    _lote(abrir, yerba, "A", _en(5, datetime.date.today()), 20)
    c = _rep._cliente(abrir)
    sin = c.get("/api/reportes/reposicion", params={"solo_a_pedir": "false"})
    assert sin.status_code == 200 and sin.json()["descontar_por_vencer"] is False
    assert sin.json()["productos"][0]["por_vencer"] == 0.0
    con = c.get("/api/reportes/reposicion", params={"descontar_por_vencer": "true", "solo_a_pedir": "false"})
    assert con.status_code == 200 and con.json()["descontar_por_vencer"] is True
    [fila] = con.json()["productos"]
    assert fila["por_vencer"] == 14.0 and fila["sugerido"] == 2
    for ruta in ("/api/reportes/reposicion", "/api/reportes/reposicion/export"):
        assert c.get(ruta, params={"descontar_por_vencer": "quizás"}).status_code == 422, ruta


def test_el_csv_trae_por_vencer_al_final(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_hoy(abrir)
    _lote(abrir, yerba, "A", _en(5, datetime.date.today()), 20)
    c = _rep._cliente(abrir)
    for params, esperado in (({}, "0"), ({"descontar_por_vencer": "true"}, "14")):   # la yerba es de unidades enteras: sin decimales (ADR-028)
        lineas = c.get("/api/reportes/reposicion/export", params={"solo_a_pedir": "false", **params}).text.splitlines()
        cabecera = lineas[0].split(",")
        assert cabecera[-2:] == ["stock_minimo_propio", "por_vencer"]
        assert lineas[1].split(",")[-1] == esperado
