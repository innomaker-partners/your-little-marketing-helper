# Quickstart: Ad Intelligence Tool

Scrapes competitor ads from Meta Ad Library, Google Ads Transparency, and LinkedIn
Ad Library, downloads creatives, OCRs Google images, and assembles a ready-to-analyse
report bundle you run through your own LLM. No vendor lock-in: you bring your own Apify
account and your own AI model.

---

## Step 1: Get your Apify account and token

1. Sign up at [apify.com](https://apify.com) (free tier covers a first test run).
2. Authenticate one of two ways:
   - **CLI:** install the Apify CLI (`npm install -g apify-cli`) and run `apify login`.
     The tool reads `~/.apify/auth.json` automatically.
   - **Environment variable:** copy `scripts/.env.example` to `scripts/.env`, fill in
     `APIFY_TOKEN=your_token_here`, then run `set -a; source scripts/.env; set +a`
     before invoking the tool. (The `scripts/.env` route works when you run from a
     checkout; when the tool is installed as a plugin its folder is read-only, so
     use the CLI login or the save-once step below instead.)
   - **Save an exported token once:** with `APIFY_TOKEN` set in your shell, run
     `python3 ${CLAUDE_PLUGIN_ROOT}/skills/ad-intelligence/scripts/config.py --persist-token`.
     It stores the token in `~/.apify/auth.json` (the same file the CLI uses) so
     later runs reuse it, and prints only a masked confirmation.

---

## Step 2: Configure your run

Copy the example config and fill it in:

```bash
cp AD_INTEL_CONFIG.example.json AD_INTEL_CONFIG.json
```

Open `AD_INTEL_CONFIG.json` and set:

- **`country_code`**: the primary market you want to research (e.g. `"us"`, `"gb"`, `"de"`).
- **`country_codes`**: optional list for multi-country runs. Each additional country
  multiplies ad volume (and Apify cost) proportionally.
- **`language`**: ad language filter (e.g. `"en"`).
- **`vertical`**: short description of the market (e.g. `"emergency plumber"`).
  Used for labelling only.

**Competitor input. Pick one of three modes:**

| Mode | When to use | Config keys |
|------|------------|-------------|
| **Domains** (best) | You already know your competitors' websites | `competitor_domains: ["acmeplumbing.com"]` |
| **Names** | You know company names but not domains | `competitor_names: ["Acme Plumbing", "Best Drains"]` |
| **Discovery** | You want the tool to find competitors for you | `seed_terms: ["emergency plumber chicago"]` |

For discovery mode the tool surfaces a list of candidates for you to review before
any paid scraping runs. Add confirmed domains to `competitor_domains` and re-run.

Set `meta_search_terms` to the keywords Meta should search for (relevant to the vertical).

Set `budget_caps.phase1` to the maximum you want to spend per run in USD.

The tool also scrapes LinkedIn Ad Library (using the `scrapesage/linkedin-ad-library-scraper` actor). LinkedIn ads are scoped by the competitor's **numeric company id**, which the tool resolves automatically from the competitor's public LinkedIn company page — this is the only precise advertiser scoping (keyword or name search returns other advertisers). This is the only source that returns real impression counts, with no LinkedIn login required. Set `include_linkedin: false` to skip it, or lower `linkedin_max_items` for a cheaper trial.

---

## Step 3: Run

**Always preview cost first with `--dry-run`:**

```bash
python3 ad_intel.py --config AD_INTEL_CONFIG.json --workdir runs/my-run --dry-run
```

This prints the execution plan and a cost forecast (~$0.40 for a single-country run
with 2 to 3 competitors) without spending anything.

**Run for real:**

```bash
python3 ad_intel.py --config AD_INTEL_CONFIG.json --workdir runs/my-run
```

The tool chains automatically: competitor resolution → Meta scrape → Meta creative
download → advertiser curation → Google scrape → Google creative download → OCR →
report bundle assembly.

---

## Step 4: What you get

After the run, `runs/my-run/` contains:

```
B4_meta_ad_library/     Raw Meta Ad Library records (JSON)
B5_google_ads/          Raw Google Ads Transparency records (JSON)
LI_linkedin_ads/        Raw LinkedIn Ad Library records (JSON)
competitors/
  domains.json                Resolved competitor domains
  DISCOVERED_advertisers.md   New advertisers found, review and add confirmed ones
creatives/
  meta/       Downloaded Meta ad images + manifest.json
  google/     Downloaded Google ad images + manifest.json
  linkedin/   Downloaded LinkedIn ad images/videos + manifest.json
google_ocr.json         OCR transcriptions for Google ad images
report/
  E3_creative_angles.md  The analysis template (six sections)
  DATA_DIGEST.md         All ad copy + durations + impressions + creative filenames, ready to analyse
  PROMPT.md              Ready-to-paste instruction for your LLM
```

**To get your E3 creative angle report:**

1. Open your LLM of choice (ChatGPT, Claude, Gemini, or any local model).
2. Paste the contents of `report/PROMPT.md` as the first message.
3. Attach `report/E3_creative_angles.md` and `report/DATA_DIGEST.md` as context.
4. Point it at the `creatives/` folder for visual reference.
5. The LLM fills the six template sections from your data and returns the completed report.

The template's gap analysis (Section 4) and seasonal timing (Section 6) reference
data from the SEO and pain-messaging tools. If you haven't run those, the LLM will
note it and complete the other four sections fully.

---

## Cost note

A single-country run with 2 to 3 competitors typically costs **~$0.40 in Apify credits**.
The tool shows a forecast before spending (use `--dry-run`). Multi-country runs scale
proportionally: 3 countries = ~3×.

OCR of Google ad text is done by a cheap-model subagent via the `claude` CLI (requires
Claude Code installed and authenticated), no API key and no extra cost beyond your normal
Claude usage. If the `claude` CLI is not found, OCR is silently skipped and Google ads
appear in the report with `(no OCR)` in the copy column. OCR runs only on the top 20%
of Google ads by how long they have been running (the performance proxy, since Google
never exposes impression counts); this is configurable via `ocr_top_fraction` in the
config file.
