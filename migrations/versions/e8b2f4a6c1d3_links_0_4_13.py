"""Links und Notizen am SL-Schirm (Schnittstelle 0.4.13)

Revision ID: e8b2f4a6c1d3
Revises: d4e7a1c9b3f6
Create Date: 2026-10-08 18:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e8b2f4a6c1d3'
down_revision: Union[str, Sequence[str], None] = 'd4e7a1c9b3f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('campaigns', schema=None) as batch_op:
        batch_op.add_column(sa.Column('links', sa.Text(), nullable=True))
    with op.batch_alter_table('entries', schema=None) as batch_op:
        batch_op.add_column(sa.Column('links', sa.Text(), nullable=True))
    with op.batch_alter_table('chapter_plans', schema=None) as batch_op:
        batch_op.add_column(sa.Column('table_notes', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('chapter_plans', schema=None) as batch_op:
        batch_op.drop_column('table_notes')
    with op.batch_alter_table('entries', schema=None) as batch_op:
        batch_op.drop_column('links')
    with op.batch_alter_table('campaigns', schema=None) as batch_op:
        batch_op.drop_column('links')
