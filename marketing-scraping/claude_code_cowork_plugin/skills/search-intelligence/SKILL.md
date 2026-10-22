---
name: search-intelligence
description: Use whenever the user wants search-demand or SEO research, OR wants to repeat or schedule search research they ran before. Triggers include "what does my market search for", "what do people search when looking for [X]", "what terms should I target", "keyword research", "keyword difficulty and volume", "who owns the SERP", "find winnable keywords", "write (or finish) the report for my [X] research", "repeat my [X] research", "set up a monthly repeat", and a prompt with "Run my scheduled marketing research repeat". A past run's report comes only from this skill, never written by hand. Monthly repeats are set up here, never with the cloud schedule skill. Past runs are in ./marketing-research: use this skill, never search the disk. A past run's report, no tool named: newest run. It scrapes real search volume, difficulty, CPC and SERP rankings on the user's OWN Apify account. It meters PER KEYWORD and needs a card on the Apify account (~USD 3 to USD 7 per default run, over the free credit); always dry-run first.
version: 1.6.0
---

# search-intelligence

Find what a market actually searches for, price every keyword by volume,
difficulty and cost-per-click, map who already ranks, and write a **search-landscape
report**: keywords clustered by intent, a measured demand table, a "winnable
opportunities" shortlist (low-difficulty terms with real volume), and a
competitive map of who owns the SERP. Every number is counted in code from scraped
data; the report writer clusters and interprets but never invents a figure.

**This is the expensive tool.** It meters per keyword on the user's Apify account
and **needs a card** (a ~300-keyword run is forecast at about **USD 3 to USD 7**: about USD 7
on Apify's free plan, less on a paid plan, and larger runs cost more). **Never run the paid step without first showing the dry-run forecast
and getting the user's explicit go-ahead.** Read `${CLAUDE_PLUGIN_ROOT}/docs/SETUP.md`
once for install, token, and the run-directory convention. Full field docs are in
`${CLAUDE_PLUGIN_ROOT}/skills/search-intelligence/QUICKSTART.md`.

**Amounts in this file are written as USD 1.23, never with a dollar sign.** Claude Code replaces
a dollar sign followed by a digit with the words typed after the skill's name, which garbles
the figure. The tool itself prints amounts with a dollar sign in front; when this file says to
quote a printed amount, quote it as the tool printed it.

## Step 0: Set up the run directory (ask the user)

These runs produce many files (the keyword universe, per-keyword metrics, SERP
rankings, a counted-facts file, the report). Keep each run self-contained.

### First: is this a repeat?

**A research folder saved to a repository.** If the research folder
(`<parent>`, by default `marketing-research/` in the current working directory)
has a `.marketing-scraping` folder, its research is also kept in the user's GitHub
repository for a repeat in the cloud, and new months reach this computer only
by an update. Before every `repeat.py list`, every comparison and opening the
app, run:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/search-intelligence/scripts/research_sync.py pull \
    --root <parent>
```

If new run folders arrived, tell the user. If it refuses, tell the user its
`message` and go on with what is on this computer, saying that newer months may
be missing. Why: without the update a cloud month looks missing, and a
comparison would treat an older month as the newest. After a paid run in such a
folder (once Step 6 is done), ask "May I save your research to your GitHub
repository now?" and, on yes, run the same script with `publish --root <parent>`.
Why: the cloud continues from the newest month it can see in the repository.

Before creating anything, check whether this research has been run before:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/search-intelligence/scripts/repeat.py list \
    --parent <parent>
```

Leave out `--tool`, so research from all three tools shows. If the research the
user means belongs to another tool (its `tool` in the list is not `search`), switch
to that tool's skill (ad-intelligence, pain-and-messaging or search-intelligence)
and continue there: each tool's commands only work on its own runs.

If the user names no tool and no subject, and asks about an existing run's report
or results (for example "write me the report for my last run"), the research they
mean is the newest run in the list: the one with the latest `run_date`, whatever
its tool. A run has finished only when its `has_config` and `indexed` are both
`true`. If the newest run has not finished, do not take it: say so, name the newest
run that has finished, and ask which of the two the user means. Otherwise say which
run you took, then continue with that tool's skill (this one, if its tool is `search`).
Ask which research instead when the newest runs of two researches (a different tool
or subject) share that `run_date`. This never applies to setting up, changing or
running a repeat, and never to starting new research: there the rules below stand
(for a repeat, list the projects and ask which one, never guess). Why: the folder
already says which run is last, and asking costs the user a turn for an answer that
is one look away.

`<parent>` is where runs live, by default `marketing-research/` in the current
working directory. If the command says the folder does not exist, or nothing in
the list matches, and the user asked to repeat, schedule or compare research
they ran before: ask them where their research folder is, and run `list` there.
Otherwise this is the first run: go to "New run" below.

**Find earlier research only this way.** Never search the whole disk, other
folders, or Claude's own session files or memory for past research (no
`find /`, no searching `~/.claude`). Why: those hold the user's private files
and other conversations, and `repeat.py list` is the product's own way to find
runs: it reads each run's saved settings, which a file search cannot.

If a project in the list matches what the user is asking about (the same brand or
market, for example search data for project management software), tell them when it last ran and offer to repeat it
with the same settings. Say why it helps: with the same settings, this run's
results can be compared directly with the last one. If they agree:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/search-intelligence/scripts/repeat.py prepare \
    --from <parent>/<run folder from the list>
```

This creates the new run folder (dated today, or with `_v2` if one already exists
for today), copies the previous settings into it, and records which run it
repeats. It never changes the earlier run. If it answers that the run has no saved
settings, tell the user plainly that it cannot be repeated exactly, and start a
new run instead.

Then continue with Step 1. In Step 2, do not fill the config from scratch: show
the user the copied settings and change nothing unless they ask. If they do change
something, tell them this run will no longer be directly comparable with the
previous one.

If nothing in the list matches, or the user wants something different, use "New
run" below.

### What changed since the last run?

When the user asks what changed since last month (or since the last run),
answer with the app's comparison, not from this skill's own files.
If `<parent>` has a `.marketing-scraping` folder, first run
`research_sync.py pull --root <parent>` as "A research folder saved to a
repository" above says.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/marketing-viewer/scripts/viewer_ctl.py" compare \
    --run "<newest run folder>"
```

