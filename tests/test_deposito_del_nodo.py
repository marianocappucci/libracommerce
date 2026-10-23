"""El depósito por defecto de un nodo es el de SU sucursal (ADR-071, enmienda de v0.72.0).

El defecto que arregla (prueba real del 2026-10-22): un nodo vinculado a la sucursal 2 hizo un ajuste de stock en ContaLibra (que no usa
`OpcionesStock.por_deposito`, o sea sin `deposito_id`). El movimiento fue al depósito por defecto de la INSTANCIA —el espejo trae el
`locations.is_default` del central, el de «Casa central»— y el central lo rechazó a revisión manual (`DepositoFueraDeLaSucursal`). Lo mismo
pasaba con las ventas, la producción y cualquier movimiento sin depósito explícito hecho en un nodo que no está en la sucursal predeterminada.

Mismas dos bases que `test_erp_offline_stock.py` (el nodo y el central, en las tres combinaciones de motores): la sucursal 1 (depósitos 1 y 2), la
sucursal 2 (depósito 3) y el depósito 4 sin sucursal. El nodo de estos tests, `nodo-s03`, es el de la **sucursal 2**; la sucursal 1 es la predeterminada.
"""

from __future__ import annotations

import json

import pytest
import test_erp_offline_stock as _stock_offline
import test_vencimientos as _vto
from conftest import USUARIO
from test_erp_offline import _vender
from test_erp_offline_stock import (
    HOY,
    NODO_ID,
    OUTBOX,
    PAGO_EFECTIVO,
    _aceptar,
    _filas,
    _modo,
    _pendientes,
    _saldo,
    _tipos,
    como_central,
    offline_stock_sin_guarda,
)
from test_vencimientos import _con_conexion, _crear_base_al_dia_de_0001, _ledger, migrar

from libracommerce.erp import SIN_GANCHOS, catalogo, offline, offline_stock, stock

libraedge = pytest.importorskip("libraedge")
from libraedge.db.repository import NodeRepository  # noqa: E402

# Las fixtures de las dos bases (con el nodo registrado y los ganchos armados) y de la cadena de migraciones: se asignan para que pytest las vea.
puntas = _stock_offline.puntas
stock_puntas = _stock_offline.stock_puntas
destino = _vto.destino

NODO_S3 = offline.NodoLocal(node_id="nodo-s03", prefijo="S03")
SUCURSAL_DEL_NODO = 2            # la «Otra sucursal»: su depósito es el 3
DEPOSITO_DEL_NODO = 3
DEPOSITO_DE_LA_INSTANCIA = 1     # el `is_default` del espejo: el de Casa central


# ── Las bases: el nodo de la sucursal 2 ─────────────────────────────────────


def _sin_tabla(nodo):
    """Un nodo cuyo producto se actualizó pero todavía no corrió `libracommerce-migrar upgrade`: sin la `0009`."""
    with nodo.abrir() as conn:
        conn.execute("DROP TABLE nodo_local")
        conn.commit()


def _con_nodo_de_la_sucursal_2(nodo, central):
    """El central con `nodo-s03` registrado en la sucursal 2 (el nodo ya trae la `0009`, como todas las bases de estos tests)."""
    with central.abrir() as conn:
        NodeRepository(conn).register_node(NODO_S3.node_id, str(SUCURSAL_DEL_NODO))
        conn.commit()


def _como_nodo_s3():
    return _modo(NODO_S3)


def _traer(central, sucursal=str(SUCURSAL_DEL_NODO)):
    """Lo que hace LibraEdge: pedirle al central la carga (por JSON, como viaja) de la sucursal del nodo."""

    def traer():
        with central.abrir() as conn:
            return json.loads(json.dumps(offline_stock.carga_inicial_del_central(conn, NODO_S3.node_id, sucursal)))

    return traer


def _cargar(nodo, traer):
    with _como_nodo_s3(), nodo.abrir() as conn:
        resumen = offline_stock.aplicar_carga_inicial(conn, traer)
        conn.commit()
    return resumen


def _central_con_stock_en_la_sucursal_2(central, cantidad=10):
    with offline_stock_sin_guarda(), central.abrir() as conn:
        stock.add_movimiento_stock(conn, 1, "entrada", cantidad, "stock de la sucursal 2", fecha=HOY, deposito_id=DEPOSITO_DEL_NODO)
        conn.commit()


