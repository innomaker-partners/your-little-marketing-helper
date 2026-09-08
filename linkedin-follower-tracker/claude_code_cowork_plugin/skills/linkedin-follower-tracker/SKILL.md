---
name: linkedin-follower-tracker
description: This skill should be used when the user wants to track LinkedIn follower growth for a person or company profile: "who followed me this week", "who unfollowed me", "classify new followers by ICP segment", "send a LinkedIn follower digest", "track a profile's audience over time". It uses two PhantomBuster phantoms on the user's own account to collect followers, enriches new ones with company and job data via a profile scraper, classifies them into the user's own ICP segments using a local Claude subagent, and delivers a push notification summary. It spends PhantomBuster credits on every real run; always rehearse with the fake backend first.
version: 0.1.0
---

# linkedin-follower-tracker

Track who follows a LinkedIn profile, run by run. Each tracking run compares the
current follower list against your stored baseline, enriches the new followers
with company and job-title data via PhantomBuster, classifies them into your own
ICP segments using a local Claude subagent (no extra API key needed), and sends
you a one-line push notification with net change, new followers, lost followers, and
per-segment counts.

Every run keeps a master record (the running follower history) and writes a dated
export of that run's new followers, enriched and classified, so you can open it
without wading through the full history.

**What you need:** a PhantomBuster account with two phantoms configured (a
LinkedIn Follower Collector and a LinkedIn Profile Scraper), plus the `claude`
CLI on PATH for classification. The only credential is `PHANTOMBUSTER_API_KEY`.
Read `${CLAUDE_PLUGIN_ROOT}/docs/SETUP.md` once for PhantomBuster setup, the API
key, and the two-phantom configuration.

It spends PhantomBuster credits on every real run (the initial seed and each
subsequent tracking run). **Always rehearse with `--backend fake` before any
live PhantomBuster call.** A failure on the paid path stops immediately and
never retries; you must not be double-charged.

Full config-field reference is in
`${CLAUDE_PLUGIN_ROOT}/skills/linkedin-follower-tracker/QUICKSTART.md`.

## Step 0: Set up a subject (intake)

Ask the user two things: **where to keep this subject's files** (a parent
directory on their machine) and **whose followers to track** (a short label,
like `"Jane Doe"` or `"Acme CEO"`).

Then scaffold the per-subject directory:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/linkedin-follower-tracker/scripts/run_pipeline.py \
  --init --parent <parent-dir> --subject "Jane Doe"
```

This creates `<parent-dir>/linkedin-follower-tracker/jane-doe/` with everything
the tool needs, copies the example configs into the hidden state folder, and then
stops with a message showing you exactly which file to fill in.

The directory layout it creates:

```
<parent-dir>/linkedin-follower-tracker/jane-doe/
    .linkedin-follower-tracker/
        config.json      <- fill in the PhantomBuster agent IDs and label
        taxonomy.json    <- edit to match your ICP segments
    captures/            <- paid data lands here the instant it arrives (money salvage)
```

## Step 1: Confirm the PhantomBuster API key

The key is read from `PHANTOMBUSTER_API_KEY`. If it is not already exported in
the shell, set it up once:

```bash
cp ${CLAUDE_PLUGIN_ROOT}/skills/linkedin-follower-tracker/scripts/.env.example \
   ${CLAUDE_PLUGIN_ROOT}/skills/linkedin-follower-tracker/scripts/.env
# Open scripts/.env and fill in PHANTOMBUSTER_API_KEY
set -a; source ${CLAUDE_PLUGIN_ROOT}/skills/linkedin-follower-tracker/scripts/.env; set +a
```

Your LinkedIn session cookie is **not** stored here. The tool fetches it live from
each phantom agent right before launch, exactly as PhantomBuster's own interface
does. The API key is the only credential.

If the `claude` CLI is not on PATH, classification still runs as a stub during
the fake rehearsal, but the real run will fail at the classify stage. Install the
CLI and confirm `claude --version` works before the real run.

## Step 2: Fill the config and taxonomy

Open `<subject-dir>/.linkedin-follower-tracker/config.json`. It was copied from
the example template. Fill in:

- **`collector_id`**: the agent ID of your LinkedIn Follower Collector phantom. Find
  it in the PhantomBuster console: open the phantom and copy the numeric ID from
  its URL.
- **`scraper_id`**: the agent ID of your LinkedIn Profile Scraper phantom (same
  method).
- **`subject_label`**: what appears in push notifications, e.g. `"Jane Doe"`.
- Leave `csv_name`, `enrich_list_name`, and `adds_per_launch` at their defaults
  unless QUICKSTART.md tells you otherwise.

Then open `<subject-dir>/.linkedin-follower-tracker/taxonomy.json`. This is your
ICP segment list: the labels the classifier assigns to each new follower based on
their company and role. Edit `labels` to match your real segments, keep one
catch-all in `fallback`, and update `instructions` to describe how to classify.
The example shows the shape:

```json
{
  "instructions": "Classify each new follower into exactly one label...",
  "labels": ["Tech", "Insurance", "Manufacturing", "Marketing", "Other"],
  "fallback": "Other"
}
```

Full documentation for every config field is in QUICKSTART.md.

## Step 3: Seed the baseline

Once the config is filled, re-run `--init` to collect the current followers and
write them as the master baseline. **This is the first real PhantomBuster call.
It spends credits (one collector run, roughly 1 hour on PhantomBuster).** It
does NOT diff, enrich, classify, or report: it only records who currently follows
the profile, so subsequent runs have something to compare against. There is no
report because there is nothing to compare against yet.

```bash
PB_LIVE=1 python3 ${CLAUDE_PLUGIN_ROOT}/skills/linkedin-follower-tracker/scripts/run_pipeline.py \
  --init --subject-dir <subject-dir> --backend real
