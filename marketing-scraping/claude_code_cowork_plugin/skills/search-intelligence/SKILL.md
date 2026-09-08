---
name: search-intelligence
description: This skill should be used whenever the user wants search-demand or SEO research. Triggers include "what does my market search for", "what do people search when looking for [X]", "what terms should I target", "keyword research", "keyword difficulty and volume", "who owns the SERP", "find winnable keywords", "search intelligence", "SEO opportunity analysis", "keyword gap analysis". It scrapes real search volume, difficulty, CPC and SERP rankings on the user's OWN Apify account, then writes a search-landscape report. It meters PER KEYWORD and needs a card on the Apify account (~$2 to $5 per run, more than the free credit); always dry-run first.
version: 1.0.0
---

# search-intelligence

Find what a market actually searches for, price every keyword by volume,
difficulty and cost-per-click, map who already ranks, and write a **search-landscape
report (S1)**: keywords clustered by intent, a measured demand table, a "winnable
opportunities" shortlist (low-difficulty terms with real volume), and a
competitive map of who owns the SERP. Every number is counted in code from scraped
data; the report writer clusters and interprets but never invents a figure.

**This is the expensive tool.** It meters per keyword on the user's Apify account
and **needs a card** (a ~300-keyword run costs about **$2 to $5**, and larger runs
cost more). **Never run the paid stage without first showing the dry-run forecast
and getting the user's explicit go-ahead.** Read `${CLAUDE_PLUGIN_ROOT}/docs/SETUP.md`
once for install, token, and the run-directory convention. Full field docs are in
`${CLAUDE_PLUGIN_ROOT}/skills/search-intelligence/QUICKSTART.md`.

## Step 0: Set up the run directory (ask the user)

These runs produce many files (the keyword universe, per-keyword metrics, SERP
rankings, a counted-facts file, the report). Keep each run self-contained.

**Ask the user where the run directory should live and confirm its name.** Propose
a name using the convention, then create it:

```
<parent>/search_<subject-slug>_<YYYY-MM-DD>/
```

- `<parent>` defaults to `marketing-research/` in the current working directory.
- `<subject-slug>` is the market, lowercased and hyphenated (e.g. `pm-software`).
- Append `_v2`, `_v3` if the same subject is re-run the same day.

Then copy the config template into that directory:

```bash
mkdir -p <run-dir>
cp ${CLAUDE_PLUGIN_ROOT}/skills/search-intelligence/SEARCH_CONFIG.example.json <run-dir>/SEARCH_CONFIG.json
```

## Step 1: Confirm the Apify token and the card (the token saves itself)

Confirm `APIFY_TOKEN` is exported, or that `apify login` has been run, and
remind the user this tool needs a payment method on the Apify account (a real
run exceeds the free monthly credit). Report writing uses the `claude` CLI; if it
is missing, the tool still writes `synthesis_facts.json` and skips the narrative.

**There is no separate "save the token" step to remember.** The first time a run
resolves the token from the environment, the tool writes it to `~/.apify/auth.json`
(the standard `apify login` location) automatically: non-destructively, mode 600,
never printing the token. Every later run in the same environment reads it back
with no re-entry.

What "the same environment" means, honestly:
- **Local Claude Code:** `~/.apify/auth.json` sits on the real machine, so the
  token is saved permanently (entered once, ever).
- **Cowork:** the token persists for the whole conversation (every run, and across
  app restarts). A brand-new Cowork conversation is a fresh sandbox, so the token
  is asked once there, a platform limit, not a bug. Suggest the user keep their
  token in a password manager so that one paste per conversation is painless.

