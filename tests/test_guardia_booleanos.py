"""La guardia contra booleanos en campos numéricos (ADR-028): `libracommerce.testing.campos_numericos_que_aceptan_booleano`.

Tres cosas. (1) **El motor entero está limpio**: se arma una app con las 21 factories de `web/` (con sus opciones prendidas) y la lista de campos que aceptan `true`/`false` tiene que ser
VACÍA; es lo que cada producto afirma sobre su `create_app()`. (2) **La guardia no es muda**: con `sin_booleanos` anulado (en otro proceso, antes de importar los routers) ve los campos que
ADR-027 arregló a mano, así que un `[]` de (1) significa algo. (3) **La función en sí**, con routers de juguete: detecta un campo `int`/`float`/`list[int]`/`dict[str, float]`/anidado sin el
validador, no marca los que lo tienen, ni `bool`, `Decimal`, `StrictInt` ni `Literal`; las excepciones `ignorar`; los parámetros de query y de path; y que recorre `include_router` con prefijo,
los `Mount` y los payloads heredados.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

import pytest

pytest.importorskip("fastapi")

from fastapi import APIRouter, Depends, FastAPI, Query  # noqa: E402
from pydantic import BaseModel, Field, StrictBool, StrictInt, field_validator  # noqa: E402

from libracommerce.testing import campos_numericos_que_aceptan_booleano  # noqa: E402
from libracommerce.web import catalogo_router as C  # noqa: E402
from libracommerce.web import compras_router as CO  # noqa: E402
from libracommerce.web import listas_router as L  # noqa: E402
from libracommerce.web import margen_router as M  # noqa: E402
from libracommerce.web import planillas_router as PL  # noqa: E402
from libracommerce.web import promociones_router as P  # noqa: E402
from libracommerce.web import reposicion_router as R  # noqa: E402
from libracommerce.web import vencimientos_router as V  # noqa: E402
from libracommerce.web import ventas_router as VE  # noqa: E402
from libracommerce.web._validacion import sin_booleanos  # noqa: E402

RAIZ = Path(__file__).resolve().parent.parent


def _usuario():
    return {"id": 1}


_DEP = [Depends(_usuario)]


def _routers_del_motor():
    """Las 21 factories de `web/`, con las opciones que agregan rutas o campos prendidas (`con_vencimientos`, `con_lotes`, `por_deposito`, `promociones`, `con_avisos_de_vencimiento`)."""
    return [
        C.build_productos_router(),
        C.build_productos_router(opciones=C.OpcionesCatalogo(con_vencimientos=True)),
        C.build_depositos_router(),
        C.build_sucursales_router(),
        C.build_stock_router(opciones=C.OpcionesStock(con_lotes=True, por_deposito=True, motivos_merma=("Otro",))),
        VE.build_ventas_router(opciones=VE.OpcionesVentas(con_avisos_de_vencimiento=True, promociones=True)),
        V.build_vencimientos_router(),
        V.build_vencimientos_escritura_router(usuario_actual=_usuario, dependencias_marcar=_DEP, dependencias_movimientos=_DEP),
        L.build_listas_precio_router(),
        L.build_quiebres_router(),
        L.build_precios_vigentes_router(),
        L.build_cliente_lista_router(),
        L.build_buscar_productos_router(),
        CO.build_compras_router(),
        P.build_promociones_router(),
        P.build_promociones_calculo_router(),
        M.build_margen_router(),
        PL.build_actualizacion_precios_router(),
        R.build_reposicion_router(),
        R.build_reposicion_parametros_router(dependencias_escribir=_DEP),
        R.build_reposicion_minimos_router(dependencias_escribir=_DEP),
        R.build_reposicion_ordenes_router(usuario_actual=_usuario, dependencias_escribir=_DEP),
    ]


def _app_del_motor() -> FastAPI:
    app = FastAPI()
    for router in _routers_del_motor():
        app.include_router(router)
    return app


# ═══════════════════════════════════════════════════ (1) El motor entero


def test_las_21_factories_no_aceptan_un_booleano_en_ningun_campo_numerico():
    app = _app_del_motor()
    assert campos_numericos_que_aceptan_booleano(app) == []


def test_la_guardia_ve_los_campos_que_adr_027_arreglo_si_se_anula_sin_booleanos():
    """Sin esto, el `[]` de arriba valdría igual con una guardia que no mide nada. Otro proceso (los routers se importan una vez) con `sin_booleanos` convertido en un no-op: tienen que aparecer
    los campos de ADR-027 y de ADR-026 (reposición)."""
    codigo = (
        "import json\n"
        "import libracommerce.web._validacion as v\n"
        "v.sin_booleanos = lambda *campos: None\n"
        "import sys; sys.path.insert(0, 'tests')\n"
        "from test_guardia_booleanos import _app_del_motor\n"
        "from libracommerce.testing import campos_numericos_que_aceptan_booleano as g\n"
        "print(json.dumps(g(_app_del_motor())))\n"
    )
    entorno = {**os.environ, "PYTHONPATH": os.pathsep.join([str(RAIZ), str(RAIZ / "tests"), os.environ.get("PYTHONPATH", "")]),
               "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([sys.executable, "-c", codigo], capture_output=True, text=True, cwd=RAIZ, env=entorno, timeout=120)
    assert r.returncode == 0, r.stderr
    hallados = {tuple(x) for x in json.loads(r.stdout.strip().splitlines()[-1])}
    for esperado in [
        ("POST /api/ventas", "items[].qty", "float"),
        ("POST /api/ventas", "pagos[].monto", "float"),
        ("POST /api/ventas/{vid}/devolver", "lineas[].sale_item_id", "int"),
        ("PUT /api/listas-precio/{lista_id}/items", "precios{valor}", "float"),
        ("POST /api/reportes/reposicion/ordenes", "producto_ids[]", "int"),
        ("PUT /api/productos/{producto_id}/reposicion", "stock_maximo", "float"),
        ("POST /api/vencimientos/merma", "variante_id", "int"),
        ("POST /api/purchase-orders", "proveedor_id", "int"),
    ]:
        assert esperado in hallados, esperado
    assert len(hallados) >= 70


# ═══════════════════════════════════════════════════ (2) La función, con routers de juguete


class Anidado(BaseModel):
    valor: int


class AnidadoBueno(BaseModel):
    valor: int
    _no_son_booleanos = sin_booleanos("valor")


class Malo(BaseModel):
    """Todo lo que acepta un booleano: int, float, list[int], dict[str, float], un modelo anidado y una lista de modelos."""
    n: int
    f: float = 0
    opcional: int | None = None
    ids: list[int] = []
    precios: dict[str, float] = {}
    hijo: Anidado | None = None
    hijos: list[Anidado] = []
    con_rango: Annotated[int, Field(ge=2)] = 2


class Bueno(BaseModel):
    n: int
    f: float = 0
    opcional: int | None = None
    ids: list[int] = []
    precios: dict[str, float] = {}
    hijo: AnidadoBueno | None = None
    hijos: list[AnidadoBueno] = []
    con_rango: Annotated[int, Field(ge=2)] = 2
    _no_son_booleanos = sin_booleanos("n", "f", "opcional", "ids", "precios", "con_rango")


class QueNoSonNumerosSueltos(BaseModel):
    """Lo que la guardia no tiene que marcar nunca."""
    activo: bool = True
    estricto: StrictBool = True
    entero_estricto: StrictInt = 1
    tipo: Literal["a", "b"] = "a"
    monto: Decimal = Decimal(1)
    texto: str = "x"
    montos: list[Decimal] = []


def _router_malo() -> APIRouter:
    router = APIRouter()

    @router.post("/malo")
    def crear(payload: Malo):
        return {}

    return router


def _router_bueno() -> APIRouter:
    router = APIRouter()

    @router.post("/bueno")
    def crear(payload: Bueno):
        return {}

    @router.post("/otros")
    def otros(payload: QueNoSonNumerosSueltos):
        return {}

    return router


def _app(*routers, prefijos=None) -> FastAPI:
    app = FastAPI()
    for i, router in enumerate(routers):
        app.include_router(router, prefix=(prefijos or {}).get(i, ""))
    return app


def test_un_router_que_acepta_booleanos_se_detecta_campo_por_campo():
    hallados = campos_numericos_que_aceptan_booleano(_app(_router_malo()))
    assert hallados == [
        ("POST /malo", "con_rango", "int"),
        ("POST /malo", "f", "float"),
        ("POST /malo", "hijo.valor", "int"),
        ("POST /malo", "hijos[].valor", "int"),
        ("POST /malo", "ids[]", "int"),
        ("POST /malo", "n", "int"),
        ("POST /malo", "opcional", "int"),
        ("POST /malo", "precios{valor}", "float"),
    ]


def test_un_router_con_sin_booleanos_no_se_marca_y_tampoco_bool_decimal_strict_ni_literal():
    assert campos_numericos_que_aceptan_booleano(_app(_router_bueno())) == []


def test_ignorar_acepta_la_ruta_con_o_sin_metodo_y_no_oculta_lo_demas():
    app = _app(_router_malo())
    todo = campos_numericos_que_aceptan_booleano(app)
    assert ("POST /malo", "n", "int") in todo
    sin_n = campos_numericos_que_aceptan_booleano(app, ignorar={("POST /malo", "n")})
    assert sin_n == [x for x in todo if x[1] != "n"]
    sin_f = campos_numericos_que_aceptan_booleano(app, ignorar={("/malo", "f")})        # sin el método: vale para todos
    assert sin_f == [x for x in todo if x[1] != "f"]
    assert campos_numericos_que_aceptan_booleano(app, ignorar={("GET /malo", "n")}) == todo     # otro método: no coincide


def test_ignorar_mal_formado_es_un_error_y_no_se_ignora_en_silencio():
    app = _app(_router_malo())
    for roto in ({"n"}, {("POST /malo",)}, {("POST /malo", 1)}):
        with pytest.raises(ValueError, match="ignorar"):
            campos_numericos_que_aceptan_booleano(app, ignorar=roto)


def test_el_prefijo_del_include_router_y_un_mount_forman_parte_de_la_ruta():
    app = _app(_router_malo(), prefijos={0: "/api"})
    assert ("POST /api/malo", "n", "int") in campos_numericos_que_aceptan_booleano(app)
    sub = FastAPI()
    sub.include_router(_router_malo())
    raiz = FastAPI()
    raiz.mount("/otra", sub)
    assert ("POST /otra/malo", "n", "int") in campos_numericos_que_aceptan_booleano(raiz)


def test_un_apirouter_suelto_tambien_se_puede_medir():
    assert ("POST /malo", "n", "int") in campos_numericos_que_aceptan_booleano(_router_malo())


class ProductoDelProducto(BaseModel):
    """Lo que hace un producto: hereda el payload del motor y le suma un número propio sin `sin_booleanos`."""
    nombre: str = "x"
    precio_venta: float = 0
    _no_son_booleanos = sin_booleanos("precio_venta")


class ProductoConExtra(ProductoDelProducto):
    orden: int = 0


def test_un_payload_heredado_con_un_campo_nuevo_sin_el_validador_se_detecta():
    router = APIRouter()

    @router.post("/productos")
    def crear(payload: ProductoConExtra):
        return {}

    assert campos_numericos_que_aceptan_booleano(_app(router)) == [("POST /productos", "orden", "int")]
    assert campos_numericos_que_aceptan_booleano(_app(router), ignorar={("POST /productos", "orden")}) == []


class SinElValidadorOtroMecanismo(BaseModel):
    """Rechaza el booleano por su cuenta (no con `sin_booleanos`): tampoco se marca. La guardia mide el resultado, no el mecanismo."""
    cantidad: float

    @field_validator("cantidad", mode="before")
    @classmethod
    def _numero(cls, valor):
        if isinstance(valor, bool):
            raise ValueError("no booleano")
        return valor


def test_se_mide_el_resultado_y_no_el_mecanismo():
    router = APIRouter()

    @router.post("/otro")
    def crear(payload: SinElValidadorOtroMecanismo):
        return {}

    assert campos_numericos_que_aceptan_booleano(_app(router)) == []


def test_query_path_y_cuerpo_suelto_se_miden_con_el_texto_true():
    """Un parámetro numérico de query o de path llega como texto y pydantic no convierte «true» en número: no se marca. Un `Query` con restricciones y una lista de enteros, tampoco."""
    router = APIRouter()

    @router.get("/x/{producto_id}")
    def leer(producto_id: int, limite: Annotated[int, Query(gt=0)] = 10, ids: Annotated[list[int] | None, Query()] = None,
             factor: float = 1.0):
        return {}

    assert campos_numericos_que_aceptan_booleano(_app(router)) == []


def test_un_parametro_de_dependencia_tambien_se_recorre():
    """El cuerpo que declara una dependencia (`Depends`) llega a la ruta igual que el propio."""
    def pagina(payload: Malo):
        return payload

    router = APIRouter()

    @router.post("/con-dependencia")
    def crear(pag=Depends(pagina)):
        return {}

    assert ("POST /con-dependencia", "n", "int") in campos_numericos_que_aceptan_booleano(_app(router))
