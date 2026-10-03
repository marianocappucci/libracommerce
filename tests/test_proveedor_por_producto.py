"""Proveedor habitual por producto (ADR-021, revisión 0004): la columna, `fijar_parametros`, la reposición y el router.
Las fixtures y los helpers son los de `tests/test_reposicion.py` y `tests/test_vencimientos.py` (una base por motor)."""
from __future__ import annotations

import pytest
import test_reposicion as _rep
import test_vencimientos as _vto
from alembic import command
from test_reposicion import (
    _fijar,
    _orden,
    _por_nombre,
    _producto,
    _proveedor,
    _reporte,
    _yerba_de_referencia,
)
from test_vencimientos import _columnas_de, _con_conexion, _crear_base_al_dia_de_0001, _ledger, catalogo, migrar, stock

from libracommerce.erp import reposicion

PREVIA = _rep.PREVIA
# Las fixtures de las dos suites hermanas (una base por motor).
destino = _vto.destino
abrir_vto_ventas = _rep.abrir_vto_ventas


def _nuevo_tercero(abrir, nombre="Distribuidora Norte", activo=True) -> int:
    from libracommerce.db.repository import repositorio_de
    from libracommerce.domain.entities import Party, PartyType

    with abrir() as conn:
        pid = repositorio_de(conn).save_party(Party(None, PartyType.ORGANIZATION, nombre)).id
        if not activo:
            conn.execute("UPDATE parties SET active = 0 WHERE id = ?", (pid,))
        conn.commit()
    return pid


def _con_proveedor(abrir, producto, proveedor):
    with abrir() as conn:
        r = reposicion.fijar_parametros(conn, producto, plazo_entrega_dias=None, stock_maximo=None, proveedor_id=proveedor)
        conn.commit()
    return r


# ── La revisión 0004 ─────────────────────────────────────────────────────


def test_la_revision_0004_corre_sobre_datos_previos_sin_tocar_una_fila_y_es_idempotente(destino):
    _crear_base_al_dia_de_0001(destino)
    migrar.upgrade(destino, "0003_parametros_reposicion")

    def poblar(conn):
        yerba = catalogo.create_producto(conn, "Yerba", precio_venta=100.0, precio_costo=60.0)
        stock.ajustar_stock(conn, yerba, 40.0, "inicial", fecha=PREVIA)
        conn.commit()
        return _ledger(conn)

    antes = _con_conexion(destino, poblar)
    migrar.upgrade(destino)

    def verificar(conn):
        assert "supplier_party_id" in _columnas_de(conn, "catalog_items")
        assert [f[0] for f in conn.execute("SELECT supplier_party_id FROM catalog_items").fetchall()] == [None]
        assert _ledger(conn) == antes
        return [f[0] for f in conn.execute("SELECT version_num FROM alembic_version_libracommerce").fetchall()]

    assert _con_conexion(destino, verificar) == [_vto._CABEZA]
    migrar.upgrade(destino)
    assert _con_conexion(destino, verificar) == [_vto._CABEZA]


def test_la_revision_0004_baja_sin_tocar_el_ledger_ni_los_parametros(destino):
    _crear_base_al_dia_de_0001(destino)
    migrar.upgrade(destino)

    def poblar(conn):
        yerba = catalogo.create_producto(conn, "Yerba", precio_venta=100.0, precio_costo=60.0)
        stock.ajustar_stock(conn, yerba, 40.0, "inicial", fecha=PREVIA)
        conn.execute("UPDATE catalog_items SET lead_time_days = 7, max_stock = 90 WHERE id = ?", (yerba,))
        conn.commit()
        return _ledger(conn)

    antes = _con_conexion(destino, poblar)
    command.downgrade(migrar.configuracion(destino), "0003_parametros_reposicion")

    def verificar(conn):
        assert "supplier_party_id" not in _columnas_de(conn, "catalog_items")
        assert conn.execute("SELECT lead_time_days FROM catalog_items").fetchone()[0] == 7
        assert _ledger(conn) == antes

    _con_conexion(destino, verificar)
    migrar.upgrade(destino)


# ── fijar_parametros y la reposición ─────────────────────────────────────


