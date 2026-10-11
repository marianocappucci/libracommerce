"""«Recibir mercadería» idempotente (ADR-046, revisión 0006): `POST /api/purchase-receipts/recibir`, contra los dos motores.

Un reintento (doble clic, o la respuesta que se perdió y la persona vuelve a apretar) con la misma `clave_operacion` no vuelve a mover stock ni crea otra recepción. Las rutas de
siempre (crear, cargar líneas, confirmar) no llevan clave y siguen andando igual. Las fixtures son las de `tests/test_vencimientos.py` (una base por motor, con la cadena al día) y los
helpers son los de `tests/test_web_compras.py`.
"""
from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import psycopg
import pytest
import test_update_producto_atomico as _tx
import test_vencimientos as _vto
from alembic import command
from fastapi.testclient import TestClient
from libracore.db import core
from test_vencimientos import _columnas_de, _con_conexion, _crear_base_al_dia_de_0001, _indices_de, _ledger, migrar
from test_web_compras import _app, _deposito, _producto, _proveedor

from libracommerce.db.repository import SqliteCommerceRepository
from libracommerce.erp import compras, stock, vencimientos

destino = _vto.destino
abrir_vto = _vto.abrir_vto
con_auditoria = _tx.con_auditoria

_ANTERIOR = "0005_min_stock_por_sucursal"
#: Lo que levanta cada motor al violar un índice único.
_ERRORES_DE_INTEGRIDAD = (sqlite3.IntegrityError, psycopg.errors.IntegrityError)


def _k() -> str:
    """Una `clave_operacion` nueva (un UUID, como la genera el kit por cada intento)."""
    return str(uuid.uuid4())


def _cuerpo(proveedor, deposito, producto, *, clave=None, cantidad=8, costo=250, **extra) -> dict:
    return {
        "proveedor_id": proveedor, "deposito_id": deposito, "clave_operacion": clave or _k(),
        "items": [{"item_id": producto, "quantity": cantidad, "unit_cost": costo}], **extra,
    }


def _recibir(client: TestClient, cuerpo: dict):
    return client.post("/api/purchase-receipts/recibir", json=cuerpo)


def _stock_de(abrir, producto) -> float:
    with abrir() as conn:
        return sum(stock.get_stock_por_deposito(conn).get(producto, {}).values())


def _contar(abrir, tabla: str) -> int:
    with abrir() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]


_COLUMNAS_RECEPCION = "id, purchase_order_id, supplier_party_id, status, received_at, document_reference, created_by, created_at"


def _recepciones(conn) -> list[tuple]:
    """Las recepciones sin la columna de la 0006, en orden (por posición: las filas de PostgreSQL no son tuplas)."""
    n = len(_COLUMNAS_RECEPCION.split(","))
    return [tuple(f[i] for i in range(n)) for f in conn.execute(f"SELECT {_COLUMNAS_RECEPCION} FROM purchase_receipts ORDER BY id").fetchall()]


def _escenario(abrir):
    """`(proveedor, producto, deposito, client)`."""
    return _proveedor(abrir), _producto(abrir), _deposito(abrir), _app(abrir)


# ── El corazón: el mismo intento dos veces cuenta una sola vez ───────────


def test_el_mismo_intento_dos_veces_cuenta_una_sola_recepcion_y_el_stock_una_vez(abrir_vto):
    proveedor, producto, deposito, client = _escenario(abrir_vto)
    cuerpo = _cuerpo(proveedor, deposito, producto, document_reference="REM-1")

    primera = _recibir(client, cuerpo)
    assert primera.status_code == 200, primera.text
    assert primera.json()["repetida"] is False and primera.json()["status"] == "confirmed"
    assert primera.json()["proveedor_id"] == proveedor and "supplier_party_id" not in primera.json()

    segunda = _recibir(client, cuerpo)   # la respuesta se perdió y la persona reintenta
    assert segunda.status_code == 200, segunda.text
    assert segunda.json()["repetida"] is True
    assert segunda.json()["id"] == primera.json()["id"] and segunda.json()["received_at"] == primera.json()["received_at"]
    assert segunda.json()["items"] == primera.json()["items"]

    assert _contar(abrir_vto, "purchase_receipts") == 1
    assert _contar(abrir_vto, "purchase_receipt_items") == 1
    assert _contar(abrir_vto, "stock_movements") == 1
    assert _stock_de(abrir_vto, producto) == 8.0