def _nodo_vinculado(nodo, central):
    """El estado después de vincular: el nodo con la `0009`, las existencias de su sucursal bajadas y su sucursal guardada."""
    _con_nodo_de_la_sucursal_2(nodo, central)
    _central_con_stock_en_la_sucursal_2(central)
    assert _cargar(nodo, _traer(central))["hecha"]


def _sucursal_guardada(base):
    with base.abrir() as conn:
        return offline_stock.sucursal_guardada_del_nodo(conn)


def _deposito_por_defecto(base, modo):
    with modo(), base.abrir() as conn:
        return catalogo.get_default_deposito_id(conn)


# ── El defecto: un ajuste sin depósito, en el nodo de la sucursal 2 ─────────


def test_un_ajuste_sin_deposito_en_el_nodo_de_la_sucursal_2_cae_en_su_deposito_y_el_central_lo_aplica(stock_puntas):
    """El caso de la prueba real. Antes caía en el depósito 1 (el de Casa central) y el central lo mandaba a revisión manual."""
    nodo, central = stock_puntas
    _nodo_vinculado(nodo, central)
    assert _sucursal_guardada(nodo) == SUCURSAL_DEL_NODO
    with _como_nodo_s3(), nodo.abrir() as conn:
        # Sin `deposito_id`, como lo hace ContaLibra: un conteo (`ajustar_stock`) y una entrada a mano.
        stock.ajustar_stock(conn, 1, 17.0, "recuento", usuario_id=USUARIO["id"], fecha=HOY)
        stock.add_movimiento_stock(conn, 1, "entrada", 2, "mercadería", fecha=HOY)
        conn.commit()
    movimientos = _filas(nodo, "SELECT location_id, movement_type, quantity_delta FROM stock_movements WHERE reason_code <> 'carga_inicial' "
                               "AND note IN ('recuento', 'mercadería') ORDER BY id")
    assert {m["location_id"] for m in movimientos} == {DEPOSITO_DEL_NODO}, movimientos
    # El depósito 1 (de Casa central) el nodo lo tenía en 10 de antes de vincularse: la carga lo llevó a cero, y el conteo (que fija el TOTAL del producto
    # en 17) y la entrada caen en el 3. En el central el 1 sigue en 10: su stock es del central.
    assert (_saldo(nodo, DEPOSITO_DE_LA_INSTANCIA), _saldo(nodo, DEPOSITO_DEL_NODO)) == (0.0, 19.0)

    operaciones = _pendientes(nodo)
    assert _tipos(operaciones) == ["stock.movimiento"] * 2
    assert [o.payload["deposito_id"] for o in operaciones] == [DEPOSITO_DEL_NODO] * 2
    _aceptar(central, operaciones)                      # el central NO lo manda a revisión (`DepositoFueraDeLaSucursal`)
    assert (_saldo(central, DEPOSITO_DE_LA_INSTANCIA), _saldo(central, DEPOSITO_DEL_NODO)) == (10.0, 19.0)


def test_una_venta_en_el_nodo_de_la_sucursal_2_descuenta_de_su_deposito_y_anota_su_sucursal(stock_puntas):
    nodo, central = stock_puntas
    _nodo_vinculado(nodo, central)
    hooks = offline.con_outbox(SIN_GANCHOS, offline.Outbox(nodo=lambda: NODO_S3, en_linea=lambda: False))
    with _como_nodo_s3():
        _vender(nodo, hooks, PAGO_EFECTIVO)
    assert (_saldo(nodo, DEPOSITO_DE_LA_INSTANCIA), _saldo(nodo, DEPOSITO_DEL_NODO)) == (0.0, 8.0)      # el 1, a cero por la carga (era de otra sucursal)
    (venta,) = _pendientes(nodo)
    assert venta.operation_type == "venta.registrada" and venta.payload["sucursal_id"] == SUCURSAL_DEL_NODO
    assert {m["deposito_id"] for m in venta.payload["stock"]} == {DEPOSITO_DEL_NODO}
    _aceptar(central, [venta])
    assert (_saldo(central, DEPOSITO_DE_LA_INSTANCIA), _saldo(central, DEPOSITO_DEL_NODO)) == (10.0, 8.0)


def test_sin_la_sucursal_guardada_el_nodo_sigue_con_el_deposito_de_la_instancia(stock_puntas):
    """Un nodo que todavía no hizo la carga (o instalado antes de este arreglo y sin deducir): el comportamiento de hoy, sin romper."""
    nodo, _ = stock_puntas
    assert _sucursal_guardada(nodo) is None
    assert _deposito_por_defecto(nodo, _como_nodo_s3) == DEPOSITO_DE_LA_INSTANCIA            # con la tabla, sin valor
    _sin_tabla(nodo)
    assert _deposito_por_defecto(nodo, _como_nodo_s3) == DEPOSITO_DE_LA_INSTANCIA            # sin la tabla


