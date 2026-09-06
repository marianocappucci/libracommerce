"""Aplicar las migraciones de LibraCommerce desde el paquete instalado.

Hasta P9-M0 (2026-09-06) este motor evolucionaba su schema con una cadena
numerada propia (`db/migrations.py`, tabla `schema_migrations`) que corre
adentro de `init_schema()`, en cada arranque del producto. Funcionaba, con dos
límites que la familia ya pagó en LibraCore y LibraGenda:

- **Nadie la invoca fuera del arranque.** Un deploy no tiene forma de migrar la
  base *antes* de levantar la app nueva: la primera petición que llega a un
  contenedor recién construido corre con la base vieja hasta que el arranque
  la altera. Y el `panel_admin.py actualizar` de los consumidores declara
  `migraciones=(...)` como una secuencia de comandos —`libracore-migrar`,
  `alembic upgrade head`— en la que este motor no aparecía.
- **Es una segunda herramienta.** Los otros dos motores con schema y los ocho
  productos usan Alembic; éste era el único con un runner propio. Tres
  mecanismos de schema en una misma base (F6.2 del plan de septiembre).

Desde acá la cadena es de Alembic y **viaja en el wheel** (`migrations/` vive
adentro del paquete, como en LibraCore `v1.53.0`):

    libracommerce-migrar upgrade --prefijo contalibra
    python -m libracommerce.migrar upgrade --prefijo contalibra
    from libracommerce.migrar import upgrade; upgrade(destino)

La baseline **llama a `init_schema()`**, que es idempotente y que a su vez corre
la cadena numerada vieja, así que una instancia viva se migra —no se estampa—
y termina exactamente donde ya estaba, más la versión registrada. La cadena
numerada queda **congelada**: todo cambio de schema posterior es una revisión
de Alembic, no una entrada más en `db/migrations.py`.

🔑 **`script_location` se resuelve desde `__file__`, no desde el cwd**, que es la
diferencia entre andar en el repo y andar en `site-packages`.

🔴 **La tabla de versión es `alembic_version_libracommerce`.** En Contalibra y
Restolibra las tablas de este motor conviven con las de LibraCore en la misma
base, y `alembic_version` a secas ya es de LibraCore. Dos cadenas sobre la
misma tabla se pisan la revisión y la segunda en correr cree que la primera es
suya. Es el mismo criterio que los productos con cadena propia
(`alembic_version_<producto>`).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: El directorio de migraciones **dentro del paquete**.
DIRECTORIO = Path(__file__).parent / "migrations"

#: La tabla de versión de esta cadena. Ver el docstring del módulo.
TABLA_DE_VERSION = "alembic_version_libracommerce"


class SinURL(RuntimeError):
    """No hay contra qué migrar: ni destino explícito ni variables del entorno."""


class SinAlembic(RuntimeError):
    """Falta el extra `[migrations]`, que es quien trae alembic y LibraCore."""


def _comandos():
    """`alembic.command`, importado tarde y con un error que dice qué falta.

    Alembic no es dependencia de LibraCommerce (`dependencies = []`): viene en
    el extra `[migrations]`. Sin este envoltorio el console script muere con un
    `ModuleNotFoundError` en medio de un deploy, que manda a buscar el problema
    en el lugar equivocado.
    """
    try:
        from alembic import command
        from alembic.config import Config
    except ModuleNotFoundError as e:  # pragma: no cover - depende del entorno
        raise SinAlembic(
            "Falta alembic: LibraCommerce no lo declara como dependencia, viene en "
            "el extra `[migrations]`. Instalá `libracommerce[migrations]` en la "
            "imagen del producto antes de declarar este comando en el deploy."
        ) from e
    return command, Config


def normalizar_url(destino: str) -> str:
    """Deja el destino tal como lo espera `migrations/env.py`.

    No traduce a URL de SQLAlchemy: el `env.py` acepta las dos formas en que la
    familia nombra una base —URL PostgreSQL o ruta de archivo SQLite— y decide
    el bind. Sólo se corrige el prefijo pelado, porque SQLAlchemy resuelve
    `postgresql://` a psycopg2 y la familia instala psycopg 3.
    """
    if destino.startswith("postgresql://"):
        return "postgresql+psycopg://" + destino[len("postgresql://"):]
    return destino


def url_de_commerce(prefijo: str | None = None, entorno=None) -> str:
    """La base donde viven las tablas de LibraCommerce en esta instancia.

    🔴 **Es la base del DOMINIO del producto, nunca la de LibraCore.** Medido
    en los cuatro consumidores (2026-09-06): Contalibra y Restolibra llevan los
    dos motores en la misma base; VentaLibra y LibraDesk llevan LibraCommerce
    en la del dominio y LibraCore aparte. En ningún caso este motor vive en la
    base separada del core, así que un `upgrade` que cayera a
    `<PREFIJO>_LIBRACORE_DATABASE_URL` crearía las tablas comerciales al lado
    de las del core, dejaría la base real sin tocar y devolvería éxito.

    El orden es:

    1. `LIBRACOMMERCE_MIGRAR_URL`, la salida de emergencia explícita.
    2. Con `prefijo`: `<PREFIJO>_DATABASE_URL` y sus nombres históricos, vía
       `url_de_instancia(prefijo)` de LibraCore.
    3. Sin `prefijo`: `DATABASE_URL`, que es el caso de un script en el host.

    Un prefijo que no resuelve **falla** en vez de caer a `DATABASE_URL`: el
    mismo criterio que `libracore.migrar.url_de_core`, y por el mismo motivo.
    """
    env = os.environ if entorno is None else entorno

    explicita = (env.get("LIBRACOMMERCE_MIGRAR_URL") or "").strip()
    if explicita:
        return explicita

    if prefijo:
        from libracore.db.url_de_instancia import nombre_normalizado, url_de_instancia

        del_dominio = url_de_instancia(prefijo, core=False, entorno=env)
        if del_dominio:
            return del_dominio
        raise SinURL(
            f"No hay base de dominio para el prefijo '{prefijo}': "
            f"{nombre_normalizado(prefijo)} (ni sus nombres históricos) no está "
            "definida en este entorno. Las tablas de LibraCommerce viven en la "
            "base del dominio del producto; si este producto no sigue la "
            "convención, pasá el destino por LIBRACOMMERCE_MIGRAR_URL."
        )

    del_entorno = (env.get("DATABASE_URL") or "").strip()
    if del_entorno:
        return del_entorno

    raise SinURL(
        "Falta el destino: pasá --prefijo <producto> para que salga de las "
        "variables de la instancia, o definí LIBRACOMMERCE_MIGRAR_URL o "
        "DATABASE_URL. Sin eso no hay base contra la cual migrar."
    )


def configuracion(destino: str):
    """El `Config` de Alembic apuntado al paquete instalado.

    No lee `alembic.ini`: ese archivo es del repo y no viaja en el wheel. Se
    arma en memoria con las opciones que importan.
    """
    _, Config = _comandos()
    cfg = Config()
    cfg.set_main_option("script_location", str(DIRECTORIO))
    normalizado = normalizar_url(destino)
    # 🔴 **La que manda de verdad.** `env.py` prefiere `DATABASE_URL` del
    # entorno por sobre `sqlalchemy.url`, así que sin esta opción propia el
    # destino explícito se ignoraría en silencio adentro de un contenedor.
    cfg.set_main_option("libracommerce.url", normalizado)
    cfg.set_main_option("sqlalchemy.url", normalizado)
    return cfg


def upgrade(destino: str, revision: str = "head") -> None:
    """Aplica las migraciones. Es lo que corre el deploy de un consumidor.

    Sobre una instancia viva: la baseline llama a `init_schema()`, que es
    idempotente, así que hace lo mismo que un arranque de la app y además
    registra la versión. Aun así, **backup antes**: es una operación de schema.
    """
    command, _ = _comandos()
    command.upgrade(configuracion(destino), revision)


def stamp(destino: str, revision: str = "head") -> None:
    """Marca la base en una revisión **sin ejecutar** las migraciones.

    Casi nunca es lo que hace falta: como la baseline es idempotente, una base
    que ya tiene el schema se pone al día con `upgrade`, que además agrega lo
    que falte. Estampar declara «esta base está en esta revisión» sin mirar si
    es cierto.
    """
    command, _ = _comandos()
    command.stamp(configuracion(destino), revision)


def current(destino: str) -> None:
    command, _ = _comandos()
    command.current(configuracion(destino))


def heads(destino: str) -> None:
    command, _ = _comandos()
    command.heads(configuracion(destino))


def _parsear(argv: list[str]) -> tuple[str, str | None, str | None]:
    """`(accion, prefijo, revision)` — sin argparse, la misma forma que LibraCore."""
    accion = argv[0] if argv else "upgrade"
    prefijo = None
    revision = None
    resto = argv[1:]
    i = 0
    while i < len(resto):
        arg = resto[i]
        if arg == "--prefijo":
            i += 1
            prefijo = resto[i] if i < len(resto) else None
        elif arg.startswith("--prefijo="):
            prefijo = arg.split("=", 1)[1]
        else:
            revision = arg
        i += 1
    return accion, prefijo, revision


def main(argv: list[str] | None = None) -> int:
    """CLI: `libracommerce-migrar [upgrade|stamp|current|heads] [--prefijo P] [rev]`."""
    argv = list(sys.argv[1:] if argv is None else argv)
    accion, prefijo, revision = _parsear(argv)
    acciones = {"upgrade": upgrade, "stamp": stamp, "current": current, "heads": heads}

    if accion in ("-h", "--help") or accion not in acciones:
        print(main.__doc__)
        print(f"  migraciones en: {DIRECTORIO}")
        print(f"  tabla de versión: {TABLA_DE_VERSION}")
        print("  --prefijo resuelve la base del DOMINIO de esa instancia, que es "
              "donde viven las tablas de LibraCommerce: ver url_de_commerce")
        # Código 0 si la pidieron, 2 si el comando no existe: un typo no puede
        # leerse como éxito desde un pipeline.
        return 0 if accion in ("-h", "--help") else 2

    try:
        destino = url_de_commerce(prefijo)
        if accion in ("upgrade", "stamp") and revision:
            acciones[accion](destino, revision)
        else:
            acciones[accion](destino)
    except (SinURL, SinAlembic) as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
