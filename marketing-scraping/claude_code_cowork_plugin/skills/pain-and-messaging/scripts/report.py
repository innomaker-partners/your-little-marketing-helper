"""
Report generator for the pain & messaging tool.

Reads synthesis_facts.json (produced by extractor.py) and generates
two analyst reports using a local claude CLI call:
  E1_pain_points.md   -- pain taxonomy, language bank, copy formulas
  E2_trust_signals.md -- trust-signal ranking, gap analysis, quick wins

Model tier (deliberate, NOT the cheapest): this step CLUSTERS and WRITES the
analyst report from the pre-counted facts. That is a judgment/synthesis task,
so the default model is a capable one (sonnet), overridable via cfg
`report_model`. This is the opposite of the companion tool's OCR step, whose design pins
the CHEAPEST model (haiku) precisely because OCR is trivial
transcription with no judgment. The cheap-model rule was task-specific to
transcription; do not carry it over to this clustering step.

Design rules (all FROZEN):
  Integrity rule: the LLM's only job is to cluster, label, and write -- using
    the pre-counted facts and verbatim quotes provided. It must never invent a
    pain or trust signal, never count, never assert anything not backed by
    a quote in the prompt.
  Honesty rule: the report header must state the low-star N and total
    denominator. When directional=True, findings are called directional,
    not statistical.
  Implementation: the LLM runs as a CLI subprocess (claude --print), never a
    direct API call. No OpenAI/Anthropic key needed; only the user's Apify token.

Integration seam (called by pain_intel.py after SCRIPTS is on sys.path):
  from scripts import extractor, report
  report.generate(workdir, cfg)   # -> {"E1": Path|None, "E2": Path|None}

Stdlib only -- no pip install needed.
"""

import datetime
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import quote_tool


# ---------------------------------------------------------------------------
# Module-level paths
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_TEMPLATES_DIR = _HERE.parent / "templates" / "reports"

# Capable default, NOT the cheapest: clustering/writing the analyst report is a
# judgment task (see module docstring). Overridable via cfg `report_model`.
_DEFAULT_MODEL = "sonnet"

# NO TIMEOUT on the LLM call. Report generation is NON-DETERMINISTIC: a capable
# model writing a full analyst report legitimately takes minutes and the wall
# time varies run to run. That slowness is normal work, not a hang, so bounding
# it with a clock would abort healthy generations. Timeouts belong to
# DETERMINISTIC, latency-bound calls where a stall means something is stuck --
# for example the Apify HTTP calls in apify_client.py, which keep theirs. This
# call blocks until the model finishes or the CLI itself errors out.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_template(name: str) -> str:
    """Load a report template by filename from the templates/reports/ directory."""
    path = _TEMPLATES_DIR / name
    if not path.exists():
        print(f"[report] WARNING: template not found at {path}")
        return ""
    return path.read_text(encoding="utf-8")


def _strip_outer_code_fence(text: str) -> str:
    """Remove a single code fence that wraps the ENTIRE output, if present.

    Some model runs return the whole report wrapped in a ```markdown ... ```
    fence, which then renders as one raw code block instead of formatted
    markdown. This is non-deterministic model behavior, not a prompt bug,
    and belongs in plain code rather than left to model reliability.

    GUARDED (conditional fix, not a blanket strip): fires only when the first
    line is a bare fence-open (``` optionally followed by a single language
    token like ```markdown) AND the last line is a bare closing fence. That is
    the wrapped-whole-document signature. Only those two boundary lines are
    removed, so any code fence INSIDE the report body is left untouched, and
    output that is not fence-wrapped is returned unchanged.
    """
    stripped = text.strip()
    lines = stripped.split("\n")
    if len(lines) < 2:
        return text
    first = lines[0].rstrip()
    last = lines[-1].rstrip()
    lang = first[3:].strip()
    is_open = first == "```" or (first.startswith("```") and lang.isalnum())
    is_close = last == "```"
    if is_open and is_close:
        return "\n".join(lines[1:-1]).strip()
    return text