Run it from the folder the user works in, without `--root`: it then reads
`marketing-research/` there, the default `<parent>`. Add `--root "<parent>"` only
if the user told you their research is somewhere else. If it says no research
folder was found, ask the user where it is; never search for it. The newest
run folder is the newest one with `"indexed": true` in `viewer_ctl.py list-runs`: a
scheduled month that was refused or stopped (its `notification.status`) is skipped
and is never compared, not even after a re-index gave it an index, so say it was
skipped or stopped and compare the newest other one that has an index.

Read the result as the marketing-viewer skill's "What changed since the last
run?" section says, and answer ONLY from this output: every number you give
must be in it. Open your answer with its `summary`, word for word: it gives
the month's size and the change counts in numbers that add up. When you say how
big a month is, use only its `month_sizes`
(records, compared, and the reasons the rest are left out), which add up with
the change counts; never a record count from `list-runs`. Totals from `synthesis_facts.json`, a report or
`frontend_index.json` are the size of one run, not a change between months;
never subtract them or present them as one. Do not say a theme, tier or finding
stayed the same or changed unless you have read the written report of both
runs. Why: a change the user acts on must come from code that matches each
record across the two runs (the same count the app shows), not from a model
reading summary statistics.

### Write (or finish) the report of a run

This applies when the user asks for the written report of research they ran,
for example "write me the report for my project management search research". The
report comes only from the run's own report step, which checks every number
against the counted data: never write a report file yourself, and never answer
with your own reading of `synthesis_facts.json` in its place.

1. Find the run with `repeat.py list` (above). If `S1_search_landscape.md` is
   already in its folder, the report exists: go to Step 5.
2. Otherwise dry-run that folder with the Step 3 command. When a paid step is
   already collected, the dry run lists it as skipped at USD 0, and its `WHOLE-RUN
   COST FORECAST` covers only what this run can still spend. If that figure is
   zero (printed as a dollar sign followed by 0.0000), the run spends nothing on Apify: tell the user so and run the Step 4
   command on the same folder without waiting for a go-ahead. If it is above
   zero, show it and wait for the user's go-ahead, as in Step 3.
3. Then continue with Step 5 and Step 6, exactly as after any other run, the app
   included.

### Set up a monthly repeat

This applies when the user asks for a monthly (or any regular) repeat, for
example "set up a monthly repeat" or "run this every month", or when you offer
it after a completed run and they agree.

**Which research?** Run `repeat.py list --parent <parent>` WITHOUT `--tool`, so
research from all three tools shows. A project can be repeated when its newest
completed run (`has_config` and `indexed` both true) is in the list. If the user
did not name the research and more than one project can be repeated, list them
all in plain words (what kind of research, and its subject) and ask which one.
Never guess, even when one of them belongs to this skill. If the chosen project
belongs to another tool, continue in that tool's skill (ad-intelligence,
pain-and-messaging or search-intelligence): each tool's commands only work on
its own runs. If nothing can be repeated, do a normal run first.

**Show the forecast.** Run:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/search-intelligence/scripts/repeat.py forecast \
    --from <completed run folder>
```

This prints `{"forecast_usd": ..., "tool": ..., "subject": ...}` and exits 0.
No actor calls, no files written.

Explain the ceiling in plain words: the most one monthly run may spend. Tell the user: if the next run's forecast is above the ceiling, or the settings changed, the run does not start and spends nothing, and its message says why. When a scheduled run starts and reaches its end, its own session shows that message, or what went wrong if the command could not give one. A notification may also bring it, but the app can decline to send one, for example while the user is at the computer, and the session then says that none was sent. While the app waits for the user to allow the command, there is nothing to show yet, and a month that did not start because the computer was asleep for more than a week shows nothing at all. A month skipped because the repeat now runs in the other place, on this computer or in the cloud, still shows its message. The
ceiling is for the whole run, and Apify is told the same limit.

The forecast is the most the run can cost, not a prediction of the bill. It
prices every step at the largest size the settings allow, because the gate
has to know the worst case before any money is spent, so a real run can cost
less. Say this to the user in plain words, for example: "This repeat can cost
at most about $X. It can cost less, and the ceiling you choose is the most it
may ever spend." The forecast covers the scraping steps only: writing the search report costs nothing on Apify. In a cloud month the routine's own session writes it, and that session counts against the user's Claude plan. A past run's bill is only the amount its own closing line or notification states,
given as written; never work one out from the forecast, and if you do not have that line, say the exact charge is in the user's Apify console. A step's RUN_LOG.md line `**Cost:** $... charged by Apify`
is what Apify charged for that one actor run, unless the line says the charge
may be incomplete or that Apify was still recording it. Those lines are per
actor run, so never add them up into what a run cost.

**Ask the user for the ceiling.** Use `AskUserQuestion`, state the forecast,
and offer two concrete options with headroom: 1.5x the forecast and 2x the
forecast, each rounded UP to the next whole cent (never to the nearest cent,
which can land below 1.5x). The 1.5x option is the recommended one. Never offer the exact forecast as a ceiling option,
and never pick the ceiling for them. If `AskUserQuestion` is not available, ask
the same question in plain text with the same options. Why: the forecast can rise month to month
(more search volume, more seeds discovered). A ceiling at the exact forecast
causes unnecessary stops. The ceiling is the user's approval, and a model
choosing it would defeat the spending gate.

**Ask where the repeat runs.** Only after the user has answered the ceiling question, ask this in a message of its own, never in the same message as the ceiling: `AskUserQuestion` with the question "Where should the
monthly repeat run?" and two options labelled "On this computer" and "In the
cloud", each with its text below, and the "Either way" text with the question.
If `AskUserQuestion` is not available, ask in plain text with the same words.
Put all three texts in your message word for word and in full: do not shorten, summarise, merge or reword any sentence of them. Why: every sentence is something the user must know before choosing, such as who can read the token saved in the cloud. Why a message of its own: the ceiling is the user's spending approval and needs its own answer, and in tested sessions, replies that asked both at once sometimes left out sentences of these texts. Do not
mark either option as recommended,
and never choose for the user. If they say they do not mind, use "On this
computer". Why: the choice is the user's, and each place asks something
different of them, which these texts state.

> **On this computer.** Nothing more to set up: it uses the research folder and
> the Apify login you already have here. It only runs while the Claude app is
> open and the computer is awake. If the computer is asleep on the day, it runs
> once when it wakes, as long as that is within a week; after that, that month
> is skipped. That week is for a missed day. If the app asks you to allow the
> command on the day it runs, that month waits for your answer. This tool sets
> no time limit on that wait, but it is not known whether the app keeps the
> question open. When you answer, the month runs then, if its forecast is still
> within your ceiling. If you choose "always allow", the app says it keeps that
> answer on the task for later months. When a month has run to its end, the
> task's own session shows its result, or what went wrong. The app may also
> send you a notification, but it can decline, for example while you are at the
> computer; the session then says that none was sent.
>
> **In the cloud.** It runs even when your computer is off, though it can start
> some minutes after the time you set. It needs more from you, once: a private
> GitHub repository for your research, your Apify token typed by you into your
> Claude cloud settings (I never see or handle it), and the websites my setup
> checklist lists, Apify among them, added to that cloud setup's allowed
> websites. The token saved there can be read by anything that runs in that
> setup, so use that setup only for this. Your research folder on this computer
> becomes a copy of that repository, and new months arrive when I update it.
> Your favorites and notes stay on this computer and are not uploaded. When
> notifications are turned on for the routine, a notification may arrive when a
> cloud month finishes. That is not guaranteed, and it can come before the month
> is saved: if the new month is not in the app after I update, ask me to update
> again later. It uses your Claude plan's usage like any other session, and
> needs a plan that includes cloud sessions.
>
> **Either way:** a run spends at most the ceiling you approve, on your own
> Apify account, and it stops before spending anything if the settings changed
> or the forecast is above the ceiling. Choose one place per research, not both.
> If you switch, once the change is saved, a month in the old place is skipped
> and spends nothing on Apify. This holds on the computer where I set it up;
> delete any scheduled task on another computer. The old scheduled task or
> routine is not removed: delete it when I tell you to.

Then follow only the part for the place the user chose: "For a repeat on this
computer" or "For a repeat in the cloud" below.

**What `schedule` prints, in both places.** If the output says `replaced: true`,
tell the user that the earlier approval (and its ceiling) was replaced. If the
output has `forecast_note`, tell the user that sentence in their language
without changing what it says. Why: it is the only place the user learns that
no forecast was kept with this approval, and a later stop over the ceiling then
cannot say whether the forecast rose.

**For a repeat on this computer.** Run `schedule`:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/search-intelligence/scripts/repeat.py schedule \
    --from <completed run folder> --ceiling <amount> --mode local
```

