"""`update_producto` guarda el producto y reemplaza su código principal en UNA transacción (ADR-028).

Antes confirmaba el producto en `save_catalog_item` (soltando el candado) y recién después reemplazaba el código: otro pedido podía pisarlo en el medio (ver
`test_carga_vencimientos.py`) y un código repetido dejaba guardados los demás campos. Acá se miden las tres cosas que importan de la transacción, contra los dos motores:
(1) `repo.transaction()` existe en las dos implementaciones del repositorio (`SqliteCommerceRepository`, que sirve a SQLite y a PostgreSQL, y `RepositorioAuditado`) y
dentro de ella ninguna escritura confirma; (2) con el repositorio que AUDITA (el de VentaLibra: `usar_fabrica_de_repositorio` con un `RepositorioAuditado`) la auditoría
queda dentro de la transacción y se revierte cuando falla el segundo paso; (3) varias ediciones a la vez no se esperan a sí mismas ni se cruzan.
"""

from __future__ import annotations

import threading

import pytest
import test_vencimientos as _vto

from libracommerce.db.auditoria import RepositorioAuditado
from libracommerce.db.repository import SqliteCommerceRepository, repositorio_de, usar_fabrica_de_repositorio
from libracommerce.domain.catalog import ItemCode, ItemCodeType
from libracommerce.erp import catalogo


class _Cuenta:
    """Una conexión que cuenta los `commit()` y delega el resto."""

    def __init__(self, conn):
        self._conn = conn
        self.commits = 0

    def commit(self):
        self.commits += 1
        return self._conn.commit()

    def __getattr__(self, nombre):
        return getattr(self._conn, nombre)


# Las fixtures de `tests/test_vencimientos.py` (una base por motor con las revisiones aplicadas).
abrir_vto = _vto.abrir_vto
destino = _vto.destino

_LOG = ("CREATE TABLE IF NOT EXISTS actividad_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, usuario TEXT NOT NULL DEFAULT 'Sistema', "
        "accion TEXT NOT NULL, entidad TEXT NOT NULL, entidad_id INTEGER, descripcion TEXT NOT NULL DEFAULT '', cambios TEXT)")


def _log(abrir) -> list[tuple]:
    with abrir() as conn:
        return [tuple(f) for f in conn.execute("SELECT accion, entidad, descripcion FROM actividad_log ORDER BY id").fetchall()]


@pytest.fixture
def con_auditoria(abrir_vto):
    """La fábrica de repositorio de un producto que audita (como VentaLibra) y la tabla `actividad_log`."""
    with abrir_vto() as conn:
        conn.execute(_LOG)
        conn.commit()
    usar_fabrica_de_repositorio(lambda conn: RepositorioAuditado(SqliteCommerceRepository(conn), conn, usuario=lambda: "ana"))
    try:
        yield abrir_vto
    finally:
        usar_fabrica_de_repositorio(None)


def _editar(abrir, pid, codigo, precio=11.0, nombre="Yerba"):
    with abrir() as conn:
        catalogo.update_producto(conn, pid=pid, nombre=nombre, codigo=codigo, descripcion="", precio_venta=precio, precio_costo=5, unidad="u", categoria="", activo=1)


def _foto(abrir, pid):
    with abrir() as conn:
        p = catalogo.get_producto(conn, pid)
        return p["nombre"], p["codigo"], p["precio_venta"]


# ═══════════════════════════════════════════════════ (1) transaction() en las dos implementaciones


@pytest.mark.parametrize("envolver", [lambda repo, conn: repo, lambda repo, conn: RepositorioAuditado(repo, conn)], ids=["SqliteCommerceRepository", "RepositorioAuditado"])
def test_dentro_de_la_transaccion_ninguna_escritura_confirma_y_al_salir_confirma_una_vez(abrir_vto, envolver):
    with abrir_vto() as real:
        real.execute(_LOG)
        real.commit()
        pid = catalogo.create_producto(real, nombre="Yerba", codigo="111", precio_venta=10, precio_costo=5)
        conn = _Cuenta(real)
        repo = envolver(SqliteCommerceRepository(conn), conn)
        item = repo.get_catalog_item(pid)
        antes = conn.commits
        with repo.transaction():
            repo.save_catalog_item(item)
            repo.save_item_code(ItemCode(None, pid, ItemCodeType.BARCODE, "777"))
            assert conn.commits == antes, "una escritura confirmó adentro de transaction()"
        assert conn.commits == antes + 1


def test_un_error_adentro_de_la_transaccion_la_revierte_con_la_auditoria(con_auditoria):
    abrir = con_auditoria
    with abrir() as conn:
        pid = catalogo.create_producto(conn, nombre="Yerba", codigo="111", precio_venta=10, precio_costo=5)
    antes = _log(abrir)
    with abrir() as conn, pytest.raises(RuntimeError, match="boom"):
        repo = repositorio_de(conn)
        with repo.transaction():
            repo.save_item_code(ItemCode(None, pid, ItemCodeType.BARCODE, "777"))
            raise RuntimeError("boom")
    assert _log(abrir) == antes
    with abrir() as conn:
        assert [c["codigo"] for c in catalogo.get_codigos(conn, pid)] == ["111"]


# ═══════════════════════════════════════════════════ El código repetido: ahora no guarda nada