def test_fijar_el_proveedor_lo_guarda_lo_devuelve_y_lo_muestra_la_reposicion(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    prov = _nuevo_tercero(abrir)
    r = _con_proveedor(abrir, yerba, prov)
    assert r["proveedor_id"] == prov and r["proveedor"] == "Distribuidora Norte"
    fila = _por_nombre(_reporte(abrir))["Yerba"]
    assert fila["proveedor_id"] == prov and fila["proveedor"] == "Distribuidora Norte"


def test_sin_proveedor_la_fila_lo_dice_con_none(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    _yerba_de_referencia(abrir)
    fila = _por_nombre(_reporte(abrir))["Yerba"]
    assert fila["proveedor_id"] is None and fila["proveedor"] is None


def test_el_filtro_por_proveedor_deja_solo_sus_productos(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    with abrir() as conn:
        sal = _producto(conn, "Sal", inicial=2.0, minimo=10.0)
    norte, sur = _nuevo_tercero(abrir, "Norte"), _nuevo_tercero(abrir, "Sur")
    _con_proveedor(abrir, yerba, norte)
    _con_proveedor(abrir, sal, sur)
    assert list(_por_nombre(_reporte(abrir, proveedor_id=norte))) == ["Yerba"]
    assert list(_por_nombre(_reporte(abrir, proveedor_id=sur))) == ["Sal"]
    assert set(_por_nombre(_reporte(abrir))) == {"Yerba", "Sal"}
    with pytest.raises(ValueError, match="no existe"):
        _reporte(abrir, proveedor_id=99999)


def test_cambiar_otros_parametros_sin_mandar_el_proveedor_lo_deja_como_estaba(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    prov = _nuevo_tercero(abrir)
    _con_proveedor(abrir, yerba, prov)
    r = _fijar(abrir, yerba, plazo=9, techo=50)               # los clientes anteriores no mandan el proveedor
    assert r["proveedor_id"] == prov and r["plazo_entrega_dias"] == 9


def test_none_borra_el_proveedor(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    _con_proveedor(abrir, yerba, _nuevo_tercero(abrir))
    r = _con_proveedor(abrir, yerba, None)
    assert r["proveedor_id"] is None and r["proveedor"] is None


@pytest.mark.parametrize("malo", [99999, True, "3", 2.5])
def test_un_proveedor_invalido_se_rechaza_y_no_escribe_nada(abrir_vto_ventas, malo):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    with abrir() as conn, pytest.raises(ValueError):
        reposicion.fijar_parametros(conn, yerba, plazo_entrega_dias=9, stock_maximo=None, proveedor_id=malo)
    with abrir() as conn:
        p = reposicion.parametros_de(conn, yerba)
        assert p["proveedor_id"] is None and p["plazo_entrega_dias"] is None   # tampoco el plazo: todo o nada


def test_un_proveedor_dado_de_baja_no_se_puede_asignar(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    baja = _nuevo_tercero(abrir, "Cerrada SA", activo=False)
    with abrir() as conn, pytest.raises(ValueError, match="de baja"):
        reposicion.fijar_parametros(conn, yerba, plazo_entrega_dias=None, stock_maximo=None, proveedor_id=baja)


def test_el_proveedor_de_la_orden_de_compra_y_el_habitual_son_cosas_distintas(abrir_vto_ventas):
    """El habitual no toca las órdenes: una orden a otro proveedor sigue contando como «en camino» del producto."""
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    _con_proveedor(abrir, yerba, _nuevo_tercero(abrir, "Habitual"))
    _orden(abrir, _proveedor(abrir), [(yerba, 5)])
    fila = _por_nombre(_reporte(abrir))["Yerba"]
    assert fila["en_camino"] == 5 and fila["proveedor"] == "Habitual"


def test_una_base_sin_la_0004_no_falla_y_no_tiene_proveedores(abrir_ventas):
    abrir = abrir_ventas                                       # ni la 0003 ni la 0004
    _yerba_de_referencia(abrir)
    fila = _por_nombre(_reporte(abrir))["Yerba"]
    assert fila["proveedor_id"] is None and fila["proveedor"] is None
    assert _reporte(abrir, proveedor_id=None)


def test_fijar_el_proveedor_sin_la_0004_pide_la_revision(destino):
    _crear_base_al_dia_de_0001(destino)
    migrar.upgrade(destino, "0003_parametros_reposicion")

    def intentar(conn):
        yerba = catalogo.create_producto(conn, "Yerba", precio_venta=100.0, precio_costo=60.0)
        with pytest.raises(reposicion.SinRevision, match="0004"):
            reposicion.fijar_parametros(conn, yerba, plazo_entrega_dias=3, stock_maximo=None, proveedor_id=1)
        # Sin pedir proveedor, la 0003 alcanza como siempre.
        reposicion.fijar_parametros(conn, yerba, plazo_entrega_dias=3, stock_maximo=None)

    _con_conexion(destino, intentar)


# ── El router ────────────────────────────────────────────────────────────


def test_el_router_lee_y_escribe_el_proveedor_y_la_clave_ausente_no_lo_toca(abrir_vto_ventas):
    from fastapi import Depends

    from libracommerce.web.reposicion_router import build_reposicion_parametros_router, build_reposicion_router

    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    prov = _nuevo_tercero(abrir)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(build_reposicion_parametros_router(conexion=abrir, dependencias_escribir=[Depends(lambda: None)]))
    app.include_router(build_reposicion_router(conexion=abrir))
    c = TestClient(app)
    ruta = f"/api/productos/{yerba}/reposicion"
    r = c.put(ruta, json={"plazo_entrega_dias": 5, "stock_maximo": None, "proveedor_id": prov})
    assert r.status_code == 200 and r.json()["proveedor_id"] == prov and r.json()["proveedor"] == "Distribuidora Norte"
    r = c.put(ruta, json={"plazo_entrega_dias": 6, "stock_maximo": None})          # clave ausente: el proveedor no cambia
    assert r.status_code == 200 and r.json()["proveedor_id"] == prov and r.json()["plazo_entrega_dias"] == 6
    assert c.get(ruta).json()["proveedor_id"] == prov
    assert c.put(ruta, json={"plazo_entrega_dias": 6, "stock_maximo": None, "proveedor_id": 99999}).status_code == 422
    assert c.get(ruta).json()["proveedor_id"] == prov
    r = c.put(ruta, json={"plazo_entrega_dias": 6, "stock_maximo": None, "proveedor_id": None})   # null lo borra
    assert r.json()["proveedor_id"] is None
    # El filtro del listado.
    c.put(ruta, json={"plazo_entrega_dias": None, "stock_maximo": None, "proveedor_id": prov})
    assert c.get("/api/reportes/reposicion", params={"proveedor_id": prov, "solo_a_pedir": "false"}).json()["proveedor_id"] == prov
    assert c.get("/api/reportes/reposicion", params={"proveedor_id": 99999}).status_code == 422
    csv = c.get("/api/reportes/reposicion/export", params={"solo_a_pedir": "false"}).text.splitlines()
    assert csv[0].endswith(",proveedor_id,proveedor,factor_estacional,stock_minimo_propio") and csv[1].endswith(f",{prov},Distribuidora Norte,,no")


def test_el_router_traduce_los_ids_del_producto_con_los_ganchos_de_compras(abrir_vto_ventas):
    """Un producto cuyos proveedores no son el `party_id` (VentaLibra: offset +100.000) pasa los mismos ganchos que a `OpcionesCompras`: el
    `proveedor_id` del cuerpo, del filtro y de cada respuesta habla en SUS ids; el motor guarda y compara por `party_id`."""
    from fastapi import Depends, FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from libracommerce.web.reposicion_router import build_reposicion_parametros_router, build_reposicion_router

    OFFSET = 100_000

    def resolver(_conn, proveedor_id):
        if proveedor_id < OFFSET:
            raise HTTPException(404, "proveedor inexistente")
        return proveedor_id - OFFSET

    def de(_conn, party_id):
        return party_id + OFFSET

    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    party = _nuevo_tercero(abrir, "Distribuidora Norte")
    app = FastAPI()
    app.include_router(build_reposicion_parametros_router(
        conexion=abrir, dependencias_escribir=[Depends(lambda: None)], resolver_proveedor=resolver, proveedor_de=de))
    app.include_router(build_reposicion_router(conexion=abrir, resolver_proveedor=resolver, proveedor_de=de))
    c = TestClient(app)
    ruta = f"/api/productos/{yerba}/reposicion"
    r = c.put(ruta, json={"plazo_entrega_dias": None, "stock_maximo": None, "proveedor_id": party + OFFSET})
    assert r.status_code == 200 and r.json()["proveedor_id"] == party + OFFSET and r.json()["proveedor"] == "Distribuidora Norte"
    with abrir() as conn:                                                           # el motor guarda el party_id
        assert reposicion.parametros_de(conn, yerba)["proveedor_id"] == party
    assert c.get(ruta).json()["proveedor_id"] == party + OFFSET
    assert c.put(ruta, json={"plazo_entrega_dias": None, "stock_maximo": None, "proveedor_id": 5}).status_code == 404      # lo dice el gancho
    fila = c.get("/api/reportes/reposicion", params={"proveedor_id": party + OFFSET}).json()["productos"][0]
    assert fila["proveedor_id"] == party + OFFSET and fila["proveedor"] == "Distribuidora Norte"
    assert c.get("/api/reportes/reposicion", params={"proveedor_id": 12}).status_code == 404
    # `null` borra y no pasa por los ganchos.
    assert c.put(ruta, json={"plazo_entrega_dias": None, "stock_maximo": None, "proveedor_id": None}).json()["proveedor_id"] is None
    csv = c.get("/api/reportes/reposicion/export", params={"solo_a_pedir": "false"}).text.splitlines()
    assert csv[1].split(",")[-3] == ""                                              # sin proveedor, la columna va vacía


def test_si_la_traduccion_de_la_respuesta_falla_el_put_no_queda_escrito(abrir_vto_ventas):
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from libracommerce.web.reposicion_router import build_reposicion_parametros_router

    def de(_conn, _party_id):
        raise RuntimeError("el party no tiene proveedor")

    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    party = _nuevo_tercero(abrir)
    app = FastAPI()
    app.include_router(build_reposicion_parametros_router(
        conexion=abrir, dependencias_escribir=[Depends(lambda: None)], proveedor_de=de))
    c = TestClient(app, raise_server_exceptions=False)
    r = c.put(f"/api/productos/{yerba}/reposicion", json={"plazo_entrega_dias": 9, "stock_maximo": None, "proveedor_id": party})
    assert r.status_code == 500
    with abrir() as conn:
        p = reposicion.parametros_de(conn, yerba)
        assert p["plazo_entrega_dias"] is None and p["proveedor_id"] is None      # nada quedó escrito
