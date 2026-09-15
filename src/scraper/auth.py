"""PowerSchool authentication module.

Provides centralized login functionality for all scraping scripts,
including multi-student support for accounts with multiple children.
"""

import os
import re
from typing import Dict, List, Optional

from dotenv import load_dotenv
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from src.logutils import get_logger, with_context

load_dotenv()

# Module logger
logger = get_logger(__name__)

# Configuration from environment
BASE_URL = os.getenv("POWERSCHOOL_URL")
USERNAME = os.getenv("POWERSCHOOL_USERNAME")
PASSWORD = os.getenv("POWERSCHOOL_PASSWORD")


class AuthenticationError(Exception):
    """Raised when login fails."""

    pass


# Human-readable reason for the most recent login failure.
#
# Log formatters do not render ``extra_data``, so the portal's own error text
# ("Your password has been expired. Create a new password.") used to be lost:
# the sync watchdog reported only "Login failed" and the operator had to
# reproduce the login by hand to learn why. Carrying the reason in the log
# message itself (and in this module state) makes it reach the console, the
# cron job's captured stderr, and the Discord DM.
_LAST_LOGIN_ERROR: Optional[str] = None

# Selectors for the portal's error banner, most specific first.
_LOGIN_ALERT_SELECTORS = (
    ".feedback-alert",
    "[role=alert]",
    ".error-message",
    "#errorMessage",
)


def get_last_login_error() -> Optional[str]:
    """Return the human-readable reason for the most recent login failure.

    Returns:
        The portal's own error text if it displayed one, else the underlying
        exception string. None when the last login attempt succeeded.
    """
    return _LAST_LOGIN_ERROR


def _is_authenticated(page: Page) -> bool:
    """Check that the page is a signed-in guardian view, not the sign-in form.

    Args:
        page: Playwright page instance after submitting credentials

    Returns:
        True when the sign-in form is gone and the URL is a guardian page.
    """
    try:
        url = page.url or ""
    except Exception:
        url = ""
    if "/guardian/" not in url:
        return False
    try:
        if page.query_selector("#fieldAccount") is not None:
            return False  # sign-in form still present -> not authenticated
    except Exception:
        return False
    return True


def _login_alert_text(page: Page) -> str:
    """Read the PowerSchool error banner, if the login page is showing one.

    Args:
        page: Playwright page instance, positioned on the login page

    Returns:
        Whitespace-normalised banner text, or "" when no banner is present.
    """
    for selector in _LOGIN_ALERT_SELECTORS:
        try:
            element = page.query_selector(selector)
        except Exception:
            continue
        if element is None:
            continue
        try:
            text = element.inner_text() or ""
        except Exception:
            continue
        text = " ".join(text.split())
        if text:
            return text
    return ""


def get_base_url() -> str:
    """Get the PowerSchool base URL from environment.

    Returns:
        Base URL for PowerSchool portal

    Raises:
        ValueError: If POWERSCHOOL_URL is not set
    """
    if not BASE_URL:
        raise ValueError("POWERSCHOOL_URL environment variable is required")
    return BASE_URL.rstrip("/")


def get_credentials() -> tuple[str, str]:
    """Get PowerSchool credentials from environment.

    Returns:
        Tuple of (username, password)

    Raises:
        ValueError: If credentials are not set
    """
    if not USERNAME or not PASSWORD:
        raise ValueError(
            "POWERSCHOOL_USERNAME and POWERSCHOOL_PASSWORD environment variables are required"
        )
    return USERNAME, PASSWORD