def _strip_leading_preamble(text: str) -> str:
    """Drop any chatter the model emits before the report's first heading.

    The report scaffold always opens with a markdown H1 ("# E1 -" / "# E2 -"),
    so there is never legitimate content before it. Some runs still prepend a
    conversational lead-in ("No incoming changes. Here is the finished report.")
    and/or a stray horizontal rule before the heading. Non-deterministic
    model behavior, handled deterministically here.

    GUARDED: strips only when the text does NOT already start with a heading AND
    a markdown heading exists later. If there is no heading anywhere, the text is
    returned unchanged (never guess a start point away and risk eating a report
    that legitimately has no H1).
    """
    stripped = text.strip()
    if stripped.startswith("#"):
        return stripped
    lines = stripped.split("\n")
    for i, line in enumerate(lines):
        if re.match(r"^#{1,6}\s", line):
            return "\n".join(lines[i:]).strip()
    return text


def _looks_like_report(text: str) -> tuple:
    """Validate that CLI output is an actual report, not a subagent side-effect.

    Returns (ok, reason). The report scaffold always opens with a markdown H1
    ("# E1 -" / "# E2 -"), so a valid report starts with a heading after the
    strippers run. Two failure modes are rejected here:

    1. The `claude` CLI, when it has tool access, sometimes IGNORES the "output
       the report" instruction and instead BUILDS AND PUBLISHES an Artifact,
       returning only a chatty "here is the artifact I made" summary that carries
       a claude.ai/code/artifact URL. That is not a report and must never
       be written as one -- and
       it is an unintended external publish. `_run_claude` now starves the
       subprocess of MCP servers (`--strict-mcp-config` + empty `--mcp-config`)
       and disables built-in tools (`--tools ""`) so this cannot happen, but this
       guard stays as defense-in-depth (and to catch any other non-report output).
    2. A refusal or bare chatter with no heading at all.

    A whole-document code fence or a conversational preamble are NOT rejected
    here -- the strippers handle those before this check runs.
    """
    t = (text or "").strip()
    if not t:
        return False, "empty"
    if "claude.ai/code/artifact" in t.lower() or "/code/artifact/" in t.lower():
        return False, "contains an artifact URL (subagent took an action instead of returning the report)"
    if not t.startswith("#"):
        return False, "does not start with a markdown heading"
    return True, "ok"


def _clean_template(text: str) -> str:
    """Replace em-dashes in template scaffold text with ' - '.

    House copy rule: no em-dashes in our authored voice. Templates authored
    before this rule may contain em-dashes in headings; strip them at the
    prompt-building seam so the LLM never sees them as a model to copy.

    Scope is deliberately OUR text only (templates + the LLM's own prose,
    governed by preamble rule (e)). It must NEVER be extended to the verbatim
    customer quotes that quote_tool.paste_refs() inserts: a quote is evidence,
    presented as the reviewer's exact words, so normalizing its punctuation
    would quietly make a "verbatim" quote non-verbatim and break the tool's
    honesty spine. FROZEN: em-dashes inside quoted review text are left untouched.
    """
    return text.replace("—", " - ")


def _fill_placeholders(template: str, cfg: dict) -> str:
    """Replace {{CLIENT}} and {{DATE}} in the template scaffold."""
    vertical = (cfg.get("vertical") or "").strip()
    city = (cfg.get("city") or "").strip()
    client_label = (cfg.get("client_label") or "").strip()

    if client_label:
        client = client_label
    elif vertical and city:
        client = f"{vertical} / {city}"
    elif vertical:
        client = vertical
    else:
        client = "Client"

    today = datetime.date.today().isoformat()
    result = template.replace("{{CLIENT}}", client)
    result = result.replace("{{DATE}}", today)
    return result


# ---------------------------------------------------------------------------
# Provenance verification -- deterministic substring check (warn-only)
# ---------------------------------------------------------------------------

# Matches quoted spans in both straight double quotes ("...") and curly double
# quotes ("“...”"). Two capturing groups -- group(1) for straight,
# group(2) for curly -- so the caller does `m.group(1) or m.group(2)`.
# [^"\n]+ stops at the closing straight quote or a newline (no cross-sentence
# matches). The curly side stops at the closing ” or a newline.
# Build the quote-extraction regex programmatically so that the ASCII double-
# quote character (U+0022) is expressed via a hex escape (\x22) rather than
# the literal “ glyph, which some editors and transfer layers auto-convert to
# smart/curly quotes and corrupt the pattern.
_QUOTE_RE = re.compile(
    '\x22([^\x22\n]+)\x22'               # straight double quotes (U+0022)
    '|“([^”\n]+)”'        # curly double quotes (U+201C / U+201D)
)