def test_el_reintento_no_vuelve_a_sumar_a_la_orden_ni_a_tocar_el_costo(abrir_vto):
    proveedor, producto, deposito, client = _escenario(abrir_vto)
    orden = client.post("/api/purchase-orders", json={"proveedor_id": proveedor}).json()
    client.post(f"/api/purchase-orders/{orden['id']}/items", json={"item_id": producto, "quantity_ordered": 20, "unit_cost": 100})
    cuerpo = _cuerpo(proveedor, deposito, producto, cantidad=8, costo=250, purchase_order_id=orden["id"])

    assert _recibir(client, cuerpo).status_code == 200
    # Entre un intento y otro el costo cambia a mano: el reintento no lo vuelve a pisar con el de la recepción.
    with abrir_vto() as conn:
        conn.execute("UPDATE catalog_items SET default_cost = 321 WHERE id = ?", (producto,))
        conn.commit()
    assert _recibir(client, cuerpo).json()["repetida"] is True

    actual = client.get(f"/api/purchase-orders/{orden['id']}").json()
    assert Decimal(actual["items"][0]["quantity_received"]) == 8 and actual["status"] == "partial"
    with abrir_vto() as conn:
        assert float(conn.execute("SELECT default_cost FROM catalog_items WHERE id = ?", (producto,)).fetchone()[0]) == 321.0


def test_con_lote_y_vencimiento_el_reintento_no_duplica_el_lote(abrir_vto):
    proveedor, producto, deposito, client = _escenario(abrir_vto)
    with abrir_vto() as conn:
        vencimientos.marcar_vence(conn, producto, True)
    cuerpo = _cuerpo(proveedor, deposito, producto, cantidad=12)
    cuerpo["items"][0].update(lot_code="L-77", expires_at="2026-12-31")

    assert _recibir(client, cuerpo).json()["repetida"] is False
    assert _recibir(client, cuerpo).json()["repetida"] is True

    with abrir_vto() as conn:
        lotes = [(f["lote"], f["saldo"]) for f in vencimientos.lotes_de(conn, producto, hoy=_vto.HOY)]
    assert lotes == [("L-77", 12)]


def test_claves_distintas_son_dos_recepciones_aunque_lleven_lo_mismo(abrir_vto):
    """Dos recepciones legítimas iguales (dos camiones con lo mismo) NO se confunden con un reintento: lo que las distingue es la clave."""
    proveedor, producto, deposito, client = _escenario(abrir_vto)

    a = _recibir(client, _cuerpo(proveedor, deposito, producto, clave="intento-a"))
    b = _recibir(client, _cuerpo(proveedor, deposito, producto, clave="intento-b"))
    assert a.status_code == b.status_code == 200
    assert a.json()["repetida"] is False and b.json()["repetida"] is False
    assert a.json()["id"] != b.json()["id"]

    assert _contar(abrir_vto, "purchase_receipts") == 2
    assert _contar(abrir_vto, "stock_movements") == 2
    assert _stock_de(abrir_vto, producto) == 16.0


def test_la_clave_se_recorta_y_se_guarda(abrir_vto):
    proveedor, producto, deposito, client = _escenario(abrir_vto)
    assert _recibir(client, _cuerpo(proveedor, deposito, producto, clave="  abc-1  ")).status_code == 200
    assert _recibir(client, _cuerpo(proveedor, deposito, producto, clave="abc-1")).json()["repetida"] is True
    with abrir_vto() as conn:
        assert [f[0] for f in conn.execute("SELECT clave_operacion FROM purchase_receipts").fetchall()] == ["abc-1"]


