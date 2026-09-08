---
name: research
description: Use to go find external material a piece needs before it is written - a real supporting statistic, a citable study, a current product fact, competitor or keyword coverage. Runs at whatever scope the orchestrator sets (run, group, or piece) on whatever the brief asks for; light on why, strict on how. Every finding must trace to a dated source and carry the source's own words - nothing it finds is trusted until an independent pass re-checks it against that source. Called by content-at-scale:orchestrate.
---

# research

You are the run's **"go and find" motion.** The brief asks for material that is
not in the owned sources — a real opening statistic, an explicitly-cited study, a
current product or version fact, competitor coverage. Your job is to find it, **prove
where it came from and when**, and hand it forward so a writer can stand behind it.

**Read `${CLAUDE_PLUGIN_ROOT}/REFERENCE.md` first** for the scope strings, the
provenance labels and the script syntax. `context-intake` and `source-intake` run
before this. The brief that tells you *what* to find, at which scope, is in the run
context, not in this file — this skill is only *how*.

## The failure this stage is built around, stated plainly

A language model finding things on the open web will produce a clean-sounding fact
that is **subtly wrong, out of date, or attributed to a page that does not actually
say it** — and it will, if allowed, quietly fill a gap from its own training memory.
A real run did exactly this: it stated "ChatGPT's free tier gives GPT-5", cited a
marketing roundup rather than the maker, and the claim was stale. Nobody caught it,
because "verifying" only asked *does the cited page say this* — never *is this the
original source* or *is this current*.

So this stage is built on one rule, and the tooling enforces the spine of it:

> **No claim about the state of the world may rest on a model's memory. Every finding
> traces to a dated source and carries that source's own words.**

`context.py append --provenance found` **refuses** a finding that does not carry, at
minimum: a source class, a source date, a verbatim quote, and a volatility tag. You
cannot stage the undated, sourceless finding even if you want to. What the tool
cannot judge — whether the source is the *primary* one, whether it is *current
enough*, whether the quote *really supports* the claim — is your discipline and the
auditor's, below.

## The safeguard: nothing is trusted until an independent pass confirms it

Every finding enters **staged** — invisible to the writer (a normal `context.py read`
returns only *promoted* entries). An **independent** pass then re-fetches the source,
confirms the quote is really on it, judges recency, cross-checks volatile facts, and
either **promotes** or **drops**. A hallucinated or stale figure is caught *before* it
can be written, as routine — nothing is deleted (the store is append-only) and nothing
is escalated to a person except a genuine, unresolvable **contradiction**, which
`promote` refuses to paper over.

## Three roles, and the searcher and auditor must be different agents

- **The planner** is the orchestrator: it turns the brief into precise, atomic
  research *questions* — one specific fact each — and tags each as durable or
  volatile with a source-class target. That is content and lives in the brief, not
  here. You will receive questions, not a topic.
- **The searcher** finds each fact, traces it to its source, and **stages** it with
  full provenance. It never promotes its own work.
- **The auditor** is independent: it re-fetches every source, confirms, judges
  recency, cross-checks, promotes or drops, and — because no stage may only subtract
  (item 8) — goes and finds what the searcher missed.

What follows is written for the searcher and the auditor. Do the part that is yours.

---

# The searcher

## 1. Read your questions, at your scope

You are dispatched for **one scope** — the whole run, a group, or a single piece —
and given a set of **atomic questions**, each a specific fact to establish. Read the
operating context for your scope first; it carries the intake rules that bind you:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/context.py --run-dir <run> read --scope <your-scope>
```

You serve the questions; you do not decide why the fact is wanted. If a question's
answer is genuinely not on the open web from a source you can stand behind, the honest
outcome is a **HOLD** with the gap recorded — not a filled-in guess.

## 2. Mark the stage running

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/manifest.py --run-dir <run> stage \
  --target <group> --stage research --status running
```

## 3. Budget — depth first

