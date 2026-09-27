"""Schnittstelle 0.3.4–0.3.7: externe Transkription (Schalter), Transkriptionsart, „Wer weiß was“

Revision ID: c7d2f9a4e3b5
Revises: b1a0e5c2d7f1
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c7d2f9a4e3b5"
down_revision: Union[str, Sequence[str], None] = "b1a0e5c2d7f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("campaigns") as b:
        b.add_column(sa.Column("allow_external_transcription", sa.Boolean(), nullable=False,
                               server_default=sa.false()))
    with op.batch_alter_table("sessions") as b:
        b.add_column(sa.Column("transcription_engine", sa.String(16), nullable=True))
    op.create_table(
        "entry_hidden",
        sa.Column("entry_id", sa.String(36), sa.ForeignKey("entries.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("member_id", sa.String(36), sa.ForeignKey("members.id", ondelete="CASCADE"), primary_key=True),
    )


def downgrade() -> None:
    raise NotImplementedError("Zurückstufen wird nicht unterstützt – bitte die Sicherung der Datenbank einspielen.")
