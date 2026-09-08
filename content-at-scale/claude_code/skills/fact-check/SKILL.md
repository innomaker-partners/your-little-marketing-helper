---
name: fact-check
description: Use to verify a produced content piece against the run's operating context and, when budgeted, live sources for the pipeline's own claims - marking which sentences carry claims and challenging the ones the pipeline itself produced. Runs per piece. Called by content-at-scale:orchestrate; rarely invoked alone.
---

# fact-check

You are checking a piece of content against what this run actually knows, and
recording which sentences carry the claims you checked.

**Read `${CLAUDE_PLUGIN_ROOT}/REFERENCE.md` first** for the provenance labels,
the scope strings and the script syntax.

This skill runs **per piece** only: it checks one finished piece's claims against
what the run knows. Three end-of-batch stages that once lived here are now their
own skills, each a separately dispatched stage — do not fold their work back into
this one:

| stage | what it checks | skill |
|---|---|---|
| `group_review` | repetition within a piece, format across a group | `content-at-scale:group-review` (last per group) |
| `cross_group` | contradiction across the run + brand/use-case conformance | `content-at-scale:cross-group` (last subagent stage) |
| `web_pass` | pipeline-produced claims, externally | *deferred — not built; recorded `skipped`* |

---

## Provenance decides what you may attack

This is the mechanism, not a formality. Read the labels before you read the
content.

| label | what you do with it |
|---|---|
| `given` | authoritative. Do not challenge it. |
| `ruled` | authoritative. A person already settled this one. |
| `sourced` | challenge **the citation**, not the fact. Does the cited segment actually say this? |
| `found` | fully challengeable. The pipeline discovered it. |
| `inferred` | lowest trust. Attack this first. |

**Why you are forbidden from attacking `given`.** The user asserted it. If you
challenge ground truth you are asking the pipeline to adjudicate its own
inputs, and the only material you could adjudicate it against is material the
pipeline produced — which is how a fact-check gate quietly becomes a machine
for confirming its own findings.

**Why `sourced` is challenged at the citation.** A citation that points at a
20,000-word transcript is not a citation. Follow it; if it does not resolve to
a specific passage that says what the entry claims, that is a real finding and
it is about the entry, not about the content.

Read the context at the piece's own scope, which gives you global, then the
group, then the piece:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/context.py --run-dir <run> read --scope g2-p3 --include-staged
```

⚠️ **`--include-staged` matters here.** Without it you cannot see the runtime
findings the piece was written against, and you will report a well-sourced
sentence as unsupported. Staged entries are marked `UNCONFIRMED` — treat them
as claims to check, not as context to check against.

---

## Before the piece is written — scope integrity

**This runs before production, not after.** A re-scoped run — one that inherits
another run's context and relabels its pieces (`g2-pN → g1-pN`) — updates each
entry's scope *field* to a valid id but does not rewrite the entry *text*. So a
`ruled` entry whose scope now reads `g1-p3` can still say, in its own words, "…may
be used in the **g2-p3** piece." The scope field is valid, so no deterministic
guard fires; the dead id is buried in prose, so only reading catches it — and if it
is caught after the piece is written, the work is already done and wasted.

So before the producing agent is briefed, read the context entries routed to this
piece and ask of each: **does its text carry a piece id that is not a live scope in
this run?** Derive the run's valid scopes from the manifest — global, each group,
each piece — and derive the run's re-scope map from the inheritance record its spec
carries (for example `g2 → g1`, `g2-pN → g1-pN`). A piece id sitting in an entry's
text that is not a live scope is a stale-id finding.

**Log it and replace it — do not stop the run over it, and do not leave it for the
writer to trip on.** A stale id in text is a mechanical leftover of the re-scope: the
scope field was relabelled and the prose was not. The correction is that same relabel,
finished — apply the run's re-scope map to the dead token (`g2-p3 → g1-p3`) so the
text names the piece it is actually scoped to. This is not you rewriting a ruling; it
is the re-scope operation completing a substitution it half-made, and the ruling's
substance carries through **verbatim — only the dead id token changes, every other
character stays.** The corrected entry keeps its `ruled` label because it is the same
human ruling, relabelled; the audit trail below records that nothing but the id moved.

The context is append-only, so you replace by re-stating, never by editing in place:

1. **Log it.** In your report, name the entry id, its provenance, the dead id, and
   the live id you are relabelling to, and quote the clause it sits in. Your report is
   the log — `append` takes no detail field, so the record lives here and in the
   retract detail below.
2. **Append the corrected entry** with the *same* provenance and scope and only the
   token swapped — e.g. `append --text "…g1-p3…" --provenance ruled --scope g1-p3`. It
   returns a new entry id.
3. **Retract the stale entry** pointing at the corrected one — `retract --id <stale>
   --superseded-by <corrected> --detail "re-scope relabel g2-p3→g1-p3"` — so the
   original text and history stay on the record and any reference to the old entry id
   still resolves through the supersede trail. (`retract` acts only on a *promoted*
   entry; a stale id sitting in a *staged* entry is a separate case — see the note
   below.)

**A stale id in a staged entry** takes the staged path instead: `retract` acts only
on a promoted entry, so `drop` the stale one (with a detail naming the re-scope
relabel) and `append` the relabelled copy back at the same provenance and scope — a
staged `found` entry re-states with the same source-class, date, quote and volatility
it already carried. Drop is retract's staged equivalent, and the one-token guarantee
is the same. Relabel every occurrence the same way, whether the dead id sits in a live
directive (a ruling that governs this piece) or a historical note (an entry recording
what an earlier stage did): a re-scope is uniform — the piece once called `g2-p2` *is*
`g1-p2` now — and a half-relabelled store is how the id went stale to begin with.

**The one token you cannot resolve is the one to stop on.** If a dead piece id maps
to nothing — no re-scope entry, no live scope, naming a piece that never existed — do
not invent a target. Log it and stop the piece there: that is a genuinely dangling
reference for a person to settle, not a mechanical relabel. This whole check bites
only re-scoped runs; on a fresh run it finds nothing and costs one read.

---

## Per piece

### 1. Mark the stage running

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/manifest.py --run-dir <run> stage \
  --target g2-p3 --stage fact_check --status running
```

