"""Schnittstelle 0.4.5: Kampagnen abschließen, Mitglieder verlassen/entfernen

Revision ID: c3e7a9d1f5b2
Revises: b7d0f4a2e8c9
Create Date: 2026-09-29 16:00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c3e7a9d1f5b2'
down_revision: Union[str, Sequence[str], None] = 'b7d0f4a2e8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('campaigns') as b:
        b.add_column(sa.Column('archived_at', sa.DateTime(), nullable=True))
    with op.batch_alter_table('members') as b:
        b.add_column(sa.Column('left_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('members') as b:
        b.drop_column('left_at')
    with op.batch_alter_table('campaigns') as b:
        b.drop_column('archived_at')
