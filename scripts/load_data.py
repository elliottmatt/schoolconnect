#!/usr/bin/env python3
"""Load scraped data into the database."""

import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# Load .env file from current directory if available
load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.database.connection import DB_PATH, init_database, verify_database
from src.database.repository import Repository

# Number of database backups to keep (configurable via .env)
DB_BACKUP_COUNT = int(os.getenv("DB_BACKUP_COUNT", "5"))


def _get_backup_dir() -> Path:
    """Get the database backup directory."""
    backup_dir = DB_PATH.parent / "db_backups"
    backup_dir.mkdir(exist_ok=True)
    return backup_dir


def _rotate_backups():
    """Delete oldest backups to keep only DB_BACKUP_COUNT copies."""
    backup_dir = _get_backup_dir()
    backups = sorted(backup_dir.glob("powerschool_*.db"))
    while len(backups) > DB_BACKUP_COUNT:
        oldest = backups.pop(0)
        oldest.unlink()
        print(f"  Removed old backup: {oldest.name}")


def _backup_database():
    """Copy the current database with a timestamp, then rotate old backups."""
    if not DB_PATH.exists():
        return
    backup_dir = _get_backup_dir()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"powerschool_{timestamp}.db"
    shutil.copy2(DB_PATH, backup_path)
    print(f"  Backed up database to {backup_path.name}")
    _rotate_backups()


def load_scraped_data():
    """Load data from full_data.json into the database."""
    # Backup current database before overwriting
    _backup_database()

    # Initialize database
    print("Initializing database...")
    init_database(force=True)

    # Verify
    info = verify_database()
    print(f"Tables: {info.get('tables', [])}")
    print(f"Views: {info.get('views', [])}")

    # Load scraped data
    data_file = Path(__file__).parent.parent / "raw_html" / "full_data.json"
    if not data_file.exists():
        print(f"ERROR: Data file not found: {data_file}")
        print("Run scripts/scrape_full.py first")
        sys.exit(1)

    with open(data_file) as f:
        data = json.load(f)

    repo = Repository()
    scrape_id = repo.start_scrape()

    try:
        _load_scraped_data_inner(repo, data)
        assignments = data.get("assignments", [])
        assignments_found = len(assignments)
        repo.complete_scrape(scrape_id, status="completed", assignments_found=assignments_found)
    except Exception as e:
        repo.complete_scrape(scrape_id, status="failed", error_message=str(e))
        raise

    print("\n=== LOAD COMPLETE ===")


