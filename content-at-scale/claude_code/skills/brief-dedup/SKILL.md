---
name: brief-dedup
description: Run after all producing briefs for a group are assembled, before any writing agent is dispatched. Checks that no two pieces share verbatim source material and that no two are set up to become near-duplicate wholes. Re-angles any real collision by rewriting planning fields only, never verbatim passages. The only pre-writing point in the pipeline where cross-piece coordination can happen.
---

# brief-dedup

You are a cross-piece planning check. You read every piece's brief and routed material in a group, decide whether any two pieces are set up to collide in the reader's hands, and re-angle any real collision before the writing agents run.

**Read `${CLAUDE_PLUGIN_ROOT}/REFERENCE.md` first** for scope strings, script syntax and exit codes.

---

## What this skill is and when it runs

Writing agents in this pipeline are deliberately blind to each other: each agent writes exactly one piece and never reads the others. That isolation is by design -- a piece must be completable as a self-contained task. But isolation means nothing downstream can notice when two pieces are covering the same ground before writing has started.

The group review (stage `group_review`) catches duplication that surfaces in finished drafts. This skill is the earlier, cheaper layer: it prevents the collision at planning time, before any writing runs.

**Run once per group, after all producing briefs for the group are assembled and on disk, and before any writing agent for that group is dispatched.** The moment the first piece goes to a writer, the window for coordination closes.

---

## The distinction it polices

This is the hardest part to get right. Read this section in full before comparing anything across pieces.

| What recurs across pieces in a group | Verdict | Why |
|---|---|---|
| The same **idea or subnarrative**, expressed in a **different form**, drawn from **different verbatim source material** | DESIRABLE -- never flag it | Good unified company messaging returns to the same core ideas again and again in different forms. This is a feature of the voice, not a defect in the routing. |
| The **same verbatim source passage** used to build two different pieces | DEFECT -- catch it | The reader sees the same actual words twice. This is literal duplication regardless of how different the surrounding argument looks. |
| Two pieces that are **near-duplicate wholes** -- same thesis direction AND substantially their whole supporting-beat structure shared, whether or not the source material is the same | DEFECT -- catch it | This is the "almost the same blog post" failure. Two pieces with different headlines still read as one essay told twice if they make the same argument across their whole structure. Whether the source is shared is a separate matter: same-verbatim-source is the row above and is caught independently; THIS row fires on whole-piece sameness even when each piece was built from different material. |

### The false-positive you must never commit

**Do not flag two pieces simply because they both touch the same idea.**

The recurrence unit that causes trouble is not the main thesis -- main theses are usually already distinct -- and it is not the presence of shared themes. The defect lives in the combination of: the same verbatim source being used for the same beat, AND/OR enough being shared across the whole that the two pieces become near-duplicate wholes.

**Worked example of desirable recurrence:** Piece 1 thesis is "use deterministic code for anything reliable." Piece 3 thesis is "discipline catches unreliable output." Both pieces independently develop the supporting beat "LLM hallucination is unavoidable because of how next-token prediction works." If piece 1 develops that beat from its own routed passage and piece 3 develops it from a different routed passage, they are NOT a defect. They are two distinct treatments of a shared sub-theme, drawn from different material. This is the unified messaging this pipeline is designed to produce.

That same example becomes a defect if both pieces are routed the same verbatim passage as the basis for that beat -- because then the reader sees the same words twice, and the beat cannot be expressed distinctly.

**A check that fires on same-idea-different-form-different-source is broken.** It would be attacking the unified messaging this pipeline exists to produce. If your check would fire on the desirable example above, revise your reasoning before proceeding.

**Second worked example -- same domain, same sub-themes, DIFFERENT argumentative moves (also clean).** Pieces in one group are topically related by design; they will always share a domain. Piece A argues "do not let AI run loose, steer it step by step or errors compound" (a prescriptive, how-to-work-with-it move). Piece B argues "confident AI output is dangerous because confidence is not correctness" (a diagnostic, what-goes-wrong move). Both touch hallucination and the danger of unmanaged AI, from different passages. This is CLEAN: same domain, even shared sub-themes, but each beat makes a different argumentative move and the theses make different core claims. The trap is pattern-matching on topic overlap -- "both are about AI being unreliable" -- and flagging it. Two pieces sharing a domain is the normal, intended state of a group, not a defect.