If the output also says `previous_mode: "cloud"`, this repeat ran in the cloud until now. Tell the user to delete the cloud routine in their Claude cloud settings, whatever they answer to the next question. Ask "May I save your research to your GitHub repository now?" and, on yes, run:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/search-intelligence/scripts/research_sync.py publish \
    --root <parent>
```

Tell the user its `message`. Offer the scheduled task below only after that save succeeded. If they say no, or the save is refused, tell them the repository still says the cloud, so the cloud routine would still run and pay for as long as it exists, and offer the scheduled task only once the save has succeeded. Why: the cloud routine reads the approval from the repository; until
the repository says this computer, a cloud month would still go ahead and both
would pay.

The schedule itself runs on this computer. The repeat needs the local run
folders and the saved Apify token, and a cloud schedule can reach neither, so it
would fail every month. Claude Code's built-in `schedule` skill, cloud routines
and remote triggers are all CLOUD schedules: never use them for this, not even
to "create the task" after `repeat.py schedule` has run. Only a tool that
creates a scheduled task that runs on this computer qualifies. If you are not
sure whether a tool runs on this computer, do not use it: give the user the
prompt instead (below).

If this session has such a local tool, offer to create the task, and create it
only after the user says yes. Use a monthly cadence, and
use this exact prompt (fill in the real absolute path from the schedule
output):

```
Run my scheduled marketing research repeat, unattended: job file <absolute job path>. Use the marketing-scraping plugin's run-scheduled command. If a notification tool is available, send the run's message, or its error if it has none, with it. Do not ask questions.
```

If no scheduling tool is available, give the user this prompt and tell them
to add it as a monthly scheduled task in their Claude app. Describe what to
look for rather than promising a specific tool name, since tool names differ
between hosts. Tell them to set it up on one computer only: two computers with
the same task would both pay for the same month.

**To stop the repeat:** delete the scheduled task.
**To change the settings or ceiling:** set up the repeat again, which replaces
the existing approval.

**For a repeat in the cloud.** Every step below is a command; run them in this order. After `status`, `link`, `cloud-setup` and `publish`, tell the user the command's `message`. `schedule` prints no `message` (what to tell the user from it is above), and the `message` of `routine-prompt` is an instruction to you, not words for the user. Never run git yourself, never create or
delete a repository, and never run `gh`: every git step goes through
`research_sync.py`. That holds also when a command fails: do not run git, or
anything else, to find out why. Tell the user the command's `message` in plain
words and stop there. Why: a cloud month exists only if it is saved correctly, and
these commands save only the research, never the user's other files.

1. Check the research folder:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/skills/search-intelligence/scripts/research_sync.py status \
       --root <parent>
   ```

2. If `linked` is false, the user needs an empty repository first. Tell them,
   word for word: "Create an empty private repository on GitHub (no README, so
   its first branch becomes the default) and give me its address. Make sure
   Claude has access to it (the Claude GitHub App, or /web-setup)." Then wait. If the user says the repository is public, do not link it: tell them it must be private, because every month's results and reports saved there could then be read by anyone, ask them for a private one, and stop until they give one. When they give the address, run:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/skills/search-intelligence/scripts/research_sync.py link \
       --root <parent> --remote <the address they gave>
   ```

   If `link` refuses, tell the user its `message` word for word and stop. Do not
   run git, not even `git init`, to see why, and do not tell the user to run git
   or to push the folder themselves. If the message says this one step can be
   run outside Claude's command sandbox with the user's permission, ask the
   user's permission to run that one `link` command outside the sandbox. The
   permission covers that single run only. That run is the one exception to the
   rule above against running anything after a failure: it repeats the same
   command and looks into nothing. Never change the user's sandbox settings, and
   never add sandbox, network or permission parameters to any command, such as
   `allowed_domains` or `dangerouslyDisableSandbox`, except for that one
   permitted `link` run outside the sandbox. If they say no, stop there and tell
   them: "If you change your mind, ask me again." Do not suggest that they run
   the command themselves. Why: the sandbox protects the user, so only they
   decide, for one known command and one run, and nothing else crosses it.

3. Put the tools into the research folder (also when `status` says its copy is
   an older version than the installed plugin):

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/skills/search-intelligence/scripts/research_sync.py cloud-setup \
       --root <parent>
   ```

