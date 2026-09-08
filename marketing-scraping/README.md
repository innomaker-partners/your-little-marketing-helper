> ### How to install these tools
> These are free tools for semi-technical marketers. You do not need to be a developer. There are two ways to set up anything in this repository:
>
> **1. If the tool is a Claude plugin** (all five tools have a plugin form): copy this repository's web address and paste it into the Claude app under Customize → Plugins → Add marketplace, then choose the tool and install it.
>
> **2. For any tool, or if you would rather not touch settings**: paste the repository's web address into a chat with Claude and ask it to set the tool up for you. Claude reads that tool's own instructions and walks you through it. The steps differ by tool (a Claude plugin, a Make.com scenario, and an n8n workflow are each set up differently), and Claude handles the difference.
>
> Repository address: `https://github.com/innomaker-partners/your-little-marketing-helper`

# Marketing-scraping tools

Three free, open-source market-research tools you run on your own accounts. Each one turns public data that already exists (competitor ads, customer reviews, search results) into a report you can act on. You bring your own Apify account and your own Claude Code CLI, so you control what runs and what it costs, and nobody else sees your data.

These are the tools our own team built to do competitor and market research without paying for a stack of subscriptions. We are sharing them as they are, with the honest edges left in.

## The three tools

| Tool | What it answers | Typical cost per run |
|------|-----------------|----------------------|
| **ad-intelligence** | What creative angles are my competitors actually running, across Meta, Google, and LinkedIn ads? | A few cents to about $0.50 on your Apify account, often inside Apify's free monthly credit |
| **pain-and-messaging** | What do customers in my market complain about, in their own words, and where is the gap I can win on? Reads reviews across Google Maps, Trustpilot, the App Store, Google Play, Reddit, G2, and Capterra. | A few cents to about $0.50, often inside Apify's free credit |
| **search-intelligence** | What does my market search for, how hard is each keyword to rank for, and who already owns the results? | About $2 to $5, which exceeds Apify's free credit, so this one needs a card |

The three tools ship together as one installable plugin,
`claude_code_cowork_plugin/`, which installs in both Claude Code and Cowork. Each
tool is a skill inside it with its own `QUICKSTART.md` giving the exact steps.

## How they work

Every tool follows the same shape:

1. You fill in a small config file (your market, your competitors, your country).
2. The tool scrapes public data through your own Apify account, one metered stage at a time.
3. It shows you a cost forecast before it spends anything, and stops at a budget cap you set.
4. It counts every number in code from the scraped data, then a local `claude` call (your Claude Code CLI) clusters and writes the report. The write-up never invents a figure; every number traces back to something that was actually scraped.

## What you need

- An **Apify account** and API token (free to create). One tool, search-intelligence, meters per keyword and will cost more than the free monthly credit, so it needs a card on your Apify account. The other two usually stay inside the free credit.
- **Claude Code CLI**, installed and signed in. The report step runs locally through it, so there is no separate API key to manage and no extra per-report cost beyond your normal Claude usage.
- **Python 3** (standard library only, nothing to pip install).

## What these tools do not do

- They do not build a backlink index or score domain authority. They read live, public search and market data, not a proprietary crawl.
- They work from samples, and they say so. When a run covers a slice of a market rather than all of it, the report calls its findings directional, not statistical.
- They cost real money on your own account when you run them at volume. Every tool shows the forecast first, but none of them pretend to be free.

## Getting started

Install the plugin first, then open the tool you want, copy its example config, and
run the preview (`--dry-run`) first to see the cost before anything is spent.

1. Install the plugin (Claude Code or Cowork): `claude_code_cowork_plugin/docs/SETUP.md`
2. Read the tool's quickstart:
   - `claude_code_cowork_plugin/skills/ad-intelligence/QUICKSTART.md`
   - `claude_code_cowork_plugin/skills/pain-and-messaging/QUICKSTART.md`
   - `claude_code_cowork_plugin/skills/search-intelligence/QUICKSTART.md`