**Do not turn this example into a bypass.** "Different argumentative moves" means the two pieces genuinely leave the reader with different arguments -- not the same argument in different clothing. A point framed one way in piece A ("here is what goes wrong when you skip oversight") and the mirror way in piece B ("here is what to do: keep oversight at every step") is the SAME argument if it rests on the same beats and leaves the reader with the same takeaway. Diagnostic-versus-prescriptive framing of the same specific argument -- the same claim about what is true or what should be done -- is a near-duplicate, not two different moves. The deciding rule is always the reader test in Step 4: if a reader of both comes away having read the same argument twice, it is a defect no matter how differently each piece is framed. Use this example to avoid flagging genuinely different arguments that happen to share a domain -- never to wave through the same argument wearing two coats.

---

## Two jobs

**Job 1 -- cross-piece verification.** Read every piece's brief and its routed verbatim material. Confirm: (a) no two pieces have been routed the same verbatim source passage, and (b) no two pieces are set up to become near-duplicate wholes. This is an independent check: the upstream routing stage (source-intake) already intends to assign each piece distinct, non-overlapping verbatim passages, but nothing else verifies across pieces that this actually held. Without this check, a group whose pieces were all built from an effectively identical source pile would reach the writers uncaught. You are that catch.

**Job 2 -- differentiate a real collision.** When Job 1 finds a defect, assign each affected piece a distinct angle and a supporting-beat allocation. Write those decisions into the planning fields of the brief. Add a sibling-constraint line to each brief so the writer knows what the other piece covers. You never rewrite the routed verbatim passages. You touch only the planning fields.

---

## Method: how to run the check

### Step 0 -- mark the stage running before you read anything

Do this FIRST, before Step 1, so the manifest records the check as in progress for the whole time you work -- this is the lock that stops any writing agent for the group being dispatched while the check is open. (The `passed` and `flagged` commands come at the end; see "Marking the manifest stage" for all three.)

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/manifest.py --run-dir <run> stage \
  --target <group-id> --stage brief_dedup --status running
```

### Step 1 -- read every brief and its routed material

For each piece in the group (for example: g2-p1, g2-p2, g2-p3):

Read the producing brief from disk:
```
<run>/pieces/<piece-id>/producing_brief.md
```

Read the piece's routed verbatim material:
```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/context.py --run-dir <run> read --scope <piece-id>
```

This returns every context entry assigned to this piece at its own scope, including the assertion text, the verbatim quote (the `--quote-text` or `--quote-from` content stored by source-intake), and the source citation.

**How to read that output: section headers are scope delimiters.** The output groups entries under Markdown headings named for their scope -- `## <group-id>` (e.g. `## g2`) for the shared group-scope pool, and `## <piece-id>` (e.g. `## g2-p1`) for that piece's own-scope material. If the output for a piece contains NO heading matching its own piece-id, that piece has zero entries at its own scope -- this is a normal, valid state (the pure-research group below), not a command error or a missing file. Do not re-run or hunt for the "missing" section; its absence IS the answer.

**What the writer actually reads is the routed material -- and runs keep it in one of two places. Check which shape you have before comparing anything.**

