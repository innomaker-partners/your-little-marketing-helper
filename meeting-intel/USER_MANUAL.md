# Meeting Intel - Umbrella User Manual

This manual covers what is shared across all four versions of Meeting Intel: how the pipeline works conceptually, the configuration fields you will set regardless of which version you use, how to read the brief it produces, what each version costs, and what data leaves your machine. For step-by-step installation and version-specific troubleshooting, open the `USER_MANUAL.md` inside the folder for your version.

---

## Contents

1. [How the pipeline works](#how-the-pipeline-works)
2. [Configuration concepts](#configuration-concepts)
3. [How to read a brief](#how-to-read-a-brief)
4. [Cost expectations](#cost-expectations)
5. [Privacy](#privacy)
6. [Troubleshooting index](#troubleshooting-index)

---

## How the pipeline works

All four versions run the same logical pipeline, even though the underlying tools differ. Understanding the stages helps you interpret both what the brief contains and what to check when something goes wrong.

### Stage 1: Calendar fetch

The pipeline reads today's calendar events from your connected calendar. It retrieves event titles, start times, attendee names, and attendee email addresses. No data is written back to your calendar at any point.

- In `claude_code`, this happens via an MCP connector you configure for Google Calendar or Outlook.
- In `cowork`, this happens via Cowork's native connector, which you authorize once in Settings.
- In `make_google`, this happens via a Make.com Google Calendar module using Google's Calendar API.
- In `make_outlook`, this happens via a Make.com Microsoft Calendar module using Microsoft's API.

### Stage 2: External participant extraction

Each attendee's email domain is compared against your configured `user_domains` list. Attendees on your own domains are your internal colleagues and are filtered out. Attendees on any other domain are external participants and are queued for research.

Additional filters remove calendar service addresses, resource rooms, and addresses with no display name.

If no external participants are found across all of today's events, the pipeline stops cleanly. No error, no brief, no output.

### Stage 3: Research

For each external participant, the pipeline researches two things: the person and their company.

**Company research:** The pipeline fetches the company's public website and extracts what the company does, its size, location, key products or services, and industry position. It supplements this with web search when the website is unavailable or sparse.

**Person research:** All four versions share the same two-step LinkedIn pipeline, first proven in the Make.com versions: an Apify people-search actor returns up to five candidate profiles for a fuzzy "name + company affiliation" query (kept at four words or more, because shorter queries are run as an exact phrase and find nothing), a second Apify actor scrapes the candidates' full public profiles, and the pipeline then validates which candidate (if any) is the participant, treating the company as a fuzzy affiliation that may sit in their current role, past roles, education, or about text. The result carries a confidence grade (HIGH, MEDIUM, or LOW_CONFIDENCE_MATCH) and a note saying where the affiliation matched.

Each fact is tagged with a confidence level as it is gathered. Nothing is inferred without a signal, and nothing is stated as verified unless a primary source returned it directly.

### Stage 4: Brief synthesis

Research findings for all participants in a meeting are combined into a structured brief. The brief archetype you configure shapes the emphasis: which facts are called out, what angle the conversation starters take, and how the summary connects to your context.

### Stage 5: Delivery

- `claude_code` writes one Markdown file per meeting to the path you configure and, if enabled, sends it via Gmail through the MCP connector.
- `cowork` writes Markdown files and, if a delivery connector is active, sends via Gmail, Microsoft 365 email, Slack, or Teams.
- `make_google` and `make_outlook` consolidate all meeting briefs into a single HTML email and send it to the address you configure.

---

## Configuration concepts

All four versions share the same core configuration concepts. Where each setting lives differs by version: plugin versions store configuration in `config/config.json` and set it through a first-run wizard; Make.com versions store configuration inside specific modules and set it by editing field values.

### user_domains

**What it is:** A list of your organization's email domains.

**Why it matters:** This is the single most important setting. Every attendee whose email domain appears in this list is treated as an internal colleague and skipped. Every attendee on any other domain is treated as an external participant and researched. An incorrect or incomplete list causes colleagues to be researched (wasted effort) or genuine external contacts to be skipped (missed briefs).

If your organization uses more than one domain, list them all. Do not include the @ symbol. Example: `acme.com,acme.io`.

### brief_archetype

**What it is:** A single keyword that sets the analytical frame of the brief.

**The four options and what each produces:**

| Archetype | What the brief emphasizes |
|---|---|
| `sales` | Fit assessment, pain points relative to your offering, buying signals, objection risks, deal openers |
| `consulting` | Engagement signals, likely challenges, budget indicators, decision-making structure, positioning angles |
| `investor` | Growth metrics, team assessment, market position, risk factors, due-diligence flags |
| `general` | Neutral intelligence with no product or service angle; suitable for networking or partnerships |

The underlying research (company website, LinkedIn profile) is identical regardless of archetype. The archetype changes only what the brief calls out and how the conversation starters are framed.

### user_context

**What it is:** One or two sentences describing what you do and what you care about in meetings.

**Why it matters:** The brief uses this to personalize conversation starters beyond generic openers. A starter that connects what you found about a person to what you are actually trying to accomplish in the meeting is more useful than one drawn from research alone.

Keep it specific. "I advise mid-market manufacturing companies on operational transformation" gives the brief enough signal to work with. "I do consulting" does not.

### max_participants_per_meeting

**What it is:** The maximum number of external participants researched per meeting.

**Why it matters:** Meetings occasionally have many external attendees. Researching all of them extends run time and, in the Make.com versions, increases Apify usage. The cap applies per meeting, not across all meetings in a day.

When a meeting exceeds the cap, participants are prioritized by seniority signal: titles such as CEO, CFO, VP, Director, Founder, and Partner are researched first. Remaining attendees are skipped, and the brief notes how many were not researched.

Default values differ between versions: the plugin versions (`claude_code`, `cowork`) default to 3; the Make.com versions (`make_google`, `make_outlook`) default to 5.

### Output settings

**markdown_path** (plugin versions): The directory where brief files are saved. Default is `~/meeting-briefs`. Briefs are named `YYYY-MM-DD-<meeting-title-slugified>.md`.

**email_enabled / email_to** (plugin versions): Set `email_enabled` to `true` and provide a destination address to also deliver each brief by email. This requires an active Gmail MCP connector (`claude_code`) or an active Gmail or Microsoft 365 email connector (`cowork`).

In the Make.com versions, email delivery is always on. The recipient address is set in module 24 before the first run.

---

## How to read a brief

The brief structure is the same across all versions. Plugin versions produce Markdown files; Make.com versions produce HTML emails. The content, sections, and confidence system are identical.

### Structure

```
# Meeting Brief: [Meeting Title]
[Date] . [Time] . [N participants researched]

## At a Glance
- [Name]: [Role] at [Company] - [single most useful fact]
- [One sentence connecting this meeting to your context]

## [Participant Name] - [Title/Role]

### Person
- Current role and tenure
- Professional background (2-3 sentences)
- Confidence tags on each fact

### Company: [Company Name]
- What they do
- Size, location, markets
- Key products and services
- Confidence tags on each field

### Conversation Starters
- [3 specific openers based only on actual research findings]
- [1-2 openers connecting their context to yours, when archetype is set]

---
Research depth: [FULL / LIMITED / MINIMAL]
Research conducted [date and time]. Sources: [URLs visited].
Confidence key: [key]
```

### Confidence tags

Each fact in the brief carries one of four tags. These appear inline next to the fact they describe.

| Tag | Meaning |
|---|---|
| checkmark (✓) | Confirmed directly on the company's own website or another authoritative primary source |
| ~ inferred | Reasonably derived from context; the basis for the inference is noted |
| hourglass (⏳) | From a LinkedIn profile, which is self-reported and may not reflect the person's current situation |
| ? not found | No source returned this information; the field is left blank rather than guessed |

LinkedIn data always carries the hourglass tag. It is never marked as verified because the source is self-reported and not independently dated.

Conversation starters are derived only from what research actually found. If nothing specific enough for a useful opener was available, the brief says so explicitly rather than producing a generic one.

### Research depth

The research depth label in the brief footer tells you how much primary-source material was confirmed.

| Label | What it means |
|---|---|
| FULL | Company website confirmed and LinkedIn profile found and validated |
| LIMITED | One of those two primary sources was missing or unreachable |
| MINIMAL | Research based on web search snippets only; no primary source confirmed |

When you see MINIMAL, treat the conversation starters with additional caution. They are based on limited data.

---

## Cost expectations

### claude_code

Requires a Claude Pro subscription and an Apify account. The Claude Code README documents an estimated ongoing cost of approximately $50-70/month once free trials end. Apify offers a 30-day free trial; the Starter plan is approximately $49/month after that. Claude's web search (used for company research and as a LinkedIn fallback) is included in the Claude subscription.

### cowork

Requires a Cowork subscription plus an Apify account for the LinkedIn research actors (connected with one token paste in Cowork's Settings; Apify offers a free trial). Company research runs through Claude's built-in web access, which is included in your plan. No OpenAI account is needed.

### make_google and make_outlook

Three separate cost sources apply: Make.com operations consumed per scenario run, Apify usage from the LinkedIn search and profile scraper actors, and OpenAI API calls for participant extraction, analysis, and brief synthesis. The Make.com free tier covers testing; regular daily use requires a paid plan. The make_google User Manual notes that a typical single-meeting brief costs a small amount from OpenAI (a few cents). Apify's Starter plan covers typical usage. Total ongoing cost depends on how many meetings per day include external participants and how many participants are in each.

---

## Privacy

### claude_code

When you run the plugin, participant names and company names are sent to Apify as search queries. Company domain URLs are fetched via public web requests. Web search queries are sent to a search engine. Brief files are saved locally to your configured output path. If email delivery is enabled, each brief is transmitted through your Gmail MCP connector to the configured recipient. No calendar data is stored by the plugin between runs.

### cowork

When you run the plugin, participant names and company name guesses are sent to the Apify LinkedIn actors as search queries, and matched candidates' public profile URLs go to the Apify profile scraper. Company research queries go through Cowork's standard web access. Calendar contents themselves (titles, descriptions) are not sent to Apify; Apify stores actor run inputs and outputs under your account, governed by their terms. Briefs are saved as local Markdown files. If email delivery is enabled, brief content is sent through your connected email provider.

### make_google

Meeting titles, meeting descriptions, and attendee lists from your Google Calendar are sent to OpenAI for participant extraction. Company email domains and company name guesses are sent to Apify's website crawler and Google Maps search actors. LinkedIn profile URLs are sent to the Apify profile scraper. Company website content and LinkedIn profile data are sent to OpenAI for analysis and brief synthesis. The brief is emailed via your Gmail connection. Apify stores run inputs and outputs; their privacy policy at apify.com/terms governs retention. OpenAI's data usage policies apply to what you send in module prompts.

### make_outlook

Same as `make_google`, except calendar data comes from your Outlook 365 calendar (via Microsoft's API) and the brief is delivered via your Microsoft 365 email connection. The scenario reads event subjects, body previews, start times, and attendee lists. No data is written back to your calendar or mailbox beyond sending the outbound email.

---

## Troubleshooting index

For step-by-step diagnosis, open the `USER_MANUAL.md` in the folder for your version. The highest-impact known issues for each version are listed below so you can identify the right place to look.

### claude_code - `claude_code/USER_MANUAL.md`

**MCP servers not loading in the Mac app.** Servers configured in `~/.claude/mcp.json` may not load in the Claude Code Mac app. The fix is to move your MCP server entries to `~/.claude.json` under a top-level `mcpServers` key. Full steps are in the "MCP servers not loading (Mac app)" section of the User Manual.

**LinkedIn research returns zero results (query too short).** The two-step actor pipeline's search runs queries of 3 words or fewer as one exact quoted phrase, which almost never matches a name-plus-company combination; queries of 4 or more words run as a fuzzy search, which is what works. The User Manual's "LinkedIn research returns zero results" section covers query construction, the honest "no LinkedIn match" outcome, and the schema trap to check before substituting a different actor.

**Empty or truncated Apify token produces misleading errors.** If Apify calls fail with what looks like a permission error, confirm the token loaded correctly with the length check described in "Credentials setup."

### cowork - `cowork/USER_MANUAL.md`

**Plugin seems installed but no command fires.** This is caused by a `plugin.json` that is missing the `skills` array. Without it, Cowork registers the plugin but loads no skill, so neither the slash command nor natural language triggers work. The shipped `plugin.json` includes both a `$schema` field and a `skills` array. If you have forked or edited the file, verify those keys are present.

**Calendar connector appears connected but the plugin asks for meeting details manually.** A connector that completed its OAuth flow without full authorization can appear active in Settings while failing silently at runtime. The fix is to remove the calendar connector in Settings and re-authorize it from scratch.

**LinkedIn search finds nothing or wrong person.** The plugin researches LinkedIn through the same two-step Apify actor pipeline as the other versions, via the Apify connector. If nothing is found, check the connector first, then the query: overly specific queries (university names, department suffixes) often return nothing while a shorter one finds the person. A genuine miss is reported as "no LinkedIn match" and the brief's depth drops to LIMITED or MINIMAL.

### make_google - `make_google/USER_MANUAL.md`

**403 error on every run (missing calendar scope).** The Google Calendar "Make an API Call" module type does not request calendar permissions on its own. If the scope `https://www.googleapis.com/auth/calendar` was not added in the advanced settings before signing in, Google created the connection without calendar access. The fix requires deleting the connection and creating a new one with the scope added before sign-in. The User Manual's "403 error" section covers the exact steps. This is the single most common setup mistake with this variant.

**Gmail connection fails even though Google Calendar connection works.** Gmail and Google Calendar are separate connection types in Make.com. A Google Calendar connection cannot be used for the Gmail module, even when it is the same Google account. Module 24 requires a dedicated Gmail connection.

**No email arrives after a successful run.** The brief only sends when research content is non-empty. If all meetings today are internal, the participant extraction step returns empty, and the downstream filters block processing. Confirm that `user_domains` in module 2 is set to your own organization's domains only.

### make_outlook - `make_outlook/USER_MANUAL.md`

**No email arrives after a successful run.** Same behavior as in `make_google`: all-internal meeting days produce no output by design. Check that `user_domains` in module 2 is correctly set.

**Stale Make.com editor tab overwrites your settings.** The Make.com editor does not live-reload. If you configure settings in one tab while another tab is open, or if the page was left open while changes were saved, reloading from the stale tab will overwrite your work. Always reload the scenario tab before editing or running.

**Expired Microsoft calendar connection surfaces only at run time.** Make.com accepts the Microsoft OAuth connection during setup without validating the token. A token expiry or revocation only appears as a failure when you click Run once. The fix is to reauthorize the connection in Make's Connections panel.
