"""La sucursal única de una empresa de un solo local (ADR-063), contra los DOS motores.

Cuatro piezas que se prueban juntas porque son una sola decisión:

1. `registrar_venta` anota `sales.branch_id` con la sucursal del depósito del que sale la venta.
2. `catalogo.adoptar_sucursal_unica` crea la sucursal predeterminada y le asigna los depósitos huérfanos (idempotente y
   serializada entre procesos).
3. `OpcionesDepositos.sucursal_por_defecto`: el depósito que se crea sin sucursal cae en la predeterminada.
4. `catalogo.completar_sucursal_de_ventas`: las ventas de antes se completan con el criterio de `erp.margen`.

No hay migración de esquema: las columnas ya existían (`test_schema_congelado` no se mueve).
"""

from __future__ import annotations

import threading
import time

import pytest
from conftest import _usuario
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_erp_ventas import HOY, _producto, _venta

from libracommerce.erp import catalogo, margen, stock, ventas
from libracommerce.web.catalogo_router import OpcionesDepositos, build_depositos_router

ITEM_SUELTO = [{"nombre": "Suelto", "qty": 1, "precio": 100.0, "subtotal": 100.0, "producto_id": None}]
PAGO = [{"medio": "efectivo", "monto": 100.0, "estado": "aprobado"}]


def _vende(abrir, pid, *, deposito_id=None, **extra) -> int:
    """Una venta de 1 unidad de `pid` (descuenta del depósito pedido o del por defecto). Devuelve el id de la venta."""
    items = [{"nombre": "Yerba", "qty": 1, "precio": 100.0, "subtotal": 100.0, "producto_id": pid}]
    return _venta(abrir, items=items, pagos=PAGO, deposito_id=deposito_id, **extra)


def _branch_de(abrir, vid):
    with abrir() as conn:
        return conn.execute("SELECT branch_id FROM sales WHERE id = ?", (vid,)).fetchone()[0]


def _ramas(conn):
    return [tuple(f) for f in conn.execute(
        "SELECT id, name, is_default, default_location_id, active FROM branches ORDER BY id").fetchall()]


def _depositos(conn):
    return [tuple(f) for f in conn.execute("SELECT id, name, branch_id, active, is_default FROM locations ORDER BY id").fetchall()]


def _deposito(conn, nombre, *, branch_id=None, activo=True):
    did = catalogo.create_deposito(conn, nombre, branch_id=branch_id)
    if not activo:
        conn.execute("UPDATE locations SET active = 0 WHERE id = ?", (did,))
    return did


def _cajas(conn):
    return [tuple(f) for f in conn.execute("SELECT id, nombre, sucursal_id FROM cajas ORDER BY id").fetchall()]


def _principal(conn) -> int:
    return catalogo.get_default_deposito_id(conn)


# ── (1) La venta anota la sucursal de su depósito ────────────────────────


def test_una_empresa_sin_sucursales_vende_con_la_sucursal_en_null(abrir_ventas):
    with abrir_ventas() as conn:
        pid = _producto(conn)
    assert _branch_de(abrir_ventas, _vende(abrir_ventas, pid)) is None
    assert _branch_de(abrir_ventas, _venta(abrir_ventas)) is None          # ni siquiera mueve stock


def test_la_venta_nace_con_la_sucursal_de_su_deposito(abrir_ventas):
    with abrir_ventas() as conn:
        pid = _producto(conn, existencia=20)
        sid = catalogo.adoptar_sucursal_unica(conn)
        conn.commit()
    # Sin `deposito_id`: el depósito por defecto, que ahora es de la sucursal.
    assert _branch_de(abrir_ventas, _vende(abrir_ventas, pid)) == sid


