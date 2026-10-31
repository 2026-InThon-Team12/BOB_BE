"""Display goals on achievement and retain the goal's external product URL.

Revision ID: 0006
Revises: 0005
"""

from collections import Counter
from datetime import timezone
from zoneinfo import ZoneInfo

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("goals", sa.Column("achieved_on", sa.Date()))
    op.add_column("goals", sa.Column("source_url", sa.Text()))
    db = op.get_bind()
    goals = sa.table(
        "goals",
        sa.column("id", sa.String()),
        sa.column("achieved_on", sa.Date()),
        sa.column("shelf_snapshot", sa.JSON()),
    )
    for goal in db.execute(sa.text("SELECT * FROM goals")).mappings().all():
        created = goal["created_at"]
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        created_on = created.astimezone(ZoneInfo("Asia/Seoul")).date()
        events = db.execute(
            sa.text("""SELECT e.*, COALESCE(i.name, t.name) AS label
            FROM saving_events e LEFT JOIN impulses i ON i.id = e.impulse_id
            LEFT JOIN items t ON t.id = e.item_id
            WHERE e.goal_id = :goal_id AND e.guest_id = :guest_id
            ORDER BY e.created_at, e.id"""),
            {"goal_id": goal["id"], "guest_id": goal["guest_id"]},
        ).mappings()
        total, credits, achieved, snapshot = 0, {}, None, None
        for event in events:
            key = (event["impulse_id"], event["item_id"])
            if event["kind"] == "CREDIT":
                credits[key] = event["label"]
            else:
                credits.pop(key, None)
            total += event["amount"]
            if total >= goal["target_amount"]:
                event_on = (
                    event["occurred_on"] or event["created_at"].astimezone(ZoneInfo("Asia/Seoul")).date()
                )
                achieved = max(created_on, event_on)
                labels = Counter(label for label in credits.values() if label)
                snapshot = {
                    "saved_amount": total,
                    "resisted_count": len(credits),
                    "elapsed_days": max(1, (achieved - created_on).days + 1),
                    "most_resisted_item": labels.most_common(1)[0][0] if labels else None,
                }
                break
        # Previous completed goals were only archived after reaching their target.
        achieved = achieved or goal["completed_on"] or goal["purchased_on"]
        if achieved:
            db.execute(
                goals.update()
                .where(goals.c.id == goal["id"])
                .values(
                    achieved_on=max(created_on, achieved), shelf_snapshot=snapshot or goal["shelf_snapshot"]
                )
            )


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM goals WHERE achieved_on IS NOT NULL OR source_url IS NOT NULL)")
    ):
        raise RuntimeError("0006 contains achievement history or product URLs; apply a forward fix.")
    op.drop_column("goals", "source_url")
    op.drop_column("goals", "achieved_on")