Your group has a reserved bucket of searches, shared across the session. Record every
search:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/manifest.py --run-dir <run> spend --bucket <group> -n <count>
```

Getting a fact **right** — tracing it to the primary, dating it, corroborating it —
takes more searches than grabbing the first roundup, and that is the point: spend
them. **Search narrow, fetch wide** — WebFetch is uncapped and caches, so spend a
search to find the right page, then read it. If a spend would exceed the reservation,
the script refuses: **stop and report**, do not borrow another bucket.

## 4. The read discipline — how you keep your own memory out of the finding

`WebSearch` and `WebFetch` are deferred tools — load them with `ToolSearch` before
calling. When you fetch a page to pull a fact out of it, **do not ask the page-reader
to summarise or answer freely** — that is where a model's stale knowledge leaks in.
Ask it to extract, verbatim, or say NONE:

> "From this page, return ONLY exact verbatim sentences that state <the specific
> fact>. Copy them word for word. If the page does not state it, return exactly NONE.
> Do not summarise, infer, or add anything from your own knowledge. Also return the
> page's visible publication or last-updated date if shown."

If it returns NONE, you have no finding from that page — keep searching, or HOLD.
**Never write a quote from memory or by paraphrase.** If a dev-time raw-text reader
(`get_page_text`) is available to you, prefer it for the raw page and extract the
quote yourself under the same rule; if it is not, WebFetch as above is the path.
Everything you depend on must be a public source reachable by anyone — no private
account, internal tool, or paid service.

## 5. Trace to the primary, then stage

For each question:

1. Search broad, find the candidates, and **trace to the original publisher** — the
   maker's own page for a product or version fact, the surveying organisation's own
   report for a statistic. A roundup or an outlet is a *pointer* to the primary, never
   the citation.
2. Pull the exact sentence that states the fact (step 4), with the page's date.
3. Decide **volatility**: a version, a price, a product status, anything "current"
   that changes over time is **volatile**; a dated historical figure is **durable**.

Stage a finding whose primary you reached:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/context.py --run-dir <run> append \
  --provenance found --scope <piece> --found-by research \
  --citation <PRIMARY_URL> --source-class primary \
  --source-date <date on the page> \
  --quote "<the verbatim sentence>" \
  --volatility <durable|volatile>
```

**When the primary is genuinely unreachable** (403, paywall, login), you may fall
back — but only to **two independent, dated secondaries that agree**, and you must
record what happened reaching the primary. A lone secondary is a **HOLD**, not a
finding — the tool will refuse it:

```
  --source-class secondary-corroborated \
  --corroborating "<url>|<date>" --corroborating "<other-url>|<date>" \
  --primary-attempted "<the primary URL and what it returned>"
```

(with `--citation` set to one of the secondaries). If you cannot get two dated
secondaries that agree, do not stage — record the HOLD.

**Prefer the primary figure, and validate it against third parties.** Always
prefer the primary source's own number. But a primary figure is only as trustworthy
as its independent corroboration: when the primary has an interest in the number — a
vendor surveying its own market, a tool citing its own adoption — a load-bearing or
volatile figure needs **quasi-validation by enough independent third parties** before
you stage it as solid. A primary number that no independent source echoes, or that
other credible sources contradict, is a candidate to check, not a fact to stage.
Prefer primary; where a primary and the third parties conflict, prefer the figure the
independent third parties support, and say so.

**What "agree" means for two secondaries.** Near-agreement on the *same metric from
the same report* — 75% and 76% for "marketers using at least one form of AI" — is
agreement: stage the substance with the range and cite both. Do not discard it, and do
not invent a numeric tolerance — this is a judgement about whether it is the *same
claim*, not a fixed cutoff. Genuine substantive disagreement — a different metric, a
different survey, or materially different numbers — is a **HOLD**.

Write a findings file per scope under the run's `research/` directory — plain
markdown, opening with a line marking it working material, not publication. For each
question record: the finding (or HOLD), and **every page you fetched with its URL, its
date, and whether you judged it primary or secondary.** The pages you read are part of
the evidence, not a number — surface them. Do **not** register this file as a routed
source; the staged `found` entries are the only channel to the writer.

## 6. Hand off

Leave the stage `running`; the auditor closes it. Do not promote anything — you found
it, you do not get to confirm it.

---

# The auditor

