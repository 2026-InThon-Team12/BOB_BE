"""Start calendar estimates, retaining legacy virtual-credit history."""

from datetime import datetime
from zoneinfo import ZoneInfo
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = depends_on = None


def upgrade():
    op.add_column("goals", sa.Column("funding", sa.JSON(), nullable=True))
    bind = op.get_bind()
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    goals = sa.table(
        "goals",
        sa.column("id"),
        sa.column("guest_id"),
        sa.column("active", sa.Boolean()),
        sa.column("achieved_on", sa.Date()),
        sa.column("funding", sa.JSON()),
    )
    expenses = sa.table(
        "expenses",
        sa.column("guest_id"),
        sa.column("amount"),
        sa.column("spent_on", sa.Date()),
        sa.column("voided", sa.Boolean()),
    )
    for row in bind.execute(
        sa.select(goals.c.id, goals.c.guest_id).where(goals.c.active.is_(True), goals.c.achieved_on.is_(None))
    ).all():
        spent = bind.scalar(
            sa.select(sa.func.coalesce(sa.func.sum(expenses.c.amount), 0)).where(
                expenses.c.guest_id == row.guest_id,
                expenses.c.spent_on >= today.replace(day=1),
                expenses.c.spent_on <= today,
                expenses.c.voided.is_(False),
            )
        )
        bind.execute(
            goals.update()
            .where(goals.c.id == row.id)
            .values(
                funding=dict(
                    startsOn=today.isoformat(),
                    openingBalance=0,
                    baselineSpent=int(spent),
                    reservedThisMonth=0,
                )
            )
        )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM goals WHERE funding IS NOT NULL")):
        raise RuntimeError("calendar funding history must be archived before downgrade")
    op.drop_column("goals", "funding")
