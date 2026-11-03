"""repair patches: allow source 'ai_assisted'

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-22

Widens ck_repair_patches_source so AI-assisted proposals (Claude) can be stored as ordinary draft patches. Backward compatible: code from 0.19.0
and earlier only writes 'generated' or 'manual', which remain valid. The downgrade refuses to run while 'ai_assisted' rows exist instead of
deleting them.
"""
import sqlalchemy as sa
from alembic import op

revision = '0017'
down_revision = '0016'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('repair_patches', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('ck_repair_patches_source'), type_='check')
        batch_op.create_check_constraint('source', "source IN ('generated','manual','ai_assisted')")


def downgrade() -> None:
    count = op.get_bind().execute(sa.text("SELECT COUNT(*) FROM repair_patches WHERE source = 'ai_assisted'")).scalar()
    if count:
        raise RuntimeError(f"{count} AI-assisted patch(es) exist; downgrading would violate the old constraint. Restore a pre-0017 backup instead.")
    with op.batch_alter_table('repair_patches', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('ck_repair_patches_source'), type_='check')
        batch_op.create_check_constraint('source', "source IN ('generated','manual')")
