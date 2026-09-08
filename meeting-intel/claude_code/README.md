> ### How to install these tools
> These are free tools for semi-technical marketers. You do not need to be a developer. There are two ways to set up anything in this repository:
>
> **1. If the tool is a Claude plugin** (all five tools have a plugin form): copy this repository's web address and paste it into the Claude app under Customize → Plugins → Add marketplace, then choose the tool and install it.
>
> **2. For any tool, or if you would rather not touch settings**: paste the repository's web address into a chat with Claude and ask it to set the tool up for you. Claude reads that tool's own instructions and walks you through it. The steps differ by tool (a Claude plugin, a Make.com scenario, and an n8n workflow are each set up differently), and Claude handles the difference.
>
> Repository address: `https://github.com/innomaker-partners/your-little-marketing-helper`

# Meeting Intel -- Claude Code Plugin

Research the external participants of your day's calendar meetings and receive a structured intelligence brief before each call. This version runs as a Claude Code plugin: install it once, complete a short first-run configuration, and invoke it with `/meeting-intel:meeting-intel` inside any Claude Code session.

> **⚠️ Enable your connectors before the first run, with access granted.** This plugin depends on them:
> - **Apify** powers the LinkedIn research. Without it, the LinkedIn step does not run and the tool falls back to a plain web search, which is noticeably less reliable.
> - **Google Calendar or Microsoft 365** (your calendar, plus email if you want briefs delivered) lets it read the day's meetings automatically.
>
> Set these up first: in the Claude app's Connector menu (Settings → Connectors) for the desktop app and for scheduled Routines, or via your local MCP config for the Claude Code CLI (see [docs/SETUP.md](docs/SETUP.md)). If a connector is missing or lacks access, the tool quietly degrades to the weaker web-search path instead of failing loudly.

## Who this is for

Anyone who uses Claude Code and wants per-meeting participant research -- company profiles, LinkedIn summaries, and conversation starters -- without switching tools or manually preparing notes.

## Prerequisites

- **Claude Code** -- requires a Claude Pro ($20/month), Team, or Enterprise subscription
- **Apify account** at [apify.com](https://apify.com) -- runs the LinkedIn profile research component; no LinkedIn credentials required. The Starter plan costs approximately $49/month after the 30-day free trial.
- **Google Calendar or Microsoft Outlook** -- connected via MCP so the plugin can read today's events
- **Gmail** (optional) -- for email delivery of briefs
- **Python 3** -- used by the calendar parsing helper script; no third-party packages required

**Estimated ongoing cost:** approximately $50-70/month once free trials end. Both Apify and Claude Code offer trials so you can run the full pipeline before paying.

## Quick start

1. Install the plugin. This directory is a self-contained marketplace, so register it and then
   install from it (substitute the absolute path to this directory), then restart Claude Code:
   ```
   claude plugin marketplace add <path-to-this-directory>
   claude plugin install meeting-intel@meeting-intel-local
   ```
2. Configure your Google Calendar (or Outlook) and Apify MCP connectors. Full steps are in [docs/SETUP.md](docs/SETUP.md).
3. Inside a Claude Code session, run:
   ```
   /meeting-intel:meeting-intel
   ```
   The first run starts a brief setup flow that writes `config/config.json`. Subsequent runs go straight to research.

Full step-by-step instructions, troubleshooting, and a privacy note are in [USER_MANUAL.md](USER_MANUAL.md).

## Folder contents

| Path | Description |
|------|-------------|
| `.claude-plugin/plugin.json` | Plugin manifest: name (`meeting-intel`), version, and skill registry path |
| `config/config.example.json` | Example configuration -- copy to `config/config.json` if you prefer to configure manually rather than through the first-run flow |
| `docs/BRIEF_TEMPLATE.md` | Reference for the output brief format, confidence tags, and archetype descriptions |
| `docs/SETUP.md` | Installation guide, cost summary, and MCP connector setup |
| `scripts/parse_calendar.py` | Helper script that filters external participants from calendar event JSON (invoked internally by the skill) |
| `skills/meeting-intel/SKILL.md` | Skill instructions loaded by Claude Code at runtime |
| `tests/test_parse_calendar.py` | Test suite for the calendar parsing script |
