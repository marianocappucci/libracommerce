"""Log de actividad del motor comercial.

Lo que fijan estos tests, en orden de lo que se rompe sin que se note:

1. 🔴 **Que ninguna escritura quede sin auditar** — `test_toda_escritura_esta_auditada`.
   Es el test que sostiene todo lo demas: un log incompleto se ve exactamente
   igual que uno completo, asi que la unica defensa contra "alguien agrego un
   metodo y se olvido" es que el CI lo cace.
2. Que el envoltorio le pase al registrador el `Cambio` correcto (tabla, accion, id,
   antes y despues) y no cambie lo que el repositorio devuelve.
3. Que sin registrador no se construya: un wrapper que no audita sin avisar es el modo de
   falla que este archivo existe para evitar.
4. Que la interfaz de lectura sea la misma que espera `build_logs_router`.

El FORMATO de la fila (secretos tapados, diff, etiqueta, usuario) ya no se define aca
(ADR-050): vive en `libraauth.auditoria` y lo prueba `test_auditoria_formato_libraauth.py`.
"""
import inspect
import sqlite3
from dataclasses import replace
from datetime import datetime
from decimal import Decimal

import pytest
from registrador_de_prueba import registrador_de_prueba

from libracommerce.db.auditoria import (
    AUDITABLES,
    CREAR,
    EDITAR,
    NO_AUDITABLES,
    ActividadRepository,
    Cambio,
    RepositorioAuditado,
    entidades,
    entidades_por_tabla,
    etiqueta_de_tabla,
    opciones_del_registrador,
    resumir_colecciones,
)
from libracommerce.db.repository import SqliteCommerceRepository
from libracommerce.db.schema import init_schema
from libracommerce.domain.catalog import CatalogItem, CatalogItemType, ItemCode, ItemCodeType, PriceList, Unit
from libracommerce.domain.entities import Party, PartyType
from libracommerce.domain.inventory import Location, StockMovement, StockMovementType


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    init_schema(c)
    return c


@pytest.fixture
def cambios() -> list[Cambio]:
    """Los `Cambio` que recibio el registrador, en orden."""
    return []


@pytest.fixture
def repo(conn, cambios) -> RepositorioAuditado:
    return RepositorioAuditado(SqliteCommerceRepository(conn), conn, registrador_de_prueba(cambios))


@pytest.fixture
def log(conn) -> ActividadRepository:
    return ActividadRepository(conn)


def _kg() -> Unit:
    return Unit("kg", "Kilogramo", True, 3)


def _producto(repo, nombre="Yerba 1kg") -> CatalogItem:
    return repo.save_catalog_item(CatalogItem(None, CatalogItemType.PRODUCT, nombre, _kg()))


# ── 🔴 El guardian ────────────────────────────────────────────────────────

def test_toda_escritura_esta_auditada():
    """Enumera los metodos de escritura del repositorio y exige que cada uno
    este declarado: auditado en `AUDITABLES`, o exceptuado a proposito en
    `NO_AUDITABLES` con el motivo escrito.

    **Este test es la razon por la que este enfoque no se degrada.** Sembrar
    llamadas a mano funciona el primer dia; lo que falla es el metodo numero 13
    que alguien agrega dentro de seis meses. Sin este test, ese metodo escribe
    sin dejar rastro y nadie se entera nunca — el log sigue mostrando filas y
    parece sano.

    Si estas leyendo esto porque el test fallo: agrega tu metodo a `AUDITABLES`
    (lo normal) o a `NO_AUDITABLES` explicando por que no corresponde. Lo que
    no vale es sacarlo del detector.
    """
    declarados = set(AUDITABLES) | set(NO_AUDITABLES)

    escrituras = set()
    for nombre, metodo in inspect.getmembers(SqliteCommerceRepository, inspect.isfunction):
        if nombre == "__init__":
            continue
        fuente = inspect.getsource(metodo).upper()
        if "INSERT INTO" in fuente or " SET " in fuente or "DELETE FROM" in fuente:
            escrituras.add(nombre)

    sin_declarar = escrituras - declarados
    assert not sin_declarar, (
        f"metodos que escriben y no estan declarados: {sorted(sin_declarar)}. "
        "Agregalos a AUDITABLES, o a NO_AUDITABLES con el motivo."
    )

    # Y al reves: que no quede basura declarada de un metodo que ya no existe,
    # porque una entrada muerta en AUDITABLES infla el filtro de la pantalla
    # con una entidad que nunca va a aparecer.
    fantasmas = declarados - {
        n for n, _ in inspect.getmembers(SqliteCommerceRepository, inspect.isfunction)
    }
    assert not fantasmas, f"declarados pero inexistentes: {sorted(fantasmas)}"


# ── Que le avise al registrador ───────────────────────────────────────────

