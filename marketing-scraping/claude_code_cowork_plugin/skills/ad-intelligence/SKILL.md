---
name: ad-intelligence
description: This skill should be used whenever the user wants to see what ads a company is running or research a competitor's advertising. Triggers include "show me what ads [competitor] is running", "what is [company] advertising", "find ads for [brand]", "what ads are my competitors running", "analyze competitor ad angles", "scrape Meta/Google/LinkedIn ads", "competitor ad intelligence", "what creative angles work in my market", "ad library research". It scrapes Meta Ad Library, Google Ads Transparency, and LinkedIn Ad Library on the user's OWN Apify account, downloads the creatives, OCRs Google ad images, and assembles a report bundle an LLM turns into a creative-angles analysis. It costs real money on the user's Apify account (~$0.40 for a single-country run); always dry-run first.
version: 1.0.0
---

# ad-intelligence

Research what a market's competitors are actually advertising. This skill scrapes
three ad libraries on the user's own Apify account, pulls the creatives, and
assembles a ready-to-analyse bundle (`DATA_DIGEST.md` + a fill-in report
template + a paste-ready `PROMPT.md`) that an LLM turns into an **E3 creative-angles
report**.

It spends real money on the user's Apify account. **Never run the paid stage
without first showing the dry-run cost forecast and getting the user's explicit
go-ahead.** Read `${CLAUDE_PLUGIN_ROOT}/docs/SETUP.md` once for install, token, and
the run-directory convention. Full config-field documentation lives in
`${CLAUDE_PLUGIN_ROOT}/skills/ad-intelligence/QUICKSTART.md`. Consult it for any
field not covered here.

## Step 0: Set up the run directory (ask the user)

These runs produce many files (raw ad records per source, downloaded creatives,
OCR output, the report bundle). Keep each run self-contained in its own directory.

**Ask the user where the run directory should live and confirm its name.** Propose
a name using the convention, then create it:

```
<parent>/adintel_<subject-slug>_<YYYY-MM-DD>/
```

- `<parent>` defaults to `marketing-research/` in the current working directory.
- `<subject-slug>` is the market or lead competitor, lowercased and hyphenated
  (e.g. `emergency-plumber`, `acme-plumbing`).
- Append `_v2`, `_v3` if the same subject is re-run the same day.

Then copy the config template into that directory:

```bash
mkdir -p <run-dir>
cp ${CLAUDE_PLUGIN_ROOT}/skills/ad-intelligence/AD_INTEL_CONFIG.example.json <run-dir>/AD_INTEL_CONFIG.json
```

## Step 1: Confirm the Apify token (it saves itself after first use)

Confirm `APIFY_TOKEN` is exported, or that `apify login` has been run (the tool
reads `~/.apify/auth.json`). If neither is present, point the user to
`docs/SETUP.md`. Do not proceed to any paid stage without a token.

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
python3 ${CLAUDE_PLUGIN_ROOT}/skills/ad-intelligence/scripts/config.py --persist-token
```

It reads the token from `APIFY_TOKEN` (never the command line) and writes the same
`~/.apify/auth.json`.

## Step 2: Fill the config (with the user)

Open `<run-dir>/AD_INTEL_CONFIG.json` and set at minimum:

- **`country_code`**: the primary market (`"us"`, `"gb"`, `"de"`).
- **`vertical`**: a short market label (`"emergency plumber"`), for labelling.
- **Competitors: pick ONE mode:** `competitor_domains` (best, e.g.
  `["acmeplumbing.com"]`), or `competitor_names`, or leave both empty and set
  `seed_terms` to let the tool discover competitors from a SERP for the user to
  confirm.
- **`meta_search_terms`**: keywords Meta searches for, relevant to the vertical.
- **`budget_caps.phase1`**: the hard USD spend ceiling for the run.

For LinkedIn ads (the only source with real impression counts), set
`competitor_names` or let the tool derive them from domains; set
`include_linkedin: false` to skip. For a cheap trial, lower
`meta_limit_per_source` and `google_max_items` (e.g. 20 / 30). See QUICKSTART.md
for every field.

## Step 3: Dry-run first (the money gate)

Always preview cost before spending anything:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/ad-intelligence/ad_intel.py \
  --config <run-dir>/AD_INTEL_CONFIG.json --workdir <run-dir> --dry-run
```

This prints the execution plan and a cost forecast (~$0.40 for a single-country,
2 to 3 competitor run) and spends nothing. **Show the user the forecast and wait for
explicit approval before Step 4.** In discovery mode, the tool surfaces candidate
competitors here for the user to confirm before any paid scrape.

**Confirm the right target resolved, not just the cost.** The dry-run shows what
each competitor resolved to: a validated domain, and for LinkedIn the numeric
company id (e.g. `[id 68529]`). Check these are the intended companies before
approving. A LinkedIn source with no numeric id is skipped rather than guessed at,
so if a competitor shows no id, that source will be empty for them by design.
Reading a zero: a source that returns nothing because the target did not resolve
is a *miss* (correct the input), not evidence the competitor runs no ads. See
`docs/SETUP.md` (the money gate) for the full distinction.

## Step 4: Run for real (only after approval)

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/ad-intelligence/ad_intel.py \
  --config <run-dir>/AD_INTEL_CONFIG.json --workdir <run-dir>
```

The tool chains automatically: competitor resolution → Meta scrape → creatives →
advertiser curation → Google scrape → creatives → OCR → report bundle. OCR of
Google ad images uses a cheap `claude`-CLI subagent (no API key); if `claude` is
not on PATH, OCR is skipped and those ads show `(no OCR)`. If instead the
creatives download as **zero images** (the report has ad counts but no visual or
OCR content), the ad-image networks are being blocked at the sandbox. This is the
creative-download egress case in `docs/SETUP.md`: in Cowork's egress settings,
allow the ad-creative hosts: `tpc.googlesyndication.com`, `media.licdn.com`,
`dms.licdn.com`, and `*.fbcdn.net` (Facebook's rotating subdomains), and re-run.
Do not use "All domains"; those four entries are all the creatives need. The
scrape already succeeded, so this costs nothing extra.

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

The run leaves a `report/` folder in the run directory with `PROMPT.md`,
`DATA_DIGEST.md`, and the `E3_creative_angles.md` template (six sections). Produce
the finished report by following `PROMPT.md`: fill the template's sections from
`DATA_DIGEST.md`, referencing the downloaded `creatives/` for visual context.
Sections 4 (gap analysis) and 6 (seasonal timing) draw on the SEO and
pain-messaging tools; if those were not run, complete the other four fully and
note the gap. Every claim in the report must trace to the scraped data; never
invent an ad, a figure, or an angle.

## Notes

- **Cost:** ~$0.40 per single-country 2 to 3 competitor run; multi-country scales
  proportionally (3 countries ≈ 3×). The forecast is always shown first.
- **Samples, not censuses**: the report is directional where it covers a slice of
  a market. Keep that framing in the write-up.
