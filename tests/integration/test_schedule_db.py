"""Integration tests for the schedules table and repository methods.

These tests verify schedule upsert/query operations work correctly with
actual SQLite operations using the project schema.
"""

import sqlite3
from pathlib import Path
from typing import Generator

import pytest

from src.database.repository import Repository

pytestmark = pytest.mark.integration


@pytest.fixture(scope="function")
def repo(tmp_path: Path) -> Generator[Repository, None, None]:
    """Create a Repository pointed at a temporary database with schema loaded."""
    db_path = tmp_path / "test_schedule.db"

    schema_path = Path(__file__).parent.parent.parent / "src" / "database" / "schema.sql"
    views_path = Path(__file__).parent.parent.parent / "src" / "database" / "views.sql"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    if schema_path.exists():
        with open(schema_path) as f:
            conn.executescript(f.read())
    if views_path.exists():
        with open(views_path) as f:
            conn.executescript(f.read())

    # Insert two test students
    conn.execute(
        """
        INSERT INTO students (id, powerschool_id, first_name, last_name, grade_level, school_name)
        VALUES (1, '1001', 'Daphne', 'Test', '8', 'Test Middle School')
        """
    )
    conn.execute(
        """
        INSERT INTO students (id, powerschool_id, first_name, last_name, grade_level, school_name)
        VALUES (2, '1002', 'Ivy', 'Test', '6', 'Test Middle School')
        """
    )
    conn.commit()
    conn.close()

    yield Repository(db_path=db_path)


def test_upsert_schedule_inserts(repo: Repository) -> None:
    """A new schedule entry is inserted and returns an id."""
    schedule_id = repo.upsert_schedule(
        student_id=1,
        course_name="Physical Education, (Grades 7-8)",
        expression="2.8(A)",
        term="26-27",
        course_section="08037G0708-6",
        teacher_name="Miller, Melissa L",
        room="GYM",
        enroll_date="2026-08-12",
        leave_date="2027-05-28",
    )
    assert isinstance(schedule_id, int)
    assert schedule_id > 0


def test_upsert_schedule_updates_in_place(repo: Repository) -> None:
    """Re-inserting the same entry updates fields instead of duplicating."""
    repo.upsert_schedule(
        student_id=1,
        course_name="Spanish 1",
        expression="4.8(A)",
        term="26-27",
        course_section="24052G1000-1174",
        teacher_name="Stroud, Megan Sue",
        room="A117",
    )
    repo.upsert_schedule(
        student_id=1,
        course_name="Spanish 1",
        expression="4.8(A)",
        term="26-27",
        course_section="24052G1000-1174",
        teacher_name="Stroud, Megan Sue",
        room="A118",  # room changed
    )

    schedule = repo.get_schedule(1)
    assert len(schedule) == 1
    assert schedule[0]["room"] == "A118"


def test_upsert_schedule_separates_students(repo: Repository) -> None:
    """Schedules for different students are kept separate."""
    repo.upsert_schedule(
        student_id=1,
        course_name="Algebra",
        expression="3.8(A)",
        term="26-27",
        course_section="SEC1",
    )
    repo.upsert_schedule(
        student_id=2,
        course_name="Reading",
        expression="1.6(A)",
        term="26-27",
        course_section="SEC2",
    )

    assert len(repo.get_schedule(1)) == 1
    assert len(repo.get_schedule(2)) == 1
    assert repo.get_schedule(1)[0]["course_name"] == "Algebra"
    assert repo.get_schedule(2)[0]["course_name"] == "Reading"


def test_upsert_schedule_dedupes_without_course_section(repo: Repository) -> None:
    """Entries with no course section still update in place rather than duplicating.

    SQLite treats NULLs as distinct in a UNIQUE constraint, so the key columns
    must be normalized to "" for the upsert to match.
    """
    for room in ("A1", "A2"):
        repo.upsert_schedule(
            student_id=1,
            course_name="Math",
            expression="1(A)",
            term="26-27",
            course_section=None,
            room=room,
        )

    schedule = repo.get_schedule(1)
    assert len(schedule) == 1
    assert schedule[0]["room"] == "A2"


def test_upsert_schedule_dedupes_with_no_key_fields(repo: Repository) -> None:
    """Course name alone is enough to dedupe when nothing else is recorded."""
    repo.upsert_schedule(student_id=1, course_name="Homeroom")
    repo.upsert_schedule(student_id=1, course_name="Homeroom")

    assert len(repo.get_schedule(1)) == 1