def test_con_deposito_explicito_rige_la_sucursal_de_ese_deposito(abrir_ventas):
    with abrir_ventas() as conn:
        pid = _producto(conn, existencia=20)
        casa = catalogo.adoptar_sucursal_unica(conn)
        norte = catalogo.create_sucursal(conn, "Norte")
        dep_norte = catalogo.get_deposito_de_venta(conn, norte)
        sin_sucursal = _deposito(conn, "Suelto")
        conn.commit()
    assert _branch_de(abrir_ventas, _vende(abrir_ventas, pid, deposito_id=dep_norte)) == norte
    assert _branch_de(abrir_ventas, _vende(abrir_ventas, pid, deposito_id=sin_sucursal)) is None
    assert _branch_de(abrir_ventas, _vende(abrir_ventas, pid)) == casa


def test_sin_stock_habilitado_la_venta_igual_anota_la_sucursal_del_deposito(abrir_ventas):
    """La sucursal es del depósito de la venta, no del movimiento de stock: sin descuento de stock se anota igual."""
    with abrir_ventas() as conn:
        sid = catalogo.adoptar_sucursal_unica(conn)
        conn.commit()
    assert _branch_de(abrir_ventas, _venta(abrir_ventas, stock_habilitado=False)) == sid


def test_sin_ningun_deposito_la_sucursal_es_null_y_la_venta_se_registra(abrir_ventas):
    with abrir_ventas() as conn:
        conn.execute("DELETE FROM locations")
        conn.commit()
    assert _branch_de(abrir_ventas, _venta(abrir_ventas, stock_habilitado=False)) is None


def test_crear_venta_sin_sucursal_no_toca_la_columna(abrir_ventas):
    """`crear_venta(...)` sin `sucursal_id` es el `INSERT` de siempre; con uno, lo escribe."""
    with abrir_ventas() as conn:
        sid = catalogo.adoptar_sucursal_unica(conn)
        kw = dict(fecha=HOY, items=ITEM_SUELTO, subtotal=100.0, descuento=0.0, total=100.0, cliente_id=None,
                  cliente_nombre="", usuario_id=None)
        a = ventas.crear_venta(conn, numero="X-1", **kw)
        b = ventas.crear_venta(conn, numero="X-2", sucursal_id=sid, **kw)
        filas = dict(conn.execute("SELECT id, branch_id FROM sales WHERE id IN (?, ?)", (a, b)).fetchall())
        assert filas == {a: None, b: sid}


# ── (2) Adoptar la sucursal única ────────────────────────────────────────


def test_adoptar_en_una_base_vacia_crea_casa_central_con_el_deposito_por_defecto(abrir_ventas):
    with abrir_ventas() as conn:
        principal = _principal(conn)
        sid = catalogo.adoptar_sucursal_unica(conn)
        conn.commit()
        assert _ramas(conn) == [(sid, "Casa central", 1, principal, 1)]
        assert _depositos(conn) == [(principal, "Depósito principal", sid, 1, 1)]
        assert catalogo.get_default_sucursal_id(conn) == sid
        assert catalogo.get_deposito_de_venta(conn, sid) == principal


def test_adoptar_con_otro_nombre(abrir_ventas):
    with abrir_ventas() as conn:
        sid = catalogo.adoptar_sucursal_unica(conn, "Local Centro")
        assert catalogo.get_sucursal(conn, sid)["nombre"] == "Local Centro"
        with pytest.raises(ValueError, match="nombre"):
            catalogo.adoptar_sucursal_unica(conn, "   ")


def test_adoptar_asigna_todos_los_depositos_huerfanos_tambien_los_inactivos(abrir_ventas):
    with abrir_ventas() as conn:
        a = _deposito(conn, "Salón")
        b = _deposito(conn, "Viejo", activo=False)
        sid = catalogo.adoptar_sucursal_unica(conn)
        conn.commit()
        assert {d[0]: d[2] for d in _depositos(conn)} == {_principal(conn): sid, a: sid, b: sid}
        assert len(_ramas(conn)) == 1


