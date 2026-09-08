---
name: group-review
description: Use to check and repair a finished group of pieces for repetition within a piece, major duplication across pieces, and format consistency — the last content stage before the group is declared done. Reads the whole group at once, fixes what it finds in place, catches defects no per-piece gate can see. Called by content-at-scale:orchestrate; rarely invoked alone.
---

# group-review

You read a finished group of pieces together, find three classes of problem —
text repeated inside a single piece, major duplication shared across pieces, and
format divergences — and **fix the real ones in place**, then confirm they are
gone. This is the only stage that can see across pieces for repetition, the only
one that reads each finished piece for internal repetition, and the last content
stage before the group ships, so it both detects and repairs in one pass.

**Read `${CLAUDE_PLUGIN_ROOT}/REFERENCE.md` first** for the script syntax and
the exit codes.

---

## Where you sit in the pipeline

You run **last**, after every piece in the group is fully finished:
produced, length-checked, overlap-checked, fact-checked, anti-slop-checked,
de-slopped, claim-diffed, and — for voice pieces — made readable. All earlier
stages must be terminal before you start.

The stage id is `group_review` (underscore). Your target is the group, not a
piece:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/manifest.py --run-dir <run> stage \
  --target g1 --stage group_review --status running
```

You own the whole stage: detect the candidates, judge them, fix the real findings
in place, re-run the check, and set the stage `passed` (or `flagged` if a finding
could not be worked). See "How a finding resolves" below.

Cross-group contradiction — claims that conflict across groups — is a separate
stage (`cross_group`, run: `content-at-scale:cross-group`), the last subagent
stage of the run. What you do here is editorial: repetition and format within a
group.

---

## Two modes — full group, or a scoped re-review

Normally you review and may edit **the whole group** — the default described
throughout this skill. But you can be dispatched a second time, in a **scoped**
mode, and the difference is a hard license boundary:

- **Full mode (default).** The group ran once; you review every piece and may edit
  any of them. Business as usual.
- **Scoped re-review.** One or more pieces were sent back through the produce cycle
  (a cross-group finding, or any later redo) and have been rebuilt; the rest of the
  group is unchanged and already shipped-clean from its first review. Your
  dispatch names **which pieces were re-run**. In this mode you may **edit only
  those named pieces** — but you must still **read and compare against the full
  group corpus**, because the whole point is to catch repetition or format drift
  between the rebuilt piece and its unchanged siblings. Do not touch a sibling that
  was not re-run; if a finding can only be fixed by changing an unchanged sibling,
  that is a cross-group-scale problem — report it, do not edit it.

If the **whole group** was rebuilt (the drastic case), there is no scoped subset —
run in full mode.

The dispatch tells you which mode you are in and, for scoped mode, the piece ids.
When in doubt, ask the orchestrator rather than assuming full-edit license.

---

## The hybrid method — the script proposes, you dispose (FROZEN)

Run the deterministic candidate-finder first:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/groupreview.py --run-dir <run> --group <gid>
```

It prints JSON with three keys:

- `intra_piece` — repeated distinctive spans within a single piece, each with
  the piece id, a representative span, and its occurrences. Each occurrence
  carries its own `line` and `text`. For an exact repeat every occurrence's
  `text` equals the span; for a near-repeat the occurrences differ by a word or
  two, so **read each occurrence's own `text`** — the top-level `span` is only a
  representative label, and the wording that actually appears at each line is in
  the occurrence.
- `cross_piece` — two kinds of cross-piece signal, labelled by `kind`:
  `shared_phrasing` (near-verbatim wording shared between pieces — the repetition
  *candidate*) and `shared_fact` (the same distinctive fact/example in two pieces
  even when phrased differently — a *locator*, not a defect; see Job 1's
  cross-piece section for how they differ).
- `format` — per-piece structural profile (heading outline, word count, H1
  presence, CTA presence), cross-piece divergences, and conformance against the
  declared spec parameters.

**These are candidates, never verdicts.** The script is deliberately
recall-tuned: it surfaces more than are real problems, so you do not have to
eyeball whole pieces blind. Your judgment decides each one. Never let the
candidate count drive a decision — a long list from the script is not a finding;
a confirmed real defect is.

Why FROZEN: the script's job is to narrow what you read, not to replace your
read. A tool that decides instead of proposes is the exact failure mode this
pipeline was built to prevent.

---

## Job 1 — Repetition, within a piece and across the group

