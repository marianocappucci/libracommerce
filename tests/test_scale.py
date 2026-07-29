from decimal import Decimal

import pytest

from libracommerce.domain.scale import (
    ScaleFormat,
    ScaleValueKind,
    parse_scale_barcode,
)


PESO = ScaleFormat()  # 20 + 5 codigo + 5 gramos + checksum
IMPORTE = ScaleFormat(value_kind=ScaleValueKind.AMOUNT, divisor=100)


def test_lee_el_peso_de_una_etiqueta_estandar():
    # 20 | 00123 | 00750 | 4  ->  producto 123, 750 gramos
    leido = parse_scale_barcode("2000123007504", PESO)
    assert leido is not None
    assert leido.item_code == "123"
    assert leido.kind is ScaleValueKind.WEIGHT
    assert leido.value == Decimal("0.750")


def test_lee_el_importe_cuando_la_balanza_ya_calculo():
    # mismos digitos, otra configuracion: 750 pasa a ser $7,50
    leido = parse_scale_barcode("2000123007504", IMPORTE)
    assert leido is not None
    assert leido.kind is ScaleValueKind.AMOUNT
    assert leido.value == Decimal("7.50")


def test_un_codigo_de_barras_comun_no_es_una_etiqueta_de_balanza():
    # Un EAN de fabrica: mismo largo, otro prefijo. No es error, simplemente
    # no le corresponde a este parser -- el caller sigue con su busqueda.
    assert parse_scale_barcode("7791234567890", PESO) is None


def test_ignora_lo_que_no_es_del_formato_del_comercio():
    assert parse_scale_barcode("", PESO) is None
    assert parse_scale_barcode("20", PESO) is None
    assert parse_scale_barcode("200012300750", PESO) is None  # 12 digitos
    assert parse_scale_barcode("20001230075040", PESO) is None  # 14 digitos
    assert parse_scale_barcode("2000123ABC504", PESO) is None


def test_tolera_espacios_del_lector():
    assert parse_scale_barcode("  2000123007504\n", PESO) is not None


def test_una_etiqueta_sin_peso_no_se_puede_cobrar():
    # Peso cero es una etiqueta mal impresa, no una venta de cero kilos.
    assert parse_scale_barcode("2000123000000", PESO) is None


def test_una_etiqueta_sin_producto_no_resuelve():
    # El codigo de producto en cero no identifica nada; sin esto quedaria
    # como cadena vacia y buscaria un item con codigo "".
    assert parse_scale_barcode("2000000007504", PESO) is None


def test_el_prefijo_puede_ser_de_un_solo_digito():
    # Algunas balanzas usan todo el rango 2x: el prefijo es "2" y el codigo
    # de producto se lleva un digito mas.
    fmt = ScaleFormat(prefix="2", code_digits=6)
    leido = parse_scale_barcode("2000123007504", fmt)
    assert leido is not None
    assert leido.item_code == "123"
    assert leido.value == Decimal("0.750")


def test_soporta_una_balanza_que_emite_ean_8():
    fmt = ScaleFormat(prefix="2", code_digits=2, value_digits=4, total_digits=8)
    leido = parse_scale_barcode("20701250", fmt)
    assert leido is not None
    assert leido.item_code == "7"
    assert leido.value == Decimal("0.125")


def test_un_formato_incoherente_se_rechaza_al_declararlo():
    # Mejor romper al guardar la configuracion que devolver pesos absurdos
    # en cada venta.
    with pytest.raises(ValueError):
        ScaleFormat(code_digits=9, value_digits=9)
    with pytest.raises(ValueError):
        ScaleFormat(prefix="")
    with pytest.raises(ValueError):
        ScaleFormat(prefix="2X")
    with pytest.raises(ValueError):
        ScaleFormat(divisor=0)