def test_adoptar_con_una_predeterminada_no_crea_otra_y_solo_asigna_huerfanos(abrir_ventas):
    with abrir_ventas() as conn:
        matriz = catalogo.create_sucursal(conn, "Matriz")
        catalogo.set_default_sucursal(conn, matriz)
        dep_matriz = catalogo.get_deposito_de_venta(conn, matriz)
        otra = catalogo.create_sucursal(conn, "Otra")
        dep_otra = catalogo.get_deposito_de_venta(conn, otra)
        huerfano = _deposito(conn, "Suelto")
        antes = _ramas(conn)
        sid = catalogo.adoptar_sucursal_unica(conn)
        conn.commit()
        assert sid == matriz
        assert _ramas(conn) == antes                                 # ni una sucursal nueva, ni se toca la predeterminada
        asignados = {d[0]: d[2] for d in _depositos(conn)}
        assert asignados[huerfano] == matriz and asignados[_principal(conn)] == matriz
        assert asignados[dep_matriz] == matriz and asignados[dep_otra] == otra     # lo ajeno, intacto


def test_adoptar_dos_veces_es_idempotente(abrir_ventas):
    with abrir_ventas() as conn:
        _deposito(conn, "Salón")
        primero = catalogo.adoptar_sucursal_unica(conn)
        conn.commit()
        despues_de_uno = (_ramas(conn), _depositos(conn))
        segundo = catalogo.adoptar_sucursal_unica(conn)
        conn.commit()
        assert primero == segundo
        assert (_ramas(conn), _depositos(conn)) == despues_de_uno


def test_adoptar_sin_ningun_deposito_deja_el_predeterminado_vacio_y_el_primero_que_se_cree_lo_ocupa(abrir_ventas):
    with abrir_ventas() as conn:
        conn.execute("DELETE FROM locations")
        sid = catalogo.adoptar_sucursal_unica(conn)
        assert _ramas(conn) == [(sid, "Casa central", 1, None, 1)]
        nuevo = catalogo.create_deposito(conn, "Salón", branch_id=sid)
        assert catalogo.get_deposito_de_venta(conn, sid) == nuevo


def test_adoptar_con_sucursales_y_ninguna_predeterminada_se_niega_sin_escribir(abrir_ventas):
    with abrir_ventas() as conn:
        catalogo.create_sucursal(conn, "Norte")           # `create_sucursal` no la marca predeterminada
        conn.commit()
        antes = (_ramas(conn), _depositos(conn))
        with pytest.raises(ValueError, match="ninguna es la predeterminada"):
            catalogo.adoptar_sucursal_unica(conn)
        assert (_ramas(conn), _depositos(conn)) == antes


def test_adoptar_no_toca_las_cajas(abrir_ventas):
    """`cajas` es de libracore: la sucursal de la caja la asigna el producto (ContaLibra, su revisión de Alembic)."""
    with abrir_ventas() as conn:
        antes = _cajas(conn)
        catalogo.adoptar_sucursal_unica(conn)
        conn.commit()
        assert _cajas(conn) == antes and all(c[2] is None for c in antes)


def test_dos_procesos_a_la_vez_dejan_una_sola_sucursal(abrir_ventas, request):
    """El segundo espera al `commit` del primero (candado del esquema, ADR-062) y encuentra la sucursal creada: sin él
    chocaría con el índice único de la predeterminada (`idx_branches_one_default`)."""
    if "postgres" not in request.node.callspec.id:
        pytest.skip("SQLite serializa a sus escritores solo")
    primero = abrir_ventas()
    sid = catalogo.adoptar_sucursal_unica(primero)            # sin commit: tiene el candado
    resultado: dict = {}

    def segundo():
        conn = abrir_ventas()
        try:
            resultado["sid"] = catalogo.adoptar_sucursal_unica(conn)
            conn.commit()
        except Exception as e:  # noqa: BLE001 - el test necesita ver QUÉ falló
            resultado["error"] = f"{type(e).__name__}: {e}"
            conn.rollback()
        finally:
            conn.close()

    hilo = threading.Thread(target=segundo)
    hilo.start()
    time.sleep(1.5)
    assert hilo.is_alive(), "el segundo proceso no esperó al primero"
    primero.commit()
    primero.close()
    hilo.join(30)
    assert resultado == {"sid": sid}
    with abrir_ventas() as conn:
        assert len(_ramas(conn)) == 1