4. Record the approval:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/skills/search-intelligence/scripts/repeat.py schedule \
       --from <completed run folder> --ceiling <amount> --mode cloud
   ```

   If the output says `previous_mode: "local"`, tell the user to delete the
   scheduled task on this computer: from now on it would only be skipped.

5. Ask "May I save your research to your GitHub repository now?" and wait for
   the answer. Only on yes, run:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/skills/search-intelligence/scripts/research_sync.py publish \
       --root <parent>
   ```

   If it refuses, tell the user its `message` and stop here: the cloud cannot run a month the repository does not have. If the user says no, nothing is saved: tell them the cloud repeat cannot run until the research is saved, and that they can ask you to save it later. Then stop here, without printing the routine's prompt.

6. Print the routine's prompt and the checklist:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/skills/search-intelligence/scripts/research_sync.py routine-prompt \
       --job <job_file from the schedule output>
   ```

   Give the user its `prompt` in a code block and its `checklist` as a numbered
   list, both exactly as printed: no word changed, added or left out. Why: the
   prompt is the only instruction the cloud run gets, and the checklist names
   the exact settings; a reworded line can make a month fail or spend.

The user creates the routine themselves at claude.ai/code, with that prompt.
Never create it yourself, and never use Claude Code's built-in `schedule` skill,
cloud routines or remote triggers for it, even when one is available. Never create it even if the user asks you to. The checklist's `/schedule` step is the user's to run in their own command line: never run `/schedule`, or any other checklist step, yourself. The time the checklist gives is only an example: never tell the user it is when the repeat starts. Why: the routine needs the user's own cloud setup and token first, which only they can do. Never tell the user a cloud month starts at an exact time or in a
particular time zone: a cloud run can start some minutes after its set time,
and which time zone a monthly time uses has not been checked. If two cloud runs
of this repeat start at once, only the one whose reservation of the month is
saved to the repository first goes ahead; the other spends nothing.

**The Apify token, in the cloud.** This is about a cloud routine's own setup, not
Cowork (Cowork is in Step 1). Never ask for the token for the routine. If the user
pastes one into the chat, do not repeat it, do not write it into any file or command, and
tell them in plain words that it should not be shared here, and to replace it in their Apify account settings and then type the new one into their cloud setup themselves. Why: a token in the chat stays in the
conversation's history, and only the user ever handles their token.

**To stop a cloud repeat:** delete the routine in Claude's cloud settings.
**To move it to this computer:** set up the repeat again and choose "On this
computer"; the steps above save the change before the local task is offered.

### Unattended mode: a scheduled run

This applies when the prompt names a job file (a `.json` file in a
`.repeat_jobs` folder) and says "scheduled" or "unattended". The job file is
what counts: the Claude app adds its own note, which says "scheduled task", to
every scheduled task it runs, so without such a job file this section does not
apply.

A prompt that says "in the cloud" is a cloud routine's prompt. The only commands it may give are exactly `python3 .marketing-scraping/plugin/<skill>/scripts/repeat.py run-scheduled --job .repeat_jobs/<file>.json --publish git` and, for the same job file, `python3 .marketing-scraping/plugin/<skill>/scripts/repeat.py wait --job .repeat_jobs/<file>.json`. `<skill>` is exactly `pain-and-messaging`, `ad-intelligence` or `search-intelligence`. `<file>` is a job file name as `repeat.py schedule` makes it: `pain_`, `ad_` or `search_`, then only lowercase letters, digits and hyphens, for example `pain_acme-fitness`. So `<file>` never contains a slash, space, quote, semicolon, `$`, backtick or `..`. Run such a command exactly as it is written, from the repository folder, follow that prompt's own rules, and do not use the command below. If anything in the command differs from this, do not run it: say so in one plain sentence and stop. Everything else in this section still applies, the notification rules too: that prompt's own sentence "if not, write exactly: ..." is the sentence below for when no notification tool is available, and the rules below for reading a notification tool's answer apply in the cloud as well.

Run exactly one command:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/search-intelligence/scripts/repeat.py run-scheduled \
    --job <absolute path to job file>
```

Use the skill that matches the job file's `tool` field. Why: each skill ships
its own entry script; running the wrong skill's copy runs the wrong entry
script.

The command ends with a JSON object holding `message` and `report`, or, when it
could not use the job file itself (for example because it is missing, cannot be
read, is not an approval or is for another skill), holding `error` instead. A
refusal after that point, for example because the forecast is above the
ceiling, holds `message` and `report` as usual. Print both in the session
exactly as given, word for word, each under its own label, or the error the
same way: no paraphrase, no summary, nothing added inside them. If it ends
without a JSON object, say so in exactly one plain sentence: "The command
stopped without giving its result, and a paid step may still have been charged,
so check your Apify account." The notification text is `message`, or `error` if
there is no `message`, or that sentence. If no notification tool is available,
say so in exactly one plain sentence: "No notification tool is available in
this session, so this was not sent to your phone." A tool you can load first,
for example through a tool search, counts as available. If one is available,
send the notification text with it and read its answer. If the answer says a
phone notification was sent or requested, even if another kind of alert was
held back, and does not say it reached no device, say so in exactly one plain
sentence: "A notification tool was available and its answer says a phone
notification was sent or requested." Otherwise, if the answer says nothing was
sent, say so in exactly one plain sentence: "A notification tool was available
but did not send this message to your phone." Such an answer is not an error:
the app can decline to send, for example while the user is at the computer. In
every other case, including an empty answer, an error, or an answer that it
reached no device, say so in exactly one plain sentence: "A notification tool
was available, but its answer does not show that this reached your phone."
Never write that a notification is being sent or was sent unless its answer
says it was sent or requested. Then stop. Why: in an unattended run the session
log is the user's only record, so it must not claim a phone alert that never
happened.

