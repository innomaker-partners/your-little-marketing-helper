---
name: meeting-intel
description: Research external meeting participants and produce intelligence briefs
---

# Meeting Intel

Research today's external meeting participants and produce intelligence
briefs with company and person profiles, confidence tagging, and
conversation starters.

**This plugin's own bundled files are addressed from `${CLAUDE_PLUGIN_ROOT}`.** The helper script
this skill runs (`scripts/parse_calendar.py`) ships inside the plugin. `${CLAUDE_PLUGIN_ROOT}` is the
absolute path Claude Code sets to this plugin's install location; prepend it when you invoke the
script, because the plugin directory is not the user's working directory and a bare relative path
will not resolve. Your `config/config.json` is different: it is your own file in your working
directory, so it stays a plain relative path.

## Prerequisites

Before first run, ensure:
1. Google Calendar MCP connector is active (or Outlook Calendar MCP)
2. Apify MCP connector is active (for LinkedIn research)
3. Gmail MCP connector is active (optional, for email delivery)
4. `config.json` exists in the plugin's `config/` directory

## Flow

### 1. Load configuration

Read `config/config.json` from this plugin's directory. If the file does
not exist, run the FIRST-RUN SETUP below instead of failing.

Required fields:
- `user_domains` (array of strings)
- `brief_archetype` (one of: sales, consulting, investor, general)
- `user_context` (string, must not be empty or the example placeholder)

### 2. Pull today's calendar

Use the Google Calendar MCP `list_events` tool. Filter to today's date.
If Outlook MCP is available instead, use its equivalent.

If the calendar MCP is unavailable: write a single file to the output
path: "Calendar could not be reached. Check your MCP connector setup."
Stop here.

### 3. Extract external participants

Run the helper script on the calendar data:

```bash
echo '<events_json>' | python ${CLAUDE_PLUGIN_ROOT}/scripts/parse_calendar.py \
  --user-domains <comma_separated_domains> \
  --max-per-meeting <config_value>
```

If the result is an empty array, there are no external meetings today.
Produce no output. Stop here silently.

If the script itself fails to run (python3 missing, syntax error, bad
input), report the error to the user and stop. Do NOT re-implement the
filtering by hand: the filtering rules live in this script and its test
suite, and an inline re-implementation silently loses them.

### 4. Research each participant

For each participant in the script's output, research in this order:

**Company research:**
- If `company_domain` is not empty (corporate email):
  1. WebFetch `https://<company_domain>` (homepage)
  2. WebFetch `https://<company_domain>/about` (about page)
  3. If both return less than 200 characters of visible text, or contain
     Cloudflare challenge / GDPR overlay / login wall signatures, mark as
     unreachable and fall back to WebSearch "<company_domain> company"
- If `company_domain` is empty (personal email):
  1. WebSearch "<participant_name>" to find their company
  2. If a company is identified, WebFetch that company's website

Extract these B2B fields with confidence tags:
- Company name [verified/inferred/not_found]
- What they do (one sentence) [verified/inferred]
- HQ location [verified/inferred/not_found]
- Markets served [verified/inferred/not_found]
- Employee count [verified/inferred/not_found]
- Industry [verified/inferred]
- Key products/services [verified/inferred/not_found]

**LinkedIn research (two-step actor pipeline):**

This mirrors, step for step, the pipeline proven in the Make.com
version of this tool.