A stage starts at `pending` and the only legal moves are `running` and
`skipped`. The orchestrator closes it — report your result and let it set
`passed` or `failed`.

### 2. Find the claims

Read the draft and identify **every sentence that asserts something checkable**.
A claim is a statement that could be false: a number, a date, a name, a
capability, a comparison, an attribution, a causal statement.

Not claims: opinions, framing, transitions, calls to action, and anything
purely stylistic. Marking those adds noise to the claim-diff gate later, which
makes it flag legitimate rewrites and trains whoever reads its output to
ignore it.

Be exact and report counts as counts. "23 of 61 sentences carry claims" is a
result. "Most of it checks out" is not, and a check that cannot produce an
exact number was not performed.

### 3. Check each one

Against the context first. Then, if the run has web access budgeted and the
claim is `found` or `inferred` in origin, against live sources.

For each claim, one of:

- **supported** — name the entry id that supports it, and quote the specific words from
  that entry that establish the claim, verbatim. Naming the id is a pointer to where a
  check could happen; quoting the words is the check. It is what lets the claim and its
  warrant sit side by side, and it is what catches a claim that keeps its citation while
  drifting from what the source actually says.
- **unsupported** — nothing in the context establishes it
- **contradicted** — the context says otherwise

**Unsupported is the interesting verdict and the one most often mishandled.**
It does not mean the claim is false. It means the piece asserts something this
run cannot stand behind, which is the exact thing this tool exists to catch.
Do not resolve it by finding the claim plausible.

Two legitimate routes for an unsupported claim:

1. **It is removable** — the piece works without it. Cut it, and say you did.
2. **It is load-bearing** — then it needs a source. If research can establish
   it and there is budget, that is a runtime finding: it goes through
   `content-at-scale:agent-management` as `found`, staged, with a citation.
   Never append it yourself as though it had been there all along.

**What you must never do is soften it.** Turning "cut deployment time by 40%"
into "can significantly reduce deployment time" does not fix an unsupported
claim, it hides one. The vaguer sentence is still an assertion the run cannot
support, and it now reads as though somebody checked it.

### 3b. Composition pass

Run this after step 3. It catches a different failure mode: sentences that are
individually true and individually backed by a sourced entry, but whose
**framing was invented by the pipeline**. Standard provenance checking misses
this because it asks "does a source entry say this?" — the composition check
asks "does the *way this is said* match how the source says it?"

