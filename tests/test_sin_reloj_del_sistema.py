"""Guard: el motor no lee la hora del sistema por su cuenta (ADR-052).

`date.today()`, `datetime.now()` sin zona, `datetime.utcnow()`, `time.localtime()` y los relojes de SQL (`CURRENT_TIMESTAMP`,
`datetime('now')` sin modificador) toman la zona del servidor: de 21 a 24 hs de Argentina un servidor en UTC da el día siguiente. Todo
«ahora» y «hoy» sale de `libracommerce.hora` (o de `vencimientos.hoy_argentina`, que lo usa). Un instante CON zona
(`datetime.now(UTC)`) no se marca: no depende de la zona del proceso.

Este test recorre el paquete entero y falla con la lista de lo que encontró. Si hace falta una excepción, va en `EXCEPCIONES` con su
motivo, y el test también falla cuando una excepción ya no hace falta."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import libracommerce

PAQUETE = Path(libracommerce.__file__).parent

#: `{"ruta/relativa.py:llamada": "motivo"}`. Hoy no hay ninguna: las llamadas con zona no se marcan y el resto sale de `hora.py`.
EXCEPCIONES: dict[str, str] = {}

_LLAMADAS_SIN_ZONA = {"today", "utcnow", "localtime", "gmtime"}
_RELOJ_DE_SQL = re.compile(r"CURRENT_(TIMESTAMP|DATE|TIME)\b|\b(datetime|date|time)\(\s*'now'\s*\)", re.IGNORECASE)


def _docstrings(arbol: ast.AST) -> set[int]:
    ids = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            cuerpo = nodo.body
            if cuerpo and isinstance(cuerpo[0], ast.Expr) and isinstance(cuerpo[0].value, ast.Constant):
                ids.add(id(cuerpo[0].value))
    return ids


def hallazgos_de(fuente: str, nombre: str) -> list[str]:
    """Las lecturas del reloj del sistema que hay en `fuente`, como `nombre:línea: texto`."""
    arbol = ast.parse(fuente)
    docs = _docstrings(arbol)
    lineas = fuente.splitlines()
    encontrados = []
    for nodo in ast.walk(arbol):
        malo = False
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute):
            if nodo.func.attr in _LLAMADAS_SIN_ZONA:
                malo = True
            elif nodo.func.attr == "now":
                sin_zona = not nodo.args and not nodo.keywords
                zona_none = (len(nodo.args) == 1 and isinstance(nodo.args[0], ast.Constant) and nodo.args[0].value is None)
                malo = sin_zona or zona_none
        elif isinstance(nodo, ast.Constant) and isinstance(nodo.value, str) and id(nodo) not in docs:
            malo = bool(_RELOJ_DE_SQL.search(nodo.value))
        if malo:
            encontrados.append(f"{nombre}:{nodo.lineno}: {lineas[nodo.lineno - 1].strip()[:110]}")
    return encontrados


def _del_paquete() -> list[str]:
    todos = []
    for ruta in sorted(PAQUETE.rglob("*.py")):
        todos += hallazgos_de(ruta.read_text(encoding="utf-8"), ruta.relative_to(PAQUETE.parent).as_posix())
    return todos


def test_el_motor_no_lee_la_hora_del_sistema():
    permitidos = tuple(EXCEPCIONES)
    nuevos = [h for h in _del_paquete() if not any(h.startswith(p.split(":")[0] + ":") and p.split(":", 1)[1] in h for p in permitidos)]
    assert not nuevos, (
        "Estas lecturas toman la zona del servidor (un servidor en UTC corre el día de 21 a 24 hs de Argentina). Usá "
        "`libracommerce.hora` (`ahora_local()`, `hoy_local()`) o `vencimientos.hoy_argentina()`:\n" + "\n".join(nuevos))


def test_las_excepciones_siguen_haciendo_falta():
    actuales = _del_paquete()
    sobran = [p for p in EXCEPCIONES if not any(h.startswith(p.split(":")[0] + ":") and p.split(":", 1)[1] in h for h in actuales)]
    assert not sobran, f"Excepciones que ya no se usan, borrarlas: {sobran}"


# ── El guard mismo: tiene que ponerse rojo con lo que dice que caza ──────────────────────────────────────────────────


def test_el_guard_caza_cada_forma_de_leer_la_hora_del_sistema():
    malos = [
        "import datetime\nhoy = datetime.date.today()",
        "from datetime import date\nhoy = date.today()",
        "from datetime import datetime\nahora = datetime.now()",
        "import datetime\nahora = datetime.datetime.now()",
        "from datetime import datetime\nahora = datetime.now(None)",
        "from datetime import datetime\nahora = datetime.utcnow()",
        "import time\nt = time.localtime()",
        "conn.execute(\"UPDATE t SET updated_at = CURRENT_TIMESTAMP\")",
        "conn.execute(\"SELECT datetime('now')\")",
        "conn.execute(\"SELECT date('now')\")",
    ]
    for fuente in malos:
        assert hallazgos_de(fuente, "x.py"), fuente


def test_el_guard_deja_pasar_lo_que_lleva_zona_o_es_texto_de_documentacion():
    buenos = [
        "from datetime import datetime, UTC\nahora = datetime.now(UTC)",
        "from datetime import datetime\nahora = datetime.now(tz=zona)",
        "conn.execute(\"INSERT INTO t (c) VALUES (datetime('now','-3 hours'))\")",
        'def f():\n    """Antes usaba CURRENT_TIMESTAMP y date.today()."""\n    return 1',
        "# datetime.now() en un comentario\nx = 1",
    ]
    for fuente in buenos:
        assert not hallazgos_de(fuente, "x.py"), fuente
