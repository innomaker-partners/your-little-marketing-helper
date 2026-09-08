---
name: source-intake
description: Use to ingest the raw material for a content-at-scale run - research documents, meeting transcripts, keyword research - and route it to the right scope. Segments each source so different sections can serve the whole run or a single group, has the segmentation adversarially challenged, then confirmed by the user in bulk. Called by content-at-scale:orchestrate.
---

# source-intake

You are taking the run's raw material and deciding **which parts of it serve
which parts of the run.** That routing decision is the whole job.

**Read `${CLAUDE_PLUGIN_ROOT}/REFERENCE.md` first** for the scope strings,
the provenance labels and the script syntax. Run `context-intake` before this
— the manifest defines the groups that segments get routed to, so the scope
ids do not exist until it has run.

## Why routing is the job

Source material is **not atomic** (FROZEN). One meeting
transcript can hold a section about the whole brand and another about a
single format. A strategy document can be half positioning and half a
per-channel plan.

Route a source wholesale and you get one of two failures, both quiet:

- **Starvation** — a group-specific section routed to global never reaches
  the group that needed it, in a form specific enough to use. The pieces come
  out generic and nothing reports an error.
- **Leakage** — a group-specific section routed to global reaches *every*
  group. Now the case studies carry the LinkedIn group's audience
  assumptions, and the pieces are confidently wrong in a way that passes
  every gate, because the gates check against this context and this context
  is what is wrong.

Neither failure announces itself. That is why this stage gets an adversary
and the others do not.

---

## 1. Gather the sources

Mark the stage running first, before you read anything:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/manifest.py --run-dir <run> stage \
  --target run --stage source_intake --status running
```

A stage starts at `pending` and the only legal moves from there are `running`
and `skipped` — marking it `passed` at the end without this simply fails. It
also makes an interrupted intake visible: `pending` means nobody started,
`running` means somebody did and stopped, and a resumed orchestrator has no
other way to tell those apart.

Ask what the run has and where it is. Research documents, meeting
transcripts, keyword research, existing content to work from, notes.

Read each one before proposing anything about it. Not its filename, not its
first page — the actual file. A segmentation proposed from a filename is a
guess wearing the costume of an analysis.

For anything large, note the line numbers as you read. You need them for
citations, and going back for them afterwards costs a second full read.

### Before you segment: ask each source's role

A source has a **role** — what it is *for* — and it is independent of who owns
it. Ask, per file, in the user's terms: *"is this here for what the pieces
should **say**, for how they should **sound**, or both?"*

- **`content`** — a source of what the pieces say. It gets segmented and routed
  below.
- **`voice`** — a source of how they sound (their own posts, a sample they want
  emulated). It feeds the voice anchor and is **not** segmented or routed. Set
  it aside here; it belongs to the voice step in `context-intake`, not to the
  segment map.
- **`both`** — a transcript that is voice *and* content at once: routed as
  material below, *and* the group's voice reference. This is the common case for
  a recorded session.

Why ask now, before segmenting: segmenting a voice-only source and routing its
lines into pieces is the exact collapse this field prevents — a voice reference
silently becoming content. Knowing the role first tells you which sources even
enter the segmentation below. The guard in step 6 reads the manifest and refuses
to route a `voice` source — but that refusal is only active once the role is a
recorded fact, not a remembered conversation answer.

**Register each source's polarity and role here, before segmentation begins.**
Ask: is this the user's own material or someone else's? That is the polarity
(the full floor/ceiling reasoning is in step 5; what matters now is the answer).

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/manifest.py --run-dir <run> source \
  --path <absolute/path/to/transcript.md> \
  --polarity owned --role both
```

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/manifest.py --run-dir <run> source \
  --path <absolute/path/to/research.md> \
  --polarity third_party --role content
