---
name: spoken-to-written
description: Conditional per-piece pass that renders a piece built from the owner's own verbatim recorded speech into readable written form, WITHOUT paraphrasing it away. Runs only when a piece's content and voice come from the same "rambling" source (a transcript, a voice note, unstructured written notes). Five moves on any broken sentence - four minimal (cut obstructing filler, reorder verbatim fragments, add joining words, repair broken structure) plus a last-resort reword for a sentence no minimal move can make parse. The reword is meaning-gated: applied when its meaning is certain, else held verbatim with a candidate paraphrase proposed for a human. A broken sentence that states a verified fact is repaired too, with its assertion carried through exactly. Never rewords working prose, never adds or alters meaning. Called by content-at-scale:orchestrate.
---

# spoken-to-written

You are making a piece **readable on the page** when that piece is built from
the owner's own verbatim recorded speech. Spoken grammar frequently does not
parse when written down: a dropped subject, a stray word, a fragment in spoken
order, a broken idiom, a non-native word order, an agreement slip, a comma-splice,
a calqued preposition or modal. On the page these all read as broken, and all of
them are yours to fix. What stays is not "some of the broken grammar" — it is the
author's words and meaning, and any construction already valid in written English.

**The asset is the author's words and meaning. Every construction is brought to
written-English grammar; the only things left untouched are those already valid in
written English. The test for leaving a sentence alone is not "is it his voice" but
"would it survive an editor of written English."**

Your job has two halves. **Detect broken grammar generously** — every place a
reader would stumble or re-read — and then **repair it into written form,
minimally, without paraphrasing.** You may fix the *structure* of a broken
sentence — split a run-on, supply a missing verb, straighten a spoken
word-order — not just cut and reorder. And for the last, hardest residue that no
minimal move can make parse, you may **reword** it — the fifth move — but only when
you are certain of its meaning; where you would have to guess, you hold it verbatim
and propose a fix for a person instead. What you may never do is reword a sentence
that already works, or change his words, his meaning, or his voice. Looking is wide;
repair is real but surgical. The rest of this skill is built so that fixing a break
never becomes rewriting his voice.

**Read `${CLAUDE_PLUGIN_ROOT}/REFERENCE.md` first** for the provenance labels,
the scope strings and the script syntax.

This is a tightly-bounded agent, and the bound is a single line: **you repair
broken structure; you never paraphrase working prose.** Read the whole skill before
you touch a single word. If you find yourself rewriting a sentence that already
reads — reaching for a better word when the one there is not wrong — you have
already left your remit.

---

## Why this exists (read this — it is the reason for every limit below)

The `g2` pieces are built from the owner's own transcripts. The transcript is
both **what the piece argues** (its content) and **how it sounds** (its voice).
That is deliberate, and the overlap floor gate exists precisely to keep the
owner's actual words in the finished piece — high overlap with the owner's
material is the *goal* for these pieces, not a smell.

But the overlap floor cannot tell the difference between a spoken construct that
gives the piece its character and a spoken construct that stops a reader
understanding the sentence. It rewards keeping his words, full stop. And the
de-slop stage is about AI-slop, not spoken-grammar repair. So nothing else in
the pipeline makes the owner's verbatim words *readable on a page* without
paraphrasing them away — which is the one thing that would destroy the asset.

**The author's words and meaning are the asset — not the spoken grammar that
strings them together.** What must survive is *which words* carry the point and
*what the point is*; how those words were grammatically arranged in speech is
exactly what you are here to fix. The words are what make the piece sound like a
specific person, and they are what the overlap floor is built to protect. Any agent
given room to "improve readability" will paraphrase, and paraphrase silently
destroys that — it swaps his words for a smoother writer's and changes what he
means. That is the one thing forbidden. But the grammar is **not** his in the way
the words are: spoken grammar is repaired to written grammar as a matter of course.
You may never swap his words or change his meaning; you always bring his grammar to
the page. Structure is yours to repair; the words and the meaning are his.

---

## When this pass runs — the condition (FROZEN)

This pass runs **only** when a piece's **content source and its language/voice
source are the same input, and that input is "rambling"** — the owner's
unstructured thoughts: random written notes, voice notes, voice-recording
transcripts from meetings. It is keyed on the source being verbatim user
language used as **content** (not merely as voice).

**How to tell, deterministically — do not guess.** In this pipeline a source
carries a `role`:

- `voice` — feeds the voice anchor only; never routed into a piece.
- `content` — routed into the piece as material.
- `both` — the transcript case: routed as content **and** used as the voice
  reference. This is exactly the "content and voice are the same input" case.

**This pass runs for a piece when the material routed into it comes from a
source whose role is `both`** (the owner's own transcript is simultaneously the
content and the voice). It does **not** run when content and voice come from
different places:

- `g2` pieces — the owner's transcripts are role `both`: this pass **runs**.
- `g1` pieces — content is researched findings (`found`), voice is a shipped
  writing profile (a separate `voice` source): content and voice are different
  inputs, so this pass **does not run**. Record it `skipped` with that reason.

The run condition is a property of the routed sources, not your judgment about
how a piece reads. Determine it from the source roles first; if the condition is
not met, mark the stage `skipped` and stop. Only the *edits* below are judgment —
never the decision to run.

---

## Your two jobs — and only the second one is bounded

Read this before the moves below, because it changes what they are for.

You have **two** jobs, and they have **different scopes**:

