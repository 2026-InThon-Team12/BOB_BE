"""Versioned daily post-purchase questions and their answers.

Revision ID: 0003
Revises: 0002
"""

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "purchase_questions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("guest_id", sa.String(36), sa.ForeignKey("guests.id"), nullable=False),
        sa.Column("purchase_id", sa.String(36), sa.ForeignKey("purchases.id"), nullable=False),
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
        sa.UniqueConstraint("purchase_id", "question_date"),
    )
    op.create_index("ix_purchase_questions_guest_id", "purchase_questions", ["guest_id"])
    op.create_index("ix_purchase_questions_purchase_id", "purchase_questions", ["purchase_id"])
    op.create_table(
        "purchase_question_answers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "question_id", sa.String(36), sa.ForeignKey("purchase_questions.id"), nullable=False, unique=True
        ),
        sa.Column("choice_code", sa.String(32)),
        sa.Column("number_value", sa.BigInteger()),
        sa.Column("answered_on", sa.Date(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(choice_code IS NULL) <> (number_value IS NULL)", name="ck_purchase_answer_value"
        ),
    )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM purchase_questions)")):
        raise RuntimeError("0003 contains question history; restore a backup or apply a forward fix.")
    op.drop_table("purchase_question_answers")
    op.drop_index("ix_purchase_questions_purchase_id", table_name="purchase_questions")
    op.drop_index("ix_purchase_questions_guest_id", table_name="purchase_questions")
    op.drop_table("purchase_questions")