```

A `voice` source registered here is set aside — it does not enter the
segmentation below, and the guard in step 6 will refuse to route it into a
piece. Registration is idempotent: re-recording the same file with the same
values is a no-op. Re-recording with different values is refused; polarity and
role are facts about the file, not adjustable defaults.

---

## 2. Propose the segmentation

For every source, propose:

| | |
|---|---|
| **segments** | where one section stops covering one thing and starts covering another |
| **scope per segment** | `global`, or a group id, or a piece id |
| **what it establishes** | the assertions this segment actually supports |

A segment boundary is a change of *subject and applicability*, not a heading.
A transcript with no headings still segments. A document with fifteen
headings may be two segments.

**Default to the narrowest scope that is true.** Global is the expensive
mistake here, because a wrong global entry contaminates every group at once
while a wrong group entry damages one. When genuinely torn between global and
a group, route to the group and say in the confirmation that you did.

**Keyword research goes to group scope.** A group effectively *is* a keyword
cluster. Brand and seed terms stay global.

---

## 3. Have it attacked

Dispatch a **Sonnet** subagent to challenge the proposal (FROZEN). Not to
review it, not to sanity-check it — to attack it.

**Why an adversary belongs here and nowhere else in this design.** Every gate
in this tool checks content against the context. **Nothing checks the context
itself.** This pass is therefore the only gate the context ever gets, and it
is also what makes bulk confirmation safe at step 5 — the user is confirming
a proposal that has already survived an attack, not a first draft.

Give the adversary the sources and the proposal, and tell it plainly that its
job is to find failures rather than to validate success. Direct it at:

- **a segment routed global that is really about one group** — the leakage
  case, and the one to hunt hardest
- **a segment routed to a group that the whole run needs** — starvation
- **a boundary in the wrong place**, especially a segment that is really two
- **an assertion the segment does not actually support** — a claim read into
  the text rather than out of it
- **material with no segment at all**, silently dropped

Require it to name a specific segment and quote the text for every finding.
A finding without a quote has not been checked against the source, and a
plausible-sounding objection to a routing decision is very cheap to generate.

### Iterate, bounded at three

Integrate what survives, re-propose, attack again. **Three passes, maximum**
(FROZEN).

After three, a disagreement the two of you could not resolve is **surfaced as
a flagged item in the bulk review**, carrying both positions. It is not
iterated further and it is not settled by you.

This is the same principle repeated failure carries elsewhere in the pipeline:
a failure to converge is information for a human, not a reason to keep looping. Two agents that cannot
agree after three passes have found something genuinely ambiguous, and the
person who owns the content is the one who can resolve it.

---

## 4. Write the segment map to disk

Before the confirmation, write the map to a file in the run directory
(`sources.md` is fine). One row per segment: source path, line range, scope,
what it establishes.

**Findings go to disk; agents pass paths, not payloads** (FROZEN). This is
what lets a fifteen-piece run finish without the
orchestrator's context filling up with transcript text.

---

## 5. The user confirms in bulk

**Say it in plain human, not agent-language — assume you are about to fail at
this.** Your first draft of anything you put in front of the person is shorthand
only another agent reads: ids like `g2-p3`, field words like scope, provenance,
segment, gate, pipeline nouns like "the routing table". They have read none of
this skill. Do not judge the draft "clear enough" and send it — assume it is not,
and rewrite it: name each thing by what it is and what it is for, and say plainly
what you are asking them to decide and what turns on it. A sentence only someone
who has read this skill could follow is not finished.

Show one table: every segment, its scope, one line on what it establishes.
Put the **unresolved items first**, marked, with both positions stated.

**Name each piece by what it is about, never by its id alone.** The user did
not memorise which topic `g2-p3` covers; a table of bare ids is a table they
cannot check. Every time a piece
is referenced — in the routing table, and especially in an unresolved item that
asks them to choose between two pieces — write its topic beside the id, so the
choice in front of them is legible without them holding the run's structure in
their head.

Ask them to correct what is wrong. Do not walk them through it segment by
segment — a forty-segment interrogation is a conversation people abandon, and
the proposal has already been through an adversary, which is what earns the
bulk review.

Say plainly what they are confirming: **that these routings are right, and
that anything scoped global will reach every piece in the run.**

### Confirm source declarations

Every source file was registered at step 1 with its polarity and role. Before
the user confirms the routing table, verify that each source appearing in the
segment map has a recorded declaration in the manifest — no source may reach
step 6 without one. If any is missing, register it now using the command from
step 1 before proceeding.

Why polarity cannot be guessed — the reasoning behind the step 1 requirement.
**A source whose polarity was not declared cannot be measured at all.** The
overlap gate runs in opposite directions depending on polarity:

- **`owned`** — the user's own material (their transcripts, their writing,
  their recorded sessions). High overlap is the **goal**: the gate is a
  floor, and a piece that draws heavily from this source is succeeding at
  sounding like the user.
- **`third_party`** — someone else's material (research, competitor docs,
  references, background reading). High overlap means the piece is
  **lifting**: the gate is a ceiling, and a piece that echoes this source
  is plagiarising it.

The same number means opposite things under each polarity. There is no
reasonable default: guessing `owned` applies a floor to a third party's
article; guessing `third_party` applies a ceiling to the user's own
transcripts, which is the bug this whole workstream exists to fix. The same
logic applies to role: a guessed role routes (or fails to route) material
silently, so `--role` is required at registration just as `--polarity` is.

---

## 6. Append to the context

Every confirmed segment appends as `sourced`, at its confirmed scope, with a
citation **and with its text**:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/context.py --run-dir <run> append \
  --text "..." --provenance sourced --scope g2 \
  --citation "research/interview-notes.md#L120-L184" \
  --quote-from "research/interview-notes.md#L120-L184"
```

