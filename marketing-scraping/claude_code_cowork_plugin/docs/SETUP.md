# Setup: marketing-scraping plugin

This plugin gives Claude Code three market-research skills (**ad-intelligence**,
**pain-and-messaging**, and **search-intelligence**) that scrape public data on
*your own* Apify account and turn it into reports. Nothing runs on InnoMaker's
infrastructure; you control what runs and what it costs.

The skills auto-activate when you describe the task ("analyze my competitors'
ads", "what do customers complain about", "keyword research for my market"), and
each tool also has an explicit slash command: `/marketing-scraping:ad-intelligence`,
`/marketing-scraping:pain-and-messaging`, `/marketing-scraping:search-intelligence`.

---

## Install

```bash
claude plugin marketplace add <path-or-url-to-this-folder>
claude plugin install marketing-scraping@marketing-scraping-local
```

The plugin installs read-only into Claude Code's plugin cache. You never edit or
run anything inside that cache; you run each tool against a **run directory** of
your own (see below), and the plugin's scripts are reached through the
`${CLAUDE_PLUGIN_ROOT}` path the skills already use.

## What you need

- **An Apify account and API token**, free to create at
  [apify.com](https://apify.com). ad-intelligence and pain-and-messaging usually
  stay inside Apify's free monthly credit; **search-intelligence meters per
  keyword and needs a card** (~$2 to $5 per run).
- **Claude Code CLI**, installed and signed in: the report-writing step calls it
  locally, so there is no separate LLM API key and no per-report cost beyond your
  normal Claude usage.
- **Python 3**: standard library only, nothing to `pip install`.

## Give the tools your Apify token

The install directory is read-only, so the old "drop a `.env` in `scripts/`"
route does not apply. Use one of these instead; both work regardless of where
the plugin is installed:

- **Apify CLI (recommended):** `npm install -g apify-cli` then `apify login`.
  The tools read `~/.apify/auth.json` automatically. Nothing else to do.
- **Environment variable:** export `APIFY_TOKEN=apify_api_...` in the shell
  session Claude Code runs in (e.g. add it to your shell profile). Every tool
  reads `APIFY_TOKEN` first.
- **Nothing to do: the token saves itself.** Whichever of the two routes above
  you use, the first run that reads `APIFY_TOKEN` from the environment also saves
  it to `~/.apify/auth.json` automatically, so later runs never re-ask. To save it
  explicitly first, without running a scrape:

  ```bash
  export APIFY_TOKEN=apify_api_...
  python3 ${CLAUDE_PLUGIN_ROOT}/skills/search-intelligence/scripts/config.py --persist-token
  ```

  It reads the token from the environment (never the command line, so it cannot
  leak into your shell history or process list), merges it into
  `~/.apify/auth.json` without disturbing an existing `apify login`, and prints a
  masked confirmation. Any of the three tools' `config.py` does this; they share
  the same token file.

**How long the saved token lasts.** In local Claude Code, `~/.apify/auth.json` is
on your real machine, so it is permanent. You enter the token once, ever. In
Cowork the sandbox is per-conversation: the token persists for the whole
conversation (every run, across app restarts), and a brand-new conversation asks
for it once (a platform limit, not a bug). Keeping the token in a password manager
makes that one paste painless.

---

## Running in Cowork: allow the domains these tools use (one-time)

If you run these tools inside Cowork, the first run can fail before it scrapes
anything, with a network or connection error (often a `403` at `CONNECT`) and
**nothing charged**. This is not a limitation of the tool. Cowork sandboxes block
outbound network by default. Turn egress on and allow the specific domains these
tools reach. You do **not** need "All domains", and should not use it:

1. Open **Settings > Capabilities** in Cowork.
2. Turn on **Allow network egress**.
3. Add the domains listed below (not "All domains").
4. Re-run.

**The domains to allow: a small, fixed set.** Everything these tools actually
scrape (Trustpilot, Reddit, G2, Capterra, Google Maps, and the Meta / Google /
LinkedIn ad libraries) runs on Apify's own servers, so your sandbox never connects
to any of it and none of it needs an allowlist entry. What your sandbox itself
reaches is only:

- **Always:** the Apify API, where every scrape is launched and its data pulled:
  - `api.apify.com`
- **Free target resolution, before any spend:** the tools confirm your target
  against the real provider before charging anything:
  - `itunes.apple.com` and `apps.apple.com` (App Store)
  - `play.google.com` (Google Play)
  - `www.linkedin.com` (LinkedIn company lookup)
  - **your competitor's own website(s):** the domains you put in the config
    (e.g. `acmeplumbing.com`), used to find their Trustpilot page. These are the
    only entries that vary by run, and you already know them: add the ones you
    entered.
- **Ad creatives, after the scrape (ad-intelligence only):** it downloads each
  ad's image to read the copy off it. Those live on content networks:
  - `tpc.googlesyndication.com` (Google)
  - `media.licdn.com` and `dms.licdn.com` (LinkedIn)
  - `*.fbcdn.net` (Facebook, with each creative from a different subdomain, so
    allow the whole suffix as a wildcard)

**Why not "All domains".** These tools run entirely on *your* Apify account and
write only to your run directory, so "All domains" would give nothing away, but
it is needlessly broad, and the set above is genuinely all the sandbox touches.
Allowing exactly it keeps egress tight. The one wildcard, `*.fbcdn.net`, is there
only because Facebook serves each creative from a rotating subdomain; if an
environment cannot express a wildcard, Meta creative images will not download and
ad-intelligence reports their counts without visual analysis; every other tool is
unaffected.

Once egress is on and these domains are allowed, runs proceed normally. If a run
still fails at the network step, or an ad report comes back with counts but no
creative or visual analysis, check this list first: it is an environment switch,
never a sign the tool cannot run in Cowork.

---

## The run-directory convention (read this once)

Every run produces **many files**: raw scraped JSON per source, downloaded
creatives, intermediate counted-facts files, and the finished reports. To keep
that organised and reviewable, each run lives in its **own self-contained
directory**: its config file and all of its output in one folder, one folder per
run.

**Before any run, the skill asks you where that directory should live and what to
call it.** It proposes a name using this convention and you confirm or edit it:

```
<parent>/<tool>_<subject-slug>_<YYYY-MM-DD>/
```

- **`<parent>`:** a `marketing-research/` folder in your current working
  directory by default (or anywhere you choose).
- **`<tool>`:** `adintel`, `pain`, or `search`.
- **`<subject-slug>`:** the market or competitor, lowercased and hyphenated:
  `emergency-plumber`, `fitness-gyms`, `pm-software`.
- **`<YYYY-MM-DD>`:** the run date.

Examples:

```
marketing-research/adintel_emergency-plumber_2026-09-02/
marketing-research/pain_fitness-gyms_2026-09-02/
marketing-research/search_pm-software_2026-09-02/
```

Re-running the same subject on the same day appends `_v2`, `_v3`, so an earlier
run is never overwritten.

Inside that directory the skill puts:

- the config file (`AD_INTEL_CONFIG.json` / `PAIN_CONFIG.json` /
  `SEARCH_CONFIG.json`), copied from the plugin's example and filled in with you;
- every scraped-data folder and the finished report(s) the run produces.

The tool is invoked with `--workdir <run-dir>` and `--config <run-dir>/<config>`,
so nothing is ever written into the read-only plugin install.

---

## The money gate: always dry-run first

These tools spend real money on **your** Apify account. Every tool supports
`--dry-run`, which resolves your inputs, prints the full plan and a **cost
forecast**, and spends nothing. The skills always run the dry-run first, show you
the forecast, and wait for your go-ahead before the paid run. Never skip it.

**Every run is also hard-capped, automatically.** On top of the forecast, each
actor run is launched with a spend ceiling of about 1.5 times its forecast. That
ceiling is passed to Apify as a hard billing cap, and the tool also watches the
live charge while the run is going and stops the run itself if it ever crosses
the ceiling. This is what keeps a scraper that ignores its own limits from
running up a surprise bill: if a review scraper is told to fetch a small number
and tries to fetch far more, the cap stops the charge, not your card. You do not
configure this. It is on for every run.

**Set an account spend limit once, as a final backstop.** In the Apify console,
under Billing, you can set a maximum you are willing to spend in a period. Do it
once. It sits underneath everything above and catches anything unexpected. Apify
describes this account limit as an estimate that can run slightly over before it
takes effect, so treat it as a safety net rather than an exact stop, and keep the
per-run gate above as your real control.

**The dry-run also shows you what your inputs resolved to, and that matters as
much as the cost.** Before you approve a paid run, check that the tool found the
**right** target: the right company, the right app, the right competitor domain,
the right seed keywords. A company name can resolve to a different advertiser, a
misspelled domain can resolve to nothing. The dry-run surfaces the resolved
identity so you can catch a wrong target before you pay for it, not after.

**Reading a zero result correctly.** If a source comes back with zero results,
that has two very different meanings and the tools keep them separate:

- **The target did not resolve** (no company id found, a domain that does not
  exist, a market label that produced no keywords). This is a *miss*: the tool
  could not find what to look at. It is flagged at the gate, and the fix is to
  correct the input.
- **The target resolved fine, and there genuinely was no data** (a real company
  that runs no ads on that network, a real product with no reviews yet). This is
  a *fact* about the market.

Never treat the first as the second. "We could not find this company" is not the
same as "this company runs no ads," and the tools are built so you can always
tell which one you are looking at.
