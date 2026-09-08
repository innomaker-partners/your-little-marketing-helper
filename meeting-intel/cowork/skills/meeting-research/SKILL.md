---
name: meeting-research
description: Research external meeting participants and produce intelligence briefs. Activates when the user asks to prepare for a meeting, research someone they're meeting, or asks about today's calendar with a prep intent. Works standalone with user input and web research, supercharged when a calendar connector is active. Trigger with "prep me for my meeting with [company]", "who am I meeting today", "research [name] at [company] before my call", or "/meeting-intel:prep".
argument-hint: <company or person name>
---

# Meeting Intel

Research external meeting participants and produce intelligence briefs
with company profiles, person backgrounds, confidence tags, and
conversation starters.

See [CONNECTORS.md](../../CONNECTORS.md) for supported integrations.

## Flow

### 1. Load configuration

Read `config/config.json` from this plugin's directory.

Required fields:
- `user_domains` -- array of email domains that belong to the user's
  organization (used to filter out internal attendees)
- `brief_archetype` -- one of: sales, consulting, investor, general
- `user_context` -- one sentence describing what the user does and cares
  about in meetings. Must not be empty or the example placeholder: if it
  still contains bracket placeholders like "[product/service]", treat
  the config as incomplete and run the FIRST-RUN SETUP.

If config.json does not exist, run the FIRST-RUN SETUP at the bottom of
this file.

### 2. Pull today's calendar

**With a calendar connector (Microsoft 365 or Google Calendar):**
List today's events. Extract each event's title, start time, and
attendee list (name + email).

**Without a calendar connector:**
Ask the user: "I don't have calendar access. Tell me who you're meeting:
their name, company, and optionally their email." Use whatever the user
provides.

### 3. Extract external participants

From the attendee list, filter out anyone whose email domain matches any
entry in `user_domains`. These are internal colleagues -- skip them.

Also filter out:
- The user's own email
- Calendar service addresses (e.g. domains containing "calendar",
  "resource", "room")
- Addresses with no real name attached

From each remaining attendee, extract:
- `participant_name` -- display name from the calendar entry
- `participant_email` -- email address
- `company_domain` -- the domain part of their email (everything after @)

If no external participants remain after filtering, report: "No external
meetings found today." Stop here.

Cap at `max_participants_per_meeting` from config (default 3) per
meeting. If more exist, research the first N and note how many were
skipped.

### 4. Research each participant

For each external participant, research in this order:

**Company research (WebFetch + WebSearch):**

If `company_domain` is not empty (corporate email):
1. WebFetch `https://<company_domain>` -- extract what the company does,
   size, markets, products, industry position
2. WebFetch `https://<company_domain>/about` -- extract founding story,
   leadership, investors, offices
3. If either page is unreachable (under 200 characters of visible text,
   Cloudflare challenge, login wall, GDPR overlay, "access denied"),
   fall back to WebSearch: `"<company_domain> company"`

If `company_domain` is empty (personal email like gmail.com):
1. WebSearch `"<participant_name>"` to identify their company
2. If a company is identified, WebFetch that company's website

Extract these fields with confidence tags:
- Company name [✓ verified / ~ inferred / ? not found]
- What they do (one sentence) [✓ / ~]
- Size (employees, customers) [✓ / ~ / ?]
- HQ location [✓ / ~ / ?]
- Markets served [✓ / ~ / ?]
- Key products/services [✓ / ~ / ?]
- Industry position / notable facts [✓ / ~ / ?]

**Person research (two-step Apify actor pipeline):**

This mirrors, step for step, the pipeline proven in the Make.com
version of this tool. It needs the Apify connector (see CONNECTORS.md).

