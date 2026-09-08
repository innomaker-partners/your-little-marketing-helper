> ### How to install these tools
> These are free tools for semi-technical marketers. You do not need to be a developer. There are two ways to set up anything in this repository:
>
> **1. If the tool is a Claude plugin** (all five tools have a plugin form): copy this repository's web address and paste it into the Claude app under Customize → Plugins → Add marketplace, then choose the tool and install it.
>
> **2. For any tool, or if you would rather not touch settings**: paste the repository's web address into a chat with Claude and ask it to set the tool up for you. Claude reads that tool's own instructions and walks you through it. The steps differ by tool (a Claude plugin, a Make.com scenario, and an n8n workflow are each set up differently), and Claude handles the difference.
>
> Repository address: `https://github.com/innomaker-partners/your-little-marketing-helper`

# Meeting Intel - Cowork Plugin

Research external meeting participants and produce intelligence briefs
before your calls.

Meeting Intel is a Cowork plugin. It pulls today's calendar events,
filters out your internal colleagues, researches each external participant
via the web, and delivers a structured brief with company profiles, person
backgrounds, confidence tags, and conversation starters.

> **⚠️ Enable your connectors before the first run, with access granted.** In the
> Claude app's Connector menu (Settings > Connectors):
> - **Apify** powers the LinkedIn research. Without it, the LinkedIn step does not
>   run and the tool falls back to a plain web search, which is noticeably less
>   reliable.
> - **Microsoft 365 or Google** gives it your calendar (to read the day's meetings)
>   and, optionally, email delivery of the briefs.
>
> If a connector is missing or lacks access, the tool quietly degrades to the weaker
> web-search path instead of failing loudly.

This folder is the complete, self-contained distribution. A user who
downloads only this folder has everything needed to install and run the
plugin.

## Who this is for

Cowork users who want automatic pre-meeting research without leaving
Cowork. Everything goes through Cowork's native connector system -
calendar, email, messaging, and the Apify connector that powers the
LinkedIn research - connect once in Settings and the plugin handles the
rest. Company research uses Claude's built-in web access and needs no
connector at all.

## How this differs from the plain Claude Code skill version

The Claude Code skill version uses MCP servers for calendar access and
requires separate API credentials. This Cowork plugin replaces all of
that with Cowork's built-in connector system. There is no `.env` file to
create and no MCP config to edit - the `.mcp.json` in this folder is
empty by design.

## Prerequisites

- A Claude Cowork account with the plugin runtime enabled
- Microsoft 365 or Google Calendar connected in Cowork (Settings >
  Connectors) for automatic calendar access

Without a calendar connection you can still run `/meeting-intel:prep` and
provide meeting details manually when asked.

An Apify account is needed for the LinkedIn research step: the plugin
runs the same two LinkedIn actors as the Make.com versions of this tool,
through the Apify connector (one token paste in Settings, see
CONNECTORS.md). Apify offers a free trial; without the connector,
LinkedIn research falls back to a weaker plain web search. No MCP
configuration is needed.

## Quick start

1. Install the plugin: `claude plugin install .`
2. Connect your calendar in Cowork: Settings > Connectors > your calendar
   provider
3. Run `/meeting-intel:prep` - on first run the plugin walks you through
   a short setup wizard (email domain, meeting role, brief output folder)
4. From the second run onward, briefs are generated automatically for
   today's external meetings

## Files in this folder

| File | Purpose |
|---|---|
| `.claude-plugin/plugin.json` | Plugin manifest |
| `.mcp.json` | MCP server config (empty - no MCP servers needed) |
| `CONNECTORS.md` | Supported connectors and how to connect them |
| `commands/prep.md` | Definition of the `/meeting-intel:prep` command |
| `skills/meeting-research/SKILL.md` | Full skill behavior and brief format |
| `config/config.example.json` | Config template; copy to `config/config.json` and edit |
| `README.md` | This file |
| `USER_MANUAL.md` | Full installation, setup, usage, and troubleshooting guide |

## Further reading

See [USER_MANUAL.md](USER_MANUAL.md) for step-by-step installation,
connector setup, first-run walkthrough, command reference, brief format,
customization options, and a troubleshooting section.

See [CONNECTORS.md](CONNECTORS.md) for the full list of supported
calendar, email, and messaging integrations.
