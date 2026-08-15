"""Unit tests for schedule parsing and schedule date normalization.

Covers the column-resolution logic in scripts/scrape_full.py and the date
normalization in scripts/load_data.py, both of which sit between the scraped
HTML and the schedules table.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.load_data import to_iso_date  # noqa: E402
from scripts.scrape_full import parse_schedule  # noqa: E402

pytestmark = pytest.mark.unit


def _schedule_html(headers: list[str], rows: list[list[str]]) -> str:
    """Build a minimal myschedule.html-shaped page."""
    header_html = "".join(f"<th>{h}</th>" for h in headers)
    row_html = "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows
    )
    return f"<html><body><table id='results'><tr>{header_html}</tr>{row_html}</table></body></html>"


STANDARD_HEADERS = ["Exp", "Trm", "Crs-Sec", "Course Name", "Teacher", "Room", "Enroll", "Leave"]
STANDARD_ROW = [
    "2.8(A)",
    "26-27",
    "08037G0708-6",
    "Physical Education, (Grades 7-8)",
    "Miller, Melissa L",
    "GYM",
    "08/12/2026",
    "05/28/2027",
]


class TestParseSchedule:
    """Column resolution and row filtering."""

    def test_parses_all_columns(self):
        courses = parse_schedule(_schedule_html(STANDARD_HEADERS, [STANDARD_ROW]))

        assert courses == [
            {
                "expression": "2.8(A)",
                "term": "26-27",
                "course_section": "08037G0708-6",
                "course_name": "Physical Education, (Grades 7-8)",
                "teacher": "Miller, Melissa L",
                "room": "GYM",
                "enroll": "08/12/2026",
                "leave": "05/28/2027",
            }
        ]

    def test_columns_resolved_by_header_not_position(self):
        """A column inserted upstream must not shift every field by one."""
        headers = ["Exp", "Trm", "Crs-Sec", "Course Name", "Credit", "Teacher", "Room", "Enroll", "Leave"]
        row = STANDARD_ROW[:4] + ["1.0"] + STANDARD_ROW[4:]

        courses = parse_schedule(_schedule_html(headers, [row]))

        assert courses[0]["teacher"] == "Miller, Melissa L"
        assert courses[0]["room"] == "GYM"
        assert courses[0]["enroll"] == "08/12/2026"
        assert courses[0]["leave"] == "05/28/2027"

    def test_missing_optional_columns_yield_empty_strings(self):
        """Older pages without Enroll/Leave still parse the remaining fields."""
        headers = STANDARD_HEADERS[:6]
        courses = parse_schedule(_schedule_html(headers, [STANDARD_ROW[:6]]))

        assert courses[0]["course_name"] == "Physical Education, (Grades 7-8)"
        assert courses[0]["enroll"] == ""
        assert courses[0]["leave"] == ""

    def test_falls_back_to_positions_without_headers(self):
        """A page with no recognizable header row uses the documented order."""
        html = _schedule_html([], [STANDARD_ROW])

        courses = parse_schedule(html)

        assert courses[0]["expression"] == "2.8(A)"
        assert courses[0]["enroll"] == "08/12/2026"

    def test_skips_rows_without_expression(self):
        rows = [STANDARD_ROW, [""] + STANDARD_ROW[1:]]

        courses = parse_schedule(_schedule_html(STANDARD_HEADERS, rows))

        assert len(courses) == 1

    def test_returns_empty_without_results_table(self):
        assert parse_schedule("<html><body><p>Nothing here</p></body></html>") == []


class TestParseScheduleFixture:
    """Parse the saved real schedule page, if present."""

    def test_parses_saved_schedule_page(self):
        fixture = Path(__file__).parent.parent.parent / "raw_html" / "schedule.html"
        if not fixture.exists():
            pytest.skip("raw_html/schedule.html fixture not present")

        courses = parse_schedule(fixture.read_text())

        assert len(courses) > 0
        for course in courses:
            assert course["course_name"]
            assert course["expression"]
            # Enroll/Leave are the columns most at risk from positional drift.
            assert course["enroll"].count("/") == 2, course
            assert course["leave"].count("/") == 2, course


class TestToIsoDate:
    """Date normalization applied before writing to the schedules table."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("08/12/2026", "2026-08-12"),
            ("05/28/2027", "2027-05-28"),
            ("2026-08-12", "2026-08-12"),
            ("", None),
            (None, None),
            ("not a date", None),
            ("13/45/2026", None),
        ],
    )
    def test_normalizes(self, value, expected):
        assert to_iso_date(value) == expected
