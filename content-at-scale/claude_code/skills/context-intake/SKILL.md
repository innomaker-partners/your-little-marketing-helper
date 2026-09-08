---
name: context-intake
description: Use at the start of a content-at-scale run to set it up - the run shape and its groups, the run parameters and search budget, then an interview that records company and product ground truth, voice, and the user's verbatim expectations. Writes the run manifest and the operating context. Called by content-at-scale:orchestrate.
---

# context-intake

You are setting up a content run. When you are done, two files exist on disk
and every later stage reads them instead of asking again.

**Read `${CLAUDE_PLUGIN_ROOT}/REFERENCE.md` first.** It holds the scope
strings, the provenance labels and the exact script syntax. This file holds
the judgment; that one holds the facts.

## Why this stage carries so much weight

Every quality gate in this tool checks a piece of content **against the
operating context**. The length gate checks it against bounds set here. The
overlap gate checks it against sources routed here. Fact-check checks its
claims against ground truth recorded here. The adversarial pass in
`source-intake` is the only gate the context itself ever gets.

So a context that is wrong does not produce a visible failure. It produces
fifteen pieces that all pass every gate and are all wrong in the same
direction, with a full audit trail saying they were verified. That is
precisely the failure this tool exists to argue against, and this stage is
where it would come from.

There is nothing to gain by rushing this and a whole run to lose.

## The shape of the conversation

Intake asks for a lot — around eight decisions before the user has seen a
sentence of content. It runs as three moves:

```
1. The run          what are we making, how many, in what groups
2. The parameters   asked ONE AT A TIME, each waiting for a real answer
   -> write the manifest, initialise the context
3. The interview    ground truth, voice, verbatim expectations,
                    appended to disk as you go
```

**Ask the parameters one at a time. Do not batch them** (FROZEN). This is not
a stylistic preference and it costs turns on purpose.

An earlier version of this skill proposed all the parameters in a single
block for the user to correct in bulk, on the reasoning that eight questions
in a row is a conversation people abandon. A dry run skimmed it: the user
replied "fine" and both overlap thresholds went into the manifest unread.
**A block permits simultaneous dismissal; sequential questions force a
separate acknowledgement for each.** Several of these choices are
irreversible once `init` runs — the search budget cannot be re-planned at all
— and an irreversible choice nobody read is worse than a longer conversation.

So do not reassemble the block. If the user says *"this is a lot of
questions, can we just start"* — and they will — that is the moment the design
is being tested, not a signal to compress. Say what you are still missing and
roughly how many are left, offer your recommendation for each as you go so
answering is cheap, and keep going. What you must not do is take silence or a
"fine" as an answer to a question you never really asked.

**Why the interview comes last.** It is the longest move and the one the user
actually wants to have, because it is about their own company. Putting it
last means the run structure is already on disk before it starts, so a
conversation interrupted halfway is resumable rather than lost. The dry run
confirmed this ordering; only the shape of move 2 changed.

**Nothing is written to disk until move 2 is done**, because the run's
location is itself one of the parameters — until then there is nowhere to
write.

---

## Move 1 — the run

Ask for, and do not proceed without:

- **What is being produced.** Blog posts, LinkedIn posts, landing pages,
  email sequences. The format changes almost every other parameter.
- **How many, and what each one is about.** Every piece needs a topic.
  `manifest.py` refuses a piece without one, deliberately: a producing agent
  handed a null topic writes something plausible about nothing, and the error
  then surfaces far downstream from the malformed spec that caused it.

  **Users routinely answer "and one more, TBD".** Do not accept it and do not
  invent a topic to fill the slot — an invented topic is indistinguishable
  from a real one by the time a producer reads it, and the user will not see
  it again until nine finished pieces arrive with one about something they
  never asked for. Say the spec cannot hold a placeholder, offer two or three
  candidates drawn from what they have already told you, and let them pick or
  cut the piece. Nine pieces they chose beats ten with one you made up.
- **Whether the run has groups** (FROZEN). Grouping is elective
  and the user decides. A run of 15 posts that is really 5 case studies + 5
  how-tos + 5 opinion pieces has three groups; 15 posts of one kind has one.

Ask that last one as a question about their work, not about the data model:
*"are these all the same kind of thing, or are there a few different kinds in
here?"*

