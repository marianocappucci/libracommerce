"""Stock mínimo por sucursal (ADR-024, revisión 0005): la tabla, `fijar_minimo_sucursal`, la reposición y el router, contra los dos motores.
Las fixtures y los helpers son los de `tests/test_reposicion.py` y `tests/test_vencimientos.py` (una base por motor).

El escenario de casi todas las pruebas: una yerba con mínimo global 12 que tiene 10 en Centro y 4 en Norte (sin ventas: lo único que empuja es el mínimo).
Toda la instancia suma 14 y no falta nada; Centro sin mínimo propio pide 2 (12 − 10) y Norte pide 8 (12 − 4)."""
from __future__ import annotations

import sqlite3

import psycopg
import pytest
import test_reposicion as _rep
import test_vencimientos as _vto
from alembic import command
from conftest import USUARIO
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_reposicion import PREVIA, _dos_sucursales, _por_nombre, _producto, _reporte
from test_vencimientos import _columnas_de, _con_conexion, _crear_base_al_dia_de_0001, _ledger, catalogo, migrar, stock

from libracommerce.erp import reposicion
from libracommerce.web.reposicion_router import (
    build_reposicion_minimos_router,
    build_reposicion_parametros_router,
    build_reposicion_router,
)

# Las fixtures de las dos suites hermanas (una base por motor).
destino = _vto.destino
abrir_vto_ventas = _rep.abrir_vto_ventas

_ANTERIOR = "0004_proveedor_por_producto"
#: Lo que levanta cada motor al violar una clave, un `NOT NULL` o un `CHECK`.
_ERRORES_DE_INTEGRIDAD = (sqlite3.IntegrityError, psycopg.errors.IntegrityError)


def _escenario(abrir, *, minimo=12.0):
    """`(yerba, centro, norte)`: mínimo global `minimo`, 10 en el depósito de Centro y 4 en el de Norte."""
    centro, norte, dep_centro, dep_norte = _dos_sucursales(abrir)
    with abrir() as conn:
        yerba = _producto(conn, "Yerba", minimo=minimo)
        stock.ajustar_stock(conn, yerba, 10.0, "inicial", usuario_id=USUARIO["id"], fecha=PREVIA, deposito_id=dep_centro)
        stock.ajustar_stock(conn, yerba, 4.0, "inicial", usuario_id=USUARIO["id"], fecha=PREVIA, deposito_id=dep_norte)
    return yerba, centro, norte


def _fijar(abrir, pid, sucursal, valor):
    with abrir() as conn:
        r = reposicion.fijar_minimo_sucursal(conn, pid, sucursal, valor)
        conn.commit()
    return r


def _filas_propias(abrir, pid) -> list[tuple]:
    with abrir() as conn:
        return [tuple(f) for f in conn.execute(
            "SELECT branch_id, min_stock FROM item_branch_min_stock WHERE item_id = ? ORDER BY branch_id", (pid,)).fetchall()]


# ── La revisión 0005 ─────────────────────────────────────────────────────


def test_la_revision_0005_corre_sobre_datos_previos_sin_tocar_una_fila_y_es_idempotente(destino):
    _crear_base_al_dia_de_0001(destino)
    migrar.upgrade(destino, _ANTERIOR)

    def poblar(conn):
        yerba = catalogo.create_producto(conn, "Yerba", precio_venta=100.0, precio_costo=60.0, stock_minimo=7.0)
        stock.ajustar_stock(conn, yerba, 40.0, "inicial", fecha=PREVIA)
        conn.commit()
        assert not conn.execute("PRAGMA table_info(item_branch_min_stock)").fetchall()
        return _ledger(conn)

    antes = _con_conexion(destino, poblar)
    migrar.upgrade(destino)

    def verificar(conn):
        assert _columnas_de(conn, "item_branch_min_stock") == {"item_id", "branch_id", "min_stock"}
        assert conn.execute("SELECT COUNT(*) FROM item_branch_min_stock").fetchone()[0] == 0     # nace vacía
        assert float(conn.execute("SELECT min_stock FROM catalog_items").fetchone()[0]) == 7.0   # el global, intacto
        assert _ledger(conn) == antes
        return [f[0] for f in conn.execute("SELECT version_num FROM alembic_version_libracommerce").fetchall()]

    assert _con_conexion(destino, verificar) == [_vto._CABEZA]
    migrar.upgrade(destino)
    assert _con_conexion(destino, verificar) == [_vto._CABEZA]


