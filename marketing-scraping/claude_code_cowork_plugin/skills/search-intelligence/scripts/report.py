"""
Report generator for the search-intelligence tool.

Reads synthesis_facts.json (produced by extractor.py) and generates one
analyst report using a local `claude` CLI call:
  S1_search_landscape.md  -- search demand narrative, tables, competitive map

Architecture:
  Code owns every number; the LLM owns exactly ONE narrative blob.
  Code renders ALL numeric tables from the facts into fixed template
  placeholders. The LLM writes ONE markdown narrative (no numbers it
  invents; it may reference the tables' figures) slotted into {{NARRATIVE}}.

Design rules:
  - The LLM's only job is to cluster, label, and narrate -- using the
    pre-counted facts provided. It must never count, never assert a figure
    not traceable to synthesis_facts.json.
  - Small-N honesty: directional findings are called directional.
  - The LLM runs as a local `claude` CLI call (--print), never a direct
    API call. No OpenAI/Anthropic key needed; only the user's Apify token.
  - Copy rules: no em-dashes/AI-tells, no invented figures, never call this
    "free Ahrefs" or "free SEO tool", no backlink claims.

Integration seam (called by search_intel.py):
  from scripts import report
  report.generate(workdir, cfg)   # -> Path | None

Stdlib only -- no pip install needed.
"""

import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Module-level paths
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_TEMPLATES_DIR = _HERE.parent / "templates" / "reports"

# Capable default (clustering/writing the analyst report is a judgment task).
# Overridable via cfg `report_model`.
_DEFAULT_MODEL = "sonnet"

# NO TIMEOUT on the LLM call. Report generation is non-deterministic: a
# capable model writing a full analyst report legitimately takes minutes and
# wall time varies run to run. That slowness is normal work, not a hang.
# Timeouts belong to DETERMINISTIC, latency-bound calls (e.g. Apify HTTP).


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_outer_code_fence(text: str) -> str:
    """Remove a single code fence that wraps the ENTIRE output, if present.

    GUARDED: fires only when the first line is a bare fence-open (``` optionally
    followed by a single language token like ```markdown) AND the last line is a
    bare closing fence. Only those two boundary lines are removed. Output that is
    not fence-wrapped is returned unchanged.
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

    GUARDED: strips only when the text does NOT already start with a heading AND
    a markdown heading exists later. If there is no heading anywhere, returns
    the text unchanged.
    """
    stripped = text.strip()
    if stripped.startswith("#"):
        return stripped
    lines = stripped.split("\n")
    for i, line in enumerate(lines):
        if re.match(r"^#{1,6}\s", line):
            return "\n".join(lines[i:]).strip()
    return text


# Matches the ★ Insight block opener emitted by the claude CLI's operator output style.
# Example: `★ Insight ─────────────────────────────────────`
# U+2605 = ★ (BLACK STAR), U+2500 = ─ (BOX DRAWINGS LIGHT HORIZONTAL)
_INSIGHT_OPENER_RE = re.compile(r"^`★\s+Insight\s+─+`\s*$")
# Matches the closing delimiter: a line that is a backtick + run of ─ + backtick
_INSIGHT_CLOSER_RE = re.compile(r"^`─+`\s*$")


def _strip_insight_blocks(text: str) -> str:
    """Remove '★ Insight' meta-commentary blocks emitted by the claude CLI output style.

    The claude operator's output style sometimes wraps educational asides in a
    box-drawing block:
        `★ Insight ─────...─────`
        Commentary text here.
        `─────...─────`
    This is AI-tell meta-commentary that must never appear in client-facing copy.

    GUARDED: fires only when the opener pattern is actually present. A clean
    narrative (no ★ Insight lines) is returned unchanged. Both the opener, the
    block body, and the closer are removed. Standalone leftover closer lines
    (matching _INSIGHT_CLOSER_RE without a preceding opener) are also stripped
    as defense-in-depth against partial matches.
    """
    if "★" not in text and "─" not in text:
        return text  # Fast path: no star (★) and no box-drawing char (─), nothing to strip

    lines = text.split("\n")
    out = []
    inside = False
    for line in lines:
        if _INSIGHT_OPENER_RE.match(line):
            inside = True
            continue  # drop opener
        if inside:
            if _INSIGHT_CLOSER_RE.match(line):
                inside = False  # drop closer, exit block
            # drop all lines inside the block
            continue
        if _INSIGHT_CLOSER_RE.match(line):
            # standalone leftover closer (defense-in-depth)
            continue
        out.append(line)

    # Collapse runs of 3+ blank lines down to 2 (block removal may leave gaps)
    result = re.sub(r"\n{3,}", "\n\n", "\n".join(out))
    return result.strip()


