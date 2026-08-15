"""Integration tests for loading scraped schedule data into the database.

Exercises scripts/load_data.py's schedule branch end to end: student
resolution, key fallbacks, date normalization, and re-load idempotency.
"""

import sqlite3
import sys
from pathlib import Path
from typing import Generator

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.load_data import _load_scraped_data_inner  # noqa: E402
from src.database.repository import Repository  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.fixture(scope="function")
def repo(tmp_path: Path) -> Generator[Repository, None, None]:
    """Repository backed by a temporary database with the project schema."""
    db_path = tmp_path / "test_schedule_load.db"
    database_dir = Path(__file__).parent.parent.parent / "src" / "database"

    conn = sqlite3.connect(db_path)
    conn.executescript((database_dir / "schema.sql").read_text())
    conn.executescript((database_dir / "views.sql").read_text())
    conn.commit()
    conn.close()

    yield Repository(db_path=db_path)


def _scraped(schedule: list, students: list | None = None) -> dict:
    """Minimal full_data.json-shaped payload with only students and schedule."""
    return {
        "students": students
        if students is not None
        else [
            {"id": "33572", "name": "Daphne", "grade_level": "8"},
            {"id": "33573", "name": "Ivy", "grade_level": "6"},
        ],
        "courses": [],
        "assignments": [],
        "schedule": schedule,
    }


def _entry(**overrides) -> dict:
    """A schedule entry shaped like scrape_full.parse_schedule output."""
    entry = {
        "expression": "2.8(A)",
        "term": "26-27",
        "course_section": "08037G0708-6",
        "course_name": "Physical Education, (Grades 7-8)",
        "teacher": "Miller, Melissa L",
        "room": "GYM",
        "enroll": "08/12/2026",
        "leave": "05/28/2027",
        "student_name": "Daphne",
        "student_id": "33572",
    }
    entry.update(overrides)
    return entry


def test_loads_schedule_entry(repo: Repository) -> None:
    """A scraped entry lands in the schedules table with dates normalized."""
    _load_scraped_data_inner(repo, _scraped([_entry()]))

    student = repo.get_student_by_name("Daphne")
    schedule = repo.get_schedule(student["id"])

    assert len(schedule) == 1
    assert schedule[0]["course_name"] == "Physical Education, (Grades 7-8)"
    assert schedule[0]["expression"] == "2.8(A)"
    assert schedule[0]["term"] == "26-27"
    assert schedule[0]["course_section"] == "08037G0708-6"
    # "teacher" from the scraper maps to teacher_name in the database.
    assert schedule[0]["teacher_name"] == "Miller, Melissa L"
    assert schedule[0]["room"] == "GYM"
    # MM/DD/YYYY from PowerSchool is normalized to ISO.
    assert schedule[0]["enroll_date"] == "2026-08-12"
    assert schedule[0]["leave_date"] == "2027-05-28"


def test_routes_entries_to_the_tagged_student(repo: Repository) -> None:
    """Multi-student syncs keep each student's schedule separate."""
    payload = _scraped(
        [
            _entry(course_name="PE 8", student_name="Daphne", student_id="33572"),
            _entry(
                course_name="Reading 6",
                expression="1.6(A)",
                course_section="01034G0606-3261",
                student_name="Ivy",
                student_id="33573",
            ),
        ]
    )
    _load_scraped_data_inner(repo, payload)

    daphne = repo.get_schedule(repo.get_student_by_name("Daphne")["id"])
    ivy = repo.get_schedule(repo.get_student_by_name("Ivy")["id"])

    assert [c["course_name"] for c in daphne] == ["PE 8"]
    assert [c["course_name"] for c in ivy] == ["Reading 6"]


def test_skips_entries_without_course_name(repo: Repository) -> None:
    """Blank rows scraped from the page are dropped rather than stored."""
    _load_scraped_data_inner(repo, _scraped([_entry(course_name=""), _entry()]))

    schedule = repo.get_schedule(repo.get_student_by_name("Daphne")["id"])
    assert len(schedule) == 1


def test_unparseable_dates_become_null(repo: Repository) -> None:
    """A date PowerSchool renders unexpectedly is stored as NULL, not garbage."""
    _load_scraped_data_inner(repo, _scraped([_entry(enroll="TBD", leave="")]))

    schedule = repo.get_schedule(repo.get_student_by_name("Daphne")["id"])
    assert schedule[0]["enroll_date"] is None
    assert schedule[0]["leave_date"] is None


def test_missing_term_does_not_create_a_bogus_term(repo: Repository) -> None:
    """An entry with no term must not become a term the CLI would default to."""
    _load_scraped_data_inner(repo, _scraped([_entry(term="")]))

    student_id = repo.get_student_by_name("Daphne")["id"]
    assert repo.get_schedule_terms(student_id) == []
    # The entry itself is still stored and reachable without a term filter.
    assert len(repo.get_schedule(student_id)) == 1


def test_reloading_same_data_does_not_duplicate(repo: Repository) -> None:
    """Loading twice into an existing database refreshes rows in place."""
    payload = _scraped([_entry(), _entry(course_name="Band", expression="5.8(A)", course_section="")])
    _load_scraped_data_inner(repo, payload)
    _load_scraped_data_inner(repo, payload)

    schedule = repo.get_schedule(repo.get_student_by_name("Daphne")["id"])
    assert len(schedule) == 2


def test_reload_reflects_upstream_changes(repo: Repository) -> None:
    """A room change upstream overwrites the stored value."""
    _load_scraped_data_inner(repo, _scraped([_entry(room="GYM")]))
    _load_scraped_data_inner(repo, _scraped([_entry(room="AUX GYM")]))

    schedule = repo.get_schedule(repo.get_student_by_name("Daphne")["id"])
    assert len(schedule) == 1
    assert schedule[0]["room"] == "AUX GYM"
