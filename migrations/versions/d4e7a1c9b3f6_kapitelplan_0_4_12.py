"""Kapitelplan der SL (Schnittstelle 0.4.12): chapter_plans

Revision ID: d4e7a1c9b3f6
Revises: c3d9e1f7a2b5
Create Date: 2026-10-08 09:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd4e7a1c9b3f6'
down_revision: Union[str, Sequence[str], None] = 'c3d9e1f7a2b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('chapter_plans',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('campaign_id', sa.String(length=36), nullable=False),
    sa.Column('title', sa.String(length=120), nullable=False),
    sa.Column('session_number', sa.Integer(), nullable=True),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('scenes', sa.Text(), nullable=False),
    sa.Column('names', sa.Text(), nullable=False),
    sa.Column('document_ids', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('chapter_plans', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_chapter_plans_campaign_id'), ['campaign_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('chapter_plans', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_chapter_plans_campaign_id'))
    op.drop_table('chapter_plans')
