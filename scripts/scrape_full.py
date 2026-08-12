#!/usr/bin/env python3
"""
Full PowerSchool scraper that extracts all data including course-level assignments.
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# Load .env file from current directory if available
load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent))

from bs4 import BeautifulSoup
from playwright.sync_api import Page, sync_playwright

from src.scraper.auth import get_base_url, get_credentials, login

BASE_URL = get_base_url()
RAW_HTML_DIR = Path(__file__).parent.parent / "raw_html"
RAW_HTML_DIR.mkdir(exist_ok=True)
SCRAPER_DEBUG = os.getenv("SCRAPER_DEBUG", "").lower() in ("1", "true", "yes")

_debug_counter = 0


def _dump_html(page: Page, label: str):
    """Dump current page HTML and screenshot to debug dir for troubleshooting.

    Enable by setting SCRAPER_DEBUG=1 in .env or environment.
    """
    if not SCRAPER_DEBUG:
        return
    global _debug_counter
    _debug_counter += 1
    debug_dir = RAW_HTML_DIR / "debug"
    debug_dir.mkdir(exist_ok=True)
    filename = f"{_debug_counter:02d}_{label}.html"
    filepath = debug_dir / filename
    html = page.content()
    filepath.write_text(html)
    # Also take a screenshot
    screenshot_path = debug_dir / f"{_debug_counter:02d}_{label}.png"
    try:
        page.screenshot(path=str(screenshot_path))
    except Exception:
        pass
    print(f"  [debug] Saved {filepath} ({len(html)} bytes)")


def get_students(page: Page) -> list:
    """Get list of students from the page."""
    # Wait for the student switcher to appear (it loads dynamically)
    try:
        page.wait_for_selector("#students-list", timeout=5000)
        if SCRAPER_DEBUG:
            print("  [debug] #students-list found in DOM")
    except Exception:
        if SCRAPER_DEBUG:
            print("  [debug] #students-list NOT found after 5s")

    _dump_html(page, "get_students")

    html = page.content()
    soup = BeautifulSoup(html, "lxml")

    students = []
    students_list = soup.select_one("#students-list")
    if SCRAPER_DEBUG:
        print(f"  [debug] #students-list element: {'found' if students_list else 'missing'}")
    if students_list:
        for a in students_list.select("a"):
            bold = a.find("b")
            text = bold.get_text(strip=True) if bold else a.get_text(strip=True)
            # Extract student number from "Student Number: XXXXXXXXXX"
            full_text = a.get_text(strip=True)
            sn_match = re.search(r"Student Number:\s*(\d+)", full_text)
            student_number = sn_match.group(1) if sn_match else ""
            href = a.get("href", "")
            # Extract student ID from javascript:switchStudent(12345)
            match = re.search(r"switchStudent\((\d+)\)", href)
            if match:
                student_id = match.group(1)
                is_selected = "selected" in a.find_parent("li").get("class", [])
                students.append({
                    "name": text,
                    "id": student_id,
                    "student_number": student_number,
                    "selected": is_selected,
                })
    return students


def _ensure_logged_in(page: Page):
    """Check if the session is still active, re-login if needed."""
    if "Sign In" in page.title() or "/public/" in page.url:
        print("  Session expired, re-logging in...")
        if not login(page):
            raise RuntimeError("Re-login failed after session expiry")


def switch_student(page: Page, student_id: str):
    """Switch to a different student."""
    print(f"Switching to student ID: {student_id}")
    # Navigate to home page to ensure switchStudent() JS is available
    page.goto(f"{BASE_URL}/guardian/home.html", wait_until="networkidle")
    _ensure_logged_in(page)
    try:
        page.wait_for_selector("table.linkDescList", timeout=10000)
    except Exception:
        page.wait_for_timeout(5000)
    # Wait for the switchStudent function to be defined
    try:
        page.wait_for_function("typeof switchStudent === 'function'", timeout=5000)
    except Exception:
        if SCRAPER_DEBUG:
            print("  [debug] switchStudent function not found, retrying page load")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(3000)
    _dump_html(page, f"before_switch_{student_id}")
    page.evaluate(f"switchStudent({student_id})")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)
    _dump_html(page, f"after_switch_{student_id}")


def scrape_home_grades(page: Page) -> dict:
    """Scrape grades from home page."""
    print("Scraping home page grades...")
    _dump_html(page, "before_goto_home")
    if SCRAPER_DEBUG:
        print(f"  [debug] Current URL: {page.url}")
    page.goto(f"{BASE_URL}/guardian/home.html", wait_until="networkidle")
    _ensure_logged_in(page)
    if SCRAPER_DEBUG:
        print(f"  [debug] After goto URL: {page.url}")
    _dump_html(page, "after_goto_home_networkidle")
    # Wait for the grades table to render (JS-loaded content)
    try:
        page.wait_for_selector("table.linkDescList", timeout=10000)
        if SCRAPER_DEBUG:
            print("  [debug] Grades table found")
    except Exception:
        if SCRAPER_DEBUG:
            print("  [debug] Grades table NOT found after 10s, falling back to 5s wait")
        page.wait_for_timeout(5000)

    _dump_html(page, "after_wait_for_content")

    html = page.content()
    soup = BeautifulSoup(html, "lxml")

    # Get school name from print-school div
    school_div = soup.select_one("#print-school")
    school_name = ""
    if school_div:
        school_span = school_div.select_one("span")
        school_name = school_span.get_text(strip=True) if school_span else school_div.get_text(strip=True)

    # Get students
    students = get_students(page)
    print(f"  Found {len(students)} student(s) in switcher")
    current_student = next((s for s in students if s.get("selected")), None)

    # Add school name to each student
    for s in students:
        s["school_name"] = school_name

    data = {
        "students": students,
        "current_student": current_student,
        "courses": [],
        "course_links": [],
    }

    # Parse grades table
    tables = soup.select("table.linkDescList.grid")
    if tables:
        table = tables[0]
        all_rows = table.select("tr")

        # Build column index map from first header row
        col_map = {}
        col_idx = 0
        if all_rows:
            for th in all_rows[0].select("th"):
                name = th.get_text(strip=True)
                colspan = int(th.get("colspan", 1))
                col_map[name] = col_idx
                col_idx += colspan
        if SCRAPER_DEBUG:
            print(f"  [debug] Grade table columns: {col_map}")

        def get_col(name):
            return col_map.get(name)

        # Skip header rows
        data_rows = all_rows[2:]

        for row in data_rows:
            cells = row.select("td")
            if len(cells) < 12:
                continue

            expression = cells[0].get_text(strip=True)

            course_idx = get_col("Course")
            if course_idx is None:
                continue
            course_cell = cells[course_idx]
            course_text = course_cell.get_text(strip=True)

            # Parse course name, teacher, and email
            teacher_email = None
            email_link = course_cell.select_one("a[href^='mailto:']")
            if email_link:
                teacher_email = email_link.get("href", "").replace("mailto:", "")

            if "Email" in course_text:
                course_name = course_text.split("Email")[0].strip()
                teacher_info = course_text.split("Email")[1].strip()
                teacher_parts = teacher_info.split("-")
                teacher_name = teacher_parts[0].strip() if teacher_parts else ""
                room = ""
                if len(teacher_parts) > 1:
                    room_match = re.search(r"Rm:(\S+)", teacher_parts[-1])
                    if room_match:
                        room = room_match.group(1)
            else:
                course_name = course_text
                teacher_name = ""
                room = ""

            # Extract grades using column map
            def clean_grade(col_name):
                idx = get_col(col_name)
                if idx is not None and idx < len(cells):
                    grade = cells[idx].get_text(strip=True)
                    if grade in ["[ i ]", "Not available", "-", ""]:
                        return ""
                    return grade
                return ""

            q1 = clean_grade("Q1")
            q2 = clean_grade("Q2")
            f1 = clean_grade("F1")
            q3 = clean_grade("Q3")
            q4 = clean_grade("Q4")
            f2 = clean_grade("F2")

            absences = clean_grade("Absences") or "0"
            tardies = clean_grade("Tardies") or "0"

            # Collect ALL term links for this course (one per grade column)
            course_term_links = []
            for col_name in ["Q1", "Q2", "F1", "Q3", "Q4", "F2"]:
                idx = get_col(col_name)
                if idx is not None and idx < len(cells):
                    link = cells[idx].select_one("a")
                    if link:
                        href = link.get("href", "")
                        if href:
                            course_term_links.append({"course_name": course_name, "term": col_name, "link": href})

            course_data = {
                "expression": expression,
                "course_name": course_name,
                "teacher_name": teacher_name,
                "teacher_email": teacher_email,
                "room": room,
                "q1": q1,
                "q2": q2,
                "f1": f1,
                "q3": q3,
                "q4": q4,
                "f2": f2,
                "absences": absences,
                "tardies": tardies,
            }
            data["courses"].append(course_data)
            data["course_links"].extend(course_term_links)

    # Infer grade level from course names (e.g., "Gr 7", "Gr 8")
    grade_level = None
    for course in data["courses"]:
        grade_match = re.search(r"Gr\s*(\d+)", course["course_name"])
        if grade_match:
            grade_level = grade_match.group(1)
            break
    if grade_level:
        for s in data["students"]:
            s["grade_level"] = grade_level
        if data["current_student"]:
            data["current_student"]["grade_level"] = grade_level

    # Extract student-level attendance totals from "Attendance Totals" row
    # This row is in the same linkDescList table and shows total absences/tardies
    attendance = {"days_absent": 0, "tardies": 0}
    for table in tables:
        for row in table.select("tr"):
            cells = row.select("td")
            if cells and cells[0].get_text(strip=True) == "Attendance Totals":
                if len(cells) >= 2:
                    try:
                        attendance["days_absent"] = int(cells[1].get_text(strip=True) or 0)
                    except ValueError:
                        pass
                if len(cells) >= 3:
                    try:
                        attendance["tardies"] = int(cells[2].get_text(strip=True) or 0)
                    except ValueError:
                        pass
                print(f"  Attendance totals: {attendance['days_absent']} absences, {attendance['tardies']} tardies")
                break
    data["attendance"] = attendance

    (RAW_HTML_DIR / "home.html").write_text(html)
    return data


def scrape_course_assignments(page: Page, course_link: str, course_name: str, term: str = "") -> list:
    """Scrape assignments from a specific course term page using #scoreTable."""
    print(f"  Scraping {course_name} [{term}]...")
    url = f"{BASE_URL}/guardian/{course_link}"
    page.goto(url, wait_until="networkidle")
    _ensure_logged_in(page)
    page.wait_for_timeout(1500)
    _dump_html(page, f"course_{course_name[:15].replace(' ', '_')}_{term}")

    html = page.content()
    soup = BeautifulSoup(html, "lxml")

    assignments = []

    # #scoreTable columns (14 cells):
    # 0=Due Date, 1=Category, 2=Assignment, 3=collected, 4=late, 5=missing,
    # 6=exempt, 7=absent, 8=incomplete, 9=excluded, 10=Score, 11=%, 12=Grade, 13=Comments
    score_table = soup.select_one("#scoreTable")
    if not score_table:
        if SCRAPER_DEBUG:
            all_tables = soup.select("table")
            print(f"  [debug] No #scoreTable for {course_name}. Tables: {[t.get('id', str(t.get('class','?'))) for t in all_tables[:5]]}")
        return assignments

    rows = score_table.select("tr")
    if SCRAPER_DEBUG:
        print(f"  [debug] #scoreTable has {len(rows)} rows for {course_name} [{term}]")

    for row in rows[1:]:  # Skip header row
        cells = row.select("td")
        if len(cells) < 13:
            continue

        due_date = cells[0].get_text(strip=True)
        category = cells[1].get_text(strip=True)
        assignment_name = cells[2].get_text(strip=True)
        # cells 3-9 are Angular-rendered flag columns (not readable from static HTML)
        score = cells[10].get_text(strip=True)
        percent = cells[11].get_text(strip=True).rstrip("%")
        letter_grade = cells[12].get_text(strip=True)

        if not assignment_name:
            continue

        # Score of "--/XX" or just "--" means not submitted (missing)
        status = "Missing" if score.startswith("--") else "Submitted"

        assignments.append({
            "course": course_name,
            "term": term,
            "due_date": due_date,
            "category": category,
            "assignment_name": assignment_name,
            "score": score,
            "percent": percent,
            "letter_grade": letter_grade,
            "status": status,
        })

    print(f"    {len(assignments)} assignments found")
    return assignments


