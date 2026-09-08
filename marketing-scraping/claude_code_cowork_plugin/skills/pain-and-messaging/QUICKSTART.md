# QUICKSTART: Pain and Messaging Mining Tool

## What this does

This tool scrapes customer reviews of your competitors, mines the low-rated reviews for customer pain language, and mines the high-rated reviews for trust signals. It produces two analyst reports: **E1** (a tiered pain-point taxonomy with a verbatim language bank and a pain-to-promise map) and **E2** (trust signals from positive reviews).

It reads from up to **seven review sources, and you choose which ones to use**: **Google Maps** (the default), **Trustpilot**, the **Apple App Store**, **Google Play**, **Reddit**, **G2**, and **Capterra**. Every source you pick flows into one combined corpus, and the report discloses how many reviews came from each. Google Maps on its own is a complete run; the other six are there for markets where your competitors live somewhere other than Maps: SaaS on G2 and Capterra, mobile apps on the App Store and Google Play, and open discussion on Reddit.

Every finding is counted in code and sourced to a real quoted review before any LLM runs. The LLM only clusters and labels what the code already found. That means you can verify any claim in the report against the source quote, and you can stand behind it.

**What you need:**

- Python 3, standard library only. No `pip install`.
- An Apify account and its API token. Apify is where the scraping runs.
- The `claude` CLI on your PATH. This is what writes the E1 and E2 reports. No separate LLM API key is needed.

Before running, take a look at `examples/E1_pain_points_SAMPLE.md` to see what a finished E1 report looks like.

---

## 1. Setup

### Get your Apify token

