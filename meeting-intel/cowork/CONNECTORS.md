# Meeting Intel Connectors

Meeting Intel uses your connected tools to pull calendar data and deliver
briefs. The plugin works with whatever you have connected -- it adapts
automatically.

## Required

| Connector | What it provides | How to connect |
|---|---|---|
| **Microsoft 365** or **Google Calendar** | Today's meetings, attendees, meeting titles | Settings > Connectors > find your calendar provider |
| **Apify** | LinkedIn research (profile search and full-profile scrape, run through your Apify account) | Settings > Connectors > Apify, paste your Apify API token |

Without a calendar connector, you can still run `/meeting-intel:prep`
manually by providing the meeting details yourself. Without the Apify
connector, LinkedIn research falls back to a plain web search, which
finds less and with lower confidence.

## Optional

| Connector | What it provides |
|---|---|
| **Gmail** or **Microsoft 365 (email)** | Email delivery of finished briefs |
| **Slack** or **Microsoft Teams** | Delivery of briefs to a channel or DM |

## Not required

Company research (company websites) uses Claude's built-in web access.
No additional connector is needed for that part.
