# QUICKSTART: LinkedIn Follower Tracker

## What this does

This tool tracks who follows a LinkedIn profile over time. On each run it
compares the current follower list against the stored baseline, enriches new
followers with company and job-title data using PhantomBuster's LinkedIn Profile
Scraper, classifies them into your own ICP segments using a local Claude subagent,
and produces a push notification summary and a dated CSV export.

**What you need:**

- Python 3, standard library only. No `pip install`.
- A PhantomBuster account with two phantoms configured: a **LinkedIn Follower
  Collector** and a **LinkedIn Profile Scraper**. See docs/SETUP.md for how to set
  them up.
- Your PhantomBuster API key in `PHANTOMBUSTER_API_KEY`.
- The `claude` CLI on PATH. This is what runs the ICP classifier (no separate
  LLM API key is needed).

---

## 1. PhantomBuster setup

You need two phantoms in the PhantomBuster store:

- **LinkedIn Follower Collector:** the one that pulls the current follower list
  from a LinkedIn profile. Run it once a week; it produces a CSV of followers.
- **LinkedIn Profile Scraper:** the one that takes a list of profile URLs and
  returns company, job title, and industry data for each. This is what the
  enrichment stage uses.

Once the phantoms are configured in your PhantomBuster console, copy each agent's
numeric ID from its URL. You will paste those into `config.json`.

See `${CLAUDE_PLUGIN_ROOT}/docs/SETUP.md` for the full PhantomBuster walkthrough:
which phantom to search for in the store, how to configure it for a specific
LinkedIn profile, and how to verify it works before using it with this tool.

---

## 2. Setting the API key

The tool looks for `PHANTOMBUSTER_API_KEY` in the environment. Three ways to
set it:

**Option A: a `.env` file (recommended)**

```bash
cp ${CLAUDE_PLUGIN_ROOT}/skills/linkedin-follower-tracker/scripts/.env.example \
   ${CLAUDE_PLUGIN_ROOT}/skills/linkedin-follower-tracker/scripts/.env
# Edit scripts/.env and paste your key after PHANTOMBUSTER_API_KEY=
set -a; source ${CLAUDE_PLUGIN_ROOT}/skills/linkedin-follower-tracker/scripts/.env; set +a
```

**Option B: export in your shell**

```bash
export PHANTOMBUSTER_API_KEY=your_key_here
```

**What the key is not:** your LinkedIn session cookie. The session cookie is
fetched live from each phantom agent right before launch. The tool reads it
from the agent's stored configuration via the PhantomBuster API. You never paste
it anywhere.

Find your PhantomBuster API key in the console: Settings > API > copy the
"X-Phantombuster-Key" value.

---

## 3. Config fields: `config.json`

Copied from `config/FOLLOWER_CONFIG.example.json` when you run `--init`.

| Field | Required | Default | Description |
|---|---|---|---|
| `collector_id` | Yes | (required) | Agent ID of your LinkedIn Follower Collector phantom. Numeric, found in the phantom's URL. |
| `scraper_id` | Yes | (required) | Agent ID of your LinkedIn Profile Scraper phantom. |
| `subject_label` | Recommended | (none) | Label for push notifications and the Followers CSV name, e.g. `"Jane Doe"`. When absent the generic `"LinkedIn followers"` prefix is used. |
| `csv_name` | No | `"followers - {date}"` | Name template for the Followers CSV. `{date}` is replaced with `YYYY-MM-DD`. |
| `enrich_list_name` | No | `"linkedin-follower-tracker enrichment {date} {time}"` | Name template for the org-storage enrichment list. `{date}` and `{time}` make each list uniquely named so a same-day re-run never collides. |
| `adds_per_launch` | No | `500` | How many profiles the scraper processes per launch. Leave at 500 unless your phantom is configured differently. |
| `active_master_name` | No | `"active master - {date}"` | Name template for the active-only master CSV. `{date}` is replaced with `YYYY-MM-DD`. This file contains only active followers (unfollowers excluded) with the `lost_on` column dropped, matching the Google Sheet format. |
| `notification` | No | see below | Object controlling which elements appear in the one-line push. When absent the default element list is used. See **Notification elements** below. |

### Notification elements

The `notification` block in `config.json` controls what appears in the one-line push
notification. Example:

```json
"notification": {
  "elements": ["active_total", "new_total", "new_in_target", "in_target_active_total"]
}
```

When the block is absent, the default element list above is used. Unknown tokens are
silently skipped so a typo never crashes the pipeline.

| Token | What it shows |
|---|---|
| `active_total` | Current active follower count (default) |
| `new_total` | New followers this run (default) |
| `new_in_target` | New followers in ICP segments this run (default) |
| `in_target_active_total` | Total active ICP-segment followers (default) |
| `per_segment_active` | Active count per ICP segment, e.g. "Insurance 803, Manufacturing 69" |
| `per_segment_new` | New followers per ICP segment this run |
| `net` | Signed net change (+N or -N); note: diverges from active-count change when followers return |
| `lost` | Lost follower count this run |

Net and lost are not in the default push because the push speaks the ACTIVE-only model.
They are always in the full log (`notification.txt`). A `notification` block is asked for
once at the start of a new subject and persisted so repeatable workflows never re-ask.

---

## 4. Taxonomy fields: `taxonomy.json`

Copied from `config/taxonomy.example.json` when you run `--init`. This is your
ICP segment definition. The classifier reads it on every run.

| Field | Required | Description |
|---|---|---|
| `instructions` | Yes | One-sentence prompt given to the Claude classifier describing the classification task, e.g. "Classify each new follower into exactly one label based on their company and role." |
| `labels` | Yes | Array of label strings. Must include the fallback. Order does not matter; the classifier chooses by best fit. |
| `fallback` | Yes | The catch-all label to use when the follower's data is thin or ambiguous. Must appear in `labels`. |

