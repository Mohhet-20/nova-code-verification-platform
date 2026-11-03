"""fk indexes for index generation deletes

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-20 15:27:44.407272
"""
from alembic import op

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Plain CREATE INDEX (no batch table rebuild): these are FK child columns that were scanned on every parent DELETE.
    op.create_index("ix_file_imports_resolved_file_id", "file_imports", ["resolved_file_id"], unique=False)
    op.create_index("ix_relationships_source_file_id", "relationships", ["source_file_id"], unique=False)
    op.create_index("ix_relationships_target_file_id", "relationships", ["target_file_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_relationships_target_file_id", table_name="relationships")
    op.drop_index("ix_relationships_source_file_id", table_name="relationships")
    op.drop_index("ix_file_imports_resolved_file_id", table_name="file_imports")