def test_cantidades_y_costos_equivalentes_son_el_mismo_pedido(abrir_vto):
    """`8` y `8.0` son lo mismo: lo que vuelve de la base (`NUMERIC`) no tiene por qué escribirse como lo mandó el cliente."""
    proveedor, producto, deposito, client = _escenario(abrir_vto)
    clave = _k()
    assert _recibir(client, _cuerpo(proveedor, deposito, producto, clave=clave, cantidad=8, costo=250)).status_code == 200
    r = _recibir(client, _cuerpo(proveedor, deposito, producto, clave=clave, cantidad="8.00", costo="250.0"))
    assert r.status_code == 200 and r.json()["repetida"] is True
    assert _stock_de(abrir_vto, producto) == 8.0


# ── La misma clave con otros datos es otra recepción: 409 y no escribe ───


@pytest.mark.parametrize("cambio", ["cantidad", "costo", "remito", "deposito", "producto", "proveedor", "orden", "lote"])
def test_la_misma_clave_con_otros_datos_es_409_y_no_escribe_nada(abrir_vto, cambio):
    proveedor, producto, deposito, client = _escenario(abrir_vto)
    otro_producto = _producto(abrir_vto, "Azúcar 1kg")
    otro_deposito = _deposito(abrir_vto)
    otro_proveedor = _proveedor(abrir_vto, "Otro SA")
    orden = client.post("/api/purchase-orders", json={"proveedor_id": proveedor}).json()
    clave = _k()
    original = _cuerpo(proveedor, deposito, producto, clave=clave, document_reference="REM-1")
    assert _recibir(client, original).status_code == 200

    distinto = _cuerpo(proveedor, deposito, producto, clave=clave, document_reference="REM-1")
    if cambio == "cantidad":
        distinto["items"][0]["quantity"] = 9
    elif cambio == "costo":
        distinto["items"][0]["unit_cost"] = 251
    elif cambio == "remito":
        distinto["document_reference"] = "REM-2"
    elif cambio == "deposito":
        distinto["deposito_id"] = otro_deposito
    elif cambio == "producto":
        distinto["items"][0]["item_id"] = otro_producto
    elif cambio == "proveedor":
        distinto["proveedor_id"] = otro_proveedor
    elif cambio == "orden":
        distinto["purchase_order_id"] = orden["id"]
    elif cambio == "lote":
        distinto["items"][0]["lot_code"] = "L-X"

    r = _recibir(client, distinto)
    assert r.status_code == 409, r.text
    assert "clave_operacion" in r.json()["detail"]
    assert _contar(abrir_vto, "purchase_receipts") == 1 and _stock_de(abrir_vto, producto) == 8.0


def test_la_misma_clave_con_mas_lineas_es_409(abrir_vto):
    proveedor, producto, deposito, client = _escenario(abrir_vto)
    otro = _producto(abrir_vto, "Azúcar 1kg")
    clave = _k()
    assert _recibir(client, _cuerpo(proveedor, deposito, producto, clave=clave)).status_code == 200
    mas = _cuerpo(proveedor, deposito, producto, clave=clave)
    mas["items"].append({"item_id": otro, "quantity": 1, "unit_cost": 1})
    assert _recibir(client, mas).status_code == 409
    assert _contar(abrir_vto, "stock_movements") == 1


# ── Una sola transacción: o queda todo o no queda nada ───────────────────


def test_si_una_linea_es_invalida_no_queda_ni_la_recepcion_ni_el_stock(abrir_vto):
    proveedor, producto, deposito, client = _escenario(abrir_vto)
    cuerpo = _cuerpo(proveedor, deposito, producto)
    cuerpo["items"].append({"item_id": 99999, "quantity": 1, "unit_cost": 1})   # un producto que no existe

    r = _recibir(client, cuerpo)
    assert r.status_code == 422 and "99999" in r.json()["detail"]
    assert _contar(abrir_vto, "purchase_receipts") == 0 and _contar(abrir_vto, "stock_movements") == 0
    # y la clave no se gastó: corregido el pedido, con la misma clave entra
    cuerpo["items"].pop()
    assert _recibir(client, cuerpo).json()["repetida"] is False
    assert _stock_de(abrir_vto, producto) == 8.0