def scrape_attendance_history(page: Page, home_attendance: dict) -> dict:
    """Scrape attendance from /guardian/attendance.html.

    Uses absence/tardy totals already extracted from the home page, and
    navigates to attendance.html to count total days for a rate calculation.
    Falls back gracefully if the page is unavailable.
    """
    print("Scraping attendance history...")
    data = dict(home_attendance)  # Start with totals already from home page

    page.goto(f"{BASE_URL}/guardian/attendance.html", wait_until="networkidle")
    _ensure_logged_in(page)
    page.wait_for_timeout(1500)

    html = page.content()
    soup = BeautifulSoup(html, "lxml")
    (RAW_HTML_DIR / "attendance.html").write_text(html)

    if len(html) < 500:
        print(f"  attendance.html unavailable ({len(html)} bytes), using home page totals")
    else:
        # Count total attendance records to estimate total days
        # The page has rows for each class period on each day
        rows = soup.select("table tr")
        record_count = sum(1 for r in rows if r.select("td"))
        if SCRAPER_DEBUG:
            print(f"  [debug] attendance.html: {len(html)} bytes, {record_count} rows")

    # Compute rate from absences if we have totals
    days_absent = data.get("days_absent", 0)
    tardies = data.get("tardies", 0)
    if days_absent > 0 or tardies > 0:
        # Estimate total school days based on current date in the school year
        # School year ~175 days; rough estimate based on April = ~130 days in
        total_days = data.get("total_days") or 130
        days_present = total_days - days_absent
        rate = round((days_present / total_days) * 100, 1) if total_days > 0 else 0.0
        data.update({
            "days_present": days_present,
            "total_days": total_days,
            "rate": rate,
        })

    print(f"  Absences: {data.get('days_absent', 0)}, Tardies: {data.get('tardies', 0)}, Rate: {data.get('rate', 0)}%")
    return data