def _load_scraped_data_inner(repo: Repository, data: dict):
    """Inner loader — called within a scrape_history transaction."""
    # Insert students
    print("\n=== LOADING STUDENTS ===")
    student_ids = {}
    for student in data.get("students", []):
        student_id = repo.upsert_student(
            powerschool_id=student["id"],
            first_name=student["name"],
            grade_level=student.get("grade_level"),
            school_name=student.get("school_name"),
        )
        student_ids[student["name"]] = student_id
        print(f"  Added student: {student['name']} (DB ID: {student_id})")

    # Helper to resolve student DB ID from scraped student name
    def get_student_db_id(item):
        """Get database student ID from a scraped item's student_name/student_id."""
        name = item.get("student_name", "")
        if name and name in student_ids:
            return student_ids[name]
        # Fall back to current_student
        current = data.get("current_student") or {}
        fallback_name = current.get("name", "")
        return student_ids.get(fallback_name, 1)

    # Insert courses and grades
    print("\n=== LOADING COURSES & GRADES ===")
    # Track course_ids per student to handle multi-student
    course_ids = {}  # (student_db_id, course_name) -> course_id
    seen_courses = set()

    for course in data.get("courses", []):
        sid = get_student_db_id(course)
        course_key = (sid, course["course_name"], course.get("expression", ""))

        # Skip duplicates
        if course_key in seen_courses:
            continue
        seen_courses.add(course_key)

        student_name = course.get("student_name", "?")
        course_id = repo.upsert_course(
            student_id=sid,
            course_name=course["course_name"],
            expression=course.get("expression"),
            room=course.get("room"),
            teacher_name=course.get("teacher_name"),
            term="S1",
        )
        course_ids[(sid, course["course_name"])] = course_id
        print(f"  [{student_name}] {course['course_name']} (ID: {course_id})")

        # Add grades — prefer F1/F2 (semester) over Q1-Q4 (quarter) when available
        absences = int(course.get("absences", 0)) if str(course.get("absences", "")).isdigit() else 0
        tardies = int(course.get("tardies", 0)) if str(course.get("tardies", "")).isdigit() else 0

        f1 = course.get("f1", "")
        f2 = course.get("f2", "")
        has_semester = bool(f1 or f2)

        if has_semester:
            terms = [("F1", f1), ("F2", f2)]
        else:
            terms = [
                ("Q1", course.get("q1", "")),
                ("Q2", course.get("q2", "")),
                ("Q3", course.get("q3", "")),
                ("Q4", course.get("q4", "")),
            ]

        for term_name, grade in terms:
            if not grade or grade in ["Not available", "[ i ]", "-"]:
                continue
            repo.add_grade(
                course_id=course_id,
                student_id=sid,
                term=term_name,
                letter_grade=grade,
                absences=absences,
                tardies=tardies,
            )
            print(f"    Grade {term_name}: {grade}")

    # Insert schedules
    print("\n=== LOADING SCHEDULES ===")
    schedule_entries = data.get("schedule", [])
    schedule_count = 0
    for entry in schedule_entries:
        sid = get_student_db_id(entry)
        course_name = entry.get("course_name")
        if not course_name:
            continue

        # Normalize enroll/leave dates to ISO format if present
        def _to_iso(value):
            if not value:
                return None
            for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
                try:
                    return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
                except ValueError:
                    continue
            return None

        repo.upsert_schedule(
            student_id=sid,
            course_name=course_name,
            expression=entry.get("expression"),
            term=entry.get("term") or "YR",
            course_section=entry.get("course_section"),
            teacher_name=entry.get("teacher") or entry.get("teacher_name"),
            room=entry.get("room"),
            enroll_date=_to_iso(entry.get("enroll_date") or entry.get("enroll")),
            leave_date=_to_iso(entry.get("leave_date") or entry.get("leave")),
        )
        schedule_count += 1

    print(f"Loaded {schedule_count} schedule entries")

    # Insert assignments
    print("\n=== LOADING ASSIGNMENTS ===")
    assignments = data.get("assignments", [])
    missing_count = 0
    for assignment in assignments:
        # Skip empty/invalid assignments
        name = (
            assignment.get("assignment_name")
            or assignment.get("assignment")
            or assignment.get("name")
        )
        if not name or len(name) < 2:
            continue

        sid = get_student_db_id(assignment)
        course_name = assignment.get("course", "Unknown")
        status = assignment.get("status", "Unknown")

        # Parse due date
        due_date = assignment.get("due_date")
        if due_date:
            try:
                dt = datetime.strptime(due_date, "%m/%d/%Y")
                due_date = dt.strftime("%Y-%m-%d")
            except ValueError:
                due_date = None

        repo.add_assignment(
            student_id=sid,
            course_name=course_name,
            assignment_name=name,
            teacher_name=assignment.get("teacher"),
            category=assignment.get("category"),
            due_date=due_date,
            score=assignment.get("score"),
            percent=float(assignment.get("percent", 0))
            if assignment.get("percent", "").replace(".", "").isdigit()
            else None,
            letter_grade=assignment.get("letter_grade"),
            status=status,
            codes=assignment.get("codes"),
            term=assignment.get("term"),
        )

        if status == "Missing":
            missing_count += 1

    print(f"\nLoaded {len(assignments)} assignments, {missing_count} missing")

    # Insert attendance summary
    print("\n=== LOADING ATTENDANCE ===")
    # Prefer per-student attendance list; fall back to legacy single-student key
    attendance_list = data.get("attendance_by_student") or []
    if not attendance_list and data.get("attendance", {}).get("rate"):
        # Legacy single-student format
        current_student = data.get("current_student") or {}
        att = data["attendance"]
        att["student_name"] = current_student.get("name", "")
        attendance_list = [att]

    loaded_student_ids = set()
    for attendance in attendance_list:
        att_student_name = attendance.get("student_name", "")
        att_student_id = student_ids.get(att_student_name)
        if att_student_id is None:
            print(f"  [WARN] Unknown student in attendance: {att_student_name!r}")
            continue
        if attendance.get("rate"):
            repo.add_attendance_summary(
                student_id=att_student_id,
                attendance_rate=attendance.get("rate", 0),
                days_present=attendance.get("days_present", 0),
                days_absent=attendance.get("days_absent", 0),
                tardies=attendance.get("tardies", 0),
                total_days=attendance.get("total_days", 0),
            )
            print(f"  [{att_student_name}] Attendance rate: {attendance.get('rate')}%")
            loaded_student_ids.add(att_student_id)

    # For any student still missing attendance, estimate from course grades data
    for student_name, sid in student_ids.items():
        if sid in loaded_student_ids:
            continue
        student_courses = [c for c in data.get("courses", []) if c.get("student_name") == student_name]
        total_absences = sum(
            int(c.get("absences", 0)) if str(c.get("absences", "")).isdigit() else 0
            for c in student_courses
        )
        total_tardies = sum(
            int(c.get("tardies", 0)) if str(c.get("tardies", "")).isdigit() else 0
            for c in student_courses
        )
        estimated_days = 80
        num_courses = max(len(student_courses), 1)
        estimated_rate = ((estimated_days - (total_absences / num_courses)) / estimated_days) * 100
        repo.add_attendance_summary(
            student_id=sid,
            attendance_rate=round(estimated_rate, 1),
            days_absent=int(total_absences / num_courses),
            tardies=int(total_tardies / num_courses),
            total_days=estimated_days,
        )
        print(f"  [{student_name}] Estimated attendance from course data")

    # Extract and insert teachers from course data
    print("\n=== LOADING TEACHERS ===")

    # First, try to extract emails from the HTML file if they're not in the JSON
    from bs4 import BeautifulSoup

    teacher_emails = {}
    home_html = Path(__file__).parent.parent / "raw_html" / "home.html"
    if home_html.exists():
        soup = BeautifulSoup(home_html.read_text(), "lxml")
        for link in soup.select("a[href^='mailto:']"):
            email = link.get("href", "").replace("mailto:", "")
            parent_td = link.find_parent("td")
            if parent_td:
                parent_text = parent_td.get_text(strip=True)
                if "Email" in parent_text:
                    parts = parent_text.split("Email")
                    if len(parts) > 1:
                        teacher_info = parts[1].strip()
                        name_parts = teacher_info.split("-")
                        teacher_name = name_parts[0].strip()
                        if teacher_name and email:
                            teacher_emails[teacher_name] = email

    teacher_data = {}
    for course in data.get("courses", []):
        teacher_name = course.get("teacher_name")
        teacher_email = course.get("teacher_email")

        # Try to get email from HTML extraction if not in JSON
        if not teacher_email and teacher_name:
            teacher_email = teacher_emails.get(teacher_name)

        if teacher_name:
            if teacher_name not in teacher_data:
                teacher_data[teacher_name] = {
                    "name": teacher_name,
                    "email": teacher_email,
                    "room": course.get("room"),
                    "courses": [],
                }
            teacher_data[teacher_name]["courses"].append(course["course_name"])
            # Update email if we didn't have it
            if teacher_email and not teacher_data[teacher_name]["email"]:
                teacher_data[teacher_name]["email"] = teacher_email

    for name, info in teacher_data.items():
        courses_json = json.dumps(list(set(info["courses"])))
        repo.upsert_teacher(
            name=info["name"],
            email=info["email"],
            room=info["room"],
            courses_taught=courses_json,
        )
        print(f"  Added teacher: {info['name']} ({info.get('email', 'no email')})")

    print(f"\nLoaded {len(teacher_data)} teachers")

    # Final verification
    print("\n=== VERIFICATION ===")
    info = verify_database()
    print(f"Row counts: {info.get('row_counts', {})}")

    # Summary for each student
    print("\n=== SUMMARY ===")
    students = repo.get_students()
    for student in students:
        sid = student["id"]
        summary = repo.get_student_summary(sid)
        print(f"\n--- {student['first_name']} ---")
        if summary:
            print(f"  Courses: {summary['course_count']}")
            print(f"  Missing: {summary['missing_assignments']}")

        missing = repo.get_missing_assignments(sid)
        if missing:
            print("  Missing assignments:")
            for m in missing:
                due = m.get("due_date", "")
                due_str = f" [due: {due}]" if due else ""
                print(f"    - {m['assignment_name']} ({m['course_name']}){due_str}")

        actions = repo.get_action_items(sid)
        if actions:
            print(f"  Action items: {len(actions)}")
            for a in actions[:5]:
                date = a.get("relevant_date", "")
                date_str = f" [{date}]" if date else ""
                print(f"    [{a['priority']}] {a['message']}{date_str}")


if __name__ == "__main__":
    load_scraped_data()