def test_si_falla_el_ingreso_a_mitad_no_queda_una_recepcion_a_medias(abrir_vto, monkeypatch):
    """Antes (tres llamadas con tres commits) un fallo entre la recepción confirmada y su stock dejaba la recepción confirmada sin mercadería. Acá se revierte todo."""
    proveedor, producto, deposito, client = _escenario(abrir_vto)
    segundo = _producto(abrir_vto, "Azúcar 1kg")
    cuerpo = _cuerpo(proveedor, deposito, producto)
    cuerpo["items"].append({"item_id": segundo, "quantity": 2, "unit_cost": 10})

    original = SqliteCommerceRepository.append_stock_movement
    llamadas = []

    def fallar_en_la_segunda(self, movimiento):
        llamadas.append(movimiento.item_id)
        if len(llamadas) == 2:
            raise RuntimeError("se cortó")
        return original(self, movimiento)

    monkeypatch.setattr(SqliteCommerceRepository, "append_stock_movement", fallar_en_la_segunda)
    with pytest.raises(RuntimeError, match="se cortó"):
        _recibir(client, cuerpo)
    monkeypatch.undo()

    assert _contar(abrir_vto, "purchase_receipts") == 0 and _contar(abrir_vto, "purchase_receipt_items") == 0
    assert _contar(abrir_vto, "stock_movements") == 0
    assert _recibir(client, cuerpo).json()["repetida"] is False   # y el reintento entra completo
    assert _stock_de(abrir_vto, producto) == 8.0 and _stock_de(abrir_vto, segundo) == 2.0


@pytest.mark.parametrize("cambio,status,texto", [
    ({"items": []}, 422, None),
    ({"clave_operacion": ""}, 422, None),
    ({"clave_operacion": "a" * 65}, 422, None),
    ({"clave_operacion": "con[corchetes]"}, 422, "corchetes"),
    ({"deposito_id": 99999}, 422, "depósito"),
    ({"purchase_order_id": 99999}, 404, None),
    ({"proveedor_id": True}, 422, None),
])
def test_los_pedidos_invalidos_no_escriben_nada(abrir_vto, cambio, status, texto):
    proveedor, producto, deposito, client = _escenario(abrir_vto)
    r = _recibir(client, {**_cuerpo(proveedor, deposito, producto), **cambio})
    assert r.status_code == status, r.text
    if texto:
        assert texto in str(r.json()["detail"])
    assert _contar(abrir_vto, "purchase_receipts") == 0 and _contar(abrir_vto, "stock_movements") == 0


@pytest.mark.parametrize("cantidad,costo", [(0, 10), (-3, 10), (5, -1)])
def test_cantidad_no_positiva_o_costo_negativo_es_422(abrir_vto, cantidad, costo):
    proveedor, producto, deposito, client = _escenario(abrir_vto)
    r = _recibir(client, _cuerpo(proveedor, deposito, producto, cantidad=cantidad, costo=costo))
    assert r.status_code == 422, r.text
    assert _contar(abrir_vto, "purchase_receipts") == 0 and _stock_de(abrir_vto, producto) == 0.0


def test_sin_clave_no_hay_recibir_a_secas(abrir_vto):
    """La ruta nueva exige la clave: es su razón de ser. Quien no la manda sigue con las rutas de siempre."""
    proveedor, producto, deposito, client = _escenario(abrir_vto)
    cuerpo = _cuerpo(proveedor, deposito, producto)
    del cuerpo["clave_operacion"]
    assert _recibir(client, cuerpo).status_code == 422
    assert _contar(abrir_vto, "purchase_receipts") == 0


# ── Quien no manda clave (las rutas de siempre) no cambia en nada ────────


def _recibir_a_la_antigua(client, proveedor, deposito, producto, *, cantidad=8, costo=250):
    recepcion = client.post("/api/purchase-receipts", json={"proveedor_id": proveedor, "document_reference": "REM-1"}).json()
    client.post(f"/api/purchase-receipts/{recepcion['id']}/items", json={"item_id": producto, "quantity": cantidad, "unit_cost": costo})
    return client.post(f"/api/purchase-receipts/{recepcion['id']}/confirm", json={"deposito_id": deposito})


