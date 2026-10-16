"""El esquema de LibraCommerce con varios procesos a la vez (ADR-062): ni claves duplicadas del catálogo ni FK repuntada dos veces.

🔴 **Por qué existe.** Con `uvicorn --workers 2` cada worker corre en su arranque `init_schema()` y, en el producto, las
tablas de cruce y `repuntar_fk_ventas_pagos`. Es DDL idempotente (`CREATE TABLE IF NOT EXISTS`, un `SELECT` sobre
`pg_constraint` y un `ALTER`), y en PostgreSQL ese «si no existe, crear» no es atómico: dos procesos que lo corren juntos
sobre una base sin las tablas contestan `duplicate key value violates unique constraint "pg_type_typname_nsp_index"` (o
`pg_class_relname_nsp_index`). ADR-053 de libracore lo arregló para `init_core_schema()`; esto es lo mismo para el DDL de
este motor, con la misma costura (`libracore.candados.esperar_candado_de_transaccion`).

Estos tests levantan **procesos de verdad** (`tests/esquema_en_paralelo_proceso.py`) que arrancan a la vez, vuelta tras vuelta
sobre bases distintas: la carrera depende de cómo se entrelacen los dos, y una sola vuelta limpia no prueba nada (sin el
arreglo, `init_schema()` sobre una base vacía falló en 30 de 30 corridas y las tablas de cruce en 24 de 60 procesos).

Sobre una base ya armada `init_schema()` no falla (medido: 0 de 60 sin el candado): no escribe nada, sólo mira. Por eso acá
no hay un test de «base armada» para él, y sí uno del mecanismo, que no depende de la suerte.

Se saltea sin `LIBRACORE_POSTGRES_URL`. Tarda unos 25 s.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import psycopg
import pytest
from conftest import _PG, url_postgres
from libracore import candados
from libracore.db import core
from libracore.db.schema import init_core_schema
from libracore.respaldo_postgres import con_base

from libracommerce.db.candado import CANDADO_DEL_ESQUEMA, serializar_esquema
from libracommerce.db.migrations import _MIGRATIONS
from libracommerce.db.schema import init_schema
from libracommerce.erp.schema import crear_cliente_lista_precio, crear_promociones, crear_venta_links
from libracommerce.erp.ventas import repuntar_fk_ventas_pagos

PROCESO = Path(__file__).parent / "esquema_en_paralelo_proceso.py"
PROCESOS = 2
VUELTAS = 4
#: Cuánto tarda un subproceso en importar el motor antes de poder arrancar. La cita (`t0`) va más allá.
CITA = 3.0


def _admin(*sentencias: str) -> None:
    with psycopg.connect(_PG.url_original.replace("postgresql+psycopg://", "postgresql://", 1), autocommit=True) as c:
        for s in sentencias:
            c.execute(s)


@pytest.fixture
def bases():
    """Fábrica de bases propias de este test (`VUELTAS` por vez), que se borran al terminar."""
    _PG.url  # noqa: B018 - fuerza la creación perezosa del worker; falla acá y no en el medio del test
    creadas: list[str] = []

    def _nueva(plantilla: str | None = None) -> str:
        nombre = f"{_PG.nombre}_ep{len(creadas)}"
        _admin(f'DROP DATABASE IF EXISTS "{nombre}" WITH (FORCE)',
               f'CREATE DATABASE "{nombre}"' + (f' TEMPLATE "{plantilla}"' if plantilla else ""))
        creadas.append(nombre)
        return con_base(_PG.url_original, nombre)

    yield _nueva
    _admin(*(f'DROP DATABASE IF EXISTS "{n}" WITH (FORCE)' for n in creadas))


def _armar_los_dos_motores(url: str) -> None:
    """La base como la deja un deploy antes de las tablas de cruce: LibraCore y LibraCommerce, sin `venta_links`."""
    conn = core.conectar(url)
    try:
        init_core_schema(conn)
        init_schema(conn)
        conn.commit()
    finally:
        conn.close()


def _arrancar_en_paralelo(secuencia: str, urls: list[str], paso: float) -> list[str]:
    """`PROCESOS` procesos que corren `secuencia` a la vez, una vuelta por base de `urls`. Devuelve los errores."""
    t0 = time.time() + CITA
    hijos = [
        subprocess.Popen([sys.executable, str(PROCESO), str(t0), str(paso), secuencia, *urls],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for _ in range(PROCESOS)
    ]
    errores = []
    for hijo in hijos:
        salida, _ = hijo.communicate(timeout=180)
        for linea in salida.strip().splitlines():
            try:
                errores.append(json.loads(linea)["error"])
            except (ValueError, KeyError):
                errores.append(f"salida inesperada: {linea}")  # un traceback al importar, por ejemplo
        if hijo.returncode not in (0, 1):
            errores.append(f"el proceso salió con {hijo.returncode}")
    return errores


def _uno(url: str, consulta: str):
    with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://", 1)) as c:
        return c.execute(consulta).fetchone()[0]


def test_dos_procesos_sobre_bases_vacias_crean_el_esquema_del_comercio(bases):
    url_postgres()
    urls = [bases() for _ in range(VUELTAS)]
    errores = _arrancar_en_paralelo("comercio", urls, paso=1.0)
    assert not errores, f"fallaron {len(errores)} de {PROCESOS * VUELTAS} arranques de init_schema():\n" + "\n".join(errores)
    for url in urls:
        assert _uno(url, "SELECT count(*) FROM schema_migrations") == len(_MIGRATIONS), "la cadena numerada no quedó completa"
        assert _uno(url, "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'") >= 20


def test_dos_procesos_arrancan_un_producto_como_ventalibra_sobre_bases_vacias(bases):
    """`init_schema()`, `init_core_schema()`, las tres tablas de cruce y el repuntado: el arranque entero de un producto."""
    url_postgres()
    urls = [bases() for _ in range(VUELTAS)]
    errores = _arrancar_en_paralelo("ventalibra", urls, paso=2.0)
    assert not errores, f"fallaron {len(errores)} de {PROCESOS * VUELTAS} arranques de producto:\n" + "\n".join(errores)
    for url in urls:
        # Una sola FK sobre `venta_id`, y a `sales`: lo que deja un solo repuntado, no dos.
        assert _uno(url, "SELECT count(*) FROM pg_constraint WHERE conrelid = 'ventas_pagos'::regclass AND contype = 'f' "
                         "AND pg_get_constraintdef(oid) LIKE '%venta_id%'") == 1
        assert _uno(url, "SELECT count(*) FROM pg_constraint WHERE conrelid = 'ventas_pagos'::regclass "
                         "AND pg_get_constraintdef(oid) LIKE '%REFERENCES sales(%'") == 1
        for tabla in ("venta_links", "cliente_lista_precio", "promotions", "sale_promotions"):
            assert _uno(url, f"SELECT to_regclass('{tabla}') IS NOT NULL"), f"falta {tabla}"


def test_dos_procesos_crean_las_tablas_de_cruce_sobre_los_dos_motores(bases):
    """Un producto que ya tiene los dos motores y estrena las tablas de cruce (el caso de una versión nueva del motor)."""
    url_postgres()
    plantilla = bases()
    _armar_los_dos_motores(plantilla)
    _admin(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{plantilla.rsplit('/', 1)[1]}' "
           "AND pid <> pg_backend_pid()")
    nombre_plantilla = plantilla.rsplit("/", 1)[1]
    urls = [bases(nombre_plantilla) for _ in range(VUELTAS)]
    errores = _arrancar_en_paralelo("cruce", urls, paso=1.0)
    assert not errores, f"fallaron {len(errores)} de {PROCESOS * VUELTAS} arranques de las tablas de cruce:\n" + "\n".join(errores)
    for url in urls:
        for tabla in ("venta_links", "cliente_lista_precio", "promotions", "promotion_items", "sale_promotions"):
            assert _uno(url, f"SELECT to_regclass('{tabla}') IS NOT NULL"), f"falta {tabla}"
        assert _uno(url, "SELECT count(*) FROM pg_constraint WHERE conrelid = 'ventas_pagos'::regclass "
                         "AND pg_get_constraintdef(oid) LIKE '%REFERENCES sales(%'") == 1


#: Lo que se serializa: cada función que crea esquema. `init_schema` y las tres `crear_*` van sobre una base ya armada (no
#: fallan, pero tienen que **esperar**); `repuntar_fk_ventas_pagos` sobre una que todavía apunta a `ventas`.
_FUNCIONES = [init_schema, crear_venta_links, crear_cliente_lista_precio, crear_promociones, repuntar_fk_ventas_pagos]


@pytest.mark.parametrize("funcion", _FUNCIONES, ids=lambda f: f.__name__)
def test_cada_funcion_de_esquema_espera_a_quien_ya_lo_esta_corriendo(bases, funcion):
    """El mecanismo, sin carreras: con el candado del esquema tomado por otra conexión la función no avanza hasta que esa
    conexión termina su transacción. No depende de la suerte: sin la llamada a `serializar_esquema` termina al instante."""
    url_postgres()
    url = bases()
    _armar_los_dos_motores(url)
    otra = core.conectar(url)
    propia = core.conectar(url)
    terminado = threading.Event()
    errores: list[BaseException] = []

    def correr():
        try:
            funcion(propia)
            propia.commit()
        except BaseException as e:  # noqa: BLE001 - se devuelve al hilo principal
            errores.append(e)
        finally:
            terminado.set()

    try:
        candados.esperar_candado_de_transaccion(otra, CANDADO_DEL_ESQUEMA)
        hilo = threading.Thread(target=correr)
        hilo.start()
        assert not terminado.wait(1.5), f"{funcion.__name__}() corrió con el candado del esquema tomado por otro"
        otra.commit()
        assert terminado.wait(30), "no terminó al soltarse el candado"
        hilo.join()
        assert not errores, errores
    finally:
        otra.close()
        propia.close()


def test_sobre_sqlite_no_hace_nada_y_no_importa_libracore(monkeypatch):
    """SQLite serializa a sus escritores solo, y el motor sigue sirviendo a quien lo usa sin libracore."""
    monkeypatch.setitem(sys.modules, "libracore.candados", None)  # `import libracore.candados` levanta ImportError
    conn = sqlite3.connect(":memory:")
    try:
        assert serializar_esquema(conn) is False
        init_schema(conn)  # y el esquema se crea igual
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == len(_MIGRATIONS)
    finally:
        conn.close()


def test_contra_postgres_sin_el_candado_del_motor_falla_diciendo_que_falta(monkeypatch):
    """Con un libracore anterior a v1.168.0 no se sigue sin candado en silencio: el arranque dice qué instalar."""
    monkeypatch.setitem(sys.modules, "libracore.candados", None)
    with pytest.raises(ImportError, match=r"libracore>=1\.168"):
        serializar_esquema(object())  # no es una `sqlite3.Connection`: se toma por PostgreSQL