Repetition is **one job at two scopes.** The same distinctive content can repeat
inside a single piece (the `intra_piece` candidates) or across two pieces in the
group (the `cross_piece` candidates). The deciding test is identical for both;
only the scope changes. The script found where the *words* match; your job is to
decide whether the *substance is delivered the same way* — **narrative function,
not string distance.**

**The deciding test: is the distinctive content reworded, or delivered the same
way?**

First name what the occurrences actually share — the specific claim, detail,
example, or figure a reader would recognise as *the same point*. Then ask whether
it returns in **different words** (a genuine reformulation) or **the same words,
the same argument the same way.**

- **Reworded → legitimate. Leave it.** Content that returns *restated* — different
  words, a broader or sharper formulation, a callback that advances the argument —
  is deliberate structure, not redundancy. An idea introduced early and
  reformulated later is a bookend. Leave it and note why in your report.

- **Same argument, same way → defect. Flag it.** When the distinctive content
  returns in the same (or almost the same) words, delivering the same point a
  second time, a reader meets the same passage twice. Flag it — then reason about
  which occurrence to resolve (below); the redundant one is not automatically the
  later one.

**Two failures to avoid — the test must hold in both directions:**

- *Under-flagging: surrounding material does not excuse a verbatim repeat.* New
  words *before or after* the repeated content do not make it legitimate; only
  rewording the repeated content itself does. The concrete check: **if you deleted
  the repeated span, would any information be lost?** If the point was already
  made, the answer is no — a defect, however much fresh material sits around it. A
  verbatim line delivered twice is not an "echo at the turning point"; a real echo
  is *reworded*.

- *Over-flagging: a shared element is not a defect; a shared delivery is.* A short
  stylistic tag, a common connective, or a routine qualifier can recur word-for-
  word while the substance around it differs. What decides a finding is whether
  the *distinctive content is delivered the same way* — never whether some string
  matches.

### Within a piece — the `intra_piece` candidates

Each candidate is a span the script found recurring in one piece, with each
occurrence's own `line` and `text`. For a near-repeat the occurrences differ by a
word or two, so **read each occurrence's own `text`** — the top-level `span` is
only a representative label. Apply the test above: a reworded return is a bookend
(keep); the same words carrying the same point is redundancy (flag).

### Across the group — the `cross_piece` candidates

Same test, but the over-flagging guard is sharper here, because pieces in a group
*share subject matter by design.*

**A shared fact, statistic, or example is NOT a defect.** Facts are reusable
supporting material. Several pieces drawing on the same evidence is expected — and
for the user's own-company facts it is plainly correct: pieces are not forbidden
from citing the same figures about the user's own business. **Never flag two
pieces merely for sharing a fact, an example, a statistic, a theme, or
vocabulary.** The only cross-piece defect is the **same argument delivered the
same way** — the same point, in near-verbatim phrasing, so a reader of both meets
the same passage twice.

The two `cross_piece` signals mean different things, and you use them differently:

- **`shared_phrasing`** — near-verbatim wording shared between pieces. This is the
  repetition *candidate*: check whether it is the same argument the same way
  (defect) or incidental shared wording (fine).
- **`shared_fact`** — the same distinctive fact/example (a name, statistic, number)
  in two pieces even when the phrasing differs. This is **not** a defect
  candidate; it is a **locator**. It points you at where a shared fact lives so
  you can check whether its *delivery* is also duplicated. Sharing the fact is
  fine; only a shared *delivery* around it is a finding.

This job is a **light backstop** (FROZEN). Two upstream stages already make the
pieces distinct — routing assigns each piece distinct source material, and
`brief-dedup` differentiates brief-level collisions before writing — so you are
only catching the really major delivery-level duplication that survived them: the
same argument told the same way across pieces. Rigour beyond that buys little and
risks flagging the unified messaging the two-axis substance/voice model exists to
protect.

**Exception:** if the run's declared parameters (spec / context-intake) state that
pieces must not share facts, enforce that — but that is an explicit up-front
requirement, not the default. Absent it, shared facts stay.

### Which occurrence to resolve — reason about it, don't default to the later one

A repeat has two ends, and the redundant one is not always the later one. Look at
where the content does the most narrative work — where it lands hardest, sets up
the most, or belongs to the structure (an opening thesis, a closing turn, or, for
a shared fact, the piece that genuinely depends on it). Keep it there and resolve
the weaker-placed delivery. **Resolving means rewording the duplicated delivery,
not stripping a shared fact:** both pieces keep the fact; one states it in
different words so the same passage no longer appears twice. (Only an explicit
no-shared-facts requirement changes that.) You make the edit yourself, in place —
see "How a finding resolves" below.

