# Repeating a run

Most research is worth doing more than once: the same brand's reviews every
month, the same competitors' ads every quarter. This page explains how a repeat
works, why it keeps your results comparable, and how to have it run by itself
every month.

## What a repeat does

Each of the three research skills can repeat an earlier run. When you ask for
research you have run before, the agent finds the earlier run and offers to
repeat it. On a yes it:

- creates a new run folder next to the old one, dated today (or with `_v2` if you
  already ran it today);
- copies the earlier run's settings into it: sources, places, competitors, search
  terms, limits;
- for ad research, also copies the competitors you confirmed last time, so the
  agent does not have to look them up again. If you change the competitor list,
  the new list is looked up again and shown to you for confirmation before
  anything is scraped;
- writes two extra settings: `subject` (the project name the app shows) and
  `repeat_of` (the folder of the run this one repeats).

The earlier run is never changed.

When you ask for a repeat yourself, you still see the cost forecast and approve the
spend before anything is scraped, as on a first run. A repeat that runs by itself
every month works differently: you approve a spending ceiling once. See "Running a
repeat every month" below.

## Why the settings should stay the same

A repeat is most useful when it asks the same question again. If this month's run
covers the same places, sources and terms as last month's, the difference between
the two is a real change in the market. If the settings changed, a difference
might only mean you looked somewhere else. So the agent shows you the copied
settings and changes nothing unless you ask, and tells you when a change makes the
two runs no longer directly comparable.

## When a run cannot be repeated

A repeat needs the earlier run's saved settings (`AD_INTEL_CONFIG.json`,
`PAIN_CONFIG.json` or `SEARCH_CONFIG.json` in its folder). Runs made before
settings were saved have none. The agent then says so and starts a fresh run
instead of guessing.

## Running a repeat every month

Ask for it in plain words ("set up a monthly repeat"). The agent sets it up once,
and each month's run then starts by itself with nobody at the keyboard. Five things
to know.

**1. The spending ceiling.** You do not approve each month's forecast. You approve
a ceiling once: the most one monthly run may spend. The agent shows the current
forecast and offers a ceiling with room above it (1.5 times or 2 times the
forecast). The forecast is a worst case, so a month can cost less, but never more
than the ceiling. Apify is given the same limit.

Each month the run starts only if its forecast is within the ceiling and the
settings are unchanged. If not, it does not start, spends nothing, and its message
says why. Forecasts can rise between versions (they are higher in 1.5.0), so a
ceiling you approved earlier may now be refused. If that happens, ask the agent to
set the repeat up again with a new ceiling. That replaces the earlier approval.

**2. On this computer or in the cloud.** The agent asks where the repeat should run,
and you choose. Pick one place per research, not both.

- *On this computer.* Nothing more to set up in advance. It runs only while the
  Claude app is open and the computer is awake. If the computer was asleep on the
  day, it runs once when it wakes, as long as that is within a week. After that,
  that month is skipped. If the Claude app asks you to allow the command on the day
  it runs, that month waits for your answer. This tool sets no time limit on that
  wait, but it is not known whether the app keeps the question open.
- *In the cloud.* It runs even when your computer is off, though it can start some
  minutes after the set time. It needs a private GitHub repository for your
  research, your Apify token typed by you into your Claude cloud settings, the
  websites the agent lists (Apify among them) added to that cloud setup's allowed
  websites, and a Claude plan that includes cloud sessions. The token saved there
  can be read by anything that runs in that setup, so use that setup only for
  this. It uses that plan's usage like any other session.

**3. The research repository.** For a cloud repeat, your research folder is kept in
your private GitHub repository (the folder gets a `.marketing-scraping` folder).
After a paid run the agent asks "May I save your research to your GitHub repository
now?", and new months reach this computer when the agent updates the folder. Your
favorites and notes stay on this computer and are not uploaded. A repeat on this
computer needs no repository.

**4. Seeing what changed.** Ask "what changed since last month?". The app compares
the two months in code, record by record, and counts what is new, no longer there,
changed and unchanged. No model reads summaries to guess at it. Your favorites,
notes, hidden records and saved views carry forward to the new month. A month that
was refused or stopped early is never used as the month to compare with.

**5. Results and stopping.** When a month has run to its end, the task's own
session shows its result, or what went wrong. To stop the repeat, delete the
scheduled task (or, for the cloud, the routine in your Claude cloud settings). To
change the ceiling or the settings, ask the agent to set the repeat up again.

## The project name (`subject`)

The app groups runs into projects by `subject`. Set it to a readable name on a
first run (for example "Acme Fitness (US)"). Every repeat copies it, so all the runs of
one piece of research stay together in one project, whatever the folders are
called.

## For the curious: the commands

The agent runs these for you. Replace `<skill>` with `ad-intelligence`,
`pain-and-messaging` or `search-intelligence`.

```bash
# earlier runs, grouped by project, newest first
python3 ${CLAUDE_PLUGIN_ROOT}/skills/<skill>/scripts/repeat.py list --parent <folder>

# prepare a repeat of one of them
python3 ${CLAUDE_PLUGIN_ROOT}/skills/<skill>/scripts/repeat.py prepare --from <run folder>

# a short code for a run's settings; two runs with the same code used the same settings
python3 ${CLAUDE_PLUGIN_ROOT}/skills/<skill>/scripts/repeat.py fingerprint --run <run folder>

# the most a monthly repeat can cost, from a finished run (spends nothing)
python3 ${CLAUDE_PLUGIN_ROOT}/skills/<skill>/scripts/repeat.py forecast --from <run folder>

# approve a ceiling and write the job file (--mode is local or cloud); this does not
# create the scheduled task, the agent (or the Claude app) does that from the job file
python3 ${CLAUDE_PLUGIN_ROOT}/skills/<skill>/scripts/repeat.py schedule --from <run folder> --ceiling <amount> --mode local

# what a scheduled month runs: repeat.py run-scheduled, with the job file that schedule wrote
python3 ${CLAUDE_PLUGIN_ROOT}/skills/<skill>/scripts/repeat.py run-scheduled --job <job file>

# the runs the app shows; its run_id is the run name that compare takes
python3 ${CLAUDE_PLUGIN_ROOT}/skills/marketing-viewer/scripts/viewer_ctl.py list-runs

# what changed since the last month (the run's name, not a path)
python3 ${CLAUDE_PLUGIN_ROOT}/skills/marketing-viewer/scripts/viewer_ctl.py compare --run <run name>
```

Run the last two from the folder that holds `marketing-research/`, or add
`--root <your marketing-research folder>` to each.

For a cloud repeat, `research_sync.py` (in each research skill's `scripts` folder)
links, saves (`publish`) and updates (`pull`) the research folder.

The settings code leaves out the parts that do not change what is collected: the
project name, `repeat_of`, the spending limit, and internal notes.
