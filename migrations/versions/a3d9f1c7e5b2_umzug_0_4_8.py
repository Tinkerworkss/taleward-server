"""Kampagnen-Umzug (Schnittstelle 0.4.8): Zustimmung zum Mitnehmen, offene Plätze, Platz-Einladungen, Export und
Import

Revision ID: a3d9f1c7e5b2
Revises: f2c6a8e0b4d7
Create Date: 2026-10-01 15:00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a3d9f1c7e5b2'
down_revision: Union[str, Sequence[str], None] = 'f2c6a8e0b4d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('members') as b:
        b.add_column(sa.Column('move_consent_at', sa.DateTime(), nullable=True))
        b.add_column(sa.Column('open_seat', sa.Boolean(), nullable=False, server_default='0'))
    with op.batch_alter_table('campaigns') as b:
        b.add_column(sa.Column('imported_by_user_id', sa.String(length=36), nullable=True))
        b.add_column(sa.Column('imported_by_member_id', sa.String(length=36), nullable=True))
    with op.batch_alter_table('invites') as b:
        b.add_column(sa.Column('member_id', sa.String(length=36), nullable=True))
    op.create_table(
        'campaign_exports',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('campaign_id', sa.String(length=36), nullable=False),
        sa.Column('requested_by_user_id', sa.String(length=36), nullable=True),
        sa.Column('state', sa.String(length=16), nullable=False),
        sa.Column('progress', sa.Float(), nullable=True),
        sa.Column('size_bytes', sa.Integer(), nullable=True),
        sa.Column('file_name', sa.String(length=300), nullable=True),
        sa.Column('consented_member_ids', sa.Text(), nullable=False),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_campaign_exports_campaign_id', 'campaign_exports', ['campaign_id'])
    op.create_table(
        'campaign_imports',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('file_name', sa.String(length=300), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('chunk_size', sa.Integer(), nullable=False),
        sa.Column('chunk_count', sa.Integer(), nullable=False),
        sa.Column('state', sa.String(length=16), nullable=False),
        sa.Column('progress', sa.Float(), nullable=True),
        sa.Column('campaign_id', sa.String(length=36), nullable=True),
        sa.Column('open_seats', sa.Integer(), nullable=True),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_campaign_imports_user_id', 'campaign_imports', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_campaign_imports_user_id', table_name='campaign_imports')
    op.drop_table('campaign_imports')
    op.drop_index('ix_campaign_exports_campaign_id', table_name='campaign_exports')
    op.drop_table('campaign_exports')
    with op.batch_alter_table('invites') as b:
        b.drop_column('member_id')
    with op.batch_alter_table('campaigns') as b:
        b.drop_column('imported_by_member_id')
        b.drop_column('imported_by_user_id')
    with op.batch_alter_table('members') as b:
        b.drop_column('open_seat')
        b.drop_column('move_consent_at')