def test_sin_clave_las_tres_rutas_de_siempre_andan_igual_y_no_deduplican(abrir_vto):
    proveedor, producto, deposito, client = _escenario(abrir_vto)

    primera = _recibir_a_la_antigua(client, proveedor, deposito, producto)
    segunda = _recibir_a_la_antigua(client, proveedor, deposito, producto)
    assert primera.status_code == segunda.status_code == 200
    assert "repetida" not in primera.json()                      # la respuesta es la de siempre
    assert primera.json()["id"] != segunda.json()["id"]
    assert _contar(abrir_vto, "purchase_receipts") == 2 and _stock_de(abrir_vto, producto) == 16.0   # sin clave, como hoy: cada una cuenta
    with abrir_vto() as conn:
        assert conn.execute("SELECT COUNT(*) FROM purchase_receipts WHERE clave_operacion IS NOT NULL").fetchone()[0] == 0


def test_las_recepciones_sin_clave_no_chocan_entre_si_en_el_indice_unico(abrir_vto):
    proveedor, producto, deposito, client = _escenario(abrir_vto)
    for _ in range(3):
        assert _recibir_a_la_antigua(client, proveedor, deposito, producto).status_code == 200
    assert _contar(abrir_vto, "purchase_receipts") == 3


def test_una_recepcion_a_la_antigua_y_una_con_clave_conviven(abrir_vto):
    proveedor, producto, deposito, client = _escenario(abrir_vto)
    assert _recibir_a_la_antigua(client, proveedor, deposito, producto).status_code == 200
    assert _recibir(client, _cuerpo(proveedor, deposito, producto)).json()["repetida"] is False
    assert _stock_de(abrir_vto, producto) == 16.0
    assert len(client.get("/api/purchase-receipts").json()) == 2


def test_la_ruta_nueva_no_tapa_a_las_de_siempre(abrir_vto):
    proveedor, producto, deposito, client = _escenario(abrir_vto)
    recepcion = _recibir(client, _cuerpo(proveedor, deposito, producto)).json()
    assert client.get(f"/api/purchase-receipts/{recepcion['id']}").json()["id"] == recepcion["id"]
    assert client.get("/api/purchase-receipts/recibir").status_code in (405, 422)   # GET sobre la ruta nueva no es la lectura de una recepción


# ── Dos pedidos a la vez con la misma clave ──────────────────────────────


def test_dos_pedidos_simultaneos_con_la_misma_clave_cuentan_una_sola_vez(abrir_vto):
    """Dos hilos con la misma clave: el segundo espera al primero (candado de la clave) y, al ver su recepción, contesta `repetida`."""
    proveedor, producto, deposito, _ = _escenario(abrir_vto)
    clave = _k()
    resultados, errores = [], []
    barrera = threading.Barrier(2)

    def pedir():
        try:
            barrera.wait(timeout=10)
            conn = core.get_connection()
            try:
                resultados.append(compras.recibir_mercaderia(
                    conn, supplier_party_id=proveedor, deposito_id=deposito, clave_operacion=clave,
                    items=[{"item_id": producto, "quantity": 8, "unit_cost": 250}],
                    occurred_at=datetime.now(UTC)))
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - se informa abajo
            errores.append(exc)

    hilos = [threading.Thread(target=pedir) for _ in range(2)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=60)
    assert not any(h.is_alive() for h in hilos), "un pedido quedó colgado"
    assert not errores, errores
    assert sorted(r["repetida"] for r in resultados) == [False, True]
    assert resultados[0]["id"] == resultados[1]["id"]
    assert _contar(abrir_vto, "purchase_receipts") == 1 and _stock_de(abrir_vto, producto) == 8.0


# ── Con el repositorio que AUDITA (el de VentaLibra) ─────────────────────


def _filas_del_log(abrir) -> list[tuple]:
    with abrir() as conn:
        return [tuple(f) for f in conn.execute("SELECT accion, entidad FROM actividad_log ORDER BY id").fetchall()]


