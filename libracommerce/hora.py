"""El reloj del motor: la hora de pared de Argentina, sin depender de la zona del servidor (ADR-052).

Argentina no tiene horario de verano desde 2009: un desfase fijo de -3 evita depender de que la imagen del contenedor traiga la base
de zonas horarias. Todo lo que el motor escribe como «ahora» o «hoy» sale de acá, no de `datetime.now()` / `date.today()` (que toman la
zona del sistema: un servidor o un CI en UTC corre el día entre las 21 y las 24 hs de Argentina).

`ahora_utc` es el ÚNICO reloj real del motor. Los demás helpers lo llaman a través del módulo, así que fijar `hora.ahora_utc` en un test
fija «ahora» y «hoy» en todo el motor, sin depender de la hora en que corra la suite.

Este módulo no importa nada del resto del motor: lo usan la capa de base (`db/repository.py`) y la ERP.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

#: La zona de Argentina (UTC-3 fijo).
ZONA_LOCAL = timezone(timedelta(hours=-3))


def ahora_utc() -> datetime:
    """El instante actual, con zona UTC. El único lugar del motor que lee el reloj del sistema."""
    return datetime.now(UTC)


def ahora_local() -> datetime:
    """La hora de pared de Argentina, SIN zona: como se guardan las vigencias y las fechas de creación (texto local, lo que carga un
    `datetime-local`), y contra lo que compara `resolve_price`."""
    return ahora_utc().astimezone(ZONA_LOCAL).replace(tzinfo=None)


def hoy_local() -> date:
    """El día de hoy en Argentina."""
    return ahora_utc().astimezone(ZONA_LOCAL).date()
