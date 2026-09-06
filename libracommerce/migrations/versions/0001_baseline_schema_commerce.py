"""Baseline: el schema de LibraCommerce tal como ya existe, sin reescribirlo.

Esta revisión **llama a `init_schema()`** en vez de re-expresar sus 20 tablas en
`op.create_table(...)`. Es lo mismo que hizo LibraCore en su `0001_baseline`, y
por el mismo motivo: la fuente de verdad es un `executescript()` de DDL crudo
más la cadena numerada de `db/migrations.py`, y reescribirlos crearía una
segunda fuente de verdad que se desincroniza en el primer cambio.

Lo que esto congela, y cómo se sostiene:

- Desde esta revisión, `init_schema()` y `db/migrations.py` son de **sólo
  lectura**. Todo cambio de schema posterior va como revisión de Alembic nueva,
  no como una entrada `_migration_0012_...` más. El congelamiento lo sostiene
  `tests/test_schema_congelado.py`, que compara el resultado de la función
  contra una fixture por motor.

**Se puede correr sobre una instancia que ya existe.** `init_schema()` es
idempotente de punta a punta —`CREATE TABLE IF NOT EXISTS`, cadena numerada
guardada por `schema_migrations` e introspección—, así que `upgrade head`
sobre una base viva hace lo mismo que cada arranque de la app, más registrar la
versión en `alembic_version_libracommerce`. Por eso las instancias existentes
**se migran, no se estampan**: el resultado es el mismo y además queda
verificado.
"""
from alembic import op
from libracore.db.migraciones import conexion_libracore

from libracommerce.db.schema import init_schema

revision = "0001_baseline_commerce"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    init_schema(conexion_libracore(op.get_bind()))


def downgrade():
    raise RuntimeError(
        "La baseline no se baja: es el schema completo del motor y su cadena "
        "numerada, y bajarla sería borrar las tablas comerciales de la instancia."
    )