def test_con_el_repositorio_auditado_queda_en_el_log_una_vez_y_el_reintento_no_agrega_filas(con_auditoria):
    abrir = con_auditoria
    proveedor, producto, deposito, client = _escenario(abrir)
    antes = _filas_del_log(abrir)
    cuerpo = _cuerpo(proveedor, deposito, producto)

    assert _recibir(client, cuerpo).json()["repetida"] is False
    despues = _filas_del_log(abrir)
    nuevas = despues[len(antes):]
    assert ("crear", "recepcion de compra") in nuevas and ("crear", "movimiento de stock") in nuevas

    assert _recibir(client, cuerpo).json()["repetida"] is True
    assert _filas_del_log(abrir) == despues          # un reintento no audita nada: no se escribió nada


def test_con_el_repositorio_auditado_un_fallo_a_mitad_no_deja_ni_las_filas_del_log(con_auditoria, monkeypatch):
    """Cada escritura auditada deja su fila de log; si la operación se revierte, las filas se van con ella."""
    abrir = con_auditoria
    proveedor, producto, deposito, client = _escenario(abrir)
    segundo = _producto(abrir, "Azúcar 1kg")
    antes = _filas_del_log(abrir)
    cuerpo = _cuerpo(proveedor, deposito, producto)
    cuerpo["items"].append({"item_id": segundo, "quantity": 2, "unit_cost": 10})

    original = SqliteCommerceRepository.append_stock_movement
    llamadas = []

    def fallar_en_la_segunda(self, movimiento):
        llamadas.append(movimiento.item_id)
        if len(llamadas) == 2:
            raise RuntimeError("se cortó")
        return original(self, movimiento)

    monkeypatch.setattr(SqliteCommerceRepository, "append_stock_movement", fallar_en_la_segunda)
    with pytest.raises(RuntimeError, match="se cortó"):
        _recibir(client, cuerpo)
    monkeypatch.undo()

    assert llamadas and _filas_del_log(abrir) == antes
    assert _contar(abrir, "purchase_receipts") == 0 and _contar(abrir, "stock_movements") == 0


# ── Una base sin la revisión ─────────────────────────────────────────────


def test_sin_la_revision_la_ruta_nueva_dice_que_falta_y_las_de_siempre_andan(destino):
    _crear_base_al_dia_de_0001(destino)
    core.configure(destino)
    try:
        abrir = core.get_connection
        proveedor, producto, deposito, client = _escenario(abrir)
        r = _recibir(client, _cuerpo(proveedor, deposito, producto))
        assert r.status_code == 503 and "libracommerce-migrar upgrade" in r.json()["detail"]
        assert _contar(abrir, "purchase_receipts") == 0
        assert _recibir_a_la_antigua(client, proveedor, deposito, producto).status_code == 200   # el resto del módulo no la necesita
        with abrir() as conn:
            assert not compras.tiene_clave_operacion(conn)
    finally:
        _vto._liberar()


# ── La revisión 0006 ─────────────────────────────────────────────────────


def test_la_revision_0006_corre_sobre_datos_previos_sin_tocar_una_fila_y_es_idempotente(destino):
    _crear_base_al_dia_de_0001(destino)
    migrar.upgrade(destino, _ANTERIOR)

    def poblar(conn):
        proveedor, producto, deposito, client = _escenario(core.get_connection)
        assert _recibir_a_la_antigua(client, proveedor, deposito, producto).status_code == 200
        assert "clave_operacion" not in _columnas_de(conn, "purchase_receipts")
        return _recepciones(conn), _ledger(conn)

    antes = _con_conexion(destino, poblar)
    migrar.upgrade(destino)

    def verificar(conn):
        assert "clave_operacion" in _columnas_de(conn, "purchase_receipts")
        assert "idx_purchase_receipts_clave_operacion" in _indices_de(conn, "purchase_receipts")
        assert _recepciones(conn) == antes[0]                              # las mismas filas
        assert [f[0] for f in conn.execute("SELECT clave_operacion FROM purchase_receipts").fetchall()] == [None]   # todas sin clave
        assert _ledger(conn) == antes[1]
        return [f[0] for f in conn.execute("SELECT version_num FROM alembic_version_libracommerce").fetchall()]

    assert _con_conexion(destino, verificar) == [_vto._CABEZA]
    migrar.upgrade(destino)
    assert _con_conexion(destino, verificar) == [_vto._CABEZA]