Step 1 - candidate search. Use the Apify MCP to run actor
`xfz4tG0OB6ClIGVGv` (memo23/linkedin-people-search) with input:
  ```json
  {
    "mode": "public",
    "keywords": "<participant_name> <company_affiliation_guess>",
    "maxResults": 5
  }
  ```
  The keywords string MUST be at least 4 words long. The actor runs
  keywords of 3 words or fewer as a single exact quoted phrase, which
  finds a person only if their profile contains those words adjacently
  (and is sensitive to accented characters) - in practice it returns
  zero results for name-plus-company queries. Keywords of 4 or more
  words run as an unquoted fuzzy search, which is what works. So use a
  multi-word affiliation guess: "Jane Doe State University", not "Jane Doe
  State"; "John Smith Acme Corporation", not "John Smith Acme". If
  the name plus affiliation is still under 4 words, lengthen the
  affiliation (e.g. add "University", "Ltd", or the industry word from
  the company research) - never pad with unrelated words.

  If the search returns zero candidates, retry ONCE with an
  alternative phrasing of the affiliation (e.g. the company's full
  legal name from the company research, or "University of X" instead
  of "X University"). If the retry also returns zero, that is the
  final answer for this step.

  Collect the `profileUrl` of every candidate returned. Do not
  substitute candidates from any other source (web search, memory):
  only URLs returned by this actor proceed to step 2.

Step 2 - full profile scrape. If step 1 returned at least one
candidate, run actor `LpVuK3Zozwuipa5bp`
(harvestapi/linkedin-profile-scraper) with input:
  ```json
  {
    "queries": ["<profileUrl 1>", "<profileUrl 2>", "..."],
    "profileScraperMode": "Profile details no email ($4 per 1k)"
  }
  ```
  From each returned profile read ONLY these fields and ignore
  everything else (the full payload is large, and the extra fields are
  noise that degrades the validation step): firstName, lastName,
  headline, linkedinUrl, publicIdentifier, location, about,
  currentPosition, experience, education, connectionsCount,
  followerCount, topSkills.

Step 3 - validate the candidates against the participant. The
participant's company is a fuzzy affiliation guessed from calendar and
email data. It is NOT necessarily their current employer: it may be a
university, a past employer, or a side affiliation.
- A candidate matches if their name aligns with the participant's name
  AND the company aligns with ANY affiliation in their profile:
  current company (currentPosition), past employers (experience),
  education, or the about text.
- Grade confidence by where the company matched: current company =
  HIGH; education, past employer, or about = MEDIUM; name matches but
  no company alignment anywhere = LOW_CONFIDENCE_MATCH with an
  explanation.
- Always state WHERE the company affiliation matched (e.g.
  "education: Example University").
- If several candidates match equally well on both name and
  affiliation, report the ambiguity rather than guessing.
- If step 1 returned no candidates, or no candidate matches, record
  "no LinkedIn match" for this participant and move on.
- Never fabricate. If a field is missing, write "not available".
- Record for the brief: linkedinUrl, confidence grade, the matched-on
  field, headline, current role, network counts, and 2-3 factual
  sentences from the profile.

- If Apify MCP is unavailable or an actor call fails: use WebSearch
  "site:linkedin.com <participant_name> <company_name>" as fallback.
  Note in the brief: "LinkedIn profile could not be retrieved." Also
  tell the user directly that the Apify MCP appears unavailable and
  point them to the "MCP servers not loading (Mac app)" and
  "Credentials setup" sections of USER_MANUAL.md - both failure modes
  look like server errors but are fixable local configuration.

**Anti-bot detection (WebFetch):**
Check every WebFetch response for:
- Total visible text under ~200 characters
- Strings: "checking your browser", "cloudflare", "ray id",
  "enable javascript", "cookie consent", "access denied"
If detected, treat as unreachable. Fall back to WebSearch.

### 5. Synthesize brief

Write one brief per meeting, grouping all researched participants.

Use this template structure:

```
# Meeting Brief: [Meeting Title]
[Date] . [Time] . [N participants researched]

## At a Glance
- [Name]: [Role] at [Company] -- [single most useful fact]
- [If angled mode: one sentence connecting this meeting to user's context]

## [Participant Name] -- [Title/Role]

### Person
- Current role and tenure (if available)
- LinkedIn headline
- Professional background (2-3 sentences)
- Confidence: [which facts verified vs. inferred]

### Company: [Company Name]
- What they do (one sentence)
- Size, location, markets
- Key products/services
- Industry position / notable facts
- Confidence tags on each field

### Conversation Starters
- [3 specific, non-generic openers based on actual research findings]
- [If angled: 1-2 openers connecting their world to the user's context]

[Repeat for each participant]

---
Research depth: [FULL / LIMITED / MINIMAL]
Research conducted [timestamp]. Sources: [URLs actually visited].
Confidence key: ✓ verified on source . ~ inferred . ⏳ verified but possibly stale . ? not found
```

