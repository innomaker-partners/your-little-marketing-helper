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

| Tool | Command | What it answers | Cost per run |
|------|---------|-----------------|----------------------|
| **ad-intelligence** | `/marketing-scraping:ad-intelligence` | What creative angles are my competitors running, across Meta, Google, and LinkedIn ads? | At most about $9.77 for 2 competitors at the example settings (a worst case, shown before anything is spent); a real run usually costs less |
| **pain-and-messaging** | `/marketing-scraping:pain-and-messaging` | What do customers in my market complain about, in their own words, and where is the gap I can win on? Reads reviews across Google Maps, Trustpilot, the App Store, Google Play, Reddit, G2, and Capterra. | Forecast at about $0.43 to $0.49 for the example (3 businesses on Google Maps, 100 reviews each), the higher figure on Apify's free plan; more sources or reviews cost more, and the forecast is shown before anything is spent |
| **search-intelligence** | `/marketing-scraping:search-intelligence` | What does my market search for, how hard is each keyword to rank for, and who already owns the results? | About $3 to $7 for ~300 keywords, the higher figure on Apify's free plan (exceeds its free credit, so this one needs a card) |

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
2. It scrapes public data through your own Apify account, one metered step at a
   time.
3. It shows you a cost forecast before it spends anything, waits for your go-ahead,
   and stops at a budget cap you set.
4. It counts every number in code from the scraped data. For pain-and-messaging
   and search-intelligence, a local `claude` call (your Claude Code CLI) then
   clusters and writes the report. ad-intelligence instead prepares a report
   bundle (`PROMPT.md`, `DATA_DIGEST.md` and a blank report template) that Claude
   in your session, or any model you choose, fills in; its only `claude` call
   reads the text off Google ad images. The write-up never invents a figure; every
   number traces back to something that was actually scraped.
5. It opens the results in the local app (below), where you can check every record
   behind the report and ask Claude about any of them.

## The local app: see every record, and work through it with Claude

When a run finishes, Claude opens its results in a small app that runs on your own
computer. You see every ad (with the downloaded images and videos), every review and
Reddit post, and the search tables, next to the written reports, with filters, a search
box, favorites, notes and a trash you can always restore from.

While it is open you can ask Claude about what you are looking at ("how many of these
mention price?"). Claude reads the same records the app shows and can save a named view
that appears in the app, so you see exactly the records it means. The app never scrapes,
never spends money and never changes the scraped data; it only adds your marks and saved
views next to it, in a `.viewer` folder inside each run.

It opens after every run by default. Say you do not want it for a run and Claude skips
it; say "open the app" any time to bring it back. It works where your files stay on your
computer (Claude Code in a terminal or in the Claude desktop app); a cloud session has
nothing lasting to show, and Claude will tell you so.

## Repeat it every month

Say "set up a monthly repeat" after a run. You approve a spending ceiling once, and
each month's run then starts by itself and spends nothing if its forecast is above
that ceiling. You choose whether it runs on your computer or in Claude's cloud (the
cloud needs a private GitHub repository for your research). Ask "what changed since
last month?" and the app compares the two months record by record. See
**[docs/REPEATS.md](docs/REPEATS.md)** before you set it up.

## What you need

- An **Apify account** and API token (free to create). search-intelligence meters
  per keyword and needs a card. The pain-and-messaging example run is forecast well
  inside the free credit; an ad-intelligence run can be forecast above it, though a
  real run usually costs less. Every tool shows its forecast before spending.
- **Claude Code CLI**, installed and signed in. The pain-and-messaging and
  search-intelligence report steps run locally through it, with no separate API
  key and no extra per-report cost. ad-intelligence uses it only to read the text
  off Google ad images.
- **Python 3.9 or newer** (a Mac already has it; standard library only, nothing to `pip install`).

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
