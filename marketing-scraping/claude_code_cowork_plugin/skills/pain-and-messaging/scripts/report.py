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
import os
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
        return False, "nothing came back"
    if "claude.ai/code/artifact" in t.lower() or "/code/artifact/" in t.lower():
        return False, "Claude did something else instead of writing the report"
    if not t.startswith("#"):
        return False, "the text that came back was not a report"
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
        "### Pain phrases (most-mentioned in low-star reviews; filler and shared topic/brand words removed)",
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
        "### Trust phrases (most-mentioned in high-star reviews; filler and shared topic/brand words removed)",
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

# Why a report was not written, kept for the run's closing lines.
# WHY: `_run_claude` returns "" for every kind of failure, so the entry script could
# only say "not generated" and the closing lines said nothing at all. The reason is
# recorded here, in plain words, when the step gives up; the entry script reads it
# with failure_reason(). generate() clears it first, so a reason never outlives the
# run that produced it. (Same code as the search tool's report.py; each skill keeps
# its own copy because a skill must work installed on its own.)
_FAIL_TAIL_CHARS = 200
_failures: dict = {}


# What a CLI message may NOT carry into the closing lines. The tail of the `claude` command's
# message is relayed to the user
# word for word, and lands in the run's status file, which a user may paste into a
# chat or a ticket. Raw, it can hold terminal colour codes and control characters,
# the user's home folder (their account name) and key-shaped strings. All three are
# removed BEFORE the tail is cut: cutting first could leave half a key visible.
#   * ANSI escapes: CSI (colours, cursor), OSC (titles, ended by BEL or ESC \), and
#     the two-character escapes. Then every remaining control character (C0, DEL
#     and C1) becomes a space, and whitespace collapses to one space.
#   * The home folder: this account's own home, then any /Users/<name>, /home/<name>
#     or C:\Users\<name> (the message may come from another account's path, or the
#     tool may run as a different user than the one whose path is quoted). A name may
#     hold spaces, up to eight words (four words leaked the
#     tail of the name); a name longer than that still leaks its tail, the bound
#     keeps ordinary words that sit before a slash from being swallowed.
#   * Secrets, replaced by [hidden]: `sk-` keys, `apify_api_` tokens, a JWT (three
#     dot-separated parts, the first starting `eyJ`), the value of a `token=`, `key=`,
#     `secret=`, `password=` or `auth=` setting or query parameter when it is 16 or
#     more characters (the name is kept), and any run of 32 or more base64, base64url
#     or hex characters that holds a digit (a real key or hash always does; a long
#     word or path without a digit stays readable). A long run of the form NAME=VALUE
#     keeps its NAME and hides the VALUE. Long ids that hold a digit are hidden too:
#     a disclosed, accepted trade-off.
#   * A key split by a line break: a terminal wraps a long key
#     onto two lines, and turning the break into a space first left the second half
#     shown. A control character or line break that sits BETWEEN TWO CHARACTERS OF A
#     TOKEN is turned into _JOIN, which the secret patterns treat as a token character,
#     so the key is whole when it is matched and whole when it is hidden. Any _JOIN
#     that is not inside a secret becomes a space again, so ordinary words on two
#     lines stay two words. The length tests count the real characters only. The join
#     is made only when what follows the break looks like key material: it holds a
#     digit (a real key always does, the same test the long-run rule uses) or it runs
#     12 or more token characters. WHY: a key that ends a line is usually followed by
#     ordinary words ("invalid key sk-... / Please run /login"), and a plain word is
#     not part of the key. The cost, disclosed: a key cut by a break so that its last
#     piece is a digit-free fragment of 11 characters or fewer keeps that piece shown.
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-Z\\-_]")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_JOIN = "\ue000"
_TOKEN_EDGE_RE = re.compile(
    r"(?<=[A-Za-z0-9+/=_.-])[\x00-\x1f\x7f-\x9f]+"
    r"(?=[A-Za-z0-9+/=_.-]*\d|[A-Za-z0-9+/=_.-]{12})")
_HOME_RE = re.compile(
    r"(?:(?:/Users|/home)/|[A-Za-z]:[\\/]Users[\\/])"
    r"(?:[^\\/\s:;,'\"()<>]+(?: [^\\/\s:;,'\"()<>]+){0,7}(?=[\\/])|[^\\/\s:;,'\"()<>]+)")


