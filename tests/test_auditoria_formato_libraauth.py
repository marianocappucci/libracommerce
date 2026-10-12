"""`RepositorioAuditado` + el registrador de `libraauth`: el formato de la fila es UNO (ADR-050).

Hasta v0.56 este motor tenia su propia copia del formato de `libraauth.auditoria`, y ya habia
divergido (sin `password_cifrada`/`token_hash`/`secret_key` tapadas, `created_at` ignorada,
`entidad_id INTEGER`). Ahora el wrapper pasa un `Cambio` y la fila la arma
`libraauth.registrador_dbapi`; estos tests fijan que **juntos** dejan lo que el log siempre dijo,
sobre la tabla `actividad_log` que crea `init_schema()` de este motor (`entidad_id INTEGER`,
`ts TEXT`) —la que tienen Contalibra, Restolibra y VentaLibra—.

Se saltea si `libraauth` no esta instalado o es anterior a v0.49.0: este motor no lo declara como
dependencia (ni en runtime ni, todavia, en `dev`: el pin se suma cuando exista el tag
`libraauth v0.49.0`, junto con su `uv.lock`). Un producto que lo une (VentaLibra, LibraClub) tiene
el mismo test en su suite.
"""
import sqlite3
from dataclasses import replace

import pytest

auditoria_de_libraauth = pytest.importorskip("libraauth.auditoria")
if not hasattr(auditoria_de_libraauth, "registrador_dbapi"):
    pytest.skip("libraauth anterior a v0.49.0: no tiene registrador_dbapi", allow_module_level=True)

from libraauth.auditoria import OCULTO, SISTEMA, registrador_dbapi, usuario_actual  # noqa: E402

from libracommerce.db.auditoria import (  # noqa: E402
    ActividadRepository,
    Cambio,
    RepositorioAuditado,
    entidades_por_tabla,
    opciones_del_registrador,
)
from libracommerce.db.repository import SqliteCommerceRepository  # noqa: E402
from libracommerce.db.schema import init_schema  # noqa: E402
from libracommerce.domain.catalog import CatalogItem, CatalogItemType, ItemCode, ItemCodeType, Unit  # noqa: E402
from libracommerce.domain.entities import Party, PartyType  # noqa: E402


def _registrador():
    return registrador_dbapi({**entidades_por_tabla(), "clients": "cliente"}, **opciones_del_registrador())


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    init_schema(c)
    return c


@pytest.fixture
def repo(conn) -> RepositorioAuditado:
    return RepositorioAuditado(SqliteCommerceRepository(conn), conn, _registrador())


@pytest.fixture
def log(conn) -> ActividadRepository:
    return ActividadRepository(conn)


@pytest.fixture(autouse=True)
def _usuario_limpio():
    token = usuario_actual.set(SISTEMA)
    yield
    usuario_actual.reset(token)


def _producto(repo, nombre="Yerba 1kg") -> CatalogItem:
    return repo.save_catalog_item(CatalogItem(None, CatalogItemType.PRODUCT, nombre, Unit("kg", "Kilogramo", True, 3)))


def _filas(log, entidad):
    return list(reversed([f for f in log.listar() if f["entidad"] == entidad]))


def test_el_alta_y_la_edicion_dejan_la_fila_con_el_formato_de_libraauth(repo, log):
    item = _producto(repo)
    repo.save_catalog_item(replace(item, name="Yerba 500g"))

    alta, edicion = _filas(log, "producto")
    assert (alta["accion"], alta["descripcion"], alta["cambios"], alta["usuario"]) == (
        "crear", "Producto — Yerba 1kg", None, SISTEMA)
    assert (edicion["accion"], edicion["descripcion"]) == ("editar", "Producto — Yerba 500g")
    assert edicion["cambios"] == {"name": ["Yerba 1kg", "Yerba 500g"]}
    # `entidad_id INTEGER` + id como texto: la base lo convierte y el log sigue diciendo de que fila habla.
    assert str(alta["entidad_id"]) == str(item.id) == str(edicion["entidad_id"])


def test_el_usuario_sale_del_contextvar_de_libraauth(repo, log):
    token = usuario_actual.set("ana")
    try:
        _producto(repo)
    finally:
        usuario_actual.reset(token)
    assert log.listar()[0]["usuario"] == "ana"


