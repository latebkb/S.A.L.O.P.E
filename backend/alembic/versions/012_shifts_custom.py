"""Shifts custom (postes parallèles au bar, ex. entrée).

Revision ID: 012
Revises: 011
Create Date: 2026-11-18 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "012"
down_revision: Union[str, None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "shifts_custom",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column(
            "evenement_id",
            sa.Integer(),
            sa.ForeignKey("events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "creneau_id",
            sa.Integer(),
            sa.ForeignKey("slots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("nom", sa.String(), nullable=False),
        sa.Column("max_personnes", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("creneau_id", "nom", name="uq_shift_custom"),
    )
    op.add_column(
        "assignments",
        sa.Column(
            "shift_custom_id",
            sa.Integer(),
            sa.ForeignKey("shifts_custom.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("assignments", "shift_custom_id")
    op.drop_table("shifts_custom")