**The citation must resolve to the segment, not the file.** A citation
pointing at a 20,000-word transcript is not a citation — the fact-check agent
that follows it has to re-read the whole thing and will settle for whichever
passage looks close enough. Line range or a heading path, always.

### `--quote-from` is not optional for a transcript segment, and here is why

**A pointer is an attribution. Only text is material.** An earlier version of
this step wrote the assertion and the citation and nothing else, and **no stage
anywhere in the pipeline ever dereferenced the citation.** The producing agent
received analytical summaries of material it had never seen, and every stage
downstream worked faithfully from an already-normalised document: named tools,
verbatim phrases and the source's actual wording never reached the draft,
however faithfully the citation pointed at them.

Supplying the routed spans verbatim — the text, not just the pointer — is what
puts the source's real phrasing in front of the writer. It is the single
largest lever this step has on how much of the user's own voice survives into
the finished piece, and it costs one field.

**The quote does not replace the assertion. It joins it.** The assertion
carries your routing judgment and any prohibition attached to it; the quote
carries the phrasing, which is the thing the assertion cannot preserve and
was never able to. Write both.

**Skip it only when the segment has no quotable text** — a keyword table, a
metrics export, a screenshot description. If a person said it, quote it.

### When one source line holds more than one point, use `--quote-text`

`--quote-from` alone stores the **whole cited line**. In these transcripts a
single line is often one long paragraph — 3,000+ characters — that carries
several distinct points. If two pieces cite that same line, both receive the
identical multi-topic block, and the pieces stop being built from different
material. That is exactly the distinctness failure this guards against: one
line's paragraph can carry three separate points in one breath, and without
naming the stretch each piece needs, the whole block lands verbatim in every
piece that cites the line.

Fix it by naming the exact stretch that belongs to *this* piece:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/context.py --run-dir <run> append \
  --text "the tool paid for itself on the first project" \
  --provenance sourced --scope g2-p2 \
  --citation "sources/interview.md#L38" \
  --quote-from "sources/interview.md:38" \
  --quote-text "honestly it paid for itself on the very first project we put through it"
```

The `Nth --quote-text` pairs with the `Nth --quote-from`. The tool checks the
passage is genuinely a contiguous stretch of the cited line (whitespace
differences are tolerated) and **refuses** anything invented, paraphrased, or
lifted from elsewhere — so precision here never costs honesty. Omit `--quote-text`
and the whole line is stored, as before; use it whenever a line carries more than
the one point you are routing.

**Quote the full relevant stretch — generously, not stingily.** The writer builds
its piece from what you route here and sees nothing else, so a lone sentence
starves it. Take as much of the real talk as belongs to this piece's point:
several sentences, the setup and the payoff, however long the person spoke to it.
The rule is only that you cut it at the boundary of *this* piece's point rather
than dragging in the neighbouring topics that belong to other pieces. Rich AND
its own — both, not one.

### When a passage genuinely serves more than one piece

Sometimes a stretch of talk is not ambiguous — it truly carries a point for two
different pieces at once. This is different from the "which piece?" disagreement
that §3 resolves by escalation; here the honest answer is "both." Do this:

- **Route the piece-relevant stretch to each piece — different words per piece.**
  A single line often holds a reliability point and a value-chain point side by
  side; with `--quote-text` you send the reliability stretch to one piece and the
  value-chain stretch to the other. Each piece gets its own material.
- **Never route the identical block to two pieces.** That is exactly the
  duplication that made three pieces read the same; it is the failure this step
  exists to prevent, and `--quote-text` exists precisely so you no longer have to.
- **If the same words are genuinely inseparable and load-bearing for both** — the
  point cannot be split without breaking it — do not silently send the duplicate.
  Surface it in the bulk review (§5) as an item for the user, naming both pieces
  by their topic, and let the owner decide where it belongs or whether it earns a
  place in both.

### Redaction, before any quote is stored

If the sources name people who must not reach a draft, register the
replacements **before appending anything**:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/context.py --run-dir <run> redact \
  --from "Real Name" --to "[Colleague A]"
```

