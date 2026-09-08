# content-at-scale — shared reference

Every skill in this plugin reads and writes the same two files and speaks the
same vocabulary. This is the one copy of that vocabulary. Skills load it with

```
${CLAUDE_PLUGIN_ROOT}/REFERENCE.md
```

which resolves to an absolute path regardless of the user's working
directory.

**This file holds facts, not judgment.** Label names, script syntax, exit
codes, scope strings. The reasoning that governs *when* to use each of them
lives in the skill that uses it, because reasoning separated from its
decision is reasoning that gets skipped. If you are about to add a paragraph
here explaining why something is a good idea, it belongs in a `SKILL.md`.

Six skills needing the same schema meant six places for it to drift; this file
is the one copy.

---

## The two files a run owns

Both live in the run directory, which the user chooses at intake. There is no
fixed location and no discovery — **a resumed run is found by being told its
directory.**

| File | Written by | Holds |
|---|---|---|
| `manifest.json` | `scripts/manifest.py` | groups, pieces, per-stage status, gate results, artifact hashes, search budget, the human flag queue |
| `context.json` | `scripts/context.py` | every operating-context entry, with provenance, scope and state |

Never edit either by hand or with a text tool. Every write is a locked
read-modify-write followed by an atomic replace, because pieces run in
parallel and an unserialised write silently loses an update, leaving a file
that looks complete and is not.

The manifest records its own absolute path. Opening a run from a different
path is refused unless you pass `--allow-moved`. A legitimately moved run is
adopted with that flag; a **copy** of a live run is the case the refusal
exists for, because two orchestrators writing two copies split the run's
state and neither ends up complete.

---

## Scope strings

Three tiers. A scope is one of:

| Scope | Means |
|---|---|
| `global` | the whole run |
| `g1`, `g2`, … | one group |
| `g1-p2`, … | one piece |

Group and piece ids are assigned by `manifest.py` in spec order. You do not
choose them. Write the spec, then read the ids back out of the manifest
before referring to them anywhere.

A piece reads `global`, then its group, then itself. Later tiers are more
specific and **none of them replaces an earlier one** — nothing here is ever
overwritten.

With no grouping the run still has exactly one group.
There is one code path; there is no flat mode.

⚠️ **That group's scope string is `g1`.** Its *name* is `all`, and the name
is a label for humans — it is not a scope and `context.py` rejects it. In an
ungrouped run, group-level entries are `--scope g1`, and `--scope global` is
a different tier that happens to contain the same pieces. Writing group-level
material as `global` is accepted and quietly wrong: it reads back at the
wrong tier, so a later stage looking for group specificity finds only
run-wide statements.

---

## Provenance labels

Every context entry carries one. The label is what lets
an adversarial agent know what it is permitted to attack.

| Label | Meaning | On arrival | How gates treat it |
|---|---|---|---|
| `given` | user-asserted at setup | live | authoritative; adversarial agents do not challenge it |
| `ruled` | a person resolved a flagged contradiction | live | authoritative like `given`, but stays distinguishable so it is auditable which calls the pipeline forced a human to make |
| `sourced` | from intake material | live | carries a citation; challenge the citation, not the fact |
| `found` | discovered at runtime | **staged** | carries full web-source provenance (schema 3): a source class (`primary` or `secondary-corroborated`), a source date, a verbatim quote, and a volatility tag - `append` refuses it without them; a `volatile` finding also cannot be promoted until the auditor records a positive recency verdict; fully challengeable |
| `inferred` | model-derived, no source | **staged** | lowest trust; the first thing a gate should attack |

**Live** means the entry is readable by every stage immediately. **Staged**
means it is held as unconfirmed until promoted, and appears in
a read only with `--include-staged`, marked `UNCONFIRMED`.

`given`, `ruled` and `sourced` arrive live because all three have already
been through a human: the first two by definition, the third because intake
material passes segmentation, an adversarial challenge and the user's bulk
confirmation before it is appended.