# ── Lo que no cambia ────────────────────────────────────────────────────────


def test_un_nodo_de_la_sucursal_predeterminada_cae_donde_siempre(stock_puntas):
    nodo, central = stock_puntas
    assert _cargar(nodo, _traer(central, "1"))["hecha"]
    assert _sucursal_guardada(nodo) == 1
    assert _deposito_por_defecto(nodo, _como_nodo_s3) == DEPOSITO_DE_LA_INSTANCIA
    with _como_nodo_s3(), nodo.abrir() as conn:
        stock.add_movimiento_stock(conn, 1, "entrada", 1, "sin depósito", fecha=HOY)
        conn.commit()
    assert _filas(nodo, "SELECT location_id FROM stock_movements WHERE note = 'sin depósito'") == [{"location_id": DEPOSITO_DE_LA_INSTANCIA}]


def test_el_deposito_predeterminado_de_la_sucursal_manda_sobre_el_de_menor_id(stock_puntas):
    nodo, central = stock_puntas
    assert _cargar(nodo, _traer(central, "1"))["hecha"]
    with nodo.abrir() as conn:
        catalogo.set_deposito_predeterminado(conn, 1, 2)
        conn.commit()
    assert _deposito_por_defecto(nodo, _como_nodo_s3) == 2


def test_fuera_de_un_nodo_el_deposito_por_defecto_es_el_de_la_instancia_aunque_la_base_guarde_una_sucursal(stock_puntas):
    nodo, central = stock_puntas
    _nodo_vinculado(nodo, central)
    assert _deposito_por_defecto(nodo, _como_nodo_s3) == DEPOSITO_DEL_NODO
    assert _deposito_por_defecto(nodo, como_central) == DEPOSITO_DE_LA_INSTANCIA              # `Outbox.nodo()` es None: no es un nodo
    assert _deposito_por_defecto(central, como_central) == DEPOSITO_DE_LA_INSTANCIA
    assert _deposito_por_defecto(central, _como_nodo_s3) == DEPOSITO_DE_LA_INSTANCIA          # un central no guardó sucursal


def test_sin_configurar_el_gancho_no_cambia_nada(stock_puntas):
    nodo, central = stock_puntas
    _nodo_vinculado(nodo, central)
    offline_stock.desconfigurar()
    try:
        assert _deposito_por_defecto(nodo, _como_nodo_s3) == DEPOSITO_DE_LA_INSTANCIA
    finally:
        offline_stock.configurar(OUTBOX)


def test_una_sucursal_sin_deposito_activo_cae_al_comportamiento_de_hoy(stock_puntas):
    nodo, central = stock_puntas
    _nodo_vinculado(nodo, central)
    with nodo.abrir() as conn:
        conn.execute("UPDATE locations SET active = 0 WHERE branch_id = ?", (SUCURSAL_DEL_NODO,))
        conn.commit()
        assert catalogo.deposito_activo_de_la_sucursal(conn, SUCURSAL_DEL_NODO) is None
    assert _deposito_por_defecto(nodo, _como_nodo_s3) == DEPOSITO_DE_LA_INSTANCIA


def test_el_deposito_de_la_sucursal_es_el_predeterminado_si_esta_activo_y_si_no_el_activo_de_menor_id(stock_puntas):
    nodo, _ = stock_puntas
    with nodo.abrir() as conn:
        otro = catalogo.create_deposito(conn, "Segundo", branch_id=SUCURSAL_DEL_NODO)
        assert catalogo.deposito_activo_de_la_sucursal(conn, SUCURSAL_DEL_NODO) == DEPOSITO_DEL_NODO
        conn.execute("UPDATE branches SET default_location_id = ? WHERE id = ?", (otro, SUCURSAL_DEL_NODO))
        assert catalogo.deposito_activo_de_la_sucursal(conn, SUCURSAL_DEL_NODO) == otro
        conn.execute("UPDATE locations SET active = 0 WHERE id = ?", (otro,))
        assert catalogo.deposito_activo_de_la_sucursal(conn, SUCURSAL_DEL_NODO) == DEPOSITO_DEL_NODO
        assert catalogo.deposito_activo_de_la_sucursal(conn, 77) is None
        conn.rollback()


