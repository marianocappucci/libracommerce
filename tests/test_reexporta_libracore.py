"""`sin_booleanos`, `rechazar_booleanos` y la guardia de booleanos son de libracore; este motor los reexporta (ADR-030).

(1) **Misma identidad** (`is`, no «se parecen»): lo que importa un producto o un router de acá es el objeto de libracore, así que un arreglo allá llega acá sin tocar nada. (2) **Los routers**
siguen tomando `sin_booleanos` de `._validacion` (el camino de ADR-026/027) y no de otro lado. (3) **Sin libracore, o con uno anterior a v1.125.0, el error es claro**: `SinLibracore` dice qué extra
falta y es un `ImportError` que viene `from` el original, no un `ModuleNotFoundError` suelto; y el núcleo del motor (sin `web`) se importa igual, porque `dependencies = []` no cambió.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

import libracore.testing  # noqa: E402
import libracore.validacion  # noqa: E402

import libracommerce.testing  # noqa: E402
import libracommerce.web._validacion  # noqa: E402
from libracommerce.web import SinLibracore  # noqa: E402

RAIZ = Path(__file__).resolve().parent.parent
_ROUTERS = ("catalogo", "compras", "listas", "promociones", "reposicion", "vencimientos", "ventas")


def test_sin_booleanos_y_rechazar_booleanos_son_los_de_libracore():
    assert libracommerce.web._validacion.sin_booleanos is libracore.validacion.sin_booleanos
    assert libracommerce.web._validacion.rechazar_booleanos is libracore.validacion.rechazar_booleanos
    assert libracommerce.web._validacion.__all__ == ["rechazar_booleanos", "sin_booleanos"]


def test_la_guardia_es_la_de_libracore():
    assert libracommerce.testing.campos_numericos_que_aceptan_booleano is libracore.testing.campos_numericos_que_aceptan_booleano
    assert libracommerce.testing.__all__ == ["campos_numericos_que_aceptan_booleano"]


@pytest.mark.parametrize("modulo", _ROUTERS)
def test_cada_router_sigue_tomando_sin_booleanos_de_su_modulo_de_validacion(modulo):
    """Un router que importara otra copia (o la suya) no se enteraría del arreglo de libracore: lo que se compara es el objeto que tiene en su espacio de nombres."""
    router = __import__(f"libracommerce.web.{modulo}_router", fromlist=["sin_booleanos"])
    assert router.sin_booleanos is libracore.validacion.sin_booleanos


def test_el_mensaje_es_el_de_siempre():
    """El 422 que ven los productos y la UI no cambió con el reexport (ADR-026)."""
    from pydantic import BaseModel, ValidationError

    class Cuerpo(BaseModel):
        proveedor_id: int
        _no_son_booleanos = libracommerce.web._validacion.sin_booleanos("proveedor_id")

    with pytest.raises(ValidationError, match="proveedor_id tiene que ser un número, no un booleano"):
        Cuerpo(proveedor_id=True)
    assert Cuerpo(proveedor_id="2").proveedor_id == 2


def _en_otro_proceso(codigo: str) -> subprocess.CompletedProcess:
    entorno = {**os.environ, "PYTHONPATH": os.pathsep.join([str(RAIZ), os.environ.get("PYTHONPATH", "")]), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, "-c", codigo], capture_output=True, text=True, cwd=RAIZ, env=entorno, timeout=120)


_CODIGO_SIN_LIBRACORE = """
import importlib, sys
{bloqueo}
import libracommerce  # el núcleo (sin `web`) se importa igual: `dependencies = []`
from libracommerce.web import SinLibracore
for nombre in ("libracommerce.testing", "libracommerce.web._validacion", "libracommerce.web.catalogo_router"):
    try:
        importlib.import_module(nombre)
    except SinLibracore as e:
        assert isinstance(e, ImportError)
        assert isinstance(e.__cause__, ImportError), repr(e.__cause__)
        assert "libracommerce[web] necesita libracore>=1.125" in str(e), str(e)
        assert "pip install libracommerce[web]" in str(e), str(e)
    else:
        raise SystemExit("no levantó SinLibracore: " + nombre)
print("ok")
"""


@pytest.mark.parametrize(
    "bloqueo",
    [
        pytest.param("sys.modules['libracore'] = None", id="sin-libracore"),
        pytest.param("sys.modules['libracore.validacion'] = None; sys.modules['libracore.testing'] = None", id="libracore-anterior-a-1.125"),
    ],
)
def test_sin_libracore_importar_la_capa_web_da_un_error_claro(bloqueo):
    r = _en_otro_proceso(_CODIGO_SIN_LIBRACORE.format(bloqueo=bloqueo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip().splitlines()[-1] == "ok"


def test_sin_libracore_el_error_que_ve_el_usuario_nombra_el_extra():
    """Lo que se lee en la terminal de quien importa sin libracore: el extra, no una traza de `ModuleNotFoundError` sola."""
    r = _en_otro_proceso("import sys; sys.modules['libracore'] = None; import libracommerce.testing")
    assert r.returncode != 0
    assert "libracommerce.web.SinLibracore: libracommerce[web] necesita libracore>=1.125" in r.stderr, r.stderr
    assert SinLibracore.__mro__[1] is ImportError
