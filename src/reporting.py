"""Shared rendering helpers for missing-assignment output.

`days_overdue` (from `v_missing_assignments`) is a whole-day, local-date
difference: positive = genuinely late, 0 = due today, negative = not yet due.
Nothing may be called "overdue" until it is at least one full day late.
"""

import math

DUE_TODAY = "due today"


def whole_days_overdue(days) -> int:
    """Normalize a raw days_overdue value to whole days late.

    Fractional values (from older views or ad-hoc date math) are floored, so a
    fraction of a day late — e.g. 0.54, the old UTC-offset artifact — counts as
    0 whole days and renders as "due today" instead of "0 days overdue".
    """
    if days is None:
        return 0
    return int(math.floor(days))


def is_overdue(days) -> bool:
    """True only for items at least one full day past their due date."""
    return whole_days_overdue(days) > 0


def is_due_today(days) -> bool:
    """True for items due today — actionable, but not overdue."""
    return whole_days_overdue(days) == 0


def days_overdue_label(days) -> str:
    """Human label: 'due today', 'N day(s) overdue', or 'not yet due'."""
    n = whole_days_overdue(days)
    if n > 0:
        return f"{n} day overdue" if n == 1 else f"{n} days overdue"
    if n == 0:
        return DUE_TODAY
    return "not yet due"
