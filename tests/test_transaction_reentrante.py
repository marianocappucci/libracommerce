"""`repo.transaction()` es reentrante (ADR-031): la exterior es dueña del commit, cada interior es un SAVEPOINT.

Antes (ADR-028) `transaction()` guardaba su estado en el repositorio (`_en_transaccion`), y `repositorio_de(conn)` arma uno NUEVO en cada llamada. Dos consecuencias, las dos medidas
en este archivo contra los dos motores: (1) el MISMO repositorio anidado levantaba `RuntimeError`; (2) un producto que abría su `transaction()` sobre un repositorio y llamaba
`update_producto`/`create_producto` (que arman el suyo sobre la misma conexión) no recibía error: el de adentro **confirmaba** al salir, o sea todo lo que la exterior llevaba
escrito, y la exterior ya no podía deshacerlo. Ahora la profundidad vive en la CONEXIÓN, la exterior confirma o revierte, y una interior que falla y se atrapa revierte sólo lo suyo.
"""

from __future__ import annotations

import threading

import pytest
import test_update_producto_atomico as _atomico
import test_vencimientos as _vto

from libracommerce.db import repository as _repository
from libracommerce.db.auditoria import RepositorioAuditado
from libracommerce.db.repository import SqliteCommerceRepository, repositorio_de
from libracommerce.domain.catalog import ItemCode, ItemCodeType
from libracommerce.erp import catalogo

# Las fixtures de `tests/test_vencimientos.py` y de `tests/test_update_producto_atomico.py` (una base por motor con las revisiones aplicadas; la misma con el repositorio que audita).
abrir_vto = _vto.abrir_vto
destino = _vto.destino
con_auditoria = _atomico.con_auditoria
_Cuenta = _atomico._Cuenta
_log = _atomico._log
_LOG = _atomico._LOG


def _upd(conn, pid, codigo, precio=11.0, nombre="Yerba"):
    catalogo.update_producto(conn, pid=pid, nombre=nombre, codigo=codigo, descripcion="", precio_venta=precio, precio_costo=5, unidad="u", categoria="", activo=1)


def _crear(conn, nombre, codigo):
    return catalogo.create_producto(conn, nombre=nombre, codigo=codigo, precio_venta=10, precio_costo=5)


def _fotos(abrir, *pids):
    """`[(nombre, código principal, precio)]` de cada producto, visto desde una conexión APARTE (sólo ve lo confirmado)."""
    with abrir() as conn:
        return [(p["nombre"], p["codigo"], p["precio_venta"]) for p in (catalogo.get_producto(conn, pid) for pid in pids)]


def _codigos(abrir, pid):
    with abrir() as conn:
        return [c["codigo"] for c in catalogo.get_codigos(conn, pid)]


def _barcode(repo, pid, codigo):
    repo.save_item_code(ItemCode(None, pid, ItemCodeType.BARCODE, codigo))


# ═══════════════════════════════════════════════════ El mismo repositorio, anidado


@pytest.mark.parametrize("envolver", [lambda repo, conn: repo, lambda repo, conn: RepositorioAuditado(repo, conn)], ids=["SqliteCommerceRepository", "RepositorioAuditado"])
def test_anidar_transaction_en_el_mismo_repositorio_no_levanta_y_confirma_una_vez(abrir_vto, envolver):
    with abrir_vto() as real:
        real.execute(_LOG)
        real.commit()
        pid = _crear(real, "Yerba", "111")
        conn = _Cuenta(real)
        repo = envolver(SqliteCommerceRepository(conn), conn)
        item = repo.get_catalog_item(pid)
        antes = conn.commits
        with repo.transaction():
            repo.save_catalog_item(item)
            with repo.transaction():
                repo.save_item_code(ItemCode(None, pid, ItemCodeType.BARCODE, "777"))
                assert conn.commits == antes, "una interior confirmó"
            assert conn.commits == antes, "salir de la interior confirmó"
            repo.save_item_code(ItemCode(None, pid, ItemCodeType.BARCODE, "888"))
        assert conn.commits == antes + 1, "la exterior tenía que confirmar exactamente una vez"
    assert sorted(_codigos(abrir_vto, pid)) == ["111", "777", "888"]


