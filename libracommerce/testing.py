"""Ayudas de prueba para quien monta routers de este motor (ADR-028). Se importan desde la suite de un producto, no desde la app.

Hoy una sola: `campos_numericos_que_aceptan_booleano(app)`, la guardia que cierra lo que ADR-026 y ADR-027 arreglaron a mano. Pydantic convierte `true` en `1` y `false` en `0` en un
campo `int`/`float` (en el modo laxo que usa FastAPI) antes de que el motor, que en varios lugares rechaza el booleano a propósito, llegue a verlo. `web/_validacion.sin_booleanos` lo evita
en cada campo, pero nada avisaba de un campo nuevo, de un router propio del producto o de un payload heredado al que se le sumó un número y no se le puso. Esta función lo mide en vez de
confiar en que alguien se acuerde: recorre las rutas reales de la app, instancia el modelo real (con sus validadores) con `True`/`False` en cada hoja numérica y devuelve las que lo aceptan.

Necesita el extra `[web]` (FastAPI y pydantic), como el resto de la capa `web/`.
"""

from __future__ import annotations

import collections.abc
import datetime
import decimal
import types
import typing

from .web import fastapi as _fastapi

_fastapi()
from fastapi import params as _params  # noqa: E402
from fastapi import routing as _routing  # noqa: E402
from pydantic import BaseModel, TypeAdapter, ValidationError  # noqa: E402

#: Los tipos que pueden convertir un booleano en número. `bool` no entra (es un booleano de verdad), ni `Literal` ni `str`: no son hojas numéricas.
_NUMEROS = (int, float, decimal.Decimal)
_SECUENCIAS = (list, set, frozenset, tuple, collections.abc.Sequence, collections.abc.Set, collections.abc.Iterable)
_MAPAS = (dict, collections.abc.Mapping)
_METODOS_QUE_NO_CUENTAN = {"HEAD", "OPTIONS"}
#: (lo que se prueba, el número en que se convertiría si pasa). Un cuerpo JSON trae `true`/`false`; query, path, header, cookie y formulario llegan como texto.
_JSON = ((True, 1), (False, 0))
_TEXTO = (("true", "1"),)


def _pelar(ann):
    """El tipo de adentro de un `Annotated` (las restricciones las conserva el modelo real, que es el que se mide)."""
    while typing.get_origin(ann) is typing.Annotated:
        ann = typing.get_args(ann)[0]
    return ann


def _es_modelo(ann) -> bool:
    return isinstance(ann, type) and issubclass(ann, BaseModel)


def _es_numero(ann) -> bool:
    return isinstance(ann, type) and issubclass(ann, _NUMEROS) and not issubclass(ann, bool)


def _clave(nombre: str, campo) -> str:
    """El nombre con que el campo entra en un cuerpo: su alias de validación, si lo tiene."""
    alias = campo.validation_alias if isinstance(campo.validation_alias, str) else campo.alias
    return alias or nombre


def _dummy(ann, hondo: int = 0):
    """Un valor válido a ojo para `ann`, sólo para que los campos requeridos de al lado no tapen los validadores del modelo. Si no sabe armarlo devuelve `None` (el campo queda
    inválido en las dos mediciones por igual y no cambia el resultado: ver `_errores`)."""
    ann = _pelar(ann)
    origen, args = typing.get_origin(ann), typing.get_args(ann)
    if hondo > 6:
        return None
    if origen in (typing.Union, types.UnionType):
        miembros = [a for a in args if a is not type(None)]
        return _dummy(miembros[0], hondo + 1) if miembros else None
    if origen is typing.Literal:
        return args[0]
    if origen in _SECUENCIAS and args:
        return [_dummy(args[0], hondo + 1)]
    if origen in _MAPAS and len(args) == 2:
        return {"1": _dummy(args[1], hondo + 1)}
    if _es_modelo(ann):
        return {_clave(n, f): _dummy(f.annotation, hondo + 1) for n, f in ann.model_fields.items() if f.is_required()}
    if ann is bool:
        return True
    if _es_numero(ann):
        return 1
    if ann is str:
        return "x"
    if ann is datetime.datetime:
        return datetime.datetime(2030, 1, 1)
    if ann is datetime.date:
        return datetime.date(2030, 1, 1)
    return None


