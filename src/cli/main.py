#!/usr/bin/env python3
"""CLI for PowerSchool Parent Portal.

Commands:
    init-db     Create or reset the database
    sync        Sync data from PowerSchool (runs scraper)
    students    List students on the account
    missing     Show missing assignments
    grades      Show current grades
    report      Generate weekly report
    serve-mcp   Start MCP server for AI agents
    status      Show database status and student overview
"""

import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env file from current directory if available
load_dotenv()

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import click  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.markdown import Markdown  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.table import Table  # noqa: E402

from src.database.connection import init_database, verify_database  # noqa: E402
from src.database.repository import Repository  # noqa: E402

console = Console()


@click.group()
@click.version_option(version="0.1.0", prog_name="powerschool")
def cli():
    """PowerSchool Parent Portal CLI - Manage and query student academic data."""
    pass


@cli.command()
@click.option("--force", is_flag=True, help="Force reset existing database")
def init_db(force: bool):
    """Initialize or reset the database."""
    if not force:
        db_path = Path("powerschool.db")
        if db_path.exists():
            if not click.confirm("Database exists. Reset it?", default=False):
                console.print("[yellow]Aborted.[/yellow]")
                return

    console.print("[blue]Initializing database...[/blue]")
    init_database(force=force)
    info = verify_database()

    console.print("[green]✓ Database initialized[/green]")
    console.print(f"  Tables: {', '.join(info.get('tables', []))}")
    console.print(f"  Views: {len(info.get('views', []))} views created")


@cli.command()
@click.option("--headless", is_flag=True, help="Run browser in headless mode")
@click.option("--student", "-s", help="Sync specific student only")
@click.option(
    "--all-students",
    is_flag=True,
    help="Sync all students on the account (iterates through student switcher)",
)
def sync(headless: bool, student: str, all_students: bool):
    """Sync data from PowerSchool (runs scraper).

    By default, syncs only the currently selected student. Use --all-students
    to iterate through all students on the parent account.
    """
    try:
        # Import scraper (will fail if not installed)
        from scripts.load_data import load_scraped_data
        from scripts.scrape_full import run_full_scrape

        console.print("[blue]Starting PowerSchool sync...[/blue]")
        console.print("This will open a browser and log into PowerSchool.")

        if all_students:
            console.print("[cyan]Mode: Syncing ALL students on account[/cyan]")
        elif student:
            console.print(f"[cyan]Mode: Syncing specific student: {student}[/cyan]")

        # Run scraper
        run_full_scrape(headless=headless, student_name=student, all_students=all_students)

        # Load data
        console.print("\n[blue]Loading data into database...[/blue]")
        load_scraped_data()

        console.print("[green]✓ Sync complete![/green]")

    except ImportError as e:
        console.print(f"[red]Error: Scraper not available. {e}[/red]")
        console.print("Run manually: python scripts/scrape_full.py && python scripts/load_data.py")
    except Exception as e:
        console.print(f"[red]Sync failed: {e}[/red]")


