"""Local transcripts, sale deadlines, reason detail, and purchased goal shelf.

Revision ID: 0005
Revises: 0004
"""

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("items", sa.Column("reason_other_text", sa.String(300)))
    op.add_column("items", sa.Column("sale_ends_on", sa.Date()))
    op.add_column("simulations", sa.Column("local_result", sa.JSON()))
    op.drop_constraint("ck_simulation_duration", "simulations", type_="check")
    op.create_check_constraint("ck_simulation_duration", "simulations", "duration_days >= 0")
    op.add_column("goals", sa.Column("image_url", sa.Text()))
    op.add_column("goals", sa.Column("purchased_on", sa.Date()))
    op.add_column("goals", sa.Column("actual_price", sa.BigInteger()))
    op.add_column("goals", sa.Column("purchase_id", sa.String(36)))
    op.add_column("goals", sa.Column("shelf_snapshot", sa.JSON()))
    op.create_foreign_key("fk_goal_purchase", "goals", "purchases", ["purchase_id"], ["id"])
    op.create_unique_constraint("uq_goal_purchase", "goals", ["purchase_id"])


def downgrade():
    if op.get_bind().scalar(
        sa.text("""SELECT
        EXISTS (SELECT 1 FROM items WHERE reason_other_text IS NOT NULL OR sale_ends_on IS NOT NULL) OR
        EXISTS (SELECT 1 FROM simulations WHERE question_version = 'local-v1' OR duration_days < 3) OR
        EXISTS (SELECT 1 FROM goals WHERE image_url IS NOT NULL OR purchased_on IS NOT NULL)
    """)
    ):
        raise RuntimeError("0005 contains handoff data; restore a backup or apply a forward fix.")
    op.drop_constraint("uq_goal_purchase", "goals", type_="unique")
    op.drop_constraint("fk_goal_purchase", "goals", type_="foreignkey")
    for column in ("shelf_snapshot", "purchase_id", "actual_price", "purchased_on", "image_url"):
        op.drop_column("goals", column)
    op.drop_constraint("ck_simulation_duration", "simulations", type_="check")
    op.create_check_constraint("ck_simulation_duration", "simulations", "duration_days >= 3")
    op.drop_column("simulations", "local_result")
    op.drop_column("items", "sale_ends_on")
    op.drop_column("items", "reason_other_text")
