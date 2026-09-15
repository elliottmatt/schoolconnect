"""Tests for surfacing the portal's login failure reason.

Regression guard: PowerSchool's own error text ("Your password has been
expired. Create a new password.") used to be logged only as ``extra_data``,
which the formatters drop. The sync watchdog therefore reported a bare
"Login failed" and the reason had to be reproduced by hand. The reason must
travel in the log message itself so it reaches cron stderr and the alert DM.
"""

from __future__ import annotations

import logging

import pytest

from src.scraper.auth import (
    AuthenticationError,
    _login_alert_text,
    get_last_login_error,
    login,
    login_or_raise,
)
from src.scraper.auth import (
    logger as auth_logger,
)

EXPIRED_MESSAGE = "Your password has been expired. Create a new password."


class _FakeElement:
    def __init__(self, text: str) -> None:
        self._text = text

    def inner_text(self) -> str:
        return self._text


class _FakePage:
    """Minimal stand-in for a Playwright page positioned on the login form."""

    def __init__(
        self,
        alert: str | None = EXPIRED_MESSAGE,
        fail: bool = True,
        url: str = "https://example.test/guardian/home.html",
        show_sign_in_form: bool = False,
    ) -> None:
        self._alert = alert
        self._fail = fail
        self.url = url
        self._show_sign_in_form = show_sign_in_form
        self.filled: dict[str, str] = {}

    def goto(self, *_args, **_kwargs) -> None:
        return None

    def wait_for_selector(self, *_args, **_kwargs) -> None:
        return None

    def fill(self, selector: str, value: str) -> None:
        self.filled[selector] = value

    def click(self, *_args, **_kwargs) -> None:
        return None

    def wait_for_url(self, *_args, **_kwargs) -> None:
        if self._fail:
            raise TimeoutError("Timeout 15000ms exceeded waiting for '**/guardian/**'")

    def query_selector(self, selector: str):
        if selector == ".feedback-alert" and self._alert:
            return _FakeElement(self._alert)
        if selector == "#fieldAccount" and self._show_sign_in_form:
            return _FakeElement("")
        return None


def test_login_failure_records_portal_reason():
    page = _FakePage()

    assert login(page, base_url="https://example.test", username="u", password="p") is False
    assert get_last_login_error() == EXPIRED_MESSAGE


def test_login_failure_reason_reaches_log_message():
    page = _FakePage()
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture()
    auth_logger.addHandler(handler)
    try:
        login(page, base_url="https://example.test", username="u", password="p")
    finally:
        auth_logger.removeHandler(handler)

    rendered = "\n".join(record.getMessage() for record in records)
    assert EXPIRED_MESSAGE in rendered, "reason must be in the log message, not only extra_data"
    assert "expired" in rendered.lower()


def test_login_failure_without_banner_falls_back_to_exception_text():
    page = _FakePage(alert=None)

    assert login(page, base_url="https://example.test", username="u", password="p") is False
    reason = get_last_login_error()
    assert reason and "Timeout" in reason


def test_login_success_clears_previous_reason():
    stale = _FakePage()
    login(stale, base_url="https://example.test", username="u", password="p")
    assert get_last_login_error() == EXPIRED_MESSAGE

    page = _FakePage(fail=False)
    assert login(page, base_url="https://example.test", username="u", password="p") is True
    assert get_last_login_error() is None


def test_login_or_raise_includes_reason():
    page = _FakePage()

    with pytest.raises(AuthenticationError) as excinfo:
        login_or_raise(page, base_url="https://example.test", username="u", password="p")

    assert EXPIRED_MESSAGE in str(excinfo.value)


def test_login_alert_text_ignores_blank_banner():
    class _BlankPage(_FakePage):
        def query_selector(self, selector: str):
            return _FakeElement("   \n  ")

    assert _login_alert_text(_BlankPage()) == ""


def test_login_url_match_alone_is_not_treated_as_success():
    """A rejected login can bounce through /guardian/ and back to sign-in.

    Regression: that bounce was reported as "Login successful", so the scraper
    ran unauthenticated, returned zero rows, and the loader overwrote a
    populated database with nothing.
    """
    page = _FakePage(fail=False, show_sign_in_form=True)

    assert login(page, base_url="https://example.test", username="u", password="p") is False
    assert get_last_login_error() == EXPIRED_MESSAGE


def test_login_off_guardian_url_is_not_success():
    page = _FakePage(fail=False, url="https://example.test/public/home.html", alert=None)

    assert login(page, base_url="https://example.test", username="u", password="p") is False
    assert get_last_login_error() is not None
