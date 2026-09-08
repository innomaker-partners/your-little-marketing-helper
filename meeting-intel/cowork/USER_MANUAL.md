# Meeting Intel Cowork Plugin - User Manual

Research external meeting participants and produce intelligence briefs
before your calls.

---

## Contents

1. [Installation](#installation)
2. [Connector setup](#connector-setup)
3. [First-run configuration](#first-run-configuration)
4. [Day-to-day use](#day-to-day-use)
5. [Reading the output](#reading-the-output)
6. [Customization](#customization)
7. [Troubleshooting](#troubleshooting)
8. [Privacy](#privacy)
9. [Updating and uninstalling](#updating-and-uninstalling)

---

## Installation

From the folder containing this file, run:

```
claude plugin install .
```

Verify the plugin loaded:

```
claude plugin validate .
```

The validator should report `meeting-intel` as a valid plugin with one
skill (`meeting-research`). If it shows the plugin registered but no
skill loaded, see the troubleshooting section below.

---

## Connector setup

Meeting Intel uses Cowork's native connector system for calendar access
and brief delivery. There is no MCP configuration to edit and no API
credentials to manage directly - connections go through Cowork's OAuth
flow.

### Calendar connector (required for automatic use)

In Cowork: **Settings > Connectors > add your calendar provider**

| Provider | What it provides |
|---|---|
| Microsoft 365 | Today's meetings, attendee names and emails |
| Google Calendar | Today's meetings, attendee names and emails |

Without a calendar connector the plugin still works - it will ask you to
describe who you're meeting when you run the command.

### Delivery connectors (optional)

| Connector | What it provides |
|---|---|
| Gmail | Email delivery of finished briefs |
| Microsoft 365 (email) | Email delivery of finished briefs |
| Slack | Delivery to a channel or DM |
| Microsoft Teams | Delivery to a channel or DM |

If none of these are connected, briefs are saved as markdown files and
shown in the conversation. Nothing breaks.

### Web research

Web research (company websites, LinkedIn search) uses Claude's built-in
web access. No additional connector or API account is needed for
research.

---

## First-run configuration

When you run `/meeting-intel:prep` for the first time, the plugin checks
for `config/config.json`. If the file does not exist, it runs an
interactive setup wizard before generating any briefs.

The wizard asks four questions:

**1. Your company email domain**

Example: `acme.com`

Enter all domains that belong to your organization, separated by commas.
The plugin uses these to tell your colleagues apart from external
participants. Getting this right determines what gets researched and what
gets skipped.

**2. How you use meetings**

Choose the archetype that best describes your role:

- `sales` - fit assessment, pain points, buying signals, deal openers
- `consulting` - engagement signals, challenges, decision-making
  structure, positioning angles
- `investor` - growth metrics, team assessment, market position, risk
  factors, due-diligence flags
- `general` - neutral intelligence, no product or service angle

**3. A brief description of what you do and care about in meetings**

One sentence. Examples the wizard shows:

- Sales: "I sell marketing automation to mid-market SaaS companies. I
  care about whether they have an existing stack and budget."
- Consulting: "I consult on digital transformation for manufacturing. I
  care about organizational readiness and decision-maker access."
- Investor: "I invest in Series A B2B SaaS. I care about team quality,
  market size, and capital efficiency."
- General: "I run partnerships at a media company. I care about mutual
  audience overlap."

This text becomes `user_context` in the config and shapes the
conversation starters at the end of each brief.

**4. Where to save briefs**

Default is `~/meeting-briefs`. Briefs are named
`YYYY-MM-DD-<meeting-title-slugified>.md`.

After the wizard finishes, it writes `config/config.json` and tells you
to run `/meeting-intel:prep` again. The second run produces your first
briefs.

**Shortcut:** If you already know your settings, copy
`config/config.example.json` to `config/config.json`, fill in the real
values, and the wizard is skipped entirely on first run.

---

## Day-to-day use

### Run for all of today's external meetings

```
/meeting-intel:prep
```

The plugin pulls today's calendar, identifies external participants
(anyone not on your configured domains), and produces one brief per
meeting.

### Run for one specific meeting or person

```
/meeting-intel:prep Acme Corp
/meeting-intel:prep Jane Smith
```

Providing a company name or person name as an argument limits research to
that meeting or participant.

### Natural language triggers

The plugin activates without the explicit command when you ask things
like:

- "Prep me for my meeting with Acme Corp"
- "Who am I meeting today?"
- "Research Jane Smith at Acme Corp before my call"

---

## Reading the output

Each brief follows this structure:

```
# Meeting Brief: [Meeting Title]
[Date] . [Time] . [N participants researched]

## At a Glance
- [Name]: [Role] at [Company] - [single most useful fact]
- [One sentence connecting this meeting to your context]

## [Participant Name] - [Title/Role]

### Person
- Current role and tenure
- Professional background (2-3 sentences from search results)
- Confidence: which facts are verified vs inferred vs not found

### Company: [Company Name]
- What they do
- Size, location, markets
- Key products and services
- [confidence tags on each field]

### Conversation Starters
- [3 specific openers based only on actual research findings]

---
Research depth: [FULL / LIMITED / MINIMAL]
Research conducted [date]. Sources: [URLs actually visited].
Confidence key: [key]
```

### Confidence tags

| Tag | Meaning |
|---|---|
| `[✓ verified]` | Stated directly on the company's own website |
| `[~ inferred]` | Reasonably inferred from context |
| `[⏳ possibly stale]` | Found on LinkedIn; self-reported and may be outdated |
| `[? not found]` | Could not be established from available sources |

LinkedIn data is always tagged `⏳`, never `✓`. Conversation starters are
derived only from what was actually found - if nothing specific turned up,
the brief says so explicitly.

### Research depth indicator

- **FULL** - company website crawled and LinkedIn profile found and
  validated
- **LIMITED** - one primary source was missing or unreachable
- **MINIMAL** - research based on web search snippets only

---

## Customization

To change any setting after first run, edit `config/config.json` directly.
The available keys are:

| Key | Type | Description |
|---|---|---|
| `user_domains` | array of strings | Email domains belonging to your organization |
| `brief_archetype` | string | `sales`, `consulting`, `investor`, or `general` |
| `user_context` | string | One sentence describing your role and what you care about in meetings |
| `max_participants_per_meeting` | number | How many external participants to research per meeting (default: 3) |
| `output.markdown_path` | string | Where to save brief files (default: `~/meeting-briefs`) |
| `output.email_enabled` | boolean | Whether to email each brief after generating it |
| `output.email_to` | string | Email address for brief delivery (requires an email connector) |

The reference template with all keys and placeholder values is at
`config/config.example.json`.

---

## Troubleshooting

### Plugin seems installed but no skill is available

Check that `plugin.json` (at `.claude-plugin/plugin.json`) contains both
a `$schema` field and a `skills` array. Without the `skills` array the
runtime registers the plugin but loads no skill, so no command or natural
language trigger fires. The shipped `plugin.json` has both fields. If you
have edited or forked the file, verify those keys are present.

### Links in SKILL.md do not resolve when you edit or fork the plugin

Relative links inside `skills/meeting-research/SKILL.md` use `../../` to
reach files at the plugin root (for example, `../../CONNECTORS.md`). If
you copy the skill file to a different location or restructure the folder,
update those paths accordingly. A single missing `../` level is the most
common cause of broken cross-references.

### LinkedIn search finds no results or wrong person

The plugin researches LinkedIn through a two-step Apify actor pipeline
(a people search that returns candidate profile URLs, then a full
profile scrape of those candidates), mirroring the Make.com versions of
this tool. If a participant's profile is not found:

- Check that the Apify connector is set up (see CONNECTORS.md). Without
  it, the plugin falls back to a plain web search, which finds less.
- The search is length-sensitive. The people-search actor runs queries
  of 3 words or fewer as one exact quoted phrase, which almost never
  matches a name-plus-company combination; queries of 4 or more words
  run as a fuzzy search, which is what works. The skill therefore
  builds its query from the name plus a multi-word affiliation guess
  ("Jane Doe State University", not "Jane Doe State") and retries once
  with an alternative affiliation phrasing before giving up.
- If the search genuinely returns no candidates, the brief is produced
  with whatever company research found, tagged as LIMITED or MINIMAL
  depth, and the person is reported as "no LinkedIn match" - never
  invented.
- A person can also be found with LOW_CONFIDENCE_MATCH: the name matched
  but no affiliation in their profile aligned. Treat those briefs with
  care; the matched-on note in the brief tells you what the match rested
  on.

### Calendar access seems connected but the command reports no meetings

A connector that completed its OAuth flow but was not fully authorized can
appear connected in Settings while failing silently at runtime. The error
message may look like a general runtime failure rather than an auth
problem. If the plugin asks you for meeting details manually despite a
connector being listed, go to Settings > Connectors, remove the calendar
connection, and re-authorize it from scratch.

### Brief email was not sent even though email delivery is enabled

Email delivery requires both `output.email_enabled: true` in your config
AND an active email connector (Gmail or Microsoft 365 email). If the
connector is missing or disconnected, the plugin saves the brief to your
output folder and appends a note explaining that email delivery was
configured but unavailable.

### No external meetings found

The plugin filters out anyone whose email domain matches `user_domains`
in your config, plus the user's own email, calendar service addresses
(domains containing "calendar", "resource", "room"), and addresses with
no display name. If all attendees are filtered out, the plugin reports
"No external meetings found today." Check that `user_domains` contains
only your own organization's domains and not a domain belonging to an
external counterpart.

---

## Privacy

When a calendar connector is active, the plugin reads event titles,
start times, and attendee names and email addresses from your calendar.
It sends participant names and company name guesses to the Apify
LinkedIn actors as search queries, and the matched candidates' profile
URLs to the Apify profile scraper. Company research queries go through
Cowork's standard web access. Calendar contents themselves (titles,
descriptions) are not sent to Apify. Apify stores actor run inputs and
outputs under your Apify account; their terms at apify.com/terms govern
retention.

Briefs are saved as local markdown files in your configured output folder.
If email delivery is enabled, the brief content is sent through your
connected email provider.

---

## Updating and uninstalling

### To update

Replace the plugin folder with the new version and re-run:

```
claude plugin install .
```

Your `config/config.json` is not touched by reinstallation; you keep your
settings.

### To uninstall

```
claude plugin uninstall meeting-intel
```

This removes the plugin from Cowork. Your `config/config.json` and any
saved briefs in your output folder are not deleted automatically.