```

`PB_LIVE=1` is the deliberate money gate. Without it, `--backend real` refuses
to run. This prevents an accidental live call when you meant to rehearse.

When the seed finishes you will see `SEEDED: wrote master.csv with N followers`.
That master is your baseline; the tool diffs every future run against it.

**Do not seed with `--backend fake`.** The fake backend writes synthetic
followers, so seeding a real subject with it would poison the baseline: your
first real tracking run would then report every genuine follower as new and
every synthetic one as lost. The seed is the one step you always run for real
(`PB_LIVE=1 --backend real`). If you want to rehearse the mechanics, do it in a
throwaway `--parent` directory you can delete, never in the subject you actually
track.

## Step 4: A tracking run

After the seed, every subsequent run is a full tracking run: collect, diff, enrich,
classify, report. Always do a free rehearsal before the real run.

**Free rehearsal (costs nothing, calls no PhantomBuster API):**

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/linkedin-follower-tracker/scripts/run_pipeline.py \
  --subject-dir <subject-dir> --stage all --backend fake
```

The fake backend substitutes PhantomBuster with synthetic data and runs the full
pipeline through to notification.json. Use it to confirm the pipeline is wired up
and your config is readable before spending credits. Show the user the output.
Only proceed to the real run after they confirm it looks right.

**Real tracking run (only after explicit approval):**

```bash
PB_LIVE=1 python3 ${CLAUDE_PLUGIN_ROOT}/skills/linkedin-follower-tracker/scripts/run_pipeline.py \
  --subject-dir <subject-dir> --stage all --backend real
```

The run has two long halves: **collect** (the Follower Collector phantom runs on
PhantomBuster, typically around 1 hour) and then **enrich** (the Profile Scraper
runs, typically around 40 minutes). The script stays alive throughout, printing a
heartbeat line every ~30 seconds. Let it run. Do not interrupt it.

**If the run prints `STOPPED (money-path rule):` at any point:** stop. Do not
re-run. The error message explains what happened and names any paid data already
captured in `captures/`. Inspect it, fix the root cause, and bring the question
to the user. Never auto-retry on a stop. PhantomBuster may have already processed
the job once and re-running would charge twice.

## Step 5: Deliver the result

When the run prints `DONE -- WAKE: run complete`, the pipeline is finished. Read
`<subject-dir>/notification.json`. It has two keys:

- `message`: a short one-line push notification (subject label, net change, new,
  lost, and active total). Send this as a Claude app notification.
- `report`: the full multi-line text (overall connection count, net increase, lost
  count, per-segment new counts, per-segment active totals). Show this to the user.

Send `message` as a Claude app notification (a mobile push). Then show the user
`report` in chat along with the headline numbers: net change, new followers, lost
followers, active total. Keep the in-chat summary brief. The CSVs and master are
in `<subject-dir>` for anyone who wants the full detail.

## Tracking several people / running on a schedule

Each subject is fully independent: its own directory, its own master, its own
phantom agent IDs. Repeat Steps 0 through 3 for each profile you want to track.
A weekly tracking run needs only `--subject-dir`, so a cron job can chain them:

```bash
PB_LIVE=1 python3 .../run_pipeline.py --subject-dir <dir-1> --stage all --backend real
PB_LIVE=1 python3 .../run_pipeline.py --subject-dir <dir-2> --stage all --backend real
```

Each runs and reports independently. Their notification.json files are separate.

## Where your files live

| File | What it is | Touch? |
|---|---|---|
| `.linkedin-follower-tracker/config.json` | PhantomBuster agent IDs and naming | Edit once at setup |
| `.linkedin-follower-tracker/taxonomy.json` | Your ICP segment labels | Edit to fit your segments |
| `master.csv` | The running follower history | Never edit by hand |
| `Followers - <date>.csv` | This run's new followers, enriched and classified | Read-only output |
| `notification.json` | Push-ready payload from the last run | Read by the agent |
| `notification.txt` | Multi-line report from the last run (same as `report` in notification.json) | Human reference |
| `captures/` | Money-salvage: paid data written the instant it arrives, so nothing is lost if the run crashes | Inspect on a STOP |

## Notes

- **Classification** uses a local Claude subagent (Haiku tier) per new follower.
  It runs after enrichment using the `claude` CLI. Classification costs only Claude
  subscription tokens (no extra API key beyond the PhantomBuster one).
- **The mass-loss guard**: if a single run would mark more than 50% of your active
  followers as lost, the pipeline stops before writing the master. This catches a
  collapsed collector run (PhantomBuster returned almost nobody due to a session
  issue) before it corrupts your history. If it fires, check `captures/` and the
  PhantomBuster console, then bring the question to the user.
- **`master.csv` is a load-bearing file.** The diff, enrichment completeness check,
  and idempotency guard all depend on its exact schema. Never hand-edit it.
- **The idempotency guard**: if you accidentally run `--stage report` twice against
  the same scrape output, the second call is refused. A legitimate next-week run
  gets a new PhantomBuster container ID and is always allowed through.