The classifier receives each follower's company name, industry, and job title.
Keep labels short (one or two words). The fallback label is excluded from the
"in-target" segment count in notifications, so put your catch-all there.

---

## 5. The fake backend

`--backend fake` substitutes PhantomBuster with a built-in synthetic data source.
It costs nothing and requires none of the PhantomBuster credentials. Use it to:

- Rehearse the full pipeline before spending any PhantomBuster credits.
- Verify that your config.json and taxonomy.json are readable and well-formed.
- Check that notification.json is written and has the right shape.

The fake backend runs from a fixed scenario (`sample` by default). Results are
synthetic: the follower names, companies, and counts are invented. Use them to
confirm the pipeline mechanics, not to read business conclusions from.

---

## 6. Run flags reference

| Flag | Description |
|---|---|
| `--init` | Seed mode: scaffold the subject dir, copy example configs, then (if config is filled) collect followers and write master.csv. Stops after seeding. Does not run diff, enrich, classify, report. |
| `--parent <dir>` | Parent directory; combined with `--subject` to compute the subject dir path. |
| `--subject "Label"` | Subject label, e.g. `"Jane Doe"`. Combined with `--parent` to compute the directory slug. |
| `--subject-dir <dir>` | Direct path to an existing subject directory. Overrides `--parent` + `--subject`. |
| `--backend fake` | Free rehearsal backend. No PhantomBuster calls, no credentials needed. |
| `--backend real` | Live PhantomBuster. Requires `PB_LIVE=1` as an additional environment variable. |
| `--stage all` | Run the full pipeline: collect, diff, enrich, classify, report. |
| `--stage <name>` | Run one stage only: `collect`, `diff`, `enrich`, `classify`, or `report`. Reads stage inputs from the work directory, so a previous run's output must be there. |
| `--classifier stub` | (Fake-backend only) Deterministic classification stub. Default for `--backend fake`. |
| `--classifier claude` | Real Claude Haiku subagent for classification. Used automatically on `--backend real`. |
| `PB_LIVE=1` | Environment variable that unlocks `--backend real`. Set it only for the deliberate production run. |

---

## 7. What each run writes

All outputs land in the subject directory:

| Path | What it is |
|---|---|
| `master.csv` | The running follower history. Every run's new followers are appended; every lost follower gets a `lost_on` date. Never edit by hand. |
| `active master - <date>.csv` | Active-only snapshot: same as `master.csv` but with unfollowers excluded and the `lost_on` column dropped. Matches the Google Sheet format. Name is controlled by `active_master_name` in `config.json`. |
| `Followers - <date>.csv` | This run's new followers, enriched and classified. One file per run. |
| `notification.json` | `{"message": "...", "report": "..."}`: the send-ready push payload. The `message` key is the one-line push to send; `report` is the multi-line text. |
| `notification.txt` | The multi-line report text (same as `report` in notification.json). Useful for reading the workdir directly without parsing JSON. |
| `captures/` | Money-salvage directory. Each paid PhantomBuster handle and result set is written here the instant it arrives. If the run crashes later, these files tell you exactly what was already processed. Inspect them on a STOP; do not delete them. |
| `.linkedin-follower-tracker/seeded.json` | Written after a successful seed: timestamp and follower count. Confirms the baseline was established and when. |
| `.linkedin-follower-tracker/runs/` | Idempotency markers. One file per scrape container ID, written after a successful report stage. Prevents the same scrape output from being appended to master.csv twice. |

---

## 8. Troubleshooting

**"REFUSED: --backend real spends money. Set PB_LIVE=1..."**

You ran `--backend real` without the safety flag. Set `PB_LIVE=1` in front of the
command: `PB_LIVE=1 python3 run_pipeline.py ...`. This is intentional. The flag
makes the money call deliberate, not accidental.

**"REFUSED: no master.csv baseline..."**

A tracking run needs a prior baseline. Run the seed first:
`PB_LIVE=1 python3 run_pipeline.py --init --subject-dir <dir> --backend real`.

**"Config placeholder detected: fill in your PhantomBuster agent IDs"**

You ran `--init` before filling in `config.json`. Open
`<subject-dir>/.linkedin-follower-tracker/config.json`, replace
`YOUR_COLLECTOR_AGENT_ID` and `YOUR_SCRAPER_AGENT_ID` with your real phantom
agent IDs, then re-run `--init --subject-dir <dir> --backend real`.

**"STOPPED (money-path rule): mass-loss guard..."**

The pipeline stopped because this run would have marked more than 50% of active
followers as lost. This usually means the Follower Collector phantom returned far
fewer results than expected, often a LinkedIn session issue on PhantomBuster's side.
Check `captures/` for what was collected, inspect the PhantomBuster console for
the collector run's output, and do NOT re-run until you understand why. The master
is untouched.

**"REFUSED: this scrape has already updated the master; refusing to append again."**

You ran `--stage report` (or `--stage all`) twice against the same scrape output.
This is the idempotency guard working as intended. If the first report run actually
succeeded, you are done. If it failed mid-way (the master was written but the
notification was not), check the subject directory manually and fix the missing
artifact rather than re-running. A legitimate next weekly run will get a new
PhantomBuster container ID and will not be blocked.

**Classification not running / `claude` not found**

Install the `claude` CLI and confirm `claude --version` returns a version. The
classifier invokes it as a subprocess. It must be on your PATH. On `--backend fake`
the stub classifier is used and the CLI is not required.
