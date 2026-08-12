"""Ejes de variante sugeridos segun el rubro del comercio.

Un consumible se describe distinto en cada vertical: el shampoo de una
peluqueria se distingue por volumen y tono, el cable de un service de
telefonia por categoria y longitud, la harina de una cocina por presentacion.
El motor ya sabia guardar eso --`ItemVariant.attributes` es un dict libre que
viaja a `item_variants.attributes_json`--, pero no sabia **proponerlo**, asi
que cada instancia inventaba sus propios nombres de atributo y despues ningun
reporte transversal cerraba.

**Estos presets sugieren; no validan nada.** Es la decision explicita del
2026-08-11: practico y flexible antes que consistente. Un atributo que no
figura en ningun eje se guarda igual, y `ejes_visibles()` existe justamente
para que ese atributo no desaparezca del formulario la proxima vez que alguien
edite el item.

Lo que se valida es el **codigo de rubro** de la instancia (ver
`usecases/presets.py`), porque un rubro mal escrito no rompe nada: simplemente
no sugiere nada, y eso es indistinguible de "todavia no lo configuraron".
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class EjeDeVariante:
    """Un atributo por el que un item se abre en variantes.

    `clave` es lo que termina como key en `attributes_json`, asi que conviene
    que sea estable: cambiarla no migra los datos ya cargados. `ejemplos` son
    valores para ofrecer, **no una lista cerrada**.
    """

    clave: str
    etiqueta: str
    ejemplos: tuple[str, ...] = ()


@dataclass(frozen=True)
class RubroPreset:
    codigo: str
    nombre: str
    ejes: tuple[EjeDeVariante, ...]


#: Rubro de una instancia que todavia no eligio ninguno.
RUBRO_POR_DEFECTO = "generico"


_MARCA = EjeDeVariante("marca", "Marca")
_PRESENTACION = EjeDeVariante(
    "presentacion", "Presentacion", ("100 g", "500 g", "1 kg", "5 kg")
)

#: Los rubros salen de los verticales que la familia ya atiende, no de una
#: taxonomia inventada: gastronomia (Restolibra), retail (VentaLibra),
#: estetica y taller (Gestiolibra), salud (MedLibra), telecomunicaciones
#: (LibraDesk/Lagrace) y generico (Contalibra, que es transversal).
PRESETS: dict[str, RubroPreset] = {
    "generico": RubroPreset(
        "generico",
        "Generico",
        (_MARCA, _PRESENTACION),
    ),
    "gastronomia": RubroPreset(
        "gastronomia",
        "Gastronomia",
        (
            _MARCA,
            _PRESENTACION,
            EjeDeVariante("unidad_de_venta", "Unidad de venta", ("unidad", "docena", "caja")),
        ),
    ),
    "retail": RubroPreset(
        "retail",
        "Retail / autoservicio",
        (
            _MARCA,
            _PRESENTACION,
            EjeDeVariante("sabor", "Sabor / variedad"),
            EjeDeVariante("talle", "Talle", ("S", "M", "L", "XL")),
            EjeDeVariante("color", "Color"),
        ),
    ),
    "estetica": RubroPreset(
        "estetica",
        "Estetica y peluqueria",
        (
            _MARCA,
            EjeDeVariante("volumen", "Volumen", ("250 ml", "500 ml", "1 L")),
            EjeDeVariante("tono", "Tono"),
        ),
    ),
    "taller": RubroPreset(
        "taller",
        "Taller y reparaciones",
        (
            _MARCA,
            EjeDeVariante("medida", "Medida"),
            EjeDeVariante("material", "Material"),
        ),
    ),
    "salud": RubroPreset(
        "salud",
        "Salud",
        (
            _MARCA,
            _PRESENTACION,
            # Solo describe el envase. La posologia no es un atributo de
            # catalogo y no tiene por que salir sugerida en un formulario de
            # producto.
            EjeDeVariante("concentracion", "Concentracion"),
        ),
    ),
    "telecomunicaciones": RubroPreset(
        "telecomunicaciones",
        "Telecomunicaciones y redes",
        (
            _MARCA,
            EjeDeVariante("categoria", "Categoria", ("Cat 5e", "Cat 6", "Cat 6A")),
            EjeDeVariante("longitud", "Longitud", ("0,5 m", "1 m", "3 m", "305 m")),
            EjeDeVariante("color", "Color"),
        ),
    ),
}


def listar_rubros() -> tuple[RubroPreset, ...]:
    """Los rubros disponibles, para que una pantalla los ofrezca."""
    return tuple(PRESETS.values())


def preset_de(codigo: str) -> RubroPreset | None:
    """El preset de un rubro, o `None` si el codigo no existe.

    Devuelve `None` en vez de caer al generico a proposito: un codigo
    desconocido casi siempre es un error de escritura, y taparlo con
    sugerencias plausibles lo vuelve invisible.
    """
    return PRESETS.get(codigo)


def ejes_visibles(
    codigo: str, atributos: dict[str, str] | None = None
) -> tuple[EjeDeVariante, ...]:
    """Los ejes que un formulario tiene que mostrar para estos atributos.

    Son los del rubro **mas** los que el item ya tiene cargados y el preset no
    contempla. Sin esa segunda mitad, un atributo escrito a mano
    --exactamente lo que la flexibilidad prometida habilita-- desapareceria de
    la pantalla la proxima vez que alguien edite el item, y el dato quedaria
    guardado pero invisible: peor que no haberlo permitido.

    El orden es estable: primero los del rubro, despues los propios en el
    orden en que vinieron, para que la pantalla no se reordene sola entre
    ediciones.
    """
    preset = preset_de(codigo)
    ejes = list(preset.ejes) if preset is not None else []
    conocidas = {eje.clave for eje in ejes}
    for clave in (atributos or {}):
        if clave not in conocidas:
            ejes.append(EjeDeVariante(clave, clave))
            conocidas.add(clave)
    return tuple(ejes)