# ── La carga inicial guarda la sucursal ─────────────────────────────────────


def test_la_carga_inicial_del_central_informa_la_sucursal_del_nodo(stock_puntas):
    _, central = stock_puntas
    with central.abrir() as conn:
        carga = offline_stock.carga_inicial_del_central(conn, NODO_ID, "2")
        assert carga["version"] == 1 and carga["sucursal_id"] == 2 and isinstance(carga["sucursal_id"], int)
        assert offline_stock.carga_inicial_del_central(conn, NODO_ID, "77")["sucursal_id"] == 77      # una sucursal sin depósitos también
    json.dumps(carga)


def test_la_carga_inicial_guarda_la_sucursal_en_la_misma_transaccion_que_los_ajustes(stock_puntas):
    nodo, central = stock_puntas
    _con_nodo_de_la_sucursal_2(nodo, central)
    _central_con_stock_en_la_sucursal_2(central)
    with _como_nodo_s3(), nodo.abrir() as conn:
        resumen = offline_stock.aplicar_carga_inicial(conn, _traer(central))
        assert resumen == {"hecha": True, "motivo": None, "ajustes": 2, "ajustes_ajenos": 1, "buckets": 1}      # el depósito 3 y el 1 (ajeno) a cero
        conn.rollback()                                  # el worker hace rollback si no confirma: ni los ajustes ni la sucursal quedan
    assert _sucursal_guardada(nodo) is None and _filas(nodo, "SELECT id FROM stock_movements WHERE reason_code = 'carga_inicial'") == []
    assert _cargar(nodo, _traer(central))["ajustes"] == 2
    assert _sucursal_guardada(nodo) == SUCURSAL_DEL_NODO


def test_volver_a_aplicar_la_carga_guarda_la_sucursal_sin_mover_stock(stock_puntas):
    """Es lo que hace `libraedge-nodo carga-inicial --forzar` en un nodo que ya había hecho su carga antes de este arreglo."""
    nodo, central = stock_puntas
    _nodo_vinculado(nodo, central)
    with nodo.abrir() as conn:
        conn.execute("DELETE FROM nodo_local")           # un nodo de antes: hizo la carga y no guardó nada
        conn.commit()
    assert _sucursal_guardada(nodo) is None
    ledger = _filas(nodo, "SELECT * FROM stock_movements ORDER BY id")
    assert _cargar(nodo, _traer(central)) == {"hecha": True, "motivo": None, "ajustes": 0, "ajustes_ajenos": 0, "buckets": 1}
    assert _sucursal_guardada(nodo) == SUCURSAL_DEL_NODO
    assert _filas(nodo, "SELECT * FROM stock_movements ORDER BY id") == ledger
    assert _cargar(nodo, _traer(central))["ajustes"] == 0                                    # idempotente
    assert len(_filas(nodo, "SELECT clave FROM nodo_local")) == 1


def test_si_el_nodo_cambia_de_sucursal_la_nueva_carga_pisa_la_guardada(stock_puntas):
    nodo, central = stock_puntas
    assert _cargar(nodo, _traer(central, "1"))["hecha"] and _sucursal_guardada(nodo) == 1
    assert _cargar(nodo, _traer(central, "2"))["hecha"] and _sucursal_guardada(nodo) == 2


def test_la_carga_que_espera_no_guarda_la_sucursal(stock_puntas):
    nodo, central = stock_puntas
    _con_nodo_de_la_sucursal_2(nodo, central)
    with _como_nodo_s3():
        _vender(nodo, offline.con_outbox(SIN_GANCHOS, offline.Outbox(nodo=lambda: NODO_S3, en_linea=lambda: False)), PAGO_EFECTIVO)
    assert _cargar(nodo, _traer(central))["motivo"] == "operaciones_pendientes"
    assert _sucursal_guardada(nodo) is None


def test_un_central_anterior_no_manda_la_sucursal_y_el_nodo_no_guarda_nada_ni_falla(stock_puntas):
    nodo, central = stock_puntas
    _con_nodo_de_la_sucursal_2(nodo, central)

    def de_un_central_viejo():
        carga = _traer(central)()
        del carga["sucursal_id"]
        return carga

    assert _cargar(nodo, de_un_central_viejo)["hecha"]
    assert _sucursal_guardada(nodo) is None
    assert _deposito_por_defecto(nodo, _como_nodo_s3) == DEPOSITO_DE_LA_INSTANCIA


