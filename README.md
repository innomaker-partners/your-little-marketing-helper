> ### How to install these tools
> These are free tools for semi-technical marketers. You do not need to be a developer. There are two ways to set up anything in this repository:
>
> **1. If the tool is a Claude plugin**: copy this repository's web address and paste it into the Claude app under Customize → Plugins → Add marketplace, then choose the tool and install it.
>
> **2. For any tool, or if you would rather not touch settings**: paste the repository's web address into a chat with Claude and ask it to set the tool up for you. Claude reads that tool's own instructions and walks you through it. The steps differ by tool (a Claude plugin, a Make.com scenario, and an n8n workflow are each set up differently), and Claude handles the difference.
>
> Repository address: `https://github.com/innomaker-partners/your-little-marketing-helper`

# Your Little Marketing Helper

**Do more with less.** Free marketing tools from the team at InnoMaker Partners.

These are the tools we use to do our own marketing work. We built them because we needed them, we run them on real client work, and we are sharing the ones that held up. You run them on your own accounts, so what they cost and what they see stays with you.

They work with you, not for you. You bring the judgment and the ground truth about your company; they do the production and the mechanical checking, and they stop and ask you when something actually needs a person.

We are sharing them as they are, with the honest edges left in. None of them replace you or your team. Where a tool has a rough spot we have not smoothed yet, its own README says so.

## The tools

| Tool | What it does for you | Versions available |
|------|----------------------|--------------------|
| [content-at-scale](content-at-scale/) | Produce a batch of content pieces in one run, with the quality control built into the run instead of left for you to catch afterwards. | Claude plugin |
| [workflow-workarounds](workflow-workarounds/) | Two skills for the platforms that give Claude no clean way in: either no usable API, or an API behind an awkward login wall. | Claude plugin |
| [marketing-scraping](marketing-scraping/) | Turn public data that already exists, competitor ads, customer reviews, search results, into a report you can act on. Runs on your own Apify account. | Claude plugin (Claude Code and Cowork) |
| [linkedin-follower-tracker](linkedin-follower-tracker/) | Track how your LinkedIn following changes over time: who joined, who left, each new follower enriched and classified, with a running history and a summary sent to you. | Claude plugin (Claude Code and Cowork), n8n workflow |
| [meeting-intel](meeting-intel/) | Read your day's calendar, research the external people you are about to meet and their companies, and hand you a brief before each call. | Claude plugin (Claude Code), Claude plugin (Cowork), Make.com (Google), Make.com (Microsoft 365) |

## Which version do I pick?

Each tool holds one or more version folders. The folder name tells you which platform that version is built and tested for:

- **`claude_code/`** is built and tested for the Claude Code command-line tool.
- **`cowork/`** is built and tested for the Cowork desktop and web app.
- **`claude_code_cowork_plugin/`** is one version we run and test on both Claude Code and Cowork. A tool that instead ships separate `claude_code/` and `cowork/` folders does so because each of those was built and checked for its one platform on its own, not for both at once. The folder name is telling you where a version has actually been run, so pick the one that matches where you work.
- **`n8n/`** is a workflow you import into your own n8n instance.
- **`make_google/`** and **`make_outlook/`** are Make.com blueprints you import into your own Make account, one built around Google Calendar and one around Microsoft 365.

If you are not sure, use the plugin version and install path 1 at the top of this page. If you already live in n8n or Make.com, use those folders instead.

## Before you run: connectors

Some tools need a connector turned on in the Claude app before they will work (Settings → Connectors), with access granted:

- **Apify** powers the scraping and LinkedIn research in marketing-scraping, linkedin-follower-tracker, and meeting-intel.
- **Microsoft 365 or Google** gives the calendar-based tools your calendar and, optionally, email delivery.

If a required connector is off or lacks access, the tool quietly falls back to a weaker path (usually a plain web search, which is less reliable) or cannot run at all. Each tool's own README says exactly which connectors it needs. The Make.com and n8n versions use their own connections, set up inside those platforms, instead of Claude connectors.

## What you bring

You run these on your own accounts, so you stay in control of what runs, what it costs, and who sees your data. Depending on the tool that means your own Apify account, your own PhantomBuster or OpenAI keys, or your own n8n or Make.com instance. Each tool's README lists what it needs. This repository contains no accounts, keys, or secrets of any kind.

## When to talk to us

The tools here are free, and they stay free. What we charge for is the work we do alongside you: fixed-scope consulting for marketing teams, one price agreed up front, no open-ended retainer.

If you want to watch one of these tools used on a real problem before you try it yourself, we run live sessions that walk through them end to end. And if a tool gets you part of the way but you want a person in it with you, that is exactly what we do for a living.

You can find both, and the rest of what we do, at **[innomakerpartners.com](https://innomakerpartners.com)**.

## License

MIT. See [LICENSE](LICENSE). Use them, change them, build on them.