def login(
    page: Page,
    base_url: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout: int = 15000,
    verbose: bool = True,
) -> bool:
    """Login to PowerSchool parent portal.

    Args:
        page: Playwright page instance
        base_url: PowerSchool URL (uses env var if not provided)
        username: Login username (uses env var if not provided)
        password: Login password (uses env var if not provided)
        timeout: Timeout in ms for login completion
        verbose: Print status messages

    Returns:
        True if login successful, False otherwise

    Example:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            if login(page):
                # Proceed with scraping
                ...
    """
    url = base_url or get_base_url()
    user = username or USERNAME
    pwd = password or PASSWORD

    global _LAST_LOGIN_ERROR
    _LAST_LOGIN_ERROR = None

    if not user or not pwd:
        logger.error("Missing credentials for PowerSchool login")
        return False

    login_url = f"{url}/public/home.html"
    logger.info("Navigating to login page", extra={"extra_data": {"url": login_url}})

    try:
        with with_context(operation="login", component="auth"):
            page.goto(login_url, wait_until="networkidle")
            page.wait_for_selector("#fieldAccount", timeout=10000)

            logger.debug(
                "Login form loaded, entering credentials", extra={"extra_data": {"username": user}}
            )

            page.fill("#fieldAccount", user)
            page.fill("#fieldPassword", pwd)
            page.click("#btn-enter-sign-in")

            page.wait_for_url("**/guardian/**", timeout=timeout)

            # A URL match alone is not proof of authentication. A rejected
            # login (wrong credentials) can pass through /guardian/ on a
            # redirect bounce and land back on the sign-in page, which used to
            # be reported as "Login successful" — producing an empty scrape and
            # an empty overwrite of the database.
            if not _is_authenticated(page):
                _LAST_LOGIN_ERROR = (
                    _login_alert_text(page) or "portal returned to the sign-in page after submit"
                )
                logger.error("Login failed: %s", _LAST_LOGIN_ERROR)
                return False

            logger.info("Login successful", extra={"extra_data": {"username": user}})
            return True

    except Exception as e:
        reason = _login_alert_text(page)
        _LAST_LOGIN_ERROR = reason or str(e)
        # Reason goes in the MESSAGE (formatters drop extra_data) so the cron
        # job's captured stderr — and therefore the alert DM — says why.
        logger.error("Login failed: %s", _LAST_LOGIN_ERROR)
        if reason:
            logger.warning(
                "Login error message displayed",
                extra={"extra_data": {"message": reason}},
            )
        if "expired" in _LAST_LOGIN_ERROR.lower():
            logger.error(
                "PowerSchool password expired — reset it at %s/public/home.html, "
                "update POWERSCHOOL_PASSWORD in .env, then re-run the sync.",
                url,
            )
        return False


def login_or_raise(
    page: Page,
    base_url: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout: int = 15000,
    verbose: bool = True,
) -> None:
    """Login to PowerSchool, raising an exception on failure.

    Args:
        page: Playwright page instance
        base_url: PowerSchool URL (uses env var if not provided)
        username: Login username (uses env var if not provided)
        password: Login password (uses env var if not provided)
        timeout: Timeout in ms for login completion
        verbose: Print status messages

    Raises:
        AuthenticationError: If login fails
    """
    if not login(page, base_url, username, password, timeout, verbose):
        reason = get_last_login_error()
        raise AuthenticationError(
            f"Failed to login to PowerSchool: {reason}"
            if reason
            else "Failed to login to PowerSchool"
        )


# =============================================================================
# Multi-Student Support
# =============================================================================


def _extract_student_id_from_href(href: Optional[str]) -> Optional[str]:
    """Extract student ID from switchStudent JavaScript call.

    Args:
        href: The href attribute value, e.g., "javascript:switchStudent(55260);"

    Returns:
        The student ID string, or None if not found.

    Example:
        >>> _extract_student_id_from_href("javascript:switchStudent(55260);")
        "55260"
    """
    if not href:
        return None

    # Match pattern like: switchStudent(12345) or switchStudent( 12345 )
    match = re.search(r"switchStudent\s*\(\s*(\d+)\s*\)", href)
    if match:
        return match.group(1)
    return None