Entry state is `staged` → `promoted` or `dropped`. A `promoted` entry can
only be retired by a **ruling**; `drop` refuses, on purpose.

---

## `manifest.py`

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/manifest.py --run-dir <run> [--allow-moved] <command>
```

| Command | Arguments |
|---|---|
| `init` | `--spec <path or ->` |
| `show` | — human-readable status |
| `json` | — dump the manifest |
| `stage` | `--target run\|g1\|g1-p2 --stage <name> --status <status> [--detail ...] [--artifact <path>]` |
| `verify` | `--piece g1-p2 --check length\|overlap\|claim_diff --ok true\|false --artifact <path> [--detail ...]` |
| `reanchor` | `--piece g1-p2 --artifact <run>/pieces/g1-p2/readable.md [--reason ...]` — re-record a voice piece's `readable.md` anti-swap hash after a small in-place fix, without reopening `spoken_to_written`. Refuses unless `spoken_to_written` passed; binds `--artifact` to the piece's own `readable.md`; stamps the stage `reanchored` with the reason. The non-voice analog is `verify` (re-record the recomputable gates against the edited `final.md`). |
| `spend` | `--bucket <name> -n <int>` |
| `flag` | `--target <id> --kind <kind> --message <text> [--refs <entry_id> ...] [--key <key>]`. `--kind contradiction` requires `--refs`, and a near-miss on that spelling is refused |
| `resolve` | `--refs <entry_id> [<entry_id> ...] --by <who>` — close open flags that refer to those ids; exits 0 even if nothing matched (prints count). **Refuses the whole call if any match is a `contradiction` or a `param_override`**: `contradiction` flags are closed only by `context.py rule`; `param_override` flags are cleared only via `accept-override` |
| `set-status` | `--status created\|running\|blocked\|complete\|abandoned` |
| `params` | `--update --key <k> --value <JSON> [--override --reason <r> --by <who>]`: changes a run parameter. Refuses to change a LOCKED parameter without `--override`; `--override` requires `--reason` and `--by`, logs the change to `param_overrides` in the manifest, and raises a `param_override` flag |
| `accept-override` | `--key <param> --by <who> [--note <text>]`: a person accepts a locked-parameter override, clearing the `param_override` flag that blocks completion |
| `barrier` | `[--group g1]` — may any piece start? exits **3** if not |
| `source` | `--path <file> --polarity owned\|third_party --role voice\|content\|both` — record a source file. `owned` → overlap gate is a floor (want it high). `third_party` → overlap gate is a ceiling (want it low). `content` → routed into pieces; `voice` → feeds the voice anchor, never routed; `both` → routed AND the voice reference. Refuses if the file does not exist. Re-recording the same path with the same polarity and role is idempotent (no-op). Re-recording with a different polarity or role raises an error; polarity and role are immutable once set. |
| `sources` | `[--polarity owned\|third_party] [--role content\|voice\|both]` — print recorded source paths, one per line, filtered by polarity and/or role when given. Exits 0 with no output when there are none. Output is consumed by shell building the overlap command — no headers, counts, or decoration. **`--role` matches one role value exactly:** `--role content` returns content sources ONLY — it does NOT include `both`-role sources, even though a `both` source is also content. So `--role content` is not a way to enumerate "everything routable" and is not a way to "exclude voice" (it drops `both` as well). To measure the content pool, prefer `overlap.py --run-dir` (registry mode), which reads the manifest directly and excludes only `voice`, keeping both `content` and `both` — see overlap.py. Use `--role` here only when you genuinely want one exact role class. |
| `redo` | `--piece g1-p2 --stage <name> --reason <text> --triggered-by <text>` — retire a closed piece stage and every downstream closed stage, archive their on-disk artifacts (never deleted), reset them to `pending`, and clear the verification block, ready for a fresh dispatch. Use when a failure surfaces *after* a stage was already `passed` (e.g. a `claim_diff` recompute implicates a closed `de_slop`). Appends a ledger record; does **not** alter the transition table. |
| `deliver` | `--out-dir <dir>` — copy each delivered piece's shipped file (`readable.md` for a voice piece, else `final.md`) to a distinctly named file in `<dir>`. First re-checks every recorded verification against its artifact on disk and refuses a file swapped since it was measured; delivers only pieces whose `produce` passed and reports any it excludes. |
| `next` | — print the single next action this run requires, with the exact command to run after completing it (delegates to `driver.py`). |

**Stages.** Run: `context_intake`, `source_intake`, `cross_group`,
`web_pass`. Group: `research`, `group_review`. Piece, in pipeline order:
`produce`, `length`, `overlap`, `fact_check`, `spoken_to_written`,
`anti_slop`, `de_slop`, `claim_diff`. `spoken_to_written` is a conditional pass:
it runs only when a piece's content and voice come from
the same rambling verbatim source (a role=both transcript), repairing spoken
grammar into readable prose without paraphrasing and writing
`pieces/<id>/readable.md`; it is skipped for pieces whose content is researched
findings with a separate voice profile. When it runs, `anti_slop` and `de_slop`
read `readable.md`; when it is skipped they fall back to `verified.md`.
`anti_slop` (schema 2) generates the checker's report on the
current working text for de-slop to read; it never fails on the checker's exit code,
but the CLI refuses to mark it `passed` without `pieces/<id>/antislop.txt` on
disk, and the run cannot complete while it is not terminal. `de_slop` cannot
open *or be skipped* until `anti_slop` is passed — de-slop reads the report, so
the report must exist first; opening `de_slop` re-checks `antislop.txt` on disk,
so a report deleted after the pass was recorded blocks it rather than passing
silently.

**Skip policy.** A piece stage is non-skippable by default; only four may be
marked `skipped` via the CLI, each for an honest reason: `overlap` (the gate did
not apply — a piece written from the intake alone has no source material),
`spoken_to_written` (the gate did not apply — the piece's content and voice come
from different sources, so there is no spoken grammar to repair; the g1 case),
`de_slop` (nothing to rewrite — allowed only once `anti_slop` passed), and
`produce` (the piece was not made — the honest marker for a dropped piece).
`length`, `fact_check`, `anti_slop` and `claim_diff` cannot be skipped **on a
produced piece**: they apply to every piece, so a skip would only be a way to
dodge the work while the record says it was done. The one exception is a
**dropped piece**: once its `produce` is skipped, the piece was never made, so
its remaining gates did not apply and may all be skipped — otherwise a piece
dropped before its gates ran would deadlock (pending gates that can neither be
skipped nor honestly passed). This does not reopen the dodge: a produce-skipped
piece is **never delivered** — `deliver` hands over only pieces whose `produce`
passed, and reports any it excludes — so a `final.md` planted for an unproduced
piece reaches no one, and skipping a dropped piece's gates ships nothing.
`deliver` also re-checks every recorded verification against
its artifact on disk at handover (the same check `set-status complete` runs),
so a `final.md` swapped after the run was completed is refused rather than
shipped — the bytes delivered are the bytes that were measured.

**Completion requires every stage *resolved* — `passed` or `skipped`.** A
`failed` stage does not count as resolved: it blocks `complete`, exactly as a
`flagged` stage does, because both mean work remains. A failure must be retried
until it passes (`failed -> running -> passed`) or flagged for a person
(`failed -> flagged`). A run cannot be marked `complete` — and therefore cannot
be delivered — while any gate is failed; otherwise the record would read "done"
over a gate that rejected the text.

**Statuses and the legal moves between them:**

```
pending  -> running, skipped
running  -> passed, failed, flagged
failed   -> running   (a gate failure retries)
         -> flagged   (repeated failure goes to a human)
