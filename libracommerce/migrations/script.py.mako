"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Las revisiones de este motor se escriben A MANO (no hay `target_metadata`):
el DDL corre con la conexión de LibraCore (`conexion_libracore(op.get_bind())`)
para que hable los dos motores, como la baseline. Idempotente por introspección
si va a correr sobre instancias vivas.
"""
from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade():
    ${upgrades if upgrades else "pass"}


def downgrade():
    ${downgrades if downgrades else "pass"}