def _hojas(ann, armar, ruta: str = "", vistos: tuple = ()):
    """Las hojas numéricas de `ann`: `(ruta del campo, nombre del tipo, armar)`, donde `armar(valor)` devuelve el valor completo de la raíz con `valor` en esa hoja.
    Baja por modelos anidados, `list`/`set`/`tuple[T, ...]`, `dict` (el valor; la clave de un JSON es siempre texto) y uniones. Un tipo que no conoce (`Any`, una tupla de largo fijo,
    una dataclass) no se mide."""
    ann = _pelar(ann)
    origen, args = typing.get_origin(ann), typing.get_args(ann)
    if origen in (typing.Union, types.UnionType):
        for miembro in args:
            if miembro is not type(None):
                yield from _hojas(miembro, armar, ruta, vistos)
    elif _es_modelo(ann):
        if ann in vistos:
            return
        for nombre, campo in ann.model_fields.items():
            def adentro(valor, nombre=nombre, campo=campo, ann=ann):
                base = _dummy(ann)
                base[_clave(nombre, campo)] = valor
                return armar(base)
            yield from _hojas(campo.annotation, adentro, f"{ruta}.{nombre}" if ruta else nombre, (*vistos, ann))
    elif origen in _SECUENCIAS and args and (origen is not tuple or (len(args) == 2 and args[1] is Ellipsis)):
        yield from _hojas(args[0], lambda valor: armar([valor]), f"{ruta}[]", vistos)
    elif origen in _MAPAS and len(args) == 2:
        yield from _hojas(args[1], lambda valor: armar({"1": valor}), f"{ruta}{{valor}}", vistos)
    elif _es_numero(ann):
        yield ruta, ann.__name__, armar


def _errores(adaptador: TypeAdapter, valor) -> frozenset:
    try:
        adaptador.validate_python(valor)
    except ValidationError as e:
        return frozenset((tuple(x["loc"]), x["type"]) for x in e.errors())
    return frozenset()


def _se_convierte(adaptador: TypeAdapter, armar, valor, equivalente) -> bool:
    """`valor` (un booleano o el texto «true») pasa si la validación da **lo mismo** que con el número en que se convierte (`True` como `1`, `False` como `0`): ni un error más, ni uno menos.
    No se pide que el modelo entero sea válido: lo de al lado que no se supo armar falla igual en las dos medidas y no cuenta. Un campo `ge=2` que recibe `true` falla por el rango, igual que
    un `1`: el booleano se convirtió en número, no se lo rechazó por ser booleano, y es justo lo que la guardia busca."""
    return _errores(adaptador, armar(valor)) == _errores(adaptador, armar(equivalente))


def _rutas(rutas, prefijo: str = ""):
    """`(metodo, ruta, dependant)` de cada ruta con sus métodos, con el prefijo de su `include_router` y también dentro de un `Mount` (una sub-aplicación). FastAPI nuevo (0.141 o más) ya
    no aplana `app.routes` al incluir un router: deja un `_IncludedRouter` perezoso, y las rutas con su prefijo y sus dependencias salen de `fastapi.routing.iter_route_contexts` (lo mismo que
    usa para armar el OpenAPI). En una versión sin esa función, `app.routes` ya es plano."""
    iterar = getattr(_routing, "iter_route_contexts", None)
    for r in (iterar(rutas) if iterar else rutas):
        original = getattr(r, "original_route", r)
        ruta = f"{prefijo}{getattr(r, 'path', None) or getattr(original, 'path', '') or ''}"
        if hasattr(original, "dependant"):
            for metodo in sorted((getattr(r, "methods", None) or set()) - _METODOS_QUE_NO_CUENTAN):
                yield metodo, ruta, r.dependant
            continue
        interna = getattr(original, "routes", None) or getattr(getattr(original, "app", None), "routes", None)
        if interna:
            yield from _rutas(interna, ruta)


def _dependants(dependant):
    """El `dependant` de la ruta y los de sus `Depends`: un parámetro de una dependencia (paginación, filtros) llega a la ruta igual."""
    yield dependant
    for sub in getattr(dependant, "dependencies", ()):
        yield from _dependants(sub)


def _parametros(dependant):
    """`(texto, parametro)`: `texto` es True para lo que llega como texto (query, path, header, cookie, formulario) y False para el cuerpo JSON."""
    for dep in _dependants(dependant):
        for p in (*dep.query_params, *dep.path_params, *dep.header_params, *dep.cookie_params):
            yield True, p
        for p in dep.body_params:
            yield isinstance(p.field_info, _params.Form), p


