"""Presets de variante por rubro.

Los tests que importan son los que fijan la promesa: **sugieren, no validan**.
Si alguno de esos se pone en rojo, lo que cambio no es un detalle sino la
decision de producto.
"""

import sqlite3

import pytest

from libracommerce.db.repository import SqliteCommerceRepository
from libracommerce.db.schema import init_schema
from libracommerce.domain.catalog import CatalogItem, CatalogItemType, ItemVariant, Unit
from libracommerce.domain.presets import (
    PRESETS,
    RUBRO_POR_DEFECTO,
    ejes_visibles,
    listar_rubros,
    preset_de,
)
from libracommerce.usecases.presets import (
    CLAVE_RUBRO,
    RubroDesconocidoError,
    ejes_para,
    fijar_rubro,
    leer_rubro,
)


@pytest.fixture
def repo() -> SqliteCommerceRepository:
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    return SqliteCommerceRepository(conn)


# ── La promesa: sugieren, no validan ─────────────────────────────────────


def test_se_guarda_un_atributo_que_ningun_preset_contempla(repo):
    """El corazon de la decision: flexible antes que consistente.

    Si esto se pone en rojo, alguien convirtio los presets en un esquema
    cerrado y eso es un cambio de producto, no un refactor.
    """
    fijar_rubro(repo, "estetica")
    item = repo.save_catalog_item(
        CatalogItem(None, CatalogItemType.PRODUCT, "Shampoo", Unit("u", "Unidad"))
    )

    guardada = repo.save_item_variant(
        ItemVariant(
            None, item.id, "SH-ORG-500", "Shampoo organico 500 ml",
            attributes={"volumen": "500 ml", "certificacion_organica": "ECOCERT"},
        )
    )

    releida = repo.get_item_variant(guardada.id)
    assert releida.attributes["certificacion_organica"] == "ECOCERT"


def test_un_atributo_propio_sigue_visible_en_el_formulario(repo):
    """Sin esto el dato queda guardado pero invisible, que es peor.

    El usuario agrega un eje a mano; al reabrir el item, la pantalla tiene que
    seguir mostrandolo aunque el preset del rubro no lo conozca.
    """
    fijar_rubro(repo, "estetica")
    atributos = {"volumen": "500 ml", "certificacion_organica": "ECOCERT"}

    claves = [eje.clave for eje in ejes_para(repo, atributos)]

    assert "certificacion_organica" in claves
    assert "volumen" in claves


def test_los_ejes_del_rubro_van_antes_que_los_propios(repo):
    """Orden estable: la pantalla no se puede reordenar sola entre ediciones."""
    fijar_rubro(repo, "telecomunicaciones")

    claves = [eje.clave for eje in ejes_para(repo, {"origen": "importado"})]

    assert claves[: len(PRESETS["telecomunicaciones"].ejes)] == [
        eje.clave for eje in PRESETS["telecomunicaciones"].ejes
    ]
    assert claves[-1] == "origen"


def test_un_atributo_que_el_preset_ya_tiene_no_se_duplica():
    claves = [eje.clave for eje in ejes_visibles("estetica", {"volumen": "250 ml"})]

    assert claves.count("volumen") == 1


# ── El rubro de la instancia ─────────────────────────────────────────────


def test_una_instancia_sin_rubro_cae_al_generico(repo):
    """Es el estado de todas las instancias que ya existen."""
    assert leer_rubro(repo) == RUBRO_POR_DEFECTO
    assert preset_de(leer_rubro(repo)) is not None


def test_fijar_y_leer_el_rubro(repo):
    fijar_rubro(repo, "gastronomia")

    assert leer_rubro(repo) == "gastronomia"
    assert repo.get_setting(CLAVE_RUBRO) == "gastronomia"


def test_un_rubro_inexistente_se_rechaza_al_configurarlo(repo):
    """Se valida el rubro aunque los atributos sean libres: un codigo mal
    escrito no rompe nada visible, y por eso hay que atajarlo al escribirlo."""
    with pytest.raises(RubroDesconocidoError, match="peluqueriaa"):
        fijar_rubro(repo, "peluqueriaa")

    assert leer_rubro(repo) == RUBRO_POR_DEFECTO


def test_un_rubro_desconocido_no_cae_al_generico_en_silencio():
    """`preset_de` devuelve None: taparlo volveria invisible el error."""
    assert preset_de("no_existe") is None
    assert ejes_visibles("no_existe") == ()


def test_un_rubro_desconocido_igual_muestra_los_atributos_cargados():
    """Aun con el rubro roto, el dato que ya existe no puede desaparecer."""
    claves = [eje.clave for eje in ejes_visibles("no_existe", {"marca": "Furukawa"})]

    assert claves == ["marca"]


# ── Integridad del catalogo de rubros ────────────────────────────────────


def test_el_codigo_de_cada_preset_coincide_con_su_clave():
    """Si divergen, `listar_rubros()` ofrece un codigo que `preset_de` no
    encuentra, y la pantalla queda ofreciendo una opcion que al elegirla
    falla."""
    for clave, preset in PRESETS.items():
        assert preset.codigo == clave


def test_el_rubro_por_defecto_existe():
    assert preset_de(RUBRO_POR_DEFECTO) is not None


def test_ningun_preset_repite_una_clave_de_eje():
    """Dos ejes con la misma clave se pisarian en `attributes_json`."""
    for preset in listar_rubros():
        claves = [eje.clave for eje in preset.ejes]
        assert len(claves) == len(set(claves)), f"claves repetidas en {preset.codigo}"


def test_todos_los_rubros_sugieren_al_menos_un_eje():
    for preset in listar_rubros():
        assert preset.ejes, f"{preset.codigo} no sugiere nada"
