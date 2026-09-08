# Workflow Workarounds -- User Manual (Claude Code Plugin)

## Contents

1. [What this plugin is](#what-this-plugin-is)
2. [Installation](#installation)
3. [The platform-connector skill](#the-platform-connector-skill)
4. [The browser-automation skill](#the-browser-automation-skill)
5. [Data handling and safety](#data-handling-and-safety)
6. [Troubleshooting](#troubleshooting)
7. [Developer note](#developer-note)
8. [Updating and uninstalling](#updating-and-uninstalling)

---

## What this plugin is

Workflow Workarounds is two independent skills for platforms that block clean programmatic access.
They address two different obstacles, and you use whichever the task calls for.

- **platform-connector** is for a platform that has an API but sits behind an OAuth wall. Instead of
  wiring that OAuth into Claude, you stand up a small Make.com scenario that holds the connection.
  Claude sends a structured request through one webhook, Make routes it to the platform API, and the
  response comes back. You get authenticated API access without handing Claude any credentials.
- **browser-automation** is for a web workflow with no usable API at all. Claude develops a
  deterministic, re-runnable browser script for the workflow using the Claude-in-Chrome tools. The
  script is the deliverable, not the data from a single run.

The two skills share nothing at runtime. Installing the plugin makes both available; you can use one
and ignore the other. platform-connector needs a Make.com account and never touches the browser;
browser-automation needs the Chrome extension and never touches Make.

---

## Installation

### Step 1: Place the folder

Put this plugin directory somewhere permanent on your machine. Claude Code records this path as the
source of the plugin and reads it when checking for updates, so moving the folder later means
reinstalling from the new location.

### Step 2: Install the plugin

This directory is a self-contained Claude Code marketplace: it carries its own marketplace manifest,
so you register the folder as a marketplace and then install the plugin from it. Run both commands,
substituting the absolute path to this directory:

```
claude plugin marketplace add <absolute-path-to-this-directory>
claude plugin install workflow-workarounds@workflow-workarounds-local
```

Confirm it registered:

```
claude plugin list
```

You should see `workflow-workarounds` in the output at user scope. Restart Claude Code so the plugin
loads into your session.

### Step 3: Set up the skill you need

Both skills are now loadable, but each has a one-time setup before its first real use. Do only the
one for the skill you plan to use. Full steps are in [docs/SETUP.md](docs/SETUP.md); the sections
below summarize day-to-day use.

---

## The platform-connector skill

### What it does

It gives Claude authenticated access to an API that lives behind OAuth, by putting a Make.com
scenario in the middle. Make holds the OAuth connection to the platform. Claude calls one webhook
with a small JSON body describing the request (which platform, which endpoint, which method, any
parameters, and a shared secret), and Make routes it to the correct platform API and returns the
response. Claude never sees or stores the platform's OAuth token.

Microsoft Graph (Calendar, Files, Mail) and Meta (Instagram, Facebook Pages) are the two worked
examples the skill knows in depth. They are examples, not the skill's limit: any API Make.com can
authenticate to is added as one more route, and the skill's "Adding a new provider" section is the
recipe for wiring one this manual never names.

### One-time setup

Ask Claude to set up the connector (for example, "set up the platform connector for my Microsoft
calendar"). The skill builds the Make.com scenario for you, ideally through the Make MCP connector so
you avoid the web UI entirely. During the build it hands you one OAuth consent link to click; that
consent is the only step that must be you, signing in with your own account. When the build
finishes, the skill writes two values into a `.env` file in your project directory and runs a
connection test plus a deliberate wrong-token test to prove the proxy both works and rejects bad
tokens. See [docs/SETUP.md](docs/SETUP.md) for the full walk-through.

### Day-to-day use

Once the scenario exists and `.env` is written, ask Claude for what you want in plain language (for
example, "list my calendar events for tomorrow" or "pull my Instagram post insights for last week").
The skill looks the operation up in its endpoint registry, gathers any parameters it needs from you,
runs the call through the webhook, and presents the result. Building the scenario is the only step
that uses the Make MCP; ordinary calls afterward do not.

### The `.env` file

The connector reads two values from a `.env` file in your own project directory:

- `MAKE_WEBHOOK_URL`: the webhook address Make generated for your scenario.
- `MAKE_SHARE_TOKEN`: a secret you choose, checked by the scenario so that only requests carrying it
  reach your platform connections.

This file is yours and stays in your project. It is never written into the plugin's own directory,
because that directory is a Claude-managed install location, not a place for your credentials. If
you ever see `share_token_mismatch`, the value in `.env` and the one set in the Make scenario have
diverged; see Troubleshooting.

### Reads are safe; writes need your say-so

Once the connection test passes, the skill will run read calls (GET and list operations) freely.
Anything that creates, edits, deletes, sends, or shares requires you to approve that specific action
first, and the skill confirms it against one real call before relying on it. A silent write to a
live calendar or mailbox is not recoverable, so the skill treats anything it cannot confirm is a
read as a write and asks.

---

## The browser-automation skill

### What it does

It develops a deterministic, re-runnable browser script for a web workflow that has no usable API,
using the Claude-in-Chrome tools. The deliverable is the script. Data that appears on screen during
development is evidence the script works, never the end product: the script either exists on disk and
re-runs from scratch, or the job is not done.

LinkedIn analytics extraction and Outlook draft creation are the two worked examples the skill
teaches its method on. The method, not those two targets, is the product: it transfers to any page.

### Prerequisites

- The Claude-in-Chrome extension installed and added as an MCP server (`claude mcp add
  claude-in-chrome`, then restart Claude Code).
- Chrome, with the target site already logged in. The skill never clicks login or submit buttons for
  you; you authenticate manually before it starts.
- Site permissions granted to the extension for the domain you are automating.

The skill runs a full pre-flight for all of this and tells you exactly what to fix if anything is
missing.

### How a session goes

Ask Claude to build an automation for a specific workflow. The skill then runs a development loop:
it probes the live page to discover the right selectors, tests each in isolation, and freezes the
ones that work into a stored script. When the pieces are frozen it assembles the whole script and
executes it once, cold, as a single run, and reads back what that run returns. That cold proving run,
not anything Claude saw during development, is what certifies the automation. You end up with a script
file you can re-run yourself, and the skill can package it wherever you want: your own skill
directory, a working folder, or a standalone file.

### Safety while it runs

The skill will not click Send, Submit, Publish, Delete, or Discard without your explicit consent for
that specific action in that session. If it meets a CAPTCHA or a challenge, it stops immediately and
does not retry, and it will tell you the platform needs to rest before anything resumes. On LinkedIn
it holds to conservative rate limits, because an account restriction there can take days to appeal.
These rules travel inside the script it hands you, so they still hold when the script runs later
without the plugin loaded.

---

## Data handling and safety

**platform-connector.** The data Claude pulls is your own calendar, mail, files, or platform
metrics. The skill renders it locally by default (a file or an HTML page on disk) and does not
upload, publish, email, or share it with any third-party service unless you explicitly ask. When it
prints a request or response to explain a result, it masks the webhook URL, the share token, and any
OAuth token. Your platform's OAuth token lives in Make.com, not on your machine and not in Claude.

**browser-automation.** Everything runs through your own logged-in browser session, using the
Claude-in-Chrome tools and only those. The skill does not reach around a blocked page to a different
tool or API to get the same data another way; if a page genuinely cannot be automated, it stops and
tells you, rather than quietly succeeding through a side channel that would not work on your install.

Neither skill sends anything to InnoMaker or to any server the plugin author controls. The plugin is
a set of instructions and a small script; it has no telemetry.

---

## Troubleshooting

Full error tables for both skills are in [REFERENCE.md](REFERENCE.md). The highest-impact issues:

### platform-connector

**`share_token_mismatch` (401).** The `MAKE_SHARE_TOKEN` in your `.env` and the token value set in
the Make scenario have diverged. Open the scenario in Make, check the token-check filter (or the
`SHARE_TOKEN` variable), and make the two match. A common cause is quoting: if your token contains a
`$` or a backtick, wrap it in single quotes in `.env`, because double quotes let the shell mangle it.

**The wrong-token test passes when it should fail.** If you run the connection test with a token you
know is wrong and all three checks still pass, stop and do not connect any accounts. It means
requests are reaching your platform connections without a token check, so anyone with the webhook URL
could read your data. Inspect the outer router filters in the scenario until a wrong token is
rejected.

**`oauth_token_expired` (401).** The platform's OAuth token inside Make expired. Reauthorize the
connection in Make: open the scenario, click the failing module, then Connection, then Reauthorize.

**A call returns `"Accepted"` with `http_200`.** The request reached Make but matched no route.
Check that the `platform` value exactly matches one of your scenario's route filters, and run the
connector's `--test` to confirm the token is accepted.

### browser-automation

**The extension is not connected.** If the skill reports that Claude-in-Chrome tools are
unavailable, install the extension and add it as an MCP server (`claude mcp add claude-in-chrome`),
then restart Claude Code.

**A navigation seemed to work but the page is wrong.** Redirects are silent and return no error. The
skill checks the actually-landed URL after every navigation for this reason; if you are debugging by
hand, do the same rather than trusting that the URL you asked for is the one you got.

**The automation stalls with no progress.** The tab it drives must stay the active tab of its window,
and the window must not be minimized. Chrome does not paint a background tab, so a screenshot of one
comes back blank. Bring the tab back to the front of its window; you can keep working in other
windows meanwhile.

---

## Developer note

To run the connector's test suite:

```bash
cd scripts/connector
uv run --with pytest -- python3 -m pytest tests/
```

The system Python installation typically does not include pytest. The `uv` invocation installs it in
an isolated environment without modifying your system packages. The tests cover the webhook caller's
payload construction, parameter encoding, and error diagnosis; they make no network calls.

---

## Updating and uninstalling

**To update:** replace the contents of this directory with the new version, then refresh the
marketplace and the plugin, and restart Claude Code:

```
claude plugin marketplace update workflow-workarounds-local
claude plugin update workflow-workarounds
```

Your `.env` file lives in your own project directory, not here, so it is untouched by an update.

**To uninstall:**

```
claude plugin remove workflow-workarounds
```

To also forget the local marketplace, run `claude plugin marketplace remove workflow-workarounds-local`.

This removes the plugin registration from Claude Code. Your `.env` file, any Make.com scenario you
built, and any browser scripts the skill packaged for you all remain where they are; remove them
separately if you want to. Deleting the plugin does not touch the Make.com scenario, which lives in
your Make account.
