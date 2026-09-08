"""Tests for parse_calendar.py -- Meeting Intel participant extraction."""

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = str(Path(__file__).parent.parent / "scripts" / "parse_calendar.py")


def run_parser(events, user_domains="example.com", max_per_meeting=3):
    """Helper: pipe events JSON into the script, return parsed output."""
    result = subprocess.run(
        [
            sys.executable,
            SCRIPT,
            "--user-domains",
            user_domains,
            "--max-per-meeting",
            str(max_per_meeting),
        ],
        input=json.dumps(events),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Script failed: {result.stderr}"
    return json.loads(result.stdout)


def test_filters_internal_participants():
    """Participants at user_domains are excluded."""
    events = [
        {
            "summary": "Sync",
            "start": {"dateTime": "2026-08-05T10:00:00Z"},
            "attendees": [
                {"email": "me@example.com", "displayName": "Me"},
                {"email": "external@acme.com", "displayName": "External"},
            ],
        }
    ]
    result = run_parser(events)
    assert len(result) == 1
    assert result[0]["participant_email"] == "external@acme.com"
    assert result[0]["company_domain"] == "acme.com"


def test_multiple_user_domains():
    """Comma-separated user domains all get filtered."""
    events = [
        {
            "summary": "Call",
            "start": {"dateTime": "2026-08-05T14:00:00Z"},
            "attendees": [
                {"email": "a@foo.com", "displayName": "A"},
                {"email": "b@bar.com", "displayName": "B"},
                {"email": "c@external.io", "displayName": "C"},
            ],
        }
    ]
    result = run_parser(events, user_domains="foo.com,bar.com")
    assert len(result) == 1
    assert result[0]["participant_email"] == "c@external.io"


def test_caps_at_max_per_meeting():
    """More than max_per_meeting external participants triggers the cap."""
    events = [
        {
            "summary": "Big meeting",
            "start": {"dateTime": "2026-08-05T09:00:00Z"},
            "attendees": [
                {"email": "me@example.com", "displayName": "Me"},
                {"email": "a@corp.com", "displayName": "A"},
                {"email": "b@corp.com", "displayName": "B"},
                {"email": "c@corp.com", "displayName": "C"},
                {"email": "d@corp.com", "displayName": "D"},
            ],
        }
    ]
    result = run_parser(events, max_per_meeting=2)
    assert len(result) == 2


def test_seniority_prioritization():
    """C-suite/VP/Director/Founder titles are preferred over generic."""
    events = [
        {
            "summary": "Strategy call",
            "start": {"dateTime": "2026-08-05T11:00:00Z"},
            "attendees": [
                {"email": "me@example.com", "displayName": "Me"},
                {"email": "intern@corp.com", "displayName": "Intern Joe"},
                {"email": "ceo@corp.com", "displayName": "CEO Sarah"},
                {"email": "vp@corp.com", "displayName": "VP Marketing Tom"},
                {"email": "admin@corp.com", "displayName": "Admin Pat"},
            ],
        }
    ]
    result = run_parser(events, max_per_meeting=2)
    emails = {r["participant_email"] for r in result}
    assert "ceo@corp.com" in emails
    assert "vp@corp.com" in emails


def test_personal_email_domain_handling():
    """gmail.com, outlook.com, etc. produce empty company_domain."""
    events = [
        {
            "summary": "Coffee chat",
            "start": {"dateTime": "2026-08-05T15:00:00Z"},
            "attendees": [
                {"email": "me@example.com", "displayName": "Me"},
                {"email": "someone@gmail.com", "displayName": "Someone"},
            ],
        }
    ]
    result = run_parser(events)
    assert len(result) == 1
    assert result[0]["company_domain"] == ""


def test_deduplication_across_meetings():
    """Same participant in two meetings appears only once."""
    events = [
        {
            "summary": "Meeting A",
            "start": {"dateTime": "2026-08-05T09:00:00Z"},
            "attendees": [
                {"email": "me@example.com", "displayName": "Me"},
                {"email": "jane@acme.com", "displayName": "Jane"},
            ],
        },
        {
            "summary": "Meeting B",
            "start": {"dateTime": "2026-08-05T14:00:00Z"},
            "attendees": [
                {"email": "me@example.com", "displayName": "Me"},
                {"email": "jane@acme.com", "displayName": "Jane"},
            ],
        },
    ]
    result = run_parser(events)
    assert len(result) == 1
    assert result[0]["meeting_title"] == "Meeting A"


def test_no_external_meetings_returns_empty():
    """All-internal meeting produces no output."""
    events = [
        {
            "summary": "Team standup",
            "start": {"dateTime": "2026-08-05T09:30:00Z"},
            "attendees": [
                {"email": "me@example.com", "displayName": "Me"},
                {"email": "colleague@example.com", "displayName": "Colleague"},
            ],
        }
    ]
    result = run_parser(events)
    assert result == []


def test_empty_events_returns_empty():
    """No events at all produces empty output."""
    result = run_parser([])
    assert result == []


def test_missing_attendees_field():
    """Events without attendees (e.g., focus time blocks) are skipped."""
    events = [
        {
            "summary": "Focus time",
            "start": {"dateTime": "2026-08-05T08:00:00Z"},
        }
    ]
    result = run_parser(events)
    assert result == []


def test_meeting_metadata_preserved():
    """Output includes correct meeting title and time."""
    events = [
        {
            "summary": "Q3 Planning",
            "start": {"dateTime": "2026-08-05T10:00:00+02:00"},
            "attendees": [
                {"email": "me@example.com", "displayName": "Me"},
                {"email": "ext@corp.com", "displayName": "Ext"},
            ],
        }
    ]
    result = run_parser(events)
    assert result[0]["meeting_title"] == "Q3 Planning"
    assert result[0]["meeting_time"] == "2026-08-05T10:00:00+02:00"


def test_displayname_fallback_to_email_local():
    """When displayName is missing, use email local part as name."""
    events = [
        {
            "summary": "Call",
            "start": {"dateTime": "2026-08-05T10:00:00Z"},
            "attendees": [
                {"email": "me@example.com"},
                {"email": "jane.doe@acme.com"},
            ],
        }
    ]
    result = run_parser(events)
    assert result[0]["participant_name"] == "jane.doe"
