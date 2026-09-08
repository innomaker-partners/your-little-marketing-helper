> ### How to install these tools
> These are free tools for semi-technical marketers. You do not need to be a developer. There are two ways to set up anything in this repository:
>
> **1. If the tool is a Claude plugin** (all five tools have a plugin form): copy this repository's web address and paste it into the Claude app under Customize → Plugins → Add marketplace, then choose the tool and install it.
>
> **2. For any tool, or if you would rather not touch settings**: paste the repository's web address into a chat with Claude and ask it to set the tool up for you. Claude reads that tool's own instructions and walks you through it. The steps differ by tool (a Claude plugin, a Make.com scenario, and an n8n workflow are each set up differently), and Claude handles the difference.
>
> Repository address: `https://github.com/innomaker-partners/your-little-marketing-helper`

# linkedin-follower-tracker

A Claude Code and Cowork plugin that tracks LinkedIn follower growth on your own
PhantomBuster account. Each run collects the current followers of a profile,
diffs them against your stored baseline, enriches the new followers with company
and job data, classifies them into your own ICP segments with a local Claude
subagent, and sends you a one-line push notification with the net change.

## What it does

- Runs a PhantomBuster collector to pull the current followers of a LinkedIn profile
- Diffs against your stored baseline to find who is new and who is lost this run
- Enriches each new follower with company and job-title data via a second PhantomBuster phantom
- Classifies each new follower against your own ICP taxonomy, using the local `claude` CLI
- Sends a push notification with net change, new, lost, and per-segment counts
- Keeps a running master history and writes a dated export of each run's new followers

## What you need

- A PhantomBuster account with two phantoms configured: a LinkedIn Follower
  Collector and a LinkedIn Profile Scraper
- A PhantomBuster API key (`PHANTOMBUSTER_API_KEY`), the only credential
- The `claude` CLI on PATH, for the classification step

Your LinkedIn session cookie is not stored here; the tool reads it live from each
phantom's own configuration at launch time, exactly as PhantomBuster's interface does.

## Cost and safety

Every real run spends PhantomBuster credits. The tool refuses a live run unless
`PB_LIVE=1` is set, so you cannot spend credits by accident. Always rehearse first
with the free `--backend fake` mode, which runs the whole pipeline on synthetic data
and calls no external API. A failure on the paid path stops immediately and never
retries, so you are never double-charged.

## Install

See [docs/SETUP.md](docs/SETUP.md) for PhantomBuster setup, the API key, and the
two-phantom configuration. Once installed, invoke it with
`/linkedin-follower-tracker:linkedin-follower-tracker` (or just describe the task and
the skill activates) and follow its steps; it walks you through setting up a tracked
subject, seeding the baseline, and running a weekly track.

## Structure

```
.claude-plugin/       plugin + marketplace manifests
skills/               the linkedin-follower-tracker skill: SKILL.md, QUICKSTART.md, scripts, config
docs/                 setup guide
```
