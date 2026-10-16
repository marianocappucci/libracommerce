"""El DDL de este motor se corre de a un proceso por vez (ADR-062).

Con `uvicorn --workers N` cada worker corre en su arranque `init_schema()` y, en el producto, las tablas de cruce
(`erp.schema.crear_*`, `erp.ventas.repuntar_fk_ventas_pagos`). Son DDL idempotente (`CREATE TABLE IF NOT EXISTS`, un
`SELECT` sobre `pg_constraint` seguido de un `ALTER`), y en PostgreSQL ese «si no existe, crear» **no es atómico**: dos
procesos que lo corren juntos sobre una base sin las tablas chocan en el catálogo
(`duplicate key … pg_type_typname_nsp_index` / `pg_class_relname_nsp_index`). Es el mismo defecto que ADR-053 de
libracore arregló en `init_core_schema()`, y se arregla igual: con **su** costura, no con una copia.

`serializar_esquema(conn)` es esa costura: `libracore.candados.esperar_candado_de_transaccion` con un nombre propio de
este motor. Va como **primera sentencia** de cada función que crea esquema, y el candado se suelta con el `commit` o el
`rollback` de `conn`: el segundo proceso espera ahí y corre sobre lo que dejó el primero.

- **SQLite** (`sqlite3.Connection`): no hace nada **y no importa libracore**. SQLite serializa a sus escritores con el
  bloqueo de la base, y este motor sigue sirviendo a quien lo usa sin libracore (`dependencies = []`).
- **PostgreSQL**: la conexión es la de `libracore.db` (el wrapper), así que libracore está instalado por definición;
  se importa acá, tarde, como ya hace `db/migrations.py`. Pide `libracore>=1.168` (donde nace la API); con uno anterior
  falla diciendo qué falta en lugar de seguir sin candado en silencio.
"""
from __future__ import annotations

import sqlite3
from typing import Any

#: El nombre del candado. Uno solo para todo el DDL del motor: las funciones se llaman unas a otras o una detrás de otra
#: en el mismo arranque, y con un nombre solo tomarlo de nuevo en la misma conexión no espera (los candados asesores son
#: reentrantes por sesión). Distinto de `libracore.db.schema.CANDADO_DEL_ESQUEMA` a propósito: no son la misma sección
#: crítica, y un producto que corre `init_core_schema()` y `init_schema()` toma los dos siempre en el mismo orden.
CANDADO_DEL_ESQUEMA = "libracommerce.esquema"


def serializar_esquema(conn: Any, *, espera: float | None = None) -> bool:
    """Toma el candado del esquema de LibraCommerce hasta el fin de la transacción de `conn`.

    Devuelve `True` si lo tomó (PostgreSQL) y `False` si no hacía falta (SQLite). `TimeoutError` si pasan `espera`
    segundos (por defecto los del motor, 120) sin conseguirlo: quien lo tiene está colgado.

    🔴 **Va lo primero de la transacción** y `conn` no puede estar en `autocommit` (ver
    `libracore.candados.esperar_candado_de_transaccion`): un candado tomado a mitad de camino no protege lo que ya se
    tocó, y con `autocommit` PostgreSQL lo suelta al terminar el `SELECT` que lo pidió.
    """
    if isinstance(conn, sqlite3.Connection):
        return False
    try:
        from libracore.candados import esperar_candado_de_transaccion
    except ImportError as e:  # pragma: no cover - depende de la versión instalada
        raise ImportError(
            "libracommerce contra PostgreSQL necesita libracore>=1.168 "
            "(libracore.candados.esperar_candado_de_transaccion, ADR-053 del motor): "
            "sin él dos procesos que arrancan juntos chocan al crear el esquema."
        ) from e
    if espera is None:
        return esperar_candado_de_transaccion(conn, CANDADO_DEL_ESQUEMA)
    return esperar_candado_de_transaccion(conn, CANDADO_DEL_ESQUEMA, espera=espera)
