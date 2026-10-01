"""Charaktere (Schnittstelle 0.4.7): Serverkopie am Mitglied, pc-Einträge, mitgebrachte Welt, Hinweise an die SL,
Gäste bei der Stimmzuordnung

Revision ID: f2c6a8e0b4d7
Revises: e4a8c2f6b1d3
Create Date: 2026-10-01 10:00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'f2c6a8e0b4d7'
down_revision: Union[str, Sequence[str], None] = 'e4a8c2f6b1d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None



def upgrade() -> None:
    with op.batch_alter_table('members') as b:
        b.add_column(sa.Column('character_id', sa.String(length=36), nullable=True))
        b.add_column(sa.Column('character_version', sa.Integer(), nullable=True))
        b.add_column(sa.Column('character_status', sa.String(length=16), nullable=True))
        b.add_column(sa.Column('character_nickname', sa.String(length=64), nullable=True))
        b.add_column(sa.Column('character_system', sa.String(length=64), nullable=True))
        b.create_index('ix_members_character_id', ['character_id'])
    with op.batch_alter_table('entries') as b:
        b.add_column(sa.Column('pc_character_id', sa.String(length=36), nullable=True))
        b.add_column(sa.Column('origin_character_id', sa.String(length=36), nullable=True))
        b.add_column(sa.Column('origin_entry_id', sa.String(length=36), nullable=True))
        b.add_column(sa.Column('origin_version', sa.Integer(), nullable=True))
        b.add_column(sa.Column('origin_member_id', sa.String(length=36), nullable=True))
        b.create_index('ix_entries_pc_character_id', ['pc_character_id'])
        b.create_index('ix_entries_origin_character_id', ['origin_character_id'])
    with op.batch_alter_table('speakers') as b:
        b.add_column(sa.Column('assigned_guest_name', sa.String(length=200), nullable=True))
    with op.batch_alter_table('proposals') as b:
        b.add_column(sa.Column('source', sa.String(length=16), nullable=True))
        b.add_column(sa.Column('origin_character_id', sa.String(length=36), nullable=True))
        b.add_column(sa.Column('origin_entry_id', sa.String(length=36), nullable=True))
        b.add_column(sa.Column('origin_version', sa.Integer(), nullable=True))
        b.add_column(sa.Column('submitted_by_member_id', sa.String(length=36), nullable=True))
        b.add_column(sa.Column('decided_at', sa.DateTime(), nullable=True))
        b.create_index('ix_proposals_submitted_by_member_id', ['submitted_by_member_id'])
    op.create_table(
        'gm_notices',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('campaign_id', sa.String(length=36), nullable=False),
        sa.Column('code', sa.String(length=48), nullable=False),
        sa.Column('member_id', sa.String(length=36), nullable=True),
        sa.Column('entry_ids', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_gm_notices_campaign_id', 'gm_notices', ['campaign_id'])


def downgrade() -> None:
    op.drop_index('ix_gm_notices_campaign_id', table_name='gm_notices')
    op.drop_table('gm_notices')
    with op.batch_alter_table('proposals') as b:
        b.drop_index('ix_proposals_submitted_by_member_id')
        for s in ('decided_at', 'submitted_by_member_id', 'origin_version', 'origin_entry_id', 'origin_character_id',
                  'source'):
            b.drop_column(s)
    with op.batch_alter_table('speakers') as b:
        b.drop_column('assigned_guest_name')
    with op.batch_alter_table('entries') as b:
        b.drop_index('ix_entries_origin_character_id')
        b.drop_index('ix_entries_pc_character_id')
        for s in ('origin_member_id', 'origin_version', 'origin_entry_id', 'origin_character_id', 'pc_character_id'):
            b.drop_column(s)
    with op.batch_alter_table('members') as b:
        b.drop_index('ix_members_character_id')
        for s in ('character_system', 'character_nickname', 'character_status', 'character_version', 'character_id'):
            b.drop_column(s)