def test_otra_instancia_del_repositorio_sobre_la_misma_conexion_anida_y_no_confirma(abrir_vto):
    """Lo que `repositorio_de(conn)` hace en cada llamada: el estado es de la conexión, no del objeto."""
    with abrir_vto() as real:
        pid = _crear(real, "Yerba", "111")
        conn = _Cuenta(real)
        antes = conn.commits
        with SqliteCommerceRepository(conn).transaction():
            with SqliteCommerceRepository(conn).transaction() as interior:
                _barcode(interior, pid, "777")
            assert conn.commits == antes, "la transacción de OTRA instancia confirmó lo de la exterior"
            _barcode(SqliteCommerceRepository(conn), pid, "888")   # una escritura suelta de una tercera instancia tampoco confirma adentro
            assert conn.commits == antes
        assert conn.commits == antes + 1


# ═══════════════════════════════════════════════════ (a) update_producto y create_producto adentro de una transacción exterior


def test_update_y_create_producto_dentro_de_una_transaccion_exterior_no_levantan_y_confirman_al_salir(abrir_vto):
    abrir = abrir_vto
    with abrir() as conn:
        pid = _crear(conn, "Yerba", "111")
    with abrir() as conn:
        repo = repositorio_de(conn)
        with repo.transaction():
            _upd(conn, pid, "222", precio=21.0, nombre="Yerba editada")
            nuevo = _crear(conn, "Leche", "LCH")
            # Nada se confirmó todavía: otra conexión sigue viendo lo de antes.
            assert _fotos(abrir, pid) == [("Yerba", "111", 10.0)], "el update_producto de adentro confirmó solo"
            with abrir() as otra:
                assert otra.execute("SELECT COUNT(*) FROM catalog_items WHERE name='Leche'").fetchone()[0] == 0, "el create_producto de adentro confirmó solo"
    assert _fotos(abrir, pid, nuevo) == [("Yerba editada", "222", 21.0), ("Leche", "LCH", 10.0)]


# ═══════════════════════════════════════════════════ (b) una excepción en la exterior revierte producto, código y auditoría


def test_una_excepcion_en_la_exterior_despues_de_un_update_interior_revierte_producto_codigo_y_auditoria(con_auditoria):
    abrir = con_auditoria
    with abrir() as conn:
        pid = _crear(conn, "Yerba", "111")
    log_antes = _log(abrir)
    with abrir() as conn, pytest.raises(RuntimeError, match="boom"):
        with repositorio_de(conn).transaction():
            _upd(conn, pid, "222", precio=21.0, nombre="Yerba editada")
            _crear(conn, "Leche", "LCH")
            raise RuntimeError("boom")
    assert _fotos(abrir, pid) == [("Yerba", "111", 10.0)]
    assert _codigos(abrir, pid) == ["111"]
    with abrir() as conn:
        assert conn.execute("SELECT COUNT(*) FROM catalog_items WHERE name='Leche'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM item_codes WHERE code IN ('222', 'LCH')").fetchone()[0] == 0
    assert _log(abrir) == log_antes, "quedó auditada una escritura que se deshizo"


def test_la_excepcion_del_llamador_que_sale_de_la_interior_y_de_la_exterior_revierte_todo(con_auditoria):
    """La excepción de la interior NO se atrapa: sube por las dos y la exterior hace rollback de todo (lo de antes del interior también)."""
    abrir = con_auditoria
    with abrir() as conn:
        pid = _crear(conn, "Yerba", "111")
        otro = _crear(conn, "Leche", "222")
        _crear(conn, "Otro", "DUP")
    log_antes = _log(abrir)
    with abrir() as conn, pytest.raises(catalogo.CodigoRepetido):
        with repositorio_de(conn).transaction():
            _upd(conn, otro, "L2", precio=33.0, nombre="Leche")
            _upd(conn, pid, "DUP", precio=99.0, nombre="Yerba editada")   # CodigoRepetido, sin atrapar
    assert _fotos(abrir, pid, otro) == [("Yerba", "111", 10.0), ("Leche", "222", 10.0)]
    assert _log(abrir) == log_antes


# ═══════════════════════════════════════════════════ (c) una interior que falla y se atrapa revierte sólo lo suyo (SAVEPOINT)


