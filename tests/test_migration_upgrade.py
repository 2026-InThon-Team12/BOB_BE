"""Run both real migrations with legacy records, independently of ORM create_all."""

import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text


def migration(name):
    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / name
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migration_connection():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        pytest.skip("PostgreSQL migration test")
    engine = create_engine(url)
    schema = "bob_migration_test_" + uuid4().hex
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        with engine.begin() as connection:
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            with Operations.context(MigrationContext.configure(connection)):
                yield connection
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


def test_empty_migration_round_trip(migration_connection):
    initial = migration("0001_initial_schema.py")
    design = migration("0002_design_flow.py")
    questions = migration("0003_purchase_questions.py")
    pre_questions = migration("0004_pre_purchase_questions.py")
    handoff = migration("0005_handoff_local_flow.py")
    shelf = migration("0006_achieved_goal_shelf.py")
    replacements = migration("0007_goal_replacements.py")
    funding = migration("0008_calendar_goal_funding.py")
    initial.upgrade()
    design.upgrade()
    questions.upgrade()
    pre_questions.upgrade()
    handoff.upgrade()
    shelf.upgrade()
    replacements.upgrade()
    funding.upgrade()
    funding.downgrade()
    replacements.downgrade()
    shelf.downgrade()
    handoff.downgrade()
    pre_questions.downgrade()
    questions.downgrade()
    design.downgrade()
    initial.downgrade()
    initial.upgrade()
    design.upgrade()
    questions.upgrade()
    pre_questions.upgrade()
    handoff.upgrade()
    shelf.upgrade()
    replacements.upgrade()
    funding.upgrade()


