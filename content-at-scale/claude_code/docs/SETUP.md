# Content at Scale -- Setup Guide

This plugin is a set of skills and standard-library scripts. There is no account to create, no key
to paste, and no service to connect. Setup is installing the plugin and confirming your Python
version.

- [Install the plugin](#install-the-plugin)
- [What you need](#what-you-need)
- [Where a run lives](#where-a-run-lives)
- [Using the bundled voices](#using-the-bundled-voices)
- [What this costs](#what-this-costs)

---

## Install the plugin

Put this plugin directory somewhere permanent on your machine first. Claude Code records this path as
the plugin's source and reads it when checking for updates, so moving it later means reinstalling
from the new location.

This directory is a self-contained Claude Code marketplace, so installation is two commands:
register the folder as a marketplace, then install the plugin from it. From your terminal,
substituting the absolute path to this directory:

```
claude plugin marketplace add <absolute-path-to-this-directory>
claude plugin install content-at-scale@content-at-scale-local
```

Then confirm:

```
claude plugin list
```

You should see `content-at-scale` at user scope. Restart Claude Code, and the `orchestrate` skill and
the stages it calls are available to Claude in any session.

---

## What you need

- **Claude Code**, which requires a Claude Pro, Team, or Enterprise subscription.
- **Python 3.8 or newer.** Confirm with `python3 --version`. Every script in the pipeline uses only
  the Python standard library, so there is nothing to `pip install` and no virtual environment to
  create. If `python3` runs and reports 3.8 or higher, you are ready.
- **Web search, only when a piece needs an external fact.** When a run has to cite a real outside
  statistic or a current fact, it uses Claude's own web search and fetch tools, which are part of
  Claude Code. A run that writes only from the material you provide never touches the web. There is
  nothing to set up either way.

That is the whole list. No API keys, no OAuth, no external accounts, no `.env` file.

---

## Where a run lives

Every run keeps its state in a run directory that you choose at the start. The `orchestrate` skill asks
you for it during setup. Everything the run produces lives there: the two state files it maintains,
each piece as it is written, and the finished files it delivers at the end.

Choose a directory inside your own project, not inside the plugin folder. The plugin folder is a
Claude-managed install location, and your run's work belongs with your project. Picking a stable
location also lets you resume a run later: a resumed run is found by being told its directory, so
keep the path if a large run spans more than one session.

---

## Using the bundled voices

The plugin ships two ready-to-use voice references, in `voice/`: one modeled on James Clear's
published writing and one on Ann Handley's. You can point a run at either of them out of the box when
you want a clean, plain, non-salesy voice and do not have your own sample to hand.

You are not limited to them. During setup you can instead give the run your own voice: a sample of
your writing, a recorded voice note, or a transcript of you talking. When a piece's content and its
voice both come from the same recorded speech, the run repairs the spoken grammar into readable prose
without paraphrasing away what you actually said.

---

## What this costs

The only paid dependency is your Claude subscription, because the plugin runs inside Claude Code.
There is nothing else to pay for: the scripts are free-standing Python, the web search a run may use
is part of Claude, and there are no third-party services in the pipeline.

A large run does use Claude tokens in proportion to how many pieces it writes and how many parallel
agents it runs, the same as any substantial Claude Code session. The plugin does not add a separate
cost on top of that.