def test_un_codigo_repetido_en_un_update_interior_atrapado_deja_intacto_lo_anterior_y_la_exterior_sigue_y_confirma(con_auditoria):
    abrir = con_auditoria
    with abrir() as conn:
        p1 = _crear(conn, "Yerba", "111")
        p2 = _crear(conn, "Leche", "222")
        p3 = _crear(conn, "Mate", "333")
        _crear(conn, "Otro", "DUP")
    log_antes = _log(abrir)
    with abrir() as conn:
        with repositorio_de(conn).transaction():
            _upd(conn, p1, "A1", precio=21.0)
            with pytest.raises(catalogo.CodigoRepetido):
                _upd(conn, p2, "DUP", precio=99.0, nombre="Leche editada")
            # La conexión sigue usable (en PostgreSQL, sin el savepoint, esto muere con «current transaction is aborted»), y lo anterior de la exterior sigue en pie.
            assert catalogo.get_producto(conn, p1)["codigo"] == "A1"
            assert catalogo.get_producto(conn, p2)["nombre"] == "Leche"
            _upd(conn, p3, "C3", precio=23.0, nombre="Mate")
    assert _fotos(abrir, p1, p2, p3) == [("Yerba", "A1", 21.0), ("Leche", "222", 10.0), ("Mate", "C3", 23.0)]
    nuevas = _log(abrir)[len(log_antes):]
    assert [(a, e) for a, e, _ in nuevas] == [("editar", "producto"), ("crear", "codigo"), ("editar", "producto"), ("crear", "codigo")], nuevas
    assert not any("Leche editada" in d for _, _, d in nuevas), "la auditoría de la interior que se revirtió quedó registrada"


def test_una_interior_atrapada_y_despues_un_error_en_la_exterior_revierte_todo(con_auditoria):
    abrir = con_auditoria
    with abrir() as conn:
        p1 = _crear(conn, "Yerba", "111")
        p2 = _crear(conn, "Leche", "222")
        _crear(conn, "Otro", "DUP")
    log_antes = _log(abrir)
    with abrir() as conn, pytest.raises(RuntimeError, match="boom"):
        with repositorio_de(conn).transaction():
            _upd(conn, p1, "A1", precio=21.0)
            with pytest.raises(catalogo.CodigoRepetido):
                _upd(conn, p2, "DUP")
            raise RuntimeError("boom")
    assert _fotos(abrir, p1, p2) == [("Yerba", "111", 10.0), ("Leche", "222", 10.0)]
    assert _log(abrir) == log_antes


# ═══════════════════════════════════════════════════ (d) tres niveles


def _tres_niveles(abrir, pid, *, interior_falla, intermedia_falla, exterior_falla):
    """Cada nivel agrega un código de barras; `interior_falla` / `intermedia_falla` levantan después de escribir y la capa de arriba lo atrapa; `exterior_falla` propaga."""
    with abrir() as conn:
        repo = repositorio_de(conn)
        try:
            with repo.transaction():
                _barcode(repo, pid, "EXT")
                try:
                    with repositorio_de(conn).transaction() as medio:
                        _barcode(medio, pid, "MED")
                        try:
                            with repositorio_de(conn).transaction() as ultimo:
                                _barcode(ultimo, pid, "INT")
                                if interior_falla:
                                    raise ValueError("interior")
                        except ValueError:
                            pass
                        _barcode(medio, pid, "MED2")
                        if intermedia_falla:
                            raise ValueError("intermedia")
                except ValueError:
                    pass
                _barcode(repo, pid, "EXT2")
                if exterior_falla:
                    raise ValueError("exterior")
        except ValueError:
            pass


def test_tres_niveles_todo_bien_confirma_todo(abrir_vto):
    with abrir_vto() as conn:
        pid = _crear(conn, "Yerba", "111")
    _tres_niveles(abrir_vto, pid, interior_falla=False, intermedia_falla=False, exterior_falla=False)
    assert sorted(_codigos(abrir_vto, pid)) == sorted(["111", "EXT", "MED", "INT", "MED2", "EXT2"])


def test_tres_niveles_la_interior_falla_y_la_atrapa_la_del_medio(abrir_vto):
    with abrir_vto() as conn:
        pid = _crear(conn, "Yerba", "111")
    _tres_niveles(abrir_vto, pid, interior_falla=True, intermedia_falla=False, exterior_falla=False)
    assert sorted(_codigos(abrir_vto, pid)) == sorted(["111", "EXT", "MED", "MED2", "EXT2"])


