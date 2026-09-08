# Maps location baseline: the known-good end-to-end reference

This folder is the canonical end-to-end baseline for the pain-and-messaging tool's
Maps source. It is an **A/B pair** that differs in exactly one field,
`maps_location_query`, and it pins the tool's behaviour when a user gives a
location Google Maps cannot geocode.

Two configs, one variable:

| Fixture | `maps_location_query` | Role |
|---|---|---|
| `runA_bad_location.json` | `"the Bay Area or Sacramento"` (a region + a list) | negative: a location that does not geocode |
| `runB_sacramento.json` | `"Sacramento, USA"` (one findable place) | positive control: proves data exists |

Everything else is identical: `sources: ["maps"]`, category discovery (mode 3) on
`"coffee shop"`, top 3 businesses queued, 20 reviews each.

## How to run

From the skill directory (`skills/pain-and-messaging/`):

```
python3 pain_intel.py --config examples/baseline/runA_bad_location.json --workdir /tmp/baseline_A
python3 pain_intel.py --config examples/baseline/runB_sacramento.json    --workdir /tmp/baseline_B
```

Only credential needed: an Apify token (env `APIFY_TOKEN`, or `~/.apify/auth.json`).

## What "pass" looks like: the durable assertions

The **behaviour** is the baseline, not the exact counts. Place and review counts
drift as Maps data and the market change; the assertions below do not.

**Run A (bad location): expect a caught false-zero, $0 spent:**
1. At the free resolution gate, before any spend, the caution prints:
   `[maps] Pinning results to: 'the Bay Area or Sacramento'` + the note that a
   region or list makes the whole run return nothing.
2. The Maps discovery actor (`compass/crawler-google-places`) runs and **FAILS**
   the geocode → `0 records saved` → `reported $0.000`.
3. The empty-corpus GUARD fires and **names the location as the first suspect**:
   `location: '...' may not be a single place Google Maps can find`.
4. The run exits (code 1) before the reviews stage. No LLM report.

**Run B (good location): expect a real corpus:**
1. Same gate caution, this time for `'Sacramento, USA'`.
2. Discovery succeeds → a full page of businesses → top 3 queued → reviews scraped.
3. The analysis half runs: the deterministic extractor writes
   `synthesis_facts.json`, then the report step writes `E1_pain_points.md` and
   `E2_trust_signals.md`.

The point of the pair: Run B proves Run A's zero was **false** (the market is full
of coffee-serving businesses); the failure was the location phrasing, not an
absence of data. A single run cannot distinguish "phrasing broke it" from "no data
exists"; the control can.

## Reference snapshot: 2026-09-03 (your Apify account)

The numbers below are one real run, kept as a reference point, not a frozen
contract.

- **Run A:** actor `[FAILED]`, 0 places, **$0.0000** charged. Gate caution + GUARD
  location line both fired live.
- **Run B:** 120 businesses discovered, top 3 queued (two McDonald's + a Black Bear
  Diner, see note), 60 reviews scraped. Extractor: 37 scored records (19 low-star
  / 18 high-star), 50 pain phrases, 21 trust phrases. E1 (28k chars) + E2 (16k
  chars) generated.
- **Charges (event ledger, `chargedEventCounts × pricingPerEvent`):** B2a $0.3602 +
  B2b $0.0182 ≈ **$0.378**. Both stages self-flag `⚠ underreports; verify on
  dashboard`: these actors pass a platform-usage portion through to the user that
  the event ledger cannot price, so the true bill is a little higher; read the
  Apify dashboard for the exact figure.

## Two things this baseline also teaches

- **A failed geocode is free.** These are pay-per-event actors, so a run that
  scrapes zero places generates zero billable events. The danger of a bad location
  was never the wasted dollar; it was the silent zero a user would misread as "no
  reviews exist here." Run A therefore doubles as a **$0 smoke test** of the guard,
  safe to run any time.
- **`budget_caps.phase1` is per-stage-subprocess, and must clear the forecast.**
  Each stage runs in its own `run_phase` process with a fresh budget, so a Maps run
  (B2a + B2b) can spend up to ~2× `phase1`, not one shared cap. And the value must
  sit **above** the B2a full-run forecast (~$0.43 for a 120-place discovery, from
  120 × $0.003 × 1.2 safety) or the pre-spend gate blocks the stage before it can
  even attempt the geocode. `0.60` is chosen for that headroom; do not lower it
  below the forecast or Run A stops for the wrong reason (budget block, not geocode
  failure) and the baseline no longer tests what it claims to.

## What this validates

The fix that surfaced non-geocodable Maps locations at the free gate could only verify the pre-spend caution by a $0 dry-run and honestly flagged the **empty-corpus GUARD line as verified by inspection, not by a live empty run**. Run A here exercises that GUARD line on a live failed run; the caveat is superseded; the fix is validated end to end.

## Note, not an assertion

Mode-3 discovery ranks businesses by review **count**, so `"coffee shop"` surfaces
high-review chains that serve coffee (McDonald's, a diner) over boutique cafés. That
is a relevance property of category discovery, orthogonal to the location behaviour
this baseline pins. It is recorded here so a reader is not surprised by the queued
names; it is not something these fixtures assert or test.

## Cost note

This baseline **spends real money** (~$0.38 for the pair, essentially all of it
Run B). It is a reference run, not something to wire into CI or auto-run; the run
stops and reports rather than retrying automatically, and re-running is your decision.
Run A alone is free and may be run as a $0 regression check of the caution and guard.