To save the token explicitly without running a scrape first (optional; normal
runs already persist it on first use):

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/search-intelligence/scripts/config.py --persist-token
```

It reads the token from `APIFY_TOKEN` (never the command line) and writes the same
`~/.apify/auth.json`.

## Step 2: Fill the config (with the user)

Open `<run-dir>/SEARCH_CONFIG.json` and set:

- **`country_code`**: the primary market as an ISO code (`"US"`, `"GB"`, `"DE"`). Use `"GB"` for the United Kingdom; `"UK"` is accepted and treated as `"GB"`.
- **`language`**: the search language (`"en"`).
- **Input: pick one mode:** `seed_terms` (you have keywords) OR `vertical`
  (a market label the tool expands from, leaving `seed_terms` empty).
- **`keyword_cap`**: the main cost lever: the most keywords to price (default
  300). Fewer keywords = cheaper run.
- **`serp_top_n`**: how many top keywords get a SERP/ranking pull (default 100).
- **`opportunity_kd_max`**: difficulty ceiling for the "winnable" list (default
  40).
- **`budget_caps.phase1`**: the hard USD spend ceiling.
- **`location`**: optional city string (`"New York,New York,United States"`) for
  a city-localized SERP; blank = country-level.

## Step 3: Dry-run first (the money gate)

Always preview cost before spending anything:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/search-intelligence/scripts/search_intel.py \
  --config <run-dir>/SEARCH_CONFIG.json --workdir <run-dir> --dry-run
```

This prints the full plan (keyword expansion → volume/difficulty/CPC → SERP) and a
per-stage cost forecast, and spends nothing. **Show the user the forecast, check it
against `budget_caps.phase1`, and wait for explicit approval before Step 4.** For a
few cents of real output first, `--test-only` runs each stage on a handful of
keywords and stops.

**Confirm the seeds resolved, not just the cost.** The dry-run prints the input
mode and the resolved seed list. In `vertical` mode especially, check that the
market label produced sensible seeds before approving: the whole run expands from
them. If neither `seed_terms` nor `vertical` is set the tool stops with a clear
message rather than scraping an empty universe, so an empty seed list is never
silently priced. Reading a zero: an empty landscape means the input did not
resolve to keywords (a *miss* to fix), not that the market has no search demand.
See `docs/SETUP.md` (the money gate).

## Step 4: Run for real (only after approval)

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/search-intelligence/scripts/search_intel.py \
  --config <run-dir>/SEARCH_CONFIG.json --workdir <run-dir>
```

The tool chains automatically: seed keywords → autocomplete expansion →
volume/difficulty/CPC per keyword → localized SERP for the top terms →
deterministic synthesis → report. It shows the forecast before each paid stage and
stops at the budget cap.

### If a run comes back INCOMPLETE: resume it, don't restart

A scrape can end before it finishes: the actor aborts, times out, or the run is
cut off. When that happens the tool prints a loud `⚠ RUN INCOMPLETE` block naming
the run id, and the data on disk is only what was pulled so far. **Your baseline
recovery is to resume that exact run**: resurrect the run id on Apify, which
continues the SAME dataset from where it stopped, then regenerate the outputs from
the now-complete dataset. Do this first, before anything else.

- Do NOT treat a stopped or `[ABORTED]` run as finished: a truncated corpus is
  not a smaller-but-complete one. Report it as partial until you have resumed.
- Do NOT research or "verify" the failure before resuming, and do NOT start a
  fresh run or hand-build a merge/dedupe workaround: a fresh run re-charges for
  data you already have. Resume is the cheap, correct default.
- If the tool says it stopped at the spend cap, raise `budget_caps` first, then
  resume. Only if resurrecting genuinely fails do you consider alternatives, and
  say so plainly.
- When the user says a run was cut off, believe them and resume; don't re-verify
  whether it "really" finished.

## Step 5: Deliver the report

Outputs land in the run directory: `keywords/`, `metrics/`, `serp/`,
`synthesis_facts.json`, and **`S1_search_landscape.md`** (the deliverable). Present
S1 with its scrape-date snapshot and its sample-honesty framing intact: rankings
and volumes drift, and where only a slice of the universe was priced the report
calls its findings directional. Do not present them as a full census, and do not
add a domain-authority score; this tool surfaces Semrush's median referring-domains
signal, not a backlink index.

## Notes

- **Cost:** ~$2 to $5 for a ~300-keyword run; scales with `keyword_cap` and
  `serp_top_n`. Cheap next to a $99 to $139/mo keyword-tool subscription, but not free.
  The forecast is always shown first.