def test_tres_niveles_la_del_medio_falla_y_se_va_con_ella_lo_de_la_interior_que_ya_se_habia_liberado(abrir_vto):
    with abrir_vto() as conn:
        pid = _crear(conn, "Yerba", "111")
    _tres_niveles(abrir_vto, pid, interior_falla=False, intermedia_falla=True, exterior_falla=False)
    assert sorted(_codigos(abrir_vto, pid)) == sorted(["111", "EXT", "EXT2"])


def test_tres_niveles_la_exterior_falla_y_no_queda_nada(abrir_vto):
    with abrir_vto() as conn:
        pid = _crear(conn, "Yerba", "111")
    _tres_niveles(abrir_vto, pid, interior_falla=False, intermedia_falla=False, exterior_falla=True)
    assert _codigos(abrir_vto, pid) == ["111"]


def test_tres_niveles_la_excepcion_sube_por_los_tres_y_no_queda_nada(abrir_vto):
    with abrir_vto() as conn:
        pid = _crear(conn, "Yerba", "111")
    with abrir_vto() as conn, pytest.raises(ValueError, match="sube"):
        repo = repositorio_de(conn)
        with repo.transaction():
            _barcode(repo, pid, "EXT")
            with repositorio_de(conn).transaction() as medio:
                _barcode(medio, pid, "MED")
                with repositorio_de(conn).transaction() as ultimo:
                    _barcode(ultimo, pid, "INT")
                    raise ValueError("sube")
    assert _codigos(abrir_vto, pid) == ["111"]


def test_una_interior_abierta_antes_de_cualquier_escritura_de_la_exterior_tampoco_confirma(abrir_vto):
    """El caso que un SAVEPOINT a secas hace mal en SQLite: sin transacción abierta, el SAVEPOINT más externo es el que abre la transacción y su RELEASE **confirma**. La exterior no
    escribió nada antes (no hay BEGIN todavía, el módulo `sqlite3` lo emite perezosamente antes del primer DML), así que la interior tiene que abrirla ella."""
    with abrir_vto() as conn:
        pid = _crear(conn, "Yerba", "111")
    with abrir_vto() as conn, pytest.raises(RuntimeError, match="boom"):
        repo = repositorio_de(conn)
        with repo.transaction():
            with repo.transaction():
                _barcode(repo, pid, "777")
            raise RuntimeError("boom")
    assert _codigos(abrir_vto, pid) == ["111"]


# ═══════════════════════════════════════════════════ (e) con RepositorioAuditado


def test_con_auditoria_ninguna_escritura_confirma_en_ningun_nivel_y_se_confirma_una_vez(abrir_vto):
    with abrir_vto() as real:
        real.execute(_LOG)
        real.commit()
        pid = _crear(real, "Yerba", "111")
        conn = _Cuenta(real)
        exterior = RepositorioAuditado(SqliteCommerceRepository(conn), conn, usuario=lambda: "ana")
        interior = RepositorioAuditado(SqliteCommerceRepository(conn), conn, usuario=lambda: "ana")   # otra instancia, como el `repositorio_de(conn)` de `update_producto`
        item = exterior.get_catalog_item(pid)
        antes = conn.commits
        with exterior.transaction():
            exterior.save_catalog_item(item)
            with interior.transaction():
                interior.save_item_code(ItemCode(None, pid, ItemCodeType.BARCODE, "777"))
                interior.save_catalog_item(item)
                assert conn.commits == antes
            assert conn.commits == antes
        assert conn.commits == antes + 1
        # y las tres filas de auditoría salieron en ese único commit
        assert [(a, e) for a, e, _ in _log(abrir_vto)][-3:] == [("editar", "producto"), ("crear", "codigo"), ("editar", "producto")]