flagged  -> running, passed        (a person resolved it)
passed   -> nothing
```

`stage` and `verify` are two different things and collapsing them loses the
check that matters. `stage` is what the **producing agent**
reports about its own work. `verify` is the **orchestrator's** independent
recomputation on the final artifact, and it is the one that decides pass or
fail. The gap between them is where an agent that honestly ran a check, and
then edited the text afterwards, becomes visible.

**A recorded verification failure cannot be recomputed away.** `verify --ok
true` on a check already recorded as failed is refused; a person accepting the
piece anyway is recorded with `--override "<reason>"`, and `show` then marks
that piece `verify=passed*` with a line saying what was accepted and why. A
`found` or `inferred` entry needs `--citation`; the script refuses without one.

**The barrier.** `barrier` reads and writes nothing. Exit `0` means pieces may
start; exit `3` means they may not and it prints every reason. It checks that
`context_intake` and `source_intake` are `passed` or `skipped`, that no flag of
kind `contradiction` is unresolved, and with
`--group`, that the group's `research` stage is `passed` or `skipped`. Only
contradiction flags block — a flag raised after production started would
otherwise deadlock the run that raised it.

**A skipped gate is dropped from the verdict, not passed.** `verification`
reaches `passed` once every check whose **stage is not `skipped`** has been
recorded. This is what lets a run with no source material finish: the overlap
gate has nothing to measure against, its stage is `skipped`, and the piece is
judged on the checks that applied. A failure already recorded cannot be
retired by skipping its stage afterwards, and skipping every gate is not a
pass.

**Search budget.** Buckets are `global`, `web_pass`, and one per group id.
Allocated at `init` against a ceiling; a bucket cannot overdraw. Record real
spend with `spend` as it happens.

**Locked parameters.** The manifest carries two fields related to parameter governance: `locked_parameters` (which parameters may not be changed mid-run without an explicit override) and `param_overrides` (the logged, human-accepted history of overrides that were applied). A `params --update` call on a locked key without `--override` is refused outright. With `--override`, the change is recorded in `param_overrides`, a `param_override` flag is raised, and the run cannot reach `complete` until `accept-override` is called by a person.

---

## `context.py`

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/context.py --run-dir <run> <command>
```

