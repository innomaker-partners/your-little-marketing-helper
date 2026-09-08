---
name: pain-and-messaging
description: This skill should be used whenever the user wants reviews of a specific app, company, or competitor, OR wants to mine customer reviews for pain and trust signals. Triggers include "App Store reviews for [X]", "Google Play reviews for [app]", "Trustpilot reviews for [company]", "what are people saying about [X]", "G2 reviews for [tool]", "Capterra reviews for [software]", "pull the reviews for [competitor]", "what do customers complain about", "mine reviews for pain points", "voice-of-customer research". A plain request like "I want the App Store reviews for [product]" is exactly this skill, not a web search. It scrapes reviews across Google Maps, Trustpilot, the App Store, Google Play, Reddit, G2 and Capterra on the user's OWN Apify account, counts every finding in code before any LLM runs, and writes an E1 pain-taxonomy report and an E2 trust-signals report. It costs real money on the user's Apify account (~$0.50 for a Maps run); always dry-run first.
version: 1.0.0
---

# pain-and-messaging

Mine customer reviews of a market's competitors for the language of **pain**
(from low-rated reviews) and **trust** (from high-rated reviews). Every finding is
counted in code and tied to a real quoted review *before* any LLM runs; the LLM
only clusters and labels what the code already found, so every claim is verifiable.
The run produces two analyst reports: **E1** (a tiered pain-point taxonomy with a
verbatim language bank and a pain-to-promise map) and **E2** (trust signals).

It spends real money on the user's Apify account. **Never run the paid stage
without first showing the dry-run cost forecast and getting the user's explicit
go-ahead.** Read `${CLAUDE_PLUGIN_ROOT}/docs/SETUP.md` once for install, token, and
the run-directory convention. Full config-field documentation lives in
`${CLAUDE_PLUGIN_ROOT}/skills/pain-and-messaging/QUICKSTART.md`; consult it for
sources, Maps input modes, and every field. A finished E1 example is at
`${CLAUDE_PLUGIN_ROOT}/skills/pain-and-messaging/examples/E1_pain_points_SAMPLE.md`.

## Step 0: Set up the run directory (ask the user)

These runs produce many files (raw reviews per source, a counted-facts file, two
reports). Keep each run self-contained in its own directory.

**Ask the user where the run directory should live and confirm its name.** Propose
a name using the convention, then create it:

```
<parent>/pain_<subject-slug>_<YYYY-MM-DD>/
```

- `<parent>` defaults to `marketing-research/` in the current working directory.
- `<subject-slug>` is the market or lead competitor, lowercased and hyphenated
  (e.g. `fitness-gyms`, `project-mgmt-saas`).
- Append `_v2`, `_v3` if the same subject is re-run the same day.

Then copy the config template into that directory:

```bash
mkdir -p <run-dir>
cp ${CLAUDE_PLUGIN_ROOT}/skills/pain-and-messaging/PAIN_CONFIG.example.json <run-dir>/PAIN_CONFIG.json
```

## Step 1: Confirm the Apify token (it saves itself after first use)

Confirm `APIFY_TOKEN` is exported, or that `apify login` has been run (the tool
reads `~/.apify/auth.json`). If neither is present, point the user to
`docs/SETUP.md`.

**There is no separate "save the token" step to remember.** The first time a run
resolves the token from the environment, the tool writes it to `~/.apify/auth.json`
(the standard `apify login` location) automatically, non-destructively, mode 600,
never printing the token. Every later run in the same environment reads it back
with no re-entry.

What "the same environment" means, honestly:
- **Local Claude Code:** `~/.apify/auth.json` sits on the real machine, so the
  token is saved permanently: entered once, ever.
- **Cowork:** the token persists for the whole conversation (every run, and across
  app restarts). A brand-new Cowork conversation is a fresh sandbox, so the token
  is asked once there (a platform limit, not a bug). Suggest the user keep their
  token in a password manager so that one paste per conversation is painless.