def test_guardar_sin_cambiar_nada_no_deja_fila(repo, log):
    """Cambio de formato respecto de v0.56, que dejaba una fila `editar` vacia: ahora rige la
    regla del log de siempre (`libraauth`): una edicion sin nada distinto es ruido."""
    item = _producto(repo)
    repo.save_catalog_item(item)

    assert [f["accion"] for f in _filas(log, "producto")] == ["crear"]


def test_una_edicion_sin_getter_deja_el_hecho_sin_detalle(repo, log):
    item = _producto(repo)
    codigo = repo.save_item_code(ItemCode(None, item.id, ItemCodeType.BARCODE, "7790001"))
    repo.save_item_code(replace(codigo, code="7790002"))

    alta, edicion = _filas(log, "codigo")
    assert (alta["accion"], edicion["accion"], edicion["cambios"]) == ("crear", "editar", None)
    assert edicion["descripcion"] == "Codigo — 7790002"


@pytest.mark.parametrize("clave", [
    "api_key", "password", "password_cifrada", "token_hash", "secret_key", "access_token",
])
def test_un_setting_secreto_deja_rastro_sin_el_valor(repo, log, clave):
    """Incluye las tres que la copia de v0.56 NO tapaba: `password_cifrada`, `token_hash`
    y `secret_key`."""
    repo.set_setting(clave, "secreto-viejo")
    repo.set_setting(clave, "secreto-nuevo")

    filas = _filas(log, "configuracion")
    assert [f["accion"] for f in filas] == ["crear", "editar"]
    assert filas[1]["cambios"] == {clave: [OCULTO, OCULTO]}
    assert "secreto" not in str(log.listar())


def test_un_setting_comun_lleva_el_antes_y_el_despues(repo, log):
    repo.set_setting("balanza_formato", "ean13")
    repo.set_setting("balanza_formato", "ean8")
    repo.set_setting("balanza_formato", "ean8")

    alta, edicion = _filas(log, "configuracion")
    assert (alta["cambios"], alta["descripcion"]) == (None, "Configuracion — balanza_formato")
    assert edicion["cambios"] == {"balanza_formato": ["ean13", "ean8"]}


def test_created_at_no_entra_al_diff(conn, log):
    """Lo recalcula el motor en cada guardado: no es un cambio del usuario."""
    registrar = _registrador()
    registrar(conn, Cambio("parties", "editar", 1,
                           {"display_name": "Ana", "created_at": "2026-10-01"},
                           {"display_name": "Ana B", "created_at": "2026-10-12"}))
    registrar(conn, Cambio("parties", "editar", 1, {"created_at": "a"}, {"created_at": "b"}))
    conn.commit()

    (fila,) = log.listar()
    assert fila["cambios"] == {"display_name": ["Ana", "Ana B"]}


def test_una_coleccion_se_resume_en_cuantos_items(conn, log):
    registrar = _registrador()
    registrar(conn, Cambio("sales", "editar", 7,
                           {"number": "V-1", "items": (1, 2)}, {"number": "V-1", "items": (1, 2, 3)}))
    registrar(conn, Cambio("sales", "editar", 7,
                           {"number": "V-1", "items": (1, 2)}, {"number": "V-1", "items": (1, 9)}))
    conn.commit()

    mismo_largo, nueva = log.listar()
    assert nueva["cambios"] == {"items": ["2 items", "3 items"]}
    # Mismo largo, otro contenido: SIGUE siendo una edicion (se compara el crudo).
    assert mismo_largo["cambios"] == {"items": ["2 items", "2 items"]}


def test_los_clientes_del_motor_core_se_etiquetan_con_las_reglas_de_libraauth(conn, log):
    """El mismo registrador sirve a `libracore.auditoria`: una tabla que este motor no conoce
    (`clients`) cae en el armado por defecto."""
    _registrador()(conn, Cambio("clients", "crear", 3, None, {"name": "Don Pepe", "cuit_dni": "20-1"}))
    conn.commit()

    (fila,) = log.listar()
    assert (fila["entidad"], fila["descripcion"]) == ("cliente", "Cliente — Don Pepe")