Replacement happens when the quote is stored, so the entry never holds the
raw name. Do this first: a redaction registered afterwards does not reach
back into quotes already written.

### What goes in the entry text, and what does not

The entry text is **the assertion the segment establishes**, not the segment.

This is a deliberate build decision, and the reason is worth stating: the
context is read in full by every producing agent, every gate and every sweep.
A store holding whole transcripts stops being readable at exactly the scale
this tool is for, and a context nobody can read is a context nobody checks
against. It also follows the same principle that keeps payloads on disk and
passes paths.

The cost, stated: turning a segment into assertions is judgment, done by a
model, at the stage the whole design treats as highest-consequence. The
adversary at step 3 is what covers it, which is why it is told to look for
claims read *into* the text rather than out of it.

So: several short, checkable assertions per segment rather than one long one.
A fact-check agent has to be able to hold a single entry against a single
sentence of content and say yes or no.

`sourced` is live on arrival, not staged — intake material has already been
through segmentation, an adversarial challenge and the user's confirmation
by the time it gets here. Staging it as well would hide the run's own
material from every stage that needs it.

### If a source contradicts the ground truth

It happens, and it is real signal — an old deck, a transcript from before a
decision changed. Pass `--contradicts <id>`. A flag lands in the manifest for
a human, keyed and idempotent, naming both entries.

**Know what that does.** The entry goes **live**, not staged. `sourced`
material is live on arrival, contradiction or not, because it has already
been through segmentation, the adversary and the user's confirmation. So
until someone rules, the context genuinely holds both statements and a
producing agent could read either.

**Which is why nothing may start until it is resolved** (FROZEN). The
orchestrator refuses to cross the intake barrier while an unresolved
contradiction flag is open — no piece begins until every declared
contradiction has a ruling. A run that produces fifteen pieces against a
context with a known, recorded conflict in it is the precise failure this
tool exists to prevent, and it would arrive with an audit trail saying
everything was checked.

So do not leave a flag sitting in the queue and hand off. **Get the ruling
here**, while nothing has been produced and resolving costs one question:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/context.py --run-dir <run> rule \
  --text "..." --scope global --resolves e0007
```

Handing an unresolved flag to the orchestrator is not an error, it is just
the expensive way round — the run will stop at the barrier and someone will
be asked the same question with less context than you have right now.

Do not decide which one wins, and do not decide the source must be stale
because it is older. Only the user knows.

---

## 7. Mark the stage

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/manifest.py --run-dir <run> stage \
  --target run --stage source_intake --status passed
```

If the user broke off before confirming, leave it `running`. A hard barrier
sits here (FROZEN) — context intake, source intake and any research complete
**before any piece starts** — and a resumed orchestrator reads this field to
decide whether it may cross it.

Then read the context back at a group scope and look at what a producer in
that group will actually see:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/context.py --run-dir <run> read --scope g2
```

If a group's stack is thin, say so now. Thin context does not fail loudly
later; it produces generic content that passes every gate.

---

## A note on the overlap gate

The sources you route here are the same files the overlap gate measures
finished pieces against. That means routing has a second consequence nobody
asks about at intake: **a source routed to a group is a source the group's
pieces will be checked against.**

A source that is meant to be closely followed and one that is background
reading were, until this workstream, measured identically. **That is now
solved by the polarity declaration in step 5.**

The gate runs in two directions depending on polarity (FROZEN):

> you will be tempted to fix this by lowering or raising the existing
> threshold. That is the wrong move and it is the same error one level down.
> The number is not wrong; the *direction* is. Do not touch a threshold until
> source polarity exists.

Source polarity now exists. The gate applies a **floor** to `owned` sources
(the piece should draw from the user's own voice and material — high overlap
is a pass) and a **ceiling** to `third_party` sources (high overlap means
lifting — a fail). Two pools, two directions, neither one merged with the
other.

**This only works when the polarity was declared.** A source recorded without
a polarity call cannot be measured at all — the orchestrator building the
overlap command reads polarity from the manifest. If a source file is in the
manifest's segment map but not in its source registry, the overlap command
will silently omit it, and the run will look measured when it was not.

The close-following group's pieces are now covered: declare those sources as
`owned` and the gate measures exactly the thing you want — whether the piece
sounds like the user — rather than penalising it for doing so. The third-party
research sources remain as ceilings, unchanged from before.