With no groups, group = global — one code path, always at least one group
(FROZEN). Do not describe an ungrouped run to the user as a
different mode, and do not build a different spec for it. Pass a bare
`pieces` list and let `manifest.py` make it a single group.

---

## Move 2 — the parameters, one at a time

Ask each of these as its own question and wait for its own answer. Carry a
recommendation into every one — state your proposal, the one-line reason for
it **drawn from this run**, and what the alternative would mean — so that
answering costs the user a word rather than an essay. A recommendation makes
a question cheap to answer. A block makes it cheap to ignore, which is the
difference (FROZEN).

Ask in this order. It is not arbitrary: (g) cannot be asked before the
forecast, and the forecast cannot be computed before (e).

### The parameters

**a. Where the run lives** (FROZEN). The tool does not choose
this. Propose `runs/<slug>/` under the current directory. Say plainly that
everything about the run goes there — manifest, context, drafts — and that
resuming means pointing at this path again. There is no discovery and no
pointer file at a fixed location.

**b. Length bounds, per group.** The length gate takes a minimum, a maximum,
or both, and has no default. Ask in whatever units the user thinks in and
record words. If they name a single target, propose a band around it and tell
them you did — a gate set to an exact word count fails every piece.

**c. The phrase-overlap threshold, per group** (no fixed default).
**Explain before you ask.** A number collected without the explanation is a
number chosen at random, and a random threshold makes the gate decorative.
This is the parameter a user is least able to answer from ordinary knowledge
of their own work, so it is the one where a skimmed answer costs most. In
your own words, roughly:

> **Phrase overlap** is what share of the piece's four-word runs appear
> word-for-word in the sources. This is the real gate. Independently written
> prose scores near zero on it even when it is about exactly the same subject;
> a passage copied out of a transcript scores near 100. Set it low for content
> that must be original writing, higher for a summary or a write-up meant to
> stay close to a source.

Propose a phrase threshold from what the content is *for*. The polarity depends
on whose material the group's sources are: a third-party group gets a ceiling
(how high is too close to the source); an own-voice group gets a floor (how
close to the author's wording is close enough).

**For an own-voice group, propose the phrase floor inside 55–73%.** When a
group's sources are the author's own material — their transcripts, their prior
writing — the phrase overlap runs in the *floor* direction: high overlap is the
goal, because the point is to keep the author's real wording (see the
FLOOR-vs-ceiling split in orchestrate). But high has a ceiling of its own. Above
roughly 73%, the writer can only clear the floor by dumping near-verbatim
transcript: the connective tissue that turns quoted fragments into an argument
is *not* the author's verbatim words, so it counts against the floor, and a
higher number strips exactly the writing you want. Below about 55% too little of
the author is left and the piece stops sounding like them. **Steer the user
toward 55–73% for an own-voice group and say why.** If they choose a number
outside the band anyway, that is a legitimate answer — record it, note that you
flagged the zone, and run what they chose. The band is where the floor and the
connective-tissue goal both survive; the script does not enforce it.

**A group may override the run-level phrase threshold.** Ask for the run-level
threshold first; it is the default for every group that does not set its own.
Then offer an override **only where the run actually needs one** — a group whose
pieces are meant to stay close to a transcript sitting alongside a group whose
pieces must be original writing. That shape has no single correct run-level
answer: the threshold that passes the close-following group waves the other
group's lifting through, so each group needs its own.

Do not walk every group asking for an override. Most runs need one threshold.

**If the user says "just pick one", that is a legitimate answer and you take
it.** Pick, say which threshold you picked and what would make you change it,
and record in the entry that the user delegated it. What you must not do is
present a number as their decision when it was yours — a run whose thresholds
nobody understands will produce a gate failure nobody can act on.

The gate also reports a profile at three, four and five-word runs and applies
the threshold at four (FROZEN). The user does not need that to
answer. You need it so you do not describe the gate as one number when it is
three.

**d. The word-overlap FLOOR (one number for the run).** Explain before you ask:

> Word overlap is what share of the piece's vocabulary appears anywhere in its
> sources. It runs high no matter what, because the subject dictates the words —
> original writing typically measures 30–46%. It is **a floor, always**: the piece
> must be grounded in the material it was built from, whether that material is your
> own or a third party's. It fails only when it falls *below* the floor — never for
> being high. (Verbatim copying is caught by phrase overlap, above, not here.)

