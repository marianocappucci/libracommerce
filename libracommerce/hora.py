"""El reloj del motor: la hora de pared de Argentina, sin depender de la zona del servidor (ADR-052, ADR-065).

Argentina no tiene horario de verano desde 2009: un desfase fijo de -3 evita depender de que la imagen del contenedor traiga la base
de zonas horarias. Todo lo que el motor escribe como «ahora» o «hoy» sale de acá, no de `datetime.now()` / `date.today()` (que toman la
zona del sistema: un servidor o un CI en UTC corre el día entre las 21 y las 24 hs de Argentina).

**El reloj es el de LibraCore** (`libracore.fechas`, ADR-070 del motor): `hoy()`, `ahora()` y `ZONA_AR` son la fuente única de la familia, y
un test que pisa `libracore.fechas._ahora_utc` mueve también las vigencias de precio, las promociones y los vencimientos de acá. Este
módulo es el adaptador: el núcleo (`db/`, `domain/`) sigue sin importar LibraCore en runtime (ADR-030, `dependencies = []`), así que si
LibraCore no está instalado, o es anterior a la v1.182.0 (sin `fechas.ahora`), cae a la misma regla con su propia lectura del reloj:
UTC-3 fijo, igual resultado. Los extras `erp`, `migrations` y `dev` piden `libracore>=1.182`, así que en un producto el reloj es siempre el
del motor.

`ahora_utc` es el gancho: los demás helpers lo llaman a través del módulo, así que fijar `hora.ahora_utc` en un test fija «ahora» y «hoy»
en todo el motor, sin depender de la hora en que corra la suite (y fijar `libracore.fechas._ahora_utc` también).

Este módulo no importa nada del resto del motor: lo usan la capa de base (`db/repository.py`) y la ERP.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

try:
    from libracore import fechas as _fechas
except ImportError:                         # el núcleo solo, sin LibraCore
    _fechas = None
if _fechas is not None and not hasattr(_fechas, "ahora"):
    _fechas = None                          # LibraCore anterior a 1.182: todavía no trae el reloj único (ADR-070 del motor)

#: La zona de Argentina (UTC-3 fijo): la de `libracore.fechas.ZONA_AR`, o la misma regla si LibraCore no está.
ZONA_LOCAL = _fechas.ZONA_AR if _fechas is not None else timezone(timedelta(hours=-3))


def ahora_utc() -> datetime:
    """El instante actual, con zona UTC. Sale de `libracore.fechas` (el reloj de la familia); sin LibraCore, es la única lectura propia
    del sistema que queda en el motor."""
    if _fechas is not None:
        return _fechas.ahora().astimezone(UTC)
    return datetime.now(UTC)


def ahora_local() -> datetime:
    """La hora de pared de Argentina, SIN zona: como se guardan las vigencias y las fechas de creación (texto local, lo que carga un
    `datetime-local`), y contra lo que compara `resolve_price`."""
    return ahora_utc().astimezone(ZONA_LOCAL).replace(tzinfo=None)


def hoy_local() -> date:
    """El día de hoy en Argentina."""
    return ahora_utc().astimezone(ZONA_LOCAL).date()


# --- El día de Argentina de un instante guardado (ADR-066) -----------------------------------------------------------------------------
#
# `stock_movements.occurred_at` y `purchase_receipts.received_at` se guardan como texto ISO en dos formas:
# - **local sin zona** (`2026-10-17T00:00:00`): ajustes, mermas, transferencias, ventas. Ya es hora de Argentina; el día son los 10 primeros.
# - **instante con zona** (`2026-10-18T02:30:00+00:00`, o `...Z`): la recepción de mercadería (`compras_router`, ADR-065). El día es el de
#   ese instante pasado a la hora de Argentina (02:30Z del 18 = 23:30 del 17 → el 17).
# Un reporte por día corta el día con `dia_local` (Python) o `dia_local_sql` (SQL); las dos dan lo mismo para el mismo texto.

#: El desfase de Argentina en minutos (-180), para el SQL. Sale de `ZONA_LOCAL`: una sola definición del huso.
_DESFASE_MINUTOS = int(ZONA_LOCAL.utcoffset(None).total_seconds() // 60)


def dia_local(valor) -> str | None:
    """El día de Argentina (`AAAA-MM-DD`) de un `occurred_at` / `received_at` guardado, o `None` si no hay valor.

    Con zona (`...+00:00`, `...Z`, o un `datetime` con `tzinfo`): el día de ese instante en Argentina. Sin zona: ya es hora local, así que
    es su fecha tal cual (no cambia el día de las filas de siempre). Un texto que no se puede leer como fecha ISO devuelve sus 10 primeros
    caracteres, como el `substr(..., 1, 10)` de antes."""
    if valor is None:
        return None
    if isinstance(valor, datetime):
        instante = valor
    elif isinstance(valor, date):
        return valor.isoformat()
    else:
        texto = str(valor)
        try:
            instante = datetime.fromisoformat(texto)
        except ValueError:
            return texto[:10]
    if instante.tzinfo is not None:
        instante = instante.astimezone(ZONA_LOCAL)
    return instante.date().isoformat()


def _es_sqlite(conn) -> bool:
    import sqlite3

    return isinstance(conn, sqlite3.Connection)


def dia_local_sql(conn, columna: str) -> str:
    """La expresión SQL que da el día de Argentina (`AAAA-MM-DD`, texto) de `columna`, con la misma regla que `dia_local`.

    Es un `CASE`: una fila **sin zona** sigue siendo `substr(columna, 1, 10)` (el mismo valor que antes); una con zona (`Z` o `±HH:MM` al
    final, más de 19 caracteres) se pasa a UTC y se le resta el desfase de Argentina. La condición es SQL común; la conversión no:
    - **SQLite** (`sqlite3.Connection`): `date(instante, '-180 minutes')`; `date()` ya lleva a UTC un texto con `±HH:MM` o `Z`.
    - **PostgreSQL** (el wrapper de `libracore.db`): `CAST(columna AS timestamptz) AT TIME ZONE 'UTC'` más el desfase, con `to_char`.
      Sin `date(...)` a propósito: el traductor de LibraCore lo reescribe como `CAST(... AS DATE)`.

    `columna` es un nombre de columna escrito en el código (`sm.occurred_at`), nunca un dato del usuario."""
    return dia_local_sql_de_motor(columna, postgres=not _es_sqlite(conn))


def dia_local_sql_de_motor(columna: str, *, postgres: bool) -> str:
    """`dia_local_sql` para quien todavía no tiene la conexión (una parte de SQL armada de antemano, `erp.actividad`)."""
    con_zona = (f"(length({columna}) > 19 AND (substr({columna}, length({columna}), 1) = 'Z'"
                f" OR substr({columna}, length({columna}) - 5, 1) IN ('+', '-')))")
    # Sin las fracciones de segundo, que no cambian el día: SQLite las redondea a milisegundos (las 02:59:59.9999Z serían las 03:00Z).
    instante = (f"(substr({columna}, 1, 19) || CASE WHEN substr({columna}, length({columna}), 1) = 'Z' THEN 'Z'"
                f" ELSE substr({columna}, length({columna}) - 5) END)")
    if not postgres:
        local = f"date({instante}, '{_DESFASE_MINUTOS:+d} minutes')"
    else:
        local = (f"to_char((CAST({instante} AS timestamptz) AT TIME ZONE 'UTC') + interval '{_DESFASE_MINUTOS:+d} minutes',"
                 f" 'YYYY-MM-DD')")
    return f"(CASE WHEN {con_zona} THEN {local} ELSE substr({columna}, 1, 10) END)"


def filtro_de_dias_sql(conn, columna: str, desde: str | None = None, hasta: str | None = None) -> tuple[list[str], list]:
    """Las condiciones `WHERE` (y sus parámetros) para «el día de Argentina de `columna` entre `desde` y `hasta`», ambos `AAAA-MM-DD`
    inclusive y opcionales.

    Además de comparar `dia_local_sql`, acota la columna **cruda** por rango, que la base puede resolver con un índice sobre `columna`: una
    fila del día D de Argentina tiene un texto `>= 'D'` (sin zona empieza con D; con zona, su instante UTC es del día D o del D+1) y
    `< 'H+2'` para el día H (con zona, a lo sumo las 02:59Z del H+1). El filtro exacto es el de `dia_local_sql`."""
    donde: list[str] = []
    params: list = []
    dia = dia_local_sql(conn, columna)
    if desde:
        donde += [f"{columna} >= ?", f"{dia} >= ?"]
        params += [desde, desde]
    if hasta:
        try:
            tope = (date.fromisoformat(hasta[:10]) + timedelta(days=2)).isoformat()
        except ValueError:      # un `hasta` ilegible se compara como texto, como antes; sin cota cruda
            tope = None
        if tope is not None:
            donde.append(f"{columna} < ?")
            params.append(tope)
        donde.append(f"{dia} <= ?")
        params.append(hasta)
    return donde, params