def _norm_text(s: str) -> str:
    """Normalize a string for provenance matching.

    Lowercases, replaces any non-word / non-space character with a space
    (Unicode-aware \\w so non-English corpora are handled), and collapses
    whitespace. Applied to both the report span and each bank entry text so
    the comparison is insensitive to punctuation variants and case.
    """
    return " ".join(re.sub(r"[^\w\s]", " ", s.lower()).split())


def _provenance_note(report_text: str, quote_bank: list) -> str:
    """Check whether quoted passages in report_text exist in quote_bank.

    Extracts spans inside straight double quotes and curly double quotes,
    keeps only spans with >=5 whitespace-separated words (shorter spans are
    emphasis / product names, not review quotes), then checks whether each
    normalized span is a SUBSTRING of any normalized bank entry text.
    Substring (not equality) is correct because the LLM often quotes a
    fragment of a longer review.

    Returns "" when all spans are verified -- no noise on clean reports.
    Returns a markdown disclosure block appended AFTER the report prose when
    unverified spans exist -- warn-only: the analysis text above
    the note is never modified.

    Bank entries may be dicts with a 'text' key or plain strings (defensive).
    """
    norm_bank = []
    for entry in quote_bank:
        if isinstance(entry, dict):
            raw = entry.get("text", "")
        else:
            raw = str(entry)
        norm_bank.append(_norm_text(raw))

    unverified = []
    seen_norms: set = set()
    for m in _QUOTE_RE.finditer(report_text):
        span = m.group(1) or m.group(2)
        if not span:
            continue
        if len(span.split()) < 5:
            continue  # short emphasis phrase, not a review quote
        norm_span = _norm_text(span)
        if norm_span in seen_norms:
            continue
        seen_norms.add(norm_span)
        if not any(norm_span in nb for nb in norm_bank):
            unverified.append(span)

    if not unverified:
        return ""

    n = len(unverified)
    bullets = "\n".join(f'- "{s}"' for s in unverified)
    return (
        f"\n\n---\n\n"
        f"### Provenance note (automated check)\n\n"
        f"{n} quoted passage(s) below could not be matched to the scraped reviews "
        f"and should be treated as unverified:\n\n"
        f"{bullets}\n\n"
        f"This is an automated string check appended after generation; "
        f"the analysis above was not modified.\n"
    )


# ---------------------------------------------------------------------------
# Prompt preamble -- encodes the core report integrity constraints in plain English
# ---------------------------------------------------------------------------

_PREAMBLE = (
    "You are writing an analyst report. "
    "You are given pre-counted findings and verbatim customer quotes. "
    "Cluster and label them into the report structure below. "
    "Rules: "
    "(a) Quoting is done for you: each customer quote below is labelled with "
    "a short ID like Q7. To include a customer's words, just write that ID in "
    "double square brackets where you want the quote, like [[Q7]], and their "
    "exact words are inserted for you. You never retype or paraphrase a "
    "customer's words, and you never put customer words in quotation marks "
    "yourself; you simply reference the ID. Every claim must be backed by a "
    "quote you reference this way. "
    "(b) do not invent a pain or trust signal that is not in the findings; "
    "(c) do not count or compute new statistics -- use the counts as given; "
    "(d) the low-star sample may be small, so state N honestly and call "
    "findings directional, not statistical; "
    "(e) write in plain professional English: no em-dashes, no en-dashes, and "
    "no double-hyphen dashes (use commas, periods, or parentheses instead), and "
    "no AI-cliche filler; "
    "(f) when the findings span more than one source, disclose N per source in "
    "the report header (the source_distribution and per_source keys in the meta "
    "block give you the per-source Ns) and attribute each quoted review to its "
    "platform (the 'source' field is present in every quote record); "
    "(g) some sources have NO real star ratings -- their pain/praise split is "
    "INFERRED from the text of each post/comment (the meta block's "
    "'inferred_band_sources' names any such source present in this run). When that "
    "list is non-empty, state plainly in the header that those sources' split is "
    "inferred sentiment, not a star rating the customer chose, and never present an "
    "inferred band as equivalent to a 1-star or 5-star review. "
    "Output ONLY the finished markdown report, nothing else."
)

# E2-specific scope restriction: no listing checklist, no competitor mechanics
# Note: deliberately avoids the exact phrases "Google Business Profile" and
# "website mechanics" so the E2 prompt is clean of those terms (test A4).
_E2_SCOPE = (
    "E2 is review-derived only; do not include listing checklist sections, "
    "third-party profile setup steps, or implementation-count data -- "
    "there is no such data in this run."
)