Propose a floor below honest-prose range (e.g. ~0.20–0.30) so it fires only when a
piece has drifted off its sources, and say why. This is the `--min-word-overlap`
value; it applies to every source pool the run has.

**Recording overlap thresholds as locked parameters (FROZEN).** After the user
confirms each overlap threshold, record it under `locked_parameters` in the run
spec using the `{value, by, at}` provenance shape — not just under `parameters`.
The key scheme is flat and JSON-serializable:

| Key | Meaning |
|---|---|
| `overlap.word_floor` | Run-level word-overlap floor (one number for the whole run) |
| `overlap.phrase_floor` | Run-level phrase-overlap floor for owned sources |
| `overlap.phrase_ceiling` | Run-level phrase-overlap ceiling for third-party sources |
| `overlap.phrase.<gid>` | Per-group phrase override — only when a group truly needs one |

Example locked_parameters entry produced at intake:

```json
"locked_parameters": {
  "overlap.word_floor":     {"value": 0.25, "by": "editor", "at": "2026-08-26T10:00:00+00:00"},
  "overlap.phrase_floor":   {"value": 0.60, "by": "editor", "at": "2026-08-26T10:00:00+00:00"},
  "overlap.phrase_ceiling":  {"value": 0.05, "by": "editor", "at": "2026-08-26T10:00:00+00:00"}
}
```

Recording under `locked_parameters` (not only `parameters`) means `overlap.py --run-dir`
will read the thresholds from the manifest automatically — the orchestrator need not pass
`--min-word-overlap` each time. A genuine mid-run change goes through
`manifest.py --run-dir <run> params --update --key overlap.<...> --override --reason "..."`,
which blocks completion until a person accepts it — a threshold
can no longer be recalibrated on the run's own data without a human sign-off.

**e. Research at each level** (FROZEN). Some research serves the
whole run, some serves one group only. Propose per level: **global** (what is
true across everything being produced) and **group** (the format, the
audience, the keyword cluster for that group). Per-piece research is
deferred to (g) because it dominates the budget.

**f. Keyword clusters**, if SEO matters for this run. Keyword research sits at
**group** level — a group effectively is a keyword cluster. Brand and seed
terms stay global.

If a run skips keyword research, **omit `keyword_cluster` from the group
entirely** rather than passing an empty string. The manifest stores whatever
you give it without judging it, so `""` is a value a later stage has to guess
about, while an absent field reads as null and means what it says.

**g. Per-piece research** (FROZEN) — decided per run **after** the budget
forecast, never before.

**h. The promotion tier for a confirmed finding** — **ask this one only if the
run holds more than one
substantively different group type.** Otherwise default to **group** scope,
do not raise it here, and mention it later only if a stage actually produces
a finding of real cross-group value.

Three LinkedIn posts and three more LinkedIn posts are not substantively
different. Case studies and how-tos are.

When you do ask, name the trade rather than the mechanism: *promote to global
and every group benefits, but all the pieces become coupled through it; keep
it at group level and the groups stay independent, but a later group may
re-research what an earlier one already established.*

**Why it is conditional now.** A dry run showed users accept this one without
engagement, and the reason is structural rather than careless: evaluating the
trade requires a mental model of a research pipeline the user has not watched
run. In a single-format run the coupling risk is negligible and the cost of
getting it wrong is a handful of re-researched searches, so asking buys
nothing but a question the user cannot answer. In a genuinely mixed run the
trade is real and it is worth the turn.

### The search-budget forecast

A session may make a limited number of **WebSearch calls**, counted across the
main conversation and every subagent it spawns. Parallel research agents share
one pool; they do not each get the full allowance.

**Read the ceiling. Do not assume 200.** Run this — it is a command, not a
reminder:

```
echo "${CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION:-200 (unset, platform default)}"
```

A dry run of this intake assumed 200 despite being told not to, which is what
an instruction phrased as a caution reliably gets you. If the variable is
unset, say so to the user in those words — the allocation is then against a
documented default rather than against this session's actual ceiling, and the
difference matters if someone has lowered it.

⚠️ **The allocation is fixed at `init` and there is no re-planning command.**
Buckets cannot be resized afterwards, one bucket cannot borrow from another,
and the `unallocated` remainder is not spendable — the summary line calls it
"unreserved, not spendable without re-planning" for exactly this reason.
Re-planning means writing a new spec into a new run directory.

