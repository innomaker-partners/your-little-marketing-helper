> ### How to install these tools
> These are free tools for semi-technical marketers. You do not need to be a developer. There are two ways to set up anything in this repository:
>
> **1. If the tool is a Claude plugin** (all five tools have a plugin form): copy this repository's web address and paste it into the Claude app under Customize → Plugins → Add marketplace, then choose the tool and install it.
>
> **2. For any tool, or if you would rather not touch settings**: paste the repository's web address into a chat with Claude and ask it to set the tool up for you. Claude reads that tool's own instructions and walks you through it. The steps differ by tool (a Claude plugin, a Make.com scenario, and an n8n workflow are each set up differently), and Claude handles the difference.
>
> Repository address: `https://github.com/innomaker-partners/your-little-marketing-helper`

# Meeting Intel

Meeting Intel reads your calendar for the day, identifies external participants (anyone outside your organization's email domains), researches each person and their company using public web sources and LinkedIn data, and produces a structured intelligence brief covering backgrounds, company context, confidence-tagged facts, and conversation starters. The brief is delivered as a local Markdown file, an HTML email, or both, depending on which version you install. Four self-contained versions are available, each suited to a different tool stack.

> **⚠️ Before you run the `claude_code` or `cowork` version, enable your connectors first.** Both rely on connectors you turn on in the Claude app's Connector menu (Settings → Connectors), with access granted, *before* the first run:
> - **Apify** powers the LinkedIn research. Without it, the LinkedIn step does not run and the tool falls back to a plain web search, which is noticeably less reliable.
> - **Microsoft 365 or Google** gives the tool your calendar (to read the day's meetings) and, optionally, email delivery of the briefs.
>
> If a connector is missing or lacks access, the tool quietly degrades to the weaker web-search path instead of failing loudly. (The `make_google` and `make_outlook` versions use their own Make.com connections instead, set up inside Make.)

---

## Versions at a glance

| | **claude_code** | **cowork** | **make_google** | **make_outlook** |
|---|---|---|---|---|
| **Platform** | Claude Code plugin | Cowork plugin | Make.com scenario | Make.com scenario |
| **Calendar source** | Google Calendar or Outlook (via MCP) | Google Calendar or Microsoft 365 (Cowork connectors) | Google Calendar | Outlook 365 |
| **Delivery** | Markdown file; Gmail optional (MCP) | Markdown file; Gmail, Microsoft 365 email, Slack, or Teams optional (Cowork connectors) | HTML email via Gmail | HTML email via Microsoft 365 |
| **Research method** | Apify actors (LinkedIn) + built-in web search | Apify actors (LinkedIn) + built-in web search | Apify actors + OpenAI | Apify actors + OpenAI |
| **External accounts needed** | Apify account; Gmail optional | Apify account (via Cowork connector) | Make.com, Apify, OpenAI, Google | Make.com, Apify, OpenAI, Microsoft 365 |
| **Cost model** | Claude Pro subscription + Apify | Cowork subscription + Apify | Make.com operations + Apify + OpenAI per run | Make.com operations + Apify + OpenAI per run |
| **Setup difficulty** | Moderate | Low | Involved | Involved |

---

## Which version should I pick?

**You already use Claude Code every day.** Start with `claude_code`. It installs as a plugin, you invoke it with `/meeting-intel:meeting-intel` inside any session, and it uses the same MCP connectors you likely already have for calendar access. You will need an Apify account for LinkedIn research.

**You want the brief to arrive without you starting a Claude session.** Go with one of the Make.com versions. They run on demand inside Make with a single click and can be put on a schedule. No Claude session is required once they are set up.

**Your whole stack is Google: Google Calendar, Gmail.** Use `make_google`. It is designed for that combination and uses Google's Calendar API directly.

**You are on Microsoft 365: Outlook calendar, Outlook or Exchange email.** Use `make_outlook`. It reads your Outlook 365 calendar and sends briefs through your Microsoft account.

**You use Cowork and want the simplest possible setup.** Use `cowork`. Everything runs through Cowork's connector system: calendar, delivery, and the Apify connector for LinkedIn research (one token paste in Settings). All four versions share the same proven two-actor LinkedIn pipeline; no MCP configuration is needed here.

---

## Folder map

| Folder | Version | Full documentation |
|---|---|---|
| `claude_code/` | Claude Code plugin | `claude_code/README.md`, `claude_code/USER_MANUAL.md` |
| `cowork/` | Cowork plugin | `cowork/README.md`, `cowork/USER_MANUAL.md` |
| `make_google/` | Make.com + Google Calendar | `make_google/README.md`, `make_google/USER_MANUAL.md` |
| `make_outlook/` | Make.com + Outlook 365 | `make_outlook/README.md`, `make_outlook/USER_MANUAL.md` |

Each folder is fully self-contained. A user who downloads only one subfolder has everything needed to install and run that version: the blueprint or plugin files, setup instructions, and a troubleshooting guide. There is no shared dependency between folders.

For the configuration concepts and brief format that all versions share, see [USER_MANUAL.md](USER_MANUAL.md) in this folder.
