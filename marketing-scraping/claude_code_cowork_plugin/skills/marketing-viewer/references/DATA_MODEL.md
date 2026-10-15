# The local app's data model (for the agent)

Read this before answering questions about what the user sees in the app. The app shows
one file per run, `<run>/frontend_index.json`, written by the research skill's index step
(`scripts/emit_index.py`). Everything the app displays comes from it, plus the user's marks
in `<run>/.viewer/`.

## 1. The index file

Top-level keys:

| key | meaning |
|---|---|
| `tool` | `ad`, `pain` or `search`: which skill made the run, and which page shows it |
| `run_id`, `subject` | the run folder name; the subject the run is about (groups runs into projects) |
| `generated_at` | when the index was built (a re-index changes it) |
| `config_summary` | the settings that matter for reading the data (country, sources, database) |
| `counts` | record totals: `by_platform` (ad, pain) or `by_kind` (search); `creatives_on_disk` (ad) |
| `synthesis` | the counted analysis the reports were written from (pain: pain points and trust signals with counts; search: keyword table, competitive map, caveats). Absent for ad runs. |
| `snapshots` | pain only: per-source rollups (for example a company's Trustpilot score) |
| `reports` | the written reports, `{id, title, path}` relative to the run folder |
| `records` | every scraped item, one envelope each (below) |

### A record (the envelope)

| field | meaning |
|---|---|
| `uid` | stable id, `<platform>:<native id>`. Use it for views and marks. |
| `uid_native` | `true` when the uid was built from a native (non-positional) platform id and is stable across re-scrapes of the same target. `false` when it fell back to list position -- that uid is only stable within this run. **Absent in indexes built by earlier versions: treat missing as unknown, never as native.** For cross-run matching, use `identity.py record_identity()` instead of reading this field directly. |
| `kind` | `ad`, `review`, `post` (Reddit), `keyword`, `serp`, `domain` |
| `platform` | `meta`, `google`, `linkedin`, `maps`, `trustpilot`, `reddit`, `semrush`, `google_serp` (and other review sources when configured: `appstore`, `googleplay`, `g2`, `capterra`) |
| `subject` | advertiser, business or subreddit the record is about |
| `text` | the ad copy, the review or post text, or the keyword. Quote it word for word. |
| `date`, `url`, `lang` | when, where it came from, language |
| `media` | ads only: downloaded creatives `{type: image or video, path}` |
| `tags` | the values the app filters on (below) |
| `data` | everything else, per tool (below) |
| `raw_ref` | `{file, index}`: where the record is in the raw scrape. The chain back to the source. |

**Ad records** (`tool: ad`)
- `tags.formats`: a list, for example `["IMAGE", "CAROUSEL"]`. An ad can count under two
  formats. DYNAMIC means Meta's dynamic creative: several versions, not a swipeable carousel.
- `tags.active`: `active`, `ended`, or `known_dates` (dates known, status not reported).
- `data`: `advertiser_name`, `headline`, `cta_text`, `link_url` (landing page), `ad_url`
  (the ad in its library), `first_shown`, `last_shown`, `duration_days`, `card_count` and
  `cards` (carousel copy), `display_format`, `publisher_platforms`, `page_like_count`,
  `page_category`, `paid_by` (LinkedIn payer), `audience_selections` (Google targeting),
  `impressions_text` (Meta range, when reported).

**Review and post records** (`tool: pain`)
- `tags.polarity`: `high` (the app says "positive"), `low` ("negative") or `mid`. From the
  star rating for reviews: 3 stars or fewer is `low`, 4 or more is `high`. So a 3-star
  review counts as negative in the app, in `viewer_ctl.py query --sentiment negative`, and
  in the pain report's low-star set. The code is `if s <= low_thresh: return "low"` in
  `emit_index.py`, with the settings `low_star_threshold` (default 3) and
  `high_star_threshold` (default 4). A half-star rating between the two (3.5 at the
  defaults) is `mid`: neither `low` nor `high`, so `--sentiment positive` and
  `--sentiment negative` both leave it out. For Reddit it is inferred from the wording,
  because Reddit has no stars; say so when it matters.
- `tags.banding`: Reddit's sentiment band (`high`, `low`, or `neutral` for the posts the
  banding set aside; those carry no `polarity`). A review with a whole-number star rating is
  never `neutral`. `tags.removed: true` on deleted Reddit posts.
- `data`: `stars`, `business_name`, `review_title`, `company_reply`, `verified`,
  `place_id`, `place_name`, `is_local_guide`, `reviewer_review_count` (Maps), `subreddit`,
  `is_comment`, `reddit_score`, `upvote_ratio`, `comments_count` (Reddit).