@cli.command()
@click.option("--student", "-s", default="all", help="Student name (default: all)")
@click.option("--ignore-future", is_flag=True, help="Hide assignments not yet past due date")
@click.option("--ignore-older-than", type=int, default=None, metavar="DAYS", help="Hide assignments overdue more than N days")
def missing(student: str, ignore_future: bool, ignore_older_than: int):
    """Show missing assignments."""
    repo = Repository()

    def filter_list(missing_list: list) -> list:
        result = missing_list
        if ignore_future:
            result = [m for m in result if (m.get("days_overdue") or 0) > 0]
        if ignore_older_than is not None:
            result = [m for m in result if (m.get("days_overdue") or 0) <= ignore_older_than]
        return result

    def print_missing_for(name: str, missing_list: list):
        """Print missing assignments for one student, split into overdue / not-yet-due."""
        console.print(f"\n[bold cyan]=== {name} ===[/bold cyan]")

        filtered = filter_list(missing_list)

        if not filtered:
            console.print("[green]  nothing missing[/green]")
            return

        overdue = [m for m in filtered if (m.get("days_overdue") or 0) > 0]
        not_yet_due = [m for m in filtered if (m.get("days_overdue") or 0) <= 0]

        def make_table(rows, title_str, name_style):
            t = Table(title=title_str, show_lines=False)
            t.add_column("Assignment", style=name_style)
            t.add_column("Course")
            t.add_column("Score")
            t.add_column("Due Date")
            t.add_column("Days Overdue", justify="right")
            for m in rows:
                days = m.get("days_overdue") or 0
                days_str = f"{int(days)}" if days > 0 else "-"
                score = m.get("score") or "--"
                t.add_row(
                    m["assignment_name"][:40],
                    m["course_name"][:25],
                    score,
                    str(m.get("due_date") or "N/A"),
                    days_str,
                )
            return t

        if overdue:
            console.print(make_table(overdue, "Overdue", "red"))
            console.print(f"  [bold red]{len(overdue)} overdue[/bold red]")

        if not_yet_due:
            console.print(make_table(not_yet_due, "Not Yet Due (no score yet)", "yellow"))
            console.print(f"  [yellow]{len(not_yet_due)} not yet due[/yellow]")

    if student.lower() == "all":
        students = repo.get_students()
        if not students:
            console.print("[yellow]No students in database.[/yellow]")
            return
        for s in students:
            missing_list = repo.get_missing_assignments(s["id"])
            print_missing_for(s["first_name"], missing_list)
    else:
        s = repo.get_student_by_name(student)
        if not s:
            console.print(f"[red]Student not found: {student}[/red]")
            return
        missing_list = repo.get_missing_assignments(s["id"])
        print_missing_for(s["first_name"], missing_list)


@cli.command()
@click.option("--student", "-s", required=True, help="Student name")
def grades(student: str):
    """Show current grades for a student."""
    repo = Repository()

    s = repo.get_student_by_name(student)
    if not s:
        console.print(f"[red]Student not found: {student}[/red]")
        # Show available students
        students = repo.get_students()
        if students:
            names = ", ".join([st["first_name"] for st in students])
            console.print(f"[yellow]Available students: {names}[/yellow]")
        return

    grades_list = repo.get_current_grades(s["id"])

    if not grades_list:
        console.print(f"[yellow]No grades found for {student}[/yellow]")
        return

    table = Table(title=f"Current Grades - {s['first_name']}")
    table.add_column("Course")
    table.add_column("Grade", justify="center")
    table.add_column("Term")
    table.add_column("Teacher")

    for g in grades_list:
        grade = g.get("letter_grade", "N/A")
        grade_style = (
            "green"
            if grade in ["A", "4", "3.5", "P"]
            else "yellow"
            if grade in ["B", "3"]
            else "red"
        )
        table.add_row(
            g["course_name"][:30],
            f"[{grade_style}]{grade}[/{grade_style}]",
            g["term"],
            g.get("teacher_name", "N/A")[:20],
        )

    console.print(table)


@cli.command()
@click.option("--student", "-s", required=True, help="Student name")
@click.option(
    "--term", "-t", default=None, help="School year term (e.g., 26-27). Defaults to most recent."
)
def schedule(student: str, term: str | None):
    """Show a student's class schedule."""
    repo = Repository()

    s = repo.get_student_by_name(student)
    if not s:
        console.print(f"[red]Student not found: {student}[/red]")
        students = repo.get_students()
        if students:
            names = ", ".join([st["first_name"] for st in students])
            console.print(f"[yellow]Available students: {names}[/yellow]")
        return

    # Default to the most recent school year, falling back to every entry when
    # none of them recorded a term.
    if term is None:
        terms = repo.get_schedule_terms(s["id"])
        term = terms[0] if terms else None

    schedule_list = repo.get_schedule(s["id"], term=term)

    if not schedule_list:
        scope = f" (term {term})" if term else ""
        console.print(
            f"[yellow]No schedule found for {student}{scope}. Run 'powerschool sync' first.[/yellow]"
        )
        return

    table = Table(title=f"Schedule - {s['first_name']} ({term or 'all terms'})")
    table.add_column("Period")
    table.add_column("Course")
    table.add_column("Teacher")
    table.add_column("Room")
    table.add_column("Enrolled", justify="center")
    table.add_column("Leaves", justify="center")

    for entry in schedule_list:
        table.add_row(
            entry.get("expression") or "-",
            entry["course_name"][:45],
            entry.get("teacher_name") or "-",
            entry.get("room") or "-",
            entry.get("enroll_date") or "-",
            entry.get("leave_date") or "-",
        )

    console.print(table)


