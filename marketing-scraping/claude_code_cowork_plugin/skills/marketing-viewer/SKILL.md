---
name: marketing-viewer
description: Use whenever the user wants to SEE, BROWSE or WORK THROUGH marketing-scraping results in the local app rather than read a report, or asks what changed between runs; it is also the default last step of ad-intelligence, pain-and-messaging and search-intelligence (run it after every scrape unless the user declined it for that run). Triggers include "open the app", "open the viewer", "show me the ads", "let me look through the reviews", "browse my results", "what am I looking at", "filter these for me", "how many of these mention X", "save these as a view", "star this one", "what changed since last month", "compare with last month", "what's new since the last run". It starts (or reuses) a small local web app on the user's own computer showing every scraped ad, review, Reddit post and search result plus the written reports, counts month-to-month changes in code, and lets you work alongside the user in it. It never scrapes, never spends money and never edits scraped data. Local mode only.
version: 1.6.0
---

# marketing-viewer (the local app)

**Run the commands below from the folder the user works in, without `--root`; if the
tool says no research folder was found, ask the user.** Without `--root`, every command
uses `marketing-research/` in the current folder, which is where the three research
tools save their runs. Never search the disk for a research folder.

A local web app that shows everything the three research skills collected: ads as an ad
library (with the downloaded images and videos), reviews and Reddit posts as cards, search
data as tables, and the written reports. It runs on the user's computer, reads only files
already on disk, and sends nothing anywhere.

You have two jobs here. **Open it** so the user ends up looking at their results without
touching a terminal, and **work alongside them in it**: when they ask about what they are
looking at, find out what that is, answer from the same records the app shows, and put the
answer in front of them as a saved view or a mark.

The field-by-field data model, the display rules and worked examples are in
`${CLAUDE_PLUGIN_ROOT}/skills/marketing-viewer/references/DATA_MODEL.md`. Read it before
your first co-working answer in a session.

Everything below goes through one tool (Python 3.9 or newer, standard library only, nothing to install):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/marketing-viewer/scripts/viewer_ctl.py" <command> ...
```

Write the full command every time. A shell variable holding it does not carry from one
command to the next, and zsh (the macOS default) would not split it into words. In the
text below, `viewer_ctl.py <command>` means that full command.

Every command prints JSON. A refusal prints `{"error": "..."}` and exits with code 2; read
the message, it says what to do.

## Hard limits (and why)

- **Never edit `frontend_index.json`, a report, or any scraped file.** The app is a trust
  surface: every card traces back to a real scraped record. A "fixed" record breaks that
  chain silently. The one exception is `viewer_ctl.py reindex`, which rebuilds the index from the raw
  data with the pipeline's own code (free, deterministic).
- **Never write files under `.viewer/` yourself; always use `viewer_ctl.py`.** It checks every
  record id against the run, takes a lock, and writes atomically, so a click in the app and
  your command in the same second cannot erase each other. A hand-written file can.
- **Never hide (trash) records for the user.** Trash removes records from their view; only
  they decide that. You can favorite, add notes and save views.
- **Never scrape or spend money from here.** New data comes only from the three research
  skills, behind their dry-run money gate.
- **Never state a count the app would not show.** Count with `viewer_ctl.py query`, which applies
  the app's own rules (trashed records and deleted Reddit posts are not shown, formats and
  subreddits are grouped the way the app groups them). A count from reading the JSON by
  hand can differ from the screen, and the user will see the difference.

## When this runs

- **At the end of every run** of ad-intelligence, pain-and-messaging or
  search-intelligence (their last step sends you here). Skip it only if the user said they
  do not want the app for this run; then tell them in one sentence how to open it later
  ("say 'open the app' any time").
- **Whenever the user asks** to see, browse or work through results.

## Step 1: Check that the app can work here

The app serves a folder on the user's computer, so it only makes sense where that folder
lasts: **local Claude Code, or the local Claude desktop app (including local Cowork)**.
In a cloud sandbox (Claude Code on the web, or Cowork running in the cloud) the folder is
gone when the session ends. If you know you are in one, do not start the app; tell the user
plainly that the app runs on their own computer and that the results and report are still
in the run folder.

If you are not sure, start it (Step 3). If the app starts but cannot be shown anywhere
(Step 4 fails completely), say so honestly and give the address.

## Step 2: Choose what the app shows (the root folder)

The app shows every run folder it finds under its root folder, grouped into projects
by subject.

- **Normally, leave out `--root`.** The tool then uses `marketing-research/` in the
  folder you run it from, the same folder the research tools save their runs in.
- **Add `--root "<folder>"` only when the research lives somewhere else:** a research
  run you just finished saved its folder under a different parent (use that parent), or
  the user tells you where their research is, or they want one client or one run only.
  Then pass the same `--root` to every command in the session.
- **If a command answers that no research folder was found,** ask the user where their
  research folder is. Never search the whole disk, other folders, or Claude's own
  session files or memory for it (no `find /`, no `find ~`, no searching `~/.claude`).
  Why: that is the user's private data, and they know where their research is.

## Step 3: Start it, or reuse the one already running

If the research folder (`marketing-research/`, or the `--root` folder) has a
`.marketing-scraping` folder, its research is also kept in the user's GitHub
repository for a repeat in the cloud, and new months reach this computer only by
an update. Run this first, and tell the user if new run folders arrived (if it
refuses, tell them its `message` and say newer months may be missing):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/marketing-viewer/scripts/research_sync.py" pull \
    --root "<the research folder>"
```

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/marketing-viewer/scripts/viewer_ctl.py" start
```

This starts the app in the background (it keeps running after the command returns) or,
if one is already running for that folder, returns its address (`"reused": true`). It
never starts a second copy. The answer includes `url`, for example
`http://127.0.0.1:53817/`.

