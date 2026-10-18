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