@cli.command()
@click.option("--student", "-s", required=True, help="Student name")
def report(student: str):
    """Generate weekly report for a student."""
    repo = Repository()

    s = repo.get_student_by_name(student)
    if not s:
        console.print(f"[red]Student not found: {student}[/red]")
        return

    summary = repo.get_student_summary(s["id"])
    grades_list = repo.get_current_grades(s["id"])
    missing_list = repo.get_missing_assignments(s["id"])
    attendance = repo.get_attendance_summary(s["id"])
    actions = repo.get_action_items(s["id"])

    # Build report
    from datetime import datetime

    report_md = f"""# Weekly Report: {s["first_name"]}
*Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}*

## Overview
"""
    if summary:
        report_md += f"- **Courses**: {summary['course_count']}\n"
        report_md += f"- **Missing Assignments**: {summary['missing_assignments']}\n"

    if attendance:
        rate = attendance.get("attendance_rate", 0)
        status = "✅" if rate >= 95 else "⚠️" if rate >= 90 else "🔴"
        report_md += f"- **Attendance**: {rate:.1f}% {status}\n"

    report_md += "\n## Current Grades\n"
    if grades_list:
        for g in grades_list:
            grade = g.get("letter_grade", "-")
            report_md += f"- {g['course_name']}: **{grade}**\n"

    report_md += "\n## Missing Work\n"
    if missing_list:
        for m in missing_list:
            report_md += f"- ❌ {m['assignment_name']} ({m['course_name']})\n"
    else:
        report_md += "✅ No missing assignments!\n"

    report_md += "\n## Action Items\n"
    if actions:
        for i, a in enumerate(actions[:5], 1):
            report_md += f"{i}. {a.get('suggested_action', a['message'])}\n"
    else:
        report_md += "No immediate actions needed.\n"

    console.print(Markdown(report_md))


@cli.command("serve-mcp")
def serve_mcp():
    """Start MCP server for AI agents."""
    console.print("[blue]Starting MCP server...[/blue]")
    console.print("Server will listen on stdio for MCP protocol messages.")
    console.print("Press Ctrl+C to stop.\n")

    try:
        import asyncio

        from src.mcp_server.server import main

        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Server stopped.[/yellow]")
    except Exception as e:
        console.print(f"[red]Server error: {e}[/red]")


@cli.command()
@click.option("--live", is_flag=True, help="Query PowerSchool directly (opens browser)")
@click.option("--headless", is_flag=True, help="Run browser in headless mode (with --live)")
def students(live: bool, headless: bool):
    """List students on the account.

    By default, shows students from the local database. Use --live to
    query PowerSchool directly (requires browser login).
    """
    if live:
        try:
            from playwright.sync_api import sync_playwright

            from scripts.scrape_full import get_students, login

            console.print("[blue]Logging into PowerSchool to fetch student list...[/blue]")

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=headless, slow_mo=200)
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                page = context.new_page()

                if not login(page):
                    console.print("[red]Login failed.[/red]")
                    browser.close()
                    return

                student_list = get_students(page)
                browser.close()

            if not student_list:
                console.print("[yellow]No students found on account.[/yellow]")
                return

            table = Table(title="Students on PowerSchool Account")
            table.add_column("Name")
            table.add_column("Student Number")
            table.add_column("ID")
            table.add_column("Selected", justify="center")

            for s in student_list:
                selected = "✓" if s.get("selected") else ""
                table.add_row(s["name"], s.get("student_number", ""), s["id"], f"[green]{selected}[/green]")

            console.print(table)

        except ImportError as e:
            console.print(f"[red]Error: Scraper dependencies not available. {e}[/red]")
        except Exception as e:
            console.print(f"[red]Failed to fetch students: {e}[/red]")
    else:
        repo = Repository()
        student_list = repo.get_students()

        if not student_list:
            console.print("[yellow]No students in database. Run 'powerschool sync' first.[/yellow]")
            return

        table = Table(title="Students in Database")
        table.add_column("Name")
        table.add_column("Grade")
        table.add_column("School")

        for s in student_list:
            name = s['first_name']
            last = s.get('last_name')
            if last:
                name = f"{name} {last}"
            table.add_row(name, str(s.get("grade_level") or ""), s.get("school_name") or "")

        console.print(table)


