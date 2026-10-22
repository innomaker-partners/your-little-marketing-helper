# Quickstart: Ad Intelligence Tool

Scrapes competitor ads from Meta Ad Library, Google Ads Transparency, and LinkedIn
Ad Library, downloads creatives, OCRs Google images, and assembles a ready-to-analyse
report bundle you run through your own LLM. No vendor lock-in: you bring your own Apify
account and your own AI model.

---

## Step 1: Get your Apify account and token

1. Sign up at [apify.com](https://apify.com) (free tier covers a first test run).
2. Authenticate one of two ways:
   - **CLI:** install the Apify CLI (`npm install -g apify-cli`; this needs a recent
     Node.js, see `docs/SETUP.md`, Route 2) and run `apify login`.
     The tool reads `~/.apify/auth.json` automatically.
   - **Environment variable:** copy `scripts/.env.example` to `scripts/.env`, fill in
     `APIFY_TOKEN=your_token_here`, then run `set -a; source scripts/.env; set +a`
     before invoking the tool. (The `scripts/.env` route is meant for a checkout. For
     an installed plugin use the CLI login or the save-once step below, which do not
     depend on where the plugin is installed.)
   - **Save an exported token once:** with `APIFY_TOKEN` set in your shell, run
     `python3 ${CLAUDE_PLUGIN_ROOT}/skills/ad-intelligence/scripts/config.py --persist-token`.
     It stores the token in `~/.apify/auth.json` (the same file the CLI uses) so
     later runs reuse it, and prints only a masked confirmation. If a different
     login is already saved there, that login is kept, nothing is saved, and the
     command says so. In your own terminal `${CLAUDE_PLUGIN_ROOT}` is not set: `docs/SETUP.md` (Route 1) gives a line that finds the plugin folder for you. Do not paste the token into a chat with Claude.

---

## Step 2: Configure your run

Copy the example config and fill it in:

```bash
cp AD_INTEL_CONFIG.example.json AD_INTEL_CONFIG.json
```

Open `AD_INTEL_CONFIG.json` and set:

- **`country_code`**: the primary market you want to research (e.g. `"us"`, `"gb"`, `"de"`).
- **`country_codes`**: optional list for multi-country runs. What extra countries do
  to the cost forecast is in the cost note below.
- **`language`**: ad language filter (e.g. `"en"`).
- **`vertical`**: short description of the market (e.g. `"emergency plumber"`).
  Used for labelling only.

**Competitor input. Pick one of these modes:**

| Mode | When to use | Config keys |
|------|------------|-------------|
| **Name and website together** (most reliable) | You know a company's name and its website | `competitor_names: ["Acme Strategy Advisors (acmesa.com)"]` |
| **Domains** | You know the websites but not the exact names | `competitor_domains: ["acmeplumbing.com"]` |
| **Names** | You know company names but not domains | `competitor_names: ["Acme Plumbing", "Best Drains"]` |
| **Discovery** | You want the tool to find competitors for you | `seed_terms: ["emergency plumber chicago"]` |

Give the name and the website together whenever you know both. Write the website in
brackets after the name, with nothing else in the brackets. The company then counts as
one competitor: the website is the one searched for Google ads, and the name is what the
Facebook page, the LinkedIn company and the advertiser names on Google ads are matched
against. A website alone carries no name, so a company that advertises under its full
name is easier to miss.

What counts as a website in the brackets: one word with no spaces inside (spaces around
it are fine) that starts with `www.` or `https://`, or that ends in one of about 200
common website endings (`.com`, `.net`, `.io`, `.shop`, `.store`, `.online`, `.agency`,
`.consulting`, `.xyz` and so on) or a two-letter country code (`.co.uk` and `.com.au` work),
with the ending in one case, and whose name before the ending is not a company form or
generic word. `AcmePlumbing.com` and `Acme.com` are websites (capitals in the name are
fine); `Ltd.co`, `Co.uk`, `Pty.Ltd` and `St.Louis` are read as names: start a website with
`https://` if one of them really is one. The same name-part rule applies to a website typed
alone as an entry (`Inc.Co` alone is a name), though not the capital-letters rule
(`AcmePlumbing.com` alone is a website). Anything else stays part of the name: `"Acme (UK) Ltd"` and
`"Best Drains (Chicago)"` quietly, and a dotted bracket that is not taken as a website,
such as `"Acme Pty (Pty.Ltd)"`, with a notice when the run starts. The company is then
looked up by search and the website found is shown as unconfirmed, with a warning on its
Google line, in the closing lines and in the data digest.
A Facebook, LinkedIn, link-shortener, link-in-bio or directory address in the brackets
(`fb.com`, `lnkd.in`, `bit.ly`, `linktr.ee`, a Google address, `yelp.com`) is not the
company's website: the run uses the name alone and tells you so. If you change the website you gave for a company
you have already run, the run looks that company up again and asks you to confirm it.

You can also fill both `competitor_names` and `competitor_domains`. A name and a domain
in separate lists count as one competitor only when the name spells the domain's first
part (`"Revolut"` and `"revolut.com"`); any other name and website are joined with the
bracket form above, so list each company once.

A website you give is the one used for Google ads. If the search finds a different best
match, the resolution report marks your website with a warning that names the search's
best match, so you can check you typed it right, and does not replace yours. The closing
lines repeat it. A name given without a website has only the search to go on, and the
search can give a different best match on another day, so check that line in the report.

To change which website Google searches, edit `use` in `competitors/resolution.json`. The
tool keeps `competitors/domains.json` in step with it. If you edit `domains.json` by hand
and the two files disagree, the tool does not guess which one you meant: it does not
search the companies concerned, tells you so, and gives the fix (set `use` to the right
website, delete `domains.json`, run the same command on the folder).

Google ads from an advertiser whose name does not match the competitor are set aside:
they are named in the report and not shown as the competitor's. Counting another
company's ad as the competitor's would go unnoticed, while setting a real one aside is
visible and can be fixed. If a set-aside advertiser is the competitor's (a holding
company, the owner's own name), ask Claude to add that name to the competitor's Google
advertisers. An advertiser counts as the competitor's when its name starts with the name
you gave. If you gave only a website whose name is one ordinary word, the advertiser's whole name
must be that word, so a longer name is set aside until you give the name with the
website: `Best Drains (bestdrains.com)`. A website made of two or more words counts
advertisers whose name starts with those words. A website that is a company's first word
plus the initials of the rest of its name (`acmesa.com` for Acme Strategy Advisors) counts
that company.

For discovery mode the tool surfaces a list of candidates for you to review before
any paid scraping runs. Add confirmed domains to `competitor_domains` and re-run.

Leave `meta_search_terms` empty for normal use. Meta is searched through each
competitor's own Facebook page, and these words are not used as soon as any competitor has
one. They matter only when no competitor has a page: then they run a paid sweep of the ads
of any advertiser that uses those words, which is not your competitors' own ads. The
resolution report says which of the two cases you are in.

Set `budget_caps.phase1` to the maximum you want to spend per run in USD.

The tool also scrapes LinkedIn Ad Library (using the `scrapesage/linkedin-ad-library-scraper` actor). LinkedIn ads are scoped by the competitor's **numeric company id**, which the tool resolves automatically from the competitor's public LinkedIn company page. This is the only precise advertiser scoping (keyword or name search returns other advertisers). This is the only source that returns impressions, as bands such as 5k-10k rather than exact counts, with no LinkedIn login required. Set `include_linkedin: false` to skip it, or lower `linkedin_max_items` for a cheaper trial.

---

## Step 3: Run

**Always preview cost first with `--dry-run`:**

```bash
python3 ad_intel.py --config AD_INTEL_CONFIG.json --workdir runs/my-run --dry-run
```

This prints the execution plan and a cost forecast without spending anything. Once
the competitors are confirmed, the whole-run forecast at the example settings is at
most about $9.77 for 2 competitors and $11.74 for 3. It is a worst case (every step
at its largest allowed size), so a real run usually costs less.

**Run for real:**

```bash
python3 ad_intel.py --config AD_INTEL_CONFIG.json --workdir runs/my-run
```

The tool chains automatically: competitor resolution → Meta scrape → Meta creative
download → advertiser curation → Google scrape → Google creative download → OCR →
report bundle assembly.

If the tool says it is reusing an earlier resolution and you want the competitors
looked up again, add `--force` to the same command. It does not run a finished
Meta, Google or LinkedIn scrape again. It looks the competitors up again from the
Google search that finds them, and then stops for you to confirm them unless you
have already marked them confirmed. If this folder already holds that search's
finished result, nothing is bought. If the search never ran in this folder (for
example in a repeat that copied last time's competitors), it runs now as a small
paid step and is billed.

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

**To get your creative angle report:**

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

At the example settings a single-country run is forecast at most about **$9.77 for 2
competitors and $11.74 for 3** in Apify credits. That is a worst case: a real run is
billed for what actually comes back, so it usually costs less. The tool shows the forecast
before spending (use `--dry-run`). Google is searched in batches of 10 pairs of
country and website (domain), each batch is its own run with its own cap on ads, and
each further batch adds to the forecast. Countries times websites up to 10 is one
batch, so adding a country raises the Google part of the forecast only once the pairs
pass 10: Google adds one more run each time the pairs pass another multiple of 10 (11
to 20 pairs is two runs). A competitor with no website adds no pair, and two
competitors with the same website count once. The Meta part does not depend on
countries when competitors are searched by their Facebook page. The optional Meta
search words are searched once per country and word, so each country adds to that
part. Meta can return up to 30 more ads than its cap for each page or search,
each one billed, and the forecast counts them.

OCR of Google ad text is done by a cheap-model subagent via the `claude` CLI (requires
Claude Code installed and authenticated), no API key and no extra cost beyond your normal
Claude usage. If the `claude` CLI is not found, OCR is skipped, the closing lines name it
as missing with that reason, and Google ads appear in the report with `(no OCR)` in the
copy column. OCR runs only on the top 20%
of Google ads by how long they have been running (the performance proxy, since Google
never exposes impression counts); this is configurable via `ocr_top_fraction` in the
config file.

---

## Repeat it every month

After a finished run, ask Claude to "set up a monthly repeat". You approve a spending
ceiling once (the most one month may spend), and each month's run then starts by
itself on this computer or in Claude's cloud, and does not start if its forecast is
above the ceiling. To see what changed, ask "what changed since last month?". Read
`docs/REPEATS.md` in the plugin before you set it up.