def scrape_schedule(page: Page) -> list:
    """Scrape schedule page."""
    print("Scraping schedule...")
    page.goto(f"{BASE_URL}/guardian/myschedule.html", wait_until="networkidle")
    _ensure_logged_in(page)
    page.wait_for_timeout(2000)

    html = page.content()
    soup = BeautifulSoup(html, "lxml")

    courses = []
    table = soup.select_one("#results")

    if table:
        rows = table.select("tbody tr, tr")
        for row in rows:
            cells = row.select("td")
            if len(cells) >= 6:
                expression = cells[0].get_text(strip=True)
                if expression and not expression.startswith("Exp"):
                    course = {
                        "expression": expression,
                        "term": cells[1].get_text(strip=True) if len(cells) > 1 else "",
                        "course_section": cells[2].get_text(strip=True) if len(cells) > 2 else "",
                        "course_name": cells[3].get_text(strip=True) if len(cells) > 3 else "",
                        "teacher": cells[4].get_text(strip=True) if len(cells) > 4 else "",
                        "room": cells[5].get_text(strip=True) if len(cells) > 5 else "",
                        # Enroll/Leave columns (e.g., "08/12/2026" / "05/28/2027")
                        "enroll": cells[6].get_text(strip=True) if len(cells) > 6 else "",
                        "leave": cells[7].get_text(strip=True) if len(cells) > 7 else "",
                    }
                    courses.append(course)

    (RAW_HTML_DIR / "schedule.html").write_text(html)
    return courses