You are independent of the searcher. Read the staged findings and their provenance
(the JSON carries `verbatim_quote`, `citation`, `corroborating`, `source_date`,
`volatility` on each entry):

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/context.py --run-dir <run> read --scope <s> --include-staged
```

## 1. Confirm the quote is really there

For each finding, **re-fetch the cited source** with the same extraction-only prompt
the searcher used (WebFetch is uncapped — this costs no budget) and confirm the
**verbatim quote literally appears**. For a `secondary-corroborated` finding, re-fetch
**both** corroborating sources and confirm they agree; try the primary once yourself.
If the quote is not on the page, or the page is gone, **drop** it
(`context.py drop --id <id> --detail "quote not on source: <why>"`). This is routine.

**Confirm the whole finding, not just the quote — and never promote with a note.** A
matching quote is necessary, not sufficient: the finding's `text` often carries specifics
beyond the quoted sentence (extra steps, a variant, a count, a date, a product name), and
a searcher will sometimes add plausible detail the source does not actually state. Check
the *entire* `text` against the source. If any claim in it is not on the page, you may
**not** promote it as-is and you may **not** promote it with a "fix this later" note — a
promoted finding is one every clause of which the source supports. Instead **drop it and
re-stage a clean version** carrying only the confirmed claims:

```
context.py drop --id <id> --detail "text asserts <X> not on source; re-staged clean as <newid>"
context.py append --provenance found --scope <s> --found-by research --citation <url> \
  --source-class <...> --source-date <...> --quote "<...>" --volatility <...> \
  --text "<only what the source confirms>"
```

Then audit and promote the clean re-stage (§2). Structural how-to findings (workflow
blueprints, tool taxonomies) are where this bites most — verify each step, trigger, count,
and name against the page, not the shape of the claim.

## 2. Judge recency, and cross-check what is volatile

A `durable` finding — a dated historical figure — needs no more than its quote
confirmed; **promote** it. A `volatile` finding — a version, price, or status — can go
stale between publication and now, so it **cannot promote until you record a verdict**,
and the tooling enforces that:

- Is the source **current enough** for this claim? A product status from months ago is
  suspect; find the maker's most recent word. Freshness is a judgement about the fact,
  not a fixed number — state your reasoning.
- **Cross-check** the volatile fact against a second independent current source.

Record the verdict, then promote:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/context.py --run-dir <run> audit \
  --id <id> --recency-ok true --cross-checked true --by auditor
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/context.py --run-dir <run> promote --id <id>
```

If the source is too stale to trust, or the cross-check disagrees, record a negative
verdict and **drop** it with the reason. Never let your own belief about "the current
state" stand in for a re-fetched source.

**When you disprove a finding but find the real answer, store it — do not just drop.**
Dropping is not the end of the question. If your cross-check shows the staged number is
wrong but surfaces the correct, better-sourced figure — a primary page you reached, or
the figure the independent third parties actually support — **stage that corrected
finding with full provenance and promote it** (you may confirm your own additive find).
A question must not be left unanswered because its first answer was wrong and the right
one was already in your hands. Prefer the primary figure that enough independent third
parties validate; where two primaries disagree because they surveyed different
populations (a vendor's 91% vs another maker's 80%), prefer the one the third parties
corroborate and note the other — never average them into a number no source states.

## 3. Audit coverage — you are the real check on thoroughness (item 8)

The searcher is not a trustworthy judge of when it did enough. Re-derive the question
list yourself and, for every question left thin, unfound, or held, **run the searches
it should have.** Where you reach a primary the searcher missed, or close a HOLD, stage
it as a `found` entry with full provenance and confirm it before promoting (you may
confirm your own additive finds — confirmation is a source-check with a definite
answer). A brief left under-covered is a result worth surfacing, not a pass.

## 4. Contradictions go to a person

If two findings **genuinely contradict** and the evidence cannot settle which is right,
do not pick — `promote` will refuse anyway. File it
(`context.py append --contradicts <id> ...` / `manifest.py flag`). This is the *only*
thing that reaches a human, and it is rare.

## 5. Close the stage and report

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/manifest.py --run-dir <run> stage \
  --target <group> --stage research --status passed \
  --detail "<pieces>; searcher staged N, auditor promoted P, dropped D, added A"
```

Mark `skipped` (with a reason) if the group's questions asked for no research. Report,
in plain language:

- per question: **promoted / dropped / held**, and for each promoted finding its
  source class, source date, volatility, and the confirming quote.
- **searcher staged N; auditor promoted P, dropped D (each drop's reason), added A.**
  The drop count is the signal for whether the searcher's discipline is holding.
- **every source that was re-fetched**, with its date and primary/secondary class — so
  a reader can see the pages, never just a count.
- for volatile findings, the **recency reasoning** you recorded.
- any genuine contradiction left for a person, if any.
