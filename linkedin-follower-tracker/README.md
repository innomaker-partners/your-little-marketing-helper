> ### How to install these tools
> These are free tools for semi-technical marketers. You do not need to be a developer. There are two ways to set up anything in this repository:
>
> **1. If the tool is a Claude plugin** (all five tools have a plugin form): copy this repository's web address and paste it into the Claude app under Customize → Plugins → Add marketplace, then choose the tool and install it.
>
> **2. For any tool, or if you would rather not touch settings**: paste the repository's web address into a chat with Claude and ask it to set the tool up for you. Claude reads that tool's own instructions and walks you through it. The steps differ by tool (a Claude plugin, a Make.com scenario, and an n8n workflow are each set up differently), and Claude handles the difference.
>
> Repository address: `https://github.com/innomaker-partners/your-little-marketing-helper`

# LinkedIn Follower Tracker

Track how your LinkedIn following changes over time. On each run the tool collects
the current followers of a profile, works out who is new and who left since last
time, enriches the new ones with company and job data, classifies each into
categories you define, keeps a running history, and sends you a summary. You run it
entirely on your own accounts, so there is no shared or per-user cost.

It comes in two self-contained versions, each suited to a different tool stack.

---

## Versions at a glance

| | **claude_code_cowork_plugin** | **n8n** |
|---|---|---|
| **Platform** | Claude Code or Cowork plugin | n8n workflow (imported) |
| **Datastore** | Local CSV files | Google Sheet |
| **Classifier** | Local `claude` CLI subagent | OpenAI API (can be turned off) |
| **Delivery** | Push notification to the Claude app | Email (Gmail) |
| **Credentials needed** | PhantomBuster API key only | PhantomBuster, LinkedIn session cookie, Google, OpenAI |
| **Cost model** | PhantomBuster credits + your normal Claude usage | PhantomBuster credits + OpenAI per run + your n8n instance |
| **Setup difficulty** | Moderate | Involved |

Both versions use the same two PhantomBuster phantoms (a follower collector and a
profile scraper) and the same core logic: diff against a stored baseline, enrich
the new followers, classify them against your own taxonomy, and report the net
change. What differs is where the data lives, what does the classifying, and how
the summary reaches you.

---

## Which version should I pick?

**You already work in Claude Code or Cowork.** Use `claude_code_cowork_plugin`. Its
only credential is a PhantomBuster API key, it stores everything in local CSV files,
and it sends a push notification to the Claude app when a run finishes. It rehearses
for free on synthetic data before you spend any PhantomBuster credits.

**You run automations in n8n, or you want the record in a Google Sheet and the
summary by email.** Use `n8n`. You import one workflow file, reconnect your own
PhantomBuster, Google, and OpenAI credentials, define your classifier categories,
and put it on a schedule.

Both need a PhantomBuster account with a LinkedIn Follower Collector and a Profile
Scraper phantom already set up (PhantomBuster is a paid service).

---

## Folder map

| Folder | Version | Full documentation |
|---|---|---|
| `claude_code_cowork_plugin/` | Claude Code + Cowork plugin | `claude_code_cowork_plugin/README.md`, `claude_code_cowork_plugin/docs/SETUP.md` |
| `n8n/` | n8n workflow | `USER_MANUAL.md` (setup and customization), `n8n/linkedin-follower-tracker.workflow.json` (import this) |

---

## License

MIT. Free to use and adapt.
