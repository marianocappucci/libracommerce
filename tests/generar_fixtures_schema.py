"""Regenera las fixtures del gate de schema, una por motor.

    LIBRACORE_POSTGRES_URL=postgresql://... python -m tests.generar_fixtures_schema

Las dos se regeneran juntas y a proposito: los `CHECK` solo se ven en la de
PostgreSQL (SQLite no los expone por introspeccion) y los tipos se escriben
distinto en cada motor, asi que la cobertura es del par y no de cada una.
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

from libracore.db import core
from libracore.db.schema_dump import volcar_schema

from libracommerce.db.schema import init_schema

FIXTURES = Path(__file__).parent / "fixtures"


def sqlite_() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        conn = sqlite3.connect(str(Path(tmp) / "gate.db"))
        init_schema(conn)
        conn.commit()
        volcado = volcar_schema(conn)
        conn.close()
    return volcado


def postgres(url: str) -> str:
    core.configure(url)
    conn = core.get_connection()
    conn.execute("DROP SCHEMA public CASCADE")
    conn.execute("CREATE SCHEMA public")
    conn.commit()
    init_schema(conn)
    conn.commit()
    volcado = volcar_schema(conn)
    conn.close()
    core._db_path = None
    core._database_url = None
    return volcado


def main() -> int:
    FIXTURES.mkdir(exist_ok=True)
    (FIXTURES / "schema_sqlite.txt").write_text(sqlite_(), encoding="utf-8")
    print("escrita fixtures/schema_sqlite.txt")

    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if not url:
        print("FALTA LIBRACORE_POSTGRES_URL: la fixture de PostgreSQL NO se regenero.")
        print("Las dos van juntas -- no commitees solo una.")
        return 1
    (FIXTURES / "schema_postgres.txt").write_text(postgres(url), encoding="utf-8")
    print("escrita fixtures/schema_postgres.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
