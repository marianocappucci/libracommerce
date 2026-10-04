"""Un código repetido da un error de dominio en castellano, no el texto crudo de la base (ADR-029), contra los dos motores.

El defecto: `PUT /api/productos/{id}` (y el alta, y el alta de un código adicional) con un código que ya tiene otro producto daba 422 (409 en `/codigos`) pero con el
`detail` de la base —`duplicate key value violates unique constraint "item_codes_code_type_code_key" DETAIL: Key (code_type, code)=(internal, YERBA-1) already exists.`
en PostgreSQL, `UNIQUE constraint failed: item_codes.code_type, item_codes.code` en SQLite— y la UI lo mostraba tal cual. Acá se fija:

- el motor lanza `catalogo.CodigoRepetido` (con el código) desde TODO camino que escribe un código: alta, edición, código adicional y código de balanza;
- el router responde con el mismo status de siempre y `{"detail": "Ya existe un producto con el código «X»."}`, sin ninguna palabra de la base;
- **sólo** la violación del único `(code_type, code)` se traduce: un segundo principal, una FK, un NOT NULL o la unicidad de otra tabla siguen su camino;
- el rollback de ADR-028 sigue: un código repetido no guarda nada (y el alta, que no era atómica, tampoco deja un producto huérfano).
"""

from __future__ import annotations

import sqlite3

import pytest
from conftest import _usuario  # noqa: F401  (y la fixture `abrir`, que pytest carga sola)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from libracommerce.erp import catalogo
from libracommerce.web.catalogo_router import build_productos_router

MENSAJE = "Ya existe un producto con el código «YERBA-1»."
TEXTOS_DE_LA_BASE = ("duplicate key", "UNIQUE constraint", "violates", "item_codes", "DETAIL")


@pytest.fixture
def client(abrir):
    app = FastAPI()
    app.include_router(build_productos_router(conexion=abrir, usuario_actual=_usuario))
    return TestClient(app)


@pytest.fixture
def dos_productos(abrir):
    """`YERBA-1` es de la yerba; el segundo producto (`LECHE-1`) es el que intenta quedarse con él."""
    with abrir() as conn:
        yerba = catalogo.create_producto(conn, nombre="Yerba", codigo="YERBA-1", precio_venta=10, precio_costo=5)
        leche = catalogo.create_producto(conn, nombre="Leche", codigo="LECHE-1", precio_venta=20, precio_costo=8)
    return yerba, leche


def _estado(abrir) -> dict:
    """Todo lo que un código repetido no puede mover: productos, códigos y categorías."""
    with abrir() as conn:
        return {
            "productos": [tuple(f) for f in conn.execute(
                "SELECT id, name, default_sale_price, default_cost, category_id FROM catalog_items ORDER BY id").fetchall()],
            "codigos": [tuple(f) for f in conn.execute(
                "SELECT item_id, code_type, code, is_primary FROM item_codes ORDER BY id").fetchall()],
            "categorias": [tuple(f) for f in conn.execute("SELECT name FROM categories ORDER BY id").fetchall()],
        }


def _sin_texto_de_la_base(detail: str) -> None:
    for texto in TEXTOS_DE_LA_BASE:
        assert texto.lower() not in detail.lower(), detail


def _payload(nombre="Leche editada", codigo="YERBA-1", **extra):
    return {"nombre": nombre, "codigo": codigo, "precio_venta": 99.0, "precio_costo": 50.0, **extra}


# ═══════════════════════════════════════════════════ El router: el mensaje en castellano, el mismo status, nada guardado


def test_la_edicion_con_un_codigo_repetido_da_422_en_castellano_y_no_guarda_nada(abrir, client, dos_productos):
    _, leche = dos_productos
    antes = _estado(abrir)
    r = client.put(f"/api/productos/{leche}", json=_payload(categoria="Nueva"))
    assert r.status_code == 422
    assert r.json() == {"detail": MENSAJE}
    _sin_texto_de_la_base(r.json()["detail"])
    assert _estado(abrir) == antes   # ni el nombre, ni el precio, ni la categoría nueva (ADR-028)


def test_el_alta_con_un_codigo_repetido_da_422_en_castellano_y_no_deja_un_producto_huerfano(abrir, client, dos_productos):
    antes = _estado(abrir)
    r = client.post("/api/productos", json=_payload(nombre="Otra yerba", categoria="Nueva"))
    assert r.status_code == 422
    assert r.json() == {"detail": MENSAJE}
    _sin_texto_de_la_base(r.json()["detail"])
    assert _estado(abrir) == antes   # antes el producto quedaba guardado, sin código


