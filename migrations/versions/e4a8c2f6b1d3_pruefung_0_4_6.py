"""Qualitätsprüfung (Schnittstelle 0.4.6): Prüfteil am Recap, unsichere Wörter, Namenshilfe, erneute Transkription

Revision ID: e4a8c2f6b1d3
Revises: d7f1b3c5e9a2
Create Date: 2026-10-01 09:00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'e4a8c2f6b1d3'
down_revision: Union[str, Sequence[str], None] = 'd7f1b3c5e9a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('recaps') as b:
        b.add_column(sa.Column('review', sa.Text(), nullable=True))
    with op.batch_alter_table('transcript_segments') as b:
        b.add_column(sa.Column('unsicher', sa.Text(), nullable=True))
    with op.batch_alter_table('campaigns') as b:
        b.add_column(sa.Column('namenshilfe', sa.Text(), nullable=True))
    with op.batch_alter_table('sessions') as b:
        b.add_column(sa.Column('nachtranskriptionen', sa.Integer(), nullable=False, server_default='0'))
        b.add_column(sa.Column('nachtranskription', sa.Boolean(), nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('sessions') as b:
        b.drop_column('nachtranskription')
        b.drop_column('nachtranskriptionen')
    with op.batch_alter_table('campaigns') as b:
        b.drop_column('namenshilfe')
    with op.batch_alter_table('transcript_segments') as b:
        b.drop_column('unsicher')
    with op.batch_alter_table('recaps') as b:
        b.drop_column('review')