def _scrape_current_student(page, all_data: dict):
    """Scrape all data for the currently selected student and merge into all_data."""
    home_data = scrape_home_grades(page)
    current_student = home_data.get("current_student") or {}
    student_name_display = current_student.get("name", "Unknown")
    print(f"\nScraping data for: {student_name_display}")

    all_data.setdefault("students", [])
    all_data.setdefault("courses", [])
    all_data.setdefault("assignments", [])
    all_data.setdefault("schedule", [])
    all_data.setdefault("attendance_by_student", [])

    # Merge student list (only once, they're the same across switches)
    if not all_data["students"]:
        all_data["students"] = home_data["students"]

    all_data["current_student"] = current_student

    # Tag courses with the student they belong to
    student_id = current_student.get("id", "")
    for c in home_data["courses"]:
        c["student_name"] = student_name_display
        c["student_id"] = student_id
    all_data["courses"].extend(home_data["courses"])

    print(f"\nFound {len(home_data['courses'])} courses")
    for c in home_data["courses"]:
        # Prefer F1/F2 (semester grades) over quarterly if available
        if c.get("f1") or c.get("f2"):
            grades = f"F1={c.get('f1', '')}, F2={c.get('f2', '')}"
        else:
            grades = f"Q1={c['q1']}, Q2={c['q2']}, Q3={c.get('q3', '')}, Q4={c.get('q4', '')}"
        print(f"  {c['course_name']}: {grades}")

    # Scrape individual course assignments using #scoreTable
    # For each course, prefer F1/F2 term links (semester grades) over Q1-Q4 (quarter grades).
    # F1 covers the same assignments as Q1+Q2 combined, so scraping both would duplicate.
    print("\n" + "=" * 40)
    print("Scraping course assignments...")
    student_assignments = []

    # Group term links by course
    from collections import defaultdict
    course_term_links: dict[str, list] = defaultdict(list)
    for link_info in home_data["course_links"]:
        course_term_links[link_info["course_name"]].append(link_info)

    for course_name, term_links in course_term_links.items():
        available_terms = {li["term"] for li in term_links}
        semester_links = [li for li in term_links if li["term"] in ("F1", "F2")]
        quarter_links = [li for li in term_links if li["term"] in ("Q1", "Q2", "Q3", "Q4")]

        # Prefer semester links; fall back to quarter links
        links_to_scrape = semester_links if semester_links else quarter_links
        if SCRAPER_DEBUG:
            print(f"  [debug] {course_name}: available terms={available_terms}, scraping={[li['term'] for li in links_to_scrape]}")

        for link_info in links_to_scrape:
            try:
                assignments = scrape_course_assignments(
                    page, link_info["link"], course_name, link_info["term"]
                )
                student_assignments.extend(assignments)
            except Exception as e:
                print(f"  Error scraping {course_name} [{link_info['term']}]: {e}")

    # Tag all assignments with the student
    for a in student_assignments:
        a["student_name"] = student_name_display
        a["student_id"] = student_id

    all_data["assignments"].extend(student_assignments)
    print(f"\nAssignments collected for {student_name_display}: {len(student_assignments)}")

    missing = [a for a in student_assignments if a.get("status") == "Missing"]
    print(f"Missing assignments: {len(missing)}")
    for a in missing:
        due = a.get("due_date", "")
        due_str = f" [due: {due}]" if due else ""
        print(
            f"  - {a.get('assignment_name', a.get('assignment', 'Unknown'))} ({a.get('course', '')}){due_str}"
        )

    # Scrape schedule
    print("\n" + "=" * 40)
    schedule = scrape_schedule(page)
    if isinstance(schedule, list):
        # Tag each entry with the student it belongs to (required for multi-student syncs)
        for entry in schedule:
            entry["student_name"] = student_name_display
            entry["student_id"] = student_id
        all_data["schedule"].extend(schedule)
    else:
        all_data["schedule"] = schedule

    # Scrape attendance — totals come from the home page, history page adds context
    print("\n" + "=" * 40)
    home_attendance = home_data.get("attendance", {})
    attendance = scrape_attendance_history(page, home_attendance)
    attendance["student_name"] = student_name_display
    all_data["attendance_by_student"].append(attendance)

    return home_data