def test_upsert_schedule_clears_removed_fields(repo: Repository) -> None:
    """A field that disappears upstream is cleared, not retained from the old row."""
    repo.upsert_schedule(
        student_id=1,
        course_name="Band",
        expression="5.8(A)",
        term="26-27",
        course_section="SEC9",
        teacher_name="Old, Teacher",
        room="B12",
        leave_date="2027-05-28",
    )
    repo.upsert_schedule(
        student_id=1,
        course_name="Band",
        expression="5.8(A)",
        term="26-27",
        course_section="SEC9",
        teacher_name="New, Teacher",
        room=None,
        leave_date=None,
    )

    schedule = repo.get_schedule(1)
    assert len(schedule) == 1
    assert schedule[0]["teacher_name"] == "New, Teacher"
    assert schedule[0]["room"] is None
    assert schedule[0]["leave_date"] is None


def test_get_schedule_orders_by_expression(repo: Repository) -> None:
    """Schedules are returned ordered by period expression."""
    for expr, course in [("4.8(A)", "Spanish"), ("2.8(A)", "PE"), ("3.8(A)", "Math")]:
        repo.upsert_schedule(
            student_id=1,
            course_name=course,
            expression=expr,
            term="26-27",
        )

    schedule = repo.get_schedule(1)
    assert [s["expression"] for s in schedule] == ["2.8(A)", "3.8(A)", "4.8(A)"]


def test_get_schedule_orders_periods_numerically(repo: Repository) -> None:
    """Multi-digit periods sort after single-digit ones, not lexicographically."""
    for expr in ("178(A)", "2.8(A)", "1.6(A)", "10.8(A)"):
        repo.upsert_schedule(
            student_id=1,
            course_name=f"Course {expr}",
            expression=expr,
            term="26-27",
        )

    schedule = repo.get_schedule(1)
    assert [s["expression"] for s in schedule] == ["1.6(A)", "2.8(A)", "10.8(A)", "178(A)"]


def test_get_schedule_orders_unnumbered_expressions_last(repo: Repository) -> None:
    """Expressions with no leading period number sort after numbered ones."""
    repo.upsert_schedule(student_id=1, course_name="Advisory", expression="", term="26-27")
    repo.upsert_schedule(student_id=1, course_name="Lunch", expression="LUNCH", term="26-27")
    repo.upsert_schedule(student_id=1, course_name="Math", expression="3.8(A)", term="26-27")

    assert [s["expression"] for s in repo.get_schedule(1)] == ["3.8(A)", "", "LUNCH"]


def test_get_schedule_terms(repo: Repository) -> None:
    """Distinct terms are returned sorted newest-first."""
    repo.upsert_schedule(student_id=1, course_name="Math", expression="1(A)", term="25-26")
    repo.upsert_schedule(student_id=1, course_name="Math", expression="1(A)", term="26-27")

    assert repo.get_schedule_terms(1) == ["26-27", "25-26"]


def test_get_schedule_terms_ignores_missing_terms(repo: Repository) -> None:
    """Entries with no term don't produce a bogus term the CLI would default to."""
    repo.upsert_schedule(student_id=1, course_name="Math", expression="1(A)", term="26-27")
    repo.upsert_schedule(student_id=1, course_name="Lunch", expression="2(A)", term=None)

    assert repo.get_schedule_terms(1) == ["26-27"]


def test_get_schedule_without_term_returns_all(repo: Repository) -> None:
    """A falsy term filter returns every entry rather than none."""
    repo.upsert_schedule(student_id=1, course_name="Math", expression="1(A)", term="25-26")
    repo.upsert_schedule(student_id=1, course_name="Science", expression="2(A)", term="26-27")

    assert len(repo.get_schedule(1, term=None)) == 2
    assert len(repo.get_schedule(1, term="")) == 2


def test_get_schedule_term_filter(repo: Repository) -> None:
    """Term filter narrows results."""
    repo.upsert_schedule(student_id=1, course_name="Math", expression="1(A)", term="25-26")
    repo.upsert_schedule(student_id=1, course_name="Science", expression="2(A)", term="26-27")

    schedule = repo.get_schedule(1, term="26-27")
    assert len(schedule) == 1
    assert schedule[0]["course_name"] == "Science"