def get_available_students(page: Page) -> List[Dict[str, any]]:
    """Get list of all students available on the parent account.

    Parses the student selector dropdown from the PowerSchool page
    to retrieve all associated students.

    Args:
        page: Playwright page instance (must be logged in)

    Returns:
        List of student dictionaries with keys:
            - id: PowerSchool student ID
            - name: Student's display name
            - selected: True if this is the currently active student

    Example:
        >>> students = get_available_students(page)
        >>> students
        [
            {"id": "55260", "name": "Delilah", "selected": True},
            {"id": "55259", "name": "Sean", "selected": False}
        ]
    """
    students = []

    # The student list is in <ul id="students-list"> with <li> items
    # Each <li> contains an anchor with href="javascript:switchStudent(ID);"
    # The selected student's <li> has class="selected"
    student_items = page.query_selector_all("#students-list li")

    for item in student_items:
        # Check if this student is selected
        class_attr = item.get_attribute("class") or ""
        is_selected = "selected" in class_attr

        # Get the anchor element inside
        anchor = item.query_selector("a")
        if anchor:
            name = anchor.inner_text().strip()
            href = anchor.get_attribute("href")
            student_id = _extract_student_id_from_href(href)

            if student_id and name:
                students.append(
                    {
                        "id": student_id,
                        "name": name,
                        "selected": is_selected,
                    }
                )

    return students


def get_current_student(page: Page) -> Optional[Dict[str, any]]:
    """Get the currently selected student.

    Args:
        page: Playwright page instance (must be logged in)

    Returns:
        Dictionary with id and name of current student, or None if not found.

    Example:
        >>> student = get_current_student(page)
        >>> student
        {"id": "55260", "name": "Delilah"}
    """
    # Find the selected student's list item
    selected_item = page.query_selector("#students-list li.selected")

    if not selected_item:
        return None

    anchor = selected_item.query_selector("a")
    if not anchor:
        return None

    name = anchor.inner_text().strip()
    href = anchor.get_attribute("href")
    student_id = _extract_student_id_from_href(href)

    if student_id and name:
        return {"id": student_id, "name": name}

    return None


def switch_to_student(
    page: Page, student_id: str, timeout: int = 15000, verbose: bool = False
) -> bool:
    """Switch to a different student on the parent account.

    Uses the PowerSchool student switcher form to change the active student.
    This submits a POST request and waits for the page to reload.

    Args:
        page: Playwright page instance (must be logged in)
        student_id: The PowerSchool ID of the student to switch to
        timeout: Timeout in ms for page reload
        verbose: Print status messages

    Returns:
        True if switch was successful, False otherwise.

    Example:
        >>> switch_to_student(page, "55259")
        True
    """
    try:
        with with_context(operation="switch_student", student_id=student_id, component="auth"):
            # Find the switch student form
            form = page.query_selector("#switch_student_form")
            if not form:
                logger.warning("Student switch form not found on page")
                return False

            # Find the hidden input for student ID
            student_input = page.query_selector(
                '#switch_student_form input[name="selected_student_id"]'
            )
            if not student_input:
                logger.warning("Student ID input not found in form")
                return False

            logger.info("Switching to student", extra={"extra_data": {"student_id": student_id}})

            # Fill the student ID value using JavaScript to avoid focus issues
            page.evaluate(
                """(studentId) => {
                    var form = document.getElementById('switch_student_form');
                    form.selected_student_id.value = studentId;
                    form.submit();
                }""",
                student_id,
            )

            # Wait for navigation to complete
            page.wait_for_load_state("networkidle", timeout=timeout)

            # Verify the switch worked by checking current student
            # Give a short delay for the page to fully update
            page.wait_for_timeout(500)

            logger.debug(
                "Student switch navigation completed",
                extra={"extra_data": {"student_id": student_id}},
            )

            return True

    except PlaywrightTimeout:
        logger.error(
            "Timeout waiting for student switch",
            extra={"extra_data": {"student_id": student_id, "timeout_ms": timeout}},
        )
        return False
    except Exception as e:
        logger.error(
            "Error switching student",
            extra={"extra_data": {"student_id": student_id, "error": str(e)}},
            exc_info=True,
        )
        return False