def _ignorados(ignorar) -> set[tuple[str, str]]:
    resultado = set()
    for par in ignorar:
        if not (isinstance(par, tuple) and len(par) == 2 and all(isinstance(x, str) for x in par)):
            raise ValueError(f"`ignorar` es un conjunto de (ruta, campo), las dos de texto; llegó {par!r}")
        resultado.add(par)
    return resultado


def campos_numericos_que_aceptan_booleano(app, *, ignorar=frozenset()) -> list[tuple[str, str, str]]:
    """Los campos numéricos de `app` que **siguen aceptando** un booleano (ADR-028). Una lista vacía es lo que se espera: `assert campos_numericos_que_aceptan_booleano(app) == []`.

    `app` es una `FastAPI` ya armada (la `create_app()` completa del producto, con todos sus routers y los del motor) o un `APIRouter`. Recorre cada ruta (y lo que cuelga de un `Mount` y de sus
    `Depends`) y, para cada hoja numérica (`int`, `float`, `Decimal`, también dentro de modelos anidados, `list[...]`, `dict[...]` y uniones), **instancia el modelo real** con `True` y con `False` en
    esa posición, con sus validadores: si el resultado es el mismo que con `1` y con `0` el booleano pasó como número y el campo se informa. En query, path, header, cookie y formulario, que llegan
    como texto, se prueba `"true"` contra `"1"`.

    Cada elemento es `(metodo y ruta, campo, tipo)`: `("POST /api/ventas", "items[].qty", "float")`, con el campo en notación de punto (`[]` lista, `{valor}` el valor de un dict; con más de un
    cuerpo en la ruta, el nombre del parámetro va delante). La lista sale ordenada y sin repetidos.

    Lo que **no** informa, a propósito: `bool`, `StrictBool`, `StrictInt`/`StrictFloat` y `Literal` (no son hojas numéricas o ya rechazan el booleano), un `Decimal` mientras pydantic lo rechace solo
    (se mide igual: si una versión futura lo aceptara, aparecería), un campo con `sin_booleanos`, y las claves de un diccionario (en JSON son siempre texto). No mide un `Any`, una tupla de largo fijo
    ni una dataclass. Límite: un validador de modelo posterior (`model_validator(mode="after")`) que rechace el booleano sólo se ve si el resto del modelo se pudo armar con valores de relleno.

    `ignorar` es un conjunto de `(ruta, campo)` que se aceptan a sabiendas, con la `ruta` como la devuelve la función (`"POST /api/ventas"`) o sin el método (`"/api/ventas"`, vale para todos). Un campo
    donde un `1` o un `0` no cambian nada (un orden de pantalla, un contador sin efecto) es la excepción razonable: **cada uno se justifica en un comentario al lado**, no es un atajo para tapar un
    informe. Una entrada que no coincide con nada no avisa."""
    ignorados = _ignorados(ignorar)
    encontrados = set()
    for metodo, ruta, dependant in _rutas(app.routes):
        sitio = f"{metodo} {ruta}"
        parametros = list(_parametros(dependant))
        varios_cuerpos = sum(1 for texto, _ in parametros if not texto) > 1
        for texto, p in parametros:
            raiz = _tipo_del_parametro(p)
            adaptador = None
            for campo_ruta, tipo, armar in _hojas(p.field_info.annotation, lambda valor: valor):
                if adaptador is None:
                    adaptador = TypeAdapter(raiz)
                campo = _nombre_del_campo(p.name, campo_ruta, varios_cuerpos and not texto)
                if (sitio, campo) in ignorados or (ruta, campo) in ignorados:
                    continue
                if any(_se_convierte(adaptador, armar, malo, equivalente) for malo, equivalente in (_TEXTO if texto else _JSON)):
                    encontrados.add((sitio, campo, tipo))
    return sorted(encontrados)


def _tipo_del_parametro(p):
    """El tipo del parámetro con sus restricciones (`Query(gt=0)`, `Body(...)`), para validar igual que FastAPI."""
    ann = p.field_info.annotation
    metadata = tuple(getattr(p.field_info, "metadata", ()) or ())
    return typing.Annotated[(ann, *metadata)] if metadata else ann


def _nombre_del_campo(parametro: str, ruta: str, con_prefijo: bool) -> str:
    """`ruta` es relativa al modelo del parámetro; si no dice nada (un número suelto) o empieza por `[`/`{`, el nombre del parámetro va delante, y siempre con más de un cuerpo."""
    if not (con_prefijo or not ruta or ruta[0] in "[{"):
        return ruta
    return f"{parametro}{ruta}" if not ruta or ruta[0] in "[{" else f"{parametro}.{ruta}"
