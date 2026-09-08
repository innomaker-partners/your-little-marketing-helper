# Your Little Marketing Helper — notes for Claude

This repository is a collection of free, standalone marketing tools. Each top-level
folder is one tool and is self-contained. There is no shared build step and no
central configuration: the tools do not depend on each other.

If someone has handed you this repository and asked you to set a tool up for them,
assume they are a marketer who is not a developer. Do the setup for them and explain
each step in plain language. Do not assume they know git, the command line, or what a
plugin is.

## How to set up a tool

1. Ask which tool they want, or infer it from what they asked for. The tools and what
   they do are listed in the root `README.md`.
2. Open that tool's folder and read its own `README.md` first, then its `SETUP.md` or
   `USER_MANUAL.md` if it has one. Those files are the source of truth for that tool.
   Follow them.
3. Each tool ships in one or more platform folders. Pick the one that matches where the
   person works:
   - `claude_code/` — the Claude Code command-line tool
   - `cowork/` — the Cowork desktop and web app
   - `claude_code_cowork_plugin/` — one version tested on both Claude Code and Cowork
   - `n8n/` — a workflow to import into their own n8n instance
   - `make_google/` / `make_outlook/` — Make.com blueprints to import into their own
     Make account (one for Google Calendar, one for Microsoft 365)
   The folder name tells you where that version has actually been tested. If unsure,
   prefer the plugin version.

## Two install paths (from the README)

- **Plugin tools** can be installed by pasting this repository's web address into the
  Claude app under Customize → Plugins → Add marketplace, then choosing the tool.
- **Anything** can be set up by you, reading that tool's own instructions and walking
  the person through it. Make.com and n8n versions are imported inside those platforms,
  not through Claude.

## Ground rules

- These tools run on the person's OWN accounts and keys (Apify, PhantomBuster, OpenAI,
  n8n, Make.com — depending on the tool). Never put a credential in a tracked file.
  Where a tool needs secrets it ships a `.env.example`; copy it to `.env` (which is
  gitignored) and fill in the person's values. Never echo a secret back.
- This repository contains no accounts, keys, or secrets, and it should stay that way.
- If a tool has a rough edge, its own README says so. Pass that on honestly rather than
  smoothing over it.