def test_el_alta_de_un_codigo_adicional_repetido_da_409_en_castellano_y_no_guarda_nada(abrir, client, dos_productos):
    """`/codigos` ya respondía 409 (`test_los_codigos_de_un_producto` lo fija): el status no cambia, sí el texto."""
    _, leche = dos_productos
    antes = _estado(abrir)
    r = client.post(f"/api/productos/{leche}/codigos", json={"tipo": "internal", "codigo": "YERBA-1"})
    assert r.status_code == 409
    assert r.json() == {"detail": MENSAJE}
    _sin_texto_de_la_base(r.json()["detail"])
    assert _estado(abrir) == antes


def test_el_mismo_codigo_en_otro_tipo_no_es_un_repetido(abrir, client, dos_productos):
    """El único es `(code_type, code)`: el `YERBA-1` interno de la yerba no impide un código de barras `YERBA-1`."""
    _, leche = dos_productos
    r = client.post(f"/api/productos/{leche}/codigos", json={"tipo": "barcode", "codigo": "YERBA-1"})
    assert r.status_code == 200, r.text


def test_un_segundo_principal_NO_se_disfraza_de_codigo_repetido(abrir, client, dos_productos):
    _, leche = dos_productos
    antes = _estado(abrir)
    r = client.post(f"/api/productos/{leche}/codigos", json={"tipo": "barcode", "codigo": "OTRO", "es_principal": True})
    assert r.status_code == 409
    assert r.json() == {"detail": "Ese código ya existe, o el producto ya tiene un código principal."}   # el camino de siempre
    assert "Ya existe un producto" not in r.json()["detail"]
    assert _estado(abrir) == antes


# ═══════════════════════════════════════════════════ El motor: la excepción de dominio, desde cada camino que escribe un código


def test_el_motor_lanza_codigo_repetido_desde_el_alta_la_edicion_el_adicional_y_el_de_balanza(abrir, dos_productos):
    yerba, leche = dos_productos
    antes = _estado(abrir)
    with abrir() as conn:
        with pytest.raises(catalogo.CodigoRepetido) as alta:
            catalogo.create_producto(conn, nombre="Otra", codigo="YERBA-1", precio_venta=1, precio_costo=1)
        with pytest.raises(catalogo.CodigoRepetido) as edicion:
            catalogo.update_producto(conn, pid=leche, nombre="Leche", codigo="YERBA-1", descripcion="", precio_venta=1, precio_costo=1, unidad="u", categoria="", activo=1)
        with pytest.raises(catalogo.CodigoRepetido) as adicional:
            catalogo.add_codigo(conn, leche, "internal", "YERBA-1")
        conn.rollback()
    with abrir() as conn:
        catalogo.agregar_codigo_balanza(conn, yerba, "0012")
    with abrir() as conn, pytest.raises(catalogo.CodigoRepetido) as balanza:
        catalogo.agregar_codigo_balanza(conn, leche, "0012")
    for e in (alta, edicion, adicional):
        assert str(e.value) == MENSAJE and e.value.codigo == "YERBA-1"
    assert str(balanza.value) == "Ya existe un producto con el código «0012»."
    assert isinstance(balanza.value, ValueError)
    assert _estado(abrir)["productos"] == antes["productos"]


def test_la_excepcion_de_dominio_conserva_la_de_la_base_de_causa(abrir, dos_productos):
    _, leche = dos_productos
    with abrir() as conn, pytest.raises(catalogo.CodigoRepetido) as e:
        catalogo.add_codigo(conn, leche, "internal", "YERBA-1")
    assert isinstance(e.value.__cause__, sqlite3.IntegrityError)


# ═══════════════════════════════════════════════════ Otros errores de integridad: no se traducen


def _provocar(abrir, pid, que):
    """La excepción REAL que da cada motor ante `que`, por el mismo camino de escritura que usa el motor."""
    from libracommerce.db.repository import repositorio_de
    from libracommerce.domain.catalog import ItemCode, ItemCodeType

    with abrir() as conn:
        if que == "segundo_principal":
            repositorio_de(conn).save_item_code(ItemCode(None, pid, ItemCodeType.BARCODE, "OTRO", is_primary=True))
        elif que == "fk":
            repositorio_de(conn).save_item_code(ItemCode(None, 99999, ItemCodeType.BARCODE, "HUERFANO"))
        elif que == "not_null":
            conn.execute("INSERT INTO item_codes (item_id, code_type, code, is_primary) VALUES (?, ?, ?, 0)", (pid, "barcode", None))
        elif que == "unicidad_de_otra_tabla":
            catalogo.create_variante(conn, pid, "SKU-1", "Chica")
            catalogo.create_variante(conn, pid, "SKU-1", "Grande")
        else:  # pragma: no cover
            raise AssertionError(que)


@pytest.mark.parametrize("que", ["segundo_principal", "fk", "not_null", "unicidad_de_otra_tabla"])
def test_otro_error_de_integridad_no_se_traduce_a_codigo_repetido(abrir, dos_productos, que):
    _, leche = dos_productos
    with pytest.raises(sqlite3.IntegrityError) as e:   # contra PostgreSQL `libracore` lo entrega como `sqlite3.IntegrityError`
        _provocar(abrir, leche, que)
    assert not isinstance(e.value, catalogo.CodigoRepetido)
    assert catalogo._es_codigo_repetido(e.value) is False