To open a specific run, get its address:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/marketing-viewer/scripts/viewer_ctl.py" url --run "<run folder name>"
```

If `start` fails, it prints the end of the app's log. Common causes: no Python 3.9 or newer, or
the folder is not readable. Say what failed in plain words.

## Step 4: Open it (built-in browser first)

1. **The built-in browser, up to 3 attempts.** If you have a browser built into the app you
   run in (in the Claude desktop app this is the Browser pane, tools named like
   `mcp__Claude_Browser__*`: open the address with its preview-start-with-URL or navigate
   tool), open the address there. After each attempt, check that it loaded: read the page
   text and look for "Scraped marketing data". If it did not load, run `viewer_ctl.py status` (restart with `start` if it says not running) and try again. Three failed
   attempts, or no built-in browser at all, moves you to 2.
2. **The user's default browser:**
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/skills/marketing-viewer/scripts/viewer_ctl.py" open-default-browser --run "<run folder name>"
   ```
   `"opened": true` means their browser was asked to open it. Do not drive the user's own
   browser with other tools for this: the app opening in a normal tab is enough.
3. **Neither worked:** give the user the address to paste into a browser, and say plainly
   that you could not open it for them.

## Step 5: Explain it (for someone who is not technical)

Keep it short, in plain words, and adapt it to what they ran. Something like:

> Your results are open in a small app that runs only on your computer. Nothing is uploaded.
> On the left are your projects; click a run to see what was collected. Each page has
> filters on its left side (source, sentiment, format and so on) and a search box. You can
> star a card, add a note to it, or move it to the trash; nothing is ever deleted and you
> can restore anything from Trash. The "report" button at the top opens the written
> analysis. While it is open, you can ask me about whatever you are looking at, for example
> "how many of these mention price?", and I can save a view for you that shows exactly
> those records.

