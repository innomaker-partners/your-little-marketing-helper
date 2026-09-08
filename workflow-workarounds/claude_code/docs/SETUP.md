# Workflow Workarounds -- Setup Guide

This plugin carries two independent skills. Set up only the one you need; they share nothing.

- [Install the plugin](#install-the-plugin)
- [platform-connector setup](#platform-connector-setup)
- [browser-automation setup](#browser-automation-setup)
- [What this costs](#what-this-costs)

---

## Install the plugin

This directory is a self-contained Claude Code marketplace. From your terminal, register the folder
as a marketplace and then install the plugin from it (substitute the absolute path to this
directory):

```
claude plugin marketplace add <path-to-this-directory>
claude plugin install workflow-workarounds@workflow-workarounds-local
```

Then confirm:

```
claude plugin list
```

You should see `workflow-workarounds` at user scope. Restart Claude Code, and both skills are
available to Claude in any session. Nothing else is needed to install; the per-skill setup below is
only for the skill you actually use.

---

## platform-connector setup

This skill routes Claude's API calls through a Make.com scenario you own, so Make holds the OAuth
and Claude never handles the platform's credentials. Setting it up is a one-time build.

### What you need

- **A Make.com account.** The free tier is enough to start (see costs below).
- **The Make MCP connector in Claude**, which lets Claude build the scenario for you
  programmatically. In Claude, open Settings, then Connectors, and add the Make integration. If you
  would rather not, the skill can walk you through the Make.com web UI instead.
- **Python 3.8 or newer**, for the webhook caller. Check with `python3 --version`.
- **An account on the platform you are connecting**, able to authorize an OAuth app.

### Building the scenario

You do not build it by hand. Inside a Claude Code session, ask Claude to set up the connection (for
example, "set up the platform connector for Microsoft Graph"). The `platform-connector` skill takes
over and:

1. Creates the webhook and the proxy scenario in your Make account, from the bundled blueprint.
2. Starts the OAuth connection and hands you a link. Granting access is the one step only you can
   do: you sign in to the platform yourself, and Claude never enters your credentials.
3. Writes the two resulting values, `MAKE_WEBHOOK_URL` and `MAKE_SHARE_TOKEN`, into a `.env` file in
   your own project directory. (`.env.example` in this folder is the template. The `.env` lives with
   your project, never inside the plugin.)
4. Runs a connection test and a deliberate wrong-token test, to confirm the proxy works and rejects
   bad tokens before you connect anything real.

After that, Claude runs your calls through the connector. Microsoft Graph and Meta are the two
worked examples the skill knows in depth; any other API Make.com can authenticate to is added as one
more route, which the skill's "Adding a new provider" section covers.

---

## browser-automation setup

This skill develops browser scripts using the Claude-in-Chrome tools, so it needs the Chrome
extension connected.

### What you need

- **The Claude-in-Chrome extension**, installed from the Chrome Web Store and added as an MCP server:
  ```
  claude mcp add claude-in-chrome
  ```
  Restart Claude Code after adding it.
- **Chrome, with the target site already logged in.** The skill never clicks login or submit
  buttons for you; you authenticate manually first.
- **Site permissions** granted to the extension for the domain you are automating.

The skill runs its own pre-flight checks for all of this at the start of a session and tells you
exactly what to fix if something is missing.

---

## What this costs

**platform-connector.** The only paid dependency is Make.com, and light use fits the free tier:
1,000 operations per month and 2 active scenarios. Each proxy call uses at least 2 operations (the
webhook plus the API call), so roughly 15 calls a day reaches the monthly free-tier limit. Heavier
use needs a paid Make.com plan. The platform APIs themselves are free to call within their own rate
limits.

**browser-automation.** No paid dependency beyond your Claude subscription. The Claude-in-Chrome
extension drives a browser you already have.

Both skills require a Claude subscription, since they run inside Claude Code.