def test_la_revision_0005_es_idempotente_por_introspeccion_aunque_la_tabla_ya_exista(destino):
    """Una base con la tabla puesta a mano (o con la versión retrocedida con `stamp`) vuelve a subir sin fallar y sin perder las filas."""
    _crear_base_al_dia_de_0001(destino)
    migrar.upgrade(destino)

    def cargar(conn):
        catalogo.create_producto(conn, "Yerba", precio_venta=1.0, precio_costo=1.0)
        sucursal = catalogo.create_sucursal(conn, "Centro")
        conn.execute("INSERT INTO item_branch_min_stock (item_id, branch_id, min_stock) VALUES (1, ?, 5)", (sucursal,))
        conn.commit()

    _con_conexion(destino, cargar)
    migrar.stamp(destino, _ANTERIOR)
    migrar.upgrade(destino)
    assert _con_conexion(destino, lambda c: c.execute("SELECT COUNT(*) FROM item_branch_min_stock").fetchone()[0]) == 1
    assert _con_conexion(destino, lambda c: [f[0] for f in c.execute(
        "SELECT version_num FROM alembic_version_libracommerce").fetchall()]) == [_vto._CABEZA]


def test_la_revision_0005_baja_la_tabla_sin_tocar_el_ledger_ni_el_minimo_global(destino):
    _crear_base_al_dia_de_0001(destino)
    migrar.upgrade(destino)

    def poblar(conn):
        yerba = catalogo.create_producto(conn, "Yerba", precio_venta=100.0, precio_costo=60.0, stock_minimo=7.0)
        stock.ajustar_stock(conn, yerba, 40.0, "inicial", fecha=PREVIA)
        sucursal = catalogo.create_sucursal(conn, "Centro")
        reposicion.fijar_minimo_sucursal(conn, yerba, sucursal, 3)
        conn.commit()
        return _ledger(conn)

    antes = _con_conexion(destino, poblar)
    command.downgrade(migrar.configuracion(destino), _ANTERIOR)

    def verificar(conn):
        assert not conn.execute("PRAGMA table_info(item_branch_min_stock)").fetchall()
        assert float(conn.execute("SELECT min_stock FROM catalog_items").fetchone()[0]) == 7.0
        assert _ledger(conn) == antes, "bajar la revisión modificó el ledger"
        assert [f[0] for f in conn.execute("SELECT version_num FROM alembic_version_libracommerce").fetchall()] == [_ANTERIOR]

    _con_conexion(destino, verificar)
    command.downgrade(migrar.configuracion(destino), _ANTERIOR)   # bajar de nuevo no falla: no hay nada que bajar
    migrar.upgrade(destino)                                       # y vuelve a subir


def test_init_schema_sigue_congelado_y_la_tabla_es_solo_de_la_revision(destino):
    _crear_base_al_dia_de_0001(destino)
    assert not _con_conexion(destino, lambda c: c.execute("PRAGMA table_info(item_branch_min_stock)").fetchall())
    migrar.upgrade(destino)
    assert _con_conexion(destino, lambda c: c.execute("PRAGMA table_info(item_branch_min_stock)").fetchall())


def test_la_tabla_exige_el_producto_la_sucursal_y_un_minimo_no_negativo(destino):
    _crear_base_al_dia_de_0001(destino)
    migrar.upgrade(destino)

    def probar(conn):
        yerba = catalogo.create_producto(conn, "Yerba", precio_venta=1.0, precio_costo=1.0)
        sucursal = catalogo.create_sucursal(conn, "Centro")
        conn.commit()
        insertar = "INSERT INTO item_branch_min_stock (item_id, branch_id, min_stock) VALUES (?, ?, ?)"
        for args in ((yerba, sucursal, -1), (yerba, sucursal, None), (9999, sucursal, 1), (yerba, 9999, 1)):
            with pytest.raises(_ERRORES_DE_INTEGRIDAD):
                conn.execute(insertar, args)
            conn.rollback()
        conn.execute(insertar, (yerba, sucursal, 0))                # 0 es válido
        with pytest.raises(_ERRORES_DE_INTEGRIDAD):                # la clave primaria: un solo mínimo por (producto, sucursal)
            conn.execute(insertar, (yerba, sucursal, 5))
        conn.rollback()

    _con_conexion(destino, probar)