Mention one or two things specific to this run (for example "the ad library has 99 ads
from 3 platforms; the videos play in the page").

**How big the run is:** take it only from `run_size` in the `url` or
`open-default-browser` answer. Quote its `sentence`, or use its numbers: `shown` is
what the app lists for the run, `shown_by_platform` splits exactly that number by
source, and `left_out_reasons` says what is not shown and why (for example "1 removed
Reddit post (deleted by the author or a moderator) is not shown"). Never mix in a
number from another output, such as the run log's per-source lines (`[reddit] 15
records` counts the posts Reddit returned, before comments are added and removed posts
are left out) or `list-runs`: joined, they do not add up. The closing size sentence a
tool prints at the end of a run states the same numbers as `run_size`: quote one of
them, never add or combine the two.

## Working alongside the user

### What are they looking at?

When the user says "these", "this page", "what I'm looking at", or asks about the screen:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/marketing-viewer/scripts/viewer_ctl.py" current-view
```

It returns the page (`overview`, `project`, `run`, `report`, `trash`, `about`), the run, the
active filters, and `visible` (how many records are on screen, how many the page lists
before filters, and their ids). Check `age_seconds` and `running`: if the report is
more than a few minutes old or the app is not running, confirm with the user before
relying on it.

### Answer from the same records

- Count and list with `viewer_ctl.py query --run "<run>" [filters]`. The filters are
  the app's own: `--platform`, `--format`, `--advertiser`, `--duration`, `--sentiment`
  (positive / negative / neutral), `--subreddit`, `--text`, `--favorites`, `--view`. To
  answer about exactly what is on screen, repeat the filters from `current-view`, or use
  its `visible.uids`.
- For reading and judgment (themes, quotes, comparisons), read the records' `text` and
  `data` from the run's `frontend_index.json` (or `query --limit N`). Quote customers
  word for word; never paraphrase a quote as if it were verbatim.
- An unknown filter value is refused with the list of valid ones. That refusal is a
  feature: the app would silently ignore the value and show everything.

### Put the answer in front of them

- **A saved view** is a named set of records the user can open in the app. It appears
  under "Saved views" in the filter panel within a few seconds, without a reload, with your
  note in a banner:
  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/skills/marketing-viewer/scripts/viewer_ctl.py" save-view --run "<run>" --title "Negative reviews about price" \
      --note "Every negative review that mentions price, cancellation fees or refunds." \
      --sentiment negative --text price
  ```
  (or `--uids a,b,c` / `--uids-file ids.json` for a hand-picked set). Titles are short and
  plain; the note says how the records were chosen. Tell the user it is there, or open the
  returned `url`. `viewer_ctl.py views` lists them, `viewer_ctl.py delete-view` removes one you made that is
  no longer wanted.
- **An address with filters** opens the app on a filtered page without saving anything:
  `viewer_ctl.py url --run "<run>" --platform reddit --sentiment negative`.
- **Marks**, only when the user asks or clearly agrees: `viewer_ctl.py mark --run
  "<run>" --uid <uid> --favorite` (or `--unfavorite`, `--note "..."`, `--clear-note`). The
  star or note appears in the open app within a few seconds.

Saved views work on the ad library and the review pages. The search page has no filter
panel, so there you answer in the chat and point to the tab (keywords, rankings, domains).

### "Not indexed", or data that looks out of date

A run from before this app existed shows "not indexed". Rebuild its index for free from
what is already on disk (no scrape, no cost). This is for such older runs, for a run that
stopped before its index was built (re-indexing the results it already collected and
paid for is a recovery path, and the app still shows that it stopped), and for the ad
case below. A scheduled month the app shows as "skipped" (its `notification.status` in
`list-runs` is `refused`) did not start and has nothing to re-index; the command refuses
it.

If a command answers that a run "stopped before it finished, so it has no results to
show" and to use an earlier run, do not stop there: run the reindex command below for
that run (free). Only if it refuses because nothing has been collected, tell the user
there is nothing to show for that run.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/marketing-viewer/scripts/viewer_ctl.py" reindex --run "<run>"
```

The open page refreshes by itself. The previous index is kept in the run's `.viewer/`
folder.

The same command is the second step when the user asks you to count an advertiser's
Google ads as a competitor's (the ad library's section "Google ads not confirmed as the
competitor's" tells them to ask you). First, in that ad run's
`competitors/resolution.json`, find the competitor's entry and add the advertiser's name,
exactly as the app shows it, to the list `google_advertisers` INSIDE the entry's
`domain` object. That is the only place the tool reads it; a key directly in the entry
or anywhere else changes nothing. Keep every other field as it is:

```json
{"input": "basecamp.com", "kind": "domain",
 "domain": {"use": "basecamp.com", "google_advertisers": ["37signals LLC"]}}
```

Then run the reindex command above. The rebuilt index counts that advertiser's Google
ads as the competitor's. If its output has `warnings`, a `google_advertisers` key is
somewhere the tool does not read: move it where the warning says, reindex again, and
tell the user what you fixed. The rebuild does not touch the `report/` folder, so the
written report does not change. Do not run the ad tool's report step again just to
update it: running the Step 4 command again on the run folder (collection steps that
already finished are skipped and not charged again) creates
`E3_creative_angles.md` again from the start, from its blank template, so the finished
report is gone and has to be written again (the ad-intelligence skill's Step 5). If the
user wants the report updated, first tell them exactly that, and run it only after they
say yes.

## What changed since the last run?

When the user asks "what changed since last month?", "what's new?", "what ads stopped
running?", or any variant, first list the runs, then use `compare` on the newest run
that has an index (`"indexed": true`).
If the research folder has a `.marketing-scraping` folder, first run
`research_sync.py pull` as Step 3 says, so a month saved in the cloud is there.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/marketing-viewer/scripts/viewer_ctl.py" list-runs
python3 "${CLAUDE_PLUGIN_ROOT}/skills/marketing-viewer/scripts/viewer_ctl.py" compare \
    --run "<newest_run_id>"
```

A run whose `notification.status` is `refused` is a scheduled month that did not start:
nothing was collected and nothing was spent. One with `incomplete` stopped before it
finished. Tell the user that month was skipped or stopped (with its
`notification.message`), and compare the newest other run that has an index. Never
re-index a refused month: it has nothing to index. A month whose `notification.status`
is `incomplete` holds only part of its records, even after `reindex` gave it an index.
Never use it as the run you compare, and never use it as the baseline: when `compare`
names it in `against`, run it again with `--against` set to the newest finished month
before the one you compare (its `notification.status` is `done`, or it has none); if
there is none, say there is no finished earlier month to compare with. If the user
asks about that stopped month, say it stopped early and that records it did not
collect are not records that disappeared. Never report them as gone or new.

The result is plain code, no model. `counts_by_platform` gives each platform's number of
new / gone / changed / unchanged records and `totals` gives the sums; `by_platform` lists
the records themselves; `urls` has ready-made app links for each category. Take every
number from `counts_by_platform` and `totals`. Never count the entries of a list
yourself: long lists get miscounted.

**Answering the question:** open your answer with `summary`, word for word. It is
composed in code and already gives this month's size and the change counts in numbers
that add up; do not rephrase its numbers or put a different number next to "compared".
Then add what helps: the per-platform counts from `counts_by_platform`, `basis` (what
baseline was used), and an offer to show the records in the app. If `settings_differ` is
true, mention that the config changed.

**How big each month is:** `month_sizes` says it, in numbers that add up with the
change counts. `this_month.records` is every record in the month's results,
`this_month.compared` is new + changed + unchanged, and `left_out_reasons` gives the
count and plain reason for every record that is not compared (for example "1 removed
Reddit post (deleted by the author or a moderator) is not compared").
`previous_month` is the same for the baseline, where compared is gone + changed +
unchanged. When you mention how big a month is, use only these numbers, or quote its
`sentence`. Never take a month's size from `list-runs` or anywhere else (for example
"out of 130 total"): that counts one run's records, including ones the comparison
leaves out, so it does not add up with the change counts.

Answer ONLY from this output: every number you give must be in it. Totals from
`synthesis_facts.json`, a report or `frontend_index.json` are the size of one run, not a
change between months; never subtract them or present them as one. Do not say a theme,
tier or finding stayed the same or changed unless you have read the written report of
both runs. Why: a change the user acts on must come from code that matches each record
across the two runs, the same count the app shows.

**Showing the records in the app:** the `urls` object has a link per non-zero category.
Share the URL for the category the user asks about.

**Drilling into new or changed records:**

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/marketing-viewer/scripts/viewer_ctl.py" query \
    --run "<run_id>" --change new --limit 20
```

`--change` accepts `new`, `changed`, or `gone`. For `--change gone` the command queries
the baseline run's records filtered to the uids that did not appear in the newer run.

**Explicit baseline** (override auto-selection):

```bash
python3 "..." compare --run "<run>" --against "<baseline_run>"
python3 "..." query  --run "<run>" --change new --vs "<baseline_run>"
```

**Not comparable:** records without a stable cross-run identity (older indexes)
are counted but not listed. This is expected for older Maps indexes; re-indexing the run
produces stable `maps:<reviewId>` uids when its raw file still has the review ids.

**Counts that could not be made:** when a platform's `cannot_tell` contains `"gone"` or
`"new"`, that platform's count for the category is left out, not zero, and `totals`
does not include it. Never tell the user "nothing disappeared" or "0 new" in that case.
Say what `cannot_tell_reason` says, e.g. "Google Maps reviews could not be matched to
last month's, so I can't count which ones are new."

**What the ad library said about a page:** a scraper error for a page is not a statement
that the page stopped advertising. When `by_platform` holds `library_unknown_now` (or
`library_unknown_before`), those ads are left out of "no longer there" (or "new") and the
`summary` says which page the library errored on; repeat that sentence and never turn those
ads into "gone". When `library_said` holds a sentence such as "Meta's ad library says
facebook.com/AcmePlumbing is not currently running ads", it is the library's own statement
and may be quoted as the library's, never as your own finding. Two pages can share a name:
the comparison ties an ad to a page by page id first, so never tie an ad to a status
message by name yourself. Never call a page "a different page" yourself when a summary says only that the tool could not
tie it to the ads' pages: an address name can differ from a display name for one page. When the summary says an address fits ads from more than one page, or that the library's
records disagree, repeat it as written: the count leaves those ads out because it is not
known which page the library meant. Never say a source was "not
collected" when the comparison says the library answered with a status message.

**Search runs:** `query --change` works on the whole run, not per platform (the search
page has no platform filter, so `--platform` is refused). Each returned record carries
its `platform`, and `compare` gives the per-platform counts in `counts_by_platform`.

## Marks from the previous run (carried forward)

When a run repeats an earlier one (`repeat_of`), the user's favorites, notes, hidden records
and saved views from the earlier run are copied into it the first time it is opened, once.
The app shows a one-line notice; to see the same thing:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/marketing-viewer/scripts/viewer_ctl.py" carried \
    --run "<run_id>"
```

- Tell the user what carried in plain words ("your 4 favorites and 1 saved view from the
  2026-09-01 run are here"), and name anything in `skipped` by its `text` and reason. A mark
  is never moved onto a record it cannot be sure is the same one.
- If any `viewer_ctl` output for the run has `carry_error`, or `carried` shows `error`, tell
  the user their marks from the previous run could not be carried and why (`error_plain`).
  Never stay silent about it.
- Hidden records carried forward are the user's own earlier decision. You still never hide a
  record yourself.

## Stopping it

The app keeps running until stopped, so the user can come back to it. Stop it when the
user asks, or offer to at the end of a working session:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/marketing-viewer/scripts/viewer_ctl.py" stop
```

## Notes

- **No secrets, no spend:** the app never reads the Apify token, never starts a scrape and
  never calls the network, except to open a link the user clicks in their own browser.
- **What the app writes:** only `.viewer/` folders: the user's favorites, notes and trash
  per run, saved views per run, and at the root the current-view report and the running
  app's address. These travel with the data.
- `--root` (only when you need it, see Step 2) must be the folder the app serves;
  `--run` is the run folder name as `viewer_ctl.py list-runs` prints it.
