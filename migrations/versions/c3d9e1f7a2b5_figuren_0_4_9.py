"""Figuren ausgetretener Spieler (Schnittstelle 0.4.9): entries.former_holder_member_id

Revision ID: c3d9e1f7a2b5
Revises: b7e2c4f9a1d3
Create Date: 2026-10-08 08:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3d9e1f7a2b5'
down_revision: Union[str, Sequence[str], None] = 'b7e2c4f9a1d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('entries', schema=None) as batch_op:
        batch_op.add_column(sa.Column('former_holder_member_id', sa.String(length=36), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('entries', schema=None) as batch_op:
        batch_op.drop_column('former_holder_member_id')