**Research depth indicator:**
- FULL: company website crawled + LinkedIn profile found and validated
- LIMITED: one source missing
- MINIMAL: research based on web search snippets only

**Archetype framing:** The `brief_archetype` from config determines the
analytical frame:
- `sales`: fit assessment, pain points relative to user's offering,
  buying signals, objection risks, deal openers
- `consulting`: engagement signals, likely challenges, budget indicators,
  decision-making structure, positioning angles
- `investor`: growth metrics, team assessment, market position, risk
  factors, due-diligence flags
- `general`: neutral intelligence, no product/service angle

The `user_context` provides specifics within the archetype.

**Rules:**
- Conversation starters are derived ONLY from what the research actually
  found (website + LinkedIn). Never fabricate.
- If nothing specific enough for a real opener was found, say:
  "No specific conversation starters could be derived from available
  sources."
- LinkedIn data is tagged with ⏳ (possibly stale), not ✓,
  because profiles are self-reported and frequently outdated.
- When LinkedIn says person works at Company X but the company website
  does not list them, surface the discrepancy.
- Source URLs are listed at the bottom. Every URL cited was actually
  visited.

### 6. Deliver

Save each brief as a markdown file:
`<output.markdown_path>/YYYY-MM-DD-<meeting_title_slugified>.md`

If `output.email_enabled` is true and Gmail MCP is available:
- Send each brief by email to `output.email_to`
- If Gmail MCP is unavailable, append a note to the file:
  "Email delivery failed -- brief saved to file only."

### FIRST-RUN SETUP

If `config.json` does not exist, run this interactive flow exactly once:

1. Ask: "What is your company email domain? (e.g., acme.com)"
   -> Store as `user_domains` array

2. Ask: "What best describes how you use meetings?"
   Options:
   - Sales: I meet prospects and clients to sell products/services
   - Consulting: I meet potential or current consulting clients
   - Investor: I meet founders, startups, or portfolio companies
   - General: Networking, partnerships, or mixed purposes
   -> Store as `brief_archetype`

3. Ask: "Briefly describe what you do and what you care about in
   meetings."
   Show example for their archetype:
   - sales: "I sell marketing automation to mid-market SaaS companies.
     I care about whether they have an existing stack and budget."
   - consulting: "I consult on digital transformation for manufacturing.
     I care about organizational readiness and decision-maker access."
   - investor: "I invest in Series A B2B SaaS. I care about team
     quality, market size, and capital efficiency."
   - general: "I run partnerships at a media company. I care about
     mutual audience overlap."
   -> Store as `user_context`

4. Ask: "Where should I save briefs? (default: ~/meeting-briefs)"
   -> Store as `output.markdown_path`

5. Write `config.json` with the collected values. Set
   `output.email_enabled: false` (nested under the `output` object, next
   to `output.markdown_path`) and `max_participants_per_meeting: 3`.

6. Confirm: "Config saved. Run /meeting-intel:meeting-intel again to generate your
   first briefs."

---

## Maintenance note (not part of the runtime flow)

If you are asked to modify `scripts/parse_calendar.py`, the filtering
rules it must preserve are encoded in `tests/test_parse_calendar.py`.
Run them after any change:

```bash
uv run --with pytest -- python3 -m pytest tests/
```

Do not consider a modification finished while any test fails.

If you are asked to substitute a different Apify actor, verify its
input schema in the Apify console first: actors silently ignore input
fields they do not recognize and return zero results with no error.
