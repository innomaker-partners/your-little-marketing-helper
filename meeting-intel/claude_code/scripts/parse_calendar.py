#!/usr/bin/env python3
"""Extract external meeting participants from calendar event JSON.

Usage:
    echo '<events_json>' | python parse_calendar.py \
        --user-domains example.com,mycompany.io \
        --max-per-meeting 3

Reads a JSON array of calendar events from stdin.
Outputs a JSON array of external participants to stdout.
"""

import argparse
import json
import re
import sys

PERSONAL_EMAIL_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "yahoo.com",
    "yahoo.co.uk",
    "icloud.com",
    "me.com",
    "mac.com",
    "aol.com",
    "protonmail.com",
    "proton.me",
    "mail.com",
    "gmx.com",
    "gmx.net",
    "yandex.com",
    "zoho.com",
}

SENIORITY_PATTERNS = re.compile(
    r"\b(CEO|CFO|COO|CTO|CIO|CMO|CPO|CRO|CISO"
    r"|Chief|C-Suite"
    r"|VP|Vice\s*President"
    r"|Director"
    r"|Partner"
    r"|Founder|Co-Founder"
    r"|Owner"
    r"|President"
    r"|Managing\s+Director"
    r"|Head\s+of"
    r"|SVP|EVP)\b",
    re.IGNORECASE,
)


def extract_domain(email):
    """Return the domain part of an email address."""
    return email.split("@", 1)[1].lower() if "@" in email else ""


def is_personal_email(domain):
    """Check if a domain is a known personal email provider."""
    return domain in PERSONAL_EMAIL_DOMAINS


def has_seniority_signal(name):
    """Check if a display name contains a seniority title keyword."""
    return bool(SENIORITY_PATTERNS.search(name))


def parse_events(events, user_domains, max_per_meeting):
    """Parse calendar events and return external participants."""
    user_domain_set = {d.strip().lower() for d in user_domains if d.strip()}
    seen_emails = set()
    results = []

    for event in events:
        attendees = event.get("attendees", [])
        if not attendees:
            continue

        meeting_title = event.get("summary", "Untitled meeting")
        start = event.get("start", {})
        meeting_time = start.get("dateTime", start.get("date", ""))

        external = []
        for att in attendees:
            email = att.get("email", "").lower()
            if not email or "@" not in email:
                continue
            domain = extract_domain(email)
            if domain in user_domain_set:
                continue
            if email in seen_emails:
                continue
            name = att.get("displayName", email.split("@")[0])
            company_domain = "" if is_personal_email(domain) else domain
            external.append(
                {
                    "meeting_title": meeting_title,
                    "meeting_time": meeting_time,
                    "participant_name": name,
                    "participant_email": email,
                    "company_domain": company_domain,
                    "_seniority": has_seniority_signal(name),
                }
            )

        if len(external) > max_per_meeting:
            external.sort(key=lambda p: (not p["_seniority"], p["participant_email"]))
            external = external[:max_per_meeting]

        for p in external:
            if p["participant_email"] not in seen_emails:
                seen_emails.add(p["participant_email"])
                del p["_seniority"]
                results.append(p)

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Extract external participants from calendar events."
    )
    parser.add_argument(
        "--user-domains",
        required=True,
        help="Comma-separated list of internal email domains.",
    )
    parser.add_argument(
        "--max-per-meeting",
        type=int,
        default=3,
        help="Maximum external participants per meeting (default: 3).",
    )
    args = parser.parse_args()

    events = json.load(sys.stdin)
    user_domains = [d.strip() for d in args.user_domains.split(",")]
    results = parse_events(events, user_domains, args.max_per_meeting)
    json.dump(results, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
