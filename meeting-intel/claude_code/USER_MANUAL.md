# Meeting Intel -- User Manual (Claude Code Plugin)

## Contents

1. [Installation](#installation)
2. [Credentials setup](#credentials-setup)
3. [First-run configuration](#first-run-configuration)
4. [Day-to-day use](#day-to-day-use)
5. [Reading the brief](#reading-the-brief)
6. [Customization](#customization)
7. [Troubleshooting](#troubleshooting)
8. [Developer note](#developer-note)
9. [Privacy](#privacy)
10. [Updating and uninstalling](#updating-and-uninstalling)

---

## Installation

### Step 1: Download this folder

Place this plugin directory somewhere permanent on your machine. Claude Code records this path as the
source of the plugin and reads it when checking for updates, so moving the folder later means
reinstalling from the new location.

### Step 2: Install the plugin

This directory is a self-contained Claude Code marketplace: it carries its own marketplace manifest,
so you register the folder as a marketplace and then install the plugin from it. Run both commands,
substituting the absolute path to this directory:

```
claude plugin marketplace add <absolute-path-to-this-directory>
claude plugin install meeting-intel@meeting-intel-local
```

Confirm it was registered:

```
claude plugin list
```

You should see `meeting-intel` in the output at user scope. Restart Claude Code so the plugin loads
into your session.

If `claude plugin validate` fails with an error about a missing `skills/` directory, see [Plugin validation fails after cloning](#plugin-validation-fails-after-cloning) in Troubleshooting.

### Step 3: Configure MCP connectors

The plugin reads your calendar and runs LinkedIn research through MCP servers. You need at least two:

- **Google Calendar** or **Microsoft Outlook** -- for reading today's meetings
- **Apify** -- for LinkedIn profile research

Optionally:
- **Gmail** -- if you want briefs delivered by email in addition to saved as files

Configure these in Claude Code's MCP settings. See [docs/SETUP.md](docs/SETUP.md) for detailed connector instructions. If you use the Claude Code Mac app, also see [MCP servers not loading (Mac app)](#mcp-servers-not-loading-mac-app) in Troubleshooting.

---

## Credentials setup

The Apify MCP server requires your Apify API token. The recommended way to supply it is via a `.env` file sourced into your shell session.

**Recommended: source the file**

```bash
set -a; . .env; set +a
```

This exports every variable in `.env` without word-splitting. The alternative -- `export $(grep -v '^#' .env | xargs)` -- silently truncates values that contain spaces. When that happens, an emptiness guard still passes while the actual token value is wrong, and the resulting error looks like a server-side permission failure rather than a credential problem. Sourcing with `set -a` avoids this class of issue entirely.

**Validate the token before first use**

```bash
echo "APIFY_TOKEN length: ${#APIFY_TOKEN}"
```

If the length is 0, the token was not loaded. Source your `.env` file and check again before running the plugin.

---

## First-run configuration

The first time you run `/meeting-intel:meeting-intel` without a `config/config.json` present, the plugin walks you through a setup flow rather than starting research. Answer each question as prompted; the plugin writes `config.json` when done.

**Question 1 -- Your company email domain**

> "What is your company email domain? (e.g., acme.com)"

Enter your organization's email domain. The plugin uses this to identify which meeting attendees are your internal colleagues and which are external participants worth researching. If your organization uses more than one domain, enter them as a comma-separated list.

Stored as: `user_domains` (array of strings)

**Question 2 -- How you use meetings**

> "What best describes how you use meetings?"

Choose one:
- **Sales** -- you meet prospects and clients to sell products or services
- **Consulting** -- you meet potential or current consulting clients
- **Investor** -- you meet founders, startups, or portfolio companies
- **General** -- networking, partnerships, or mixed purposes

This setting determines the analytical frame of the brief's conversation starters and at-a-glance summary. The underlying research is the same regardless of choice.

Stored as: `brief_archetype`

**Question 3 -- What you do and care about**

> "Briefly describe what you do and what you care about in meetings."

The plugin shows an example for your chosen archetype. Write one or two sentences in the same style. This context shapes how the brief frames research findings relative to your specific goals -- for example, what signals to surface in conversation starters.

Stored as: `user_context`

**Question 4 -- Where to save briefs**

> "Where should I save briefs? (default: ~/meeting-briefs)"

Enter a path or press Enter to accept the default. The plugin writes one Markdown file per meeting to this location.

Stored as: `output.markdown_path`

**After setup**

The plugin writes `config/config.json` and confirms: "Config saved. Run /meeting-intel:meeting-intel again to generate your first briefs." The config file persists across all future runs. The setup flow does not repeat unless you delete the file.

Default values written automatically: `email_enabled: false`, `max_participants_per_meeting: 3`.

---

## Day-to-day use

Inside a Claude Code session, run:

```
/meeting-intel:meeting-intel
```

The plugin:

1. Reads your `config/config.json` to load your domains, archetype, and preferences
2. Pulls today's calendar events via your Google Calendar or Outlook MCP connector
3. Identifies external participants -- attendees whose email domains are not in your `user_domains`
4. Researches each participant: company website, public web searches, and LinkedIn via Apify
5. Writes one brief per meeting to your configured output path

If there are no external meetings today, the plugin stops silently without writing any files. If the calendar MCP is unreachable, it writes a diagnostic note to the output path and stops.

---

## Reading the brief

Each brief is a Markdown file named `YYYY-MM-DD-<meeting-title-slugified>.md` and follows this structure:

```
# Meeting Brief: [Meeting Title]
[Date] . [Time] . [Number of participants researched]

## At a Glance
- [Name]: [Role] at [Company] -- [single most useful fact]
- [If angled archetype: one sentence connecting this meeting to your context]

## [Participant Name] -- [Title/Role]

### Person
- Current role and tenure (if available)
- LinkedIn headline
- Professional background (2-3 sentences)
- Confidence tags on each fact

### Company: [Company Name]
- What they do (one sentence)
- Size, location, markets
- Key products/services
- Industry position and notable facts
- Confidence tags on each field

### Conversation Starters
- [3 specific, non-generic openers grounded in actual research]
- [If angled archetype: 1-2 openers connecting their context to yours]

---
Research depth: [FULL / LIMITED / MINIMAL]
Research conducted [timestamp]. Sources: [URLs actually visited].
Confidence key: ...
```

**Research depth** tells you how much primary-source material was actually found:

- **FULL** -- company website confirmed and LinkedIn profile found and validated
- **LIMITED** -- one source is missing (website unreachable or LinkedIn profile not found)
- **MINIMAL** -- research based on web search snippets only; no primary source confirmed; treat all facts with additional skepticism

**Confidence tags on individual facts:**

- **✓ verified on source** -- confirmed directly on the company's website or another authoritative source
- **~ inferred** -- derived logically from available data, not directly confirmed anywhere
- **⏳ verified but possibly stale** -- sourced from a LinkedIn profile, which is self-reported and may not reflect the person's current situation
- **? not found** -- no source returned this information; the field is left blank rather than guessed

Conversation starters are derived only from what research actually found. If nothing specific enough for a real opener was available, the brief says so rather than fabricating one.

---

## Customization

Open `config/config.json` in any text editor. The available fields are:

| Field | Type | Description |
|-------|------|-------------|
| `user_domains` | array of strings | Internal email domains; attendees at these domains are filtered out and not researched |
| `brief_archetype` | string | One of: `sales`, `consulting`, `investor`, `general` |
| `user_context` | string | Your description of what you do and what matters to you in meetings; must not be empty or the example placeholder |
| `max_participants_per_meeting` | integer | Maximum external participants to research per meeting (default: 3) |
| `output.markdown_path` | string | Directory where brief files are saved |
| `output.email_enabled` | boolean | Set to `true` to also send each brief by email via Gmail MCP |
| `output.email_to` | string | Destination address for brief delivery; required when `email_enabled` is `true` |

When a meeting has more external participants than `max_participants_per_meeting` allows, the plugin prioritizes by seniority signal (titles such as CEO, VP, Director, Founder, and Partner) before capping the list.

---

## Troubleshooting

### MCP servers not loading (Mac app)

**Symptom:** The plugin reports that calendar or Apify tools are unavailable, even though you have configured MCP servers.

**Cause:** MCP servers defined in `~/.claude/mcp.json` may not be loaded by the Claude Code Mac app. Servers defined in `~/.claude.json` under a top-level `mcpServers` key load reliably.

**Fix:** Move your MCP server entries to `~/.claude.json`:

```json
{
  "mcpServers": {
    "apify": { ... },
    "google-calendar": { ... }
  }
}
```

Restart Claude Code after editing the file.

### Plugin validation fails after cloning

**Symptom:** `claude plugin validate` fails with an error about a missing `skills/` directory immediately after cloning or copying the folder.

**Cause:** Git does not track empty directories. If the `skills/meeting-intel/` directory was empty at some point in history, or if a copy operation did not preserve the directory structure, the path may be absent.

**Fix:** Create the directory and confirm `SKILL.md` is present:

```bash
mkdir -p skills/meeting-intel
ls skills/meeting-intel/SKILL.md
```

If `SKILL.md` is missing, copy it from the distributed folder or re-download the plugin. Then re-run validate.

### Empty or truncated Apify token produces misleading errors

**Symptom:** Apify calls fail with what looks like a server-side permission error (401 or similar), but you believe the token is configured.

**Cause:** An empty or whitespace-only token value causes the Apify MCP server to send an unauthenticated request. The resulting error message points at the server rather than the missing credential, so the problem is easy to misread as a configuration or account issue.

**Fix:** Confirm the token is non-empty:

```bash
echo "APIFY_TOKEN length: ${#APIFY_TOKEN}"
```

If the length is 0, source your `.env` using `set -a; . .env; set +a` (see [Credentials setup](#credentials-setup)) and check again. Do not use `export $(grep ... | xargs)` -- that method silently truncates values containing spaces, and the resulting token appears set while being wrong.

### LinkedIn research returns zero results

**Symptom:** The Apify LinkedIn actor runs but returns no profiles for a participant you expect to find.

**How the research works:** LinkedIn research is a two-step actor pipeline, mirroring the Make.com versions of this tool: a people search actor (memo23/linkedin-people-search) returns up to five candidate profile URLs, then a profile scraper actor (harvestapi/linkedin-profile-scraper) pulls their full profiles, and the skill validates which candidate (if any) is your participant.

**Cause (two common forms):**

1. **Query too short.** The people-search actor runs a keywords string of 3 words or fewer as one exact quoted phrase, which matches a person only if their profile contains those words adjacently -- in practice, "Jane Doe Acme" returns nothing. Keywords of 4 or more words run as an unquoted fuzzy search, which is what works: "Jane Doe Acme Corporation" or "Jane Doe State University" finds the person. The skill builds its query from the name plus a multi-word affiliation guess and retries once with an alternative affiliation phrasing; if you edit the search behavior, keep the keywords at 4 words or more.

2. **Wrong input field for a substitute actor.** LinkedIn actors on Apify have widely differing input schemas, and actors silently ignore input fields they do not recognize, returning zero results with no error message. If you swap in a different actor, check its input schema in the Apify console and test your input there before wiring it into the skill.

**Fix:** Keep the keywords at 4 words or more (name plus a multi-word affiliation guess); when the search truly finds nothing after the one retry, the skill reports "no LinkedIn match" honestly and the web-search fallback covers actor outages; for substitute actors, verify the real input schema in the Apify console first.

### Calendar MCP returns no events or plugin stops silently

**Symptom:** The plugin exits without writing any files, and you have external meetings today.

**Likely causes:**

- The calendar MCP connector has an expired or missing OAuth token. Reauthorize it in your MCP server configuration.
- The MCP server is defined in `~/.claude/mcp.json` and not loading in the Mac app. Move it to `~/.claude.json` -- see [MCP servers not loading (Mac app)](#mcp-servers-not-loading-mac-app).
- All meetings today are internal: every attendee is at a domain listed in `user_domains`. The plugin produces no output in this case by design.

---

## Developer note

To run the test suite for `scripts/parse_calendar.py`:

```bash
uv run --with pytest -- python3 -m pytest tests/
```

The system Python installation typically does not include pytest. The `uv` invocation above installs pytest in an isolated environment without modifying your system packages.

---

## Privacy

When you run `/meeting-intel:meeting-intel`, the following data leaves your machine:

- **Participant names and company names** -- sent to Apify as search queries for LinkedIn profile research; the matched candidates' public profile URLs are then sent to the Apify profile scraper
- **Company domain URLs** -- fetched directly via web requests to retrieve company information from public websites
- **Web search queries** -- sent to a search engine when company websites are unreachable or return insufficient content

The briefs themselves are saved locally to `output.markdown_path`. If `email_enabled` is `true`, each brief is also transmitted through your Gmail MCP connector to the address in `email_to`.

No calendar data is stored by the plugin between runs. LinkedIn research runs through Apify's infrastructure independently of your personal LinkedIn account. Apify's data sourcing and retention practices are governed by their Terms of Service at [apify.com/terms](https://apify.com/terms); review those terms before use if data sourcing is a concern for your context (regulated industries, enterprise compliance, or similar).

---

## Updating and uninstalling

**To update:** Replace the contents of this directory with the new version, then refresh the marketplace and the plugin, and restart Claude Code:

```
claude plugin marketplace update meeting-intel-local
claude plugin update meeting-intel
```

Your `config/config.json` is preserved as long as you do not overwrite it.

**To uninstall:**

```
claude plugin remove meeting-intel
```

To also forget the local marketplace, run `claude plugin marketplace remove meeting-intel-local`.

This removes the plugin registration from Claude Code. Your `config/config.json` and any briefs written to `output.markdown_path` remain on disk; delete them manually if you want to remove all traces.
