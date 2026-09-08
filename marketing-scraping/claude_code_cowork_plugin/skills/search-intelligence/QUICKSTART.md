# Quickstart: Search Intelligence

Finds what your market actually searches for, prices every keyword by volume,
difficulty, and cost-per-click, maps who already ranks for those terms, and
writes you a search-landscape report. You bring your own Apify account and your
own Claude Code CLI. No vendor lock-in, no subscription.

> ## This tool costs real money to run. Read this first.
>
> Unlike a one-off scrape, this tool meters **per keyword** on your own Apify
> account. A focused run of roughly 300 keywords costs about **$2 to $5** in
> Apify credits, and larger runs (1,000+ keywords) cost more. That **exceeds
> Apify's free monthly credit, so you need a card on your Apify account.**
>
> It is cheap next to a $99 to $139 per month keyword-tool subscription, and it
> shows you the exact forecast before it spends anything (always run `--dry-run`
> first). But it is not free, and this guide will not pretend it is.

---

## Step 1: Get your Apify account and token

1. Sign up at [apify.com](https://apify.com) and add a payment method (a real
   run costs more than the free monthly credit; see the cost note above).
2. Give the tool your token one of two ways:
   - **CLI:** install the Apify CLI (`npm install -g apify-cli`) and run
     `apify login`. The tool reads `~/.apify/auth.json` automatically.
   - **Environment variable:** copy `scripts/.env.example` to `scripts/.env`,
     fill in `APIFY_TOKEN=your_token_here`, then run
     `set -a; source scripts/.env; set +a` before invoking the tool. (This
     `scripts/.env` route works from a checkout; an installed plugin's folder is
     read-only, so use the CLI login or the save-once step below instead.)
   - **Save an exported token once:** with `APIFY_TOKEN` set in your shell, run
     `python3 ${CLAUDE_PLUGIN_ROOT}/skills/search-intelligence/scripts/config.py --persist-token`.
     It stores the token in `~/.apify/auth.json` so later runs reuse it, and
     prints only a masked confirmation.

---

## Step 2: Configure your run

Copy the example config and fill it in:

```bash
cp SEARCH_CONFIG.example.json SEARCH_CONFIG.json
```

Open `SEARCH_CONFIG.json` and set:

- **`country_code`** the primary market as an ISO code (e.g. `"US"`, `"GB"`, `"DE"`). Use `"GB"` for the United Kingdom; `"UK"` is accepted and normalized to `"GB"`.
- **`language`** the search language (e.g. `"en"`).

**Pick one input mode:**

| Mode | When to use | Config keys |
|------|-------------|-------------|
| **Seed terms** | You already know some keywords | `seed_terms: ["project management software", "kanban board app"]` |
| **Vertical** | You want the tool to expand from a market label | `vertical: "project management software"` (leave `seed_terms` empty) |

**Optional localization.** Set `location` to a city string
(e.g. `"New York,New York,United States"`) for a city-localized SERP. Leave it
blank for country-level results. The tool converts the location into the search
parameter Google uses; you do not need to compute anything.

**Tuning knobs:**

- **`keyword_cap`** the most keywords to price and analyse (default 300). This
  is the main cost lever: fewer keywords, cheaper run.
- **`serp_top_n`** how many of the top keywords get a SERP (ranking) pull
  (default 100).
- **`opportunity_kd_max`** the difficulty ceiling for the "winnable" list
  (default 40, the common easy-to-rank threshold).
- **`budget_caps.phase1`** the hard spending cap in USD for the run. The tool
  stops before exceeding it.

---

## Step 3: Preview the cost (always do this first)

```bash
python3 scripts/search_intel.py --config SEARCH_CONFIG.json --workdir runs/my-run --dry-run
```

This prints the full plan (keyword expansion, then volume/difficulty/CPC, then
SERP) and a cost forecast for each stage, and spends **nothing**. Check the
forecast against your `budget_caps.phase1` before running for real.

You can also do a tiny paid smoke test first with `--test-only`, which runs each
stage on a handful of keywords and stops, so you see real output for a few cents
before committing to the full run.

---

## Step 4: Run

```bash
python3 scripts/search_intel.py --config SEARCH_CONFIG.json --workdir runs/my-run
```

The tool chains automatically: seed keywords, keyword expansion (autocomplete),
volume / difficulty / CPC per keyword, the localized SERP for the top terms,
then a deterministic synthesis, then the report. It shows the forecast before
each paid stage and stops at your budget cap.

---

## Step 5: What you get

After the run, `runs/my-run/` contains:

```
keywords/keywords.json        The expanded keyword universe
metrics/keyword_metrics.json  Volume, difficulty, CPC, median referring domains (Semrush)
serp/rankings.json            Per-keyword ranked results
serp/domain_map.json          Which domains own which keywords
synthesis_facts.json          Every number, counted in code and source-tagged
S1_search_landscape.md        The finished report
```

**`S1_search_landscape.md` is the deliverable.** It has an executive summary,
your keywords clustered by search intent, a measured keyword-demand table, a
"winnable opportunities" shortlist (low-difficulty keywords with real volume),
and a competitive map of who owns the SERP and where the gaps are. Every number
in it is counted in code from your scraped data; the report writer clusters and
interprets but never invents a figure.

**The report writer is the `claude` CLI** (Claude Code, installed and signed
in). It runs locally with no API key and no extra cost beyond your normal Claude
usage. If the `claude` CLI is not found, the tool still writes
`synthesis_facts.json` (all the counted facts) and skips the narrative; you can
generate the report yourself from that file.

---

## What this tool does and does not do

- **Freshness:** SERP rankings and search volumes are a snapshot from the scrape
  date, printed in the report. They drift over time. For a moving target, re-run.
- **Sample honesty:** if you price only a slice of the keyword universe, or pull
  SERPs for only a few terms, the report says so and calls the findings
  directional, not statistical. Widen `keyword_cap` and `serp_top_n` for a fuller
  picture (at higher cost).
- **No backlink index, no domain-authority score:** this tool does not crawl or
  index backlinks and does not assign domain-authority scores. It does surface
  one link signal from Semrush, the median number of referring domains for the
  pages that rank on each keyword, as an input to how hard that keyword is to
  win. Beyond that it reads live search demand and who currently ranks, nothing
  more.
- **What it is:** real search-demand and SERP data on your own account, turned
  into a report you can act on. It is not a free version of any paid keyword tool,
  and it does not try to be one.