def test_toda_entrada_declarada_apunta_a_una_tabla_que_existe(conn):
    """La `tabla` de cada `AUDITABLES` es la clave con la que el registrador resuelve la
    entidad y su etiqueta: si se desalinea del schema, la fila sale sin entidad conocida."""
    reales = {f[0] for f in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    tablas = [meta.tabla for meta in AUDITABLES.values()]
    assert set(tablas) <= reales
    assert len(tablas) == len(set(tablas)), "dos metodos auditan la misma tabla"
    assert entidades_por_tabla() == {meta.tabla: meta.entidad for meta in AUDITABLES.values()}


def test_un_alta_le_avisa_al_registrador(repo, log, cambios):
    item = _producto(repo)

    assert len(cambios) == 1
    c = cambios[0]
    assert (c.tabla, c.accion, c.id, c.antes) == ("catalog_items", CREAR, item.id, None)
    assert c.despues["name"] == "Yerba 1kg"

    filas = log.listar()
    assert (filas[0]["accion"], filas[0]["entidad"], filas[0]["usuario"]) == (CREAR, "producto", "ana")
    assert "Yerba 1kg" in filas[0]["descripcion"]


def test_una_edicion_lleva_el_antes_y_el_despues(repo, cambios):
    item = _producto(repo)
    repo.save_catalog_item(replace(item, name="Yerba 500g"))

    editar = [c for c in cambios if c.accion == EDITAR][0]
    assert (editar.tabla, editar.id) == ("catalog_items", item.id)
    assert (editar.antes["name"], editar.despues["name"]) == ("Yerba 1kg", "Yerba 500g")
    # Las dos fotos son de la entidad entera: el diff lo arma el registrador, no este motor.
    assert editar.antes.keys() == editar.despues.keys()


def test_guardar_sin_cambiar_nada_igual_le_avisa_al_registrador(repo, cambios):
    """El motor no puede saber si el usuario apreto guardar a proposito: avisa, y es el
    registrador (el de `libraauth`) quien descarta la edicion que no cambio nada."""
    item = _producto(repo)
    repo.save_catalog_item(item)

    editar = [c for c in cambios if c.accion == EDITAR][0]
    assert editar.antes == editar.despues


def test_una_edicion_sin_getter_llega_sin_antes(repo, cambios):
    """`save_item_code` no tiene como leer el estado previo: el cambio llega con `antes=None`
    y el registrador deja el hecho sin detalle."""
    item = _producto(repo)
    codigo = repo.save_item_code(ItemCode(None, item.id, ItemCodeType.BARCODE, "7790001"))
    repo.save_item_code(replace(codigo, code="7790002"))

    editar = [c for c in cambios if c.tabla == "item_codes" and c.accion == EDITAR][0]
    assert editar.antes is None and editar.despues["code"] == "7790002"


def test_el_ledger_de_stock_siempre_es_un_alta(repo, log, cambios):
    """Un movimiento de stock nunca se edita: se agrega otro que lo compensa.
    Registrar "editar" sobre un ledger seria mentir sobre lo que paso."""
    item = _producto(repo)
    ubicacion = repo.save_location(Location(None, "Deposito"))
    repo.append_stock_movement(StockMovement(
        None, item.id, ubicacion.id, StockMovementType.ADJUSTMENT,
        Decimal("10"), datetime(2026, 8, 5, 10, 0),
    ))

    movimientos = [c for c in cambios if c.tabla == "stock_movements"]
    assert [(c.accion, c.antes) for c in movimientos] == [(CREAR, None)]
    assert len([f for f in log.listar() if f["entidad"] == "movimiento de stock"]) == 1


def test_una_coleccion_anidada_llega_como_tupla_y_se_resume_con_el_gancho(repo, cambios):
    """El diff de una tupla de dataclasses no lo lee nadie: el registrador resume con
    `resumir_colecciones`, que este motor entrega en `opciones_del_registrador()`."""
    assert resumir_colecciones((1, 2, 3)) == "3 items"
    assert resumir_colecciones([]) == "0 items"
    assert resumir_colecciones("texto") == "texto"
    assert opciones_del_registrador()["resumir"] is resumir_colecciones

    lista = repo.save_price_list(PriceList(None, "Mostrador"))
    repo.save_price_list(replace(lista, name="Mayorista"))
    editar = [c for c in cambios if c.tabla == "price_lists" and c.accion == EDITAR][0]
    assert (editar.antes["name"], editar.despues["name"]) == ("Mostrador", "Mayorista")


def test_las_opciones_del_registrador_ignoran_created_at_y_etiquetan_por_tabla():
    opciones = opciones_del_registrador()
    assert opciones["columnas_ignoradas"] == {"created_at"}
    assert opciones["etiqueta"] is etiqueta_de_tabla
    assert etiqueta_de_tabla("parties", {"display_name": "Ana"}) == "Ana"
    assert etiqueta_de_tabla("sales", {"number": "V-1"}) == "V-1"
    assert etiqueta_de_tabla("item_prices", {"price": 10}) == ""   # conocida, sin etiqueta
    assert etiqueta_de_tabla("commerce_settings", {"balanza_formato": "ean13"}) == "balanza_formato"
    assert etiqueta_de_tabla("clients", {"name": "Ana"}) is None   # no es de este motor: la resuelve el registrador


# ── Configuraciones ───────────────────────────────────────────────────────

def test_un_setting_llega_como_alta_y_despues_como_edicion(repo, cambios):
    repo.set_setting("balanza_formato", "ean13")
    repo.set_setting("balanza_formato", "ean8")

    alta, edicion = cambios
    assert (alta.tabla, alta.accion, alta.antes, alta.despues) == (
        "commerce_settings", CREAR, None, {"balanza_formato": "ean13"})
    assert (edicion.accion, edicion.antes, edicion.despues) == (
        EDITAR, {"balanza_formato": "ean13"}, {"balanza_formato": "ean8"})


def test_guardar_el_mismo_setting_no_registra_nada(repo, log, cambios):
    repo.set_setting("balanza_formato", "ean13")
    repo.set_setting("balanza_formato", "ean13")

    assert len(cambios) == 1
    assert len([f for f in log.listar() if f["entidad"] == "configuracion"]) == 1


# ── El envoltorio no puede cambiar lo que el repositorio hace ─────────────

def test_las_lecturas_pasan_derecho(repo):
    """Se delega por `__getattr__`: un metodo de lectura nuevo funciona sin
    tocar el envoltorio."""
    item = _producto(repo)
    assert repo.get_catalog_item(item.id).name == "Yerba 1kg"
    assert len(repo.list_catalog_items()) == 1


def test_la_escritura_devuelve_lo_mismo_que_sin_envolver(conn):
    """Si el envoltorio se comiera el valor de retorno, el producto entero
    dejaria de funcionar — pero recien al usarlo, no al arrancar."""
    desnudo = SqliteCommerceRepository(conn)
    envuelto = RepositorioAuditado(desnudo, conn, registrador_de_prueba())

    party = envuelto.save_party(Party(None, PartyType.PERSON, "Ana"))
    assert party.id is not None
    assert desnudo.get_party(party.id) == party


def test_sin_registrador_no_se_construye(conn):
    """Un wrapper que no audita sin avisar es el modo de falla que este archivo existe para
    evitar; y un default seria una segunda copia del formato (ADR-050)."""
    with pytest.raises(TypeError):
        RepositorioAuditado(SqliteCommerceRepository(conn), conn)
    with pytest.raises(TypeError, match="registrador"):
        RepositorioAuditado(SqliteCommerceRepository(conn), conn, None)


def test_el_parametro_usuario_de_antes_ya_no_existe(conn):
    """El usuario lo resuelve el registrador (del ContextVar de libraauth). Pasar `usuario=`
    como hasta v0.56 es un error ruidoso y no un usuario ignorado en silencio."""
    with pytest.raises(TypeError):
        RepositorioAuditado(SqliteCommerceRepository(conn), conn, registrador_de_prueba(), usuario=lambda: "x")


def test_si_el_registrador_falla_la_excepcion_sube(conn):
    def roto(conn, cambio):
        raise RuntimeError("el log no se pudo escribir")

    repo = RepositorioAuditado(SqliteCommerceRepository(conn), conn, roto)
    with pytest.raises(RuntimeError, match="el log"):
        _producto(repo)


# ── La interfaz que espera el router del motor de auth ────────────────────

def test_la_lectura_tiene_la_interfaz_que_espera_el_router(repo, log):
    """`build_logs_router` no sabe cual de las dos implementaciones recibe:
    llama `listar`, `contar` y `usuarios` por duck typing. Si alguna cambiara
    de firma, el router se rompe en runtime y no al importar."""
    _producto(repo)

    assert log.contar() == 1
    assert log.usuarios() == ["ana"]
    assert log.listar(limit=10, offset=0)


def test_los_filtros_recortan(repo, log):
    _producto(repo, "Yerba")
    repo.save_party(Party(None, PartyType.PERSON, "Ana"))

    assert log.contar(entidad="producto") == 1
    assert log.contar(accion=CREAR) == 2
    assert log.contar(usuario="nadie") == 0


def test_el_filtro_hasta_incluye_el_dia_entero(repo, log):
    """`hasta` llega como dia (`2026-08-05`) y `ts` tiene hora. Sin completar
    el fin del dia, filtrar "hasta hoy" no devolveria nada de hoy — que es
    justo lo que uno busca cuando abre la pantalla."""
    _producto(repo)
    hoy = datetime.now().strftime("%Y-%m-%d")

    assert log.contar(hasta=hoy) == 1


def test_el_filtro_de_entidades_sale_de_lo_declarado(repo, log):
    """Y no de un `SELECT DISTINCT` sobre el log: el filtro tiene que ofrecer
    las entidades auditables aunque todavia no haya actividad de ninguna."""
    valores = set(entidades().values())
    assert {"producto", "venta", "deposito", "movimiento de stock"} <= valores
    assert log.listar() == []
