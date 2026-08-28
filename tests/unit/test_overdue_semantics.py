"""Unit tests for overdue day math and rendering.

Covers the two bugs that made the daily report cry wolf: assignments flipping to
"overdue" the evening before they were due (UTC vs. local date), and rendering
"0 days overdue" for work that is not late at all.
"""

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.reporting import days_overdue_label, is_due_today, is_overdue, whole_days_overdue

pytestmark = pytest.mark.unit

DATABASE_DIR = Path(__file__).parent.parent.parent / "src" / "database"


class TestWholeDaysOverdue:
    def test_none_is_zero(self):
        assert whole_days_overdue(None) == 0

    def test_fraction_of_a_day_is_not_overdue(self):
        # 0.54 is the old UTC-offset artifact for an item due today.
        assert whole_days_overdue(0.54) == 0
        assert is_overdue(0.54) is False
        assert is_due_today(0.54) is True

    def test_full_day_late_counts(self):
        assert whole_days_overdue(1.0) == 1
        assert whole_days_overdue(1.9) == 1
        assert is_overdue(1.0) is True

    def test_future_is_negative(self):
        assert whole_days_overdue(-2.0) == -2
        assert is_overdue(-2.0) is False
        assert is_due_today(-2.0) is False


class TestDaysOverdueLabel:
    def test_due_today_is_never_overdue(self):
        assert days_overdue_label(0) == "due today"
        assert days_overdue_label(0.54) == "due today"
        assert "overdue" not in days_overdue_label(0.54)

    def test_singular_and_plural(self):
        assert days_overdue_label(1) == "1 day overdue"
        assert days_overdue_label(3) == "3 days overdue"

    def test_future(self):
        assert days_overdue_label(-4) == "not yet due"


@pytest.fixture
def view_db(tmp_path: Path) -> sqlite3.Connection:
    """In-memory-ish DB with the real schema and views applied."""
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    conn.executescript((DATABASE_DIR / "schema.sql").read_text())
    conn.executescript((DATABASE_DIR / "views.sql").read_text())
    conn.execute(
        "INSERT INTO students (id, powerschool_id, first_name, last_name)"
        " VALUES (1, 'PS1', 'Daphne', '')"
    )
    return conn


def _add_assignment(conn, name, due, status="Missing"):
    conn.execute(
        "INSERT INTO assignments (student_id, course_name, assignment_name, due_date, status)"
        " VALUES (1, 'History', ?, ?, ?)",
        (name, due.isoformat(), status),
    )


def _days_overdue(conn, name):
    row = conn.execute(
        "SELECT days_overdue FROM v_missing_assignments WHERE assignment_name = ?", (name,)
    ).fetchone()
    return row[0]


class TestMissingAssignmentsView:
    def test_due_today_is_zero_days_overdue(self, view_db):
        _add_assignment(view_db, "Due Today", date.today())
        assert _days_overdue(view_db, "Due Today") == 0

    def test_due_tomorrow_is_negative(self, view_db):
        _add_assignment(view_db, "Tomorrow", date.today() + timedelta(days=1))
        assert _days_overdue(view_db, "Tomorrow") == -1

    def test_yesterday_is_one_day_overdue(self, view_db):
        _add_assignment(view_db, "Yesterday", date.today() - timedelta(days=1))
        assert _days_overdue(view_db, "Yesterday") == 1

    def test_values_are_whole_days(self, view_db):
        _add_assignment(view_db, "Old", date.today() - timedelta(days=5))
        assert _days_overdue(view_db, "Old") == 5
