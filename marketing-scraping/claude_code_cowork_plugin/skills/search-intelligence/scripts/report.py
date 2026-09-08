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
        return False, "empty"
    if "claude.ai/code/artifact" in t.lower() or "/code/artifact/" in t.lower():
        return False, "contains an artifact URL (subagent took an action instead of returning the report)"
    if not t.startswith("#"):
        return False, "does not start with a markdown heading"
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
            f"| {kw} | {_fmt(r.get('volume'))} | {_fmt(r.get('keyword_difficulty'))} "
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
                f"| {r.get('keyword', '')} | {_fmt(r.get('volume'))} "
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
                "volume": r.get("volume"),
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

        # Deterministic output hygiene: unwrap a whole-report code fence,
        # then drop any conversational preamble before the first heading.
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

    facts_path = workdir / "synthesis_facts.json"
    if not facts_path.exists():
        print(f"[report] synthesis_facts.json not found at {facts_path} -- skipping S1")
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
    narrative = _run_claude(prompt, resolved_model, "S1")

    if not narrative:
        return None

    # Post-process: strip Insight blocks (deterministic, GUARDED), clean em-dashes,
    # check copy rules, check provenance
    narrative = _strip_insight_blocks(narrative)
    narrative = _clean_template(narrative)

    violations = _copy_guard(narrative)
    if violations:
        print(f"[report] S1 copy-guard flagged {len(violations)} violation(s):")
        for v in violations:
            print(f"   {v}")

    provenance_note = _provenance_check(narrative, facts)
    if provenance_note:
        n_flags = provenance_note.count("\n- ")
        print(f"[report] provenance: {n_flags} number(s) unmatched -- disclosure appended")

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
    print(f"   report -> S1_search_landscape.md  ({len(report_text)} chars)")
    return out_path