To save the token explicitly without running a scrape first (optional; normal
runs already persist it on first use):

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/pain-and-messaging/scripts/config.py --persist-token
```

It reads the token from `APIFY_TOKEN` (never the command line) and writes the same
`~/.apify/auth.json`.

Report writing also needs the `claude` CLI on PATH; if it is missing, the run
still completes acquisition and extraction and leaves `synthesis_facts.json` for
later report generation at no Apify cost.

## Step 2: Choose sources and fill the config (with the user)

Open `<run-dir>/PAIN_CONFIG.json`. The one required field is **`country_code`**.
Then:

- **`sources`**: defaults to `["maps"]`. Add any of `trustpilot`, `appstore`,
  `googleplay`, `reddit`, `g2`, `capterra`, and fill each one's subject key
  (`trustpilot_domains`, `appstore_app_ids`, `googleplay_app_ids`,
  `reddit_queries`/`reddit_subreddits`/`reddit_urls`, `g2_urls`, `capterra_urls`).
  Match the source to where the competitors live: SaaS → G2/Capterra, apps →
  App Store/Google Play, open discussion → Reddit. Maps alone is a complete run.
- **Google Maps input**: fill exactly one of `competitor_place_urls` (you have
  the URLs), `competitor_names` (tool matches listings), or `maps_search_queries`
  + `vertical` + `city` (category discovery). See QUICKSTART.md §3.
- **`max_reviews_per_place`**: 100 to 200 for a real corpus (default 20 is a test).
- **`report_model`**: blank uses `sonnet`; the clustering/writing step is
  judgment, so do not drop below Sonnet.
- **`budget_caps.phase1`**: the hard USD spend ceiling.
- **`maps_location_query`**: if using Maps mode 2 or 3, read QUICKSTART.md §4:
  it must be a single geocodable place (`"Chicago, USA"`) or `""`, or the Maps
  actor fails the whole run with "LOCATION NOT FOUND."

A source name in `sources` with an empty subject key warns at startup; an unknown
source name stops the run cleanly, so a typo never scrapes nothing silently.

## Step 3: Dry-run first (the money gate)

Always preview cost before spending anything:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/pain-and-messaging/pain_intel.py \
  --config <run-dir>/PAIN_CONFIG.json --workdir <run-dir> --dry-run
```

This resolves subjects, prints the plan and a per-stage cost forecast, and spends
nothing. In Maps mode 2 it shows which search strings map to each competitor name
; review them with the user. **Show the user the forecast and wait for explicit
approval before Step 4.**

**Confirm each source resolved to the right entity, not just the cost.** Every
source resolves or validates its target at the gate: App Store to a real app
(name and review count shown), Google Play to a package, G2 to a product slug,
Trustpilot to a reachable company domain (a redirect to a different domain is
flagged), Capterra to a valid profile URL, Maps to real listings. A target that
does not resolve is marked and skipped rather than scraped blindly. Reading a
zero: if a source returns nothing because its target did not resolve, that is a
*miss* to fix in the input, not evidence the competitor has no reviews. See
`docs/SETUP.md` (the money gate) for the full distinction.

## Step 4: Run for real (only after approval)

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/pain-and-messaging/pain_intel.py \
  --config <run-dir>/PAIN_CONFIG.json --workdir <run-dir>
```

Two halves: **acquisition** (each source scraped in turn, a forecast before each
Apify stage; Reddit is banded into pain/praise/set-aside-neutral right after
scrape), then **analysis** (the extractor counts and sources pain/trust phrases in
code into `synthesis_facts.json`, then the `claude` CLI writes E1 and E2 from
those pre-counted facts). Report writing takes a few minutes per report and is not
timed out; slowness is normal, let it finish.

### If a run comes back INCOMPLETE: resume it, don't restart

A scrape can end before it finishes: the actor aborts, times out, or the run is
cut off. When that happens the tool prints a loud `⚠ RUN INCOMPLETE` block naming
the run id, and the data on disk is only what was pulled so far. **Your baseline
recovery is to resume that exact run**: resurrect the run id on Apify, which
continues the SAME dataset from where it stopped, then regenerate the outputs from
the now-complete dataset. Do this first, before anything else.

- Do NOT treat a stopped or `[ABORTED]` run as finished; a truncated corpus is
  not a smaller-but-complete one. Report it as partial until you have resumed.
- Do NOT research or "verify" the failure before resuming, and do NOT start a
  fresh run or hand-build a merge/dedupe workaround: a fresh run re-charges for
  data you already have. Resume is the cheap, correct default.
- If the tool says it stopped at the spend cap, raise `budget_caps` first, then
  resume. Only if resurrecting genuinely fails do you consider alternatives, and
  say so plainly.
- When the user says a run was cut off, believe them and resume; don't re-verify
  whether it "really" finished.

## Step 5: Deliver and read the reports

Outputs land in the run directory: `synthesis_facts.json` (the counted, sourced
findings with per-source breakdown), `E1_pain_points.md`, and `E2_trust_signals.md`.

When presenting E1, lead with its header numbers: total reviews, the low-rated
(pain) corpus size, the per-source breakdown, and whether findings are labelled
**statistical** or **directional**. The pain corpus is usually a small fraction of
all reviews, so many runs are directional. Say so. A directional finding tied to
real customer language is useful for copy and positioning, but it is not a survey
result; never present it as one.

## Notes

- **Cost:** roughly $0.50 for a Maps run at 100 reviews/competitor; per-source
  per-event rates are in QUICKSTART.md §8. The forecast is always shown first.
- **The `claude` report step runs without MCP servers loaded** (via --strict-mcp-config, for a faster start with no side effects). If blank browser windows open or a report stalls for minutes, the copy is old/modified or someone piped the prompt into `claude` by hand; let `pain_intel.py` invoke the CLI itself. See QUICKSTART.md §9.