- **Embedded briefs:** the producing brief pastes its routed-source section directly into itself -- a block of `**[speaker]` transcript lines or routed passages. Then that embedded block is what the writer reads and the authoritative thing to compare in Step 3. The `context.py` query above is a cross-check: if the brief's embedded block and the database scope disagree, the assembly and the routing have come apart, and that disagreement is itself a defect (a real failure mode: the routing records one thing while the assembled briefs carry an identical pooled block regardless).
- **Runtime-fetched briefs:** the brief embeds NO routed block and instead tells the writer to run `context.py --scope <piece-id>` at write time. Then the `context.py` output IS what the writer reads and IS the authoritative material for Step 3. There is no embedded block to disagree with, so the assembly-vs-database check does not apply. Do NOT conclude "no embedded block, so nothing to compare" -- the routed material is the `context.py` output; compare that.
- **Pure-research group (a runtime-fetched sub-case):** some groups route NO own-scope verbatim passages to any piece -- `context.py --scope <piece-id>` returns only group-scope `[FOUND]` research findings and zero `[SOURCED]` entries for every piece. This is a VALID, EXPECTED state, not an error and not a routing failure: a research-only group is built entirely from a shared fact pool by design. When you see it, do not hunt for missing piece-scope material or treat the emptiness as a defect. Step 3 then has nothing to compare (see Step 3's automatic-clean rule), so the whole check rests on Step 4.

Whichever shape the run is, the principle is identical: compare the routed material the writer will actually receive.

### Step 2 -- enumerate supporting beats per piece

For each piece, record two things:

1. **Main thesis** -- from the brief's thesis field. Brief formats vary in field names: it may be a "Topic / thesis" line, or a "Topic:" line paired with a keyword brief, or similar. Take the piece's core claim from whatever field carries it; do not assume one exact field name.
2. **Supporting beats** -- the subsidiary claims or sub-narratives the piece is set up to develop. Derive these from whatever the brief actually provides, in this order of preference: (a) an explicit argument field if the brief has one (named e.g. "The argument to make" -- not every brief format includes it, so do not stall if it is absent); (b) the routed verbatim material, where each routed passage carries a point the piece is intended to develop; (c) when the piece has NO own-scope routed material (the pure-research group in Step 1), the brief's topic, angle and keyword/heading structure -- the beats are the sub-topics the piece will cover. List each beat as a plain sentence describing what argumentative move the piece will make. A piece with no piece-scope material still has beats; derive them from its topic structure rather than concluding there is nothing to enumerate.

This beat list is your working record for steps 3 and 4. Write it out explicitly -- do not hold it in inference.

### Step 3 -- check for verbatim source overlap across pieces

Compare the routed material each piece's writer will actually read against every other piece's (see Step 1 for where this run keeps it -- an embedded block, or the `context.py --scope` output). Look for the same passage appearing in two or more pieces, regardless of whether the source citation matches.

**"The same passage" includes partial and nested overlap, not only an exact match.** If one piece holds a line range and another holds a range that contains or overlaps it -- for example `#L38` in one piece and `#L34-L38` in another -- the shared span is a collision: the reader would meet the same words in both. Judge by the actual overlapping text, not by whether the citation strings are identical. A larger range that wholly contains a smaller one is the same passage for this purpose.

**Compare piece-scope material only; group-scope material is shared by design.** The routed material a run gives a piece usually has two parts, and `context.py` output labels them by scope heading:
- Material under the piece's OWN scope (e.g. `## g2-p1`) -- typically the owner's `[SOURCED]` verbatim voice passages routed specifically to that piece. THIS is what Step 3 compares across pieces.
- Material under the GROUP scope (e.g. `## g2`) -- typically `[FOUND]` research findings made available to every piece in the group ON PURPOSE, a shared fact pool. Two pieces both drawing on it is NOT a defect and must never be flagged. (Research findings are also for accuracy only, not quoted verbatim into the output, so they cannot duplicate the reader's words.)

So the defect is one piece's OWN-scope routed passage appearing verbatim under ANOTHER piece's OWN scope. A finding sitting in the shared group pool is never the defect.

**If no piece in the group has any own-scope material, Step 3 is automatically clean.** With zero `[SOURCED]` passages anywhere (the pure-research group from Step 1), there is nothing that could appear verbatim in two pieces' own scopes. The shared group-scope `[FOUND]` pool being identical across pieces is the intended design, never a many-way collision -- do not flag it. Record Step 3 clean and move to Step 4, which is the whole check for such a group.

**What counts as a passage (the floor that prevents a false positive).** The test is SUBSTANCE, not length. A passage is a run of source material that carries a piece's actual content -- a substantive claim, a specific point, an argument the writer builds on. Word count is only a rough guide: substantive shared runs are usually 20 or more words, but a shorter one can still be a passage, and a longer one may not be. Judge the two boundaries by substance, not by the number:
- **Under the rough count but still a passage (fire):** a complete substantive claim is a passage even at 12-19 words. Example: "hallucination is not a bug, it is a mathematical property of the architecture" -- short, but a complete point the piece is built on. The same substantive claim appearing verbatim in two briefs is a defect.
- **Over the rough count but still NOT a passage (do not fire):** conversational filler or a stock phrase that recurs naturally is not a passage even at 20-40 words. Example: "you still have to put the work in, you can just get a much larger volume done, but you still have to put the work in" -- long, but filler, not substantive content. Two briefs sharing this is not a defect.

**When the count and the substance disagree, substance wins.** If you would honestly describe the shared run as conversational filler or a stock phrase repeated naturally across sessions, do not flag it regardless of length. If it is a substantive point the piece is built on, flag it even if short. Transcript-heavy material repeats short phrases for legitimate reasons; firing on those is exactly the false positive this skill must avoid. The defect is a substantial shared point, not a shared fragment and not a shared bit of filler.

A defect is present when the same substantial passage appears in two pieces' briefs. Record which passage, which pieces, and which source citation it belongs to. (A real collision of this kind can run to thousands of words carried verbatim across every brief in the group -- unmistakable, and nowhere near the fragment floor.)

**This step is purely about the routed material.** Do not compare theses or supporting beats here.

### Step 4 -- check for near-duplicate wholes

Using the beat lists from Step 2, compare across pieces. This step is about WHOLE-PIECE sameness, and it is independent of the source check in Step 3 -- it fires even when the two pieces were built from entirely different material.

A pair of pieces is a **near-duplicate whole** when both hold together:
1. Their main theses point in the same direction -- not merely touching the same topic, but making the same core argumentative move.
2. They share substantially their WHOLE supporting-beat structure -- not one beat or a few, but most of what each piece is built on. A reader of both would come away feeling they read the same argument twice.

Do NOT require the shared beats to come from the same source. Source-sharing is Step 3's job and is caught there. Step 4 exists precisely to catch the case Step 3 cannot: two pieces that are the same essay end to end even though each drew on different verbatim material.

**The false-positive line, restated where it bites hardest:** a SINGLE shared beat, or a handful, expressed from different material, is NOT a near-duplicate whole. That is the desirable recurrence from the table -- unified messaging returning to a core idea in different forms. The threshold for this defect is substantially the ENTIRE piece coinciding, not the mere presence of overlap. When you are torn between "they share a theme" and "they are the same essay," it is not a near-duplicate whole.

**There is no beat-count formula, on purpose -- the deciding rule is the reader test.** Do not reach for a fraction like "more than half the beats." Beats do not divide cleanly, and a number invites false precision on what is genuinely a judgment. Decide it by asking concretely: would a reader who read BOTH finished pieces come away feeling they had read the same argument twice? If yes, it is a near-duplicate whole. If they would come away with two distinct takeaways -- even ones that touch the same themes -- it is not. **When the reader test is genuinely balanced, default to CLEAN.** A wrongly-flagged pair costs the owner their deliberate unified messaging, which is the more damaging error here.

**Borderline worked example -- structural overlap alone is not enough.** Piece A: "AI saves time, for anyone willing to invest structure upfront." Piece B: "AI does not save time; staying critical costs exactly what you hoped to save." These two can share most of their supporting terrain -- both walk through where the human effort actually goes, both discuss reviewing model output, both draw on the same kind of examples -- and still be CLEAN, because their theses point in OPPOSITE directions and a reader comes away with two genuinely different arguments. Structural overlap by itself never makes a near-duplicate whole: Criterion 1 (same thesis direction) must hold too, and the reader test must confirm the sameness. When the theses genuinely diverge, stop; it is clean.

### Step 5 -- if clean, confirm and stop

If Step 3 finds no verbatim source overlap and Step 4 finds no near-duplicate wholes: the group is clean. Write the outcome into the manifest stage detail and mark the stage passed. Stop.

### Step 6 -- if a defect, differentiate (Job 2)

**Verbatim source defect (same passage routed to two OR MORE pieces):**

A passage can collide across two pieces or across several; handle any number the same way. Assign the shared passage to exactly ONE piece -- the one whose thesis it most naturally anchors. (If it anchors more than one equally, give it to the piece that has the least other distinctive material, so the passage does the most work where it is scarcest.) For EVERY other piece that held the passage, remove it and examine what routed material remains. If a piece can still support its thesis from its remaining material, re-angle it within that material -- and prefer a different passage the piece already holds that makes the same point in its own words (this is common: a foundational idea often recurs across the source, so each piece can carry it from its own distinct passage, which is the desirable form of recurrence). If a piece genuinely cannot support its thesis from what remains, log a re-route request for it (see "The re-route rule").

When three or more pieces shared one passage, you assign once and then re-angle-or-re-route each of the others one at a time. After doing so, re-check that no two of the re-angled pieces now lean on the same remaining passage -- resolving one collision must not create another.

**A single large passage can carry several distinct beats -- it still cannot be split across pieces.** A long transcript block often contains multiple argumentative points, and each colliding piece may have been leaning on a different point within the same block. You still assign the WHOLE passage to exactly one piece (the thesis it most naturally anchors), and every other piece loses the entire block -- not merely "its" beat -- because leaving the same verbatim block in two briefs is the very defect, no matter which sentence each piece meant to use from it. Splitting one verbatim block into "p1 uses these lines, p2 uses those lines" does NOT resolve the collision if the ranges still overlap; the reader still meets shared words. The other pieces then carry their beat from different material they already hold (a foundational idea usually recurs elsewhere in the source in the piece's own words -- the desirable form of recurrence) or, failing that, log a re-route.

**Near-duplicate-whole defect:**

Assign each piece a distinct angle. An angle is a specific facet of the shared topic that the piece foregrounds -- different enough from the other piece that a reader encountering both perceives two distinct arguments rather than one argument told twice.

Then allocate the supporting beats: decide which beats belong to which piece, so neither piece is developing beats the other has claimed. The beat allocation follows the routed material: assign each beat to the piece whose routed passages most naturally anchor it.

Write these decisions into the planning fields (see "What to write back into the briefs" below).

---

## What to write back into the briefs

You write into these fields and no others:

- **Topic / thesis** -- update to reflect the assigned angle if the collision changed it. Preserve the original: write the revised thesis, and keep the original on a line beneath it marked `[original, superseded by brief-dedup]`, so the change is visible to the orchestrator's review rather than silently overwritten.
- **The argument to make** -- update to reflect the beat allocation and any beats the piece must not develop.
- **Supporting-beat plan** (a new section, added to the brief when this skill runs) -- lists the specific beats this piece owns, and explicitly names any beat it must not develop because the sibling claims it.

Add this block to each affected brief's planning section, before the producing agent reads it:

```
## Sibling constraint [added by brief-dedup]
This piece covers: [angle and beats assigned to this piece].
The sibling piece in this group covers: [angle and beats assigned to the sibling].
Do not develop [specific beat or sub-narrative] -- that is the sibling's ground.
```

Do not add invented content. Do not rewrite, summarise, or paraphrase any routed verbatim passage. Touch only the fields above.

For pieces where no collision was found, write nothing. A brief that passed the check is unchanged.

---

## The re-route rule

**Default: re-angle within the material this piece already has.** Use the existing routed passages to support a distinct angle. Fall back to requesting more material from source-intake only when a piece genuinely cannot support any distinct angle from what it holds -- when the remaining material after a beat allocation is insufficient to anchor a coherent piece.

**When re-routing is needed, log it -- never silently.** Write explicitly in your return: which piece needs more material, what angle it was assigned, and why the existing material cannot support that angle. The orchestrator takes this back to source-intake; you do not resolve it yourself.

**When re-routing, prioritise material from the same original document unit** that the piece's main thesis came from. For transcript-based runs, that means the same transcript. A unified narrative stays consistent in voice and context when its supporting material comes from the same source-home. Good messaging returns to the same well in different forms; a piece that draws from the same session as its thesis is coherent in a way that a piece assembled from unrelated sessions is not.

---

## The never-paraphrase guard

**This skill never causes source material to be paraphrased.** The routed verbatim passages carry the author's real phrasing and voice. The overlap floor gate measures that the finished piece retains enough of that actual wording. If paraphrase creeps in at the planning stage -- if an angle is chosen that only works by smoothing the routed words into something else -- the overlap floor will fail the piece.

Differentiation happens at the level of thesis, angle, and beat allocation only. You are deciding which ideas go where, not rewriting what any idea sounds like. The verbatim passages stay exactly as source-intake stored them.

If a differentiation you are considering would require a writer to paraphrase a routed verbatim passage to make it fit the new angle, that differentiation is wrong. Find an angle that the existing material can support as it is, or log a re-route request.

**The two-part success test for any differentiation you make:**
1. The pieces come out distinct -- a reader encountering both perceives two different arguments.
2. Every piece still passes its overlap floor -- the verbatim material survived into the draft, because the planning decisions you made did not require rewriting it.

A differentiation that satisfies only criterion 1 is not a success.

---

## Marking the manifest stage

Mark the stage running before you read anything -- this is Step 0 of the Method above, done first, not here at the end:
```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/manifest.py --run-dir <run> stage \
  --target <group-id> --stage brief_dedup --status running
```

Mark it passed when the check is clean or all collisions are resolved and briefs are updated:
```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/manifest.py --run-dir <run> stage \
  --target <group-id> --stage brief_dedup --status passed \
  --detail "<N pieces checked; N collisions found; outcome: clean | resolved>"
```

Mark it flagged if a re-route is needed and source-intake has not yet re-run:
```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/manifest.py --run-dir <run> stage \
  --target <group-id> --stage brief_dedup --status flagged \
  --detail "re-route needed for <piece-id>: <reason>"
```

No writing agent for this group may be dispatched until this stage is passed or the flagged item is resolved.

---

## Return

State: which pieces were checked; whether the group is clean or a defect was found (verbatim source overlap, near-duplicate whole, or both); for each defect, what angle was assigned to each affected piece and what beat-allocation was written; whether any re-route was logged and which piece triggered it; whether the two-part success test is met. An honest "re-route needed" or "partial resolution" is a correct return. Do not call the group clean if any collision remains unresolved.
