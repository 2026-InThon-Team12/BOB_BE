"""Pre-purchase question bank and purchase-time prediction snapshots.

Revision ID: 0004
Revises: 0003
"""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("budgets", sa.Column("hourly_wage", sa.BigInteger(), nullable=True))
    op.add_column("purchases", sa.Column("pre_purchase_snapshot", sa.JSON(), nullable=True))
    op.create_table(
        "simulation_questions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("guest_id", sa.String(36), sa.ForeignKey("guests.id"), nullable=False),
        sa.Column("simulation_id", sa.String(36), sa.ForeignKey("simulations.id"), nullable=False),
        sa.Column("question_date", sa.Date(), nullable=False),
        sa.Column("template_code", sa.String(32), nullable=False),
        sa.Column("template_version", sa.String(32), nullable=False),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("answer_spec", sa.JSON(), nullable=False),
        sa.Column("context_snapshot", sa.JSON(), nullable=False),
        sa.Column("generation_method", sa.String(16), nullable=False),
        sa.Column("model_id", sa.String(200)),
        sa.Column("fallback_reason", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("simulation_id", "question_date"),
    )
    op.create_index("ix_simulation_questions_guest_id", "simulation_questions", ["guest_id"])
    op.create_index("ix_simulation_questions_simulation_id", "simulation_questions", ["simulation_id"])
    op.create_table(
        "simulation_question_answers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "question_id",
            sa.String(36),
            sa.ForeignKey("simulation_questions.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("choice_code", sa.String(32)),
        sa.Column("number_value", sa.Integer()),
        sa.Column("text_value", sa.String(300)),
        sa.Column("object_value", sa.JSON()),
        sa.Column("answered_on", sa.Date(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "CASE WHEN choice_code IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN number_value IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN text_value IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN object_value IS NOT NULL THEN 1 ELSE 0 END = 1",
            name="ck_simulation_answer_value",
        ),
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM simulation_questions) OR "
            "EXISTS (SELECT 1 FROM budgets WHERE hourly_wage IS NOT NULL) OR "
            "EXISTS (SELECT 1 FROM purchases WHERE pre_purchase_snapshot IS NOT NULL AND pre_purchase_snapshot::text <> 'null')"
        )
    ):
        raise RuntimeError("0004 contains pre-purchase data; restore a backup or apply a forward fix.")
    op.drop_table("simulation_question_answers")
    op.drop_index("ix_simulation_questions_simulation_id", table_name="simulation_questions")
    op.drop_index("ix_simulation_questions_guest_id", table_name="simulation_questions")
    op.drop_table("simulation_questions")
    op.drop_column("purchases", "pre_purchase_snapshot")
    op.drop_column("budgets", "hourly_wage")
