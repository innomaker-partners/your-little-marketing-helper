> ### How to install these tools
> These are free tools for semi-technical marketers. You do not need to be a developer. There are two ways to set up anything in this repository:
>
> **1. If the tool is a Claude plugin** (all five tools have a plugin form): copy this repository's web address and paste it into the Claude app under Customize → Plugins → Add marketplace, then choose the tool and install it.
>
> **2. For any tool, or if you would rather not touch settings**: paste the repository's web address into a chat with Claude and ask it to set the tool up for you. Claude reads that tool's own instructions and walks you through it. The steps differ by tool (a Claude plugin, a Make.com scenario, and an n8n workflow are each set up differently), and Claude handles the difference.
>
> Repository address: `https://github.com/innomaker-partners/your-little-marketing-helper`

# Workflow Workarounds -- Claude Code Plugin

Two independent skills for getting work done on platforms that fight automation. Install the plugin
once and both become available to Claude in any session.

- **platform-connector** gives Claude authenticated access to an API that sits behind OAuth,
  through a Make.com webhook proxy you own. For platforms that have an API but gate it.
- **browser-automation** teaches Claude to develop deterministic, re-runnable browser scripts for
  web workflows that have no usable API. The script it builds is the deliverable.

The two share nothing at runtime. Use either on its own.

## Who this is for

Anyone using Claude Code who keeps hitting a platform that will not cooperate: an API locked behind
OAuth that is not worth wiring into an agent by hand, or a web app with no API where the work has
to happen in the browser.

## Prerequisites

Different per skill. You only need the prerequisites for the skill you use.

**Both:**
- **Claude Code**, which requires a Claude Pro, Team, or Enterprise subscription.

**platform-connector:**
- **A Make.com account.** The free tier is enough to start; see [docs/SETUP.md](docs/SETUP.md) for
  the limits. Make.com holds the OAuth connection to the platform.
- **The Make MCP connector** (recommended) or the Make.com web UI, to build the proxy scenario once.
- **Python 3.8 or newer.** The connector's webhook caller is a single standard-library script with
  no third-party packages.
- An account on whatever platform you are connecting, with permission to authorize an OAuth app.

**browser-automation:**
- **The Claude-in-Chrome extension**, added as an MCP server, plus Chrome with the target site
  logged in.

## Quick start

1. Install the plugin. This directory is a self-contained marketplace, so register it and then
   install from it (substitute the absolute path to this directory):
   ```
   claude plugin marketplace add <path-to-this-directory>
   claude plugin install workflow-workarounds@workflow-workarounds-local
   ```
2. Confirm it registered, then restart Claude Code:
   ```
   claude plugin list
   ```
   You should see `workflow-workarounds` at user scope.
3. Use the skill you need inside any Claude Code session:
   - **API access through Make:** ask Claude to set up or use a platform connection (for example,
     "connect to my Microsoft calendar through Make and list today's events"). The
     `platform-connector` skill loads, walks the one-time scenario build, then runs your calls.
   - **A browser automation:** ask Claude to build an automation for a web workflow (for example,
     "build me a re-runnable script that pulls my LinkedIn post analytics"). The
     `browser-automation` skill loads and runs its development loop.

Full setup, including the one-time Make.com scenario build, is in [docs/SETUP.md](docs/SETUP.md).
Step-by-step use, troubleshooting, and a data-handling note are in [USER_MANUAL.md](USER_MANUAL.md).

## Folder contents

| Path | Description |
|---|---|
| `.claude-plugin/plugin.json` | Plugin manifest: name (`workflow-workarounds`), version, description |
| `skills/platform-connector/SKILL.md` | The API-connector skill Claude loads at runtime |
| `skills/browser-automation/SKILL.md` | The browser-automation skill Claude loads at runtime |
| `REFERENCE.md` | Shared reference: the connector's endpoint registry and error taxonomy, and the browser skill's worked-example discoveries |
| `scripts/connector/platform_request.py` (+ `tests/`) | Standard-library webhook caller for the connector |
| `scripts/browser/dom_patterns.json` | Baseline selector patterns the browser skill reads |
| `blueprints/universal_proxy.json` | Importable Make.com scenario for the connector |
| `.env.example` | Template for the two connector values (`MAKE_WEBHOOK_URL`, `MAKE_SHARE_TOKEN`); copy to a `.env` in your own project |
| `docs/SETUP.md` | Installation, the one-time Make.com build, and prerequisites per skill |