**Hard rules for unattended runs, each with its reason:**

- Do not ask questions, and do not use `AskUserQuestion`. Nobody is watching,
  and a question would hang the task.
- Do not open the app. Do not do a dry run. Why: the only output from an
  unattended run is the session log, and a notification if the app sends one.
- Never run an entry script directly. Never drop or change
  `--standing-approval`. Why: the standing-approval flag is the spending gate;
  removing it bypasses the spending check.
- Never edit a job file or a config file. Why: the job file is the user's
  approval record; changing it means the agent is approving its own spending.
- Never retry, resume, or re-run after a refusal or an incomplete result. Why:
  every such step can spend money the user did not approve.
- The "If a run comes back INCOMPLETE: resume it, don't restart" advice does
  NOT apply here. Do not resume an unattended run. Report the result and stop.

### New run

**Ask the user where the run directory should live and confirm its name.** Propose
a name using the convention, then create it:

```
<parent>/search_<subject-slug>_<YYYY-MM-DD>/
```

- `<parent>` defaults to `marketing-research/` in the current working directory.
- `<subject-slug>` is the market, lowercased and hyphenated (e.g. `pm-software`).
- `subject` in the config is the human-readable name for display (e.g. "Pm Software");
  set it explicitly so the viewer and repeat commands show the right label.
- Append `_v2`, `_v3` if the same subject is re-run the same day.

Then copy the config template into that directory:

```bash
mkdir -p <run-dir>
cp ${CLAUDE_PLUGIN_ROOT}/skills/search-intelligence/SEARCH_CONFIG.example.json <run-dir>/SEARCH_CONFIG.json
```

## Step 1: Confirm the Apify token and the card (the token saves itself)

Confirm `APIFY_TOKEN` is exported, or that `apify login` has been run, and
remind the user this tool needs a payment method on the Apify account (a real
run exceeds the free monthly credit). Report writing uses the `claude` CLI; if it
is missing, the tool still writes `synthesis_facts.json` and skips the narrative.

Check this without ever showing the token. This command prints only whether
each is there:

```bash
printenv APIFY_TOKEN >/dev/null && echo "APIFY_TOKEN is set" || echo "APIFY_TOKEN is not set"
[ -f ~/.apify/auth.json ] && echo "saved login found" || echo "no saved login"
```

Check `~/.apify/auth.json` only for whether it exists: never open, print or copy
it. Never print, quote or echo the token's value, not even part of it, and never
say whether it looks real. Never run a command that would show it, such as one
that lists the environment (`env`, `set`, or `printenv` without a name), not even
to look for the cause of a problem. In local Claude Code, never ask the user for
the token in the chat: they give it with `apify login` or the `APIFY_TOKEN`
environment variable (see `docs/SETUP.md`). If they paste one there anyway, do not
repeat it or use it: tell them in one plain sentence that it should not be shared in
the chat, and suggest they replace it in their Apify account and then use
`apify login` or the environment variable. Why: everything shown in the
conversation stays in its history, and only the user ever handles their token.

**There is no separate "save the token" step to remember.** The first time a run
resolves the token from the environment and no login is saved yet, the tool writes it
to `~/.apify/auth.json` (mode 600, never printed), and later runs read it back. If a
different login is already saved there, it is kept: the environment's token is used
for that run only, and the run says so.

What "the same environment" means, honestly:
- **Local Claude Code:** `~/.apify/auth.json` sits on the real machine, so the
  token is saved permanently (entered once, ever).
- **Cowork:** the token persists for the whole conversation (every run, and across
  app restarts). A brand-new Cowork conversation is a fresh sandbox, so the token
  is asked once there, a platform limit, not a bug. Suggest the user keep their
  token in a password manager so that one paste per conversation is painless.

In Cowork, when the user gives you the token there, never repeat it back and never
write it into any file. Put it on a command line only once, as `APIFY_TOKEN=...` in
front of the save command below, which in Cowork is required, not optional: never
in front of the run command, and never in `echo`, `printf` or any other command.
That command reads it from the environment, and the tool saves it itself. The
`export` line in `docs/SETUP.md` is for the user's own shell, not for you.

To save the token explicitly without running a scrape first (optional in local
Claude Code, where normal runs already persist it on first use; required in
Cowork, as above):

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/search-intelligence/scripts/config.py --persist-token
```

It reads the token from `APIFY_TOKEN` (never as an argument) and writes the same
`~/.apify/auth.json`. It prints a masked form of the token to confirm: never repeat
or reproduce it.

If the save command says a different login is already saved, that is not an error,
although the command ends with exit code 1. The saved login stays. Do not retry, do
not delete or edit `auth.json`, and do not run the save again. Tell the user in one
sentence that their saved login was kept. Then say which account the runs bill: in
local Claude Code, while `APIFY_TOKEN` is set in their shell, every run started from
that shell bills the token in `APIFY_TOKEN`, not the saved login; in Cowork, where the
token is never put in front of the run command, runs bill the saved login. Ask if
that is the account they want to bill. If it is not: in local Claude Code, the user
sets `APIFY_TOKEN` in their own shell to the account they want billed, or removes it
(and `APIFY_API_TOKEN`, if they set that too) so that runs bill the saved login (which
they can change themselves with `apify login`), and then starts Claude Code again from
that shell so the change reaches it. In Cowork, do not replace or delete the saved
login: a new Cowork conversation normally starts with a fresh sandbox, so the user
can start one and give the token there. If the save command says a login is kept
there too, tell the user that it cannot be changed from Cowork, and do not try.

## Step 2: Fill the config (with the user)

Open `<run-dir>/SEARCH_CONFIG.json` and set:

- **`country_code`**: the primary market as an ISO code (`"US"`, `"GB"`, `"DE"`). Use `"GB"` for the United Kingdom; `"UK"` is accepted and treated as `"GB"`.
- **`language`**: the search language (`"en"`).
- **Input: pick one mode:** `seed_terms` (you have keywords) OR `vertical`
  (a market label the tool expands from, leaving `seed_terms` empty).
- **`keyword_cap`**: the main cost lever: the most keywords to price (default
  300). Fewer keywords = cheaper run.
- **`serp_top_n`**: how many keywords get a Google search-results (ranking) pull
  (default 100). They are the highest-volume keywords of the ones priced; keywords
  with the same volume keep their listed order. If no volumes are available, the
  first keywords in listed order are used and the run says so.
- **`opportunity_kd_max`**: difficulty ceiling for the "winnable" list (default
  40).
- **`budget_caps.phase1`**: the hard USD spend ceiling.
- **`location`**: optional city string (`"New York,New York,United States"`) for
  a city-localized SERP; blank = country-level.

## Step 3: Dry-run first (the money gate)

Always preview cost before spending anything:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/search-intelligence/scripts/search_intel.py \
  --config <run-dir>/SEARCH_CONFIG.json --workdir <run-dir> --dry-run
```

