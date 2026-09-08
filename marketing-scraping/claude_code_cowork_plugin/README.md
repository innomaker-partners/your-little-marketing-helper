> ### How to install these tools
> These are free tools for semi-technical marketers. You do not need to be a developer. There are two ways to set up anything in this repository:
>
> **1. If the tool is a Claude plugin** (all five tools have a plugin form): copy this repository's web address and paste it into the Claude app under Customize → Plugins → Add marketplace, then choose the tool and install it.
>
> **2. For any tool, or if you would rather not touch settings**: paste the repository's web address into a chat with Claude and ask it to set the tool up for you. Claude reads that tool's own instructions and walks you through it. The steps differ by tool (a Claude plugin, a Make.com scenario, and an n8n workflow are each set up differently), and Claude handles the difference.
>
> Repository address: `https://github.com/innomaker-partners/your-little-marketing-helper`

# marketing-scraping (Claude Code plugin)

Three free, open-source market-research tools, packaged as a Claude Code plugin.
You run them on your own accounts: bring your own Apify account and your own Claude
Code CLI, so you control what runs and what it costs, and nobody else sees your
data. Each tool turns public data that already exists (competitor ads, customer
reviews, search results) into a report you can act on.

These are the tools our own team built to do competitor and market research
without paying for a stack of subscriptions. We are sharing them as they are, with
the honest edges left in.

## Install

```bash
claude plugin marketplace add <path-or-url-to-this-folder>
claude plugin install marketing-scraping@marketing-scraping-local
```

See **[docs/SETUP.md](docs/SETUP.md)** for the Apify token, the run-directory
convention, and the money gate. Read it once before your first run.

## The three tools

Each is both an **auto-activating skill** (describe the task and it triggers) and
an explicit **slash command**.

| Tool | Command | What it answers | Typical cost per run |
|------|---------|-----------------|----------------------|
| **ad-intelligence** | `/marketing-scraping:ad-intelligence` | What creative angles are my competitors running, across Meta, Google, and LinkedIn ads? | A few cents to ~$0.50 on your Apify account, often inside the free credit |
| **pain-and-messaging** | `/marketing-scraping:pain-and-messaging` | What do customers in my market complain about, in their own words, and where is the gap I can win on? Reads reviews across Google Maps, Trustpilot, the App Store, Google Play, Reddit, G2, and Capterra. | A few cents to ~$0.50, often inside the free credit |
| **search-intelligence** | `/marketing-scraping:search-intelligence` | What does my market search for, how hard is each keyword to rank for, and who already owns the results? | ~$2 to $5 (exceeds Apify's free credit, so this one needs a card) |

## What to say to trigger each tool

Describe the task in plain words and the matching skill activates. If it does not,
type the slash command instead (that always works). Phrasings that route well:

- **Reviews / customer pain** (`/marketing-scraping:pain-and-messaging`): "App Store reviews for
  Notion Calendar", "what do customers complain about Pipedrive", "what are people
  saying about [product] on Reddit", "pull the Trustpilot reviews for [company]",
  "G2 reviews for [tool]".
- **Competitor ads** (`/marketing-scraping:ad-intelligence`): "what ads is Acme Plumbing running",
  "show me HubSpot's LinkedIn ads", "what is [competitor] advertising on Meta",
  "analyze competitor ad angles".
- **Keyword / SEO research** (`/marketing-scraping:search-intelligence`): "keyword research for
  project management software", "what does my market search for", "who owns the
  SERP for [term]", "find winnable keywords".

A request like "I want the reviews for X" is a job for this plugin, not a plain web
search. If in doubt, use the slash command.

## How they work

Every tool follows the same shape:

1. The skill asks where this run's directory should live and names it per the
   convention, then copies a small config file into it for you to fill in (your
   market, competitors, country).
2. It scrapes public data through your own Apify account, one metered stage at a
   time.
3. It shows you a cost forecast before it spends anything, waits for your go-ahead,
   and stops at a budget cap you set.
4. It counts every number in code from the scraped data, then a local `claude`
   call (your Claude Code CLI) clusters and writes the report. The write-up never
   invents a figure; every number traces back to something that was actually
   scraped.

## What you need

- An **Apify account** and API token (free to create). search-intelligence meters
  per keyword and needs a card; the other two usually stay inside the free credit.
- **Claude Code CLI**, installed and signed in. The report step runs locally
  through it, with no separate API key and no extra per-report cost.
- **Python 3** (standard library only, nothing to `pip install`).

## What these tools do not do

- They do not build a backlink index or score domain authority. They read live,
  public search and market data, not a proprietary crawl.
- They work from samples, and they say so. When a run covers a slice of a market
  rather than all of it, the report calls its findings directional, not
  statistical.
- They cost real money on your own account when you run them at volume. Every tool
  shows the forecast first, but none of them pretend to be free.

## License

MIT. See the plugin manifest.