So do not tell the user that leftover headroom can be added to a bucket later.
It cannot. This is the one moment the budget is decided, and it is the
clearest example of why these questions are asked one at a time — an
irreversible allocation waved through in a list is a commitment nobody read.

Forecast the proposal and show the arithmetic. **The unit is the atomic
question, not the group.** The planner (in `orchestrate`) breaks each scope into
atomic research questions, and a bucket has to cover *tracing every one of them
to a primary source* — which costs more searches than grabbing the first
roundup, and that is the point — depth first. Reckon each question
depth-first:

```
per atomic question:
  1     broad search to surface candidates
  1-2   to trace to the primary publisher (the maker's or surveyor's own page)
  +1-2  more only when the primary is blocked and two dated secondaries are
        needed instead
  ~= 4  searches per question   (WebFetch is uncapped, so reading the pages
                                 found costs nothing — only the searching is scarce)
a volatile question adds ~1 for the auditor's independent recency cross-check.
```

So a group of six atomic questions reserves on the order of 24-30, not a flat
handful. Forecast the whole run on that basis and show the arithmetic:

```
global research          6 questions x 4       =  24
group research      2 x (6 questions x 4)       =  48
per-piece research       (if enabled, same basis per question)
                                                   ---
                                                   72   against the session ceiling
```

Reserve depth-first: the provenance discipline — trace to primary, date it,
corroborate — **is** the quality lever, not an overhead to trim, so size the
bucket to spend on it rather than to grab first results. This forecast exists
because without it the run dies at the final gate — **after every piece has
already been produced**, the most expensive possible moment to discover a
budget problem.

**If the forecast does not fit, refuse and say what would** (FROZEN). Drop
per-piece research, cut the run, or raise the ceiling. Do not
quietly under-reserve a bucket to make the numbers work. `manifest.py`
refuses an over-allocation at create time and a bucket cannot overdraw
afterwards, which is the entire point of allocating rather than spending
opportunistically.

⚠️ **State the limit; do not imply precision.** Nothing exposes how many
searches this session has *already* spent. You can read the ceiling and you
cannot reconcile it against a balance. Tell the user the forecast is
best-effort against a ceiling, not a live budget. Writing it up as a
guarantee would be a lie the architecture cannot cash.

### Then write the two files

Manifest first. The context store refuses to initialise without one, because
the manifest defines the groups and pieces that context entries are scoped
to.

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/manifest.py --run-dir <run> init --spec spec.json
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/context.py  --run-dir <run> init
```

The spec is JSON, with either `groups` or a bare `pieces` list, never both:

```json
{
  "parameters": {
    "max_phrase_overlap": 0.05,
    "min_word_overlap": 0.25,
    "phrase_length": 4,
    "overlap_overrides": {"g2": {"max_phrase_overlap": 0.25}},
    "length_bounds": {"g1": {"min": 800, "max": 1200}},
    "per_piece_research": false,
    "promotion_tier": "group"
  },
  "search_budget": {
    "ceiling": 200,
    "reserved": {"global": 24, "g1": 24, "g2": 24}
  },
  "groups": [
    {"name": "case studies", "keyword_cluster": "...",
     "pieces": [{"topic": "...", "brief": "..."}]},
    {"name": "how-tos", "keyword_cluster": "...",
     "pieces": [{"topic": "...", "brief": "..."}]}
  ]
}
```

**`overlap_overrides` is shown because the schema accepts it, not because you
should reach for it** (FROZEN): the overlap gate is a phrasing-authenticity
measure, not a plagiarism ceiling, and for sources the
user owns high overlap is the goal. Until source polarity is declared per
source, moving the number for one group tunes a value measured to carry no
authorship signal. Set the run-level parameters the user gives you and leave
the overrides out unless the user asks for one by name.

**Every reserved bucket must name a group that exists.** Two groups means
`g1` and `g2` and nothing else; reserving for a `g3` that no group backs is
refused at `init`, which is the check doing its job — a budget bucket with no
group is a bucket nothing can ever spend from.

### Mark the stage running

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/manifest.py --run-dir <run> stage \
  --target run --stage context_intake --status running
```

Do this **immediately** after the manifest exists, before the interview
starts. Two reasons, and the first is mechanical: a stage begins at `pending`
and the only legal moves from there are `running` and `skipped`. Marking it
`passed` at the end without this fails outright.

