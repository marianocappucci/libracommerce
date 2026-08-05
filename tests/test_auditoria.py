"""Log de actividad del motor comercial.

Lo que fijan estos tests, en orden de lo que se rompe sin que se note:

1. 🔴 **Que ninguna escritura quede sin auditar** — `test_toda_escritura_esta_auditada`.
   Es el test que sostiene todo lo demas: un log incompleto se ve exactamente
   igual que uno completo, asi que la unica defensa contra "alguien agrego un
   metodo y se olvido" es que el CI lo cace.
2. Que el envoltorio no cambie lo que el repositorio devuelve.
3. Que un secreto no termine escrito, y que igual quede rastro de que cambio.
4. Que la interfaz de lectura sea la misma que espera `build_logs_router`.
"""
import inspect
import sqlite3
from dataclasses import replace
from datetime import datetime
from decimal import Decimal

import pytest

from libracommerce.db.auditoria import (
    AUDITABLES,
    CREAR,
    EDITAR,
    NO_AUDITABLES,
    OCULTO,
    ActividadRepository,
    RepositorioAuditado,
    entidades,
)
from libracommerce.db.repository import SqliteCommerceRepository
from libracommerce.db.schema import init_schema
from libracommerce.domain.catalog import CatalogItem, CatalogItemType, PriceList, Unit
from libracommerce.domain.entities import Party, PartyType
from libracommerce.domain.inventory import Location, StockMovement, StockMovementType


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    init_schema(c)
    return c


@pytest.fixture
def repo(conn) -> RepositorioAuditado:
    return RepositorioAuditado(SqliteCommerceRepository(conn), conn, usuario=lambda: "ana")


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


# ── Que registre ──────────────────────────────────────────────────────────

def test_un_alta_queda_registrada(repo, log):
    _producto(repo)

    filas = log.listar()
    assert len(filas) == 1
    assert filas[0]["accion"] == CREAR
    assert filas[0]["entidad"] == "producto"
    assert "Yerba 1kg" in filas[0]["descripcion"]
    assert filas[0]["usuario"] == "ana"


def test_una_edicion_guarda_el_antes_y_el_despues(repo, log):
    item = _producto(repo)
    repo.save_catalog_item(replace(item, name="Yerba 500g"))

    edicion = [f for f in log.listar() if f["accion"] == EDITAR][0]
    assert edicion["cambios"]["name"] == ["Yerba 1kg", "Yerba 500g"]
    assert edicion["entidad_id"] == item.id


def test_guardar_sin_cambiar_nada_no_inventa_un_diff(repo, log):
    """No se descarta la fila —el motor no puede saber si el usuario apreto
    guardar a proposito—, pero `cambios` tiene que quedar vacio en vez de
    listar todos los campos."""
    item = _producto(repo)
    repo.save_catalog_item(item)

    edicion = [f for f in log.listar() if f["accion"] == EDITAR][0]
    assert not edicion["cambios"]


def test_el_ledger_de_stock_siempre_es_un_alta(repo, log):
    """Un movimiento de stock nunca se edita: se agrega otro que lo compensa.
    Registrar "editar" sobre un ledger seria mentir sobre lo que paso."""
    item = _producto(repo)
    ubicacion = repo.save_location(Location(None, "Deposito"))
    repo.append_stock_movement(StockMovement(
        None, item.id, ubicacion.id, StockMovementType.ADJUSTMENT,
        Decimal("10"), datetime(2026, 8, 5, 10, 0),
    ))

    movimientos = [f for f in log.listar() if f["entidad"] == "movimiento de stock"]
    assert len(movimientos) == 1
    assert movimientos[0]["accion"] == CREAR


def test_una_coleccion_anidada_se_resume_en_vez_de_volcarse(repo, log):
    """El diff de una tupla de dataclasses no lo lee nadie. Lo que importa es
    que el log diga que los items cambiaron, no que los imprima."""
    lista = repo.save_price_list(PriceList(None, "Mostrador"))
    repo.save_price_list(replace(lista, name="Mayorista"))

    edicion = [f for f in log.listar() if f["accion"] == EDITAR][0]
    assert edicion["cambios"]["name"] == ["Mostrador", "Mayorista"]


# ── Secretos ──────────────────────────────────────────────────────────────

def test_un_setting_secreto_deja_rastro_sin_el_valor(repo, log):
    """Mismo criterio que `libraauth` v0.12.0: la fila se registra igual y lo
    unico que se tapa es el valor. Un log que descarta la edicion entera no
    oculta el secreto — oculta el hecho de que alguien lo cambio."""
    repo.set_setting("api_key", "secreto-viejo")
    repo.set_setting("api_key", "secreto-nuevo")

    filas = [f for f in log.listar() if f["entidad"] == "configuracion"]
    assert len(filas) == 2
    assert filas[0]["cambios"] == {"api_key": [OCULTO, OCULTO]}
    assert "secreto" not in str(filas)


def test_guardar_el_mismo_setting_no_registra_nada(repo, log):
    repo.set_setting("balanza_formato", "ean13")
    repo.set_setting("balanza_formato", "ean13")

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
    envuelto = RepositorioAuditado(desnudo, conn)

    party = envuelto.save_party(Party(None, PartyType.PERSON, "Ana"))
    assert party.id is not None
    assert desnudo.get_party(party.id) == party


def test_sin_usuario_la_fila_queda_a_nombre_del_sistema(conn, log):
    """El caso de un script o una tarea programada, que escriben sin request."""
    repo = RepositorioAuditado(SqliteCommerceRepository(conn), conn)
    _producto(repo)

    assert log.listar()[0]["usuario"] == "Sistema"


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