def run_full_scrape(headless: bool = False, student_name: str | None = None, all_students: bool = False):
    """Run full scraping operation.

    Args:
        headless: Run browser in headless mode (no visible window)
        student_name: Optional student name to filter scraping to
        all_students: If True, iterate through all students on the account
    """
    print("=" * 60)
    print("PowerSchool Full Scrape")
    print(f"Target: {BASE_URL}")
    print(f"Time: {datetime.now().isoformat()}")
    print("=" * 60)

    try:
        get_credentials()  # Validate credentials are available
    except ValueError as e:
        print(f"ERROR: Missing credentials - {e}")
        sys.exit(1)

    all_data = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, slow_mo=200)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()

        if not login(page):
            browser.close()
            sys.exit(1)

        if SCRAPER_DEBUG:
            print(f"  [debug] Post-login URL: {page.url}")
        _dump_html(page, "post_login")

        if all_students:
            # Scrape default student (also discovers the student list)
            home_data = _scrape_current_student(page, all_data)
            students = home_data.get("students", [])
            current_id = (home_data.get("current_student") or {}).get("id")

            print(f"\nFound {len(students)} students on account")
            for s in students:
                print(f"  - {s['name']} (ID: {s['id']})")

            # Switch to and scrape each other student
            for student in students:
                if student["id"] != current_id:
                    print(f"\n{'=' * 60}")
                    print(f"Switching to student: {student['name']}")
                    switch_student(page, student["id"])
                    _scrape_current_student(page, all_data)
        elif student_name:
            # Scrape home to find student list, then switch
            home_data = scrape_home_grades(page)
            students = home_data.get("students", [])
            matching_student = None
            for student in students:
                if student_name.lower() in student.get("name", "").lower():
                    matching_student = student
                    break

            if matching_student:
                print(f"Switching to student: {matching_student['name']}")
                switch_student(page, matching_student["id"])
            else:
                print(f"WARNING: Student '{student_name}' not found. Available students:")
                for student in students:
                    print(f"  - {student['name']}")
                print("Continuing with default student...")
            _scrape_current_student(page, all_data)
        else:
            _scrape_current_student(page, all_data)

        browser.close()

    # Save all data
    data_file = RAW_HTML_DIR / "full_data.json"
    with open(data_file, "w") as f:
        json.dump(all_data, f, indent=2)
    print(f"\nSaved all data to {data_file}")

    # Summary
    print("\n" + "=" * 60)
    print("SCRAPE SUMMARY")
    print("=" * 60)
    print(f"Student: {all_data.get('current_student', {}).get('name', 'Unknown')}")
    print(f"Courses: {len(all_data.get('courses', []))}")
    print(f"Assignments: {len(all_data.get('assignments', []))}")
    print(
        f"Missing: {len([a for a in all_data.get('assignments', []) if a.get('status') == 'Missing'])}"
    )
    for att in all_data.get("attendance_by_student", []):
        print(f"Attendance [{att.get('student_name', '?')}]: {att.get('rate', 0)}%")

    return all_data


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Scrape PowerSchool parent portal")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument("--student", "-s", type=str, help="Scrape specific student only")
    args = parser.parse_args()

    run_full_scrape(headless=args.headless, student_name=args.student)