def test_sin_la_revision_0009_la_carga_pide_migrar_y_no_escribe_nada(stock_puntas):
    nodo, central = stock_puntas
    _sin_tabla(nodo)
    _central_con_stock_en_la_sucursal_2(central)
    with _como_nodo_s3(), nodo.abrir() as conn:
        with pytest.raises(offline_stock.NodoSinRevision, match="0009_sucursal_del_nodo.*libracommerce-migrar"):
            offline_stock.aplicar_carga_inicial(conn, _traer(central))
        conn.rollback()
    assert _filas(nodo, "SELECT id FROM stock_movements WHERE reason_code = 'carga_inicial'") == []
    assert issubclass(offline_stock.NodoSinRevision, ValueError)


def test_la_sucursal_guardada_que_no_es_un_id_se_ignora(stock_puntas):
    nodo, _ = stock_puntas
    with nodo.abrir() as conn:
        conn.execute("INSERT INTO nodo_local (clave, valor) VALUES ('sucursal_id', 'S02')")
        conn.commit()
    assert _sucursal_guardada(nodo) is None
    assert _deposito_por_defecto(nodo, _como_nodo_s3) == DEPOSITO_DE_LA_INSTANCIA


# ── `libraedge-nodo carga-inicial --forzar` ─────────────────────────────────


def test_carga_inicial_forzar_con_el_worker_de_libraedge_deja_la_sucursal_guardada(stock_puntas):
    """El worker de verdad (libraedge >= 0.20): la carga hecha y marcada, un nodo de antes sin sucursal, y `--forzar` (`run_once(forzar=True)`)."""
    carga_inicial = pytest.importorskip("libraedge.sync.carga_inicial")
    nodo, central = stock_puntas
    _nodo_vinculado(nodo, central)
    with nodo.abrir() as conn:
        conn.execute("DELETE FROM nodo_local")
        conn.commit()

    class Transporte:
        traer = staticmethod(_traer(central))

    with _como_nodo_s3(), nodo.abrir() as conn:
        trabajador = carga_inicial.CargaInicialWorker(
            NodeRepository(conn), conn, NODO_S3.node_id, Transporte(), "libracommerce.erp.offline_stock:aplicar_carga_inicial")
        conn.commit()
        assert trabajador.run_once().estado == carga_inicial.HECHA            # la primera vez (sin marca) la hace y la marca
        conn.execute("DELETE FROM nodo_local")
        conn.commit()
        assert trabajador.run_once().estado == carga_inicial.YA_HECHA         # sin --forzar no vuelve a correr...
        assert offline_stock.sucursal_guardada_del_nodo(conn) is None
        ledger = _ledger(conn)
        resultado = trabajador.run_once(forzar=True)                          # ...con --forzar sí
        assert resultado.estado == carga_inicial.HECHA and resultado.resultado["ajustes"] == 0
        assert offline_stock.sucursal_guardada_del_nodo(conn) == SUCURSAL_DEL_NODO
        assert _ledger(conn) == ledger


# ── La revisión 0009 ────────────────────────────────────────────────────────


def _nodo_de_antes(conn, sucursales_de_la_carga, con_ajuste_propio=True):
    """La base de un nodo instalado antes de este arreglo: sucursales, depósitos y filas `carga_inicial` en las sucursales dadas."""
    from libracommerce.erp import catalogo as _catalogo
    from libracommerce.erp import stock as _stock

    pid = _catalogo.create_producto(conn, "Yerba", precio_venta=100.0, precio_costo=60.0)
    conn.execute("INSERT INTO branches (name, is_default) VALUES ('Casa central', 1)")
    conn.execute("INSERT INTO branches (name) VALUES ('Sucursal 2')")
    conn.execute("UPDATE locations SET branch_id = 1")
    _catalogo.create_deposito(conn, "Depósito 2", branch_id=2)
    for deposito in sucursales_de_la_carga:
        conn.execute(
            "INSERT INTO stock_movements (item_id, location_id, movement_type, quantity_delta, occurred_at, reason_code) "
            "VALUES (?, ?, 'adjustment', 5, '2026-10-21T00:00:00', 'carga_inicial')", (pid, deposito))
    if con_ajuste_propio:
        _stock.add_movimiento_stock(conn, pid, "entrada", 3, "propio", fecha="2026-10-21", deposito_id=1)
    conn.commit()
    return _ledger(conn)