def test_la_revision_0006_es_idempotente_por_introspeccion_aunque_la_columna_ya_exista(destino):
    """Una base con la columna puesta a mano (o con la versión retrocedida con `stamp`) vuelve a subir sin fallar y sin perder las claves."""
    _crear_base_al_dia_de_0001(destino)
    migrar.upgrade(destino)

    def cargar(conn):
        proveedor, producto, deposito, client = _escenario(core.get_connection)
        assert _recibir(client, _cuerpo(proveedor, deposito, producto, clave="guardada")).status_code == 200

    _con_conexion(destino, cargar)
    migrar.stamp(destino, _ANTERIOR)
    migrar.upgrade(destino)
    assert _con_conexion(destino, lambda c: [f[0] for f in c.execute("SELECT clave_operacion FROM purchase_receipts").fetchall()]) == ["guardada"]
    assert _con_conexion(destino, lambda c: [f[0] for f in c.execute(
        "SELECT version_num FROM alembic_version_libracommerce").fetchall()]) == [_vto._CABEZA]


def test_la_revision_0006_baja_la_columna_y_el_indice_sin_tocar_recepciones_ni_ledger(destino):
    _crear_base_al_dia_de_0001(destino)
    migrar.upgrade(destino)

    def poblar(conn):
        proveedor, producto, deposito, client = _escenario(core.get_connection)
        assert _recibir(client, _cuerpo(proveedor, deposito, producto)).status_code == 200
        return _ledger(conn), conn.execute("SELECT COUNT(*) FROM purchase_receipts").fetchone()[0]

    ledger, recepciones = _con_conexion(destino, poblar)
    command.downgrade(migrar.configuracion(destino), _ANTERIOR)

    def verificar(conn):
        assert "clave_operacion" not in _columnas_de(conn, "purchase_receipts")
        assert "idx_purchase_receipts_clave_operacion" not in _indices_de(conn, "purchase_receipts")
        assert _ledger(conn) == ledger, "bajar la revisión modificó el ledger"
        assert conn.execute("SELECT COUNT(*) FROM purchase_receipts").fetchone()[0] == recepciones
        assert [f[0] for f in conn.execute("SELECT version_num FROM alembic_version_libracommerce").fetchall()] == [_ANTERIOR]

    _con_conexion(destino, verificar)
    command.downgrade(migrar.configuracion(destino), _ANTERIOR)   # bajar de nuevo no falla: no hay nada que bajar
    migrar.upgrade(destino)                                       # y vuelve a subir


def test_el_indice_unico_rechaza_dos_recepciones_con_la_misma_clave_aunque_el_candado_fallara(abrir_vto):
    """La red de seguridad detrás del candado: la base misma no deja que dos recepciones compartan clave. Las que no tienen clave (NULL) sí pueden ser muchas."""
    proveedor = _proveedor(abrir_vto)
    with abrir_vto() as conn:
        for _ in range(2):
            conn.execute("INSERT INTO purchase_receipts (supplier_party_id) VALUES (?)", (proveedor,))   # NULL y NULL: bien
        conn.execute("INSERT INTO purchase_receipts (supplier_party_id, clave_operacion) VALUES (?, ?)", (proveedor, "x"))
        conn.commit()
        with pytest.raises(_ERRORES_DE_INTEGRIDAD):
            conn.execute("INSERT INTO purchase_receipts (supplier_party_id, clave_operacion) VALUES (?, ?)", (proveedor, "x"))
        conn.rollback()


def test_init_schema_sigue_congelado_y_la_columna_es_solo_de_la_revision(destino):
    _crear_base_al_dia_de_0001(destino)
    sin = _con_conexion(destino, lambda c: _columnas_de(c, "purchase_receipts"))
    migrar.upgrade(destino)
    con = _con_conexion(destino, lambda c: _columnas_de(c, "purchase_receipts"))
    assert "clave_operacion" not in sin
    assert con - sin == {"clave_operacion"}
