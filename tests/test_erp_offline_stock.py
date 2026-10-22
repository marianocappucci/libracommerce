"""El stock de la sucursal de un nodo offline: encolado, guarda del central, carga inicial y aplicadores (ADR-071).

Misma estructura que `test_erp_offline.py` y `test_erp_offline_caja.py`: DOS bases —el nodo y el central— y el `SyncReceiver` de LibraEdge de
verdad con el `Despachador` (`registrar_en`), en las tres combinaciones de motores (`puntas`). Las dos bases tienen los mismos depósitos (el nodo
los espeja): el 1 y el 2 son de la sucursal 1 (la del nodo `nodo-s02`), el 3 es de la sucursal 2 y el 4 no tiene sucursal. Como el nodo y el
central corren en el mismo proceso, el `Outbox` de estos tests cambia de modo (`como_nodo` / `como_central`): el gancho y la guarda se arman una
vez, como en un producto, y leen el modo en cada movimiento. Los ids del nodo y los del central se hacen **distintos a propósito** (el central ya
tiene movimientos propios en un depósito sin sucursal) para que una traducción que copie el id del nodo no pase por casualidad.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import test_erp_offline as _ventas
import test_vencimientos as _vto
from conftest import USUARIO
from test_erp_offline import HOY, NODO, _hooks_de_nodo, _pendientes, _subir, _vender
from test_recibir_mercaderia import _cuerpo, _recibir
from test_vencimientos import _columnas_de, _con_conexion, _crear_base_al_dia_de_0001, _indices_de, _ledger, migrar
from test_web_compras import _app, _deposito, _proveedor

from libracommerce.db import ganchos_de_movimientos
from libracommerce.domain.entities import Party, PartyType
from libracommerce.erp import catalogo, compras, offline, offline_stock, stock, ventas
from libracommerce.web.compras_router import OpcionesCompras, build_compras_router

libraedge = pytest.importorskip("libraedge")
from libraedge.db.repository import NodeRepository  # noqa: E402

# Las fixtures de las dos bases (`test_erp_offline.py`) y de la cadena de migraciones al día (`test_vencimientos.py`).
puntas = _ventas.puntas
destino = _vto.destino
abrir_vto = _vto.abrir_vto

NODO_ID = NODO.node_id
PAGO_EFECTIVO = [{"medio": "efectivo", "monto": 200.0, "estado": "aprobado"}]

# ── El modo del Outbox: nodo o central ─────────────────────────────────────

_MODO = {"nodo": NODO}
OUTBOX = offline.Outbox(nodo=lambda: _MODO["nodo"])


@contextlib.contextmanager
def _modo(nodo):
    anterior = _MODO["nodo"]
    _MODO["nodo"] = nodo
    try:
        yield
    finally:
        _MODO["nodo"] = anterior


def como_nodo():
    return _modo(NODO)


def como_central():
    return _modo(None)


# ── Las dos bases, con sucursales, depósitos y el nodo registrado ───────────


def _en_las_dos(puntas, fn):
    for base in puntas:
        with base.abrir() as conn:
            fn(conn)
            conn.commit()


def _revision_0008(conn):
    """Lo que hace la revisión `0008_origen_del_stock` (su propio test la corre de verdad)."""
    conn.execute("ALTER TABLE stock_movements ADD COLUMN origen TEXT")
    conn.execute("ALTER TABLE stock_movements ADD COLUMN origen_id INTEGER")
    conn.execute("CREATE UNIQUE INDEX idx_stock_movements_origen ON stock_movements(origen, origen_id) WHERE origen IS NOT NULL")


def _revision_0009(conn):
    """Lo que hace la revisión `0009_sucursal_del_nodo` (su propio test la corre de verdad, en `test_deposito_del_nodo.py`)."""
    conn.execute("CREATE TABLE nodo_local (clave TEXT PRIMARY KEY, valor TEXT NOT NULL)")


def _preparar(puntas, *, con_0008=True):
    """Sucursales 1 y 2; depósitos 1 y 2 (sucursal 1), 3 (sucursal 2) y 4 (sin sucursal); en el central, el nodo registrado y la revisión `0008`; en
    el nodo, la `0009` (donde guarda su sucursal la carga inicial)."""

    def sucursales(conn):
        conn.execute("INSERT INTO branches (name, is_default) VALUES ('Casa central', 1)")
        conn.execute("INSERT INTO branches (name) VALUES ('Sucursal 2')")
        conn.execute("UPDATE locations SET branch_id = 1 WHERE id = 1")
        catalogo.create_deposito(conn, "Góndola", branch_id=1)
        catalogo.create_deposito(conn, "Otra sucursal", branch_id=2)
        catalogo.create_deposito(conn, "Sin sucursal")

    _en_las_dos(puntas, sucursales)
    nodo, central = puntas
    with nodo.abrir() as conn:
        _revision_0009(conn)
        conn.commit()
    with central.abrir() as conn:
        NodeRepository(conn).register_node(NODO_ID, "1")
        if con_0008:
            _revision_0008(conn)
            conn.commit()
        # Movimientos propios del central en el depósito sin sucursal, para correr sus ids.
        for _ in range(3):
            stock.add_movimiento_stock(conn, 1, "entrada", 1, "propio", fecha=HOY, deposito_id=4)
        conn.commit()


@pytest.fixture
def stock_puntas(puntas):
    """Las dos bases preparadas y los ganchos armados como en un producto (una vez, en las dos puntas)."""
    _preparar(puntas)
    offline_stock.configurar(OUTBOX)
    _MODO["nodo"] = NODO
    yield puntas
    offline_stock.desconfigurar()
    _MODO["nodo"] = NODO


@contextlib.contextmanager
def offline_stock_sin_guarda():
    """Una carga del central que simula lo que había ANTES de tener nodo (la guarda sale y vuelve)."""
    offline_stock.desconfigurar()
    try:
        yield
    finally:
        offline_stock.configurar(OUTBOX)


def _filas(base, sql, params=()):
    with base.abrir() as conn:
        return [dict(f) for f in conn.execute(sql, params).fetchall()]


def _saldo(base, deposito=1, producto=1):
    with base.abrir() as conn:
        return float(stock.get_stock_actual(conn, producto, deposito))


def _tipos(operaciones):
    return [o.operation_type for o in operaciones]


def _en_el_nodo(nodo, fn):
    """`fn(conn)` en el nodo, con el Outbox en modo nodo, confirmando al final."""
    with como_nodo(), nodo.abrir() as conn:
        resultado = fn(conn)
        conn.commit()
    return resultado


def _aceptar(central, operaciones):
    with como_central():
        resultados = _subir(central, operaciones)
    assert [r.result for r in resultados] == ["accepted"] * len(operaciones), [r.error for r in resultados]


def _confirmar(nodo):
    """El nodo da por confirmadas todas sus operaciones pendientes (como si el worker las hubiera subido)."""
    with nodo.abrir() as conn:
        for op in NodeRepository(conn).list_pending_operations(limit=1000):
            NodeRepository(conn).acknowledge_operation(op.operation_id, datetime.now(UTC).isoformat())


# ── El encolado en el nodo ──────────────────────────────────────────────────


def test_un_ajuste_a_mano_se_encola_en_la_misma_transaccion(stock_puntas):
    """El escritor de la capa ERP (`add_movimiento_stock`): si el cambio no se confirma, la operación tampoco queda."""
    nodo, _ = stock_puntas
    with como_nodo(), nodo.abrir() as conn:
        stock.ajustar_stock(conn, 1, 7.0, "recuento", usuario_id=USUARIO["id"], fecha=HOY, deposito_id=1)
        conn.rollback()
    assert _pendientes(nodo) == []
    assert _saldo(nodo) == 10.0

    _en_el_nodo(nodo, lambda conn: stock.ajustar_stock(conn, 1, 7.0, "recuento", usuario_id=USUARIO["id"], fecha=HOY, deposito_id=1))
    (op,) = _pendientes(nodo)
    mid = _filas(nodo, "SELECT id FROM stock_movements WHERE reason_code = 'ajuste' AND note = 'recuento'")[0]["id"]
    assert op.operation_type == "stock.movimiento" and op.aggregate_type == "stock_movimiento"
    assert op.aggregate_id == f"{NODO_ID}:stock_movimiento:{mid}" and op.schema_version == 1
    p = op.payload
    assert (p["version"], p["movimiento_id"], p["producto_id"], p["variante_id"], p["deposito_id"]) == (1, mid, 1, None, 1)
    assert (p["movement_type"], p["reason_code"], p["nota"], p["usuario_id"]) == ("adjustment", "ajuste", "recuento", USUARIO["id"])
    assert float(p["cantidad"]) == -3.0 and p["fecha"].startswith(HOY) and (p["lote"], p["vence"], p["costo"]) == (None, None, None)
    assert (p["ref_tipo"], p["ref_id"]) == (None, None)
    json.dumps(p)  # viaja como JSON


def test_la_transferencia_sube_como_dos_movimientos_y_la_entrada_apunta_a_su_salida(stock_puntas):
    """El escritor del repositorio hexagonal (`append_stock_movement`): también encola, en la transacción de la transferencia."""
    nodo, _ = stock_puntas
    _en_el_nodo(nodo, lambda conn: catalogo.transferir_stock(conn, 1, 1, 2, 2.0, usuario_id=USUARIO["id"], fecha=HOY))
    salida, entrada = _pendientes(nodo)
    assert _tipos([salida, entrada]) == ["stock.movimiento"] * 2
    assert (salida.payload["movement_type"], salida.payload["deposito_id"], float(salida.payload["cantidad"])) == ("transfer_out", 1, -2.0)
    assert (entrada.payload["movement_type"], entrada.payload["deposito_id"], float(entrada.payload["cantidad"])) == ("transfer_in", 2, 2.0)
    assert (salida.payload["ref_tipo"], salida.payload["ref_id"]) == ("transfer", None)
    assert (entrada.payload["ref_tipo"], entrada.payload["ref_id"]) == ("transfer", salida.payload["movimiento_id"])
    assert salida.sequence < entrada.sequence


def test_una_transferencia_que_falla_no_deja_ni_el_movimiento_ni_la_operacion(stock_puntas):
    nodo, _ = stock_puntas
    with como_nodo(), nodo.abrir() as conn:
        with pytest.raises(ValueError):
            catalogo.transferir_stock(conn, 1, 1, 2, 9999.0, usuario_id=USUARIO["id"], fecha=HOY)   # no hay tanto
    assert _pendientes(nodo) == [] and _saldo(nodo, 1) == 10.0 and _saldo(nodo, 2) == 0.0


def test_las_filas_de_una_venta_no_suben_como_movimientos(stock_puntas):
    """La salida de la venta viaja en `venta.registrada`; la anulación y la devolución las reproduce el central: nada de eso sube aparte."""
    nodo, _ = stock_puntas
    hooks = _hooks_de_nodo()
    with como_nodo():
        vid = _vender(nodo, hooks, PAGO_EFECTIVO)
    assert _tipos(_pendientes(nodo)) == ["venta.registrada"]
    with como_nodo():
        otra = _vender(nodo, hooks, PAGO_EFECTIVO)
    with como_nodo(), nodo.abrir() as conn:
        linea = conn.execute("SELECT id FROM sale_items WHERE sale_id=?", (vid,)).fetchone()[0]
        ventas.devolver_items(conn, vid, {linea: 1}, 1, usuario_id=USUARIO["id"], hooks=hooks, clave_operacion="dev-1")
        conn.commit()
        ventas.anular_venta(conn, otra, usuario_id=USUARIO["id"], hooks=hooks)
        conn.commit()
    assert _tipos(_pendientes(nodo)) == ["venta.registrada", "venta.registrada", "venta.devolucion", "venta.anulada"]
    # El ledger del nodo sí tiene las filas de la venta (salidas, devolución y anulación): son 2 + 1 + 1, y ninguna subió aparte.
    assert len(_filas(nodo, "SELECT id FROM stock_movements WHERE source_type = 'venta'")) == 4
    assert _saldo(nodo) == 10.0 - 2 - 2 + 1 + 2


def test_la_carga_inicial_y_las_recepciones_no_suben_como_movimientos():
    assert offline_stock.sube_como_movimiento(_fila(reason_code="ajuste")) is True
    assert offline_stock.sube_como_movimiento(_fila(reason_code="transferencia_entrada", source_type="transfer")) is True
    assert offline_stock.sube_como_movimiento(_fila(reason_code="carga_inicial")) is False
    assert offline_stock.sube_como_movimiento(_fila(source_type="purchase_receipt", movement_type="purchase")) is False
    for origen in ("venta", "sale", "sale_return"):
        assert offline_stock.sube_como_movimiento(_fila(source_type=origen)) is False


def _fila(**campos):
    base = {"id": 1, "item_id": 1, "variant_id": None, "location_id": 1, "movement_type": "adjustment", "quantity_delta": 1,
            "occurred_at": "2026-10-21T00:00:00", "source_type": None, "source_id": None, "unit_cost": None, "lot_code": None,
            "expires_at": None, "note": "", "created_by": None, "reason_code": None}
    return {**base, **campos}


def test_fuera_de_un_nodo_no_encola_nada(stock_puntas):
    nodo, _ = stock_puntas
    with como_central(), nodo.abrir() as conn:
        stock.add_movimiento_stock(conn, 1, "entrada", 1, "x", fecha=HOY, deposito_id=4)   # el depósito sin sucursal: la guarda no lo ve
        conn.commit()
    assert _pendientes(nodo) == []


def test_un_gancho_que_falla_se_lleva_el_movimiento_en_los_dos_escritores(stock_puntas):
    """Nunca queda un movimiento sin su operación: ni con el escritor ERP (la excepción sube al llamador, que revierte) ni con el del
    repositorio fuera de `transaction()` (que revierte él, o el próximo commit de la conexión lo confirmaría)."""
    from libracommerce.db.repository import SqliteCommerceRepository
    from libracommerce.domain.inventory import StockMovement, StockMovementType

    def falla(conn, movimiento):
        raise RuntimeError("no se pudo encolar")

    nodo, _ = stock_puntas
    ganchos_de_movimientos.configurar_al_mover(falla)
    with como_nodo(), nodo.abrir() as conn:
        with pytest.raises(RuntimeError, match="no se pudo encolar"):
            stock.add_movimiento_stock(conn, 1, "entrada", 5, "x", fecha=HOY, deposito_id=1)
        conn.rollback()
    with como_nodo(), nodo.abrir() as conn:
        with pytest.raises(RuntimeError, match="no se pudo encolar"):
            SqliteCommerceRepository(conn).append_stock_movement(StockMovement(
                id=None, item_id=1, location_id=1, movement_type=StockMovementType.ADJUSTMENT, quantity_delta=5,
                occurred_at=datetime(2026, 10, 21)))
        conn.commit()      # aunque el llamador commitee después: el escritor ya revirtió lo suyo
    assert _saldo(nodo) == 10.0 and _pendientes(nodo) == []


def test_sin_ganchos_registrados_los_escritores_no_cambian(puntas):
    """Sin `configurar`, el camino de siempre (y los ganchos son globales: este test no los arma)."""
    nodo, _ = puntas
    with nodo.abrir() as conn:
        stock.add_movimiento_stock(conn, 1, "entrada", 5, "x", fecha=HOY)
        conn.commit()
    assert _pendientes(nodo) == [] and _saldo(nodo) == 15.0


# ── Los aplicadores del central ─────────────────────────────────────────────


def test_un_ajuste_con_lote_vencimiento_y_costo_llega_tal_cual_y_con_su_origen(stock_puntas):
    nodo, central = stock_puntas
    _en_el_nodo(nodo, lambda conn: stock.add_movimiento_stock(
        conn, 1, "entrada", 5, "ingreso con lote", fecha=HOY, deposito_id=2, lot_code="L1", expires_at="2027-01-31",
        unit_cost="61.5", usuario_id=USUARIO["id"]))
    ops = _pendientes(nodo)
    _aceptar(central, ops)
    (propio,) = _filas(nodo, "SELECT * FROM stock_movements WHERE note = 'ingreso con lote'")
    (en_el_central,) = _filas(central, "SELECT * FROM stock_movements WHERE origen IS NOT NULL")
    assert en_el_central["id"] != propio["id"]
    assert (en_el_central["origen"], en_el_central["origen_id"]) == (NODO_ID, propio["id"])
    for campo in ("item_id", "variant_id", "location_id", "movement_type", "occurred_at", "lot_code", "expires_at", "note",
                  "created_by", "reason_code"):
        assert en_el_central[campo] == propio[campo], campo
    assert float(en_el_central["quantity_delta"]) == float(propio["quantity_delta"]) == 5.0
    assert float(en_el_central["unit_cost"]) == 61.5
    assert _saldo(central, 2) == _saldo(nodo, 2) == 5.0


def test_aplicar_dos_veces_el_mismo_movimiento_es_una_fila(stock_puntas):
    nodo, central = stock_puntas
    _en_el_nodo(nodo, lambda conn: stock.ajustar_stock(conn, 1, 4.0, "recuento", fecha=HOY, deposito_id=1))
    (op,) = _pendientes(nodo)
    aplicadores = offline.aplicadores()
    for _ in range(3):
        with como_central(), central.abrir() as conn:
            aplicadores["stock.movimiento"](conn, op)
            conn.commit()
    assert len(_filas(central, "SELECT id FROM stock_movements WHERE origen IS NOT NULL")) == 1
    assert _saldo(central) == 4.0


def test_la_transferencia_dentro_de_la_sucursal_sube_y_la_entrada_se_ata_a_la_salida_del_central(stock_puntas):
    nodo, central = stock_puntas
    _en_el_nodo(nodo, lambda conn: catalogo.transferir_stock(conn, 1, 1, 2, 2.0, usuario_id=USUARIO["id"], fecha=HOY))
    _aceptar(central, _pendientes(nodo))
    filas = _filas(central, "SELECT * FROM stock_movements WHERE source_type = 'transfer' ORDER BY id")
    salida, entrada = filas
    assert (salida["movement_type"], salida["source_id"], entrada["movement_type"]) == ("transfer_out", None, "transfer_in")
    assert entrada["source_id"] == salida["id"]            # el id del central, no el del nodo
    nodo_salida = _filas(nodo, "SELECT id FROM stock_movements WHERE movement_type = 'transfer_out'")[0]["id"]
    assert salida["id"] != nodo_salida and salida["origen_id"] == nodo_salida
    assert (_saldo(central, 1), _saldo(central, 2)) == (_saldo(nodo, 1), _saldo(nodo, 2)) == (8.0, 2.0)
    # El par se reconoce igual que en el nodo: la salida tiene su contraparte.
    assert [f["id"] for f in _filas(central, "SELECT id FROM stock_movements WHERE source_type = 'transfer' AND source_id = ?",
                                    (salida["id"],))] == [entrada["id"]]


def test_una_transferencia_a_otra_sucursal_ni_se_escribe_en_el_nodo(stock_puntas):
    nodo, _ = stock_puntas
    with como_nodo(), nodo.abrir() as conn:
        with pytest.raises(offline_stock.TransferenciaEntreSucursales, match="sucursales distintas"):
            catalogo.transferir_stock(conn, 1, 1, 3, 2.0, usuario_id=USUARIO["id"], fecha=HOY)
        conn.rollback()
    assert _pendientes(nodo) == [] and _saldo(nodo, 1) == 10.0 and _saldo(nodo, 3) == 0.0
    # Al depósito sin sucursal tampoco.
    with como_nodo(), nodo.abrir() as conn:
        with pytest.raises(offline_stock.TransferenciaEntreSucursales):
            catalogo.transferir_stock(conn, 1, 1, 4, 2.0, usuario_id=USUARIO["id"], fecha=HOY)
        conn.rollback()


def _con_payload(op, **cambios):
    return replace(op, payload={**op.payload, **cambios})


def test_el_central_rechaza_un_movimiento_de_otra_sucursal_o_sin_sucursal(stock_puntas):
    nodo, central = stock_puntas
    _en_el_nodo(nodo, lambda conn: stock.ajustar_stock(conn, 1, 7.0, "recuento", fecha=HOY, deposito_id=1))
    (op,) = _pendientes(nodo)
    antes = len(_filas(central, "SELECT id FROM stock_movements"))
    for deposito, texto in ((3, "sucursal"), (4, "sucursal"), (999, "no existe")):
        malo = replace(_con_payload(op, deposito_id=deposito), operation_id=f"{op.operation_id}-{deposito}")
        with como_central():
            (resultado,) = _subir(central, [malo])
        assert resultado.result == "rejected" and texto in resultado.error, resultado.error
    assert len(_filas(central, "SELECT id FROM stock_movements")) == antes
    with pytest.raises(offline_stock.DepositoFueraDeLaSucursal):
        with como_central(), central.abrir() as conn:
            offline_stock.aplicar_stock_movimiento(conn, _con_payload(op, deposito_id=3))


def test_la_entrada_de_una_transferencia_cuya_salida_no_subio_se_rechaza(stock_puntas):
    nodo, central = stock_puntas
    _en_el_nodo(nodo, lambda conn: catalogo.transferir_stock(conn, 1, 1, 2, 2.0, fecha=HOY))
    _, entrada = _pendientes(nodo)
    with como_central():
        (resultado,) = _subir(central, [entrada])
    assert resultado.result == "rejected" and "salida" in resultado.error and "no está en el central" in resultado.error


def test_un_producto_o_un_tipo_que_el_central_no_entiende_se_rechaza_antes_de_escribir(stock_puntas):
    nodo, central = stock_puntas
    _en_el_nodo(nodo, lambda conn: stock.ajustar_stock(conn, 1, 7.0, "recuento", fecha=HOY, deposito_id=1))
    (op,) = _pendientes(nodo)
    for cambio, texto in (({"producto_id": 999}, "producto"), ({"variante_id": 999}, "variante"),
                          ({"movement_type": "regalo"}, "desconocido"), ({"cantidad": "0"}, "cero"),
                          ({"cantidad": "mucho"}, "número"), ({"version": 9}, "versión")):
        with como_central():
            (resultado,) = _subir(central, [replace(_con_payload(op, **cambio), operation_id=f"x-{texto}")])
        assert resultado.result == "rejected" and texto in resultado.error, (cambio, resultado.error)
    assert _filas(central, "SELECT id FROM stock_movements WHERE origen IS NOT NULL") == []


def test_un_nodo_que_el_central_no_conoce_se_rechaza(stock_puntas):
    nodo, central = stock_puntas
    _en_el_nodo(nodo, lambda conn: stock.ajustar_stock(conn, 1, 7.0, "recuento", fecha=HOY, deposito_id=1))
    (op,) = _pendientes(nodo)
    with central.abrir() as conn:
        conn.execute("DELETE FROM node_identity")
        conn.commit()
    with como_central():
        (resultado,) = _subir(central, [op])
    assert resultado.result == "rejected" and "node_identity" in resultado.error


def test_sin_la_revision_0008_el_central_pide_migrar_y_no_traba_la_cola(puntas):
    _preparar(puntas, con_0008=False)
    nodo, central = puntas
    offline_stock.configurar(OUTBOX)
    try:
        _en_el_nodo(nodo, lambda conn: stock.ajustar_stock(conn, 1, 7.0, "recuento", fecha=HOY, deposito_id=1))
        (op,) = _pendientes(nodo)
        with como_central():
            (resultado,) = _subir(central, [op])
    finally:
        offline_stock.desconfigurar()
    assert resultado.result == "rejected" and "0008_origen_del_stock" in resultado.error and "libracommerce-migrar" in resultado.error
    with pytest.raises(offline_stock.StockSinRevision):
        with central.abrir() as conn:
            offline_stock.aplicar_stock_movimiento(conn, op)


def test_los_errores_del_central_son_value_error_y_no_trabarian_la_cola():
    """Una excepción que no es `ValueError` es un fallo de transporte para el nodo y corta la tanda (ADR-020 de libraedge)."""
    for clase in (offline_stock.StockDelNodoError, offline_stock.StockSinRevision, offline_stock.NodoDesconocido,
                  offline_stock.DepositoFueraDeLaSucursal, offline_stock.SalidaNoSincronizada,
                  offline_stock.MovimientoRechazadoPorElCentral, offline_stock.CompraRechazadaPorElCentral,
                  offline_stock.TransferenciaEntreSucursales, offline_stock.DepositosSinSucursal):
        assert issubclass(clase, ValueError)
    assert not issubclass(offline_stock.DepositoDeSucursalConNodo, ValueError)


# ── La guarda del central ───────────────────────────────────────────────────


def test_el_central_se_niega_a_mover_el_stock_de_una_sucursal_con_nodo_en_los_dos_escritores(stock_puntas):
    _, central = stock_puntas
    with como_central(), central.abrir() as conn:
        with pytest.raises(offline_stock.DepositoDeSucursalConNodo) as error:
            stock.ajustar_stock(conn, 1, 3.0, "recuento", fecha=HOY, deposito_id=1)               # el escritor ERP
        conn.rollback()
        assert (error.value.deposito_id, error.value.node_id) == (1, NODO_ID)
        with pytest.raises(offline_stock.DepositoDeSucursalConNodo):
            catalogo.transferir_stock(conn, 1, 1, 2, 1.0, fecha=HOY)                             # el escritor del repositorio
        conn.rollback()
    assert _saldo(central, 1) == 10.0 and _saldo(central, 2) == 0.0
    assert len(_filas(central, "SELECT id FROM stock_movements WHERE source_type = 'transfer'")) == 0


def test_la_guarda_no_toca_la_otra_sucursal_ni_los_depositos_sin_sucursal(stock_puntas):
    _, central = stock_puntas
    with como_central(), central.abrir() as conn:
        stock.add_movimiento_stock(conn, 1, "entrada", 5, "x", fecha=HOY, deposito_id=3)
        stock.add_movimiento_stock(conn, 1, "entrada", 5, "x", fecha=HOY, deposito_id=4)
        catalogo.transferir_stock(conn, 1, 3, 4, 2.0, fecha=HOY)
        conn.commit()
    assert (_saldo(central, 3), _saldo(central, 4)) == (3.0, 5.0 + 3.0 + 2.0)


def test_una_venta_desde_la_web_y_anular_una_venta_del_nodo_dan_409(stock_puntas):
    nodo, central = stock_puntas
    hooks = _hooks_de_nodo()
    with como_central():
        with pytest.raises(offline_stock.DepositoDeSucursalConNodo):
            _vender(central, hooks, PAGO_EFECTIVO)
    assert _saldo(central) == 10.0 and _filas(central, "SELECT id FROM sales") == []

    with como_nodo():
        _vender(nodo, hooks, PAGO_EFECTIVO)
    _aceptar(central, _pendientes(nodo))                         # el aplicador de la venta del nodo NO dispara la guarda
    assert _saldo(central) == 8.0
    (venta,) = _filas(central, "SELECT id, status FROM sales")
    with como_central(), central.abrir() as conn:
        with pytest.raises(offline_stock.DepositoDeSucursalConNodo):
            ventas.anular_venta(conn, venta["id"], usuario_id=USUARIO["id"])
        conn.rollback()
    assert _saldo(central) == 8.0 and _filas(central, "SELECT status FROM sales")[0]["status"] == venta["status"]


def test_un_nodo_dado_de_baja_o_una_base_sin_nodos_no_frenan_nada(stock_puntas):
    _, central = stock_puntas
    with central.abrir() as conn:
        conn.execute("UPDATE node_identity SET active = 0")
        conn.commit()
    with como_central(), central.abrir() as conn:
        stock.add_movimiento_stock(conn, 1, "entrada", 1, "x", fecha=HOY, deposito_id=1)
        conn.commit()
        conn.execute("UPDATE node_identity SET active = 1")
        conn.commit()
        with pytest.raises(offline_stock.DepositoDeSucursalConNodo):
            stock.add_movimiento_stock(conn, 1, "entrada", 1, "x", fecha=HOY, deposito_id=1)
        conn.rollback()
        conn.execute("DROP TABLE node_identity")        # una base sin LibraEdge: no hay nodos, no hay guarda
        conn.commit()
        stock.add_movimiento_stock(conn, 1, "entrada", 1, "x", fecha=HOY, deposito_id=1)
        conn.commit()


def test_en_un_nodo_la_guarda_no_corre_aunque_su_propia_sucursal_tenga_nodo(stock_puntas):
    """El nodo tiene su `node_identity` y su sucursal: si la guarda corriera ahí, no podría mover su propio stock."""
    nodo, _ = stock_puntas
    with nodo.abrir() as conn:
        NodeRepository(conn).register_node(NODO_ID, "1")
    _en_el_nodo(nodo, lambda conn: stock.add_movimiento_stock(conn, 1, "entrada", 1, "x", fecha=HOY, deposito_id=1))
    assert _tipos(_pendientes(nodo)) == ["stock.movimiento"]


def test_los_aplicadores_corren_con_aplicando_del_nodo_y_lo_sueltan_al_terminar_aunque_fallen():
    visto = []

    def aplicador(conn, operacion):
        visto.append(offline_stock.aplicando_del_nodo.get())
        if operacion == "falla":
            raise ValueError("no")

    envuelto = offline_stock.aplicador_del_nodo(aplicador)
    envuelto(None, "bien")
    with pytest.raises(ValueError):
        envuelto(None, "falla")
    assert visto == [True, True] and offline_stock.aplicando_del_nodo.get() is False
    assert all(isinstance(f, type(lambda: 0)) for f in offline.aplicadores().values())
    assert set(offline.aplicadores()) >= set(offline_stock.TIPOS_DE_STOCK)


def test_instalar_manejador_409_convierte_la_guarda_en_un_409_de_verdad():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    def armar():
        app = FastAPI()

        @app.post("/mover")
        def mover():
            raise offline_stock.DepositoDeSucursalConNodo(3, "nodo-s02", "1")

        return app

    sin_manejador = TestClient(armar(), raise_server_exceptions=False)
    assert sin_manejador.post("/mover").status_code == 500          # una Exception pelada no es un 422 ni un 409: hay que instalarlo
    app = armar()
    offline_stock.instalar_manejador_409(app)
    respuesta = TestClient(app).post("/mover")
    assert respuesta.status_code == 409
    assert respuesta.json()["codigo"] == "deposito_de_sucursal_con_nodo" and respuesta.json()["deposito_id"] == 3
    assert "nodo-s02" in respuesta.json()["detail"]


# ── Los depósitos sin sucursal ──────────────────────────────────────────────


def test_depositos_sin_sucursal_se_listan_y_se_exige_asignarlos(stock_puntas):
    _, central = stock_puntas
    with central.abrir() as conn:
        assert offline_stock.listar_depositos_sin_sucursal(conn) == [{"id": 4, "nombre": "Sin sucursal", "activo": True}]
        # Para `Aprovisionamiento(validar_registro=...)` de libraedge: textos legibles.
        assert offline_stock.depositos_sin_sucursal(conn) == ["El depósito 'Sin sucursal' no tiene sucursal"]
        with pytest.raises(offline_stock.DepositosSinSucursal, match="Sin sucursal") as error:
            offline_stock.exigir_depositos_con_sucursal(conn)
        assert error.value.depositos[0]["id"] == 4
        conn.execute("UPDATE locations SET active = 0 WHERE id = 4")
        assert offline_stock.depositos_sin_sucursal(conn) == []                                  # uno inactivo no frena
        assert offline_stock.listar_depositos_sin_sucursal(conn, solo_activos=False)[0]["activo"] is False
        offline_stock.exigir_depositos_con_sucursal(conn)
        conn.rollback()


def test_con_una_sola_sucursal_se_pueden_adoptar_los_depositos(puntas):
    nodo, _ = puntas
    with nodo.abrir() as conn:
        conn.execute("INSERT INTO branches (name, is_default) VALUES ('Casa central', 1)")
        catalogo.create_deposito(conn, "Huérfano")
        conn.commit()
        assert [d["nombre"] for d in offline_stock.listar_depositos_sin_sucursal(conn)] == ["Depósito principal", "Huérfano"]
        with pytest.raises(offline_stock.DepositosSinSucursal):
            offline_stock.exigir_depositos_con_sucursal(conn)
        conn.rollback()
        offline_stock.exigir_depositos_con_sucursal(conn, adoptar_si_hay_una=True)     # la única sucursal recibe los huérfanos
        assert offline_stock.listar_depositos_sin_sucursal(conn) == []
        assert {f[0] for f in conn.execute("SELECT branch_id FROM locations").fetchall()} == {1}
        conn.commit()
        conn.execute("INSERT INTO branches (name) VALUES ('Otra')")
        catalogo.create_deposito(conn, "Otro huérfano")
        conn.commit()
        with pytest.raises(offline_stock.DepositosSinSucursal):      # con dos sucursales no elige por el dueño
            offline_stock.exigir_depositos_con_sucursal(conn, adoptar_si_hay_una=True)
        conn.rollback()


# ── La carga inicial ────────────────────────────────────────────────────────


def _traer(central, sucursal="1"):
    """Lo que hace LibraEdge: pedirle al central la carga (por JSON, como viaja) de la sucursal del nodo."""

    def traer():
        with central.abrir() as conn:
            return json.loads(json.dumps(offline_stock.carga_inicial_del_central(conn, NODO_ID, sucursal)))

    return traer


def _cargar(nodo, traer):
    with como_nodo(), nodo.abrir() as conn:
        resumen = offline_stock.aplicar_carga_inicial(conn, traer)
        conn.commit()
    return resumen


def _vaciar_el_ledger(nodo):
    """Un nodo recién instalado: su ledger está vacío."""
    with nodo.abrir() as conn:
        conn.execute("DELETE FROM stock_movements")
        conn.commit()


def test_la_carga_inicial_del_central_trae_los_saldos_de_los_depositos_de_la_sucursal(stock_puntas):
    _, central = stock_puntas
    with como_central(), central.abrir() as conn:
        stock.add_movimiento_stock(conn, 1, "entrada", 5, "x", fecha=HOY, deposito_id=3)          # otra sucursal: no va
        stock.add_movimiento_stock(conn, 1, "entrada", 5, "x", fecha=HOY, deposito_id=4)          # sin sucursal: no va
        conn.commit()
    with offline_stock_sin_guarda(), central.abrir() as conn:
        stock.add_movimiento_stock(conn, 1, "entrada", 3, "con lote", fecha=HOY, deposito_id=2, lot_code="L1", expires_at="2027-01-31")
        conn.commit()
    with central.abrir() as conn:
        carga = offline_stock.carga_inicial_del_central(conn, NODO_ID, "1")
    assert carga["version"] == 1 and [d["id"] for d in carga["depositos"]] == [1, 2]
    assert {(s["location_id"], s["lote"], s["vence"]): s["cantidad"] for s in carga["saldos"]} == {
        (1, None, None): "10", (2, "L1", "2027-01-31"): "3"}
    assert all(set(s) == {"item_id", "variant_id", "location_id", "lote", "vence", "cantidad"} for s in carga["saldos"])
    assert carga["corte"]["movimiento_id"] is not None and carga["corte"]["a"]
    json.dumps(carga)
    with central.abrir() as conn:
        assert offline_stock.carga_inicial_del_central(conn, NODO_ID, "77")["saldos"] == []      # una sucursal sin depósitos
        with pytest.raises(ValueError, match="sucursal"):
            offline_stock.carga_inicial_del_central(conn, NODO_ID, "S02")


def test_un_nodo_nuevo_trae_las_existencias_y_no_las_sube_de_vuelta(stock_puntas):
    nodo, central = stock_puntas
    with offline_stock_sin_guarda(), central.abrir() as conn:
        stock.add_movimiento_stock(conn, 1, "entrada", 3, "con lote", fecha=HOY, deposito_id=2, lot_code="L1", expires_at="2027-01-31")
        conn.commit()
    _vaciar_el_ledger(nodo)

    resumen = _cargar(nodo, _traer(central))
    assert resumen == {"hecha": True, "motivo": None, "ajustes": 2, "buckets": 2}
    filas = _filas(nodo, "SELECT * FROM stock_movements ORDER BY id")
    assert {f["reason_code"] for f in filas} == {"carga_inicial"} and {f["movement_type"] for f in filas} == {"adjustment"}
    assert (_saldo(nodo, 1), _saldo(nodo, 2)) == (10.0, 3.0)
    assert [(f["location_id"], f["lot_code"], f["expires_at"]) for f in filas] == [(1, None, None), (2, "L1", "2027-01-31")]
    assert _pendientes(nodo) == []                              # la carga viene del central: no vuelve a subir


def test_la_carga_inicial_es_idempotente(stock_puntas):
    nodo, central = stock_puntas
    _vaciar_el_ledger(nodo)
    assert _cargar(nodo, _traer(central))["ajustes"] == 1
    for _ in range(2):
        assert _cargar(nodo, _traer(central)) == {"hecha": True, "motivo": None, "ajustes": 0, "buckets": 1}
    assert len(_filas(nodo, "SELECT id FROM stock_movements")) == 1 and _saldo(nodo) == 10.0


def test_ventas_antes_de_la_carga_no_se_cuentan_dos_veces(stock_puntas):
    """El nodo ya vendió, la venta subió y el central la aplicó: está en las dos puntas y el delta es 0."""
    nodo, central = stock_puntas
    with como_nodo():
        _vender(nodo, _hooks_de_nodo(), PAGO_EFECTIVO)
    _aceptar(central, _pendientes(nodo))
    _confirmar(nodo)
    assert (_saldo(nodo), _saldo(central)) == (8.0, 8.0)
    assert _cargar(nodo, _traer(central)) == {"hecha": True, "motivo": None, "ajustes": 0, "buckets": 1}
    assert _saldo(nodo) == _saldo(central) == 8.0


def test_si_el_central_tenia_mas_la_carga_suma_la_diferencia_y_la_venta_posterior_no_se_cuenta_dos_veces(stock_puntas):
    nodo, central = stock_puntas
    with offline_stock_sin_guarda(), central.abrir() as conn:
        stock.add_movimiento_stock(conn, 1, "entrada", 5, "se contó de más en el central", fecha=HOY, deposito_id=1)
        conn.commit()
    assert _cargar(nodo, _traer(central)) == {"hecha": True, "motivo": None, "ajustes": 1, "buckets": 1}
    (ajuste,) = _filas(nodo, "SELECT quantity_delta, reason_code FROM stock_movements WHERE reason_code = 'carga_inicial'")
    assert float(ajuste["quantity_delta"]) == 5.0 and _saldo(nodo) == 15.0
    # Después de la carga el nodo vende, la venta sube y se aplica en el central: las dos puntas dan lo mismo y volver a cargar no cambia nada.
    with como_nodo():
        _vender(nodo, _hooks_de_nodo(), PAGO_EFECTIVO)
    ops = _pendientes(nodo)
    assert _tipos(ops) == ["venta.registrada"]
    _aceptar(central, ops)
    _confirmar(nodo)
    assert _saldo(nodo) == _saldo(central) == 13.0
    assert _cargar(nodo, _traer(central))["ajustes"] == 0 and _saldo(nodo) == 13.0


def test_con_operaciones_sin_confirmar_la_carga_espera_y_no_escribe(stock_puntas):
    nodo, central = stock_puntas
    with como_nodo():
        _vender(nodo, _hooks_de_nodo(), PAGO_EFECTIVO)
    llamadas = []
    resumen = _cargar(nodo, lambda: llamadas.append(1))
    assert resumen["hecha"] is False and resumen["motivo"] == "operaciones_pendientes" and llamadas == []
    assert _saldo(nodo) == 8.0 and len(_filas(nodo, "SELECT id FROM stock_movements WHERE reason_code = 'carga_inicial'")) == 0
    # Cuando el central aplicó y el nodo confirmó, el ciclo siguiente la hace.
    _aceptar(central, _pendientes(nodo))
    _confirmar(nodo)
    assert _cargar(nodo, _traer(central))["hecha"] is True and _saldo(nodo) == _saldo(central) == 8.0


def test_un_movimiento_de_stock_en_revision_manual_frena_la_carga(stock_puntas):
    nodo, central = stock_puntas
    _en_el_nodo(nodo, lambda conn: stock.ajustar_stock(conn, 1, 7.0, "recuento", fecha=HOY, deposito_id=1))
    (op,) = _pendientes(nodo)
    with nodo.abrir() as conn:
        NodeRepository(conn).mark_operation_manual_review(op.operation_id, "el central no pudo")
    assert _cargar(nodo, _traer(central))["motivo"] == "stock_en_revision"
    # Una de otro tipo en revisión no frena: no toca el stock del que se compara.
    with nodo.abrir() as conn:
        conn.execute("UPDATE sync_outbox SET operation_type = 'cliente.alta' WHERE operation_id = ?", (op.operation_id,))
        conn.commit()
    assert _cargar(nodo, _traer(central))["hecha"] is True


def test_si_el_stock_cambia_mientras_se_trae_la_carga_no_escribe_nada(stock_puntas):
    """Una venta que entra DURANTE `traer()` no está en la foto del central: escribir el delta la descontaría dos veces."""
    nodo, central = stock_puntas

    def traer_y_vender():
        with como_nodo():
            _vender(nodo, _hooks_de_nodo(), PAGO_EFECTIVO)
        return _traer(central)()

    resumen = _cargar(nodo, traer_y_vender)
    assert resumen["hecha"] is False and resumen["motivo"] == "el_stock_cambio"
    assert len(_filas(nodo, "SELECT id FROM stock_movements WHERE reason_code = 'carga_inicial'")) == 0 and _saldo(nodo) == 8.0
    _aceptar(central, _pendientes(nodo))
    _confirmar(nodo)
    assert _cargar(nodo, _traer(central))["ajustes"] == 0 and _saldo(nodo) == _saldo(central) == 8.0


def test_un_espejo_incompleto_espera_y_no_escribe_a_medias(stock_puntas):
    nodo, central = stock_puntas
    with offline_stock_sin_guarda(), central.abrir() as conn:
        otro = catalogo.create_producto(conn, "Solo en el central", precio_venta=1.0, precio_costo=1.0)
        stock.add_movimiento_stock(conn, otro, "entrada", 4, "x", fecha=HOY, deposito_id=1)
        conn.commit()
    _vaciar_el_ledger(nodo)
    resumen = _cargar(nodo, _traer(central))
    assert resumen["hecha"] is False and resumen["motivo"] == "espejo_incompleto" and resumen["faltan"] == {"productos": [otro]}
    assert _filas(nodo, "SELECT id FROM stock_movements") == []                                    # ni siquiera el producto que sí está


def test_la_carga_pide_un_nodo_y_una_version_que_entienda(stock_puntas):
    import sqlite3

    nodo, central = stock_puntas
    with pytest.raises(ValueError, match="outbox"):
        offline_stock.aplicar_carga_inicial(sqlite3.connect(":memory:"), lambda: {})
    with pytest.raises(ValueError, match="versión"):
        _cargar(nodo, lambda: {"version": 9})


# ── compra.recibida ─────────────────────────────────────────────────────────


def _escenario_de_compra(puntas):
    """Un proveedor y una orden enviada (24 de Yerba a 250), creados en las dos bases con los mismos ids: se crean en la web y bajan por el espejo."""

    def alta(conn):
        from libracommerce.db.repository import repositorio_de

        proveedor = repositorio_de(conn).save_party(Party(None, PartyType.ORGANIZATION, "Distribuidora SA")).id
        orden = compras.crear_orden(conn, supplier_party_id=proveedor)["id"]
        compras.agregar_linea_orden(conn, orden, item_id=1, quantity_ordered=Decimal("24"), unit_cost=Decimal("250"))
        compras.enviar_orden(conn, orden)
        return proveedor, orden

    nodo, central = puntas
    en_el_nodo, en_el_central = None, None
    with nodo.abrir() as conn:
        en_el_nodo = alta(conn)
    with central.abrir() as conn:
        en_el_central = alta(conn)
    assert en_el_nodo == en_el_central
    return en_el_nodo


def _recibir_en_el_nodo(nodo, proveedor, orden, *, cantidad=10, clave="k-1", deposito=1, lote="L9", vence=datetime(2027, 3, 1), al_recibir=None):
    with como_nodo(), nodo.abrir() as conn:
        recepcion = compras.recibir_mercaderia(
            conn, supplier_party_id=proveedor, deposito_id=deposito, clave_operacion=clave, occurred_at=datetime(2026, 10, 21, 13, 5, tzinfo=UTC),
            items=[{"item_id": 1, "quantity": cantidad, "unit_cost": 250, "lot_code": lote, "expires_at": vence}],
            purchase_order_id=orden, document_reference="R-0001-00004567",
            al_recibir=al_recibir or offline_stock.recepcion_con_outbox(OUTBOX))
        conn.commit()
    return recepcion


@pytest.fixture
def con_compra(stock_puntas):
    nodo, central = stock_puntas
    # La clave de operación de la recepción (revisión 0006) y el resto de la cadena las pone la migración; acá la columna sola.
    for base in (nodo, central):
        with base.abrir() as conn:
            if "clave_operacion" not in {f[1] for f in conn.execute("PRAGMA table_info(purchase_receipts)").fetchall()}:
                conn.execute("ALTER TABLE purchase_receipts ADD COLUMN clave_operacion TEXT")
                conn.execute("CREATE UNIQUE INDEX idx_purchase_receipts_clave_operacion ON purchase_receipts(clave_operacion) "
                             "WHERE clave_operacion IS NOT NULL")
                conn.commit()
    proveedor, orden = _escenario_de_compra(stock_puntas)
    return nodo, central, proveedor, orden


def test_una_recepcion_en_el_nodo_sube_como_compra_recibida_y_no_como_movimiento(con_compra):
    nodo, central, proveedor, orden = con_compra
    recepcion = _recibir_en_el_nodo(nodo, proveedor, orden)
    (op,) = _pendientes(nodo)                                       # una sola: las filas `purchase_receipt` no suben aparte
    assert op.operation_type == "compra.recibida" and op.aggregate_type == "compra_recepcion"
    assert op.aggregate_id == f"{NODO_ID}:compra_recepcion:{recepcion['id']}" and op.schema_version == 1
    p = op.payload
    assert (p["version"], p["recepcion_id"], p["proveedor_id"], p["orden_id"], p["remito"], p["deposito_id"]) == (
        1, recepcion["id"], proveedor, orden, "R-0001-00004567", 1)
    assert p["fecha"] == "2026-10-21T13:05:00+00:00"
    (linea,) = p["lineas"]
    assert (linea["producto_id"], float(linea["cantidad"]), float(linea["costo"]), linea["lote"]) == (1, 10.0, 250.0, "L9")
    assert linea["vence"].startswith("2027-03-01")
    json.dumps(p)
    assert _saldo(nodo) == 20.0


def test_la_compra_recibida_en_el_nodo_se_aplica_en_el_central_y_la_orden_avanza(con_compra):
    nodo, central, proveedor, orden = con_compra
    _recibir_en_el_nodo(nodo, proveedor, orden, cantidad=10)
    ops = _pendientes(nodo)
    _aceptar(central, ops)                                           # bajo la guarda del central: no la dispara
    assert _saldo(central) == _saldo(nodo) == 20.0
    (orden_central,) = _filas(central, "SELECT status FROM purchase_orders")
    assert orden_central["status"] == "partial"
    (item,) = _filas(central, "SELECT quantity_received FROM purchase_order_items")
    assert float(item["quantity_received"]) == 10.0
    assert float(_filas(central, "SELECT default_cost FROM catalog_items WHERE id = 1")[0]["default_cost"]) == 250.0
    (recepcion,) = _filas(central, "SELECT * FROM purchase_receipts")
    assert recepcion["clave_operacion"] == offline.clave_de_operacion(ops[0]) and recepcion["document_reference"] == "R-0001-00004567"
    assert recepcion["supplier_party_id"] == proveedor and recepcion["purchase_order_id"] == orden
    (fila,) = _filas(central, "SELECT * FROM stock_movements WHERE source_type = 'purchase_receipt'")
    assert (fila["lot_code"], fila["location_id"], float(fila["unit_cost"])) == ("L9", 1, 250.0) and fila["expires_at"].startswith("2027-03-01")
    # La segunda recepción completa la orden.
    _confirmar(nodo)
    _recibir_en_el_nodo(nodo, proveedor, orden, cantidad=14, clave="k-2", lote=None, vence=None)
    _aceptar(central, _pendientes(nodo))
    assert _filas(central, "SELECT status FROM purchase_orders")[0]["status"] == "received" and _saldo(central) == 34.0


def test_aplicar_dos_veces_la_misma_compra_es_una_recepcion(con_compra):
    nodo, central, proveedor, orden = con_compra
    _recibir_en_el_nodo(nodo, proveedor, orden)
    (op,) = _pendientes(nodo)
    aplicadores = offline.aplicadores()
    for _ in range(3):
        with como_central(), central.abrir() as conn:
            aplicadores["compra.recibida"](conn, op)
            conn.commit()
    assert len(_filas(central, "SELECT id FROM purchase_receipts")) == 1 and _saldo(central) == 20.0
    assert float(_filas(central, "SELECT quantity_received FROM purchase_order_items")[0]["quantity_received"]) == 10.0


def test_la_confirmacion_en_tres_pasos_tambien_sube_la_compra(con_compra):
    nodo, central, proveedor, orden = con_compra
    with como_nodo(), nodo.abrir() as conn:
        recepcion = compras.crear_recepcion(conn, supplier_party_id=proveedor, purchase_order_id=orden, document_reference="R-2")
        compras.agregar_linea_recepcion(conn, recepcion["id"], item_id=1, quantity=Decimal("6"), unit_cost=Decimal("250"), lot_code="L7")
        compras.confirmar_recepcion(conn, recepcion["id"], location_id=1, occurred_at=datetime(2026, 10, 21, 14, 0, tzinfo=UTC),
                                    al_recibir=offline_stock.recepcion_con_outbox(OUTBOX))
        conn.commit()
    (op,) = _pendientes(nodo)
    assert op.operation_type == "compra.recibida" and op.payload["remito"] == "R-2" and op.payload["recepcion_id"] == recepcion["id"]
    _aceptar(central, [op])
    assert _saldo(central) == 16.0 and _filas(central, "SELECT status FROM purchase_orders")[0]["status"] == "partial"


def test_si_el_gancho_de_la_recepcion_falla_no_queda_la_recepcion(con_compra):
    nodo, _, proveedor, orden = con_compra

    def falla(conn, recepcion, deposito_id):
        raise RuntimeError("no se pudo encolar")

    with pytest.raises(RuntimeError, match="no se pudo encolar"):
        _recibir_en_el_nodo(nodo, proveedor, orden, al_recibir=falla)
    assert _saldo(nodo) == 10.0 and _filas(nodo, "SELECT id FROM purchase_receipts") == []
    assert float(_filas(nodo, "SELECT quantity_received FROM purchase_order_items")[0]["quantity_received"]) == 0.0
    # En tres pasos, con `al_recibir`, la confirmación entera es una transacción.
    with como_nodo(), nodo.abrir() as conn:
        recepcion = compras.crear_recepcion(conn, supplier_party_id=proveedor, purchase_order_id=orden)
        compras.agregar_linea_recepcion(conn, recepcion["id"], item_id=1, quantity=Decimal("6"), unit_cost=Decimal("250"))
        conn.commit()
        with pytest.raises(RuntimeError):
            compras.confirmar_recepcion(conn, recepcion["id"], location_id=1, occurred_at=datetime(2026, 10, 21, tzinfo=UTC),
                                        al_recibir=falla)
        conn.rollback()
    assert _saldo(nodo) == 10.0 and _filas(nodo, "SELECT status FROM purchase_receipts")[0]["status"] == "draft"


def test_la_orden_cancelada_o_inexistente_en_el_central_manda_la_compra_a_revision(con_compra):
    nodo, central, proveedor, orden = con_compra
    _recibir_en_el_nodo(nodo, proveedor, orden)
    (op,) = _pendientes(nodo)
    with central.abrir() as conn:
        compras.cancelar_orden(conn, orden)
    with como_central():
        (resultado,) = _subir(central, [op])
    assert resultado.result == "rejected" and "cancelada" in resultado.error
    assert _saldo(central) == 10.0 and _filas(central, "SELECT id FROM purchase_receipts") == []
    with como_central():
        (resultado,) = _subir(central, [replace(_con_payload(op, orden_id=999), operation_id="x-999")])
    assert resultado.result == "rejected" and _saldo(central) == 10.0
    with pytest.raises(offline_stock.CompraRechazadaPorElCentral):
        with como_central(), central.abrir() as conn:
            offline_stock.aplicar_compra_recibida(conn, op)


def test_la_compra_de_otra_sucursal_o_de_un_proveedor_que_no_existe_se_rechaza(con_compra):
    nodo, central, proveedor, orden = con_compra
    _recibir_en_el_nodo(nodo, proveedor, orden)
    (op,) = _pendientes(nodo)
    for cambio, texto in (({"deposito_id": 3}, "sucursal"), ({"proveedor_id": 999}, "proveedor")):
        with como_central():
            (resultado,) = _subir(central, [replace(_con_payload(op, **cambio), operation_id=f"x-{texto}")])
        assert resultado.result == "rejected" and texto in resultado.error, resultado.error
    assert _saldo(central) == 10.0


def test_fuera_de_un_nodo_la_recepcion_no_encola_y_corre_el_gancho_del_producto(con_compra):
    nodo, central, proveedor, orden = con_compra
    vistos = []
    with como_central(), central.abrir() as conn:
        compras.recibir_mercaderia(
            conn, supplier_party_id=proveedor, deposito_id=4, clave_operacion="k-9", occurred_at=datetime(2026, 10, 21, tzinfo=UTC),
            items=[{"item_id": 1, "quantity": 1, "unit_cost": 1}],
            al_recibir=offline_stock.recepcion_con_outbox(OUTBOX, lambda conn, r, d: vistos.append((r["id"], d))))
        conn.commit()
    assert _pendientes(nodo) == [] and _pendientes(central) == [] and vistos == [(1, 4)]


def test_router_de_compras_llama_a_al_recibir_una_vez_por_recepcion_nueva(abrir_vto):
    proveedor, producto, deposito = _proveedor(abrir_vto), _producto_vto(abrir_vto), _deposito(abrir_vto)
    vistos = []
    client = _app(abrir_vto, opciones=OpcionesCompras(al_recibir=lambda conn, r, d: vistos.append((r["id"], r["status"], d))))
    cuerpo = _cuerpo(proveedor, deposito, producto)
    assert _recibir(client, cuerpo).status_code == 200
    assert _recibir(client, cuerpo).json()["repetida"] is True          # un reintento no escribe: no vuelve a avisar
    assert vistos == [(1, "confirmed", deposito)]

    borrador = client.post("/api/purchase-receipts", json={"proveedor_id": proveedor}).json()
    client.post(f"/api/purchase-receipts/{borrador['id']}/items", json={"item_id": producto, "quantity": 3, "unit_cost": 10})
    assert client.post(f"/api/purchase-receipts/{borrador['id']}/confirm", json={"deposito_id": deposito}).status_code == 200
    assert vistos[1:] == [(borrador["id"], "confirmed", deposito)]


def _producto_vto(abrir):
    from libracommerce.erp import catalogo

    with abrir() as conn:
        return catalogo.create_producto(conn, "Yerba 1kg", precio_venta=1500.0, precio_costo=900.0)


def test_el_409_llega_hasta_el_router_de_compras_cuando_el_deposito_es_de_un_nodo(abrir_vto):
    proveedor, producto, deposito = _proveedor(abrir_vto), _producto_vto(abrir_vto), _deposito(abrir_vto)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    def guarda(conn, deposito_id):
        raise offline_stock.DepositoDeSucursalConNodo(deposito_id, "nodo-s02", "1")

    app = FastAPI()
    app.include_router(build_compras_router(conexion=abrir_vto, usuario_actual=lambda: USUARIO))
    offline_stock.instalar_manejador_409(app)
    ganchos_de_movimientos.configurar_antes_de_mover(guarda)
    try:
        respuesta = TestClient(app).post("/api/purchase-receipts/recibir", json=_cuerpo(proveedor, deposito, producto))
    finally:
        ganchos_de_movimientos.configurar_antes_de_mover(None)
    assert respuesta.status_code == 409 and respuesta.json()["codigo"] == "deposito_de_sucursal_con_nodo"
    with abrir_vto() as conn:
        assert conn.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM purchase_receipts").fetchone()[0] == 0


# ── La revisión 0008 ────────────────────────────────────────────────────────


def test_la_revision_0008_agrega_las_columnas_y_el_indice_sin_tocar_el_ledger(destino):
    _crear_base_al_dia_de_0001(destino)

    def poblar(conn):
        from libracommerce.erp import catalogo
        from libracommerce.erp import stock as _stock

        pid = catalogo.create_producto(conn, "Yerba", precio_venta=100.0, precio_costo=60.0)
        _stock.ajustar_stock(conn, pid, 40.0, "inicial", fecha="2026-09-01")
        conn.commit()
        assert not {"origen", "origen_id"} & _columnas_de(conn, "stock_movements")      # `init_schema()` sigue congelado
        return _ledger(conn)

    antes = _con_conexion(destino, poblar)
    migrar.upgrade(destino)

    def verificar(conn):
        assert {"origen", "origen_id"} <= _columnas_de(conn, "stock_movements")
        assert "idx_stock_movements_origen" in _indices_de(conn, "stock_movements")
        assert _ledger(conn) == antes, "la revisión modificó el ledger"
        assert conn.execute("SELECT COUNT(*) FROM stock_movements WHERE origen IS NOT NULL").fetchone()[0] == 0
        return [f[0] for f in conn.execute("SELECT version_num FROM alembic_version_libracommerce").fetchall()]

    assert _con_conexion(destino, verificar) == [_vto._CABEZA]
    migrar.upgrade(destino)                                    # idempotente
    assert _con_conexion(destino, verificar) == [_vto._CABEZA]


def test_el_indice_de_origen_es_unico_solo_sobre_las_filas_con_origen(destino):
    _crear_base_al_dia_de_0001(destino)
    migrar.upgrade(destino)

    def escribir(conn):
        from libracommerce.erp import catalogo

        pid = catalogo.create_producto(conn, "Yerba", precio_venta=100.0, precio_costo=60.0)
        conn.execute("INSERT INTO locations (name, is_default) VALUES ('D', 1)") if not conn.execute("SELECT 1 FROM locations").fetchone() else None
        loc = conn.execute("SELECT id FROM locations ORDER BY id").fetchone()[0]

        def fila(origen, origen_id):
            conn.execute(
                "INSERT INTO stock_movements (item_id, location_id, movement_type, quantity_delta, occurred_at, origen, origen_id) "
                "VALUES (?,?,?,?,?,?,?)", (pid, loc, "adjustment", 1, "2026-10-21T00:00:00", origen, origen_id))

        for _ in range(3):
            fila(None, None)                                   # las filas propias no compiten entre sí
        fila("nodo-a", 1)
        fila("nodo-b", 1)                                      # el mismo id local de otro nodo no choca
        fila("nodo-a", 2)
        conn.commit()
        with pytest.raises(Exception) as error:
            fila("nodo-a", 1)                                  # la misma fila dos veces: la base la rechaza
        assert "unique" in str(error.value).lower() or "UNIQUE" in str(error.value)
        conn.rollback()

    _con_conexion(destino, escribir)


def test_la_revision_0008_baja_sin_tocar_el_ledger_y_vuelve_a_subir(destino):
    from alembic import command

    _crear_base_al_dia_de_0001(destino)
    migrar.upgrade(destino)

    def con_stock(conn):
        from libracommerce.erp import catalogo
        from libracommerce.erp import stock as _stock

        pid = catalogo.create_producto(conn, "Yerba", precio_venta=100.0, precio_costo=60.0)
        _stock.ajustar_stock(conn, pid, 10.0, "inicial", fecha="2026-09-01")
        conn.commit()
        return _ledger(conn)

    antes = _con_conexion(destino, con_stock)
    command.downgrade(migrar.configuracion(destino), "0007_devoluciones_de_servicio")

    def verificar(conn):
        assert not {"origen", "origen_id"} & _columnas_de(conn, "stock_movements")
        assert "idx_stock_movements_origen" not in _indices_de(conn, "stock_movements")
        assert _ledger(conn) == antes, "bajar la revisión modificó el ledger"

    _con_conexion(destino, verificar)
    migrar.upgrade(destino)
    assert {"origen", "origen_id"} <= _con_conexion(destino, lambda conn: _columnas_de(conn, "stock_movements"))