The second reason is the one that matters. `pending` means *nobody has
started this*. If you skip straight from `pending` to a completed interview,
then a run interrupted mid-interview is indistinguishable from a run that was
never begun — and a resumed orchestrator, reading only this field, would
start the whole intake again over a context that is already half populated.

`parameters` is a free-form dict that the manifest stores and does not
validate — deliberately, because it is the run's record of what the user
chose and each script validates its own values at the point of use. It is
also the **only** place those choices are written down, so put everything the
user confirmed into it, including things no script reads.

**If the user states a parameter is LOCKED** — fixed, not to be re-proposed — also record its key in the spec's `locked_parameters` list alongside `parameters`. `manifest.py` treats a locked parameter as read-only: the only permitted change path is `manifest.py params --update … --override --reason …`, which logs the change and raises a flag a person must accept with `manifest.py accept-override` before the run can complete. **Never change a locked parameter by rewriting `spec.json` or the manifest directly**, and never fold a change to a locked value into an unrelated question — surface it as its own explicit decision.

---

## Move 3 — the interview

Now the part that is about their company. **Append as you go.** Do not hold
the whole interview in context and write it at the end — a session that runs
out first then loses all of it.

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/context.py --run-dir <run> append \
  --text "..." --provenance given --scope global
```

### Structure the interview around the groups

**Walk the groups explicitly, one at a time, by name.** Read them out of the
manifest and use them as the shape of the conversation.

This is the single largest failure mode found in testing, and it is
structural rather than careless. An interview organised only around *the
company* — what it does, its voice, its prohibitions — feels complete and
finishes with every group's context populated **only at the global tier**. In
the dry run, two of three groups happened to get specific material because
the user volunteered it while answering something else. The third got
nothing, and three pieces would have been produced against run-wide context
alone.

Nothing downstream reports this. It does not fail a gate. It produces
plausible, generic content that passes everything, which is the failure this
tool exists to prevent, arriving through the front door.

So for **each group**, ask what is true about *these* pieces specifically:

- who reads this group's pieces, if it differs from the run's audience
- what a reader should come away believing or able to do
- the subject matter itself — for a group like "how we actually use AI",
  *which* uses, on what work, with what result
- register and format, if this group differs from the others
- anything that would be wrong to say in this group but fine elsewhere

Append those at the **group's** scope, not global. A group with a thin stack
after this is a group you have not finished interviewing.

### What to ask about

- **The company and the product.** What it does, who it is for, **what it is
  not.** The negative half matters as much as the positive half — "we are not
  an agency" prevents more bad sentences than any positive statement.
- **Ground truth content must not contradict.** Prices, names, claims the
  company will stand behind, and things that were true last year and are not
  now.
- **Voice — and it is per group, not one for the run.** Voice is a
  group-scoped attribute: the SEO group and the
  point-of-view group in one run can read differently, and each carries its own
  anchor. Walk the groups. For **each** group, establish one anchor and record
  it with that group's `--scope`. A group that genuinely shares another's voice
  can inherit — record one anchor with no scope and every group without its own
  falls back to it — but do this only when the user says the groups read the
  same, not to save a step.

  Not adjectives. For a group, ask for a piece of writing they think sounds
  right and one they think sounds wrong, and record what separates them.
  If the user supplies writing for that group, record it with (here for `g1`):

  ```
  python3 ${CLAUDE_PLUGIN_ROOT}/scripts/voice.py --run-dir <run> record --kind user --text "<their writing>" --scope g1
  ```

  **If the group's voice IS its own source material** — a recorded session that
  `source-intake` will register `--role both`, where the group's real words are
  routed into each brief as content — do not paste the whole transcript as the
  anchor. Its voice is already in each brief as routed material. Record the
  canonical instruction to preserve that phrasing:

  ```
  python3 ${CLAUDE_PLUGIN_ROOT}/scripts/voice.py --run-dir <run> record --kind routed --scope g2
  ```

  `routed` needs no `--text`: the instruction is fixed, and the voice is the
  routed content itself. This is the routed-voice case — a group whose voice is
  its own source material.

  **If they cannot supply a sample for that group**, present the two shipped
  profiles as a real choice — enough description that a stranger can pick:

  - **James Clear** — plain, declarative, aphoristic. Short self-contained
    sentences. Each idea stands alone. No warm-up, no wind-down. The register
    of rigorous non-fiction web writing: concrete, direct, zero throat-clearing.
  - **Ann Handley** — conversational and peer-level. Warm, witty, with visible
    personality. Reads like a smart colleague explaining something they find
    genuinely interesting, not a marketer making a pitch.

  Record the chosen profile for that group with:

  ```
  python3 ${CLAUDE_PLUGIN_ROOT}/scripts/voice.py --run-dir <run> record --kind profile --name james_clear --scope g1
  ```
  or
  ```
  python3 ${CLAUDE_PLUGIN_ROOT}/scripts/voice.py --run-dir <run> record --kind profile --name ann_handley --scope g1
  ```

  **The two shipped profiles are the only fallback. Do not substitute other
  existing copy.** When the user has no sample and points you to none, the
  answer is one of the two profiles — not the company's website copy, not a
  deck, not a repositioning doc, not any marketing text you can find. Reaching
  for such copy on your own initiative is precisely how a group ends up in the
  wrong voice: scavenging the website copy, calling it the group's voice
  reference, and every draft in that group comes out voice-unvalidated. That
  copy is not a voice sample; it is marketing text written for a different
  purpose. (Existing copy becomes a voice reference *only* if the user
  explicitly decides it is — that is their call to state, never an inference you
  make for them. A group whose voice genuinely is a source they name — a set of
  call transcripts, say — records that text with `--kind user --scope <group>`.)

  **The run does not proceed until every group has a voice reference** — a real
  sample (`user`) or its own routed content (`routed`) is the preferred reference;
  a shipped profile is the fallback for "I can't", never the default. "Anchor" is
  loose shorthand for any of these, and reading it as "a profile must be chosen" is
  the exact mistake that puts a group in a borrowed voice when its own recorded
  words are already sitting in every brief. When a real sample or
  routed content is the reference, the anti-slop checker runs with no rule stood
  down — which is what you want whenever a genuine sample exists. Ask for the
  user's own writing first; reach for a profile only on a genuine "I can't".

- **Verbatim expectations.** See below — this one has a rule.
- **Hard prohibitions.** Words, claims, comparisons, formats. The cheapest
  entries to record and the most expensive to omit.

  When a prohibition is a *lexical* rule — a specific word or phrase that must
  never appear in the output (e.g. "readiness" on a run where the client has
  moved off that framing) — record it in the run's `voice/banned.json` at
  intake rather than in the context store alone. The deterministic anti-slop
  gate reads from `banned.json` and rejects any piece that contains the word,
  without requiring a judgment call. This is the right path for *lexical*
  prohibitions only: a fixed string the checker can match exactly. A fidelity
  rule that is not a fixed string — "do not call brine seawater" — is not a
  banned-list entry; the checker cannot evaluate it, and it belongs in the
  fact-check brief instead.

- **What has changed recently.** Ask directly: *what was true six months ago
  that is not true now?* Discontinued products, old prices, a positioning
  they have moved off, someone who has left. This question was not in the
  first version of this skill and was improvised during a dry run, where it
  surfaced the single largest stale-fact risk in the run — a discontinued
  product still live in the source material. Nothing else in the interview
  reaches it, because the user is answering *what is true* and a stale fact
  is not false to them, it is simply not top of mind. It is also the class of
  error the pipeline cannot catch: the sources agree with it.
- **The scope of each of the above** (FROZEN). Scope is an
  attribute of every item, established proactively with the user rather than
  assumed from the architecture. Most ground truth is global — but a group
  can have its own audience, positioning or register. A formal case-study
  voice and a loose LinkedIn voice in one run are two group-scoped entries,
  not one global compromise between them that suits neither.

**How to establish scope without turning it into a data-modelling
conversation.** Do not ask "is this global or group-scoped?" — that asks the
user to hold the architecture in their head. Ask it in their terms: *"does
that hold for all nine posts, or just the how-we-use-AI ones?"* Then you
assign the scope. Walking the groups by name, as above, does most of this
work for you, because the answers arrive already attached to a group.

When you genuinely cannot tell, ask. When you can tell but it is close,
**prefer the narrower scope and say which you chose** — a wrong global entry
reaches every piece in the run, a wrong group entry reaches one group.

**Scope strings are group ids, never group names.** Read them out of the
manifest. This bites hardest on an ungrouped run, where the single group is
*named* `all` but *scoped* `g1` — `--scope all` is rejected outright, which
is the safe failure. The unsafe one is reaching for `--scope global` instead:
it is accepted, and it files group-level material at the run-wide tier where
a later stage looking for group specificity will not find it.

### Verbatim expectations are verbatim

When the user says how something should read, what it must never do, or what
*good* means to them — **record their exact words as the entry text.** Do not
summarise, tidy, or sharpen.

This is a FROZEN rule and it is not a stylistic preference. A
paraphrase is your reading of what they meant, and every downstream stage
then optimises against your reading rather than their intent. The drift is
invisible, because the entry still looks like ground truth. Their words are
the ground truth.

If their phrasing is genuinely ambiguous, record it verbatim **and** ask a
follow-up, then record the answer as a second entry. Two entries cost
nothing.

### Provenance

At this stage you write only two of the five labels: `given` for anything the
user asserts here, and `ruled` for a resolved contradiction. The other three
belong to later stages.

Do not reach for `sourced` because the user read something off a document.
`sourced` is challengeable by an adversarial agent, and the user asserting
something **is** the citation. Mislabelling here hands a later agent
permission to attack ground truth.

### Contradictions

Writes are append-only and a contradiction is **flagged to the user, never
auto-resolved** (FROZEN).

At *this* stage the second half of that sentence is already satisfied,
because the user is in the room. Everything you are recording here is `given`
— the user asserting it — so if something they say now conflicts with
something they said ten minutes ago, there is no one to escalate to. They are
the authority.

**When you put that conflict to them, say it in plain human, not
agent-language.** Assume your first phrasing is agent shorthand — ids, field
names, pipeline nouns — that they cannot read, and that they have read none of
this skill. Name what each version actually says and what turns on the choice;
do not judge your phrasing already clear and send it.

**So do not reach for `--contradicts`. The script refuses it.** `given` and
`ruled` are authoritative, and an authoritative entry cannot be filed as
contradicting anything — authoritative entries are how a contradiction gets
*resolved*. Attempting it fails outright:

```
error: A 'given' entry cannot be filed as contradicting something.
```

Instead: say what the conflict is, in both their phrasings, ask which holds
for this run, and record the answer as a ruling:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/context.py --run-dir <run> rule \
  --text "..." --scope global --resolves e0007
```