Step 1 - candidate search. Run Apify actor `xfz4tG0OB6ClIGVGv`
(memo23/linkedin-people-search) with input:
`{"mode": "public", "keywords": "<participant_name>
<company_affiliation_guess>", "maxResults": 5}`. The keywords string
MUST be at least 4 words long. The actor runs keywords of 3 words or
fewer as a single exact quoted phrase, which finds a person only if
their profile contains those words adjacently (and is sensitive to
accented characters) - in practice it returns zero results for
name-plus-company queries. Keywords of 4 or more words run as an
unquoted fuzzy search, which is what works. So use a multi-word
affiliation guess: "Jane Doe State University", not "Jane Doe State";
"John Smith Acme Corporation", not "John Smith Acme". If the name plus
affiliation is still under 4 words, lengthen the affiliation (e.g. add
"University", "Ltd", or the industry word from the company research) -
never pad with unrelated words.

If the search returns zero candidates, retry ONCE with an alternative
phrasing of the affiliation (e.g. the company's full legal name from
the company research, or "University of X" instead of "X University").
If the retry also returns zero, that is the final answer for this
step.

Collect the `profileUrl` of every candidate returned. Do not
substitute candidates from any other source (web search, memory):
only URLs returned by this actor proceed to step 2.

Step 2 - full profile scrape. If step 1 returned at least one
candidate, run actor `LpVuK3Zozwuipa5bp`
(harvestapi/linkedin-profile-scraper) with input:
`{"queries": ["<profileUrl 1>", "..."], "profileScraperMode":
"Profile details no email ($4 per 1k)"}`. From each returned profile
read ONLY these fields and ignore everything else (the full payload is
large, and the extra fields are noise that degrades validation):
firstName, lastName, headline, linkedinUrl, publicIdentifier, location,
about, currentPosition, experience, education, connectionsCount,
followerCount, topSkills.

Step 3 - validate the candidates against the participant. The
participant's company is a fuzzy affiliation guessed from calendar and
email data. It is NOT necessarily their current employer: it may be a
university, a past employer, or a side affiliation.
- A candidate matches if their name aligns AND the company aligns with
  ANY affiliation in their profile: current company (currentPosition),
  past employers (experience), education, or the about text.
- Grade confidence by where the company matched: current company =
  HIGH; education, past employer, or about = MEDIUM; name matches but
  no company alignment anywhere = LOW_CONFIDENCE_MATCH with an
  explanation.
- Always state WHERE the company affiliation matched (e.g.
  "education: Example University").
- If several candidates match equally well, report the ambiguity
  rather than guessing. If step 1 returned no candidates, or none
  match, record "no LinkedIn match" and move on.
- Never fabricate. If a field is missing, write "not available".

If the Apify connector is unavailable or an actor call fails, fall
back to WebSearch: `site:linkedin.com "<participant_name>"
"<company_name>"` and note in the brief: "LinkedIn profile could not
be retrieved." Also tell the user directly to check the Apify
connector under Settings > Connectors (see CONNECTORS.md) - otherwise
every future brief silently degrades to the weaker fallback.

Tag all LinkedIn-sourced data with ⏳ (possibly stale) -- profiles are
self-reported and may be outdated.

**Anti-bot detection (WebFetch):**

Check every WebFetch response for:
- Total visible text under ~200 characters
- Strings: "checking your browser", "cloudflare", "ray id", "enable
  javascript", "cookie consent", "access denied"

If detected, treat as unreachable. Fall back to WebSearch.

### 5. Synthesize brief

Write one brief per meeting, grouping all researched participants.

Template:

```
# Meeting Brief: [Meeting Title]
[Date] . [Time] . [N participants researched]

## At a Glance
- [Name]: [Role] at [Company] -- [single most useful fact]
- [If archetype is not "general": one sentence connecting this meeting
  to user's context]

## [Participant Name] -- [Title/Role]

### Person
- Current role and tenure (if known)
- LinkedIn headline (if found)
- Professional background (2-3 sentences from search results)
- Confidence: [which facts are verified vs inferred vs not found]

### Company: [Company Name]
- What they do (one sentence)
- Size, location, markets
- Key products/services
- Industry position / notable facts
- [confidence tags on each field]

### Conversation Starters
- [3 specific openers based ONLY on actual research findings]
- [If archetype is not "general": 1-2 openers connecting their world to
  the user's context from user_context]

[Repeat for each participant]

---
Research depth: [FULL / LIMITED / MINIMAL]
Research conducted [date]. Sources: [URLs actually visited].
Confidence key: ✓ verified on source . ~ inferred . ⏳ verified but
possibly stale . ? not found
```