def test_el_helper_reconoce_el_unico_de_los_codigos_en_el_motor_que_corre(abrir, dos_productos):
    _, leche = dos_productos
    with abrir() as conn:
        try:
            conn.execute("INSERT INTO item_codes (item_id, code_type, code, is_primary) VALUES (?, 'internal', 'YERBA-1', 0)", (leche,))
        except sqlite3.IntegrityError as e:
            assert catalogo._es_codigo_repetido(e) is True
        else:  # pragma: no cover
            raise AssertionError("no hubo error")


def test_un_error_que_no_es_de_integridad_sube_sin_tocarse(abrir, dos_productos):
    _, leche = dos_productos
    with abrir() as conn, pytest.raises(ValueError, match="tipo de código inválido") as e:
        catalogo.add_codigo(conn, leche, "ean", "1")
    assert not isinstance(e.value, catalogo.CodigoRepetido)


# ═══════════════════════════════════════════════════ El helper, sin base: SQLite y PostgreSQL a mano (el texto sólo se mira en SQLite)


def _sqlite_error(mensaje: str, nombre: str = "SQLITE_CONSTRAINT_UNIQUE") -> sqlite3.IntegrityError:
    e = sqlite3.IntegrityError(mensaje)
    e.sqlite_errorname = nombre
    return e


@pytest.mark.parametrize("mensaje,esperado", [
    ("UNIQUE constraint failed: item_codes.code_type, item_codes.code", True),
    ("UNIQUE constraint failed: item_codes.code, item_codes.code_type", True),     # el orden de las columnas no importa
    ("UNIQUE constraint failed: item_codes.item_id", False),                       # un segundo principal
    ("UNIQUE constraint failed: item_variants.sku", False),
    ("UNIQUE constraint failed: otra.code_type, otra.code", False),                # mismas columnas, otra tabla
    ("FOREIGN KEY constraint failed", False),
    ("NOT NULL constraint failed: item_codes.code", False),
])
def test_helper_sqlite_por_clase_y_columnas(mensaje, esperado):
    nombre = "SQLITE_CONSTRAINT_UNIQUE" if mensaje.startswith("UNIQUE") else "SQLITE_CONSTRAINT_FOREIGNKEY"
    assert catalogo._es_codigo_repetido(_sqlite_error(mensaje, nombre)) is esperado


def test_helper_sqlite_no_mira_el_texto_si_la_clase_no_es_de_unicidad():
    # El mismo texto en un error que SQLite no clasificó como unicidad no cuenta.
    assert catalogo._es_codigo_repetido(_sqlite_error("UNIQUE constraint failed: item_codes.code_type, item_codes.code", "SQLITE_CONSTRAINT_CHECK")) is False
    assert catalogo._es_codigo_repetido(ValueError("UNIQUE constraint failed: item_codes.code_type, item_codes.code")) is False
    assert catalogo._es_codigo_repetido(sqlite3.OperationalError("database is locked")) is False


def test_helper_postgres_por_clase_y_constraint_name_sin_leer_el_mensaje():
    pytest.importorskip("psycopg")
    from psycopg import errors

    def error(clase, constraint):
        class _Con(clase):   # `diag` de psycopg sale del servidor: acá se fija a mano
            diag = type("Diag", (), {"constraint_name": constraint})()

        return _Con("mensaje que no se lee")

    unico = error(errors.UniqueViolation, "item_codes_code_type_code_key")
    assert catalogo._es_codigo_repetido(unico) is True
    assert catalogo._es_codigo_repetido(error(errors.UniqueViolation, "idx_item_codes_one_primary_per_item")) is False
    assert catalogo._es_codigo_repetido(error(errors.UniqueViolation, None)) is False
    assert catalogo._es_codigo_repetido(error(errors.ForeignKeyViolation, "item_codes_item_id_fkey")) is False
    assert catalogo._es_codigo_repetido(error(errors.NotNullViolation, "item_codes_code_type_code_key")) is False
    # Como lo entrega `libracore`: un `sqlite3.IntegrityError` con el de psycopg de causa.
    envuelto = sqlite3.IntegrityError("duplicate key value violates unique constraint ...")
    envuelto.__cause__ = unico
    assert catalogo._es_codigo_repetido(envuelto) is True
    otro = sqlite3.IntegrityError("duplicate key value violates unique constraint \"item_codes_code_type_code_key\"")
    otro.__cause__ = error(errors.UniqueViolation, "idx_item_codes_one_primary_per_item")
    assert catalogo._es_codigo_repetido(otro) is False   # el nombre en el texto no engaña: manda la causa
    assert catalogo._es_codigo_repetido(sqlite3.IntegrityError("duplicate key value violates unique constraint \"item_codes_code_type_code_key\"")) is False