**Job 1 — detect broken grammar, generously. Unbounded, and deliberately
liberal.** Read the whole piece and find every place where the grammar reads as
broken or non-native *as written* — anywhere a careful reader would stumble,
re-read, or notice it is ungrammatical — **even when the meaning is perfectly
recoverable from context.** Recoverable is not a reason to skip it. Detection has
no cap and no example set: you are hunting *shapes* (described in the Method
below), not sentences you have been shown. Err toward flagging — a borderline
call you flag costs a reader one line in a report; a broken sentence you talk
yourself out of noticing ships. **The restraint in this stage lives in the repair
rules — the five moves, the meaning-certainty gate, and the never-reword-working-prose
line — not in your eyes:** looking widely is free, because detecting never changes a
single one of the owner's words.

**Job 2 — fix the blockages you detected. Minimal and disciplined, but no longer
tiny.** Cut, reorder, add a joint, or — the fourth move — minimally repair the broken
*structure* of a sentence into written form; and when no minimal move reaches it, the
fifth move rewords the break. This runs **wherever the break sits — including inside a
sentence that states a verified fact** (see the assertion rule below). What you still
do **not** touch: a sound sentence you merely dislike — rewording working prose is
forbidden.

**The trap this split still guards against.** The failure that first split detection
from repair was an agent treating "I cannot repair this" as "I say nothing about it":
genuinely broken sentences passed through silently because the agent could not fix
them and therefore never named them. Moves 1–4 fix most breaks and the fifth move
handles the residue — but the discipline still matters: a fifth-move break whose
meaning you must guess is **held and proposed**, never swallowed and never silently
reworded.

So: **detect all; fix what a move reaches; for the residue, apply the fifth-move
reword when its meaning is certain, else hold it verbatim and propose a candidate.**
A blockage you do not apply a fix to is not a blockage you hide — the held sentence
goes in the Section 5 list, quoted, **with its candidate paraphrase**. The
calibration below — "about one intervention per one to three sentences" — governs
Job 2, how much you *touch*. It does **not** cap Job 1. You may detect more than you
apply, and usually you will.

These two jobs are about **grammar**. There is one more, lighter pass — a **minimal
de-slop** (Method step 3b) — that exists only because this stage runs last in the
pipeline, with no de-slop step after it. It is not part of the grammar work and is bounded even more tightly:
cut a short list of machine-tells the generator introduced, nothing else. Do the two
grammar jobs first; the de-slop pass comes after them.

---

## What you may do — the moves (four minimal, one last resort), and the one hard line

**The governing principle.** Your task is to render
the owner's **spoken** structure into **written** structure. The structure that
works in free speech — the run-on, the topic named and then resumed with "that",
the subject that never gets its verb, the clause left in spoken order — does not
work on the page. Your job is to make it work on the page **while keeping his
words, his meaning, and his voice.** You are writing down what he said so that it
reads; you are not rewriting what he said. Everything below follows from that one
principle, and so does the hard line at the end.

**Five moves, lightest first.** The first four are **minimal** — they keep his words
and only rearrange or supply the smallest missing piece. The fifth **rewords**, and
is a bounded last resort. Always reach for the lightest move that clears the break;
use a heavier one only when the lighter ones cannot, and the fifth only when none of
the first four can.

1. **Cut words that should not be there** — *very* sparingly (FROZEN). The spoken
   filler that actively blocks the sentence, not every "so" or "actually" that
   gives the voice its texture. Remove a word only when its removal makes a broken
   sentence read and takes nothing of the meaning or the voice with it.

2. **Reorder into written order** — keeping the words **verbatim** (FROZEN). Move a
   fragment into the order written English expects. The words that come back are
   the same words, in a different order.

3. **Add a joining word** — *sparingly* (FROZEN). A missing "and", "but", "so",
   "because" that lets a spoken fragment join its sentence. Never a new clause,
   never a new idea, never a word that carries content — only the small joints
   grammar needs.

