"""Preserve goal changes and allocations of original savings credits.

Revision ID: 0007
Revises: 0006
"""

from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "goal_replacements",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("guest_id", sa.String(36), sa.ForeignKey("guests.id"), nullable=False),
        sa.Column("from_goal_id", sa.String(36), sa.ForeignKey("goals.id"), nullable=False, unique=True),
        sa.Column("to_goal_id", sa.String(36), sa.ForeignKey("goals.id"), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("carry_savings", sa.Boolean(), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("guest_id", "idempotency_key"),
    )
    op.create_index("ix_goal_replacements_guest_id", "goal_replacements", ["guest_id"])
    op.create_table(
        "goal_saving_transfers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("guest_id", sa.String(36), sa.ForeignKey("guests.id"), nullable=False),
        sa.Column("replacement_id", sa.String(36), sa.ForeignKey("goal_replacements.id"), nullable=False),
        sa.Column("credit_id", sa.String(36), sa.ForeignKey("saving_events.id"), nullable=False),
        sa.Column("from_goal_id", sa.String(36), sa.ForeignKey("goals.id"), nullable=False),
        sa.Column("to_goal_id", sa.String(36), sa.ForeignKey("goals.id"), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("replacement_id", "credit_id"),
        sa.CheckConstraint("amount > 0", name="ck_goal_transfer_amount"),
        sa.CheckConstraint("from_goal_id <> to_goal_id", name="ck_goal_transfer_destination"),
    )
    for column in ("guest_id", "credit_id", "from_goal_id", "to_goal_id"):
        op.create_index(f"ix_goal_saving_transfers_{column}", "goal_saving_transfers", [column])


def downgrade():
    db = op.get_bind()
    if db.scalar(sa.text("SELECT count(*) FROM goal_replacements")):
        raise RuntimeError("Cannot downgrade: goal replacement history would be lost")
    op.drop_table("goal_saving_transfers")
    op.drop_table("goal_replacements")
