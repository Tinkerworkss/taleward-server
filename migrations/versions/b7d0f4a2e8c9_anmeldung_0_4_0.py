"""Schnittstelle 0.3.10 und 0.4.0: allow_cloud_summary, E-Mail am Konto, Einmal-Tokens

Revision ID: b7d0f4a2e8c9
Revises: a6c9e3f1d4b7
Create Date: 2026-09-27 14:00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b7d0f4a2e8c9'
down_revision: Union[str, Sequence[str], None] = 'a6c9e3f1d4b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('campaigns') as b:
        b.add_column(sa.Column('allow_cloud_summary', sa.Boolean(), nullable=False, server_default='0'))
    # In der Betriebsart „Nur Cloud“ gibt es kein lokales Modell – bestehende Kampagnen dort wie neue behandeln
    op.execute("UPDATE campaigns SET allow_cloud_summary = 1 WHERE EXISTS "
               "(SELECT 1 FROM server_meta WHERE key = 'betrieb.art' AND value = 'cloud')")
    with op.batch_alter_table('users') as b:
        b.add_column(sa.Column('email', sa.String(length=254), nullable=True))
        b.add_column(sa.Column('email_pending', sa.String(length=254), nullable=True))
        b.add_column(sa.Column('email_verified_at', sa.DateTime(), nullable=True))
        b.add_column(sa.Column('token_ausnahme', sa.String(length=80), nullable=True))
        b.create_index('ix_users_email', ['email'])
    op.create_index('ix_auth_methods_kind_secret', 'auth_methods', ['kind', 'secret'])
    op.create_table(
        'einmal_tokens',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('art', sa.String(length=24), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=True),
        sa.Column('daten', sa.Text(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_einmal_tokens_art', 'einmal_tokens', ['art'])
    op.create_index('ix_einmal_tokens_user_id', 'einmal_tokens', ['user_id'])
    op.create_index('ix_einmal_tokens_expires_at', 'einmal_tokens', ['expires_at'])


def downgrade() -> None:
    op.drop_table('einmal_tokens')
    op.drop_index('ix_auth_methods_kind_secret', 'auth_methods')
    with op.batch_alter_table('users') as b:
        b.drop_index('ix_users_email')
        for c in ('token_ausnahme', 'email_verified_at', 'email_pending', 'email'):
            b.drop_column(c)
    with op.batch_alter_table('campaigns') as b:
        b.drop_column('allow_cloud_summary')
