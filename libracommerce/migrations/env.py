"""Entorno de Alembic para LibraCommerce.

Vive **adentro del paquete** (`libracommerce/migrations/`) para viajar en el
wheel: un consumidor lo aplica con `libracommerce-migrar` sin clonar nada. Hay
un test que abre el wheel construido y lo verifica.

Es el espejo del `env.py` de LibraCore, con **una diferencia que importa**: la
tabla de versión es `alembic_version_libracommerce`. En Contalibra y Restolibra
los dos motores conviven en la misma base y `alembic_version` a secas ya es de
LibraCore. Con la misma tabla las dos cadenas se pisarían la revisión.

Igual que en LibraCore:

1. **No hay `target_metadata`.** La fuente de verdad es `init_schema()` (DDL
   crudo); `alembic revision --autogenerate` produce una revisión vacía. Las
   revisiones se escriben a mano.
2. **El destino se acepta en las dos formas** de la familia: URL PostgreSQL o
   ruta de archivo SQLite. Se traduce a URL de SQLAlchemy sólo para el bind, y
   se configura además `libracore.db.core` con el destino original, porque
   contra PostgreSQL `init_schema()` corre a través del adaptador de LibraCore
   (es la conexión que los productos le pasan, y el `PRAGMA` de su primera
   línea sólo lo saltea ese adaptador).
"""
import os
from pathlib import Path

from alembic import context
from libracore.db import core
from sqlalchemy import create_engine, pool

TABLA_DE_VERSION = "alembic_version_libracommerce"


def _destino() -> str:
    """El destino tal como lo escribe un producto: URL PostgreSQL o ruta SQLite.

    1. `libracommerce.url`, que pone `libracommerce.migrar.configuracion()`
       cuando alguien pasa el destino explícito. Va primero porque es la única
       señal inequívoca de intención: sin esta precedencia, adentro de un
       contenedor el destino explícito se ignoraría en silencio a favor de
       `DATABASE_URL`.
    2. `DATABASE_URL` del entorno — un script parado en el host.
    3. `sqlalchemy.url` del `alembic.ini`, que en este repo es un placeholder.
    """
    destino = (
        context.config.get_main_option("libracommerce.url", default="")
        or os.environ.get("DATABASE_URL")
        or context.config.get_main_option("sqlalchemy.url", default="")
    )
    if not destino or destino.startswith("postgresql://user:password@"):
        raise RuntimeError(
            "Falta DATABASE_URL. Acepta una URL PostgreSQL "
            "(postgresql://usuario:clave@host/base) o la ruta del archivo SQLite "
            "de la instancia."
        )
    return destino


def _url_sqlalchemy(destino: str) -> str:
    if core.es_url_postgres(destino):
        # SQLAlchemy resuelve "postgresql://" a psycopg2, que la familia no
        # instala: el driver es psycopg 3.
        return destino.replace("postgresql://", "postgresql+psycopg://", 1)
    return f"sqlite:///{Path(destino).expanduser().resolve()}"


def run_migrations_online():
    destino = _destino()
    # `configure` antes de abrir nada: la baseline llama a código que lee esta
    # configuración para decidir si está frente a PostgreSQL.
    core.configure(destino)
    connectable = create_engine(_url_sqlalchemy(destino), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=None,
            version_table=TABLA_DE_VERSION,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    # `--sql` genera el SQL sin conectarse. La baseline no puede: ejecuta
    # Python (`init_schema()`), que decide parte del DDL mirando la base que
    # tiene enfrente. Un modo offline que emitiera algo estaría mintiendo.
    raise RuntimeError(
        "El modo offline (--sql) no está soportado: la baseline ejecuta "
        "init_schema(), que inspecciona la base antes de decidir el DDL."
    )

run_migrations_online()