This prints the full plan (keyword expansion → volume/difficulty/CPC → SERP) and a
per-step cost forecast, and spends nothing. **Show the user the forecast, check it
against `budget_caps.phase1`, and wait for explicit approval before Step 4.** For a
small sample of real output first, `--test-only` runs each step on a handful of
keywords and stops (forecast at most about USD 0.18 with two seed terms, about USD 0.23
with five or more).

**The cost to show for approval.** The dry-run ends with a `WHOLE-RUN COST
FORECAST` line. Show the user that figure: it covers every step (the per-step
lines above it can leave steps out), and it is the same number `repeat.py
forecast` gives. It is a worst case, so say "at most". The forecast covers the scraping steps only: writing the search report runs on this computer and costs nothing on Apify. Never add an
estimate of your own ("well under USD 1", "similar to last time"), and never state
what a past run cost except as that run's own closing line or notification gave it.

**A keyword Semrush has no data for.** The cost block ends with more sentences: a
keyword that comes back with no data from Semrush is asked for once more, usually at about
USD 0.02 each, inside the budget cap and counted in the total billed. If the budget cap
is a little short for that, by at most USD 0.25, the user is asked first and nothing
extra is spent without their yes; if it is short by more, only the keywords that fit
inside the cap are asked for. Give those sentences with the figure, and say they are
not part of the "at most" amount.

**Confirm the seeds resolved, not just the cost.** The dry-run prints the input
mode and the resolved seed list. In `vertical` mode especially, check that the
market label produced sensible seeds before approving: the whole run expands from
them. If neither `seed_terms` nor `vertical` is set the tool stops with a clear
message rather than scraping an empty universe, so an empty seed list is never
silently priced. Reading a zero: an empty landscape means the input did not
resolve to keywords (a *miss* to fix), not that the market has no search demand.
See `docs/SETUP.md` (the money gate).

## Step 4: Run for real (only after approval)

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/search-intelligence/scripts/search_intel.py \
  --config <run-dir>/SEARCH_CONFIG.json --workdir <run-dir>
```

The tool chains automatically: seed keywords → autocomplete expansion →
volume/difficulty/CPC per keyword → localized SERP for the top terms →
deterministic synthesis → report. It shows the forecast before each paid step and
stops at the budget cap.

Run the command above as one plain command, with the real run folder written
into it. Do not put a shell variable (such as `RUN=...`), a `cd` or a `;` in
front of it. Why: a combined command can need an approval the session cannot
give, and then the paid run never starts.

**Waiting for the run.** A paid run can take many minutes. You may run the
command above in the foreground or start it in the background
(`run_in_background`); neither is required. The Bash tool stops a foreground
command after at most 10 minutes, which would cut a long paid run off midway
after money was spent, so for a run that may take longer the background is the
safer choice. If you start it in the background, wait for it with this command
straight away:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/search-intelligence/scripts/run_status.py wait \
  --workdir <run-dir>
```

Each call waits up to about a minute and a half, then answers one of:
- `STILL RUNNING`: call the same command again at once, and keep calling it
  until the answer changes. Never end your turn while the run is going (that can
  stop it midway), and never stop waiting because it is slow: a run has no time
  limit, and slow is normal.
- `FINISHED`: the run has ended. It says how (all steps done, stopped early,
  failed, or stopped from outside) and prints the run's closing lines exactly as
  the run wrote them. These are the closing lines the rest of this step is about:
  quote them from there.
- `SILENT`: the run has not finished and its process still exists, but it has
  shown no sign of life for a while (paused, stuck, or the computer slept). Tell
  the user in its plain words and ask whether to keep waiting. Never start the run
  again while that process exists: a second run in the folder could pay for the
  same step twice.
- `DIED`: the run stopped without finishing (killed, crashed, or its process is
  gone). Tell the user in its plain words, and do not start the run again unless
  they say so.

**When the run's answer comes back.** In a run you started here (not a dry run, and
not a scheduled one), whatever the run's outcome (all steps done, stopped early,
failed, found nothing or wrote no report), the reply that reports it gives the
run's closing lines, from the `FINISHED` answer or from the end of the command's
output, in the form Step 6 gives under "Your last reply carries the run's closing
lines", before any question about what to do next. If there are none (a `DIED`
answer, a run stopped from outside, a `FINISHED` answer that says it wrote no
closing lines, or output that ends without them), say plainly that the run printed
no bill and that any charge is in the user's Apify console, and never write a bill
line of your own. Why: the user may stop reading at that question, and a run that
spent money and found nothing is the run whose bill and gaps matter most.

If the run command itself answers `REFUSED:` because another run is going in that
folder (`a run is already going in`, `another run is still working in`, or
`another run was started in`), nothing was spent: wait for that run with the
command above instead of starting another.

**Never clear a step to make it run again.** Never delete, move or edit files in a
run folder to make a step run again. Why: they are the user's data and the output
of a paid step. To redo a step, use the tool's documented command or option for
it, and when that redo is charged, tell the user first. Running the Step 4 command
again on the same folder skips the steps that already finished and does not charge
them again. That includes a step whose latest attempt failed or hit the budget cap but
whose folder still holds an earlier finished result: it is skipped and keeps that result
(the closing lines say so), and fetching it again takes a new run folder, which is
charged again for every step. Only a step with no finished result runs again. Filling in or correcting, with the user, the files any of this
plugin's skills tell you to edit (the marketing-viewer skill's `resolution.json`
corrections included) is not this.