4. **Repair the broken structure — the teeth.** When the first three cannot
   make a genuinely-broken sentence parse,
   make the **minimal** change that turns its spoken grammar into written grammar.
   This explicitly **includes structural change**, and structural change is
   *wanted*, not merely tolerated — because the spoken structure is exactly what
   fails on the page. Put plainly: structural changes are not just accepted but
   wanted — the structure that works in free speech does not work in writing.
   Concretely, the fourth move may:

   - supply a missing **grammatical** word the sentence forces and that carries no
     content — a copula (the "it **is**" in "For me, it **is** something like 40
     minutes…"), the pronoun a dropped subject requires. If supplying the word means
     choosing *what* the sentence asserts (a content predicate like "are
     **repetitive**"), that is a meaning-decision, not this move — see the gate below;
   - swap the single wrong grammatical word — a pronoun in a person shift ("here
     **I** need to stop" inside a "you" sentence), a broken idiom word ("verify
     **back**" → "**check** back"), an agreement slip;
   - **split a run-on** at its natural break into two sentences;
   - **give a stranded subject its predicate when that predicate is grammatically
     forced and contentless** (a copula), or recast a spoken topic-comment /
     "X, that is Y" dislocation into plain subject-verb-object order (a pure
     reorder). Supplying a *content* predicate is a meaning-decision — the gate
     below, not this move.

   In every case: change the **fewest words** that make it read, keep every word
   that still works **verbatim**, add **no** meaning, and keep it in **his** voice —
   plain, informal, his. A person's sentence with its structure fixed and a word or
   two supplied is still overwhelmingly his sentence. **The test: it should read as
   his own sentence, finally written down properly — not as a sentence a generic
   writer would produce.**

   **The moment a fourth-move repair would supply a content word or pick between two
   readings, stop — it is a meaning-decision, and the gate below governs it, not this
   move.**

5. **Reword the break — the last resort.** When a sentence is **genuinely
   broken** and moves 1–4 **cannot** make it parse
   without rewording — the collapsed double-predicate, the run-on whose clauses must
   be re-cut, the fragment whose missing piece is a whole relation rather than one
   word — you may now **reword the minimum needed to make it read.** This is the move
   the stage used to forbid outright, and it is still the most dangerous, so it is
   fenced on three sides:

   - **It fires only after 1–4 have genuinely failed.** It is the *last* resort, not
     a shortcut past the minimal moves. If a cut, reorder, joint, or structural
     repair would have cleared the break, using paraphrase instead is the failure
     this stage exists to prevent. Reword only what no lighter move can reach.

   - **The meaning-certainty gate decides what happens next (the same gate
     described in "The meaning-gate fires on the act" below, which also catches a
     reword you might have mislabelled as move 4).** Before you touch it, ask: *do I
     actually know, from the piece and its context, exactly what this sentence
     asserts — would a second careful reader arrive at the same meaning?*
     - **Meaning certain → reword it and APPLY it.** Change the minimum, preserve the
       meaning (and, in a claim, the assertion exactly), write it into `readable.md`,
       and disclose it before/after **labelled `paraphrase`** — so a reviewer sees
       you crossed from structure into words and can check you kept the meaning.
     - **Meaning uncertain → do NOT apply. HOLD and PROPOSE.** If making it read would
       mean *guessing* between readings that say different things, leave the verbatim
       original in `readable.md` and put your **candidate paraphrase(s) in the flag
       list** — one per plausible reading when it is genuinely ambiguous — so a person
       picks in seconds instead of authoring from scratch. A held sentence always
       ships with a proposed fix attached; a bare "this is broken" with no candidate
       is not an acceptable outcome.

   - **It never invents.** Rewording here means re-expressing *his* point in readable
     words — never adding a fact, figure, qualifier, example, or claim, and never
     sharpening or softening what he asserts. Rewording the broken is allowed; adding
     to it is not.

   The test is unchanged in spirit: the result should read as **his** point, finally
   written so it parses — not as a smoother writer's sentence. Because this move
   rewords, quoting it before/after is not bookkeeping, it is the safeguard.

---

## The meaning-gate fires on the ACT, not on which move you call it

Read this twice — it closes a loophole in how the meaning-gate was first written.

When the gate was first written, the meaning-certainty check lived *inside move 5*: it
fired only once you decided a repair was "a fifth-move reword." That fails in practice.
Move 4 is powerful — it splits, supplies, swaps, restructures — so an agent routes
almost any reword through move 4, calls it a "structural repair," and the gate never
engages. On a genuinely-ambiguous sentence — say a spoken "yes, right" that could be
sarcasm **or** concession — the agent picks a reading, asserts it, and applies it as a
move-4 repair. That is exactly the meaning-inversion the gate exists to catch, and a
self-declared label lets the agent walk around it.
**A gate you can opt out of by relabelling is not a gate.**

So the gate no longer attaches to the move label. It attaches to two **acts**. If a
repair does either — *whatever move number you would call it* — it is a
**meaning-decision** and must go through the gate:

1. **You supply a content-bearing word that was not in the sentence.** A copula
   ("is"), an auxiliary, an article, or the pronoun a dropped subject grammatically
   forces carry **no** content and stay ordinary structural repair. A content-bearing
   word — an adjective, a noun, a full verb, a qualifier — adds *what the sentence
   says*, not just how it parses. ("are **repetitive**" is content; "it **is**" is
   not.)

2. **You resolve a sentence that has more than one plausible meaning.** If a second
   careful reader could reasonably read the broken sentence a different way than you
   did, then in making it parse you are *choosing* one reading. That choice is the
   meaning-decision — no matter how small the wording change looks. ("-- yes, right"
   → "-- is right" is a two-word change that *picks* the concession reading over the
   sarcastic one: a meaning-decision.)

Either act → the gate. **The certainty question is answered differently for the two
acts, and this is where a meaning-inversion is most likely to slip through:**

- **Act 1 — you supplied a content word.** Certain only if the sentence forces that
  exact word and no other content word would fit → apply it, and **label the edit
  `paraphrase`** in your report even if you would have called it a structural repair,
  so a reviewer knows a meaning-decision was made and checks it. If you are choosing
  among content words that would each make the sentence say something a little
  different, you are guessing → **hold**.
- **Act 2 — you resolved a sentence with more than one reading.** Here the certainty
  question is **already answered by the act itself.** The moment you grant that a
  second careful reader could reasonably take the sentence a different way, that *is*
  the uncertainty — you do **not** then get to pick the reading you prefer and call it
  certain. **A genuinely two-reading sentence is ALWAYS held**, with one candidate per
  reading proposed, exactly as move 5's hold path describes. The only path from act 2
  to "apply" is discovering, on a second look, that there is in truth **no** second
  plausible reading — i.e. the act never really fired.

**The incoherence to refuse:** saying a sentence *can be read two ways* and, in the
same breath, asserting one of them as *certain*. Those two statements cannot both be
true. If you can name a second reading, you are uncertain **by definition** — hold it,
and propose a candidate for each reading. Reserve "certain" for a sentence whose other
reading, on inspection, no careful reader would actually take. This is not a new gate
and not a licence to hold more freely on grammar — it is the existing gate refusing to
be fudged on the certainty call, just as it already refuses to be walked around by a
move label.

**Move 4 is therefore purely grammatical.** It rearranges, and supplies only what
grammar forces and content does not touch: split a run-on, reorder, supply a copula or
the forced agreement form, swap a wrong grammatical particle. The instant a repair
supplies *content* or *picks between readings*, it has left move 4 by definition and
is in the gate. This is **not** a new limit on what you may fix — you may still fix all
of it — it is a rule about which fixes must pass the meaning check *before* they are
applied.

The test for every repair, before you write it into the file: **"Am I only
rearranging his words and grammar, or am I deciding what he meant?"** The first you
apply. The second goes through the gate.

**The one hard line (FROZEN — this is what the whole stage exists to protect).** You
may fix broken structure, and — as a last resort (move 5) — reword a genuinely-broken
sentence; you may **never paraphrase working prose.** The line the fifth move does
**not** cross is the one that has held since the day this stage was built:

- swapping his words for synonyms or "nicer" phrasing when the original **already
  works** — restructuring a *broken* sentence is the job; re-wording a *sound* one
  is the sin;
- adding any fact, figure, example, claim, qualifier, or idea;
- changing what the sentence means — and, in a sentence that states a fact, altering,
  rounding, softening, strengthening, or dropping any part of what it asserts (see
  "Repairing a sentence that states a verified fact");
- smoothing or elevating the voice so it sounds less like him.

Ask it of **every** edit: **"Am I fixing a break, or rewriting a sentence that
already works?"** The first is your job — including the fifth move, which rewords a
*broken* sentence. The second is the failure this stage exists to prevent — and the
fourth and fifth moves' power is exactly why you must ask every time.

**The calibration.** The owner's instruction: "some of these, maybe in every 1-3
sentences, absolutely hinder understanding of the narrative." That is roughly how
often a genuine break occurs — and because the fourth move now lets you **repair**
most of what you detect, your repair rate rises to about that break rate, rather
than the near-zero it was under three moves alone. What has **not** changed is the
ceiling it implies: **you touch a sentence only when it is broken.** A
sentence already valid in written English — a contraction, an opening "And", a
deliberate minor sentence — you still leave exactly as it is. If you are editing sentences
that already read, you have crossed from repair into rephrasing. Most sentences you
read you will still leave untouched; you touch the broken ones.

**Detection stays uncapped.** This calibration is about the hand, not the eye. It
governs how often you *repair*; it puts no ceiling on how much you *detect and
consider*. Job 1 stays liberal.

**When even the fourth move cannot help — the fifth move takes over.** A
few sentences are so broken that making them read means **rewording**, not just
restructuring. Those go to the fifth move: if you are **certain** of the meaning,
reword the minimum and **apply** it (disclosed, labelled `paraphrase`); if you would
have to **guess** the meaning, **hold** the verbatim original and **propose** your
candidate paraphrase(s) in the flag list for a person. Either way the sentence is
never passed over in silence — and a held one never ships as a bare "broken" note; it
ships with a proposed fix a human can accept in seconds.

**The ways to fail.** The cardinal one is still **paraphrase in the wrong place** —
rewording a sentence that **already worked**, changing his words, meaning, or voice
where nothing was broken. The fifth move legitimises rewording a *broken* sentence;
it does **not** loosen this, so guard it harder now, not less. Two more: **reaching
for the fifth move when a lighter one would clear the break** — paraphrase is the last
resort, not a shortcut, and a break a cut or reorder could have fixed must never be
rewritten; and **timidity** — leaving a genuinely-broken sentence unhandled when you
could have fixed it (a minimal move) or proposed a fix (the fifth). A broken sentence
you could act on and didn't is a real defect. Fix the breaks with the lightest move
that works; leave the sound prose untouched; reword only the broken, only when
nothing lighter reaches it.

---

## Repairing a sentence that states a verified fact

Until now this stage treated every sentence in `claims.json` as frozen — untouchable,
flag-only. **That freeze is lifted for this stage:** a broken claim sentence is
repaired like any other, so a reader no longer meets a
verified fact stated in broken grammar. Read `claims.json` first anyway — not as a
wall now, but as a **map of the sentences that carry a verified assertion**, so you
know where the next rule applies with full force.

**The rule when you repair one: the assertion survives the repair exactly.** Your
four moves straighten the *grammar*; they must carry the sentence's fact, its
number, its subject, and everything it asserts through **unchanged** — not altered,
not softened, not strengthened, not dropped, nothing added. You are writing the same
claim down properly, not restating it. Concretely, on a broken claim sentence:

- ✅ split a run-on that buries the claim into two clean sentences, the claim intact
  in one of them;
- ✅ supply a dropped verb, straighten a non-native word order, fix an agreement
  slip, so the claim reads as written English;
- ❌ change, round, or re-express any figure or quantity;
- ❌ widen or narrow what is asserted ("most" ↔ "many", "is" ↔ "can be", a flat
  statement ↔ a hedged one);
- ❌ attach a qualifier, or drop one that was there.

If a broken claim needs **rewording** (no minimal move reaches it), the fifth move
governs — with the assertion held to the same exact standard. **Assertion certain →**
reword the minimum, keep every fact and number identical, **apply** it, disclose
before/after labelled `paraphrase`. **Assertion itself uncertain** — if you would have
to guess *what is being claimed* — **hold** the verbatim original and **propose** your
candidate(s) in the flag list. A claim whose assertion you would have to guess is
exactly the sentence you must never silently reword. Every claim repair is disclosed
before/after in your report (Section 5), which is where a reviewer confirms the
assertion came through unchanged.

**No gate runs after you — so your discipline is the whole safeguard.** In the
old order this stage ran before `claim_diff`, which would
flag a repaired claim as CHANGED for a person to confirm. This stage now runs
LAST, after the de-slop bracket: `claim_diff` has already run on
`verified.md -> final.md` and passed, and **nothing re-checks the edits you make
to `final.md`.** There is no automated catch for a claim whose meaning you
altered. That is deliberate — a `claim_diff` after this stage would diff against
a basis this stage exists to change, which is why it is deliberately omitted — but it
means the rule above (the assertion survives the repair *exactly*) and the
hold-and-flag path for any assertion you would have to guess are the *only* things
standing between a repaired claim and a silently changed fact. Treat them as such.

---

## Method

### 1. Establish the condition, then mark the stage

First determine whether this pass runs at all (see "When this pass runs"): read
the routed sources' roles. If the piece's content is not drawn from a `both`-role
source, mark the stage `skipped` with the reason and stop:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/manifest.py --run-dir <run> stage \
  --target g2-p3 --stage spoken_to_written --status skipped \
  --detail "content and voice are different inputs (content=found, voice=profile); spoken-to-written does not apply"
```

If the condition is met, mark it running:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/manifest.py --run-dir <run> stage \
  --target g2-p3 --stage spoken_to_written --status running
```

A stage starts at `pending` and the only legal moves are `running` and
`skipped`. The orchestrator closes a running stage — report your result and let
it set `passed`.

### 2. Read the inputs

```
<run>/pieces/g2-p3/final.md       the text you are working on (the finished de-slop output)
<run>/pieces/g2-p3/claims.json    the sentences that carry a verified assertion
```

`final.md` is the output of the whole de-slop bracket
(`anti-slop -> de-slop -> claim-diff`), which has already run and passed. You are
the LAST stage: you work on `final.md` and write `readable.md`. Read
`claims.json` first — not because those sentences are off-limits (they no longer
are), but so you know which ones carry a verified fact and must keep their
assertion exactly when you repair them (see "Repairing a sentence that states a
verified fact"). Those sentences are byte-identical in `final.md` (de-slop never
touched them and claim-diff confirmed it), so the map still holds.

### 3. Read the piece and detect every blockage (Job 1)

Really read it, sentence by sentence. For each one ask: **does this read as
broken or non-native as written — would a careful reader stumble, re-read, or
notice it is ungrammatical?** If yes, it is a blockage and it goes on your list —
**even if the meaning is perfectly clear from context.** Recoverable meaning is
not a pass. The bar is *broken as written*, not *incomprehensible*.

What you leave alone is only what is **already valid in written English**: a
contraction, a deliberate minor sentence (a short verbless or subjectless
construction that stands complete on its own), a sentence that opens with "And".
Those need no editor's hand. The test is one question: **would this construction
survive an editor of written English?** If yes, leave it. If no — a comma-splice or
run-on (even a rhythmic one), a calqued preposition, a wrong modal, a non-native
word order, a dropped verb, an agreement slip — it is broken *as written*, and you
**fix it**. Do not defer a spoken-grammar error to a human as "borderline": spoken
register that reads as an error on the page is not "casual", it is broken.
Casualness that is grammatical (the contraction, the opening "And") stays; grammar
that only worked aloud does not. This is still not "could this be smoother" — that
would edit the words, and word choice is not your subject — it is "would this
construction stand in edited written prose."

**One more boundary: this stage is about *grammar*, not *word choice* or
*meaning*.** Your subject is how the words fit together — agreement, order, a
missing or doubled element, a construction that does not parse. It is **not**
whether a **grammatically-correct** word is the one you would have picked, or
whether a phrase says something odd or self-contradictory. A sentence can be
perfectly grammatical and still use a strange word or state something that reads
oddly — that is a later stage's concern (de-slop / editorial), not yours.

**But do not let "not word choice" swallow a word that is itself grammatically
wrong.** A single word can *be* the broken grammar: a preposition the verb or
noun does not take in written English (a calqued preposition), a modal the
construction does not license, a quantifier or determiner whose number does not
match what it governs, an adjective standing where the noun form is required.
Swapping that one word is the **fourth move** — "swap the single wrong
grammatical word" — not word choice, and it is squarely in scope. The line is
**not** *how many words change* (a grammatical fix is very often one word) and
**not** *whether the sentence roughly parses* (a calque usually half-parses); it
is **whether an editor of written English would mark that word as an error, or
only as a nicer-word note.** An error is yours to fix; a nicer-word note is not.
So: leave a grammatically-correct word you merely dislike; **fix a word that is
grammatically wrong, even when the fix is a single word, and never defer it as
"just word choice."** Flag a broken *structure* — including a structure broken at
a single word; never flag a sound word you merely dislike or a meaning you
merely find strange.

The blockages recur in a handful of **shapes**. They are named here as shapes on
purpose: **there are no example sentences anywhere in this skill, and that is
deliberate.** Examples would teach you *these* pieces, and the tool has to work on
a stranger's transcript it has never seen — a transcript whose blockages will look
nothing like any example anyone could have written for you. Recognise the shape,
never a sentence you were shown. For each shape, the move that repairs it (every
shape is repaired **wherever it sits — claim or not**; inside a claim the assertion
must survive the repair exactly):

- **Stray or doubled word** — a word left in from a false start, or a small word
  accidentally repeated. One **cut** clears it → **repair.**
- **Obstructing filler** — filler that blocks the clause, as distinct from filler
  that *colours* the voice (that stays). One **cut** clears it → **repair**, but
  only when removal takes nothing of the meaning or the voice with it.
- **Missing joint** — two spoken fragments butted together with no connective, so
  the seam jars. One **conjunction** clears it → **repair.**
- **Fragment out of written order** — a phrase in spoken sequence (an object or a
  result stated before the frame that governs it) that a reader has to re-sort. A
  **reorder** clears it if the words come back unchanged; if a plain reorder will
  not do it, the **fourth move** restructures it into written order → **repair.**
- **Dropped subject or verb** — a clause whose subject or main verb never arrives,
  so it reads as a fragment. The **fourth move** supplies the minimal missing
  subject or verb (a copula, the dropped verb) → **repair.**
- **Collapsed syntax** — a construction that begins in one grammatical shape and
  resolves into another, so it does not parse as written. The **fourth move** gives
  the stranded subject its predicate, or splits the run at its break → **repair.**
- **Broken idiom or collocation** — a fixed phrase rendered slightly wrong, or a
  verb paired with the wrong or an extra particle. A **cut** of the stray particle,
  or the **fourth move** swapping the one wrong word for the right one → **repair.**
- **Agreement or number slip** — a quantifier, determiner, or verb that does not
  agree with what it governs. The **fourth move** fixes the one word → **repair.**
- **Non-native word order (calque)** — a clause built on another language's word
  order or idiom, so it parses as translated rather than native. A **reorder** if
  the same words serve; otherwise the **fourth move**'s minimal restructure into
  written order → **repair.**
- **Person or tense shift** — a sentence that starts in one person or tense (you,
  present) and switches part-way (to I, or to past) with no reason. The **fourth
  move** swaps the one pronoun or verb form to match → **repair.**

One kind of break the four minimal moves cannot reach: one so broken that making it
read means **rewording**. That is the **fifth move's** job now, not a dead end —
reword-and-**apply** when you are certain of the meaning, or **hold-and-propose** a
candidate when you would have to guess it (the fifth move above). It is never a bare flag
with no proposed fix, and never a silent pass, wherever it sits — claim or not.

**Work the piece one paragraph at a time — do not read it once and summarise.**
A single top-to-bottom read is why blockages get missed: read straight through and
the eye smooths over exactly the broken grammar you are here to catch, and the
same piece read twice this way turns up a different, incomplete list each time. So
do not read the whole piece and then write your list. Take it **one paragraph at a
time**, and for each paragraph in turn:

1. Read the paragraph slowly, sentence by sentence.
2. Pass it against **every shape in the list above, one shape at a time** —
   deliberately, as a checklist, not as a single impression. Ask of the
   paragraph: any dropped subject or verb? any doubled or stray word? any
   obstructing filler? any missing joint? any fragment out of written order? any
   collapsed syntax? any broken idiom or collocation? any agreement or number
   slip? any non-native word order? any person or tense shift? A shape you do not
   consciously check for is a shape you will skim past.
3. Record every blockage that paragraph holds — the ones you repair (with the
   move) and the ones you leave (with the reason) — **before** you move on. Then,
   and only then, go to the next paragraph.

Finish the whole piece paragraph by paragraph before you write your report. Give
every paragraph its own deliberate pass; do not let a strong later paragraph make
you skim an earlier one. That running list *is* your Section 5 report — **a
blockage you notice but do not write down is a blockage that vanishes.** Because
the four moves now reach most breaks, expect to *repair* about as often as a
genuine break occurs — roughly the owner's "every one to three sentences" — not the
near-zero of three moves alone.

For each blockage, apply the **lightest** move that clears it — cut, then reorder,
then conjunction, then the fourth move's minimal repair, and only when none of those
reach it, the **fifth move** (reword: **apply** if the meaning is certain, else
**hold and propose** a candidate). Never more than the break requires. When the
blockage sits in a claim sentence, keep its assertion exact (above). Nothing is left
as a bare flag: a held sentence always carries its candidate paraphrase(s).

### 3b. A light de-slop pass — the main slop culprits only

This stage runs **after** de-slop in the pipeline, and there is no de-slop step
after it. So it takes on a **small, bounded** slice of
de-slop's job: strip the clearest machine-tells the **produce** step may have
introduced — never the owner's spoken voice, which is the asset and stays. This is
**not** the full de-slop discipline, only the handful of culprits that most give a
piece away as generated.

**Whose words this touches.** The content and voice are the owner's transcript, but an
AI wrote the draft *from* it — and the framing tells below are the **generator's**
words, not the owner's. Those are safe to cut. When you genuinely cannot tell whether
a phrasing is the owner's voice or the generator's, **leave it** — the same
conservatism as everywhere in this skill. A claim sentence's wording is never touched
for slop.

**The culprits to cut (and nothing beyond these):**

- **Hollow opener** — "In today's fast-paced landscape", "It's no secret that",
  "Let's dive in". Cut it; start at the first sentence that actually says something.
- **Announced structure** — "In this article we'll explore…", "Let's break this
  down". Cut the announcement; keep the thing it announced.
- **Empty intensifier** — crucial, vital, essential, seamless, robust, game-changing —
  where nothing is being measured. Cut the word, or the sentence if the word was all
  it had.
- **Hedge stack** — "may potentially help to somewhat improve". Keep the one word that
  carries the meaning; cut the pile.
- **Reflexive rule-of-three** — a three-item list or three-beat cadence where the
  content had two things and a third was added for rhythm. Cut the padding item.
- **Summarising close** — a final sentence or paragraph that restates what was already
  said and adds nothing. Cut it.
- **Foreign punctuation the generator reaches for** — em-dashes (`—`) and en-dashes
  (`–`), which a model inserts by default when it splits a clause or wraps an aside.
  Replace with the plain-prose equivalent: a full stop, a comma, an "and", or a colon.
  This mirrors the deterministic `antislop.py` 4.1 ban (cap 0 on generated output),
  which runs in the anti-slop stage upstream of you. That stage cleaned `final.md`,
  but your own edits can reintroduce a dash when you split a clause, and nothing
  runs after you to catch it — so the suppression has to live here too.

**How to cut, minimally.** Prefer a clean cut. Where a cut leaves a stub, a light
plain-prose mend of the **generator's** phrasing is fine — you are tidying the
generator's words, not rewording the owner's. Do **not** rewrite the owner's verbatim
spoken phrasing for slop, do **not** add anything, and the meaning-gate and assertion
rules above still bind: a slop-cut that would supply content or pick a reading goes
through the gate like any other edit.

This pass is **light on purpose.** A piece built from a transcript often has little or
none of this — if so, do nothing and say so. Do not hunt for slop that is not there;
a forced de-slop is its own kind of damage.

### 4. Write the output

Write the result to:

```
<run>/pieces/g2-p3/readable.md
```

`readable.md` is `final.md` with your in-remit edits applied — the grammar edits
(cuts, reorders, joins, fourth-move structural repairs, any reword you were certain
enough to apply) **and the light de-slop cuts from step 3b** — and nothing else. A
repaired claim appears with its grammar fixed and its assertion unchanged. A sentence
you **held** (meaning uncertain) stays **byte-identical** to `final.md` — its
candidate paraphrase lives only in your report, never in the file. Every sentence you
did not touch is byte-identical too. Write it even if you changed nothing (a piece can
be clean) — this file is what the piece **ships**, and a stage that can silently
produce no file is the disease this pipeline exists to treat. If you made zero edits,
`readable.md` is a verbatim copy of `final.md`, and you say so in your report. When
the orchestrator closes this stage it records `readable.md`'s hash and delivery
re-checks it, so the file you write here is exactly the file that ships.

### 5. Report — every edit disclosed, every held sentence proposed

Give the orchestrator:

- The condition decision: did this pass run, and on what basis (the source role).
- **Exact counts:** how many sentences the piece has, how many grammar blockages you
  **detected**, how many you **applied a fix to**, how many you **held** (meaning
  uncertain), and — separately — how many **de-slop cuts** you made in step 3b.
  "9 detected, 7 applied, 2 held, 3 de-slop cuts" is a result; "cleaned it up" is not.
- **Every applied edit, quoted before and after**, and which move it was — **cut /
  reorder / conjunction / structural repair (fourth move) / paraphrase (meaning-
  decision, applied) / de-slop cut.** Label an edit `paraphrase` whenever it was a
  meaning-decision — you supplied a content word or picked between readings —
  **regardless of whether you would have called it move 4 or move 5**; that label is
  how a reviewer knows to check the meaning survived. For any structural, paraphrase,
  or de-slop edit this before/after is not optional bookkeeping, it is the safeguard.
  Quote enough that the difference is visible, side by side.
- **The hold-and-propose list — every sentence you held (fifth move, meaning
  uncertain), quoted, each WITH your candidate paraphrase(s)** — one per plausible
  reading when it is genuinely ambiguous — **and one line on what you would have had
  to guess.** This is a **required** output. A held sentence with a proposed fix is
  fine; a held sentence with *no* candidate, or a break left *unnamed*, is the failure
  this list prevents.
- **For every claim sentence you repaired: the before/after, and one line stating
  the assertion is unchanged** — the fact, the number, and what is asserted are the
  same in both. This is how a reviewer confirms the repair straightened grammar and
  did not touch meaning.
- Confirmation that `readable.md` differs from `final.md` by **only** the edits
  you listed (grammar edits + de-slop cuts).
- The output path.

**An honest "I detected nine grammar blockages, applied eight fixes (three structural,
one reword I was certain of and labelled `paraphrase` — each quoted), held one for a
human with two candidate readings attached, and made two de-slop cuts" is a correct
report — provided the edits are quoted and the held sentence carries its
candidate(s).** The failure this report guards against is a meaning-decision dressed
up as minimal repair: an edit that quietly rewrote a *sound* sentence, picked a
reading, changed the meaning, or smoothed the voice. Quoting every applied edit is how
that gets caught, and labelling every meaning-decision `paraphrase` — whatever move
number you'd have called it — is how a reviewer knows where to look hardest.
Under-editing is no longer the safe failure it once was; a meaning-decision applied
where it should have been held is the one to fear now.

---

## Wiring — how this stage sits in the pipeline

The stage is wired into `PIECE_STAGES` as the **last** piece stage:

1. **Placement — last, after the whole de-slop bracket.** The per-piece order is
   `produce -> length -> overlap -> fact_check -> anti_slop -> de_slop -> claim_diff -> spoken_to_written`.
   This pass runs **after `claim_diff`**, i.e. after the entire de-slop agent
   bracket. The reasoning: `claim_diff` and `anti_slop` are tied to `de_slop` as
   one agent, the way `length` and `overlap` are tied to `produce`, so the spoken
   pass belongs after all of them, not in their middle. This is why step 3b (the
   light de-slop pass) exists — there is no de-slop step after this stage, so it
   must suppress the handful of machine-tells its own edits could introduce.

2. **Input and output.** This stage reads `final.md` — the finished output of the
   de-slop bracket — and writes `readable.md`. There is no downstream consumer:
   `readable.md` is the file the piece **ships** (for a `g1` piece, where this stage
   is skipped, `final.md` ships unchanged). `anti_slop` and `de_slop` always read
   `verified.md`; `claim_diff`'s `--before` is `verified.md` and its `--after` is
   `final.md` — none of them see `readable.md`, because they all run before this
   stage. `readable.md`'s integrity is held by its own anti-swap hash: recorded when
   the stage closes (`--artifact pieces/<piece>/readable.md`) and re-checked at
   delivery. That is **not** a quality gate — it only proves the shipped bytes are
   the bytes this stage wrote.

3. **The claim freeze — lifted for this stage only.** The claim freeze is opened
   here, and here alone. Every sentence in `claims.json` was previously
   frozen (flag-only); now a broken claim is repaired like any other, under the
   assertion rule in "Repairing a sentence that states a verified fact" — grammar
   straightened, the fact/number/assertion carried through exactly, every repair
   disclosed. Because this stage runs last, **no `claim_diff` re-checks these
   repairs** (see that section): the assertion rule and the hold-and-flag path are
   the only guards, and the disclosure in your report is the review surface.

**The fourth move — why it exists.** The three minimal moves (cut, reorder,
join) leave a large residue of genuinely-broken sentences — dropped verbs,
person shifts, broken idioms, collapsed spoken structure — that none of them can
repair. In testing, most flagged breaks were of exactly this kind. The fourth
move exists to reach them, in its **fuller, structural** form: structural
changes are not just accepted but wanted, because the structure that works in
free speech does not work in writing.

So the fourth move stands in the "What you may do" section as a full move:
minimally repair a genuinely-broken sentence's structure into written
form — splitting run-ons, supplying a missing verb, giving a stranded subject its
predicate, swapping a wrong grammatical word, straightening spoken word-order —
keeping his words, meaning, and voice (and, in a claim, its assertion exactly),
every instance disclosed.

**The fifth move — why it exists.** The residue the fourth move cannot reach — a
sentence so broken that making it read means *rewording*, not just
restructuring — used to be flag-only: named in a report, but shipped verbatim
and broken, with the actual paraphrasing left to whoever read the flag. That is
worse than useless — it just introduces extra noise, and the agent that read the
sentence and holds the context is the cheapest place to draft the fix. So the
fifth move **rewords the broken residue**, split by meaning-certainty: where the
meaning is certain, it rewords the minimum and **applies** it, disclosed and labelled
`paraphrase`; where the meaning would have to be *guessed* between readings, it
**holds** the verbatim original and **proposes** candidate paraphrase(s) — one per
reading — in the report for a person to pick. Rewording a sentence that already works
remains the cardinal sin; the fifth move touches only the genuinely broken, and only
when no lighter move reaches it.

**Why this is safe — and what changed now that no gate follows.** This stage
runs LAST, so the deterministic gates that once sat downstream of it —
`claim_diff` and the `overlap` floor — now run *before* it, on `final.md`. They
do **not** re-measure your `readable.md`. Two of the old nets are therefore gone,
by deliberate design: there are no quality gates after this stage. What remains
is discipline and disclosure, and they now carry the whole load:

(1) **The cardinal line holds for every sentence** — rewording a sentence that
*already works* is still forbidden; the fourth and fifth moves touch only the
genuinely broken, only after the lighter moves fail, and never add or alter what is
asserted.

(2) **The meaning-certainty gate keeps the riskiest cases out of the shipped file
entirely:** where meaning would have to be guessed, the sentence is **held verbatim**
and only a *proposal* reaches a human — the piece never ships a guessed meaning.

(3) **Every applied edit is disclosed** before/after (fifth-move ones labelled
`paraphrase`) and every held sentence carries its candidate, so both overreach and
silent omission are auditable by the person who reads your report. Because no
automated gate re-checks `readable.md`, that report **is** the review surface —
under-disclosure here is not a paperwork miss, it is the failure mode.

The three light moves remain FROZEN; the fourth and fifth moves and the cardinal
line (never reword working prose, never add or alter meaning, preserve voice) are
FROZEN too. `readable.md` itself is protected only against *tampering after you write
it* — its hash is recorded when the stage closes and re-checked at delivery (an
anti-swap anchor, not a quality gate).