# ── La reposición ────────────────────────────────────────────────────────


def test_el_minimo_propio_de_la_sucursal_gana_sobre_el_global(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba, centro, norte = _escenario(abrir)
    _fijar(abrir, yerba, centro, 15)                          # Centro tiene 10: con 15 propios faltan 5
    [fila] = _reporte(abrir, sucursal_id=centro)
    assert (fila["producto_id"], fila["stock"], fila["stock_minimo"], fila["stock_minimo_propio"]) == (yerba, 10.0, 15.0, True)
    assert (fila["sugerido"], fila["motivo"]) == (5, "bajo_minimo")


def test_un_minimo_propio_menor_que_el_global_deja_de_avisar_donde_alcanza(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba, centro, norte = _escenario(abrir)
    assert [f["nombre"] for f in _reporte(abrir, sucursal_id=centro)] == ["Yerba"]       # con el global (12 > 10) pide
    _fijar(abrir, yerba, centro, 8)                                                    # con 8 propios, 10 alcanza
    assert _reporte(abrir, sucursal_id=centro) == []
    fila = _reporte(abrir, sucursal_id=centro, solo_a_pedir=False)[0]
    assert (fila["stock_minimo"], fila["stock_minimo_propio"], fila["sugerido"], fila["motivo"]) == (8.0, True, 0, None)


def test_sin_fila_la_sucursal_cae_al_minimo_global_y_cada_una_mira_el_suyo(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba, centro, norte = _escenario(abrir)
    _fijar(abrir, yerba, centro, 8)
    [fila] = _reporte(abrir, sucursal_id=norte)               # Norte no tiene fila: el global (12), tiene 4
    assert (fila["stock_minimo"], fila["stock_minimo_propio"], fila["sugerido"]) == (12.0, False, 8)
    assert _filas_propias(abrir, yerba) == [(centro, 8)]


def test_sin_sucursal_se_usa_siempre_el_global(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba, centro, norte = _escenario(abrir)
    _fijar(abrir, yerba, centro, 100)
    _fijar(abrir, yerba, norte, 0)
    fila = _reporte(abrir, solo_a_pedir=False)[0]            # toda la instancia: 14 de stock, el global es 12
    assert (fila["stock"], fila["stock_minimo"], fila["stock_minimo_propio"], fila["sugerido"]) == (14.0, 12.0, False, 0)


def test_un_minimo_propio_de_cero_significa_no_me_avises_y_no_es_lo_mismo_que_borrarlo(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba, centro, norte = _escenario(abrir)
    _fijar(abrir, yerba, norte, 0)                            # el global vigila (12 > 4) pero Norte pidió que no le avisen
    assert _reporte(abrir, sucursal_id=norte) == []
    fila = _reporte(abrir, sucursal_id=norte, solo_a_pedir=False)[0]
    assert (fila["stock_minimo"], fila["stock_minimo_propio"], fila["motivo"]) == (0.0, True, None)
    assert _filas_propias(abrir, yerba) == [(norte, 0)]       # una fila con 0, no la ausencia de fila
    _fijar(abrir, yerba, norte, None)                         # borrarla sí vuelve al global
    [fila] = _reporte(abrir, sucursal_id=norte)
    assert (fila["stock_minimo"], fila["stock_minimo_propio"], fila["sugerido"]) == (12.0, False, 8)


def test_borrar_el_propio_vuelve_al_global_y_borrar_lo_que_no_hay_no_falla(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba, centro, _ = _escenario(abrir)
    _fijar(abrir, yerba, centro, 15)
    assert _filas_propias(abrir, yerba) == [(centro, 15)]
    _fijar(abrir, yerba, centro, None)
    assert _filas_propias(abrir, yerba) == []
    _fijar(abrir, yerba, centro, None)                        # ya no hay: igual no falla
    [fila] = _reporte(abrir, sucursal_id=centro)
    assert (fila["stock_minimo"], fila["stock_minimo_propio"]) == (12.0, False)


def test_fijar_dos_veces_reemplaza_el_valor_y_deja_una_sola_fila(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba, centro, _ = _escenario(abrir)
    _fijar(abrir, yerba, centro, 15)
    _fijar(abrir, yerba, centro, 3.5)
    assert _filas_propias(abrir, yerba) == [(centro, 3.5)]


def test_la_fila_trae_siempre_el_campo_nuevo_y_los_de_siempre(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba, centro, _ = _escenario(abrir)
    for sucursal in (None, centro):
        [fila] = _reporte(abrir, sucursal_id=sucursal, solo_a_pedir=False)
        assert fila["stock_minimo_propio"] is False and fila["stock_minimo"] == 12.0
        assert {"producto_id", "stock", "stock_minimo", "sugerido", "motivo", "factor_estacional"} <= set(fila)


def test_otros_productos_de_la_sucursal_no_se_ven_afectados(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba, centro, _ = _escenario(abrir)
    with abrir() as conn:
        sal = _producto(conn, "Sal", minimo=30.0)
        stock.ajustar_stock(conn, sal, 10.0, "inicial", fecha=PREVIA, deposito_id=catalogo.get_deposito_de_venta(conn, centro))
    _fijar(abrir, yerba, centro, 1)
    filas = _por_nombre(_reporte(abrir, sucursal_id=centro))
    assert list(filas) == ["Sal"] and filas["Sal"]["stock_minimo"] == 30.0 and filas["Sal"]["stock_minimo_propio"] is False


# ── minimos_por_sucursal_de ──────────────────────────────────────────────


def test_minimos_por_sucursal_de_lista_las_activas_con_el_efectivo_y_el_global(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba, centro, norte = _escenario(abrir)
    with abrir() as conn:
        baja = catalogo.create_sucursal(conn, "Cerrada")
        conn.execute("UPDATE branches SET active = 0 WHERE id = ?", (baja,))
        conn.commit()
    _fijar(abrir, yerba, centro, 15)
    with abrir() as conn:
        lista = reposicion.minimos_por_sucursal_de(conn, yerba)
    assert sorted(lista, key=lambda r: r["sucursal"]) == [
        {"sucursal_id": centro, "sucursal": "Centro", "stock_minimo": 15.0, "stock_minimo_propio": True, "stock_minimo_global": 12.0},
        {"sucursal_id": norte, "sucursal": "Norte", "stock_minimo": 12.0, "stock_minimo_propio": False, "stock_minimo_global": 12.0},
    ]
    with abrir() as conn, pytest.raises(reposicion.ProductoNoEncontrado):
        reposicion.minimos_por_sucursal_de(conn, 9999)


# ── Validaciones ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("malo", [-1, -0.001, float("nan"), float("inf"), "x", "", True, [3], {"a": 1}, 10**9 + 1, "1e400"])
def test_un_minimo_invalido_se_rechaza_y_no_escribe_nada(abrir_vto_ventas, malo):
    abrir = abrir_vto_ventas
    yerba, centro, _ = _escenario(abrir)
    with abrir() as conn, pytest.raises(ValueError):
        reposicion.fijar_minimo_sucursal(conn, yerba, centro, malo)
    assert _filas_propias(abrir, yerba) == []


@pytest.mark.parametrize("valor", [0, 0.5, 12, "7", 10**9])
def test_los_valores_validos_se_aceptan(abrir_vto_ventas, valor):
    abrir = abrir_vto_ventas
    yerba, centro, _ = _escenario(abrir)
    _fijar(abrir, yerba, centro, valor)
    [(sucursal, guardado)] = _filas_propias(abrir, yerba)
    assert sucursal == centro and float(guardado) == float(valor)


def test_una_sucursal_que_no_existe_es_un_error(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba, _, _ = _escenario(abrir)
    for sucursal in (9999, 0, -1):
        for valor in (5, None):                               # ni para fijar ni para borrar
            with abrir() as conn, pytest.raises(ValueError, match="no existe"):
                reposicion.fijar_minimo_sucursal(conn, yerba, sucursal, valor)
    for sucursal in (True, "1", 1.5, None):
        with abrir() as conn, pytest.raises(ValueError, match="entero"):
            reposicion.fijar_minimo_sucursal(conn, yerba, sucursal, 5)
    assert _filas_propias(abrir, yerba) == []


def test_un_producto_que_no_existe_es_un_error(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    _, centro, _ = _escenario(abrir)
    with abrir() as conn, pytest.raises(reposicion.ProductoNoEncontrado):
        reposicion.fijar_minimo_sucursal(conn, 9999, centro, 5)


def test_una_sucursal_dada_de_baja_no_admite_fijar_pero_si_borrar(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba, centro, norte = _escenario(abrir)
    _fijar(abrir, yerba, norte, 3)
    with abrir() as conn:
        conn.execute("UPDATE branches SET active = 0 WHERE id = ?", (norte,))
        conn.commit()
    with abrir() as conn, pytest.raises(ValueError, match="de baja"):
        reposicion.fijar_minimo_sucursal(conn, yerba, norte, 9)
    assert _filas_propias(abrir, yerba) == [(norte, 3)]
    _fijar(abrir, yerba, norte, None)                         # limpiar el propio de una sucursal cerrada sí se puede
    assert _filas_propias(abrir, yerba) == []


def test_el_minimo_por_sucursal_no_puede_pasar_el_techo_y_el_techo_no_puede_quedar_bajo_un_minimo_propio(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba, centro, norte = _escenario(abrir, minimo=5.0)
    with abrir() as conn:
        reposicion.fijar_parametros(conn, yerba, plazo_entrega_dias=None, stock_maximo=20)
        conn.commit()
    with abrir() as conn, pytest.raises(ValueError, match="mayor que el stock máximo"):
        reposicion.fijar_minimo_sucursal(conn, yerba, centro, 21)
    _fijar(abrir, yerba, centro, 20)                          # igual al techo: válido
    with abrir() as conn, pytest.raises(ValueError, match="mínimo por sucursal"):
        reposicion.fijar_parametros(conn, yerba, plazo_entrega_dias=None, stock_maximo=19)
    with abrir() as conn:                                      # sin techo, o con uno que alcanza, sigue andando
        reposicion.fijar_parametros(conn, yerba, plazo_entrega_dias=4, stock_maximo=20)
        reposicion.fijar_parametros(conn, yerba, plazo_entrega_dias=4, stock_maximo=None)
        conn.commit()
    _fijar(abrir, yerba, norte, 500)                          # sin techo, el mínimo por sucursal es libre (hasta el tope)


def test_el_techo_sigue_mandando_sobre_el_minimo_propio_si_los_datos_ya_estaban_cruzados(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba, centro, _ = _escenario(abrir, minimo=0.0)
    _fijar(abrir, yerba, centro, 50)
    with abrir() as conn:                                      # el techo escrito por fuera de la guarda (datos de antes)
        conn.execute("UPDATE catalog_items SET max_stock = 14 WHERE id = ?", (yerba,))
        conn.commit()
    [fila] = _reporte(abrir, sucursal_id=centro)
    assert fila["stock_minimo"] == 50.0 and fila["sugerido"] == 4          # 14 − 10: gana el techo (ADR-020)


def test_borrar_el_producto_se_lleva_sus_minimos_por_sucursal(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    _, centro, _ = _escenario(abrir)
    with abrir() as conn:
        otro = _producto(conn, "Sin movimientos")
    _fijar(abrir, otro, centro, 4)
    assert _filas_propias(abrir, otro) == [(centro, 4)]
    with abrir() as conn:
        catalogo.delete_producto(conn, otro)                   # sin la limpieza, la FK lo rechazaría
        conn.commit()
    with abrir() as conn:
        assert conn.execute("SELECT COUNT(*) FROM item_branch_min_stock").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM catalog_items WHERE id = ?", (otro,)).fetchone()[0] == 0


# ── Una base sin la 0005 ─────────────────────────────────────────────────


def test_una_base_sin_la_0005_se_comporta_como_siempre_y_pide_la_revision_para_escribir(destino):
    _crear_base_al_dia_de_0001(destino)
    migrar.upgrade(destino, _ANTERIOR)

    def intentar(conn):
        assert not reposicion.tiene_minimos_sucursal(conn)
        yerba = catalogo.create_producto(conn, "Yerba", precio_venta=100.0, precio_costo=60.0, stock_minimo=12.0)
        sucursal = catalogo.create_sucursal(conn, "Centro")
        stock.ajustar_stock(conn, yerba, 4.0, "inicial", fecha=PREVIA, deposito_id=catalogo.get_deposito_de_venta(conn, sucursal))
        [fila] = reposicion.sugerencia_reposicion(conn, sucursal_id=sucursal, hoy=_rep.HOY)
        assert (fila["stock_minimo"], fila["stock_minimo_propio"], fila["sugerido"]) == (12.0, False, 8)
        with pytest.raises(reposicion.SinRevision, match="0005"):
            reposicion.fijar_minimo_sucursal(conn, yerba, sucursal, 3)
        with pytest.raises(reposicion.SinRevision, match="0005"):
            reposicion.minimos_por_sucursal_de(conn, yerba)
        reposicion.fijar_parametros(conn, yerba, plazo_entrega_dias=3, stock_maximo=50)      # y el techo sigue andando sin la tabla

    _con_conexion(destino, intentar)


def test_borrar_un_producto_en_una_base_sin_la_0005_sigue_andando(destino):
    _crear_base_al_dia_de_0001(destino)
    migrar.upgrade(destino, _ANTERIOR)

    def borrar(conn):
        yerba = catalogo.create_producto(conn, "Yerba", precio_venta=100.0, precio_costo=60.0)
        catalogo.delete_producto(conn, yerba)
        assert conn.execute("SELECT COUNT(*) FROM catalog_items").fetchone()[0] == 0

    _con_conexion(destino, borrar)


# ── El router ────────────────────────────────────────────────────────────


def _cliente(abrir, *, escribir=None, leer=None) -> TestClient:
    app = FastAPI()
    app.include_router(build_reposicion_minimos_router(
        conexion=abrir, dependencias_escribir=escribir or [Depends(lambda: None)], dependencias_leer=leer))
    app.include_router(build_reposicion_parametros_router(conexion=abrir, dependencias_escribir=[Depends(lambda: None)]))
    app.include_router(build_reposicion_router(conexion=abrir))
    return TestClient(app)


def _prohibido():
    raise HTTPException(403, "sin permiso")


def test_el_router_lee_y_escribe_los_minimos_por_sucursal(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba, centro, norte = _escenario(abrir)
    c = _cliente(abrir)
    ruta = f"/api/productos/{yerba}/reposicion/minimos"
    r = c.get(ruta)
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["producto_id"] == yerba and {s["sucursal"]: s["stock_minimo"] for s in cuerpo["sucursales"]} == {"Centro": 12.0, "Norte": 12.0}
    assert all(s["stock_minimo_propio"] is False and s["stock_minimo_global"] == 12.0 for s in cuerpo["sucursales"])

    r = c.put(f"{ruta}/{centro}", json={"stock_minimo": 15})
    assert r.status_code == 200, r.text
    por_id = {s["sucursal_id"]: s for s in r.json()["sucursales"]}
    assert por_id[centro]["stock_minimo"] == 15.0 and por_id[centro]["stock_minimo_propio"] is True
    assert por_id[norte]["stock_minimo"] == 12.0 and por_id[norte]["stock_minimo_propio"] is False
    assert c.get(ruta).json() == r.json()

    # La reposición por sucursal lo usa; la de toda la instancia no.
    [fila] = c.get("/api/reportes/reposicion", params={"sucursal_id": centro}).json()["productos"]
    assert (fila["stock_minimo"], fila["stock_minimo_propio"], fila["sugerido"]) == (15.0, True, 5)
    [fila] = c.get("/api/reportes/reposicion", params={"solo_a_pedir": "false"}).json()["productos"]
    assert (fila["stock_minimo"], fila["stock_minimo_propio"]) == (12.0, False)

    r = c.put(f"{ruta}/{centro}", json={"stock_minimo": None})          # null: borra el propio
    assert r.status_code == 200 and {s["stock_minimo_propio"] for s in r.json()["sucursales"]} == {False}
    r = c.put(f"{ruta}/{norte}", json={"stock_minimo": 0})              # 0: no avisar
    assert {s["sucursal"]: (s["stock_minimo"], s["stock_minimo_propio"]) for s in r.json()["sucursales"]}["Norte"] == (0.0, True)


def test_el_router_responde_404_422_y_no_escribe_con_lo_invalido(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba, centro, _ = _escenario(abrir)
    c = _cliente(abrir)
    ruta = f"/api/productos/{yerba}/reposicion/minimos"
    assert c.get("/api/productos/9999/reposicion/minimos").status_code == 404
    assert c.put(f"/api/productos/9999/reposicion/minimos/{centro}", json={"stock_minimo": 1}).status_code == 404
    assert c.put(f"{ruta}/9999", json={"stock_minimo": 1}).status_code == 422                # la sucursal no existe
    assert c.put(f"{ruta}/9999", json={"stock_minimo": None}).status_code == 422
    for cuerpo in ({"stock_minimo": -1}, {"stock_minimo": "mucho"}, {"stock_minimo": 10**10}, {}, {"stock_minimo": 1, "x": 1}, {"stock_minimo": [1]}):
        assert c.put(f"{ruta}/{centro}", json=cuerpo).status_code == 422, cuerpo
    assert c.put(f"{ruta}/{centro}", content=b'{"stock_minimo": NaN}', headers={"content-type": "application/json"}).status_code == 422
    assert c.put(f"{ruta}/{centro}", content=b'{"stock_minimo": Infinity}', headers={"content-type": "application/json"}).status_code == 422
    assert c.put(f"{ruta}/abc", json={"stock_minimo": 1}).status_code == 422
    assert _filas_propias(abrir, yerba) == []                                                 # nada de lo inválido escribió


def test_el_router_respeta_los_permisos_de_lectura_y_de_escritura(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba, centro, _ = _escenario(abrir)
    ruta = f"/api/productos/{yerba}/reposicion/minimos"

    sin_escritura = _cliente(abrir, escribir=[Depends(_prohibido)])
    assert sin_escritura.put(f"{ruta}/{centro}", json={"stock_minimo": 5}).status_code == 403
    assert sin_escritura.get(ruta).status_code == 200                                         # leer es opcional
    assert _filas_propias(abrir, yerba) == []

    sin_lectura = _cliente(abrir, leer=[Depends(_prohibido)])
    assert sin_lectura.get(ruta).status_code == 403
    assert sin_lectura.put(f"{ruta}/{centro}", json={"stock_minimo": 5}).status_code == 200


def test_el_router_no_se_monta_sin_autorizacion_para_escribir(abrir_vto_ventas):
    for vacio in (None, [], ()):
        with pytest.raises(ValueError, match="dependencias_escribir"):
            build_reposicion_minimos_router(conexion=abrir_vto_ventas, dependencias_escribir=vacio)


def test_el_router_sin_la_revision_responde_503(abrir_ventas):
    c = _cliente(abrir_ventas)                                # `abrir_ventas`: ni la 0003 ni la 0005
    assert c.get("/api/productos/1/reposicion/minimos").status_code == 503
    assert c.put("/api/productos/1/reposicion/minimos/1", json={"stock_minimo": 1}).status_code == 503


def test_el_csv_lista_si_el_minimo_es_propio(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba, centro, norte = _escenario(abrir)
    c = _cliente(abrir)
    c.put(f"/api/productos/{yerba}/reposicion/minimos/{centro}", json={"stock_minimo": 15})
    for sucursal, esperado in ((centro, ("15.0", "si")), (norte, ("12.0", "no"))):
        lineas = c.get("/api/reportes/reposicion/export", params={"sucursal_id": sucursal}).text.splitlines()
        cabecera = lineas[0].split(",")
        assert cabecera[-1] == "stock_minimo_propio"
        fila = lineas[1].split(",")
        assert (fila[cabecera.index("stock_minimo")], fila[cabecera.index("stock_minimo_propio")]) == esperado


def test_el_put_rechaza_los_booleanos_en_vez_de_convertirlos_en_numero(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba, centro, _ = _escenario(abrir)
    c = _cliente(abrir)
    for crudo in (b'{"stock_minimo": true}', b'{"stock_minimo": false}'):
        r = c.put(f"/api/productos/{yerba}/reposicion/minimos/{centro}", content=crudo, headers={"content-type": "application/json"})
        assert r.status_code == 422, crudo
    assert _filas_propias(abrir, yerba) == []                                                 # ni 1.0 ni 0.0
    assert c.put(f"/api/productos/{yerba}/reposicion/minimos/{centro}", json={"stock_minimo": 0}).status_code == 200   # el 0 numérico sigue valiendo


# ── Concurrencia: el techo y el mínimo por sucursal se validan uno contra el otro ──


def _escribir_y_esperar(abrir, barrera, salida: dict, nombre: str, operacion) -> None:
    """Un hilo: abre su conexión, hace `operacion` (que valida y escribe), espera al otro antes de confirmar y anota cómo terminó."""
    import threading

    try:
        with abrir() as conn:
            try:
                operacion(conn)
            except ValueError:
                conn.rollback()
                salida[nombre] = "rechazada"
                return
            try:
                barrera.wait(timeout=2)
            except threading.BrokenBarrierError:
                pass
            conn.commit()
            salida[nombre] = "confirmada"
    except Exception as exc:  # noqa: BLE001 - se informa en el test
        salida[nombre] = f"error: {exc!r}"


def test_un_techo_y_un_minimo_por_sucursal_a_la_vez_nunca_quedan_cruzados(abrir_vto_ventas):
    """Sin serializar, cada escritura valida contra lo que la otra todavía no confirmó y las dos pasan (mínimo 80, techo 50). Cada hilo, ya validada y escrita su
    parte, espera al otro antes de confirmar: sin candado los dos llegan juntos a la barrera y confirman; con el candado del producto el segundo no puede ni validar
    hasta que el primero confirma (la barrera se rompe por el plazo, y el primero confirma solo), y después ve lo del primero y se rechaza."""
    import threading

    abrir = abrir_vto_ventas
    yerba, centro, _ = _escenario(abrir, minimo=5.0)

    def cruzado() -> bool:
        with abrir() as conn:
            techo = conn.execute("SELECT max_stock FROM catalog_items WHERE id = ?", (yerba,)).fetchone()[0]
            minimos = [float(f[0]) for f in conn.execute("SELECT min_stock FROM item_branch_min_stock WHERE item_id = ?", (yerba,)).fetchall()]
        return techo is not None and any(m > float(techo) for m in minimos)

    for ronda in range(4):
        with abrir() as conn:                                  # cada ronda parte limpia
            conn.execute("DELETE FROM item_branch_min_stock WHERE item_id = ?", (yerba,))
            conn.execute("UPDATE catalog_items SET max_stock = NULL WHERE id = ?", (yerba,))
            conn.commit()
        barrera = threading.Barrier(2)
        salida: dict[str, str] = {}

        hilos = [
            threading.Thread(target=_escribir_y_esperar, args=(abrir, barrera, salida, "minimo", lambda c: reposicion.fijar_minimo_sucursal(c, yerba, centro, 80))),
            threading.Thread(target=_escribir_y_esperar, args=(abrir, barrera, salida, "techo", lambda c: reposicion.fijar_parametros(c, yerba, plazo_entrega_dias=None, stock_maximo=50))),
        ]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join(timeout=60)
        assert not any(v.startswith("error") for v in salida.values()), salida
        assert not cruzado(), f"ronda {ronda}: quedó un mínimo por sucursal por encima del techo ({salida})"
        assert sorted(salida.values()) == ["confirmada", "rechazada"], salida                # exactamente una gana