def test_muchos_procesos_a_la_vez_dejan_una_sola_sucursal_y_ningun_error(abrir_ventas, request):
    if "postgres" not in request.node.callspec.id:
        pytest.skip("SQLite serializa a sus escritores solo")
    hilos_n = 6
    salida: list = []
    largada = threading.Barrier(hilos_n)

    def uno():
        conn = abrir_ventas()
        try:
            largada.wait(10)
            salida.append(catalogo.adoptar_sucursal_unica(conn))
            conn.commit()
        except Exception as e:  # noqa: BLE001
            salida.append(f"{type(e).__name__}: {e}")
            conn.rollback()
        finally:
            conn.close()

    hilos = [threading.Thread(target=uno) for _ in range(hilos_n)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(60)
    assert len(set(salida)) == 1 and isinstance(salida[0], int), salida
    with abrir_ventas() as conn:
        assert [r[0] for r in _ramas(conn)] == [salida[0]]
        assert all(d[2] == salida[0] for d in _depositos(conn))


# ── (3) OpcionesDepositos.sucursal_por_defecto ───────────────────────────


def _client(abrir, **opciones) -> TestClient:
    app = FastAPI()
    app.include_router(build_depositos_router(conexion=abrir, usuario_actual=_usuario,
                                              opciones=OpcionesDepositos(**opciones)))
    return TestClient(app)


def _alta(client, nombre, **extra):
    r = client.post("/api/depositos", json={"nombre": nombre, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_sucursal_por_defecto_el_deposito_nuevo_cae_en_la_predeterminada(abrir_ventas):
    with abrir_ventas() as conn:
        sid = catalogo.adoptar_sucursal_unica(conn)
        conn.commit()
    client = _client(abrir_ventas, sucursal_por_defecto=True)
    creado = _alta(client, "Salón")
    assert creado["branch_id"] == sid
    with abrir_ventas() as conn:
        assert catalogo.get_sucursal(conn, sid)["deposito_predeterminado_id"] == _principal(conn)   # no pisa el de venta


def test_sucursal_por_defecto_respeta_la_sucursal_que_manda_el_pedido(abrir_ventas):
    with abrir_ventas() as conn:
        catalogo.adoptar_sucursal_unica(conn)
        norte = catalogo.create_sucursal(conn, "Norte")
        conn.commit()
    assert _alta(_client(abrir_ventas, sucursal_por_defecto=True), "Almacén", branch_id=norte)["branch_id"] == norte


def test_sucursal_por_defecto_sin_predeterminada_el_deposito_queda_sin_sucursal(abrir_ventas):
    creado = _alta(_client(abrir_ventas, sucursal_por_defecto=True), "Salón")
    assert creado["branch_id"] is None


def test_apagada_el_alta_es_la_de_siempre_aunque_haya_predeterminada(abrir_ventas):
    with abrir_ventas() as conn:
        catalogo.adoptar_sucursal_unica(conn)
        conn.commit()
    assert OpcionesDepositos().sucursal_por_defecto is False
    assert _alta(_client(abrir_ventas), "Salón")["branch_id"] is None


# ── (4) Completar las ventas históricas ──────────────────────────────────


def test_completar_las_ventas_de_antes_con_el_criterio_de_margen(abrir_ventas):
    """Ventas hechas SIN sucursales (branch_id NULL): con depósito (ledger) y sin él. Después de adoptar, las primeras
    se completan con la sucursal de su depósito, las segundas quedan; y lo que da `margen` antes es lo que queda escrito."""
    with abrir_ventas() as conn:
        pid = _producto(conn, existencia=50)
        extra = _deposito(conn, "Salón")
        stock.ajustar_stock(conn, pid, 10, "carga", usuario_id=1, fecha=HOY, deposito_id=extra)
        conn.commit()
    con_stock_1 = _vende(abrir_ventas, pid)
    con_stock_2 = _vende(abrir_ventas, pid, deposito_id=extra)
    sin_deposito = _venta(abrir_ventas)                                  # un ítem suelto: no mueve stock
    sin_stock = _venta(abrir_ventas, stock_habilitado=False)
    with abrir_ventas() as conn:
        assert all(f[0] is None for f in conn.execute("SELECT branch_id FROM sales").fetchall())
        deducida = margen._sucursal_de_las_ventas(conn, "", "")
        assert deducida == {con_stock_1: None, con_stock_2: None, sin_deposito: None, sin_stock: None}   # aún sin sucursales

        sid = catalogo.adoptar_sucursal_unica(conn)
        deducida = margen._sucursal_de_las_ventas(conn, "", "")
        assert deducida == {con_stock_1: sid, con_stock_2: sid, sin_deposito: None, sin_stock: None}

        assert catalogo.completar_sucursal_de_ventas(conn) == 2
        conn.commit()
        escrito = dict(conn.execute("SELECT id, branch_id FROM sales").fetchall())
        assert escrito == deducida                                       # lo escrito es lo que margen ya deducía
        assert catalogo.completar_sucursal_de_ventas(conn) == 0          # idempotente
        assert dict(conn.execute("SELECT id, branch_id FROM sales").fetchall()) == escrito


def test_completar_no_pisa_la_sucursal_que_la_venta_ya_trae(abrir_ventas):
    with abrir_ventas() as conn:
        pid = _producto(conn, existencia=20)
        casa = catalogo.adoptar_sucursal_unica(conn)
        norte = catalogo.create_sucursal(conn, "Norte")
        conn.commit()
    vid = _vende(abrir_ventas, pid)
    with abrir_ventas() as conn:
        conn.execute("UPDATE sales SET branch_id = ? WHERE id = ?", (norte, vid))     # declarada, aunque el stock salió de Casa central
        conn.commit()
        assert catalogo.completar_sucursal_de_ventas(conn) == 0
        assert conn.execute("SELECT branch_id FROM sales WHERE id = ?", (vid,)).fetchone()[0] == norte
        assert casa != norte


def test_completar_con_el_stock_en_depositos_de_varias_sucursales_toma_la_menor(abrir_ventas):
    """Mismo desempate que `margen`: si el ledger de una venta toca dos sucursales, la de menor id."""
    with abrir_ventas() as conn:
        pid = _producto(conn, existencia=20)
        casa = catalogo.adoptar_sucursal_unica(conn)
        norte = catalogo.create_sucursal(conn, "Norte")
        dep_norte = catalogo.get_deposito_de_venta(conn, norte)
        conn.commit()
    vid = _vende(abrir_ventas, pid)
    with abrir_ventas() as conn:
        conn.execute("UPDATE sales SET branch_id = NULL WHERE id = ?", (vid,))
        conn.execute(
            "INSERT INTO stock_movements (item_id, location_id, movement_type, quantity_delta, occurred_at, source_type, "
            "source_id) VALUES (?, ?, 'sale', -1, ?, 'venta', ?)", (pid, dep_norte, HOY, vid))
        deducida = margen._sucursal_de_las_ventas(conn, "", "")[vid]
        assert deducida == min(casa, norte)
        assert catalogo.completar_sucursal_de_ventas(conn) == 1
        assert conn.execute("SELECT branch_id FROM sales WHERE id = ?", (vid,)).fetchone()[0] == deducida


def test_completar_deja_en_null_la_venta_cuyo_deposito_no_tiene_sucursal(abrir_ventas):
    with abrir_ventas() as conn:
        pid = _producto(conn, existencia=20)
        catalogo.adoptar_sucursal_unica(conn)
        suelto = _deposito(conn, "Suelto")              # creado después de adoptar y sin la opción: huérfano
        stock.ajustar_stock(conn, pid, 5, "carga", usuario_id=1, fecha=HOY, deposito_id=suelto)
        conn.commit()
    vid = _vende(abrir_ventas, pid, deposito_id=suelto)
    with abrir_ventas() as conn:
        assert catalogo.completar_sucursal_de_ventas(conn) == 0
        assert conn.execute("SELECT branch_id FROM sales WHERE id = ?", (vid,)).fetchone()[0] is None


def test_completar_sin_ventas_ni_sucursales_no_hace_nada(abrir_ventas):
    with abrir_ventas() as conn:
        assert catalogo.completar_sucursal_de_ventas(conn) == 0
