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

Claude Code may copy the plugin into its own plugin cache (`~/.claude/plugins/cache/`),
and an update can replace that copy, so do not keep anything of yours in the plugin's
folder and do not edit files there. You never need to: you run each tool against a
**run directory** of your own (see below), and the plugin's scripts are reached
through the `${CLAUDE_PLUGIN_ROOT}` path the skills already use.

## What you need

- **An Apify account and API token**, free to create at
  [apify.com](https://apify.com). The pain-and-messaging example run is forecast
  well inside Apify's free monthly credit; an ad-intelligence run can be forecast
  above it, though a real run usually costs less; **search-intelligence meters per
  keyword and needs a card** (a ~300-keyword run is forecast at about $3 to $7,
  the higher figure on Apify's free plan; paid Apify plans cost less).
- **Claude Code CLI**, installed and signed in: the pain-and-messaging and
  search-intelligence report steps call it locally, so there is no separate LLM API
  key and no per-report cost beyond your normal Claude usage. ad-intelligence uses
  it only to read the text off Google ad images; its report is filled in from a
  bundle by Claude in your session or any model you choose.
- **Python 3.9 or newer** (a Mac already has it): standard library only, nothing to `pip install`.
- **A recent Node.js, only if you choose the Apify CLI route** for the token
  (below; the Apify CLI install page lists the version it needs). The route that comes first needs nothing beyond Python.

## Give the tools your Apify token

First copy your token from the **API & Integrations** page of the Apify Console
(`console.apify.com/settings/integrations`). Treat it like a password.

**Do not paste the token into a chat with Claude.** Everything shown in a
conversation stays in its history, and the skills never need you to: you give the
token to your own terminal, as below.

The tools find the token in two places, in this order: the `APIFY_TOKEN`
environment variable of the process Claude Code runs them in, then the saved login
file `~/.apify/auth.json`. Route 1 puts the token in the file, which needs nothing
beyond the Python you already have.

### Route 1 (recommended): save it once with the plugin's own script

On a Mac, in Terminal.app. This route needs nothing beyond the Python you already
have: it does not use the `claude` command, an export line or your shell profile.

**Step 1.** Open a **new** Terminal window (press Cmd and Space, type `Terminal`,
press Return).

**Step 2.** Copy the whole block below, paste it into that window and press Return.
It finds the folder where Claude Code keeps the marketing-scraping plugin (the newest
enabled install, if you have more than one) and runs the plugin's save script from
there. The script then asks for your token: paste it and press Return. Nothing is
shown as you paste, and the token is not recorded in your shell history, because it is
never typed on a command line. The token is the long text that starts with
`apify_api_` on the Apify Console page named above; the script refuses anything else
and saves nothing.

```bash
P=$(python3 -c '
import json, os, sys
try:
    home = os.path.expanduser("~")
    root = os.environ.get("CLAUDE_CODE_PLUGIN_CACHE_DIR") or os.path.join(home, ".claude", "plugins")
    installs = json.load(open(os.path.join(root, "installed_plugins.json")))["plugins"]
    try:
        settings = json.load(open(os.path.join(home, ".claude", "settings.json")))
        off = [k for k, v in settings.get("enabledPlugins", {}).items() if v is False]
    except (OSError, ValueError, AttributeError):
        off = []
    found = []
    for pid, entries in installs.items():
        if not pid.startswith("marketing-scraping@"):
            continue
        for e in (entries if isinstance(entries, list) else [entries]):
            path = str(e.get("installPath") or "") if isinstance(e, dict) else ""
            if path and os.path.isfile(os.path.join(path, "skills", "search-intelligence", "scripts", "config.py")):
                found.append((pid not in off, str(e.get("lastUpdated") or ""), path))
    print(max(found)[2])
except Exception:
    sys.exit("Could not find the marketing-scraping plugin automatically. If it is not installed yet, install it with the two commands in the Install section of this guide (SETUP.md) and run this block again. If it is installed, use the manual line under this block in the same guide (Route 1), or Route 2.")
') && python3 "$P/skills/search-intelligence/scripts/config.py" --persist-token
```

**The manual line.** If the block cannot find the plugin but it is installed, run
this instead, with the plugin's folder in place of `<plugin folder>`:

```bash
python3 "<plugin folder>/skills/search-intelligence/scripts/config.py" --persist-token
```

To find the plugin's folder:

1. In Terminal, run `claude plugin list`. Find the entry that starts with
   `marketing-scraping@`. Under it is a line that starts "Read from:" (on some
   installs the line starts "Path:") followed by a folder. Copy that folder.
2. Paste it in place of `<plugin folder>` and keep the double quotes around it, so a
   folder name with a space in it still works.

If your entry shows no such line, use the folder you added as a marketplace, the one
that holds the `skills` folder.

Any of the three tools' `config.py` does the same; they share one token file.

**Step 3.** Check the message. A line that starts "Saved Apify token", then the last
four characters of your token and the full path of your `auth.json` file, and ends
"(mode 600)." means it worked. "Apify token" followed by the same four characters
and "was already saved" means it already was. "No token was entered" means you
pressed Return on an empty line: run the block again. "That does not look like an
Apify API token" means what you pasted is not just `apify_api_` followed by letters and
digits (a stray space, semicolon or quote, a token pasted twice, or a partial paste):
nothing was saved, copy the token from the Apify Console and run the block again. "A different Apify login is already saved" means a login from an earlier
`apify login` was kept and nothing was saved (see below). "Could not find the
marketing-scraping plugin automatically" means the block could not locate it: install
it (the two commands under "Install") if you have not, or use the manual line above.

**Step 4.** Close the window. The token is now in `~/.apify/auth.json`. Claude Code
reads that file each time a tool runs, so a Claude Code session that was already open
picks it up, with no restart.

The script reads the token from the `APIFY_TOKEN` environment variable if it is set
and otherwise asks for it, hidden, when it is run by a person at a terminal (never as
an argument). If you paste a whole line at the prompt that starts with the word
export, or with the variable name and an equals sign, it uses the value after the
equals sign. It merges the token into `~/.apify/auth.json` and prints a masked
confirmation. If a different login is already saved there, such as an earlier
`apify login`, that login is kept, nothing is saved, and the command says so.

### Route 2: the Apify CLI

`npm install -g apify-cli`, then `apify login`. The tools read `~/.apify/auth.json`
automatically. This route needs a recent Node.js, which includes `npm` (the Apify CLI install page
lists the version it needs). A stock Mac does
not have it: install it with the macOS installer from nodejs.org, then open a new
Terminal window before running the commands. If `npm` reports a permissions error
(`EACCES`), use Route 1 instead. Apify's own page lists its other install routes:
`docs.apify.com/cli/docs/installation`.

### Route 3: the environment variable, kept in your shell profile

Add `export APIFY_TOKEN=apify_api_...` as a line in the `~/.zshrc` file in your home
folder (create the file if it does not exist). Every tool reads `APIFY_TOKEN` first.

Add it with a text editor, not by typing the line into a Terminal window: what you
type into Terminal is kept in plain text in your shell history (`~/.zsh_history`).

**An edit to a shell profile such as `~/.zshrc` reaches only terminal windows, and
the Claude Code sessions started from them, that were opened after the edit.** A
window or a Claude Code session that was already running does not see it, and
neither does the Route 1 script run in such a window (it stops with "Set APIFY_TOKEN
in the environment first"). Open a new window, and start a new Claude Code session
from it. Route 1 does not depend on a profile at all, which is why it comes first.

**Nothing else to do: the token saves itself.** Whichever way the token reaches a
run, the first run that reads `APIFY_TOKEN` from the environment also saves it to
`~/.apify/auth.json` automatically (unless a different login is already saved there:
that one is kept), so later runs never re-ask. Route 1 is that save, done by hand
without running a scrape.

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
  - `itunes.apple.com` (App Store)
  - `play.google.com` (Google Play)
  - `www.linkedin.com` (LinkedIn company lookup)
  - **the Trustpilot domains in your pain-and-messaging config** (for example
    `acmeplumbing.com`): the tool asks each site whether it answers and where it
    redirects, to catch a typo or a different name on Trustpilot. These are the
    only entries that vary by run, and you already know them: add the ones you
    entered. A domain you did not allow shows as "could not be checked" and is used
    as written; the run is not stopped. ad-intelligence does not fetch competitor
    websites.
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
so a run's own files are kept out of the plugin's folder.

---

## The local app (opens after every run)

At the end of each run the skill opens the results in a small local app (the
`marketing-viewer` skill). Nothing to install: it runs on the same Python as the tools,
standard library only.

- It runs on your computer only, at an address like `http://127.0.0.1:53817/`, and
  reads only the run folders under the folder it was started on (by default
  `marketing-research/`). It never scrapes, never spends money and never reads your
  Apify token.
- It opens in the browser built into the Claude desktop app when there is one, and
  otherwise in your default browser.
- It keeps running so you can come back to it. Ask Claude to stop it, or it stops
  when you restart your computer.
- It writes only small files in `.viewer` folders: your favorites, notes and trash,
  and saved views, inside each run; the address of the running app and the page you
  have open, in the root folder. The scraped data and reports are never changed.
- A run made before the app existed shows as "not indexed". Ask Claude to re-index
  it: that is free and reads only what is already on disk.
- It needs your files to stay on your computer. In a cloud session there is nothing
  lasting to show; Claude says so instead of starting it.

---

## The money gate: always dry-run first

These tools spend real money on **your** Apify account. Every tool supports
`--dry-run`, which resolves your inputs, prints the full plan and a **cost
forecast**, and spends nothing. The skills always run the dry-run first, show you
the forecast, and wait for your go-ahead before the paid run. Never skip it.

**Every run is also hard-capped, automatically.** On top of the forecast, each
actor run is launched with a spend ceiling of about 1.5 times its forecast, and
never more than what is left of your budget cap. That ceiling is passed to Apify
as a hard billing cap, and the tool also watches the
live charge while the run is going and stops the run itself if it ever crosses
the ceiling. This is what keeps a scraper that ignores its own limits from
running up a surprise bill: if a review scraper is told to fetch a small number
and tries to fetch far more, the cap stops the charge, not your card. You do not
configure this. It is on for every run.

**search-intelligence has one more request that is not in its forecast.** A keyword
that comes back with no data from Semrush is asked for once more, usually at about
$0.02 each. That request is counted inside your budget cap and in the total billed,
but not in the forecast figure. If your budget cap is short for it by up to $0.25,
nothing is spent and you are asked first. In a scheduled month nobody is there to
ask, and the second request is still made when it fits inside the lower of your
budget cap and the ceiling you approved. Only the case short by up to $0.25 waits:
nothing is spent on it, and those keywords stay "no data from Semrush" until you next
talk to Claude about that research and agree to the small extra cost. If it is short by
more, only the keywords that fit inside the cap are asked for. A keyword with no data
shows as "no data from Semrush", never as 0 searches.

**A monthly repeat is approved once, as a ceiling.** When a repeat runs by itself,
nobody is there to approve each month's forecast. You approve the most one month may
spend, and a month whose forecast is above it does not start and spends nothing. See
[REPEATS.md](REPEATS.md).

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
that can mean very different things, and the tools keep them separate:

- **The target did not resolve** (no company id found, a domain that does not
  exist, a market label that produced no keywords). This is a *miss*: the tool
  could not find what to look at. It is flagged at the gate, and the fix is to
  correct the input.
- **The target resolved fine, and nothing came back.** This can be a fact about
  the market (a real company that runs no ads on that network, a real product
  with no reviews yet), or a scraper that fetched nothing. When the scraper says
  it did not work (for example, that it is under maintenance), the run names it
  as a gap. Otherwise the tool cannot tell the two apart, so it calls the zero
  unconfirmed rather than a fact.
- **You asked for 0.** A source whose count setting is 0 is not run and costs
  nothing; the run names it as not run by your settings, never as an empty result.
  There are two exceptions. A category run in pain-and-messaging still pays to find
  the places, and a search `keyword_cap` of 0 still pays for keyword ideas.

Never treat the first as the second. "We could not find this company" is not the
same as "this company runs no ads," and the tools are built so you can always
tell which one you are looking at.
