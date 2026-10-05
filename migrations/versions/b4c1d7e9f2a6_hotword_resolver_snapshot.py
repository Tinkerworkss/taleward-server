"""Hotword-Resolver: reproduzierbarer Snapshot der tatsächlich verwendeten Kurzliste

Revision ID: b4c1d7e9f2a6
Revises: a3d9f1c7e5b2
Create Date: 2026-10-05 16:20:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b4c1d7e9f2a6"
down_revision: Union[str, Sequence[str], None] = "a3d9f1c7e5b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("sessions") as b:
        b.add_column(sa.Column("hotword_snapshot", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("sessions") as b:
        b.drop_column("hotword_snapshot")