def _hide_if_real_length(n):
    """A replacement function: [hidden] when the match holds at least n real
    characters (the _JOIN marks do not count), else the match as it was."""
    def sub(m):
        return "[hidden]" if len(m.group(0).replace(_JOIN, "")) >= n else m.group(0)
    return sub


def _hide_assigned_value(m):
    """NAME=VALUE where the name says secret: keep the name, hide the value (16 or
    more real characters)."""
    if len(m.group(2).replace(_JOIN, "")) < 16:
        return m.group(0)
    return m.group(1) + "[hidden]"


def _hide_long_run(m):
    """A run of 32 or more token characters with a digit. NAME=VALUE keeps its NAME
    and hides the VALUE; a base64 run that only ends in padding (`==`) is hidden whole."""
    s = m.group(0)
    if len(s.replace(_JOIN, "")) < 32:
        return s
    named = re.match(r"([A-Za-z_][A-Za-z0-9_" + _JOIN + r"-]{0,63}=)(.*)$", s, re.S)
    if named and named.group(2).strip("="):
        return named.group(1) + "[hidden]"
    return "[hidden]"


_SECRET_RULES = (
    (re.compile("(?<![A-Za-z0-9" + _JOIN + "])sk-[A-Za-z0-9_" + _JOIN + "-]{8,}"),
     _hide_if_real_length(11)),
    (re.compile("apify_api_[A-Za-z0-9_" + _JOIN + "-]{6,}"), _hide_if_real_length(16)),
    (re.compile("(?<![A-Za-z0-9_" + _JOIN + "-])eyJ[A-Za-z0-9_" + _JOIN + "-]+\\.[A-Za-z0-9_"
                + _JOIN + "-]+\\.[A-Za-z0-9_" + _JOIN + "-]*"), _hide_if_real_length(0)),
    (re.compile("(?i)((?<![A-Za-z0-9_." + _JOIN + "-])[A-Za-z0-9_." + _JOIN + "-]{0,40}?"
                "(?:token|key|secret|passw(?:or)?d|pwd|auth|credential|signature)"
                "[A-Za-z0-9_." + _JOIN + "-]{0,40}=[\"']?)([A-Za-z0-9+/=_.~%" + _JOIN + "-]{16,})"),
     _hide_assigned_value),
    (re.compile("(?<![A-Za-z0-9+/=_" + _JOIN + "-])(?=[A-Za-z0-9+/=_" + _JOIN + "-]*\\d)"
                "[A-Za-z0-9+/=_" + _JOIN + "-]{32,}"), _hide_long_run),
)


def _clean_cli_text(text) -> str:
    """A CLI message made safe to show: no escapes or control characters, the home
    folder as ~, secrets as [hidden], one line. See the note above _ANSI_RE."""
    t = _ANSI_RE.sub("", str(text or "")).replace(_JOIN, " ")
    t = _TOKEN_EDGE_RE.sub(_JOIN, t)
    t = _CONTROL_RE.sub(" ", t)
    home = os.path.expanduser("~")
    if len(home) > 1 and home not in ("/", "~"):
        t = t.replace(home, "~")
    t = _HOME_RE.sub("~", t)
    for rx, fn in _SECRET_RULES:
        t = rx.sub(fn, t)
    return " ".join(t.replace(_JOIN, " ").split())


def _short_tail(text) -> str:
    """The last _FAIL_TAIL_CHARS characters of a CLI message, on one line, cleaned.

    WHY the tail: a CLI error ends with what went wrong ("Not logged in"), and
    an unexpected banner comes first. WHY cut at all: the message is relayed to the
    user word for word, so it stays short. WHY cleaned first: see _clean_cli_text.
    Dashes are plain hyphens (copy rule)."""
    t = _clean_cli_text(text).replace("\u2014", "-").replace("\u2013", "-")
    return t if len(t) <= _FAIL_TAIL_CHARS else "..." + t[-_FAIL_TAIL_CHARS:]


def _cli_message(result) -> str:
    """What a failed `claude` call said: its stderr, or its stdout when stderr is
    empty (`claude --print` can put its error on stdout, for example when it is
    not logged in), cut to a short tail."""
    return _short_tail((result.stderr or "").strip() or (result.stdout or "").strip())


