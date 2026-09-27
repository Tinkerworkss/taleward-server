"""Schritt 1b: Grundlagen aus Schnittstelle 0.3.3

Organisation, Anmeldearten getrennt vom Konto, Server-ID, Kampagnen- und Charakterfelder,
Einwilligung mit Gästen und Protokoll, geheimer Teil von Bibeleinträgen, Gelesen-Marker.

Revision ID: b1a0e5c2d7f1
Revises: c05348d6cac5
"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b1a0e5c2d7f1"
down_revision: Union[str, Sequence[str], None] = "c05348d6cac5"
branch_labels = None
depends_on = None


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def upgrade() -> None:
    conn = op.get_bind()

    # --- neue Tabellen -------------------------------------------------------
    op.create_table(
        "server_meta",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
    )
    op.create_table(
        "auth_methods",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("secret", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("user_id", "kind"),
    )
    op.create_index("ix_auth_methods_user_id", "auth_methods", ["user_id"])
    op.create_table(
        "organizations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "org_members",
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("joined_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_org_members_user_id", "org_members", ["user_id"])
    op.create_table(
        "consent_log",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("campaign_id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=True),
        sa.Column("member_id", sa.String(36), nullable=True),
        sa.Column("user_id", sa.String(36), nullable=True),
        sa.Column("guest_name", sa.String(200), nullable=True),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("recorded_by_user_id", sa.String(36), nullable=True),
        sa.Column("at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_consent_log_campaign_id", "consent_log", ["campaign_id"])
    op.create_table(
        "session_seen",
        sa.Column("member_id", sa.String(36), sa.ForeignKey("members.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("seen_at", sa.DateTime(), nullable=False),
    )

    # --- Passwörter in Anmeldearten verschieben ------------------------------
    for uid, pw_hash, created in conn.execute(sa.text("SELECT id, password_hash, created_at FROM users")).fetchall():
        conn.execute(
            sa.text("INSERT INTO auth_methods (id, user_id, kind, secret, created_at) VALUES (:i, :u, 'password', :s, :c)"),
            {"i": str(uuid.uuid4()), "u": uid, "s": pw_hash, "c": created or _now()},
        )
    with op.batch_alter_table("users") as b:
        b.drop_column("password_hash")

    # --- Kampagnen -----------------------------------------------------------
    with op.batch_alter_table("campaigns") as b:
        b.add_column(sa.Column("organization_id", sa.String(36),
                               sa.ForeignKey("organizations.id", name="fk_campaigns_organization", ondelete="SET NULL"),
                               nullable=True))
        b.add_column(sa.Column("language", sa.String(8), nullable=False, server_default="de"))
        b.add_column(sa.Column("system", sa.String(32), nullable=True))
        b.add_column(sa.Column("system_name", sa.String(100), nullable=True))
        b.add_column(sa.Column("world_info", sa.Text(), nullable=True))
        b.add_column(sa.Column("cover_preset", sa.String(32), nullable=True))
        b.add_column(sa.Column("cover_image_updated_at", sa.DateTime(), nullable=True))
        b.add_column(sa.Column("next_session_at", sa.DateTime(), nullable=True))
        b.create_index("ix_campaigns_organization_id", ["organization_id"])

    # Standard-Organisation anlegen und alles Vorhandene zuordnen
    from app.config import get_settings  # Name aus der .env

    org_id = str(uuid.uuid4())
    conn.execute(sa.text("INSERT INTO organizations (id, name, created_at) VALUES (:i, :n, :c)"),
                 {"i": org_id, "n": get_settings().organization_name, "c": _now()})
    users = [r[0] for r in conn.execute(sa.text("SELECT id FROM users ORDER BY created_at")).fetchall()]
    for uid in users:
        conn.execute(
            sa.text("INSERT INTO org_members (organization_id, user_id, role, joined_at) VALUES (:o, :u, :r, :c)"),
            {"o": org_id, "u": uid, "r": "member", "c": _now()},
        )
    conn.execute(sa.text("UPDATE campaigns SET organization_id = :o"), {"o": org_id})
    # Vorhandene Kampagnen bekommen – wie neue – ein zufälliges mitgeliefertes Titelmotiv
    import random

    motive = ["meadow", "forest", "desert", "city", "cyber", "mountains", "coast", "swamp", "dungeon", "space", "castle"]
    for (cid,) in conn.execute(sa.text("SELECT id FROM campaigns")).fetchall():
        conn.execute(sa.text("UPDATE campaigns SET cover_preset = :p WHERE id = :i"), {"p": random.choice(motive), "i": cid})

    # --- Mitglieder ----------------------------------------------------------
    with op.batch_alter_table("members") as b:
        b.add_column(sa.Column("character_summary", sa.Text(), nullable=True))
        b.add_column(sa.Column("character_backstory", sa.Text(), nullable=True))
        b.add_column(sa.Column("portrait_updated_at", sa.DateTime(), nullable=True))
        b.add_column(sa.Column("recording_consent_at", sa.DateTime(), nullable=True))
        b.add_column(sa.Column("chronicle_seen_at", sa.DateTime(), nullable=True))
        b.add_column(sa.Column("bible_seen_at", sa.DateTime(), nullable=True))

    # --- Bibel ---------------------------------------------------------------
    with op.batch_alter_table("entries") as b:
        b.add_column(sa.Column("gm_notes", sa.Text(), nullable=True))
        b.add_column(sa.Column("public_changed_at", sa.DateTime(), nullable=True))
    conn.execute(sa.text("UPDATE entries SET public_changed_at = updated_at WHERE visibility = 'public'"))

    # --- Anwesende: neues Format mit Gästen und Herkunft der Zustimmung ------
    op.create_table(
        "attendees_neu",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("member_id", sa.String(36), sa.ForeignKey("members.id", ondelete="CASCADE"), nullable=True),
        sa.Column("guest_name", sa.String(200), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("consent", sa.Boolean(), nullable=False),
        sa.Column("consent_source", sa.String(16), nullable=False),
        sa.Column("consent_at", sa.DateTime(), nullable=True),
    )
    alt = conn.execute(sa.text("SELECT session_id, member_id, position, consent, consent_at FROM attendees")).fetchall()
    for sid, mid, pos, consent, cat in alt:
        conn.execute(
            sa.text(
                "INSERT INTO attendees_neu (id, session_id, member_id, guest_name, position, consent, consent_source, consent_at) "
                "VALUES (:i, :s, :m, NULL, :p, :c, 'on_site', :a)"
            ),
            {"i": str(uuid.uuid4()), "s": sid, "m": mid, "p": pos, "c": consent, "a": cat},
        )
    op.drop_table("attendees")
    op.rename_table("attendees_neu", "attendees")
    op.create_index("ix_attendees_session_id", "attendees", ["session_id"])


def downgrade() -> None:
    raise NotImplementedError("Zurückstufen wird nicht unterstützt – bitte die Sicherung der Datenbank einspielen.")