def test_un_codigo_repetido_no_guarda_ningun_campo(abrir_vto):
    abrir = abrir_vto
    with abrir() as conn:
        pid = catalogo.create_producto(conn, nombre="Yerba", codigo="111", precio_venta=10, precio_costo=5)
        catalogo.create_producto(conn, nombre="Leche", codigo="DUP", precio_venta=10, precio_costo=5)
    with pytest.raises(Exception):  # noqa: B017, PT011 - IntegrityError de uno u otro motor
        _editar(abrir, pid, "DUP", precio=99.0, nombre="Yerba editada")
    assert _foto(abrir, pid) == ("Yerba", "111", 10.0)


def test_un_codigo_repetido_con_una_categoria_nueva_tampoco_deja_la_categoria(abrir_vto):
    abrir = abrir_vto
    with abrir() as conn:
        pid = catalogo.create_producto(conn, nombre="Yerba", codigo="111", precio_venta=10, precio_costo=5)
        catalogo.create_producto(conn, nombre="Leche", codigo="DUP", precio_venta=10, precio_costo=5)
    with pytest.raises(Exception):  # noqa: B017, PT011
        with abrir() as conn:
            catalogo.update_producto(conn, pid=pid, nombre="Yerba", codigo="DUP", descripcion="", precio_venta=10, precio_costo=5, unidad="u", categoria="Nueva", activo=1)
    with abrir() as conn:
        assert conn.execute("SELECT COUNT(*) FROM categories WHERE name='Nueva'").fetchone()[0] == 0


# ═══════════════════════════════════════════════════ (2) con el repositorio que audita


def test_con_auditoria_una_edicion_buena_registra_el_producto_y_el_codigo(con_auditoria):
    abrir = con_auditoria
    with abrir() as conn:
        pid = catalogo.create_producto(conn, nombre="Yerba", codigo="111", precio_venta=10, precio_costo=5)
    antes = len(_log(abrir))
    _editar(abrir, pid, "222")
    nuevas = _log(abrir)[antes:]
    assert [(a, e) for a, e, _ in nuevas] == [("editar", "producto"), ("crear", "codigo")]
    assert _foto(abrir, pid)[1:] == ("222", 11.0)


def test_con_auditoria_un_codigo_repetido_no_deja_ni_el_producto_ni_su_auditoria(con_auditoria):
    abrir = con_auditoria
    with abrir() as conn:
        pid = catalogo.create_producto(conn, nombre="Yerba", codigo="111", precio_venta=10, precio_costo=5)
        catalogo.create_producto(conn, nombre="Leche", codigo="DUP", precio_venta=10, precio_costo=5)
    log_antes = _log(abrir)
    with pytest.raises(Exception):  # noqa: B017, PT011
        _editar(abrir, pid, "DUP", precio=99.0, nombre="Yerba editada")
    assert _foto(abrir, pid) == ("Yerba", "111", 10.0)
    assert _log(abrir) == log_antes, "quedó registrada una edición que se deshizo"


# ═══════════════════════════════════════════════════ (3) ediciones a la vez


def test_ediciones_simultaneas_del_mismo_producto_no_se_cruzan_ni_se_esperan_a_si_mismas(abrir_vto):
    """Cuatro hilos editan el MISMO producto a la vez (barrera): cada uno pone su código y su precio (`C<i>`, 100+i). Todos terminan (sin deadlock ni espera sobre sí mismo) y el
    producto queda con el par de UNO solo: nunca el código de uno y el precio de otro."""
    abrir = abrir_vto
    with abrir() as conn:
        pid = catalogo.create_producto(conn, nombre="Yerba", codigo="C0", precio_venta=10, precio_costo=5)
    barrera = threading.Barrier(4)
    errores: list[str] = []

    def escribir(i):
        try:
            barrera.wait(timeout=30)
            _editar(abrir, pid, f"C{i}", precio=100.0 + i)
        except Exception as exc:  # noqa: BLE001
            errores.append(repr(exc))

    hilos = [threading.Thread(target=escribir, args=(i,)) for i in range(1, 5)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=60)
    assert not any(h.is_alive() for h in hilos), "un hilo quedó esperando (deadlock)"
    assert not errores, errores
    _, codigo, precio = _foto(abrir, pid)
    assert precio == 100.0 + int(codigo[1:]), (codigo, precio)


def test_dos_productos_que_se_intercambian_los_codigos_no_se_traban(abrir_vto):
    """Dos hilos, dos productos, cada uno quiere el código del otro (a la vez). Uno gana y el otro falla por repetido; ninguno se queda esperando al otro para siempre."""
    abrir = abrir_vto
    with abrir() as conn:
        a = catalogo.create_producto(conn, nombre="A", codigo="CA", precio_venta=10, precio_costo=5)
        b = catalogo.create_producto(conn, nombre="B", codigo="CB", precio_venta=10, precio_costo=5)
    barrera = threading.Barrier(2)
    resultado: dict[str, str] = {}

    def cambiar(nombre, pid, codigo):
        try:
            barrera.wait(timeout=30)
            _editar(abrir, pid, codigo, nombre=nombre)
            resultado[nombre] = "ok"
        except Exception as exc:  # noqa: BLE001
            resultado[nombre] = type(exc).__name__

    hilos = [threading.Thread(target=cambiar, args=("A", a, "CB")), threading.Thread(target=cambiar, args=("B", b, "CA"))]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=60)
    assert not any(h.is_alive() for h in hilos), "deadlock"
    assert sorted(resultado) == ["A", "B"] and "ok" not in resultado.values(), resultado   # los dos fallan: cada código sigue siendo del otro mientras el otro no confirma