A ruling retires the superseded entry and stays labelled `ruled` rather than
`given`, so it remains auditable which calls the pipeline forced a person to
make. A ruling is the only thing that can retire an authoritative entry — do
not reach for `drop`, which refuses on purpose.

`--contradicts` belongs to the stages that come after this one, where the
material is `sourced`, `found` or `inferred` and the person is no longer in
the conversation. That is when staging and flagging is the right answer,
because there is genuinely nobody to ask.

**Never pick a winner yourself, however obvious it looks.** A pipeline that
quietly resolves conflicting facts is the failure this tool exists to
prevent. It is also usually not obvious: the two statements are often both
true at different times, and only the user knows which one this run is about.

---

## Before you hand off

Mark the stage, and mark it honestly:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/manifest.py --run-dir <run> stage \
  --target run --stage context_intake --status passed
```

`passed` means the interview finished and the context is populated. If the
user broke off partway, leave it `running`. A resumed orchestrator reads this
field to decide whether it may proceed, and a hard barrier sits here (FROZEN):
context intake, source intake and any research complete **before any piece
starts.**

Then read the context back and actually look at it:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/context.py --run-dir <run> read --scope global
```

Read it the way the producing agent will, with nothing else in mind. If it
does not tell you enough to write one of these pieces, it will not tell the
producer either — and you have found that out at the one point where fixing
it is cheap.

## The three documents

The original design called for intake to write "three documents" — the
operating context, the company and product ground truth, and the implementation
plan carrying verbatim user expectations. In the built system they are **two
files**:

- the **manifest** is the implementation plan — groups, pieces, parameters,
  budget, every stage's status
- the **context store** holds both the ground truth and the verbatim
  expectations, told apart by scope and provenance rather than by living in
  separate files

That is a merge and it is recorded rather than glossed over. A separate
ground-truth file would have needed its own provenance model, its own
scoping and its own serialised writer — all of which the context store
already has — so three documents would have meant three of everything and
one more place to drift.