def failure_reason(*labels) -> str:
    """The plain reason a report was not written, or "" when none was recorded.
    With labels, the first of those reports that failed; without, any."""
    keys = labels or tuple(_failures)
    for k in keys:
        if _failures.get(k):
            return _failures[k]
    return ""


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
    last_plain = "the Claude Code command-line tool was not run"
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
            print(f"[report] `claude` CLI not found, so {label} is not generated.")
            _failures[label] = ("the Claude Code command-line tool (claude) was not "
                                "found on this computer")
            return ""
        except Exception as exc:  # noqa: BLE001
            last_reason = f"unexpected error: {exc}"
            last_plain = (f"the Claude Code command-line tool could not be run "
                          f"({_short_tail(exc)})")
            print(f"[report] {label}: {last_reason} (attempt {attempt}/{max_attempts})")
            continue

        if result.returncode != 0:
            said = _cli_message(result)
            last_reason = f"non-zero exit ({result.returncode})" + (f": {said}" if said else "")
            last_plain = (f"the Claude Code command-line tool failed (exit {result.returncode})"
                          + (f": {said}" if said else ""))
            print(f"[report] {label}: {last_reason} (attempt {attempt}/{max_attempts})")
            continue

        text = (result.stdout or "").strip()
        if not text:
            last_reason = "empty output"
            last_plain = "the Claude Code command-line tool returned no text"
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
            _failures.pop(label, None)
            return cleaned
        last_reason = reason
        last_plain = (f"the Claude Code command-line tool answered, but it did not "
                      f"give a usable report: {_short_tail(reason)}")
        print(
            f"[report] {label}: output failed report validation ({reason}); "
            f"attempt {attempt}/{max_attempts}"
            + ("; retrying" if attempt < max_attempts else "")
        )

    print(f"[report] {label}: no valid report after {max_attempts} attempts "
          f"({last_reason}), not generated.")
    _failures[label] = last_plain
    return ""


# ---------------------------------------------------------------------------
# Reddit neutral set-aside -> optional "general topics & questions" appendix (E3)
# ---------------------------------------------------------------------------

_NEUTRAL_MIN_ITEMS = 3   # fewer than this: just stored for rigour, no section
_TOPICS_REPORT = "E3_reddit_topics.md"