def test_upgrade_preserves_legacy_reviews_and_backfills_ledgers(migration_connection):
    db = migration_connection
    initial = migration("0001_initial_schema.py")
    design = migration("0002_design_flow.py")
    initial.upgrade()
    db.execute(text("INSERT INTO guests VALUES ('guest', :hash, '2026-10-01T00:00:00Z')"), {"hash": "a" * 64})
    db.execute(
        text("""INSERT INTO budgets (id, guest_id, month, income, food, living, transport)
        VALUES ('budget', 'guest', '2026-10', 1000000, 100000, 200000, 30000)""")
    )
    db.execute(
        text("""INSERT INTO goals (id, guest_id, name, target_amount, active, created_at)
        VALUES ('goal', 'guest', 'goal', 240000, true, now())""")
    )
    for key, decision in (("reviewed", "BUY"), ("pending", "BUY"), ("bye", "GIVE_UP")):
        db.execute(
            text("""INSERT INTO items (id, guest_id, name, price, category, purchase_reason, source, decision, created_at)
            VALUES (:id, 'guest', :id, 30000, 'HOBBY', 'STRESS', 'MANUAL', :decision, '2026-10-01T16:00:00Z')"""),
            {"id": key, "decision": decision},
        )
        db.execute(
            text("""INSERT INTO simulations (id, guest_id, item_id, price_snapshot, duration_days, start_date, end_date,
                measurement_question, supplemental_question, question_version)
            VALUES (:id, 'guest', :id, 30000, 3, '2026-10-03', '2026-10-06', 'question', 'question', 'v1')"""),
            {"id": key},
        )
        if decision == "BUY":
            db.execute(
                text("""INSERT INTO purchases (id, guest_id, item_id, purchased_on, actual_price, review_due_on, review_start, review_end)
                VALUES (:id, 'guest', :id, '2026-10-06', 27000, '2026-10-20', '2026-10-13', '2026-10-19')"""),
                {"id": key},
            )
    db.execute(text("INSERT INTO purchase_reviews VALUES ('review', 'reviewed', 2, '2026-10-20')"))
    db.execute(
        text("""INSERT INTO impulses (id, guest_id, goal_id, name, amount, decision, idempotency_key, request_hash, budget_month, created_at)
        VALUES ('impulse', 'guest', 'goal', 'coffee', 4800, 'PURCHASED', 'old-impulse', :hash, '2026-10', '2026-10-01T16:00:00Z')"""),
        {"hash": "b" * 64},
    )
    design.upgrade()
    assert db.scalar(text("SELECT savings FROM budgets")) == 0
    assert db.scalar(text("SELECT purchase_reasons FROM items WHERE id = 'bye'")) == ["STRESS"]
    assert str(db.scalar(text("SELECT review_start FROM purchases WHERE id = 'reviewed'"))) == "2026-10-13"
    assert str(db.scalar(text("SELECT review_start FROM purchases WHERE id = 'pending'"))) == "2026-10-06"
    assert db.scalar(text("SELECT used_days FROM purchase_reviews")) == 2
    assert db.scalar(text("SELECT count(*) FROM expenses")) == 3
    assert db.scalar(text("SELECT sum(amount) FROM expenses")) == 58800
    assert str(db.scalar(text("SELECT spent_on FROM expenses WHERE impulse_id = 'impulse'"))) == "2026-10-02"
    assert db.scalar(text("SELECT sum(amount) FROM saving_events WHERE goal_id = 'goal'")) == 30000
    assert db.scalar(text("SELECT count(*) FROM simulations WHERE base_duration_days = 3")) == 3
    with pytest.raises(RuntimeError, match="empty database"):
        design.downgrade()
    assert db.scalar(text("SELECT count(*) FROM expenses")) == 3
    questions = migration("0003_purchase_questions.py")
    questions.upgrade()
    assert db.scalar(text("SELECT used_days FROM purchase_reviews")) == 2
    assert db.scalar(text("SELECT count(*) FROM expenses")) == 3
    db.execute(
        text("""INSERT INTO purchase_questions (id, guest_id, purchase_id, question_date,
        template_code, template_version, stage, question_text, answer_spec, context_snapshot,
        generation_method, created_at) VALUES ('q', 'guest', 'reviewed', '2026-10-31', 'MOOD',
        'purchase-v1', 'IMMEDIATE', 'question', '{}', '{}', 'TEMPLATE', now())""")
    )
    db.execute(
        text("""INSERT INTO purchase_question_answers (id, question_id, choice_code, answered_on, updated_at)
        VALUES ('a', 'q', 'EXCITED', '2026-10-31', now())""")
    )
    with pytest.raises(RuntimeError, match="question history"):
        questions.downgrade()
    assert db.scalar(text("SELECT choice_code FROM purchase_question_answers")) == "EXCITED"
    pre = migration("0004_pre_purchase_questions.py")
    pre.upgrade()
    assert db.scalar(text("SELECT hourly_wage FROM budgets")) is None
    assert db.scalar(text("SELECT pre_purchase_snapshot FROM purchases WHERE id = 'reviewed'")) is None
    assert db.scalar(text("SELECT question_version FROM simulations WHERE id = 'reviewed'")) == "v1"
    db.execute(
        text("""INSERT INTO simulation_questions (id, guest_id, simulation_id, question_date,
        template_code, template_version, stage, question_text, answer_spec, context_snapshot,
        generation_method, created_at) VALUES ('pre-q', 'guest', 'pending', '2026-10-04',
        'EXPECTED_USE_DAYS', 'pre-v1', 'PREDICTION', 'question', '{}', '{}', 'TEMPLATE', now())""")
    )
    db.execute(
        text("""INSERT INTO simulation_question_answers (id, question_id, number_value, answered_on, updated_at)
        VALUES ('pre-a', 'pre-q', 15, '2026-10-04', now())""")
    )
    with pytest.raises(RuntimeError, match="pre-purchase data"):
        pre.downgrade()
    assert db.scalar(text("SELECT number_value FROM simulation_question_answers")) == 15
    assert db.scalar(text("SELECT choice_code FROM purchase_question_answers")) == "EXCITED"
