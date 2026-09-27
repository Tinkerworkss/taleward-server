"""Rechenknechte dürfen auch Zusammenfassungen übernehmen (Fähigkeit llm, Schritt 5)

Revision ID: d2e8a4c6f0b1
Revises: c9d5f7a1b3e4
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'd2e8a4c6f0b1'
down_revision: Union[str, Sequence[str], None] = 'c9d5f7a1b3e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Ob ein Rechenknecht wirklich zusammenfasst, entscheidet er selbst (nur mit Ollama) und die Verwaltung.
    op.execute("UPDATE workers SET capabilities = 'asr,llm' WHERE capabilities = 'asr'")
    op.execute("UPDATE workers SET name = 'Lokaler Worker' WHERE local = 1 AND name = 'Dieser Rechner'")


def downgrade() -> None:
    op.execute("UPDATE workers SET capabilities = 'asr' WHERE capabilities = 'asr,llm'")
