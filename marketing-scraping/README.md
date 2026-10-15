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

| Tool | What it answers | Cost per run |
|------|-----------------|--------------|
| **ad-intelligence** | What creative angles are my competitors actually running, across Meta, Google, and LinkedIn ads? | At most about $9.77 for 2 competitors at the example settings (a worst case, shown before anything is spent); a run usually costs less than its forecast |
| **pain-and-messaging** | What do customers in my market complain about, in their own words, and where is the gap I can win on? Reads reviews across Google Maps, Trustpilot, the App Store, Google Play, Reddit, G2, and Capterra. | Forecast at about $0.43 to $0.49 for the example (3 businesses on Google Maps, 100 reviews each), the higher figure on Apify's free plan; more sources or reviews cost more, and the forecast is shown before anything is spent |
| **search-intelligence** | What does my market search for, how hard is each keyword to rank for, and who already owns the results? | About $3 to $7 for about 300 keywords, the higher figure on Apify's free plan (exceeds its free credit, so this one needs a card) |

The three tools ship together as one installable plugin,
`claude_code_cowork_plugin/`, which installs in both Claude Code and Cowork. Each
tool is a skill inside it with its own `QUICKSTART.md` giving the exact steps.

## How they work

Every tool follows the same shape:

1. You fill in a small config file (your market, your competitors, your country).
2. The tool scrapes public data through your own Apify account, one metered stage at a time.
3. It shows you a cost forecast before it spends anything, and stops at a budget cap you set.
4. It counts every number in code from the scraped data. For pain-and-messaging and search-intelligence, a local `claude` call (your Claude Code CLI) then clusters and writes the report. ad-intelligence instead prepares a report bundle that Claude in your session, or any model you choose, fills in; its only `claude` call reads the text off Google ad images. The write-up never invents a figure; every number traces back to something that was actually scraped.
5. It opens the results in a small app on your own computer, where you can check every record behind the report and ask Claude about any of them. See `claude_code_cowork_plugin/README.md`.

Research worth doing again can repeat every month: you approve a spending ceiling once, and a month whose forecast is above it spends nothing. See `claude_code_cowork_plugin/docs/REPEATS.md`.

## What you need

- An **Apify account** and API token (free to create). search-intelligence meters per keyword and needs a card on your Apify account. The pain-and-messaging example run is forecast well inside the free monthly credit; an ad-intelligence run can be forecast above it, though a run usually costs less than its forecast. Every tool shows its forecast before spending.
- **Claude Code CLI**, installed and signed in. The pain-and-messaging and search-intelligence report steps run locally through it, with no separate API key and no extra per-report cost beyond your normal Claude usage. ad-intelligence uses it only to read the text off Google ad images.
- **Python 3.9 or newer** (a Mac already has it; standard library only, nothing to pip install).

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