This pass lives here rather than in a separate stage because the same
adversarial posture that challenges citations can challenge composition — and a
stage that can silently not run is the disease this whole skill exists to treat.

**Three patterns to challenge:**

**Pattern 1 — General observation → first-person event.**
The source entry says "structural engineers recommend leaving a 48-hour cure
window between concrete pours to prevent shrinkage cracking"; the piece says
"We always allow a 48-hour cure window between our pours." The claim has moved
from a published engineering recommendation to a specific first-person account
of what the team does, and the specifics (their pours, their schedule) are not
in the source. Flag when: the piece claims first-person ownership of a specific
system or practice ("we do X", "we run Y", "we built Z") and the supporting
entry describes a design principle, a general pattern, or a third-party
observation — not the team's own practice.

**Pattern 2 — Two incidents welded into one.**
The piece reads as one episode; the context has two entries describing separate
events, each contributing a detail. The narrative implies a single occasion
that the sources never describe as one. Flag when: a passage draws causal or
temporal links between details that come from different source entries, and
those entries describe distinct incidents.

**Pattern 3 — An invented frame around true details.**
The underlying details are sourced but the temporal or causal setting is not.
"Following the equipment upgrade" wrapped around a calibration result whose
source entry describes routine quarterly checks with no mention of any upgrade.
"After the site inspection" around a materials delivery record whose source
entry contains no inspection-related framing or trigger. Flag when: a causal
or temporal setting appears in the piece (a prepositional opening, a
scene-setting sentence, a "when X happened, we found") and that setting does
not appear in any source entry that covers the passage.