Log in to [console.apify.com](https://console.apify.com), go to Settings, then Integrations, and copy your API token.

The tool finds your token in one of two ways:

**Option A: a `.env` file in the `scripts/` folder**

```
cp scripts/.env.example scripts/.env
```

Open `scripts/.env` and paste your token after the equals sign:

```
APIFY_TOKEN=apify_api_your_token_here
```

Then load it into your shell before running:

```
set -a; source scripts/.env; set +a
```

**Option B: the Apify CLI login**

If you have the `apify` CLI installed and have already run `apify login`, the tool reads `~/.apify/auth.json` automatically. Nothing extra is needed.

**Option C: save an exported token once**

If you only have the token in your current shell (no CLI, no shell profile), save it so every later run reuses it:

```
export APIFY_TOKEN=apify_api_your_token_here
python3 ${CLAUDE_PLUGIN_ROOT}/skills/pain-and-messaging/scripts/config.py --persist-token
```

It reads the token from the environment (never the command line), merges it into `~/.apify/auth.json` without disturbing an existing `apify login`, and prints only a masked confirmation. Note the Option A `.env` route only applies when you run from a checkout; an installed plugin's folder is read-only.

### Create your config file

```
cp PAIN_CONFIG.example.json PAIN_CONFIG.json
```

Open `PAIN_CONFIG.json` in any text editor. Sections 2 through 4 below walk you through filling it in.

---

## 2. Choose your review sources

`PAIN_CONFIG.json` has a `sources` field. It defaults to Google Maps only:

```json
"sources": ["maps"]
```

To add a source, put its name in `sources` **and** fill its subject key. For example, to mine Google Maps, Trustpilot and Reddit together:

```json
"sources": ["maps", "trustpilot", "reddit"],
"trustpilot_domains": ["competitor.com"],
"reddit_queries": ["competitor name complaints"]
```

| Source | `sources` name | Subject key to fill | What to put there |
|---|---|---|---|
| Google Maps | `maps` | one of the three input modes in Section 3 | competitor Maps listings, names, or a category |
| Trustpilot | `trustpilot` | `trustpilot_domains` | the domain Trustpilot lists the company under (e.g. `"company.com"`) |
| Apple App Store | `appstore` | `appstore_app_ids` | numeric App Store id (e.g. `"389801252"`) or the app's App Store URL |
| Google Play | `googleplay` | `googleplay_app_ids` | package name (e.g. `"com.company.app"`) or the Google Play URL |
| Reddit | `reddit` | `reddit_queries`, `reddit_subreddits`, and/or `reddit_urls` | search terms, subreddit names, or direct post/subreddit URLs |
| G2 | `g2` | `g2_urls` | a G2 product reviews URL or a bare product slug |
| Capterra | `capterra` | `capterra_urls` | a Capterra product URL of the form `/p/<id>/<slug>/` |

An unknown source name stops the run at startup with a clear error, so a typo can never quietly scrape nothing.

**Reddit is the one exception: it has no star ratings.** The tool infers pain from praise by reading the language of each post and comment, not the upvote count (upvotes measure agreement, not sentiment). Genuinely neutral discussion, such as questions, news and jokes, is set aside rather than forced into pain or praise, and Reddit findings are labelled directional. **G2 and Capterra are star-rated**, so they behave like Maps.

---

## 3. Google Maps: choose your input mode

When `maps` is one of your sources, choose how you point the tool at the listings. All three modes produce the same Maps review corpus. Fill exactly one of the three input keys and leave the other two as empty arrays.

### Mode 1: you have the Google Maps URLs

Use this when you already know which Maps listings you want. It is the fastest path because it skips the discovery step entirely.

```json
"competitor_place_urls": [
  "https://www.google.com/maps/place/Planet+Fitness/@41.88,-87.62,17z/data=...",
  "https://www.google.com/maps/place/Anytime+Fitness/@41.89,-87.63,17z/data=..."
],
"competitor_names": [],
"maps_search_queries": []
```

Find a place URL by searching the business on Google Maps and copying the address-bar URL.

### Mode 2: you know the names

Use this when you have a list of competitor names. The tool searches Maps for each name and picks the best-matching listing, skipping aggregator results. Every named competitor is kept regardless of how many reviews it has.

```json
"competitor_place_urls": [],
"competitor_names": [
  "Planet Fitness",
  "Anytime Fitness",
  "Crunch Fitness"
],
"maps_search_queries": []
```

After the discovery step, the tool prints which Maps listing it matched to each name. Run with `--dry-run` first (Section 5) to review the matches before spending money on review scraping.

### Mode 3: category discovery

Use this when you want to find the busiest businesses in a category and location. The tool searches by category and location, sorts results by review count, and queues the top `review_target_count` businesses.

```json
"competitor_place_urls": [],
"competitor_names": [],
"maps_search_queries": ["fitness gym chicago", "gym membership chicago"],
"vertical": "fitness gym",
"city": "Chicago"
```

---

## 4. Fill the config

### Required

**`country_code`** (string): two-letter ISO country code, for example `"us"` or `"gb"`. This is the only field the tool requires. It exits with an error if it is missing.

### Place and category context

**`country`** (string, no default): full country name, for example `"USA"`. Used as location context alongside `city`.

**`city`** (string, no default): the city name. Used by Maps modes 2 and 3 for location anchoring and by the default `maps_location_query`.

**`vertical`** (string, no default): the business category, for example `"fitness gym"`. Used by Maps mode 3 when `maps_search_queries` is absent.

**`language`** (string, default `"en"`): the review language to scrape.

### Review volume

**`max_reviews_per_place`** (integer, default `20`): how many Google Maps reviews to scrape per business. For a real pain-and-trust analysis, 100 to 200 per business gives a solid corpus. The default of 20 is sufficient for a test run.

**`review_target_count`** (integer, default `60`): Maps mode 3 only. How many businesses (sorted by review count) to queue for review scraping. In mode 2 the tool always keeps all named competitors, regardless of this value.

**Per-source volume caps** (all optional, with sensible defaults): `app_max_reviews` (default `100`, App Store and Google Play), `saas_max_reviews` (default `100`, G2 and Capterra), `reddit_max_posts` (default `50`) and `reddit_max_comments_per_post` (default `20`).

### Location anchor: read this before running Maps mode 2 or mode 3

**`maps_location_query`** (string): the geographic anchor the Maps discovery actor uses to resolve search results. It must be a single geocodable place, for example `"Chicago, USA"` or `"London, UK"`. If the value cannot be geocoded as a single place, the actor fails the entire run with "LOCATION NOT FOUND."

Three behaviors depending on what you set:

- Absent (key not in your config): defaults to `"city, country"` constructed from your other fields.
- `""` (empty string): omits the location anchor entirely and lets your search strings carry the location. Use this when `maps_search_queries` already contains a city name, such as `"fitness gym chicago"`.
- A non-empty string like `"Chicago, USA"`: uses exactly that value.

If you get "LOCATION NOT FOUND," the value you set is not a single geocodable place. Replace it with a specific city and country, or set it to `""`.

### Privacy

**`personal_data`** (boolean, default `false`): keep this false. The pain and trust analysis needs review text and star ratings, not reviewer identities. Setting it true pulls reviewer names and profile URLs that this tool does not use.

### Report generation

**`report_model`** (string, default blank): the model alias your `claude` CLI uses to write the E1 and E2 reports. Blank means the tool uses `sonnet`. The clustering and writing step is a judgment task, so do not set this to the cheapest model you have. Override with a specific alias such as `"opus"` only if you have a reason.

Report writing is not put on a clock. A capable model writing a full report can take several minutes, and that runtime varies from run to run, so the tool lets it finish rather than cutting it off. This is a non-deterministic step where slowness is normal, unlike the Apify scraping calls, which are bounded.

### Spend control

**`budget_caps.phase1`** (number, default `10.0`): the hard spend ceiling in USD for the Apify scraping phases. The tool prints a cost forecast before each Apify stage and stops if the estimate would exceed this cap. For a handful of competitors at 100 reviews each, you can expect roughly $0.50 in Maps costs. Raise the cap if you are running a large source set.

### Advanced

**`stages`** (array, default empty): which Maps sub-stages to run. Leave empty to run the full pipeline in order. Set to `["B2a"]` (Maps place discovery) or `["B2b"]` (Maps review scraping) to run only one, for example when re-running after a partial failure. The non-Maps sources each run as their own stage automatically when they are listed in `sources`.

---

## 5. Dry-run first

Before spending anything on Apify, run the tool in dry-run mode. It resolves your subjects, prints the full plan and a cost forecast, and exits without calling Apify.

```
python3 pain_intel.py --config PAIN_CONFIG.json --workdir my_run/ --dry-run
```

In Maps mode 2, the dry run shows you which search strings will be sent to Maps for each competitor name. Review that output and confirm the names look right before running for real.

---

## 6. Run it

```
python3 pain_intel.py --config PAIN_CONFIG.json --workdir my_run/
```

Run this from the tool root directory. The tool creates `my_run/` if it does not exist. You can name the workdir anything; a descriptive name is useful if you run against multiple markets.

The tool runs in two halves:

**Acquisition:** each source you listed in `sources` is scraped in turn. For Google Maps that is two steps (a discovery step finds the businesses, then a review step scrapes them); the other sources scrape directly from the subject keys you filled. A cost forecast prints before each Apify stage. For Reddit, the raw post-and-comment corpus is banded into pain, praise and set-aside neutral right after the scrape.

**Analysis:** the extractor counts and sources pain and trust phrases in code across the whole combined corpus, with no LLM, and writes `synthesis_facts.json` (including a per-source breakdown). Then the `claude` CLI writes E1 and E2 from those pre-counted facts.

Report generation takes a few minutes per report. If the `claude` CLI is not installed or not on your PATH, the tool still completes acquisition and extraction and leaves `synthesis_facts.json` on disk. You can regenerate the reports later at no Apify cost by re-running.

---

## 7. What you get

All outputs land in the workdir you specified. Which review folders appear depends on the sources you chose:

| Path | What it contains |
|---|---|
| `B2a_maps_places/` | Raw Maps business listings from the discovery stage (Maps only) |
| `B2b_reviews/` | Raw review records scraped from Google Maps |
| `trustpilot_reviews/` | Raw Trustpilot review records |
| `appstore_reviews/` | Raw Apple App Store review records |
| `googleplay_reviews/` | Raw Google Play review records |
| `reddit_reviews/` | Raw Reddit posts and comments |
| `g2_reviews/` | Raw G2 review records |
| `capterra_reviews/` | Raw Capterra review records |
| `synthesis_facts.json` | The sourced, pre-counted pain and trust findings: each phrase with its count, star distribution, supporting verbatim quotes, and a per-source breakdown. This is the intermediate layer the reports are built from. |
| `E1_pain_points.md` | Pain-point taxonomy, verbatim language bank, pain-to-promise map |
| `E2_trust_signals.md` | Trust signals from high-star reviews |

### How to read the reports

The E1 report opens with the review counts: total reviews scraped, the low-rated count (the pain corpus), a per-source breakdown, and whether the findings are labeled statistical or directional. Read those numbers before reading the findings.

The pain corpus is the low-rated subset: reviews at three stars or below for the star-rated sources, and for Reddit the posts and comments the tool banded as complaints. For most businesses this is a small fraction of total reviews. When N is small, the report labels the findings directional, not statistical. That label is there on purpose. A directional finding sourced to real customer language is still useful for writing ad copy or sharpening positioning, but it is not a survey result. Do not treat it as one.

For a finished example, see `examples/E1_pain_points_SAMPLE.md`.

---

## 8. Cost and the forecast gate

Every source is billed per event on Apify. The tool prints a forecast before each stage and stops if the projected cost would exceed `budget_caps.phase1`. The rates below include Apify platform overhead and a safety margin; your first real run recalibrates the estimate, per source, for your own account.

- Google Maps: roughly $0.003 per business discovered, then roughly $0.00045 per review
- Trustpilot: roughly $0.0005 per review
- Apple App Store: roughly $0.0001 per review
- Google Play: roughly $0.0001 per review
- Reddit: roughly $0.0018 per result (a post or a comment)
- G2: roughly $0.0035 per review
- Capterra: roughly $0.0009 per review

The forecast reads the per-event billing figures, not the platform resource cost field, which understates the real charge for these actors.

---

## 9. Troubleshooting

**"No Apify token found"**

The tool did not find a token in `APIFY_TOKEN`, `scripts/.env`, or `~/.apify/auth.json`. Either run `apify login`, or copy `scripts/.env.example` to `scripts/.env`, fill in your token, and run `set -a; source scripts/.env; set +a` before running the tool.

**"LOCATION NOT FOUND" error**

The value in `maps_location_query` is not a single geocodable place. Change it to a city and country such as `"Chicago, USA"`, or set it to `""` to omit it and let your search strings carry the location.

**A source returned nothing**

Check that the source name is in `sources` and that you filled its subject key (Section 2). Each source also warns at run start if its subject key is empty. Then check the source-specific cause:

- Maps mode 1: verify that the URLs are valid Google Maps place URLs (not Yelp, not a search result page).
- Maps mode 2: check that the names are findable on Maps. Inspect `B2a_maps_places/` in the workdir to see what the actor returned, and verify the best-match selection resolved correctly.
- Maps mode 3: check that discovery ran and produced places before review scraping ran. If `maps_search_queries` is empty and `vertical` or `city` is also empty, discovery has nothing to search and returns zero results.
- Trustpilot, G2, Capterra: confirm the domain or product URL is the one the site actually uses for that company.
- App Store, Google Play: confirm the app id or package name resolves to a live listing.
- Reddit: confirm your queries, subreddits or URLs return posts. A narrow query on a quiet subreddit can legitimately return very little.

**E1 and E2 reports were not generated**

The `claude` CLI was not found on your PATH, or it errored. Install the CLI and confirm it is reachable as `claude`. Note that report writing is not timed out: a capable model can take several minutes per report, and that is expected, so let it run rather than assuming it is stuck. If a report is genuinely missing, the corpus and `synthesis_facts.json` are already on disk, so re-running repeats only the free analysis step.

**Blank browser windows open while reports generate, or report generation stalls for many minutes**

You should never see this with the tool as shipped. The report step runs the `claude` CLI internally, and it runs it deliberately stripped down: with its MCP servers switched off and its tools disabled, so it can only write text. That is what keeps report generation a quiet, fast, text-only step.

If you do see blank browser windows opening, or a report call hanging for minutes before any output, it means that internal call is loading your machine's full CLI environment when it should not be. On a machine with browser or other CLI integrations installed, a plain `claude` call boots all of them, which opens windows and adds minutes of startup to every report. Two things cause this, and neither is a normal operator action:

- **You are running an old or modified copy of the tool.** The guard that switches those integrations off lives in `scripts/report.py`. Update to the current version, or restore that file.
- **You tried to write a report by piping the prompt into `claude` yourself.** Do not do that. Let `pain_intel.py` generate the reports; it invokes the CLI with the right isolation. Running `claude` by hand on the prompt loses that isolation and is what pulls in the browser integrations.

The corpus and `synthesis_facts.json` are already on disk either way, so once you are back on a clean copy you can regenerate the reports at no Apify cost by re-running.
