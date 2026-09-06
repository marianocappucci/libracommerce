"""Los contratos de la capa ERP y la guarda de la capa web (M0 de P9).

Lo que se fija: que el default de cada gancho sea el comportamiento de
Contalibra (no hacer nada), que un gancho enganchado reciba exactamente lo que
el contrato promete, y que los dos subpaquetes nuevos se importen sin sus
extras — `erp.hooks` para tipar del lado del producto, `web` para que la
ausencia de FastAPI se lea como lo que es.
"""

from __future__ import annotations

import dataclasses
import sys
from decimal import Decimal

import pytest

from libracommerce import web
from libracommerce.erp import SIN_GANCHOS, Hooks, Insumo


def test_los_defaults_no_hacen_nada():
    assert SIN_GANCHOS.resolver_receta(7) is None
    assert SIN_GANCHOS.al_confirmar_venta("conn", "venta") is None
    assert SIN_GANCHOS.al_anular_venta("conn", "venta") is None
    assert SIN_GANCHOS.lista_de_precio_para("conn", 3) is None
    assert SIN_GANCHOS.canales == ()


def test_un_gancho_enganchado_recibe_la_misma_conexion_y_la_venta():
    """🔴 La conexión que llega al gancho es la de la transacción de la venta:
    se aserta identidad (`is`), no igualdad, porque una copia o una conexión
    nueva harían lo mismo en el test y perderían la atomicidad en producción."""
    recibido = {}
    conn = object()
    venta = object()

    def al_confirmar(c, v):
        recibido["conn"], recibido["venta"] = c, v

    def receta(item_id):
        return [Insumo(item_id=100 + item_id, cantidad=Decimal("0.250"))]

    ganchos = Hooks(al_confirmar_venta=al_confirmar, resolver_receta=receta, canales=("mostrador", "delivery"))
    ganchos.al_confirmar_venta(conn, venta)
    assert recibido["conn"] is conn and recibido["venta"] is venta
    assert ganchos.resolver_receta(5) == [Insumo(item_id=105, cantidad=Decimal("0.250"))]
    assert ganchos.canales == ("mostrador", "delivery")
    # Los que no se engancharon siguen con el default.
    assert ganchos.al_anular_venta(conn, venta) is None


def test_los_ganchos_son_inmutables():
    with pytest.raises(dataclasses.FrozenInstanceError):
        SIN_GANCHOS.canales = ("x",)  # type: ignore[misc]


def test_web_devuelve_fastapi_cuando_esta_el_extra():
    modulo = web.fastapi()
    assert hasattr(modulo, "APIRouter")


def test_web_dice_que_extra_falta_cuando_no_esta_fastapi(monkeypatch):
    """Simula el entorno sin el extra `[web]`: `None` en `sys.modules` hace que
    el import falle como si el paquete no estuviera instalado."""
    monkeypatch.setitem(sys.modules, "fastapi", None)
    with pytest.raises(web.SinFastAPI, match=r"\[web\]"):
        web.fastapi()