def test_con_auditoria_la_fila_de_una_interior_que_se_revierte_se_revierte_con_ella(abrir_vto):
    with abrir_vto() as real:
        real.execute(_LOG)
        real.commit()
        pid = _crear(real, "Yerba", "111")
    with abrir_vto() as conn:
        repo = RepositorioAuditado(SqliteCommerceRepository(conn), conn, usuario=lambda: "ana")
        antes = len(_log(abrir_vto))
        with repo.transaction():
            _barcode(repo, pid, "EXT")
            with pytest.raises(ValueError, match="interior"):
                with repo.transaction():
                    _barcode(repo, pid, "INT")
                    raise ValueError("interior")
            assert conn.execute("SELECT COUNT(*) FROM actividad_log WHERE descripcion LIKE '%INT%'").fetchone()[0] == 0
    assert sorted(_codigos(abrir_vto, pid)) == ["111", "EXT"]
    nuevas = _log(abrir_vto)[antes:]
    assert [(a, e, d.endswith("EXT")) for a, e, d in nuevas] == [("crear", "codigo", True)], nuevas


# ═══════════════════════════════════════════════════ (f) hilos: los candados de ADR-028/029 siguen serializando


def test_dos_update_producto_concurrentes_en_transacciones_exteriores_propias_se_serializan_sin_deadlock(abrir_vto):
    """Cuatro hilos editan el MISMO producto a la vez, cada uno dentro de su propia `repo.transaction()` exterior (y escribiendo un código de barras después, para que la exterior
    tenga trabajo propio): todos terminan, y el producto queda con el código y el precio de UNO solo."""
    abrir = abrir_vto
    with abrir() as conn:
        pid = _crear(conn, "Yerba", "C0")
    barrera = threading.Barrier(4)
    errores: list[str] = []

    def escribir(i):
        try:
            barrera.wait(timeout=30)
            with abrir() as conn:
                repo = repositorio_de(conn)
                with repo.transaction():
                    _upd(conn, pid, f"C{i}", precio=100.0 + i)
                    _barcode(repo, pid, f"B{i}")
        except Exception as exc:  # noqa: BLE001
            errores.append(repr(exc))

    hilos = [threading.Thread(target=escribir, args=(i,)) for i in range(1, 5)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=60)
    assert not any(h.is_alive() for h in hilos), "un hilo quedó esperando (deadlock)"
    assert not errores, errores
    ((_, codigo, precio),) = _fotos(abrir, pid)
    assert precio == 100.0 + int(codigo[1:]), (codigo, precio)
    assert sorted(c for c in _codigos(abrir, pid) if c.startswith("B")) == ["B1", "B2", "B3", "B4"]


def test_cada_conexion_lleva_su_propia_profundidad_entre_hilos(abrir_vto):
    """Dos hilos, dos conexiones: uno adentro de su transacción no hace que el otro crea estar anidado (ni que no confirme)."""
    abrir = abrir_vto
    with abrir() as conn:
        pid = _crear(conn, "Yerba", "111")
    adentro, seguir = threading.Event(), threading.Event()
    errores: list[str] = []

    def retener():
        try:
            with abrir() as conn:
                with repositorio_de(conn).transaction():
                    adentro.set()
                    seguir.wait(timeout=30)
        except Exception as exc:  # noqa: BLE001
            errores.append(repr(exc))

    h = threading.Thread(target=retener)
    h.start()
    assert adentro.wait(timeout=30)
    with abrir() as conn:
        repo = repositorio_de(conn)
        with repo.transaction():
            _barcode(repo, pid, "777")
    assert "777" in _codigos(abrir, pid), "la transacción de este hilo se tomó por anidada y no confirmó"
    seguir.set()
    h.join(timeout=30)
    assert not errores, errores


# ═══════════════════════════════════════════════════ La profundidad no queda pegada


def test_la_profundidad_se_limpia_al_salir_bien_o_mal(abrir_vto):
    with abrir_vto() as conn:
        pid = _crear(conn, "Yerba", "111")
        repo = repositorio_de(conn)
        with repo.transaction():
            with repo.transaction():
                assert _repository._PROFUNDIDAD[id(conn)] == 2
        assert id(conn) not in _repository._PROFUNDIDAD
        with pytest.raises(RuntimeError, match="boom"):
            with repo.transaction():
                with repo.transaction():
                    raise RuntimeError("boom")
        assert id(conn) not in _repository._PROFUNDIDAD
        # y fuera de toda transacción una escritura vuelve a confirmar sola
        _barcode(repo, pid, "777")
    assert "777" in _codigos(abrir_vto, pid)