def _looks_like_report(text: str) -> tuple:
    """Validate that CLI output is an actual report, not a subagent side-effect.

    Returns (ok, reason). A valid narrative starts with a markdown heading
    after the strippers run. Two failure modes are rejected:
    1. The claude CLI sometimes builds and publishes an Artifact instead of
       returning text, coming back as a chatty summary with a claude.ai URL.
    2. A refusal or bare chatter with no heading at all.
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
    """Replace em-dashes in text with ' - '.

    House copy rule: no em-dashes in our authored voice. Applied to both the
    narrative (LLM output) and any template scaffold text passed to the LLM
    so it does not copy our em-dashes as a style to emulate.
    """
    return text.replace("—", " - ")


# ---------------------------------------------------------------------------
# Template loader
# ---------------------------------------------------------------------------

def _load_template(name: str) -> str:
    """Load a report template by filename from the templates/reports/ directory."""
    path = _TEMPLATES_DIR / name
    if not path.exists():
        print(f"[report] WARNING: template not found at {path}")
        return ""
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Scaffold renderer -- CODE renders ALL numeric tables (never the LLM)
# ---------------------------------------------------------------------------

def _fmt(v) -> str:
    """Format a fact value for display: null -> 'n/a', else str()."""
    return "n/a" if v is None else str(v)


def _volume_cell(r) -> str:
    """The Volume cell of a keyword row. A keyword Semrush had no data for (the one rule,
    postprocess.semrush_no_data) shows "no data from Semrush": never a 0 it did
    not measure, and not the bare "n/a" of a value that is only missing."""
    import postprocess as _PP  # noqa: E402
    return "no data from Semrush" if _PP.semrush_no_data(r) else _fmt(r.get("volume"))


def _render_scaffold(facts: dict) -> dict:
    """Render the four deterministic table strings from synthesis_facts.

    All values are lifted directly from facts with no re-computation.
    Null cells display as 'n/a'. Order matches the facts file (already
    volume-descending for keyword_table). Returns a dict:
      KEYWORD_DEMAND_TABLE, OPPORTUNITY_TABLE,
      COMPETITIVE_MAP_TABLE, COVERAGE_CAVEATS
    """
    # --- KEYWORD_DEMAND_TABLE ---
    rows = facts.get("keyword_table", [])
    kd_lines = [
        "| Keyword | Volume | Difficulty | Ref. domains (median) | CPC (USD) | Competition | In SERP |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        kw = r.get("keyword", "")
        has_serp = r.get("has_serp")
        serp_str = "Yes" if has_serp is True else ("No" if has_serp is False else "n/a")
        kd_lines.append(
            f"| {kw} | {_volume_cell(r)} | {_fmt(r.get('keyword_difficulty'))} "
            f"| {_fmt(r.get('referring_domains_median'))} "
            f"| {_fmt(r.get('cpc_usd'))} | {_fmt(r.get('competition'))} | {serp_str} |"
        )
    keyword_demand_table = "\n".join(kd_lines)

    # --- OPPORTUNITY_TABLE ---
    opps = facts.get("low_difficulty_opportunities", [])
    if opps:
        opp_lines = [
            "| Keyword | Volume | Difficulty |",
            "| --- | --- | --- |",
        ]
        for r in opps:
            opp_lines.append(
                f"| {r.get('keyword', '')} | {_volume_cell(r)} "
                f"| {_fmt(r.get('keyword_difficulty'))} |"
            )
        opportunity_table = "\n".join(opp_lines)
    else:
        opportunity_table = "No keywords below the difficulty threshold in this sample."

    # --- COMPETITIVE_MAP_TABLE ---
    cmap = facts.get("competitive_map", [])
    if cmap:
        cm_lines = [
            "| Domain | Keywords owned | Avg position | Best position |",
            "| --- | --- | --- | --- |",
        ]
        for r in cmap:
            cm_lines.append(
                f"| {r.get('domain', '')} | {_fmt(r.get('keywords_owned'))} "
                f"| {_fmt(r.get('avg_position'))} | {_fmt(r.get('best_position'))} |"
            )
        competitive_map_table = "\n".join(cm_lines)
    else:
        competitive_map_table = "No SERP data captured."

    # --- COVERAGE_CAVEATS ---
    coverage = facts.get("coverage", {})
    sources = facts.get("sources", {})
    caveats = facts.get("caveats", [])
    params = facts.get("params", {})

    def _src_date(src_dict: dict) -> str:
        return src_dict.get("scraped_date") or "date not recorded"

    metrics_src = sources.get("metrics", {})
    serp_src = sources.get("serp", {})

    cov_lines = [
        f"**Keyword universe:** {_fmt(coverage.get('keyword_universe'))} keywords expanded.",
        f"**Priced keywords** (volume, difficulty, CPC available): {_fmt(coverage.get('priced_keywords'))}.",
        *([f"**No data from Semrush:** {coverage['no_data_keywords']} "
           f"{'keyword' if coverage['no_data_keywords'] == 1 else 'keywords'} "
           f"(volume unknown, not zero)."] if coverage.get("no_data_keywords") else []),
        f"**SERP coverage:** {_fmt(coverage.get('serp_keywords'))} keywords.",
        f"**Overlap (priced and SERP):** {_fmt(coverage.get('priced_and_serp'))} keywords.",
        f"**Distinct SERP domains:** {_fmt(coverage.get('unique_domains'))}.",
        "",
        f"**Metrics source:** {_fmt(metrics_src.get('source')) if metrics_src else 'n/a'} "
        f"(scraped {_src_date(metrics_src)}).",
        f"**SERP source:** {_fmt(serp_src.get('source')) if serp_src else 'n/a'} "
        f"(scraped {_src_date(serp_src)}).",
    ]

    if params.get("opportunity_kd_max") is not None:
        cov_lines.append(
            f"**Opportunity threshold:** keywords with difficulty <= "
            f"{params['opportunity_kd_max']} qualify as winnable."
        )

    if caveats:
        cov_lines.append("")
        cov_lines.append("**Caveats:**")
        for c in caveats:
            cov_lines.append(f"- {c}")

    coverage_caveats = "\n".join(cov_lines)

    return {
        "KEYWORD_DEMAND_TABLE": keyword_demand_table,
        "OPPORTUNITY_TABLE": opportunity_table,
        "COMPETITIVE_MAP_TABLE": competitive_map_table,
        "COVERAGE_CAVEATS": coverage_caveats,
    }


# ---------------------------------------------------------------------------
# Prompt builder (pure -- no I/O, testable without LLM)
# ---------------------------------------------------------------------------

_S1_PREAMBLE = (
    "You are writing a search landscape analyst report. "
    "You are given pre-counted keyword data and a set of rendered tables. "
    "Your task is to write ONE narrative block that clusters the keywords by "
    "search intent, interprets the competitive landscape, and gives an honest "
    "assessment of the search opportunity. "
    "Rules: "
    "(a) cluster and label only -- you may invent NO numbers and NO figures; "
    "every number you write must appear verbatim in the facts or the tables "
    "provided. Do NOT do arithmetic or derive new numbers (for example, do not "
    "subtract priced from universe to state how many are 'unpriced' -- say 'most "
    "of the universe' or cite the two counts as given). Do NOT invent target or "
    "recommended quantities (do not say 'target 20 to 30 keywords' -- say 'a "
    "larger sample'). When referring to a result's placement, cite its actual "
    "position number from the table (say 'position 3'), not a rounded band like "
    "'top 10'; "
    "(b) do not restate or rebuild the tables -- the tables are rendered by code "
    "and will be placed below your narrative; you may reference their figures "
    "but must not reproduce the whole table in your prose; "
    "(c) state small-N and directional findings honestly -- if the SERP sample "
    "covers only a few keywords, say so and call the findings directional; "
    "(d) write in plain professional English: no em-dashes, no en-dashes, and "
    "no double-hyphen dashes (use commas, periods, or parentheses instead), and "
    "no AI-cliche filler (no 'delve', 'unlock', 'empower', 'leverage', etc.); "
    "(e) do NOT call this report, this tool, or its output 'free' -- this tool "
    "has real costs and is not a free version of any other product; "
    "(f) do NOT invoke domain authority or any per-domain authority score -- "
    "this tool has none. Do NOT claim to crawl, measure, or index backlinks. "
    "You MAY cite the 'median referring domains' figure the table provides as a "
    "link-competitiveness signal when explaining how hard a keyword is to rank "
    "for -- quote it exactly as given, and derive no other link metric from it; "
    "(g) do NOT name any paid SEO tool (Ahrefs, SEMrush, Moz, etc.) as "
    "something this replaces or competes with; "
    "(h) begin your output with a H2 heading (## Executive summary or similar) -- "
    "do NOT use a H1 heading; the report title is set by the template above your "
    "narrative slot; "
    "(i) output ONLY the report narrative -- do NOT add any 'Insight' block, "
    "educational note, box-drawing separator, or meta-commentary about your own "
    "process or method. No starred callouts, no bordered aside blocks, no "
    "reflection on structural choices. "
    "Output ONLY the narrative markdown -- no title, no tables, no preamble. "
    "The narrative will be inserted into a fixed template that already contains "
    "the numeric tables. Structure your output as: "
    "## Executive summary (2-4 bullets on what the data says), "
    "## Intent clusters (group the keywords by search intent with a sentence per "
    "cluster explaining the commercial signal), "
    "## Competitive landscape (interpret who owns the SERP and where the gaps "
    "are -- name the domains from the table, do not invent new ones), "
    "## Coverage and data quality (one short honest paragraph on sample size, "
    "what the data does and does not show, and what a follow-up run would add)."
)


def build_s1_prompt(facts: dict, scaffold: dict, cfg: dict, template_text: str) -> str:
    """Build the full S1 (search landscape) prompt.

    Pure function: no file I/O, no subprocess, no side effects.
    Accepts the raw template text and the pre-rendered scaffold dict.
    Returns the complete prompt string to pipe to the claude CLI.
    """
    # Derive a market label from cfg for context
    vertical = (cfg.get("vertical") or "").strip()
    seeds = cfg.get("seed_terms") or []
    if vertical:
        market_label = vertical
    elif seeds:
        seed_preview = ", ".join(str(s) for s in seeds[:3])
        market_label = seed_preview + (" ..." if len(seeds) > 3 else "")
    else:
        market_label = "the target market"

    today = datetime.date.today().isoformat()

    # Compact facts summary for the LLM (keyword list + coverage -- NOT the tables,
    # which are passed separately so the LLM sees the code-rendered backbone)
    import postprocess as _PP  # noqa: E402 (the one "no data" rule, not a row's own flag)
    facts_summary = json.dumps({
        "market": market_label,
        "date": today,
        "coverage": facts.get("coverage", {}),
        "serp_feature_summary": facts.get("serp_feature_summary", {}),
        "params": facts.get("params", {}),
        "caveats": facts.get("caveats", []),
        "keyword_list": [
            {
                "keyword": r.get("keyword"),
                # a keyword with no data shows its volume as unknown, never as a number
                "volume": "no data from Semrush" if _PP.semrush_no_data(r) else r.get("volume"),
                "keyword_difficulty": r.get("keyword_difficulty"),
                "cpc_usd": r.get("cpc_usd"),
                "competition": r.get("competition"),
                "has_serp": r.get("has_serp"),
            }
            for r in facts.get("keyword_table", [])
        ],
        "low_difficulty_opportunities": facts.get("low_difficulty_opportunities", []),
        "top_serp_domains": [r.get("domain") for r in facts.get("competitive_map", [])[:10]],
    }, ensure_ascii=False, indent=2)

    # Clean the template of any em-dashes so the LLM does not inherit them as style
    cleaned_template = _clean_template(template_text)

    parts = [
        _S1_PREAMBLE,
        "",
        f"## Facts (pre-counted -- trust these numbers; do not re-derive them)",
        "",
        facts_summary,
        "",
        "## Pre-rendered tables (code-generated backbone -- do NOT reproduce these as tables)",
        "",
        "### Keyword demand table",
        scaffold.get("KEYWORD_DEMAND_TABLE", ""),
        "",
        "### Winnable opportunities table",
        scaffold.get("OPPORTUNITY_TABLE", ""),
        "",
        "### Competitive map table (first 10 of " + str(len(facts.get("competitive_map", []))) + " domains)",
        "\n".join(
            scaffold.get("COMPETITIVE_MAP_TABLE", "").split("\n")[:12]
        ),
        "",
        "## Report template structure (for context -- you write ONLY the {{NARRATIVE}} slot)",
        "",
        cleaned_template,
        "",
        "## Your task",
        "",
        (
            f"Write the narrative for the {{{{NARRATIVE}}}} slot above. "
            f"Market: {market_label}. "
            "Follow all rules in the preamble. "
            "Output ONLY the narrative markdown starting with a H2 heading."
        ),
    ]
    # Clean the full assembled prompt of em-dashes so the LLM never sees them
    # as a style to emulate. The caveats in synthesis_facts.json may carry em-dashes
    # authored by the extractor; this normalises them at the prompt-building seam.
    return _clean_template("\n".join(parts))


# ---------------------------------------------------------------------------
# CLI subprocess
# ---------------------------------------------------------------------------

# Why a report was not written, kept for the run's closing lines.
# WHY: `_run_claude` returns "" for every kind of failure, so the entry script could
# only say "not generated" and the closing lines said nothing at all. The reason is
# recorded here, in plain words, when the step gives up; the entry script reads it
# with failure_reason(). generate() clears it first, so a reason never outlives the
# run that produced it.
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

    The LLM runs as a local `claude` CLI call (--print), never a direct API
    call. NO timeout -- report generation is non-deterministic; normal runtime
    is minutes. --strict-mcp-config + empty --mcp-config starves the subprocess
    of the parent's MCP server stack (prevents blank browser windows opening
    on every call). --tools "" disables built-in tools as defense-in-depth.
    Degrades gracefully to "" on persistent failure or a missing CLI.
    """
    cmd = [
        "claude",
        "--print",
        "--output-format", "text",
        # FOUNDATIONAL FIX: stop the subprocess booting the parent's
        # MCP stack (browser servers -> blank windows, minutes of startup).
        "--strict-mcp-config",               # ignore ~/.claude.json MCP servers
        "--mcp-config", '{"mcpServers":{}}',   # ...and supply an empty set
        "--tools", "",           # defense-in-depth: no built-in tools either
        "--model", model,
        # Belt-and-suspenders against the operator's "★ Insight" output style
        # (--append-system-prompt confirmed present in this CLI version).
        # The deterministic _strip_insight_blocks post-processor is the guaranteed
        # fix; this flag is defense-in-depth at the source.
        "--append-system-prompt",
        (
            "Respond with only the requested markdown. "
            "Do not add Insight blocks, educational notes, box-drawing separators, "
            "or meta-commentary about your process or structural choices."
        ),
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

        # Deterministic output hygiene: unwrap a whole-report code fence,
        # then drop any conversational preamble before the first heading.
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
# Copy guard -- verify LLM output against house rules
# ---------------------------------------------------------------------------

# Phrases we never allow in LLM output regardless of prompt.
# Checked case-insensitively. A violation is rare (the prompt forbids them)
# but we verify output, never trust the prompt -- per the house rule that
# LLM output hygiene is enforced in code, not by asking nicely.
_COPY_GUARD_PHRASES = [
    "backlink",
    "free ahrefs",
    "free seo tool",
    "free semrush",
]

# The tool's own name variants. "free " adjacent to these is also a violation.
_TOOL_NAME_VARIANTS = [
    "search-intelligence",
    "search intelligence",
]


def _copy_guard(narrative: str) -> list:
    """Return a list of copy-rule violation strings found in the narrative.

    Checks case-insensitively for:
    - Any of _COPY_GUARD_PHRASES (backlink, free ahrefs, free seo tool, free semrush)
    - 'free ' adjacent to the tool's own name variants

    Returns an empty list when the narrative is clean. A non-empty list is a
    signal for the caller to review; this function does NOT edit the prose.
    """
    lower = narrative.lower()
    violations = []
    for phrase in _COPY_GUARD_PHRASES:
        if phrase in lower:
            violations.append(f"forbidden phrase: {phrase!r}")
    for name in _TOOL_NAME_VARIANTS:
        if f"free {name}" in lower:
            violations.append(f"forbidden phrase: 'free {name}'")
    return violations


# ---------------------------------------------------------------------------
# Provenance check -- numbers in narrative must trace to facts
# ---------------------------------------------------------------------------

# Matches integers, decimals, and percentages in text.
# Intentionally broad: catches "550,000", "18.97", "76", "31%", "0.07".
_NUMBER_RE = re.compile(r"\b[\d,]+(?:\.\d+)?%?\b")


def _provenance_check(narrative: str, facts: dict) -> str:
    """Check that every number in the narrative traces back to synthesis_facts.

    Extracts number-like tokens from the narrative (integers, decimals,
    percentages, volume-style numbers with commas). Normalizes commas before
    comparing (550,000 -> 550000). Checks each normalized token against:
      1. The string representation of the full facts JSON (exact string match).
      2. The set of float values parsed from the facts JSON (handles formatting
         variants like 8.20 vs 8.2 -- both parse to float 8.2 and both match).

    Returns "" when all numbers trace.
    Returns a warn-only markdown note (appended AFTER the report) listing
    untraceable numbers when any are found. The analysis prose above is never
    modified -- this is a disclosure, not an edit.
    """
    facts_str = json.dumps(facts, ensure_ascii=False)

    # Build a set of all number-like strings that appear in the facts (string check)
    facts_numbers: set = set()
    facts_floats: set = set()  # float check: catches 8.20 == 8.2 variants
    for m in _NUMBER_RE.finditer(facts_str):
        token = m.group(0).replace(",", "").rstrip("%")
        facts_numbers.add(token)
        try:
            facts_floats.add(float(token))
        except ValueError:
            pass

    untraceable = []
    seen_normalized: set = set()

    for m in _NUMBER_RE.finditer(narrative):
        raw = m.group(0)
        normalized = raw.replace(",", "").rstrip("%")

        # Skip if already checked
        if normalized in seen_normalized:
            continue
        seen_normalized.add(normalized)

        # Skip single-digit numbers -- too common to be meaningful provenance anchors
        if len(normalized) <= 1:
            continue

        # String match (exact, handles integers and already-normalized decimals)
        if normalized in facts_numbers:
            continue

        # Float match (handles trailing-zero variants: 8.20 -> 8.2, 550,000 -> 550000.0)
        try:
            if float(normalized) in facts_floats:
                continue
        except ValueError:
            pass

        untraceable.append(raw)

    if not untraceable:
        return ""

    n = len(untraceable)
    bullets = "\n".join(f"- {t}" for t in untraceable)
    return (
        f"\n\n---\n\n"
        f"### Provenance note (automated check)\n\n"
        f"{n} number(s) in the narrative above could not be matched to "
        f"synthesis_facts.json and should be reviewed:\n\n"
        f"{bullets}\n\n"
        f"This is an automated string check appended after generation; "
        f"the analysis above was not modified.\n"
    )


# ---------------------------------------------------------------------------
# Public API (called by search_intel.py)
# ---------------------------------------------------------------------------

def generate(workdir, cfg) -> "Path | None":
    """Generate the S1 search landscape report from synthesis_facts.json.

    Reads:  <workdir>/synthesis_facts.json  (written by extractor.py)
    Writes: <workdir>/S1_search_landscape.md

    Returns the Path of the written report, or None when not generated
    (synthesis_facts.json missing, or CLI error after retries).
    Graceful degradation: if synthesis_facts.json is absent, prints a clear
    message and returns None without crashing.
    """
    workdir = Path(workdir)
    _failures.clear()

    facts_path = workdir / "synthesis_facts.json"
    if not facts_path.exists():
        print("[report] the analysis the search landscape report is written from was not "
              "found in this folder, so the report is skipped")
        _failures["the search landscape report"] = ("the analysis the report is written from "
                                                    "was not found")
        return None

    facts = json.loads(facts_path.read_text(encoding="utf-8"))

    resolved_model = (cfg.get("report_model") or "").strip() or _DEFAULT_MODEL

    coverage = facts.get("coverage", {})
    print(
        f"   report: {coverage.get('keyword_universe', '?')} keywords total, "
        f"{coverage.get('priced_keywords', '?')} priced, "
        f"{coverage.get('serp_keywords', '?')} SERP  "
        f"model={resolved_model}"
    )

    template_text = _load_template("S1_search_landscape.md")

    scaffold = _render_scaffold(facts)

    prompt = build_s1_prompt(facts, scaffold, cfg, template_text)

    # Run the LLM. The selftest does NOT call this line (it tests _render_scaffold,
    # build_s1_prompt, _copy_guard, _provenance_check, and template assembly directly).
    print("   report: generating S1_search_landscape.md ...")
    narrative = _run_claude(prompt, resolved_model, "the search landscape report")

    if not narrative:
        return None

    # Post-process: strip Insight blocks (deterministic, GUARDED), clean em-dashes,
    # check copy rules, check provenance
    narrative = _strip_insight_blocks(narrative)
    narrative = _clean_template(narrative)

    violations = _copy_guard(narrative)
    if violations:
        print(f"[report] the search landscape report's copy check flagged "
              f"{len(violations)} violation(s):")
        for v in violations:
            print(f"   {v}")

    provenance_note = _provenance_check(narrative, facts)
    if provenance_note:
        n_flags = provenance_note.count("\n- ")
        print(f"[report] provenance: {n_flags} number(s) unmatched, disclosure appended")

    # Derive market label for the title
    vertical = (cfg.get("vertical") or "").strip()
    seeds = cfg.get("seed_terms") or []
    if vertical:
        market = vertical
    elif seeds:
        market = ", ".join(str(s) for s in seeds[:3])
        if len(seeds) > 3:
            market += " ..."
    else:
        market = "Market"

    # Assemble: fill all template placeholders
    report_text = template_text
    report_text = report_text.replace("{{market}}", market)
    report_text = report_text.replace("{{NARRATIVE}}", narrative + provenance_note)
    report_text = report_text.replace("{{KEYWORD_DEMAND_TABLE}}", scaffold["KEYWORD_DEMAND_TABLE"])
    report_text = report_text.replace("{{OPPORTUNITY_TABLE}}", scaffold["OPPORTUNITY_TABLE"])
    report_text = report_text.replace("{{COMPETITIVE_MAP_TABLE}}", scaffold["COMPETITIVE_MAP_TABLE"])
    report_text = report_text.replace("{{COVERAGE_CAVEATS}}", scaffold["COVERAGE_CAVEATS"])

    # Final em-dash clean on the fully assembled report. Applied here rather than
    # only to the narrative, because em-dashes may arrive from the COVERAGE_CAVEATS
    # scaffold (sourced from facts caveats strings) or from any other facts field.
    # This is safe: "|" and "---" in markdown tables are unaffected by the replacement.
    report_text = _clean_template(report_text)

    out_path = workdir / "S1_search_landscape.md"
    out_path.write_text(report_text, encoding="utf-8")
    # What the automated check found goes at the top of the file too.
    stamp_check_block(workdir)
    print(f"   report -> S1_search_landscape.md  ({len(report_text)} chars)")
    return out_path


# ---------------------------------------------------------------------------
# What the automated number check found, written where the code owns the words
# ---------------------------------------------------------------------------
# WHY: the check's finding used to reach the user only through
# Claude's reply. Four rounds of SKILL wording asked Claude to pass it on
# "exactly as printed", and every live session lost something: the folder and
# file name, a "tidied" line, a softened warning. So the finding is now written
# by code onto the two surfaces the user reads directly: the top of the report
# file (stamp_check_block) and the local app (the viewer reads `check --json`).
#
# One wording, one count: every line comes from check_data / _file_line, and
# the count is the one the check wrote into the file's own note
# (_provenance_check); nothing is recounted. A report written by an earlier version
# has no top block; its finding is read from the same note.

_NOTE_HEADING = "Provenance note (automated check)"
_NOTE_COUNT_RE = re.compile(r"(\d+) number\(s\) in the narrative above could not be matched")
_REPORT_FILE = "S1_search_landscape.md"
# The heading of the block stamp_check_block puts at the top of the report.
TOP_HEADING = "## What the automated check found"


# The folder is named the way the user knows it, never a machine path, in one
# of two wordings.  A run folder directly in a research repository (see
# _in_research_repository) is "the folder search_project-management_2026-01-01
# in your research repository", because the repository's own folder has
# another name on each machine.  Any other run folder is "marketing-research/
# search_project-management_2026-01-01": the research folder's name and the run
# folder's name. os.path.abspath is lexical, so a folder reached through a link
# keeps the name the user gave it.
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


def _file_line(text) -> str:
    """One plain line: what the automated number check found in the report.
    The note sits right after the written analysis, before the tables; the
    first such heading is the check's own (the top block names it only in
    quotation marks, never as a heading)."""
    at = text.find("### " + _NOTE_HEADING)
    m = _NOTE_COUNT_RE.search(text, at) if at >= 0 else None
    if at >= 0 and not m:
        # A note is there but its count cannot be read: never say that nothing
        # was flagged.
        return (f"{_REPORT_FILE}: the automated check left a note under the heading "
                f"\"{_NOTE_HEADING}\", right after the written analysis and before "
                f"the tables. Read it there.")
    if not m:
        # Said positively, and true for an analysis with no numbers ("if
        # there were any"); "looked at" because single digits are not checked
        # (_provenance_check).
        return (f"{_REPORT_FILE}: nothing in it is flagged for checking. Every number in "
                f"the written analysis that the automated check looked at, if there "
                f"were any, matched the counted data.")
    n = int(m.group(1))
    one = n == 1
    return (f"{_REPORT_FILE}: {n} number{'' if one else 's'} in the written analysis could "
            f"not be matched to the counted data, so check {'it' if one else 'them'} before "
            f"relying on {'it' if one else 'them'}. {'It is' if one else 'They are'} listed "
            f"under the heading \"{_NOTE_HEADING}\", right after the written analysis and "
            f"before the tables.")


def _has_top_block(text) -> bool:
    """True when the block sits at the top: as the first heading, or right after the
    report's own first-line title."""
    lines = text.split("\n", 3)
    if lines and lines[0].strip() == TOP_HEADING:
        return True
    return (len(lines) >= 3 and lines[0].startswith("# ") and not lines[1].strip()
            and lines[2].strip() == TOP_HEADING)


def check_data(workdir) -> dict:
    """What the automated number check found, for the app and for `check --json`:
    {"folder_line": str, "files": [{"file", "line", "present", "top_block"}]}.
    top_block says whether the file already starts with the block (a report from
    an earlier version does not, so the app shows the line itself)."""
    workdir = Path(workdir)
    where = _short_folder(workdir)
    # The same two places emit_index._discover_reports looks (an early layout kept
    # the report in a report/ subfolder), so the app and this check name one file.
    rel = next((r for r in (_REPORT_FILE, "report/" + _REPORT_FILE)
                if (workdir / r).exists()), None)
    if rel is None:
        return {"folder_line": f"There is no written report in {where} yet.",
                "files": [{"file": _REPORT_FILE, "present": False, "top_block": False,
                           "line": f"{_REPORT_FILE}: this report file is not in the folder."}]}
    text = (workdir / rel).read_text(encoding="utf-8", errors="replace")
    return {"folder_line": f"The written report is in {where}: {rel}.",
            "files": [{"file": rel, "present": True,
                       "top_block": _has_top_block(text), "line": _file_line(text)}]}


def check_lines(workdir) -> list:
    """Where the report is, then one plain line: what the automated number
    check found."""
    d = check_data(workdir)
    return [d["folder_line"]] + [f["line"] for f in d["files"]]


def cli_lines(workdir) -> list:
    """What `check` prints: check_lines, then one line when the report was written
    before the check was put at the top of the file. WHY: the SKILL tells Claude to
    say the report starts with what the check found; for an older report that is
    not true, and the first live session on an old folder said it anyway. This line
    is the code's own signal, so Claude does not have to tell old from new."""
    d = check_data(workdir)
    old = [f["file"] for f in d["files"] if f["present"] and not f["top_block"]]
    out = check_lines(workdir)
    if old:
        out.append(f"{old[0]}: written before reports started with what the automated "
                   f"check found, so the top of that file does not show it. The note "
                   f"named above has it, and the app shows these lines above the report.")
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


def stamp_check_block(workdir) -> bool:
    """Put what the automated number check found at the top of the report file:
    the folder line and the file's own line, both from check_data, so the words are
    the ones `check` prints. The full note stays where it is. The block goes right
    after the report's first-line title, or first when there is none. Returns False
    when the file is missing or could not be rewritten (the note is still there)."""
    p = Path(workdir) / _REPORT_FILE
    if not p.exists():
        return False
    try:
        text = _strip_top_block(p.read_text(encoding="utf-8", errors="replace"))
        d = check_data(workdir)
        block = f"{TOP_HEADING}\n\n{d['folder_line']}\n\n{_file_line(text)}\n\n---\n"
        first, sep, rest = text.partition("\n")
        if first.startswith("# ") and sep:
            text = f"{first}\n\n{block}\n{rest.lstrip(chr(10))}"
        else:
            text = f"{block}\n{text}"
        p.write_text(text, encoding="utf-8")
        return True
    except OSError as exc:
        print(f"[report] could not add what the automated check found to the top of "
              f"{_REPORT_FILE} ({exc})")
        return False


if __name__ == "__main__":
    import argparse
    _ap = argparse.ArgumentParser(description="What the automated number check found in "
                                              "a run's written report (reads files only).")
    _ap.add_argument("command", choices=["check"])
    _ap.add_argument("--workdir", required=True, help="the run folder")
    _ap.add_argument("--json", action="store_true", help="print it as JSON (the app reads this)")
    _args = _ap.parse_args()
    if _args.json:
        print(json.dumps(check_data(_args.workdir), ensure_ascii=False))
    else:
        print("\n".join(cli_lines(_args.workdir)))

