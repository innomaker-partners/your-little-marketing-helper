# Content at Scale -- User Manual (Claude Code Plugin)

## Contents

1. [What this plugin is](#what-this-plugin-is)
2. [Installation](#installation)
3. [How a run goes](#how-a-run-goes)
4. [What the run asks of you](#what-the-run-asks-of-you)
5. [The quality gates, in plain terms](#the-quality-gates-in-plain-terms)
6. [Data handling and safety](#data-handling-and-safety)
7. [Troubleshooting](#troubleshooting)
8. [Developer note](#developer-note)
9. [Updating and uninstalling](#updating-and-uninstalling)

---

## What this plugin is

Content at Scale produces many content pieces in one run and puts the quality control inside the run.
The idea is simple: the ways batch-generated content usually goes wrong (drifting off what your
company does, inventing a number, repeating itself, reading like a machine) are exactly the things
the pipeline checks for as it works, so you catch them while the run is in front of you rather than
by reading fifteen finished pieces afterwards.

It is built to work with you, not instead of you. You hold the things only you can hold: what your
company and product truly are, whose voice the pieces should carry, and the judgment calls the tool
is not allowed to make on its own. It holds the things a tool does well: producing at volume, running
the same checks on every piece without tiring, and keeping an honest record of what passed and what
did not. When something needs a person, it stops and brings it to you instead of guessing.

You interact with one skill, **orchestrate**. Everything else in the plugin is a stage it calls.

---

## Installation

### Step 1: Place the folder

Put this plugin directory somewhere permanent on your machine. Claude Code records this path as the
source of the plugin and reads it when checking for updates, so moving the folder later means
reinstalling from the new location.

### Step 2: Install the plugin

This directory is a self-contained Claude Code marketplace: it carries its own marketplace manifest,
so you register the folder as a marketplace and then install the plugin from it. Run both commands,
substituting the absolute path to this directory:

```
claude plugin marketplace add <absolute-path-to-this-directory>
claude plugin install content-at-scale@content-at-scale-local
```

Confirm it registered:

```
claude plugin list
```

You should see `content-at-scale` in the output at user scope. Restart Claude Code so the plugin
loads into your session.

### Step 3: Check Python

The pipeline scripts run on Python 3.8 or newer, using only the standard library. Confirm with
`python3 --version`. There is nothing else to install. Full prerequisites are in
[docs/SETUP.md](docs/SETUP.md).

---

## How a run goes

Ask Claude for the batch in plain language (for example, "write 15 blog posts from these
transcripts"). The `orchestrate` skill takes over and runs the pipeline in this order. The early steps
are groundwork, and nothing is written until they are done.

**1. Setup and your ground truth.** The run asks where it should live (a directory in your own
project) and then interviews you: what your company and product actually are, whose voice the pieces
should carry, and what you expect from the batch in your own words. This record becomes the thing
every piece is later checked against. It is recorded with its origin marked, so the run always knows
which facts came from you and must never be argued with.

**2. Your source material.** You hand the run its raw material: research documents, meeting
transcripts, keyword research. The run segments each source, has the segmentation adversarially
challenged so nothing is quietly miscategorized, and then asks you to confirm the result in bulk.
Each part is routed to where it belongs: to the whole run, to one group, or to a single piece.

**3. Grouping.** The run is organized into groups of related pieces. Even a run you did not group
has exactly one group internally; there is a single code path, so a small run and a large one behave
the same way.

**4. The barrier.** Before any piece is written, the run checks that setup is complete and that every
contradiction anyone flagged in your ground truth has been resolved by you. If something is
unresolved, the run stops here and tells you exactly what. This is deliberate: a contradiction is
cheapest to fix now, when nothing has been produced against it yet.

**5. Production, piece by piece, in parallel.** Each piece is written and then run through a sequence
of checks (described in [the next section](#the-quality-gates-in-plain-terms)). Pieces run
concurrently, and their results are written back to the run's record one at a time so nothing is lost
when two finish at once.

**6. Group review.** When a group is finished, the whole group is read at once and checked for
repetition within a piece, near-duplication across pieces, and format consistency. This catches
things no single-piece check can see.

**7. Whole-run review.** Finally the entire run is reviewed against itself and against your brand:
any two pieces that contradict each other, and any piece that has drifted from the voice, positioning,
or company details you set. This stage reports and suggests; it does not silently edit your pieces.

**8. Delivery.** The finished pieces are copied out to a directory you name. Before it hands anything
over, the run re-checks each file against the checks that were recorded for it, and refuses to
deliver a file that was changed after it was measured. The file you receive is provably the file the
checks passed.

---

## What the run asks of you

The run is designed to reach you rarely, and only when your input genuinely changes the outcome.
These are the moments it will:

- **The intake interview.** Once, at the start. Your answers are the ground truth for everything that
  follows, so this is worth doing carefully.
- **Confirming your source material.** Once, in bulk, after the run has segmented and challenged it.
  You are approving how the material was understood, not reviewing it line by line.
- **A contradiction in your ground truth.** If two things you or your sources asserted cannot both be
  true, the run stops at the barrier and asks you to rule. Your ruling is recorded as authoritative
  and stays distinguishable, so it is always clear which calls the pipeline had to bring to a person.
- **A claim it cannot source.** The run will not ship an invented figure. If a piece needs an
  external fact that no dated source supports, that is surfaced to you rather than written in anyway.
- **A sentence it cannot safely rewrite.** When a piece is built from your own recorded speech and a
  broken sentence cannot be repaired without risking a change in meaning, the run holds your words
  verbatim and proposes a candidate rewrite for you to approve, instead of quietly changing what you
  said.
- **Accepting a piece over a failed check.** A recorded failure cannot be made to disappear by
  re-running the check. If you decide a piece is fine anyway, that acceptance is recorded with your
  reason, and the run marks the piece as passed-with-an-override so the record stays honest.

---

## The quality gates, in plain terms

Each produced piece goes through these. They are deterministic checks with readable results, not a
second model asked for its opinion.

- **Length.** The piece falls within the word bounds set for it.
- **Overlap.** How much of the piece appears verbatim in your source material. Against material you
  own and want echoed, this is a floor to clear; against third-party material you must not copy, it
  is a ceiling not to cross. The check separates writing that is genuinely about the same subject
  from writing that was lifted.
- **Fact-check.** Each sentence that carries a claim is marked, and the claims the pipeline itself
  produced are the ones it challenges hardest. Claims you asserted or that came from your sources are
  trusted; claims the model found or merely inferred are not, until verified.
- **Spoken-to-written** (only when it applies). If a piece's content and voice both come from the
  same recorded speech, this pass repairs the spoken grammar into readable prose without paraphrasing
  it away. It never touches working prose and never changes meaning.
- **AI-tell removal.** Generic phrasing and the tells that make text read as machine-written are
  stripped, while the verified wording of claims is left intact.
- **Claim comparison.** The piece is compared before and after the language passes, so any claim that
  was subtly changed while the prose was being cleaned up is flagged for a look.

A piece is only counted done when every check that applies to it is resolved. A check that does not
apply (for example, overlap on a piece written with no source material) is dropped from the verdict
honestly, not marked as passed.

---

## Data handling and safety

**Everything is local.** A run's material, its working files, and its finished pieces all live in the
run directory you chose, inside your own project. The plugin does not upload, publish, email, or
share your content anywhere. It sends nothing to InnoMaker or to any server the plugin author
controls, and it has no telemetry. It is a set of skills and standard-library scripts.

**The web is touched only to verify a fact, and only through Claude.** When a piece needs an external
statistic or a current fact, the run uses Claude's own web search and fetch tools to find it and then
to re-check it against the source. Every such finding is stored with the source's own words, the
source's date, and whether the fact is the kind that goes stale. A fact that can go stale is held as
unconfirmed until a separate pass verifies it is still current. A run that writes only from your
material never touches the web.

**No invented figures, by construction.** The distinction between a fact you gave, a fact from your
sources, a fact found online, and a fact the model inferred is tracked through the whole run. The
inferred ones are the first thing the checks attack, and nothing found online is trusted until an
independent pass re-checks it against its source.

**The delivered file is the measured file.** At delivery the run re-checks every recorded result
against the file on disk and refuses to hand over a file that was altered after it was measured. A
piece that was never actually produced is never delivered, whatever is sitting in its folder.

---

## Troubleshooting

**The run stops before writing and lists reasons.** This is the barrier doing its job. It means setup
is not finished, or a contradiction in your ground truth is unresolved. Read the reasons it prints,
answer what it asks, and it continues. A contradiction is resolved by you ruling on it.

**A run refuses to open from its directory.** If you moved the run folder, the run needs to be told
the move was intentional before it will adopt the new path. If instead you copied a live run, the
refusal is protecting you: two copies of one run, each being written to, would split its state so
that neither finishes. Work from a single copy.

**A check is marked failed and the piece looks fine to you.** A failed check blocks the run from
being marked complete, because a record that says "done" over a rejected check would be a lie. Either
the piece is retried until the check passes, or, if you judge it acceptable, you accept it explicitly
and the run records that override with your reason. You do not have to leave a run stuck.

**A piece is missing from the delivered set.** Delivery hands over only pieces that were actually
produced and whose checks are resolved. A piece that was dropped during the run is reported as
excluded rather than shipped. Check the run's status for what it says about that piece.

**`python3` is not found, or reports an old version.** The scripts need Python 3.8 or newer on your
`python3` command. Install or update Python and confirm with `python3 --version`. Nothing else needs
installing, since the scripts use only the standard library.

---

## Developer note

Every gate is a standalone command-line script under `scripts/`, and you can run any of them yourself
on a file to see exactly what the pipeline sees. For example, the length and overlap checks each take
a content file and print a readable result, and they use only the Python standard library, so there
is nothing to install first. The one reference the skills share at runtime is `REFERENCE.md`, which
documents each script's arguments, exit codes, and the file schema the run maintains. Reading it is
the fastest way to understand what the pipeline is doing under the surface.

---

## Updating and uninstalling

**To update:** replace the contents of this directory with the new version, then refresh the
marketplace and the plugin, and restart Claude Code:

```
claude plugin marketplace update content-at-scale-local
claude plugin update content-at-scale
```

Your run directories live in your own projects, not here, so they are untouched by an update.

**To uninstall:**

```
claude plugin remove content-at-scale
```

This removes the plugin registration from Claude Code. To also forget the local marketplace, run
`claude plugin marketplace remove content-at-scale-local`. Any run directories you created, with
their material and finished pieces, remain where they are; remove them separately if you want to.