def _drop_old_topics_report(workdir):
    """Remove the Reddit topics report an earlier run wrote, and say so, when this run
    has nothing to write one from (the same rule and the same one console
    line as postprocess._drop_stale_outputs, which is also used for the Reddit set-aside file
    itself).

    WHY: the report is written from reddit_neutral.json and from nothing else. When a
    re-run in the same folder brings no neutral items (the file is removed then), or
    too few, the earlier run's report would stay in the folder looking like this run's:
    the app and the user's own reading would show last time's discussion topics as this
    time's. A fixed path, never a search: only the file this function itself writes."""
    p = Path(workdir) / _TOPICS_REPORT
    try:
        p.unlink()
    except FileNotFoundError:
        return  # nothing left over: the usual case
    except OSError as exc:
        print(f"   report: WARNING: could not remove the Reddit topics report an earlier "
              f"run left in this folder ({exc}); it may be old")
        return
    print("   report: removed the Reddit topics report an earlier run left in this "
          "folder, so it cannot be mistaken for this run's")


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
        _drop_old_topics_report(workdir)
        return None
    try:
        items = json.loads(neutral_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        _drop_old_topics_report(workdir)
        return None
    items = [r for r in items if isinstance(r, dict) and (r.get("text") or "").strip()]
    if len(items) < _NEUTRAL_MIN_ITEMS:
        _drop_old_topics_report(workdir)
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
        "# General discussion and questions (Reddit)",
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
        theme_md = _run_claude(theme_prompt, model, "the Reddit discussion themes",
                               max_attempts=2)

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

    out_path = workdir / _TOPICS_REPORT
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
    _failures.clear()

    if not shutil.which("claude"):
        print(
            "[report] claude CLI not found, so the pain-points and trust-signals reports "
            "were not generated; "
            "run with any CLI LLM available"
        )
        for _lbl in ("the pain-points report", "the trust-signals report"):
            _failures[_lbl] = ("the Claude Code command-line tool (claude) was not "
                               "found on this computer")
        return {"E1": None, "E2": None}

    resolved_model = (cfg.get("report_model") or "").strip() or _DEFAULT_MODEL

    facts_path = workdir / "synthesis_facts.json"
    if not facts_path.exists():
        print("[report] the analysis the pain-points and trust-signals reports are written "
              "from was not found in this folder, so the reports are skipped")
        for _lbl in ("the pain-points report", "the trust-signals report"):
            _failures[_lbl] = ("the analysis the report is written from "
                               "was not found")
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
    e1_text = _run_claude(e1_prompt, resolved_model, "the pain-points report")
    if e1_text:
        low_bank = facts.get("low_star_quotes", [])
        # Measure bypass rate on the RAW output (before substitution)
        n_inline_e1 = quote_tool.count_inline_quotes(e1_text)
        # Paste [[Q<n>]] refs -- verbatim by construction, so provenance passes
        e1_text, n_pasted_e1, n_bad_e1 = quote_tool.paste_refs(e1_text, low_bank)
        print(
            f"[report] pain-points report quotes: {n_pasted_e1} pasted via [[Q]] ref, "
            f"{n_inline_e1} typed inline (bypassed the tool), "
            f"{n_bad_e1} bad refs"
        )
        e1_audit = {"pasted": n_pasted_e1, "inline_typed": n_inline_e1, "bad_refs": n_bad_e1}
        # Provenance check on the now-substituted text (mainly catches any
        # inline-typed fabrication that bypassed the [[Q]] tool)
        e1_note = _provenance_note(e1_text, low_bank)
        if e1_note:
            n_unmatched = sum(1 for line in e1_note.splitlines() if line.startswith('- "'))
            print(f"[report] provenance: {n_unmatched} pain-points quote(s) unmatched, disclosure appended")
            e1_text = e1_text + e1_note
        e1_out = workdir / "E1_pain_points.md"
        e1_out.write_text(e1_text, encoding="utf-8")
        e1_path = e1_out
        print(f"   report -> E1_pain_points.md  ({len(e1_text)} chars)")

    # --- E2 ---
    e2_path = None
    e2_audit: dict = {}
    print("   report: generating E2_trust_signals.md ...")
    e2_text = _run_claude(e2_prompt, resolved_model, "the trust-signals report")
    if e2_text:
        high_bank = facts.get("high_star_quotes", [])
        # Measure bypass rate on the RAW output (before substitution)
        n_inline_e2 = quote_tool.count_inline_quotes(e2_text)
        # Paste [[Q<n>]] refs -- verbatim by construction, so provenance passes
        e2_text, n_pasted_e2, n_bad_e2 = quote_tool.paste_refs(e2_text, high_bank)
        print(
            f"[report] trust-signals report quotes: {n_pasted_e2} pasted via [[Q]] ref, "
            f"{n_inline_e2} typed inline (bypassed the tool), "
            f"{n_bad_e2} bad refs"
        )
        e2_audit = {"pasted": n_pasted_e2, "inline_typed": n_inline_e2, "bad_refs": n_bad_e2}
        # Provenance check on the now-substituted text
        e2_note = _provenance_note(e2_text, high_bank)
        if e2_note:
            n_unmatched = sum(1 for line in e2_note.splitlines() if line.startswith('- "'))
            print(f"[report] provenance: {n_unmatched} trust-signals quote(s) unmatched, disclosure appended")
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

    # What the automated check found goes at the top of each report
    # this call wrote, last, so the folder line names every report file written.
    for _p in (e1_path, e2_path):
        if _p is not None:
            stamp_check_block(workdir, _p.name)

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


# ---------------------------------------------------------------------------
# What the automated quote check found, written where the code owns the words
# ---------------------------------------------------------------------------
# WHY: the check's findings used to reach the user only through
# Claude's reply. Four rounds of SKILL wording asked Claude to pass them on
# "exactly as printed", and every live session lost something: the folder and
# file names, a "tidied" line, once "treat as unverified" softened into
# something reassuring. So the findings are now written by code onto the two
# surfaces the user reads directly: the top of each checked report file
# (stamp_check_block) and the local app (the viewer reads `check --json`).
# A reply that paraphrases can no longer lose them, because they are in the file.
#
# One wording, one count: every line comes from check_data / _file_line, and
# the count is the one the check wrote into the file's own end note
# (_provenance_note); nothing is recounted. A report written by an earlier version
# has no top block; its findings are read from the same end note.

_NOTE_HEADING = "Provenance note (automated check)"
_NOTE_COUNT_RE = re.compile(r"(\d+) quoted passage\(s\) below could not be matched")
_REPORT_FILES = ("E1_pain_points.md", "E2_trust_signals.md")
# Every report file the report step can write, in the order it writes them.
# E3 is written only when Reddit is a source; its topics are not quote-checked.
_ALL_REPORT_FILES = _REPORT_FILES + ("E3_reddit_topics.md",)
# The heading of the block stamp_check_block puts at the top of a checked report.
TOP_HEADING = "## What the automated check found"


# The folder is named the way the user knows it, never a machine path, in one
# of two wordings.  A run folder directly in a research repository (see
# _in_research_repository) is "the folder pain_acme-fitness_2026-01-01 in your
# research repository", because the repository's own folder has another name
# on each machine.  Any other run folder is "marketing-research/
# pain_acme-fitness_2026-01-01": the research folder's name and the run folder's
# name. os.path.abspath is lexical, so a folder reached through a link keeps
# the name the user gave it.
def _short_folder(workdir) -> str:
    p = Path(os.path.abspath(str(workdir)))
    if _in_research_repository(p):
        return f"the folder {p.name} in your research repository"
    return f"{p.parent.name}/{p.name}" if p.parent.name else p.name


# A run folder directly in a research repository is named without the
# repository's own folder.  WHY: that name differs between machines.  A cloud
# month is written on the cloud machine, whose clone is named after the
# repository ("research-repo/pain_acme-fitness_2026-10-09" in a cloud test), and the report is saved
# to the repository with that sentence
# in it; on the user's computer the same month sits in their own research
# folder, which may have another name.  "the folder <run> in your research
# repository" is true on every machine.
# WHY these two markers: they are the ones research_sync.py itself leaves and
# reads, as plain files, so no git call is needed to write one sentence.  .git
# in the parent: the research folder is its own git folder (research_sync's
# "linked" means exactly that, plus a remote).  .marketing-scraping/plugin/VERSION:
# research_sync.py cloud-setup wrote it, and repeat.py schedule --mode cloud
# checks the same file; the cloud routine runs the tools from that copy, so
# every cloud month has it.  A folder with only one of the two (a research
# folder that is a git folder for the user's own reasons, or a cloud setup
# never linked) keeps the <research folder>/<run folder> wording, which is
# true on the one machine that has it.
def _in_research_repository(p) -> bool:
    parent = p.parent
    try:
        return ((parent / ".git").exists()
                and (parent / ".marketing-scraping" / "plugin" / "VERSION").is_file())
    except OSError:
        return False


def _folder_line(workdir, written) -> str:
    """Where the written reports are: the folder as the user knows it, and each file in it."""
    where = _short_folder(workdir)
    if not written:
        return f"There is no written report in {where} yet."
    if len(written) == 1:
        return f"The written report is in {where}: {written[0]}."
    return f"The written reports are in {where}: {', '.join(written)}."


def _end_note_at(text) -> int:
    """Where the check's own end note starts, or -1. The last heading counts: the
    note is always appended after the report, and the top block names the heading
    only in quotation marks, never as a heading."""
    return text.rfind("### " + _NOTE_HEADING)


def _file_line(name, text) -> str:
    """One plain line: what the automated quote check found in this report file."""
    at = _end_note_at(text)
    m = _NOTE_COUNT_RE.search(text, at) if at >= 0 else None
    if at >= 0 and not m:
        # A note is there but its count cannot be read: never say that
        # nothing was flagged.
        return (f"{name}: the automated check left a note at the end of that "
                f"file, under the heading \"{_NOTE_HEADING}\". Read it there.")
    if not m:
        # Said positively, and true for a report that quotes no review
        # ("if there were any"); "looked at" because passages under five words
        # are not checked (_provenance_note).
        return (f"{name}: nothing in it is flagged as unverified. Every quoted passage "
                f"the automated check looked at, if there were any, matched the "
                f"collected reviews word for word.")
    n = int(m.group(1))
    one = n == 1
    return (f"{name}: {n} quoted passage{'' if one else 's'} could not be matched word for "
            f"word to the collected reviews, so treat {'it' if one else 'them'} as "
            f"unverified. {'It is' if one else 'They are'} listed at the end of that file, "
            f"under the heading \"{_NOTE_HEADING}\".")


def _read(p) -> str:
    return Path(p).read_text(encoding="utf-8", errors="replace")


def _has_top_block(text) -> bool:
    """True when the block sits at the top: as the first heading, or right after the
    report's own first-line title."""
    lines = text.split("\n", 3)
    if lines and lines[0].strip() == TOP_HEADING:
        return True
    return (len(lines) >= 3 and lines[0].startswith("# ") and not lines[1].strip()
            and lines[2].strip() == TOP_HEADING)


def check_data(workdir) -> dict:
    """What the automated quote check found, for the app and for `check --json`:
    {"folder_line": str, "files": [{"file", "line", "present", "top_block"}]}.
    top_block says whether the file already starts with the block (a report from
    an earlier version does not, so the app shows the line itself)."""
    workdir = Path(workdir)
    out = {"folder_line": _folder_line(
               workdir, [n for n in _ALL_REPORT_FILES if (workdir / n).exists()]),
           "files": []}
    for name in _REPORT_FILES:
        p = workdir / name
        if not p.exists():
            out["files"].append({"file": name, "present": False, "top_block": False,
                                 "line": f"{name}: this report file is not in the folder."})
            continue
        text = _read(p)
        out["files"].append({"file": name, "present": True,
                             "top_block": _has_top_block(text),
                             "line": _file_line(name, text)})
    return out


def check_lines(workdir) -> list:
    """Where the reports are, then one plain line per checked report file:
    what the automated quote check found."""
    d = check_data(workdir)
    return [d["folder_line"]] + [f["line"] for f in d["files"]]


def cli_lines(workdir) -> list:
    """What `check` prints: check_lines, then one line naming any report written
    before the check was put at the top of the file. WHY: the SKILL tells Claude to
    say each report starts with what the check found; for an older report that is
    not true, and the first live session on an old folder said it anyway. This line
    is the code's own signal, so Claude does not have to tell old from new."""
    d = check_data(workdir)
    old = [f["file"] for f in d["files"] if f["present"] and not f["top_block"]]
    out = check_lines(workdir)
    if old:
        one = len(old) == 1
        out.append(f"{', '.join(old)}: written before reports started with what the "
                   f"automated check found, so {'the top of that file does' if one else 'the tops of those files do'} "
                   f"not show it. The note named above has it, and the app shows these "
                   f"lines above the report.")
    return out


def _strip_top_block(text) -> str:
    """The text without a top block stamped earlier (so a stamp never doubles)."""
    if not _has_top_block(text):
        return text
    start = text.find(TOP_HEADING)
    end = text.find("\n---\n", start)
    if end < 0:
        return text
    return text[:start] + text[end + len("\n---\n"):].lstrip("\n")


def stamp_check_block(workdir, name) -> bool:
    """Put what the automated quote check found at the top of report file `name`:
    the folder line (where the reports are, each file named) and this file's own
    line, both from check_data, so the words are the ones `check` prints. The full
    end note stays where it is. The block goes right after the report's first-line
    title, or first when there is none. Returns False when the file is missing or
    could not be rewritten (the run goes on; the end note is still there)."""
    p = Path(workdir) / name
    if not p.exists():
        return False
    try:
        text = _strip_top_block(_read(p))
        d = check_data(workdir)
        line = _file_line(name, text)
        block = f"{TOP_HEADING}\n\n{d['folder_line']}\n\n{line}\n\n---\n"
        first, sep, rest = text.partition("\n")
        if first.startswith("# ") and sep:
            text = f"{first}\n\n{block}\n{rest.lstrip(chr(10))}"
        else:
            text = f"{block}\n{text}"
        p.write_text(text, encoding="utf-8")
        return True
    except OSError as exc:
        print(f"[report] could not add what the automated check found to the top of "
              f"{name} ({exc})")
        return False


if __name__ == "__main__":
    import argparse
    _ap = argparse.ArgumentParser(description="What the automated quote check found in "
                                              "a run's written reports (reads files only).")
    _ap.add_argument("command", choices=["check"])
    _ap.add_argument("--workdir", required=True, help="the run folder")
    _ap.add_argument("--json", action="store_true", help="print it as JSON (the app reads this)")
    _args = _ap.parse_args()
    if _args.json:
        print(json.dumps(check_data(_args.workdir), ensure_ascii=False))
    else:
        print("\n".join(cli_lines(_args.workdir)))