- `data.place_id` and `data.place_name` (Maps): the Google Maps place the review was
  collected from. The review card is labelled with `place_name` (unless it is blank,
  "unknown" or "n/a": then with `business_name`, else the subject, else "Google Maps").
  Each Google Maps overview card counts exactly the reviews whose cards show its title.
  When branches of a chain share one name, one card counts all the reviews with that name
  and says how many places share it, since their review cards all show the same name. A
  place whose reviews carry no stars still gets its card when other places have one, so
  no review is left out of the cards without the page saying why. Indexes built before
  these fields existed do not have them; their review cards show the subject instead.

**Search records** (`tool: search`)
- `keyword`: `data.volume`, `cpc_usd`, `competition` (0 to 1), `keyword_difficulty` (0 to
  100), `intents`, `volume_trend`; `tags.intent`, `tags.difficulty_band`. A keyword Semrush
  had no data for (no volume, or a volume of 0, and no cost per click, competition or
  12-month trend) carries `data.no_data: true` and no `data.volume` (the scraper's own 0 is
  not a measurement); the app shows it as "no data from Semrush". A keyword with a volume
  above 0 is measured whatever else is empty: it carries `data.volume` and no `no_data`, and
  its empty fields show as empty cells. Indexes built before this field existed do not have it.
- `serp`: one results page per query: `data.results` (position, domain, url), `serp_features`,
  `ai_overview`, `ad_examples`.
- `domain`: a site across all result pages: `keywords_owned`, `avg_position`, `best_position`.

## 2. What the app shows (the display rules)

These are applied on screen. `viewer_ctl.py query` applies the same ones, which is why you
count with it.

1. **Trashed records are not shown** (on the ad and review pages). The user trashed them;
   they are listed in Trash and can be restored.
2. **Deleted Reddit posts are not shown**: `tags.removed: true`, or text that is only
   `[removed]` / `[deleted]`. They stay in the data.
3. **Formats** come from `tags.formats`. An ad matches a format filter if any of its formats
   is ticked.
4. **Subreddits** are one name each: `r/acmefitness` and `acmefitness` are the same.
5. **Meta template tags** such as `{{product.brand}}` are hidden in the ad text on screen
   (Meta fills them per viewer). The raw text still contains them; do not quote the tags as
   copy.
6. **Run duration bands** (ads): under a week (`u1w`), 1 to 4 weeks (`1to4w`), 1 to 3 months
   (`1to3m`), over 3 months (`3mplus`), not reported (`unknown`).
7. **Text search** matches a lowercase substring across the text, subject, headline,
   advertiser, business name, Google Maps place name, button text and platform.
8. The search page shows everything (no trash, no filter panel); it has three tabs:
   keywords, rankings (serp), domains.

## 3. Pages, filters and addresses

| tool | page | filters (address parameter) |
|---|---|---|
| ad | Ad library | Platform (`platforms`), Advertiser (`advertisers`), Run duration (`durations`), Format (`formats`), Favorites only (`fav=1`), text (`q`), Saved view (`view`) |
| pain | Reviews | Source (`platforms`), Sentiment (`polarities`: high / low / neutral), Subreddit (`subreddits`), Favorites only, text, Saved view |
| search | Search landscape | tabs only |

Address shape: `http://127.0.0.1:<port>/#/run/<run id>?platforms=reddit&polarities=low&q=price`.
Build it with `viewer_ctl.py url`; it checks every value exists in the run.

## 4. The app's own files (`.viewer/`)

| file | written by | content |
|---|---|---|
| `<run>/.viewer/annotations.json` | the app and `viewer_ctl mark` | `favorites {uid: true}`, `notes {uid: text}`, `trashed {uid: true}` |
| `<run>/.viewer/views.json` | `viewer_ctl save-view` | `views: [{id, title, note, uids, created_by, created_at, updated_at}]` |
| `<run>/.viewer/carried.json` | `viewer_store.ensure_carried`, on the first read of a run that has `repeat_of` | `carried_from`, `carried_at`, `favorites_carried`, `notes_carried`, `trash_carried`, `views_carried`, `conflicts`, `skipped [{uid, kind, text, reason, mapped_to?}]`, `skipped_views [{view_id, title, reason}]`; on failure also `error` and `error_plain` |
| `<root>/.viewer/current_view.json` | the app, on every page or filter change | page, route, run, filters, visible `{count, listed, uids}`, search tab |
| `<root>/.viewer/server.json` | the running app | `url`, `port`, `pid`, `root`; removed when it stops |