# ---------------------------------------------------------------------------
# Prompt builders (pure functions -- testable without LLM or file I/O)
# ---------------------------------------------------------------------------

# Sources whose low/high split is INFERRED (no real star rating). banding.py
# synthesizes stars for these from text, so the report must disclose them as
# inferred sentiment (honesty rule). Extend as more no-star sources arrive.
_INFERRED_BAND_SOURCES = frozenset({"reddit"})


def _inferred_sources_present(source_dist):
    """Which inferred-band sources actually appear in this run (order-stable)."""
    return [s for s in source_dist if s in _INFERRED_BAND_SOURCES]


def build_e1_prompt(facts: dict, cfg: dict, template_text: str) -> str:
    """Build the full E1 (pain points) prompt.

    Pure function: no file I/O, no subprocess, no side effects.
    Accepts the raw template text (read externally so tests can supply stubs).
    Returns the complete prompt string to pipe to the claude CLI.
    """
    meta = facts.get("meta", {})
    pain_list = facts.get("pain_points_low_star", [])
    low_quotes = facts.get("low_star_quotes", [])

    low_n        = meta.get("low_star_n", 0)
    total        = meta.get("total_reviews", 0)
    high_n       = meta.get("high_star_n", 0)
    directional  = meta.get("directional", True)
    star_dist    = meta.get("star_distribution", {})
    source_dist  = meta.get("source_distribution", {})
    per_source   = meta.get("per_source", {})
    note         = meta.get("note", "")

    scaffold = _fill_placeholders(_clean_template(template_text), cfg)

    meta_block = json.dumps({
        "low_star_n":          low_n,
        "total_reviews":       total,
        "high_star_n":         high_n,
        "star_distribution":   star_dist,
        "source_distribution": source_dist,
        "per_source":          per_source,
        "inferred_band_sources": _inferred_sources_present(source_dist),
        "directional":         directional,
        "note":                note,
    }, ensure_ascii=False, indent=2)

    pain_block = json.dumps(pain_list, ensure_ascii=False, indent=2)

    # ID-labelled quote list for the [[Q<n>]] reference system.
    # IDs are 1-based and MUST match bank order exactly (Q1 = low_quotes[0]),
    # because paste_refs() resolves [[Q<n>]] to bank[n-1]. Never reorder.
    _e1_labeled = []
    for _i, _q in enumerate(low_quotes, start=1):
        _txt = _q["text"] if isinstance(_q, dict) else str(_q)
        _src = (_q.get("source") or _q.get("business") or "") if isinstance(_q, dict) else ""
        _src_note = f"   (source: {_src})" if _src else ""
        _e1_labeled.append(f'[[Q{_i}]] "{_txt}"{_src_note}')
    labeled_quotes_e1 = "\n".join(_e1_labeled)

    # Per-source disclosure token (per-source N, v1.1). For a maps-only
    # corpus this is "  sources: maps=36"; for multi-source it names each.
    if source_dist:
        src_token = "  sources: " + ", ".join(
            f"{s}={n}" for s, n in source_dist.items()
        )
    else:
        src_token = ""

    parts = [
        _PREAMBLE,
        "",
        (
            f"## Pre-counted findings"
            f"  (low_star_n={low_n}"
            f"  total_reviews={total}"
            f"  directional={directional}"
            f"{src_token})"
        ),
        "",
        "### Meta",
        meta_block,
        "",
        "### Pain phrases (ranked by distinctiveness in low-star reviews)",
        pain_block,
        "",
        f"## Customer quotes -- reference these by ID (for example [[Q3]])  (N={low_n})",
        "",
        labeled_quotes_e1,
        "",
        "## Report scaffold to fill in",
        "",
        scaffold,
    ]
    return "\n".join(parts)


