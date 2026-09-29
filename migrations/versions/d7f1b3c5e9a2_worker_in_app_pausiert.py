"""Worker in der Worker-App pausiert (workers.app_paused_since)

Revision ID: d7f1b3c5e9a2
Revises: c3e7a9d1f5b2
Create Date: 2026-09-29 23:00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd7f1b3c5e9a2'
down_revision: Union[str, Sequence[str], None] = 'c3e7a9d1f5b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('workers') as b:
        b.add_column(sa.Column('app_paused_since', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('workers') as b:
        b.drop_column('app_paused_since')
