"""Kapitel per Hinweis korrigieren (Schnittstelle 0.4.15)

Revision ID: f1a6c3e9d2b7
Revises: e8b2f4a6c1d3
Create Date: 2026-10-11 10:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f1a6c3e9d2b7'
down_revision: Union[str, Sequence[str], None] = 'e8b2f4a6c1d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('recaps', schema=None) as batch_op:
        batch_op.add_column(sa.Column('revision', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('recaps', schema=None) as batch_op:
        batch_op.drop_column('revision')