**What the run cost.** A run you started here (not a scheduled one) ends with
one closing line. Quote its amount exactly as the tool printed it, sign included, with all four
decimals (for example 0.2810, not 0.28), and never add up the per-step `reported $...` or
`spent $...` lines above it: they are rounded to cents and are not the bill.
- `Total billed: ` then the amount exactly as printed, sign included (four decimals, for example 0.2810), then `. This is the whole run's bill: ...` means the folder was
  new and this run did every step: that amount is what the run cost.
- `Billed by this run: ` then the amount exactly as printed, sign included (four decimals, for example 0.0000), then `. This folder already held results from an earlier
  run, ...` means this was a run over a folder that already had finished steps
  (for example to write missing reports or to resume). It is only what this run
  billed, never the month's cost: say so in those words, and never present it,
  or add it to anything, as what the month cost. If you do not have the month's
  own bill from its first run, say that its cost is in their Apify console.
If the line says `see run log`, tell the user the amount could not be read and
that their Apify console shows the exact charge. A dry run prints no such line,
because it spends nothing.

**How big the run is.** The same closing line ends with the size of the results,
counted by source from the file the app reads, for example `The results in this
folder hold 106 records: Google 56, Semrush 50.` When you say
how big the run is, use only that sentence (or, once the app is open, its
`run_size`). The two state the same numbers, because both count what the app shows:
quote one of them, and never add or combine numbers from both. Never take sizes from
the per-source or banding lines in the run log (such as `[reddit] 15 records`): they
count something else, and mixed with other numbers they do not add up.

**What the run did not cover.** Only when a source returned nothing, or was only
partly searched, does the run print lines about it: they start the closing lines,
before the bill, with one line per source and its reason. Tell the user those
lines as written. When the run printed none, there is nothing to say about gaps:
never write such a line yourself, not even one saying that nothing is missing.
Anything you want to add of your own goes above the closing lines, in your own
words, and never starts the way a closing line does (never with "Run complete").
Why: the user must be able to tell what the run printed from what you add. A source
named there is a gap in what the tool could collect, not a fact about the
market: never say that a keyword has no searches or no results because of it.

**A keyword Semrush had no data for.** When one came back with no data, a closing line
before the bill says how many were asked for once more, what that cost, how many now
have data, and that the rest show as "no data from Semrush". Give it as written. A
keyword with no data is not a keyword nobody searches: never say it has 0 searches or
no demand, and never count it among the keywords with a volume.

**A second request waiting for the user's yes.** When a closing line says that asking
Semrush again for the keywords with no data can be charged at most an amount, which is a
stated extra more than what is left of the budget cap, so no second request was made,
that request waits for the user's yes to the extra. Ask the user once, in plain words,
with the closing line's figures: how many keywords, the most it can be charged, and the extra.
Wait for the answer. Only on a clear yes, run this one command, with the extra from
the closing line as a number (never a larger one):

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/search-intelligence/scripts/search_intel.py \
  --config <run-dir>/SEARCH_CONFIG.json --workdir <run-dir> --second-request --approved-extra-usd <extra>
```

It asks Semrush once, for those keywords only, and ends with its own closing lines and
the bill of this one request; give them as written, and keep their words for the three
figures: "usually about" is the usual cost, "at most" is the most it can be charged, and
"actually billed" is what was charged. A yes covers that one request: a
second yes buys nothing, and if the closing lines say the price grew past what the
user approved, nothing was spent and you ask again with the new extra. When a dry run
or the run listing says instead that the keywords now fit inside what is left of the
budget cap and no extra is needed, ask the same way, giving that figure as the most it
can be charged, and on a clear yes run the same command with `--approved-extra-usd 0`; a 0 is
only for that case. For a scheduled run's folder, the `Total billed` in its notification
then includes this request: give that total as written, and never add this request's own
bill to it. On a no, or no answer, do nothing: the keywords keep showing as "no data from Semrush". The offer
stays with the research: when the user comes back to a run that ended this way, the
dry run of its folder (the Step 3 command, which spends nothing) and the `notification`
in the run listing both say that a second request is waiting, so offer it then. In an
unattended (scheduled) run never ask this and never run this command: nobody is there
to say yes, the run spends nothing beyond the ceiling the user approved, and the
report in its notification gives the extra cost, so you offer it the next time the user
talks to you about that research. Why: asking Semrush again spends the user's money, and the budget
cap is the most they approved; only their own yes to a stated extra moves it, and an
unattended run has no one to give it.

### If a run comes back INCOMPLETE: resume it, don't restart

**This advice is for interactive runs only. If you are in an unattended
scheduled run, do NOT follow it. Report the result and stop. See "Unattended
mode: a scheduled run" above.**

A scrape can end before it finishes: the actor aborts, times out, or the run is
cut off. When that happens the tool prints a loud `⚠ RUN INCOMPLETE` block naming
the run id, and the data on disk is only what was pulled so far. **Your baseline
recovery is to resume that exact run**: resurrect the run id on Apify, which
continues the SAME dataset from where it stopped, then regenerate the outputs from
the now-complete dataset. Do this first, before anything else.

- Do NOT treat a stopped or `[ABORTED]` run as finished: a truncated corpus is
  not a smaller-but-complete one. Report it as partial until you have resumed.
- Do NOT research or "verify" the failure before resuming, and do NOT start a
  fresh run or hand-build a merge/dedupe workaround: a fresh run re-charges for
  data you already have. Resume is the cheap, correct default.
- If a closing line says a step was not started because its cost forecast was more
  than the budget cap, nothing was charged for that step, and the cap was not
  reached: the step's forecast, added to what the run had already spent, was over it.
  Offer the user both remedies and let them choose: raise `budget_caps`, or make the
  step smaller with the setting the line names (`keyword_cap`, `serp_top_n` or fewer `seed_terms`).
  Running the same command on the folder then checks that step again against the new
  cap or setting. Any line that says something was charged means that money was
  spent, whatever it says about the cap. A line that says an earlier step was stopped
  by the budget cap, and that this step did not run, is not a size problem of this
  step: running the stopped step again runs it too.
- If the tool says it stopped at the spend cap, ask the user first: raising
  `budget_caps` lets the run spend more, so raise it only after they agree to
  the new cap, then resume. Running the tool again on the folder is a new
  command, and its cap counts from USD 0 again: tell the user the new cap is what
  that command may spend on top of what was already spent. Only if resurrecting
  genuinely fails do you consider alternatives, and say so plainly.
- When the user says a run was cut off, believe them and resume; don't re-verify
  whether it "really" finished.

## Step 5: Deliver the report

Outputs land in the run directory: `keywords/`, `metrics/`, `serp/`,
`synthesis_facts.json`, and **`S1_search_landscape.md`** (the deliverable). Present
the report with its scrape-date snapshot and its sample-honesty framing intact: rankings
and volumes drift, and where only a slice of the universe was priced the report
calls its findings directional. Do not present them as a full census, and do not
add a domain-authority score; this tool surfaces Semrush's median referring-domains
signal, not a backlink index.

**What the automated check found.** The report step checks every number in the
written analysis against the counted data. It writes what it found at the top of
the report file, under the heading "What the automated check found": the folder
the report is in, the report file by name, and how many numbers it could not match
(check those before relying on them). The full list stays right after the written
analysis, and the app shows the same lines. To learn the folder and the file name
for your reply, run:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/search-intelligence/scripts/report.py check \
  --workdir <run-dir>
```