| Command | Arguments |
|---|---|
| `init` | — requires an existing manifest |
| `append` | `--text ... --provenance <label> --scope <scope> [--citation ...] [--quote-from PATH:START-END ...] [--quote-text PASSAGE ...] [--found-by ...] [--contradicts <id> ...]` — `--quote-from` stores the cited span's verbatim text in the entry (a `sourced` entry with a `--citation` requires it). `--quote-text` pairs by position with `--quote-from` to store only a contiguous stretch of the cited line instead of the whole line — verified against the span (whitespace-tolerant) and refused if absent; use it when one long line holds several points that route to different pieces. |
| `append --provenance found` (schema-3 web provenance) | additionally requires `--source-class <primary\|secondary-corroborated> --source-date <date> --quote "<verbatim sentence from the source>" --volatility <durable\|volatile>`; the entry is refused without all four. `--source-class secondary-corroborated` additionally requires `--corroborating "<url>\|<date>"` given **at least twice** (each an independent dated secondary) and `--primary-attempted "<what the primary returned>"`. `--quote` stores the source's own words (redacted at store time, distinct from the file-span `--quote-from`); it is the sentence the auditor re-fetches to confirm. |
| `audit` | `--id e0007 --recency-ok <true\|false> --cross-checked <true\|false> --by <stage>` — the auditor's verdict on a `found` entry. Records `{recency_ok, cross_checked, by}`; `promote` reads it. A `volatile` finding cannot promote until `recency-ok` and `cross-checked` are both `true`; a `durable` finding needs no audit. |
| `promote` | `--id e0007 [--detail ...]` — refuses a `volatile` `found` entry whose `audit` verdict is not recorded positive |
| `drop` | `--id e0007 [--detail ...]` |
| `retract` | `--id e0007 [--detail ...] [--superseded-by e0042]` — pull back a **promoted** finding whose text the source does not fully support. Append-only (records the transition in history, keeps the entry's text); the finding disappears from `read` like a drop. Only a `promoted` entry can be retracted — reject a staged one with `drop`. `--superseded-by` records the clean re-stage that replaces it (refused if that id is unknown). The preferred path is still to catch embellishment while staged; retract is the recovery path for when it is found after promotion. |
| `rule` | `--text ... --scope <scope> --resolves <id> [<id> ...]` |
| `reconcile` | — repair context/manifest disagreement after a crash |
| `read` | `--scope <scope> [--include-staged] [--json]` |

`append --contradicts` files a flag in the **manifest**, so a run has one
human queue rather than a second one nobody checks. A contradiction is
surfaced, never auto-resolved. Whether the entry itself stages
follows its label, not the contradiction: `found` and `inferred` stage
anyway, `sourced` stays live, and `given` and `ruled` cannot be filed as
contradicting at all.

⚠️ **An open contradiction flag stops the run.** The orchestrator refuses to
cross the intake barrier — no piece starts until
every declared contradiction has a ruling. Resolve them at intake, where
nothing has been produced and the answer costs one question.

`reconcile` exists because the context and the manifest are two files and a
write to both cannot be one atomic act. Run it when resuming a run that died
mid-write.

**Naming, stated because the wrong word would be a lie:** this script does
not *detect* contradictions and cannot — that is semantic judgment with no
deterministic test. A caller **declares** one; the script guarantees
everything that follows. It is contradiction enforcement.

---

## The three gates

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/length.py <content> [--min N] [--max N] [--raw] [--json]

python3 ${CLAUDE_PLUGIN_ROOT}/scripts/overlap.py <content> \
    [--owned-source <file> ...] [--min-word-overlap F --min-phrase-overlap F] \
    [--third-party-source <file> ...] [--max-word-overlap F --max-phrase-overlap F] \
    [--phrase-length N] [--json] [--producer]

# Registry-driven mode (preferred in orchestrator runs):
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/overlap.py <content> \
    --run-dir <run> \
    [--min-word-overlap F --min-phrase-overlap F] \
    [--max-word-overlap F --max-phrase-overlap F] \
    [--phrase-length N] [--json] [--producer]

python3 ${CLAUDE_PLUGIN_ROOT}/scripts/claimdiff.py \
    --before <file> --after <file> --claims <claims-file> [--json]
```

`--source` is no longer accepted (exit 1). Polarity must be declared with
`--owned-source` or `--third-party-source`. At least one class is required.
Each class requires its own threshold pair: `--min-*` for owned (floor),
`--max-*` for third-party (ceiling). Thresholds for an absent class must not
be passed.

**`--run-dir PATH`** reads sources and their polarities from the run's
manifest registry instead of requiring explicit flags. `--owned-source` /
`--third-party-source` are refused alongside it (exit 1) — two sources of
truth that can silently disagree is the defect this mode was built to prevent.
If the run has no recorded sources, exit 1. Thresholds are still required for
each class the registry holds; the script reports which are missing.

`--claims` is a **path to a file**, not inline JSON. The file holds either
`{"claims": ["sentence", ...]}` or a bare list of strings — one entry per
claim sentence that fact-check verified. Passing JSON on the command line
gets a file-not-found error, which reads like a missing artifact rather than
a malformed call.

The content file is **positional** in the first two. `--source` repeats,
once per source file.

**The overlap gate takes two thresholds and both are required**, because it
measures two different things and neither one alone is a gate:

- `--max-word-overlap` — what share of the piece's vocabulary also appears
  in the sources. Runs high on completely original writing, because the
  subject dictates the words. Measured on real text: independent prose on
  the same subject scored **29.7%–45.6%**. This is not a similarity
  threshold and must not be set like one; it is the tripwire that stays high
  when phrase overlap is being gamed.
- `--max-phrase-overlap` — what share of the piece's n-word runs appear
  verbatim in the sources. This is the one that separates *about the same
  thing* from *copied*. Independent prose scored **0.0%** at n=4 against a
  30,000-token source; a lifted passage scored 100%.

`--phrase-length` defaults to 4, and the gate reports a profile at 3, 4 and
5 while applying the threshold at 4. The spread is the
signature: a piece assembled by breaking up borrowed runs scores high at 3
and near zero at 5, and no honestly written piece does that.

**Exit codes.** `0` pass · `3` the gate failed · `1` the call was wrong ·
`2` belongs to argparse, which exits before any of this code runs. An agent
that reads argparse's 2 as a gate failure starts rewriting perfectly good
content to fix a command-line error.

`antislop` follows the same `0`/`3`/`1`/`2` contract as `length` and
`overlap`. Two invocation-error conditions share exit code `1` (file
unreadable; profile name unresolvable); gate failure — a banned word or
structural cap exceeded — is `3`.

`claimdiff` **always exits 0** — it flags, it does not block,
because a changed claim may be a legitimate tightening. Read the JSON, not
the exit code.

`overlap --producer` prints the lifted passages and **no score at all**.
The producing agent gets what it needs to fix real lifting and
nothing it could tune a near-miss against.

Neither `length` nor `overlap` has a default threshold. Both refuse to run
without one.

---

## `voice.py`

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/voice.py --run-dir <run> <command>
```

| Command | Arguments |
|---|---|
| `record` | `--kind user\|profile` and either `--text <text>` (user) or `--name <short-name>` (profile) |
| `emit` | — prints the recorded voice reference text to stdout |

**`record`** writes the voice reference to `voice_ref.md` in the run directory
and a sidecar (`voice_ref_anchor.json`) recording the anchor identity. Calling
it a second time overwrites both. A run has one active voice reference chosen
once at intake.

**`emit`** prints the full text of the recorded reference. Its output goes into
producing briefs verbatim — nothing else goes into that output. Exits non-zero
if no reference has been recorded, or if the file is empty or whitespace-only;
both are loud errors by design.

---

## `groupreview.py`

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/groupreview.py --run-dir <run> --group <gid>
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/groupreview.py --pieces <p1.md> <p2.md> ... --group <gid>
[--phrase-length N]
```

The group-review candidate-surfacer. Reads every finished piece in one group and
prints a JSON object with three keys — `intra_piece`, `cross_piece`, `format`.
With `--run-dir` it discovers pieces under `<run>/pieces/<gid>-p*`, preferring
the most finished file it finds (`readable.md` → `final.md` → `verified.md` →
`draft.md`) and reads declared length bounds from the run manifest. `--run-dir`
and `--pieces` are mutually exclusive; `--phrase-length` defaults to 4.

**Output shape.**

- `intra_piece` — a list of repeated spans within one piece. Each entry:
  `{"piece", "span", "occurrences": [{"line", "context", "text"}, ...]}`. The
  top-level `span` is a **representative label**. Each occurrence carries its
  own `line` and its own actual `text`: for an exact repeat every occurrence's
  `text` equals the span; for a near-repeat (one or two words differ) the
  occurrences' `text` values differ, and the differing wording is the point —
  read the occurrence `text`, not the span, to see what is on each line.
- `cross_piece` — two kinds of cross-piece signal. Each entry is
  `{"kind": "shared_phrasing"|"shared_fact", "shared", "pieces":
  [{"piece", "line"}, ...]}`. `shared_phrasing` is near-verbatim wording shared
  between pieces — the repetition candidate. `shared_fact` is the same distinctive
  fact/example (name, statistic, number) reused across pieces even when the
  phrasing differs — a **locator**, not a defect: a shared fact is legitimate
  supporting material, and it is flagged only if its *delivery* is also
  duplicated. The reviewer's judgment, not the signal kind, decides.
- `format` — `{"per_piece", "cross_piece_divergences", "declared_param_status"}`:
  a structural profile per piece (heading outline, word count, H1, CTA), the
  cross-piece divergences, and conformance against the declared length bounds.

**Exit code.** `groupreview.py` **always exits 0.** It surfaces candidates; it
does not gate. There is no pass/fail here — the reviewing agent decides each
candidate, and the orchestrator sets `group_review` to `passed` or `flagged`.
Everything the script emits is a candidate, never a verdict.
