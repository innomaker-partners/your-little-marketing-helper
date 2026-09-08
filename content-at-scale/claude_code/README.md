> ### How to install these tools
> These are free tools for semi-technical marketers. You do not need to be a developer. There are two ways to set up anything in this repository:
>
> **1. If the tool is a Claude plugin** (all five tools have a plugin form): copy this repository's web address and paste it into the Claude app under Customize → Plugins → Add marketplace, then choose the tool and install it.
>
> **2. For any tool, or if you would rather not touch settings**: paste the repository's web address into a chat with Claude and ask it to set the tool up for you. Claude reads that tool's own instructions and walks you through it. The steps differ by tool (a Claude plugin, a Make.com scenario, and an n8n workflow are each set up differently), and Claude handles the difference.
>
> Repository address: `https://github.com/innomaker-partners/your-little-marketing-helper`

# Content at Scale -- Claude Code Plugin

Produce many content pieces in one run, with the quality control built into the run instead of left
for you to do afterwards. Install the plugin once and Claude can take a batch of raw material and
turn it into finished pieces that stay true to your company, carry the voice you chose, and pass a
set of checks you can read.

You start it with one skill, **orchestrate**. It owns the whole pipeline: it interviews you for your
ground truth, ingests your source material, groups the work, writes the pieces in parallel, runs the
gates, and does the final reviews. The other skills in the plugin are stages it calls; you do not
invoke them by hand.

## Who this is for

Anyone using Claude Code who needs to produce content in volume without giving up control of it: a
batch of blog posts from a pile of transcripts, a set of articles from research documents, a run of
pieces in a consistent voice. It is for the person who has been burned by fast content that drifted
off-brand, invented a number, or read like a machine, and wants the checking to happen while the run
is in front of them.

## Prerequisites

- **Claude Code**, which requires a Claude Pro, Team, or Enterprise subscription.
- **Python 3.8 or newer.** Every script in this plugin uses only the Python standard library. There
  are no third-party packages to install. Check your version with `python3 --version`.
- **Web search, only for pieces that need it.** If a piece must cite a real external statistic or a
  current fact, the run uses Claude's own web search and fetch tools to find and verify it. A run
  that writes only from the material you provide needs no web access at all.

There are no API keys, no external accounts, and no `.env` file. The plugin is a set of skills and a
set of standard-library scripts.

## Quick start

1. Install the plugin. This directory is a self-contained marketplace, so register it and then
   install from it (substitute the absolute path to this directory):
   ```
   claude plugin marketplace add <path-to-this-directory>
   claude plugin install content-at-scale@content-at-scale-local
   ```
2. Confirm it registered, then restart Claude Code:
   ```
   claude plugin list
   ```
   You should see `content-at-scale` at user scope.
3. In any Claude Code session, ask for the batch in plain language, for example:
   - "write 15 blog posts from these transcripts"
   - "turn this research into a batch of articles in James Clear's voice"
   - "produce this set of pieces and keep them on-brand"

   The `orchestrate` skill loads and walks you through setup before any writing starts: it asks where
   the run should live, interviews you for your ground truth, and confirms your source material with
   you. Nothing is written until that groundwork is done.

Full setup and prerequisites are in [docs/SETUP.md](docs/SETUP.md). A step-by-step account of how a
run goes, what it asks of you, the safety rules, and troubleshooting is in
[USER_MANUAL.md](USER_MANUAL.md).

## Folder contents

| Path | Description |
|---|---|
| `.claude-plugin/plugin.json` | Plugin manifest: name (`content-at-scale`), version, description |
| `skills/orchestrate/SKILL.md` | The entry-point skill: the whole pipeline. Start here |
| `skills/context-intake/SKILL.md` | Sets up a run and records your company, product, and voice ground truth |
| `skills/source-intake/SKILL.md` | Ingests your raw material and routes each part to the right scope |
| `skills/research/SKILL.md` | Finds external supporting facts, each traced to a dated source |
| `skills/brief-dedup/SKILL.md` | Checks the piece briefs for collisions before any writing starts |
| `skills/fact-check/SKILL.md` | Verifies each piece against your ground truth and its sources |
| `skills/spoken-to-written/SKILL.md` | Renders a piece built from your own recorded speech into readable prose |
| `skills/de-slop/SKILL.md` | Strips AI tells and generic phrasing from a finished piece |
| `skills/group-review/SKILL.md` | Reviews a finished group for repetition and format consistency |
| `skills/cross-group/SKILL.md` | Reviews the whole run for contradictions and brand drift |
| `skills/agent-management/SKILL.md` | Governs the parallel agents inside a run |
| `scripts/*.py` | Standard-library pipeline: manifest, context, the gates, voice, delivery |
| `voice/voice_james_clear.md`, `voice/voice_ann_handley.md` (+ `profile_*.json`) | Bundled voice references you can use out of the box |
| `voice/banned.json` | The banned-phrase list the AI-tell gate reads |
| `REFERENCE.md` | The shared runtime reference every skill loads: file schema, script syntax, exit codes |