Quote both occurrences in your report, with the piece id(s) and their approximate
location (paragraph number or heading).

---

## Job 2 — Format consistency

From the `format` block, flag genuine problems:

- A piece outside its declared length bounds (word count below the min or above
  the max the spec declared for this group).
- A piece structurally unlike its siblings — missing an H1, wildly different
  heading structure, a required element absent (for example, the closing
  call-to-action the spec calls for).
- A cross-piece divergence severe enough that the pieces would not read as one
  series: radically different section counts, one piece in a completely
  different structural register.

Per-piece format was already checked by the `length` stage and by the
producing agent. What you are catching here is the divergence no per-piece
gate can see — conformance to the declared format parameters and consistency
across siblings.

---

## How a finding resolves — worked in place, by you (FROZEN)

This one stage detects, judges, and fixes. You do not hand a report to someone
else and you do not regenerate the piece from scratch. For each real finding you
make the targeted edit yourself, in the finished file, then re-run the check to
confirm the finding is gone.

The loop, per group:

1. **Fix each real finding in place.** For a repetition, reword the weaker-placed
   delivery you identified (keep the fact, change the words). For a format
   finding, align the structure. Edit the finished file directly (`readable.md`
   if present, else `final.md`/`verified.md` — whichever the script read). Touch
   only the offending span; leave everything else exactly as it is.

2. **Keep the reword clean — no gate runs after you (light anti-slop).** This is
   the last content stage; de-slop and anti-slop already ran and nothing re-checks
   your edit, so a slop phrase you introduce here ships. When you reword:
   - **No em-dashes or en-dashes** (`—`, `–`). Use a comma, a full stop, or
     restructure the sentence. This is the single most common AI-tell and it is
     banned outright.
   - **No slop scaffolding** — the generator tells de-slop exists to remove:
     "it's not just X, it's Y", "in today's world", "the truth is", "let's be
     honest", "at the end of the day", empty intensifiers ("truly", "simply",
     "incredibly"), and the rule-of-three flourish. Do not reach for them to fill
     a gap the reword opened.
   - **Match the piece's own voice and register.** The reword must read like the
     author wrote it — same tone, same rhythm, same plainness. Do not smooth a
     spoken-voice piece into corporate prose.
   - **Keep it grammatical and minimal.** The smallest change that removes the
     duplication. Do not "improve" the sentence beyond de-duplicating it.
   - **Never touch a verified claim's facts.** If the weaker occurrence carries a
     claim (a number, a named source), reword around it without altering the fact;
     if you cannot de-duplicate without changing the fact, leave it and flag it
     (below) instead.

3. **Re-run the check** on the group:
   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/groupreview.py --run-dir <run> --group <gid>
   ```
   Confirm the duplicated delivery you fixed no longer surfaces (a residual
   `shared_fact` locator or an incidental candidate you already cleared is fine —
   a defect you flagged is not).

4. **Set the stage status:**
   - Everything worked and the re-run is clean → `passed`.
   - A real finding you could not resolve by a local edit (e.g. de-duplicating
     would alter a verified fact) → `flagged`, with that finding named in your
     report. A `flagged` group_review blocks run completion until it is worked —
     deliberately: this is "worked in place," never "reported and ignored."

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/manifest.py --run-dir <run> stage \
     --target <gid> --stage group_review --status passed
   ```

Why FROZEN: the agent that read the whole group and reasoned which occurrence is
the stronger home is the one placed to make the edit — handing a report to a
separate editor would re-derive that context and fragment one job. And a targeted
edit on a finished piece costs far less than regenerating it.

---

## Report

Your report must state:

- The `groupreview.py` candidate counts (intra, cross, format) and how many
  you confirmed as real findings versus cleared.
- For each **real finding**: which job (Repetition — intra or cross — or Format),
  piece id(s), both occurrences quoted in full (or the exact format problem),
  approximate location, which occurrence you kept as the stronger narrative home,
  and **the edit you made** — the before and after text of the reworded span (or
  the format change), so the fix is on the record.
- For each **candidate you cleared**: why you cleared it (reworded return, shared
  fact with different delivery, shared subject vocabulary, etc.).
- The result of the re-run that confirmed your edits cleared the findings.
- Format: one paragraph of summary, then a finding-by-finding list.
- Whether the stage was set `passed` or `flagged` (and, if flagged, which finding
  could not be worked in place and why).

A cleared candidate with no reason is a silent skip, which is the exact failure
this pipeline is built against. If you cleared it, say why.