**Research depth indicator:**
- FULL: company website crawled + LinkedIn profile found and validated
- LIMITED: one primary source missing or unreachable
- MINIMAL: research based on web search snippets only

**Archetype framing** (from config `brief_archetype`):
- `sales`: fit assessment, pain points, buying signals, objection risks,
  deal openers
- `consulting`: engagement signals, likely challenges, budget
  indicators, decision-making structure, positioning angles
- `investor`: growth metrics, team assessment, market position, risk
  factors, due-diligence flags
- `general`: neutral intelligence, no product/service angle

The `user_context` provides specifics within the archetype.

**Rules:**
- Conversation starters come ONLY from what was actually found. Never
  fabricate. If nothing specific was found, say: "No specific
  conversation starters could be derived from available sources."
- LinkedIn data is tagged ⏳ (possibly stale), never ✓.
- Company website data is tagged ✓ if directly stated on the page,
  ~ if inferred from context.
- When LinkedIn says a person works at Company X but the company website
  does not list them, surface the discrepancy.
- Every URL cited in the sources footer was actually visited. Never cite
  a URL you did not fetch or search.

### 6. Deliver

Save each brief as a markdown file in the user's configured output path:
`<output.markdown_path>/YYYY-MM-DD-<meeting_title_slugified>.md`

If email delivery is enabled and an email connector is available, send
the brief to `output.email_to`. If the connector is unavailable, append
a note: "Email delivery was configured but the email connector is not
available. Brief saved to file only."

Present the brief to the user in the conversation as well.

---

## FIRST-RUN SETUP

If `config/config.json` does not exist, run this interactive setup:

1. Ask: "What is your company email domain? (e.g., acme.com)"
   -> Store as `user_domains` array. Accept multiple comma-separated
   domains.

2. Ask: "What best describes how you use meetings?"
   Options:
   - Sales: I meet prospects and clients to sell products/services
   - Consulting: I meet potential or current consulting clients
   - Investor: I meet founders, startups, or portfolio companies
   - General: Networking, partnerships, or mixed purposes
   -> Store as `brief_archetype`

3. Ask: "Briefly describe what you do and what you care about in
   meetings."
   Show an example for their archetype:
   - sales: "I sell marketing automation to mid-market SaaS companies.
     I care about whether they have an existing stack and budget."
   - consulting: "I consult on digital transformation for
     manufacturing. I care about organizational readiness and
     decision-maker access."
   - investor: "I invest in Series A B2B SaaS. I care about team
     quality, market size, and capital efficiency."
   - general: "I run partnerships at a media company. I care about
     mutual audience overlap."
   -> Store as `user_context`

4. Ask: "Where should I save briefs? (default: ~/meeting-briefs)"
   -> Store as `output.markdown_path`

5. Write `config/config.json` with the collected values. Set
   `output.email_enabled: false` (nested under the `output` object,
   next to `output.markdown_path`) and
   `max_participants_per_meeting: 3` (top level).

6. Confirm: "Config saved. Run /meeting-intel:prep again to generate
   your first briefs."

---

## Maintenance note (not part of the runtime flow)

If you are asked to edit this plugin's structure: `plugin.json` (at
`.claude-plugin/plugin.json`) must keep both its `$schema` field and
its `skills` array - without `skills` the plugin registers but loads
no skill, with no error shown. Relative links inside this file need
`../../` to reach files at the plugin root (e.g. `../../CONNECTORS.md`).

If you are asked to substitute a different Apify actor, verify its
input schema in the Apify console first: actors silently ignore input
fields they do not recognize and return zero results with no error.