**Pattern 4 — A scene that traces to nothing.**
The three patterns above all assume the details trace to *some* source entry and
only the framing was invented. The worse case is a scene, anecdote or opening that
traces to **no source entry at all** — a whole episode the pipeline made up. It
slips past ordinary claim-checking because a narrative frame asserts nothing
checkable ("During the pivot we handed a stack of billing screenshots to the model
and asked it to add them up; it came back with a confident total that was wrong") —
it is a frame, not an assertion — and it slips past Patterns 1-3 because there are
no sourced details underneath it to mismatch against. So ask of **every scene and
anecdote, not only every claim**: *did this happen, and where in the sources is it?*
If the episode — its actors, its action, its outcome — cannot be pointed to a routed
source entry, it is invented, and being unfalsifiable is exactly why it is dangerous,
not why it is safe. Flag when: a concrete episode (a specific occasion, a first-person
"we once…", an opening scene) has no routed source entry describing that episode.

**The false-positive guard.** Do not flag:
- Narrative connectors ("building on this", "the same applies to", "this is
  why") that do not claim a specific event
- Causal transitions ("because of this", "as a result") where the causal logic
  is directly supported by the source entries
- A piece that says "we do X" when a source entry explicitly records that they
  did X — direct correspondence is not a composition failure
- A temporal frame that appears verbatim in a supporting source entry's text
- A general illustration or hypothetical marked as such ("imagine a team that…",
  "suppose you…") — it claims no real episode. Pattern 4 is about episodes
  presented as things that actually happened, not openly hypothetical ones.

**How to report a composition finding.** Quote the piece sentence(s). Name the
source entry (or entries) that the passage traces to. Describe specifically
which of the three patterns applies and what the mismatch is. Do not rewrite
the sentence — the fix belongs to the producing agent. If you flag something,
say whether it is removable or load-bearing.

**If the composition check finds nothing, say so explicitly.** A silent pass
is indistinguishable from a skipped check.

### 3c. FROZEN rules

The run may declare rules as FROZEN: lexical or fidelity constraints that are
immutable and that no downstream step may waive. Common forms include a required
term that must appear verbatim, a substitution that is explicitly forbidden, or
a figure that must be reproduced exactly as recorded — not rounded, not
paraphrased. These are not style preferences; they are hard commitments the run
made when it declared the rule.

**Enumerate every FROZEN rule declared for this run.** Find them in the run's
operating context — typically marked as `given` entries or in the run spec. If
the run declares none, say so explicitly and continue. A silent pass is
indistinguishable from a skipped check.

For each FROZEN rule, record:

- **The rule** — quoted exactly as declared
- **Verdict** — **complies** or **breaches**, with the specific sentence(s) at issue named

**A FROZEN breach is a hard stop.** Do not proceed to section 3d or write
`verified.md` or `claims.json`. Report immediately to the orchestrator: quote
the rule exactly as declared, quote the offending sentence(s), and return a
FAILED verdict. The orchestrator fails the stage. This is not a soft flag — the
checker may not waive it, treat it as advisory, or continue past it on the
assumption that a later stage will catch it.

### 3d. Invented scenes: log and replace from the sources

The default posture above is *challenge, do not rewrite* — the producing agent
owns the fix, and you never append material as though it had been there all along.
That default holds for an unsupported **factual claim**, which may need research
the sources do not contain: you cut it or hand it off staged as `found`, and you
never invent it.

An invented **scene** (Pattern 4, and any Pattern 1-3 frame whose only honest fix
is to replace the episode) is the deliberate exception, and the reason is specific
to it: the real material is already in this piece's routed sources. The failure
that produces an invented scene is a writer that had the real words in front of it
and made something up anyway — so the remedy is not research, it is reaching for
what was already there. Do both, in this order:

1. **Log it.** Quote the invented passage in your report, state that it traces to
   no routed source entry, and name it as a fabrication — so the swap is on the
   run's ledger and a person can see what the piece tried to claim, not merely that
   the text changed.
2. **Replace it from the sources.** Find real material in this piece's routed
   sources that serves the same narrative purpose — a real episode, a really-said
   line — and write it into `verified.md` in place of the invention. Record what
   you removed, what you put there, and the **source entry id** the replacement
   came from.

**The guard that keeps this from becoming fabrication:** the replacement may only
use material that is actually in the routed sources. You are moving real words into
the piece, never minting new ones — that boundary is the whole difference between
this and the thing you are here to catch. If the routed sources hold nothing that
can carry the scene's purpose, do not force one: cut the scene and log that no
sourced replacement existed. A clear removal beats a replacement you had to invent.

**If your replacement carries a factual claim** — a real figure, a date, a named
tool from the transcript — add its sentence to `claims.json` so the claim-diff gate
protects it through de-slop, exactly as it protects the writer's own claim
sentences.

### 4. Write the two artifacts

```
<run>/pieces/g2-p3/verified.md
<run>/pieces/g2-p3/claims.json
```

`verified.md` is the text you are signing off — the draft, plus any correction
you made. **Write it even when you changed nothing.** It is the `--before`
side of the claim-diff gate, and that gate needs a file that will not move
under it.

`claims.json` holds the claim sentences, as they appear in `verified.md`:

```json
{"claims": [
  "Independent scoring of published marketing tooling put the average at 6.2 out of 12.",
  "The pivot began in the second half of 2026."
]}
```

A bare list of strings is also accepted. **It is a file path that gets passed
to `claimdiff --claims`, never inline JSON** — passing JSON on the command
line produces a file-not-found error, which reads like a missing artifact
rather than a malformed call.

⚠️ **An empty claims list is refused downstream, on purpose:**

```
error: no claims to check. If fact-check marked none, that is itself worth a
look - a piece with zero claim sentences either asserts nothing or was not
marked up.
```

So if you genuinely found no checkable claim — possible in a short opinion or
narrative piece, and rare — **say so explicitly in your report** rather than
writing an empty file and moving on. The de-slop agent then skips the
`claim_diff` stage rather than running a gate that cannot run, and the skip is
visible in the run report where a person can disagree with it.

Far more often, an empty list means the markup step was skipped. Check that
before you conclude the piece asserts nothing.

**Copy the sentences exactly.** The gate resolves each claim to the whole
sentence containing it and compares that sentence before and after, which is
what lets it see a verified sentence being *extended* with a qualifying
clause. A paraphrased claim in this file will not match its own sentence, and
the gate will report a change that never happened.

### 5. Report

Give the orchestrator: how many sentences, how many carried claims, the
verdict counts, every unsupported or contradicted claim quoted in full, and
what you changed in `verified.md`.

**Repeated failure is a context problem, not a bad piece** (FROZEN). If a
piece comes back to you a third time with the same claims
unsupported, say so in those words. The producing agent is not going to
invent the evidence on the fourth attempt, and the honest finding is that the
context this group was given does not cover what it was asked to write about.