Never write these by hand (see the skill's hard limits).

**Marks carried forward.** The first time a run with `repeat_of` is read (by the app or
by `viewer_ctl`), the user's favorites, notes, hidden records and saved views from the
previous run are copied onto the same records in this run, once, and `carried.json` records
what happened. Records are matched only by stable identity (`identity.record_identity`); a
record without one, or matching more than one record, is never guessed at and is listed in
`skipped`. A mark already in this run always wins. Carrying is transitive (a later month
first makes sure the month before it has carried). A run without `repeat_of` gets no files.

## 5. Worked examples

**"How many of these mention the price?"** (user on the Acme Fitness reviews page)
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/marketing-viewer/scripts/viewer_ctl.py" current-view --root marketing-research
#  -> run pain_acme-fitness_2026-08-28, filters {platforms: [reddit], polarities: [low]}, visible 17 of 128
python3 "${CLAUDE_PLUGIN_ROOT}/skills/marketing-viewer/scripts/viewer_ctl.py" query --root marketing-research --run pain_acme-fitness_2026-08-28 \
    --platform reddit --sentiment negative --text price --limit 10
#  -> count N, with the records' text to read and quote
```
Answer with the count and two or three verbatim quotes. Offer a saved view of them.

**"Show me only the ones about the Guide."**
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/marketing-viewer/scripts/viewer_ctl.py" save-view --root marketing-research --run pain_acme-fitness_2026-08-28 \
    --title "Negative about the Guide" \
    --note "Every negative review or post that mentions the Acme Fitness Guide." \
    --sentiment negative --text guide
```
Tell the user it is under "Saved views" on the left; clicking it shows only those records,
with your note at the top.

**"Which competitors run carousels, and for how long?"** (ad library)
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/marketing-viewer/scripts/viewer_ctl.py" query --root marketing-research --run adintel_acme-plumbing_2026-08-27 --format CAROUSEL --limit 20
```
Read `subject` / `data.advertiser_name` and `data.duration_days` of each; answer per
advertiser. A value the run does not have (a misspelled advertiser) is refused with the
list of valid names; pick the right one rather than guessing.

**A theme across the text that no filter captures** ("complaints about delivery")
Read the records' `text` (from `frontend_index.json` or `query --limit`), decide which are
about delivery, and save exactly those ids: `save-view --uids-file picked.json`. Say in the
note that you picked them by reading, so the user knows it is a judgment, not a filter.

---

## 6. Compare output (`/api/compare` and `viewer_ctl compare`)

Design rule (FROZEN): comparison is plain code, one module (`compare.py`), no model. The result is
the same whether the agent calls `viewer_ctl compare` or the app calls `/api/compare`.

### Output shape

```json
{
  "run":                   "pain_acme-fitness_2026-10-01",
  "against":               "pain_acme-fitness_2026-09-01",
  "basis":                 "repeat_of",
  "tool":                  "pain",
  "settings_differ":       false,
  "differing_settings_keys": [],
  "platforms_only_in_one_run": [],
  "by_platform": {
    "maps": {
      "new":        [{"uid": "maps:abc123"}],
      "gone":       [{"uid": "maps:xyz789"}],
      "changed":    [{"uid": "maps:def456", "changed_fields": ["data.stars"], "label": null}],
      "unchanged":  [{"uid": "maps:ghi012"}],
      "not_comparable": 3,
      "cannot_tell": [],
      "cannot_tell_reason": "",
      "cannot_tell_counts": { "gone": 0, "new": 0 }
    }
  },
  "totals": { "new": 1, "gone": 1, "changed": 1, "unchanged": 1, "not_comparable": 3 }
}
```

**`cannot_tell`** (per-platform list, positional-uid guard): categories suppressed because
records with no stable cross-run identity prevent reliable assertion. Possible values:
`"gone"` (this run has positional records on this platform, so the removed count is omitted) and/or
`"new"` (the baseline has positional records, so the new count is omitted). When non-empty, the
corresponding list in `by_platform` is `[]` and the totals reflect `0` for that category.
The strip shows `cannot_tell_reason` in the per-platform row in plain language (e.g.
"20 Google Maps reviews in this run could not be matched to the previous run, so we
can't count which ones are no longer there."). The agent reads this field directly.
`cannot_tell_counts` holds the raw positional-record counts: `gone` is the number of
positional records in this run, `new` is the number in the baseline. Both are always
present (zero when the category is not suppressed).

**`month_sizes`**: how big each month is, in numbers that add up
with the change counts. It sits before `by_platform` in the output.

```json
"month_sizes": {
  "this_month":     {"run": "...", "records": 130, "compared": 129, "left_out": 1,
                     "left_out_reasons": [{"count": 1, "reason": "1 removed Reddit post (deleted by the author or a moderator) is not compared"}],
                     "sentence": "This month has 130 reviews: 129 are compared (new, changed or unchanged); ..."},
  "previous_month": {"run": "...", "records": 130, "compared": 128, "left_out": 2, "...": "..."}
}
```

- `records` is the length of the month's `frontend_index.json` records.
- `this_month.compared` = new + changed + unchanged; `previous_month.compared` =
  gone + changed + unchanged (from `totals`).
- `left_out` = records - compared, and the `left_out_reasons` counts sum to it. Each
  reason is one bucket: unreadable entries, removed Reddit posts (pain), records moved
  to the trash, a source the other month does not have, no matchable id, new or gone
  that cannot be told (`cannot_tell`), or (previous month) a match to a record trashed
  in this month. A baseline record in the trash that this month's changed/unchanged
  already counts is counted as compared. A reason that says "could not be paired one
  to one" means a case this breakdown does not foresee; it is reported, never hidden.
- With no baseline, `previous_month` is null and every record of this month is left out
  with the reason "there is no earlier month to compare with". When earlier months of
  the project exist but every one stopped before it finished, the reason says that
  instead (they are named, none is used as a baseline, one can still be named by hand).

It only counts: which records `new` / `gone` / `changed` / `unchanged` hold is unchanged.

**`summary`**: one ready-to-quote answer, just before `month_sizes`:
this month's `sentence`, then "Of the N compared: A new, B changed, C unchanged. D
{records} from the previous month are no longer there." Its only denominator is
`this_month.compared`, so the numbers in it add up. With no baseline it is this
month's `sentence` alone. The skills tell the agent to open its answer with it.

### Baseline selection (the order may change)

1. **explicit `against`** passed in the call
2. **`repeat_of`** field in the run's config: the run that this one repeats
3. **same tool + subject, previous by run order** (folder date, then `_vN` version, then
   `generated_at`, then `run_id` as tie-breaker)
4. **no baseline** (`against: null`, `basis: null`) -- the run has no predecessor

A month that stopped before it finished (its status file in the run folder says `incomplete`, as a
scheduled run or a run by hand stopped at the spending cap leaves it) is never chosen by
rules 2 and 3. Three optional keys of the result say so; each is present only when it is
true, and the `summary` opens with a sentence for it:

- `skipped_partial`: the stopped months passed over while choosing (the one `repeat_of`
  names, or later months of the same project), or every earlier month when none is left.
- `run_partial`: the run itself stopped, so a record it lacks may simply not have been
  collected and the `gone` count may only be a gap. The `new` and `changed` counts describe
  records the run has and are not affected.
- `against_partial`: the baseline was named by hand (`against`) and stopped early.

### Change categories

| Category | Meaning | Uid owner |
|---|---|---|
| `new` | In this run, not in the baseline | This run |
| `gone` | In the baseline, not in this run. Label: "not in this run" (never "deleted") | Baseline |
| `changed` | In both; one or more key fields differ | This run |
| `unchanged` | In both; all key fields identical | This run |
| `not_comparable` | Count only: record has no stable cross-run identity | N/A |

### Key fields per tool

| Tool / kind | Key fields compared |
|---|---|
| `pain` | `data.stars`, `text` |
| `ad` | `tags.active`, `data.last_shown`, `data.headline`, `text` |
| `search` / `keyword` | `data.volume`, `data.cpc_usd`, `data.keyword_difficulty` |
| `search` / `serp` | Ordered list of result domains (position change counts) |
| `search` / `domain` | Not compared field-by-field (roll-up, recomputed) |

Ad-specific label: when `tags.active` changes from `active` to `ended`, the `changed` entry
carries `"label": "stopped running"`.

### Platforms only in one run

Platforms present in only one run are listed in `platforms_only_in_one_run` and are NOT
counted as new or gone. This is a settings change (the user added or removed a source),
not a world change.

```json
"platforms_only_in_one_run": [
  {"platform": "reddit", "in_run": "this"}
]
```

### `change=` URL parameter

| Value | What the app shows |
|---|---|
| `change=new` | Records in this run with `basis = "new"` |
| `change=changed` | Records in this run with `basis = "changed"` |
| `change=gone&vs=<newer_run_id>` | Baseline run's records not in the newer run |

For `gone`: the URL opens the **baseline** run with `vs=<newer_run>`. The app fetches
`/api/compare?run=<newer_run>&against=<baseline>` and filters the baseline's records to
the gone uids. The agent uses `viewer_ctl url --change gone` which builds this URL.