It prints those lines for any run's report, also one written before the check was
put at the top of the file. In your reply, tell the user which folder the report is
in, name the report file, and say that it starts with what the automated check
found. When `check` says the report was written before that, say instead that the
check's finding is in the note it names and that the app shows it above the report.
You do not need to repeat the check's lines, and you never count the numbers
yourself. Why: the user must learn what the tool could not check. Those words are
written by code into the file and the app, so they reach the user however your
reply is phrased.

**Every figure comes from the tool.** Every number you give about the research
(volumes, difficulties, costs per click, counts, dates, costs) must appear as
written in the run's output, in `report.py check`'s lines or in the report file:
quote it, never work one out. Do not compute a share, a difference or a sum, do
not turn a date into an age ("about nine months old"; give the date as written
instead), and do not read a range or a trend off a table yourself ("most of the
long tail has 0 to 20 searches a month"). Why: every time a model combined numbers
from the outputs, it got them slightly wrong in a plausible way, and the user
cannot tell a computed figure from a counted one.

### If the written report is missing

If the run ends without `S1_search_landscape.md` (the `claude` command-line
tool is missing, or the report step failed), never write report files yourself,
under that name or any other name in the run folder. Tell the user the collected
data is saved (the keyword, metrics and ranking files and
`synthesis_facts.json`), and that the report can be written later at no Apify
cost: once the `claude` command-line tool works, run the Step 4 command again on
the same run folder. The collection steps that already hold a finished result are
skipped and not charged again, even one whose latest attempt failed; a step with no
finished result runs again and is charged. If the user asks for a summary in the chat,
say plainly that it is your own reading of the data, not the tool's report. Why:
a file named like a report must come from the counted pipeline; the user and the
app treat it as the tool's report. Once a later run on that folder has written
it, continue with Step 5 and Step 6 as after any run.

## Step 6: Open the results in the local app (the default last step)

After every run you started here (the user approved it and its command ran; a dry
run is not one, and neither is a scheduled one), open this run in the local app,
also when the run stopped, failed, found nothing or wrote no report: follow the
`marketing-viewer` skill (`${CLAUDE_PLUGIN_ROOT}/skills/marketing-viewer/SKILL.md`). In short: check
that you are running locally, start or reuse the app with this run's parent folder as
the root, open this run in the built-in browser first (the user's default browser if that
fails), and explain it in plain words. The app shows the keywords, rankings and domains as sortable tables, next to the written report. It costs nothing and sends
nothing anywhere.

A run that found nothing still opens in the app, as an empty run. If the app's
answer says the run has no index yet, run the `reindex` command that answer names.
If it says the run stopped before it finished and has no results to show, do not
stop there or use an earlier run: run the marketing-viewer skill's `reindex` command
for this run. Either way it is free, scrapes nothing, and builds the index from
what this run already collected, so it applies to this run as well as to the older runs
the marketing-viewer skill names. Then open the run. Only when `reindex` refuses (it
says nothing has been collected yet), or the run holds 0 records, tell the user in
one plain sentence that there is nothing to show in the app for this run yet.

When you tell the user how big the run is, use only the closing line's size
sentence or the `run_size` in the app's `url` or `open-default-browser` answer
(see the marketing-viewer skill), never the run log's per-source lines such as
`[reddit] 15 records`. Quote one of the two; they state the same numbers, so never
add or combine them.

**Your last reply carries the run's closing lines.** The reply that hands over
the app must also give the run's closing lines from Step 4 (from the `FINISHED`
answer, or from the end of the command's output), word for word: first any lines
about what the run did not cover, then the `Total billed` (or `Billed by this
run`) line with its size sentence. Give them there even if you already said them
earlier. Give each closing line as a plain line of its own, exactly as printed: no
bold, italics or other formatting, no bullet or number in front, no quotation marks
around it, and nothing added inside or between the lines, so the figures read
exactly as the run printed them. Give the bill line whole, every sentence of it as printed (the sentence
`This is the whole run's bill: ...` included), and add no lines of your own among
the closing lines (for example, none saying that nothing is missing). Because that
line already states the run's size, do not add the app's `run_size` sentence as
well. Why: the user may read only your last reply, and the bill and the gaps are
the two things they must not miss.

This holds after every run you started here (not a dry run, and not a scheduled
one), also one that stopped, failed, found nothing or wrote no report, and also
when the app is not opened: your last reply about that run gives its closing lines
as above, or says it printed none as Step 4 says, even when it also asks the user
what to do next.

Do this after every run, unless the user said they do not want the app for this run. In
that case skip it and tell them in one sentence that they can say "open the app" any time.
A run that only wrote the report is a run too, and a report that already existed is
delivered the same way. Asking only for the report (or for anything else) is not saying
no to the app: skip it only when the user said in words that they do not want it.
Why it is the default: the report is a summary, and the app is where the user can check
every record behind it themselves and ask you about any of them while you work alongside
them.

## Notes

- **Cost:** a ~300-keyword run is forecast at about USD 3 to USD 7 (about USD 7 on Apify's
  free plan; paid Apify plans charge less per keyword); scales with `keyword_cap` and
  `serp_top_n`. It is not free. The forecast is always shown first.
