# Meeting Intel -- Setup Guide

## What this costs

Before you start, here is what to budget:

- **Apify:** ~$49/month (Starter plan, after a 30-day free trial). Apify runs the LinkedIn research component. Pricing is at apify.com/pricing.
- **Claude:** $20/month (Pro plan), or included in Team or Enterprise. Claude Code requires one of these plans. Pricing is at anthropic.com/pricing.
- **Estimated total:** ~$50-70/month once both trials end.

If you are evaluating, both Apify and Claude offer free trials -- you can run the full pipeline without paying until those expire.

---

## What you need

- **Claude Code** -- requires a Claude Pro, Team, or Enterprise subscription
- **An Apify account** (apify.com) -- used for LinkedIn profile search; no LinkedIn credentials required
- **Google Calendar or Outlook** -- for reading your upcoming meetings
- **Gmail** (optional) -- for email delivery of briefs

---

## Install the plugin

This directory is a self-contained Claude Code marketplace. From your terminal, register the folder
as a marketplace and then install the plugin from it (substitute the absolute path to this
directory):

```
claude plugin marketplace add <path-to-meeting-intel-directory>
claude plugin install meeting-intel@meeting-intel-local
```

Then restart Claude Code and run `/meeting-intel:meeting-intel` for first-time setup. The setup flow will prompt you for your company email domain, brief archetype, user context, and output path for briefs, then write a `config.json` that all subsequent runs read from.

---

## Set up MCP connectors (for Routines)

**Local CLI use:** Standard MCP server configuration in Claude Code is sufficient. The plugin reads your calendar and sends requests through whichever MCP servers you have configured locally.

**Scheduled / Routine use on claude.ai:** For unattended runs, the plugin needs MCP connectors registered in your claude.ai account. Without them, a Routine cannot authenticate to Google Calendar, Apify, or Gmail.

To register connectors:

1. Go to [claude.ai/customize/connectors](https://claude.ai/customize/connectors)
2. Add **Google Calendar** (or Microsoft Outlook if that is your calendar)
3. Add **Apify** -- paste your Apify API token when prompted
4. Optionally add **Gmail** if you want briefs delivered by email

Once connectors are registered, the plugin can run fully unattended as a morning Routine.

---

## Schedule as a Routine

1. Go to [claude.ai](https://claude.ai) and open **Routines**
2. Create a new Routine
3. Set the prompt to: `/meeting-intel:meeting-intel`
4. Set the schedule -- for example, weekdays at 7:00 AM
5. Save

From that point on, the plugin runs automatically each morning. It reads your calendar for the day, researches each participant, and delivers briefs -- no interaction required, as long as `config.json` exists and your MCP connectors are active.

---

## LinkedIn and Apify

Meeting Intel does not use your LinkedIn credentials. LinkedIn research runs through Apify's infrastructure, which operates independently of your personal account.

What Apify does and how it sources data is governed by Apify's Terms of Service at [apify.com/terms](https://apify.com/terms). Review those terms before use if data sourcing matters for your context (regulated industries, enterprise compliance, etc.).