def build_e2_prompt(facts: dict, cfg: dict, template_text: str) -> str:
    """Build the full E2 (trust signals) prompt.

    Pure function: no file I/O, no subprocess, no side effects.
    Accepts the raw template text (read externally so tests can supply stubs).
    Returns the complete prompt string to pipe to the claude CLI.
    """
    meta = facts.get("meta", {})
    trust_list  = facts.get("trust_signals_high_star", [])
    high_quotes = facts.get("high_star_quotes", [])

    high_n       = meta.get("high_star_n", 0)
    low_n        = meta.get("low_star_n", 0)
    total        = meta.get("total_reviews", 0)
    directional  = meta.get("directional", True)
    star_dist    = meta.get("star_distribution", {})
    source_dist  = meta.get("source_distribution", {})
    per_source   = meta.get("per_source", {})
    note         = meta.get("note", "")

    scaffold = _fill_placeholders(_clean_template(template_text), cfg)

    meta_block = json.dumps({
        "high_star_n":         high_n,
        "low_star_n":          low_n,
        "total_reviews":       total,
        "star_distribution":   star_dist,
        "source_distribution": source_dist,
        "per_source":          per_source,
        "inferred_band_sources": _inferred_sources_present(source_dist),
        "directional":         directional,
        "note":                note,
    }, ensure_ascii=False, indent=2)

    trust_block = json.dumps(trust_list, ensure_ascii=False, indent=2)

    # ID-labelled quote list for the [[Q<n>]] reference system.
    # IDs are 1-based and MUST match bank order exactly (Q1 = high_quotes[0]),
    # because paste_refs() resolves [[Q<n>]] to bank[n-1]. Never reorder.
    _e2_labeled = []
    for _i, _q in enumerate(high_quotes, start=1):
        _txt = _q["text"] if isinstance(_q, dict) else str(_q)
        _src = (_q.get("source") or _q.get("business") or "") if isinstance(_q, dict) else ""
        _src_note = f"   (source: {_src})" if _src else ""
        _e2_labeled.append(f'[[Q{_i}]] "{_txt}"{_src_note}')
    labeled_quotes_e2 = "\n".join(_e2_labeled)

    # Per-source disclosure token (mirrors build_e1_prompt)
    if source_dist:
        src_token = "  sources: " + ", ".join(
            f"{s}={n}" for s, n in source_dist.items()
        )
    else:
        src_token = ""

    parts = [
        _PREAMBLE,
        "",
        _E2_SCOPE,
        "",
        (
            f"## Pre-counted findings"
            f"  (high_star_n={high_n}"
            f"  total_reviews={total}"
            f"{src_token})"
        ),
        "",
        "### Meta",
        meta_block,
        "",
        "### Trust phrases (ranked by distinctiveness in high-star reviews)",
        trust_block,
        "",
        f"## Customer quotes -- reference these by ID (for example [[Q3]])  (N={high_n})",
        "",
        labeled_quotes_e2,
        "",
        "## Report scaffold to fill in",
        "",
        scaffold,
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# CLI subprocess -- mirrors the companion tool's OCR call pattern (FROZEN)
# ---------------------------------------------------------------------------

def _run_claude(prompt: str, model: str, label: str, max_attempts: int = 2) -> str:
    """Invoke the claude CLI with prompt via stdin; return the report text.

    Pattern follows the companion tool's OCR call (CLI subprocess, never direct API), with two
    deliberate differences from OCR:

    1. No timeout. OCR is a fast, near-deterministic transcription; this is an
       open-ended non-deterministic generation whose normal runtime is minutes,
       so it is left to run to completion (see the _DEFAULT_MODEL note above).
    2. `--strict-mcp-config --mcp-config '{"mcpServers":{}}'` STARVES THE
       SUBPROCESS OF ITS ENVIRONMENT. A freshly spawned `claude` does NOT
       inherit the parent as a lightweight
       text call -- it re-reads ~/.claude.json and boots the operator's ENTIRE
       MCP server stack. In an MCP-heavy environment that means launching the
       browser servers (claude-in-chrome, computer-use, Control_Chrome,
       playwright) on every single report call -- blank browser windows opening
       and multi-minute per-call startup. `--tools ""`
       does NOT prevent this: it gates which tools the model may CALL, not which
       MCP servers LOAD, and the windows come from server *startup* (a load-time
       side effect), a layer no tool flag reaches. `--strict-mcp-config` ignores
       ~/.claude.json entirely and `--mcp-config '{"mcpServers":{}}'` supplies an
       empty set, so ZERO servers boot: no windows, ~6s startup. Verified by
       process delta (browser procs unchanged across a call), not just output.
    3. `--tools ""` DISABLES ALL BUILT-IN TOOLS -- kept as defense-in-depth. This
       step wants pure text: cluster the given findings and write the markdown
       report to stdout. With no MCP servers there is no Artifact publisher to
       reach, but disabling built-in tools too guarantees the CLI can only emit
       text -- it cannot act or publish. (A run without these flags came back
       as a 1.5 KB "here is the artifact" note carrying a
       claude.ai/code/artifact URL instead of the report.) We also pass
       neither `--permission-mode` nor `--allow-dangerously-skip-permissions`:
       this subprocess is a pure, un-privileged text generator.

    Output hygiene + validation: strip a whole-document code fence and any
    conversational preamble, then check the result actually looks like a report
    (_looks_like_report). If not -- a refusal, chatter, or (belt-and-suspenders)
    an artifact URL -- retry up to max_attempts, since the failure is
    non-deterministic. Degrades gracefully to "" on persistent failure or a
    missing CLI, never raising.
    """
    cmd = [
        "claude",
        "--print",
        "--output-format", "text",
        # Stop the subprocess from booting the parent's
        # MCP stack (browser servers -> blank windows, minutes of startup).
        "--strict-mcp-config",              # ignore ~/.claude.json MCP servers
        "--mcp-config", '{"mcpServers":{}}',  # ...and supply an empty set
        "--tools", "",          # defense-in-depth: no built-in tools either
        "--model", model,
    ]

    last_reason = "no attempt made"
    for attempt in range(1, max_attempts + 1):
        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            # Not retryable: the binary is absent.
            print(f"[report] `claude` CLI not found -- {label} not generated.")
            return ""
        except Exception as exc:  # noqa: BLE001
            last_reason = f"unexpected error: {exc}"
            print(f"[report] {label}: {last_reason} (attempt {attempt}/{max_attempts})")
            continue

        if result.returncode != 0:
            stderr_snippet = (result.stderr or "").strip()[:200]
            last_reason = f"non-zero exit ({result.returncode})" + (
                f": {stderr_snippet}" if stderr_snippet else "")
            print(f"[report] {label}: {last_reason} (attempt {attempt}/{max_attempts})")
            continue

        text = (result.stdout or "").strip()
        if not text:
            last_reason = "empty output"
            print(f"[report] {label}: {last_reason} (attempt {attempt}/{max_attempts})")
            continue

        # Deterministic output hygiene (non-deterministic model quirks seen on
        # live runs): unwrap a whole-report ```markdown ... ``` fence,
        # then drop any conversational preamble before the report's first heading.
        # Order: unfence first so the heading is exposed, then strip any lead-in.
        cleaned = _strip_outer_code_fence(text)
        cleaned = _strip_leading_preamble(cleaned)

        ok, reason = _looks_like_report(cleaned)
        if ok:
            return cleaned
        last_reason = reason
        print(
            f"[report] {label}: output failed report validation ({reason}); "
            f"attempt {attempt}/{max_attempts}"
            + (" -- retrying" if attempt < max_attempts else "")
        )

    print(f"[report] {label}: no valid report after {max_attempts} attempts "
          f"({last_reason}) -- not generated.")
    return ""


# ---------------------------------------------------------------------------
# Reddit neutral set-aside -> optional "general topics & questions" appendix (E3)
# ---------------------------------------------------------------------------

_NEUTRAL_MIN_ITEMS = 3   # fewer than this: just stored for rigour, no section


def _neutral_topics_report(workdir, cfg, model):
    """Offer the Reddit neutral set-aside for mining as an optional E3 section.

    Items that banding judged neither complaint nor praise are stored in
    <workdir>/reddit_reviews/reddit_neutral.json regardless. This surfaces them
    as a light appendix (E3) WHEN there is enough to be useful: the questions
    people asked (content/SEO opportunities) and, if the CLI is available, a short
    synthesis of the general discussion themes. Counting stays deterministic;
    the LLM only labels themes, never counts. Returns a Path or None
    (None = nothing worth a section; the items still sit in the json).

    The whole section is clearly framed as general discussion, NOT pain or praise,
    so it can never be mistaken for the analysis proper.
    """
    workdir = Path(workdir)
    neutral_path = workdir / "reddit_reviews" / "reddit_neutral.json"
    if not neutral_path.exists():
        return None
    try:
        items = json.loads(neutral_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    items = [r for r in items if isinstance(r, dict) and (r.get("text") or "").strip()]
    if len(items) < _NEUTRAL_MIN_ITEMS:
        return None

    # Deterministic: the questions people asked (verbatim, deduped, capped).
    seen, questions = set(), []
    for r in items:
        t = " ".join((r.get("text") or "").split())
        if "?" in t and t.lower() not in seen:
            seen.add(t.lower())
            questions.append(t if len(t) <= 300 else t[:297] + "...")
        if len(questions) >= 12:
            break

    n = len(items)
    lines = [
        "# E3 - General discussion & questions (Reddit)",
        "",
        f"_Generated from {n} Reddit items that the sentiment banding judged "
        "NEITHER a complaint NOR praise. This is general discussion, not pain or "
        "trust signal, and is kept separate from the E1/E2 analysis on purpose. "
        "It is offered as a place to find content and question ideas; treat it as "
        "directional._",
        "",
    ]

    # Optional LLM synthesis of discussion themes (run without MCP servers loaded, cheap).
    theme_md = ""
    if shutil.which("claude"):
        sample = "\n".join(f"- {(' '.join((r.get('text') or '').split()))[:300]}"
                           for r in items[:60])
        theme_prompt = (
            "Below are Reddit posts/comments that are general discussion about a "
            "product/topic, NOT complaints or praise. Cluster them into 2 to 4 "
            "general discussion themes. Output ONLY markdown starting with the "
            "heading '## Discussion themes', then one bullet per theme as "
            "`**Theme name.** one plain sentence`. Do not invent themes not present "
            "below. No em-dashes, no AI-cliche filler.\n\n"
            f"Items:\n{sample}\n"
        )
        theme_md = _run_claude(theme_prompt, model, "E3_themes", max_attempts=2)

    if theme_md:
        lines += [theme_md.strip(), ""]

    if questions:
        lines += ["## Questions people asked", ""]
        lines += [f"- {q}" for q in questions]
        lines += [""]
    elif not theme_md:
        # Nothing minable surfaced, but the set is non-trivial -- say so honestly
        # rather than pretend. (The items remain in reddit_neutral.json.)
        lines += ["_No distinct questions or themes surfaced from this neutral set; "
                  "it is retained in reddit_neutral.json for transparency._", ""]

    out_path = workdir / "E3_reddit_topics.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"   report -> E3_reddit_topics.md  ({n} neutral items, "
          f"{len(questions)} questions{', themes' if theme_md else ''})")
    return out_path


# ---------------------------------------------------------------------------
# Public API (called by pain_intel.py)
# ---------------------------------------------------------------------------

def generate(workdir, cfg) -> dict:
    """Generate E1 and E2 analyst reports from synthesis_facts.json.

    Reads:  <workdir>/synthesis_facts.json  (written by extractor.py)
    Writes: <workdir>/E1_pain_points.md
            <workdir>/E2_trust_signals.md

    Returns {"E1": Path | None, "E2": Path | None}.
    Paths are None when that report was not generated (CLI missing or error).

    Graceful degradation: if the claude CLI is absent from PATH, prints a
    clear message and returns {"E1": None, "E2": None} without crashing.
    The acquisition + extraction half of the pipeline already succeeded
    independently and its output is on disk regardless.
    """
    workdir = Path(workdir)

    if not shutil.which("claude"):
        print(
            "[report] claude CLI not found -- E1/E2 not generated; "
            "run with any CLI LLM available"
        )
        return {"E1": None, "E2": None}

    resolved_model = (cfg.get("report_model") or "").strip() or _DEFAULT_MODEL

    facts_path = workdir / "synthesis_facts.json"
    if not facts_path.exists():
        print(f"[report] synthesis_facts.json not found at {facts_path} -- skipping")
        return {"E1": None, "E2": None}

    facts = json.loads(facts_path.read_text(encoding="utf-8"))
    meta = facts.get("meta", {})
    low_n = meta.get("low_star_n", 0)
    total = meta.get("total_reviews", 0)
    directional = meta.get("directional", True)

    print(
        f"   report: {low_n} low-star / {total} total  "
        f"({'directional' if directional else 'statistical'})  "
        f"model={resolved_model}"
    )

    e1_template = _load_template("E1_pain_points.md")
    e2_template = _load_template("E2_trust_signals.md")

    e1_prompt = build_e1_prompt(facts, cfg, e1_template)
    e2_prompt = build_e2_prompt(facts, cfg, e2_template)

    # --- E1 ---
    e1_path = None
    e1_audit: dict = {}
    print("   report: generating E1_pain_points.md ...")
    e1_text = _run_claude(e1_prompt, resolved_model, "E1_pain_points")
    if e1_text:
        low_bank = facts.get("low_star_quotes", [])
        # Measure bypass rate on the RAW output (before substitution)
        n_inline_e1 = quote_tool.count_inline_quotes(e1_text)
        # Paste [[Q<n>]] refs -- verbatim by construction, so provenance passes
        e1_text, n_pasted_e1, n_bad_e1 = quote_tool.paste_refs(e1_text, low_bank)
        print(
            f"[report] E1 quotes: {n_pasted_e1} pasted via [[Q]] ref, "
            f"{n_inline_e1} typed inline (bypassed the tool), "
            f"{n_bad_e1} bad refs"
        )
        e1_audit = {"pasted": n_pasted_e1, "inline_typed": n_inline_e1, "bad_refs": n_bad_e1}
        # Provenance check on the now-substituted text (mainly catches any
        # inline-typed fabrication that bypassed the [[Q]] tool)
        e1_note = _provenance_note(e1_text, low_bank)
        if e1_note:
            n_unmatched = sum(1 for line in e1_note.splitlines() if line.startswith('- "'))
            print(f"[report] provenance: {n_unmatched} E1 quote(s) unmatched -- disclosure appended")
            e1_text = e1_text + e1_note
        e1_out = workdir / "E1_pain_points.md"
        e1_out.write_text(e1_text, encoding="utf-8")
        e1_path = e1_out
        print(f"   report -> E1_pain_points.md  ({len(e1_text)} chars)")

    # --- E2 ---
    e2_path = None
    e2_audit: dict = {}
    print("   report: generating E2_trust_signals.md ...")
    e2_text = _run_claude(e2_prompt, resolved_model, "E2_trust_signals")
    if e2_text:
        high_bank = facts.get("high_star_quotes", [])
        # Measure bypass rate on the RAW output (before substitution)
        n_inline_e2 = quote_tool.count_inline_quotes(e2_text)
        # Paste [[Q<n>]] refs -- verbatim by construction, so provenance passes
        e2_text, n_pasted_e2, n_bad_e2 = quote_tool.paste_refs(e2_text, high_bank)
        print(
            f"[report] E2 quotes: {n_pasted_e2} pasted via [[Q]] ref, "
            f"{n_inline_e2} typed inline (bypassed the tool), "
            f"{n_bad_e2} bad refs"
        )
        e2_audit = {"pasted": n_pasted_e2, "inline_typed": n_inline_e2, "bad_refs": n_bad_e2}
        # Provenance check on the now-substituted text
        e2_note = _provenance_note(e2_text, high_bank)
        if e2_note:
            n_unmatched = sum(1 for line in e2_note.splitlines() if line.startswith('- "'))
            print(f"[report] provenance: {n_unmatched} E2 quote(s) unmatched -- disclosure appended")
            e2_text = e2_text + e2_note
        e2_out = workdir / "E2_trust_signals.md"
        e2_out.write_text(e2_text, encoding="utf-8")
        e2_path = e2_out
        print(f"   report -> E2_trust_signals.md  ({len(e2_text)} chars)")

    # --- quote audit sidecar ---
    # Persists counts for the caller to inspect across runs.
    # Never crashes the run if the write fails.
    _audit_data: dict = {}
    if e1_audit:
        _audit_data["E1"] = e1_audit
    if e2_audit:
        _audit_data["E2"] = e2_audit
    if _audit_data:
        try:
            _audit_path = workdir / "quote_audit.json"
            _audit_path.write_text(
                json.dumps(_audit_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as _ae:
            print(f"[report] WARNING: could not write quote_audit.json: {_ae}")

    # --- E3 (optional): general topics & questions from the Reddit neutral set ---
    # Only produced when reddit is a source AND the neutral set has enough to mine.
    e3_path = None
    if "reddit" in (cfg.get("sources") or []):
        e3_path = _neutral_topics_report(workdir, cfg, resolved_model)

    return {"E1": e1_path, "E2": e2_path, "E3": e3_path}


# ---------------------------------------------------------------------------
# Inline verification tests
# Run with:  python3 scripts/report.py
#
# PART A: Deterministic prompt-assembly tests (no LLM, no files, no network).
#   Hits build_e1_prompt / build_e2_prompt directly with a fake facts dict.
#   Pattern mirrors subjects.py and extractor.py exactly.
#
# PART B: One real end-to-end LLM run (requires `claude` CLI in PATH).
