"""Recap-Modell: bisheriger Standard ministral-3:8b wird „auto“ (nach Grafikkarte des Workers)

Modellvergleich vom 01.10.2026: gemma4:e4b bzw. gemma4:12b schreiben deutlich bessere Recaps. Wer ein anderes Modell
eingetragen hat, behält es.

Revision ID: b7e2c4f9a1d3
Revises: a3d9f1c7e5b2
Create Date: 2026-10-02 10:00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b7e2c4f9a1d3'
down_revision: Union[str, Sequence[str], None] = 'a3d9f1c7e5b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("UPDATE server_meta SET value = 'auto' "
                       "WHERE key = 'llm.lokal_modell' AND value = 'ministral-3:8b'"))
    op.execute(sa.text("UPDATE server_meta SET value = '32768' "
                       "WHERE key = 'llm.lokal_kontext' AND value = '12288'"))


def downgrade() -> None:
    op.execute(sa.text("UPDATE server_meta SET value = 'ministral-3:8b' "
                       "WHERE key = 'llm.lokal_modell' AND value = 'auto'"))
