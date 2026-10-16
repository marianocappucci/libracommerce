"""Un proceso que arranca el esquema como un producto, para `tests/test_esquema_en_paralelo.py` (ADR-062).

No es un test: es el banco de pruebas, y se corre uno por proceso, con el intérprete del que lo lanza::

    python tests/esquema_en_paralelo_proceso.py <t0> <paso> <secuencia> <url> [<url> ...]

Hace `len(urls)` vueltas, una por base: la vuelta `i` arranca a las `t0 + i * paso` (`time.time()`), **todos** los procesos
a la vez. Cada uno tarda un rato en importar el motor, y sin esa cita el primero termina antes de que el segundo empiece
y la carrera no se ve. Varias vueltas por proceso —y no un proceso por vuelta— porque lo caro es arrancar el intérprete.

Por cada vuelta que falla escribe una línea JSON `{"vuelta": i, "error": "<tipo>: <mensaje>"}`; sale con 1 si alguna falló.

Secuencias (cada una es lo que corre un producto al arrancar):

- `comercio`: sólo `init_schema()` (LibraDesk, LibraClub; y el primer paso de VentaLibra).
- `ventalibra`: `db.connect()` (`init_schema`) y después `billing.configure()` (`init_core_schema`, las tres tablas de cruce
  y `repuntar_fk_ventas_pagos`), cada una con su conexión, como VentaLibra.
- `cruce`: sólo las tablas de cruce y el repuntado, sobre una base que ya tiene los dos motores.
"""
from __future__ import annotations

import json
import sys
import time

from libracore.db import core
from libracore.db.schema import init_core_schema

from libracommerce.db.schema import init_schema
from libracommerce.erp.schema import crear_cliente_lista_precio, crear_promociones, crear_venta_links
from libracommerce.erp.ventas import repuntar_fk_ventas_pagos


def _cruce(conn) -> None:
    crear_venta_links(conn)
    crear_cliente_lista_precio(conn)
    crear_promociones(conn)
    repuntar_fk_ventas_pagos(conn)


def comercio(url: str) -> None:
    conn = core.conectar(url)
    try:
        init_schema(conn)
    finally:
        conn.close()


def ventalibra(url: str) -> None:
    dominio = core.conectar(url)  # `app/db.py::connect()`
    try:
        init_schema(dominio)
        dominio.commit()
    finally:
        dominio.close()
    conn = core.conectar(url)  # `app/services/billing.py::configure()`
    try:
        init_core_schema(conn)
        conn.commit()
        _cruce(conn)
        conn.commit()
    finally:
        conn.close()


def cruce(url: str) -> None:
    conn = core.conectar(url)
    try:
        _cruce(conn)
        conn.commit()
    finally:
        conn.close()


SECUENCIAS = {"comercio": comercio, "ventalibra": ventalibra, "cruce": cruce}


def main(t0: float, paso: float, secuencia: str, urls: list[str]) -> int:
    fallo = False
    for i, url in enumerate(urls):
        time.sleep(max(0.0, t0 + i * paso - time.time()))
        try:
            SECUENCIAS[secuencia](url)
        except Exception as e:  # noqa: BLE001 - el test necesita ver QUÉ falló, sea lo que sea
            fallo = True
            print(json.dumps({"vuelta": i, "error": f"{type(e).__name__}: {str(e).splitlines()[0][:200]}"}), flush=True)
    return 1 if fallo else 0


if __name__ == "__main__":
    sys.exit(main(float(sys.argv[1]), float(sys.argv[2]), sys.argv[3], sys.argv[4:]))