@cli.command()
def status():
    """Show last sync time and student overview."""
    from datetime import datetime, timezone

    repo = Repository()

    # Last sync
    sync = repo.get_last_sync()
    if sync:
        completed_at = sync.get("completed_at") or sync.get("started_at")
        try:
            synced_dt = datetime.fromisoformat(completed_at)
            if synced_dt.tzinfo is None:
                synced_dt = synced_dt.replace(tzinfo=timezone.utc)
            delta = datetime.now(timezone.utc) - synced_dt
            hours = int(delta.total_seconds() // 3600)
            minutes = int((delta.total_seconds() % 3600) // 60)
            if hours >= 24:
                age_str = f"{hours // 24}d {hours % 24}h ago"
            elif hours > 0:
                age_str = f"{hours}h {minutes}m ago"
            else:
                age_str = f"{minutes}m ago"
        except (ValueError, TypeError):
            age_str = "unknown"

        sync_status = sync.get("status", "unknown")
        status_icon = (
            "[green]✅ completed[/green]" if sync_status == "completed"
            else "[red]❌ failed[/red]" if sync_status == "failed"
            else f"[yellow]{sync_status}[/yellow]"
        )
        console.print(f"Last sync:   {completed_at}  {status_icon}  ({age_str})")
        if sync.get("assignments_found") is not None:
            console.print(f"Assignments: {sync['assignments_found']} found in last sync")
        if sync.get("error_message"):
            console.print(f"[red]Error:       {sync['error_message']}[/red]")
    else:
        console.print("[yellow]Last sync:   never — run `sync` to pull data from PowerSchool[/yellow]")

    # Students overview
    students = repo.get_students()
    if students:
        console.print()
        for s in students:
            summary = repo.get_student_summary(s["id"])
            if summary:
                missing_count = summary.get("missing_assignments", 0)
                missing_style = "red" if missing_count > 0 else "green"
                console.print(
                    f"  • {s['first_name']}: "
                    f"[{missing_style}]{missing_count} missing[/{missing_style}], "
                    f"{summary.get('course_count', 0)} courses"
                )


@cli.command()
@click.option("--student", "-s", required=True, help="Student name")
def actions(student: str):
    """Show action items for a student."""
    repo = Repository()

    s = repo.get_student_by_name(student)
    if not s:
        console.print(f"[red]Student not found: {student}[/red]")
        return

    action_list = repo.get_action_items(s["id"])

    if not action_list:
        console.print(
            Panel(
                "[green]No action items - everything looks good! 🎉[/green]",
                title=f"Action Items - {s['first_name']}",
            )
        )
        return

    console.print(Panel(f"[bold]Action Items - {s['first_name']}[/bold]"))

    for a in action_list:
        priority = a.get("priority", "medium").upper()
        if priority in ["HIGH", "CRITICAL"]:
            emoji = "🔴"
            style = "red"
        else:
            emoji = "🟡"
            style = "yellow"

        console.print(f"{emoji} [{style}][{priority}][/{style}] {a['message']}")
        if a.get("suggested_action"):
            console.print(f"   → {a['suggested_action']}")
        console.print()


def main():
    """Main entry point."""
    cli()


if __name__ == "__main__":
    main()
