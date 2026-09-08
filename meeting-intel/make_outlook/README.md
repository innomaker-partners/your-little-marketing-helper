> ### How to install these tools
> These are free tools for semi-technical marketers. You do not need to be a developer. There are two ways to set up anything in this repository:
>
> **1. If the tool is a Claude plugin** (all five tools have a plugin form): copy this repository's web address and paste it into the Claude app under Customize → Plugins → Add marketplace, then choose the tool and install it.
>
> **2. For any tool, or if you would rather not touch settings**: paste the repository's web address into a chat with Claude and ask it to set the tool up for you. Claude reads that tool's own instructions and walks you through it. The steps differ by tool (a Claude plugin, a Make.com scenario, and an n8n workflow are each set up differently), and Claude handles the difference.
>
> Repository address: `https://github.com/innomaker-partners/your-little-marketing-helper`

# Meeting Intel - Make.com + Microsoft 365 Variant

This folder contains the **Microsoft 365 variant** of Meeting Intel: a Make.com scenario that reads your Outlook calendar, researches the external participants of today's meetings using Apify and OpenAI, and emails you a formatted HTML intelligence brief before each meeting.

If you use Google Calendar, this is the wrong folder. A separate Google Calendar variant lives alongside this one.

---

## What it does

For each meeting today that has at least one external participant (someone outside your company's email domain), the scenario:

1. Reads your Outlook 365 calendar for today's events via Microsoft's API
2. Extracts external attendee names, emails, and company guesses
3. Crawls each company's website and cross-references it against Google Maps data
4. Searches LinkedIn for each person and validates the match against their full profile
5. Synthesizes a structured HTML brief covering each person and their company
6. Emails the brief to the address you specify

The scenario runs on demand - you click Run once when you want it. No background polling, no cursor to initialize.

---

## Accounts you will need

- **Make.com account** - the free tier works for testing; a paid plan is needed for regular use because each scenario run consumes multiple operations
- **Apify account and API token** - the Starter plan covers typical usage; create a token at apify.com/account/integrations
- **OpenAI API account and API key** - usage is billed per token; a typical run costs a small amount (a few cents for a single-meeting brief)
- **Microsoft 365 account** - used for both reading your Outlook calendar and sending the brief via email; the same Microsoft account can serve both purposes

---

## Quick start

1. In Make.com, go to Scenarios and click **Create a new scenario**
2. Click the three-dot menu and select **Import Blueprint**
3. Paste the contents of `meeting_intel.json` and click **Save**
4. Set up your four connections (Microsoft Calendar, Apify, OpenAI, Microsoft Email)
5. Configure the five settings described in the user manual (email domains, recipient address, your context, brief archetype, participant cap)
6. Click **Run once**

Full step-by-step instructions are in `USER_MANUAL.md` in this folder.

---

## Files in this folder

| File | Purpose |
|---|---|
| `meeting_intel.json` | The Make.com scenario blueprint to import |
| `README.md` | This file - orientation and prerequisites |
| `USER_MANUAL.md` | Full setup, configuration, first run, and troubleshooting guide |

---

## Where to go next

Open `USER_MANUAL.md` in this folder. It covers everything from import to your first brief.