def _nodo_local(conn):
    return [tuple(f) for f in conn.execute("SELECT clave, valor FROM nodo_local ORDER BY clave").fetchall()]


def test_la_revision_0009_crea_la_tabla_y_deduce_la_sucursal_de_la_carga_inicial_sin_tocar_el_ledger(destino):
    _crear_base_al_dia_de_0001(destino)
    antes = _con_conexion(destino, lambda conn: _nodo_de_antes(conn, [2]))           # la carga inicial cayó en el depósito de la sucursal 2

    def sin_la_tabla(conn):
        assert not _vto._columnas_de(conn, "nodo_local")                             # `init_schema()` sigue congelado

    _con_conexion(destino, sin_la_tabla)
    migrar.upgrade(destino)

    def verificar(conn):
        assert _vto._columnas_de(conn, "nodo_local") == {"clave", "valor"}
        assert _nodo_local(conn) == [("sucursal_id", "2")]
        assert offline_stock.sucursal_guardada_del_nodo(conn) == 2
        assert _ledger(conn) == antes, "la revisión modificó el ledger"
        return [f[0] for f in conn.execute("SELECT version_num FROM alembic_version_libracommerce").fetchall()]

    assert _con_conexion(destino, verificar) == [_vto._CABEZA]
    migrar.upgrade(destino)                                                          # idempotente
    assert _con_conexion(destino, verificar) == [_vto._CABEZA]


def test_la_revision_0009_no_pisa_una_sucursal_ya_guardada(destino):
    _crear_base_al_dia_de_0001(destino)
    _con_conexion(destino, lambda conn: _nodo_de_antes(conn, [2]))
    migrar.upgrade(destino)

    def cambiar(conn):
        conn.execute("UPDATE nodo_local SET valor = '1'")
        conn.commit()

    _con_conexion(destino, cambiar)
    migrar.upgrade(destino)
    assert _con_conexion(destino, _nodo_local) == [("sucursal_id", "1")]


@pytest.mark.parametrize("deposito_de_la_carga", [[], [1, 2]], ids=["sin-carga-inicial", "carga-en-dos-sucursales"])
def test_la_revision_0009_no_deduce_si_no_hay_una_sola_sucursal(destino, deposito_de_la_carga):
    """Sin filas `carga_inicial` (un central, o un nodo cuya sucursal no tenía stock) o con filas de dos sucursales no hay con qué deducir: la tabla queda vacía."""
    _crear_base_al_dia_de_0001(destino)
    _con_conexion(destino, lambda conn: _nodo_de_antes(conn, deposito_de_la_carga))
    migrar.upgrade(destino)
    assert _con_conexion(destino, _nodo_local) == []


def test_la_revision_0009_ignora_las_cargas_en_depositos_sin_sucursal(destino):
    _crear_base_al_dia_de_0001(destino)

    def poblar(conn):
        ledger = _nodo_de_antes(conn, [], con_ajuste_propio=False)
        conn.execute("INSERT INTO locations (name, is_default) VALUES ('Sin sucursal', 0)")
        sin = conn.execute("SELECT id FROM locations WHERE name = 'Sin sucursal'").fetchone()[0]
        conn.execute(
            "INSERT INTO stock_movements (item_id, location_id, movement_type, quantity_delta, occurred_at, reason_code) "
            "VALUES (1, ?, 'adjustment', 5, '2026-10-21T00:00:00', 'carga_inicial')", (sin,))
        conn.commit()
        return ledger

    _con_conexion(destino, poblar)
    migrar.upgrade(destino)
    assert _con_conexion(destino, _nodo_local) == []


def test_la_revision_0009_baja_sin_tocar_el_ledger_y_vuelve_a_subir(destino):
    from alembic import command

    _crear_base_al_dia_de_0001(destino)
    antes = _con_conexion(destino, lambda conn: _nodo_de_antes(conn, [2]))
    migrar.upgrade(destino)
    command.downgrade(migrar.configuracion(destino), "0008_origen_del_stock")

    def verificar(conn):
        assert not _vto._columnas_de(conn, "nodo_local")
        assert offline_stock.sucursal_guardada_del_nodo(conn) is None                # sin la tabla el nodo vuelve al defecto de antes
        assert _ledger(conn) == antes, "bajar la revisión modificó el ledger"

    _con_conexion(destino, verificar)
    migrar.upgrade(destino)
    assert _con_conexion(destino, _nodo_local) == [("sucursal_id", "2")]             # al subir de nuevo deduce otra vez
