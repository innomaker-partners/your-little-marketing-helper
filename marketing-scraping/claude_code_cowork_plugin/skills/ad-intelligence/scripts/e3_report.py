"""
e3_report.py — Assemble the E3 report bundle.

Reads pipeline outputs from workdir and writes a report/ folder containing:
  - E3_creative_angles.md  — template copy (the user's LLM fills this in)
  - DATA_DIGEST.md         — deterministic, LLM-ready distillation of Meta + Google data
  - PROMPT.md              — ready-to-paste instruction for the user's LLM

No LLM is called here. The user brings their own model and uses PROMPT.md
to drive the analysis over E3_creative_angles.md + DATA_DIGEST.md + creatives/.

Stdlib only — no pip install needed.
"""

import json
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[warn] could not parse {p}: {exc}")
        return default


def _truncate(text, max_chars=300):
    """Trim long copy for the digest — keeps it scannable without losing substance."""
    if not text:
        return ""
    text = str(text).strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + " …"


def _compute_duration_days(first_shown, last_shown):
    """
    Compute duration in days between firstShownAt and lastShownAt.

    Accepts Unix timestamps (int or numeric str — as returned by the Apify actor)
    or ISO 8601 date strings. Returns an int or None on parse failure.
    """
    def _parse(val):
        if val is None:
            return None
        # Unix timestamp — the actor returns these as strings like "1776832935"
        try:
            ts = int(val)
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, TypeError):
            pass
        # ISO 8601 (e.g. "2026-04-21T00:00:00.000Z" found in countryStats)
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
            try:
                return datetime.strptime(str(val), fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None

    dt_first = _parse(first_shown)
    dt_last = _parse(last_shown)
    if dt_first and dt_last and dt_last >= dt_first:
        return (dt_last - dt_first).days
    return None


def _meta_copy_parts(snapshot):
    """
    Extract all text copy from a Meta ad snapshot dict.

    Returns (body_text: str, card_lines: list[str]).

    Field assumptions [CONFIRMED against live actor output]:
      - snapshot.body.text     — top-level ad copy (dict with "text" key)
      - snapshot.cards[]       — carousel cards; each has .body, .title, .cta_text, .link_url
      - snapshot.cta_text      — top-level CTA (separate from card CTAs)
      - snapshot.page_name     — advertiser display name
    """
    body_text = ""
    body = snapshot.get("body")
    if isinstance(body, dict):
        body_text = (body.get("text") or "").strip()
    elif isinstance(body, str):
        body_text = body.strip()

    card_lines = []
    for i, card in enumerate(snapshot.get("cards") or []):
        if not isinstance(card, dict):
            continue
        parts = []
        t = (card.get("title") or "").strip()
        b = (card.get("body") or "").strip()
        cta = (card.get("cta_text") or "").strip()
        url = (card.get("link_url") or "").strip()

        if t:
            parts.append(f"title: {t}")
        # Only include card body if it differs from the top-level body (often identical)
        if b and b != body_text:
            parts.append(f"copy: {_truncate(b)}")
        if cta:
            parts.append(f"cta: {cta}")
        if url:
            parts.append(f"url: {url}")

        if parts:
            card_lines.append(f"  card {i + 1}: " + " | ".join(parts))

    return body_text, card_lines


def _cell(s):
    """Escape pipe characters for Markdown table cells."""
    return str(s).replace("|", "/")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def assemble_report(workdir, cfg):
    """
    Assemble the E3 report bundle into <workdir>/report/.

    Reads:
      B4_meta_ad_library/run_02_full.json
      B5_google_ads/run_02_full.json
      creatives/meta/manifest.json
      creatives/google/manifest.json
      google_ocr.json                     (optional — degrades gracefully if absent)

    Writes:
      report/E3_creative_angles.md  — template (LLM fills this)
      report/DATA_DIGEST.md         — deterministic data distillation
      report/PROMPT.md              — ready-to-paste LLM instruction
    """
    workdir = Path(workdir)
    report_dir = workdir / "report"
    report_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 1. Read pipeline outputs
    # ------------------------------------------------------------------
    meta_items = _read_json(workdir / "B4_meta_ad_library" / "run_02_full.json", []) or []
    google_items = _read_json(workdir / "B5_google_ads" / "run_02_full.json", []) or []
    li_items = _read_json(workdir / "LI_linkedin_ads" / "run_02_full.json", []) or []
    meta_manifest = _read_json(workdir / "creatives" / "meta" / "manifest.json", {}) or {}
    google_manifest = _read_json(workdir / "creatives" / "google" / "manifest.json", {}) or {}
    li_manifest = _read_json(workdir / "creatives" / "linkedin" / "manifest.json", {}) or {}
    google_ocr = _read_json(workdir / "google_ocr.json", {}) or {}

    # ------------------------------------------------------------------
    # 2. Copy the E3 template verbatim
    # ------------------------------------------------------------------
    here = Path(__file__).resolve().parent
    template_path = here.parent / "templates" / "reports" / "E3_creative_angles.md"
    if template_path.exists():
        template_text = template_path.read_text(encoding="utf-8")
    else:
        template_text = (
            "# E3 — Creative Angle Audit\n\n"
            "(Template not found. Expected at templates/reports/E3_creative_angles.md)\n"
        )

    (report_dir / "E3_creative_angles.md").write_text(template_text, encoding="utf-8")

    # ------------------------------------------------------------------
    # 3. Build DATA_DIGEST.md
    # ------------------------------------------------------------------
    generated_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "# DATA_DIGEST — E3 Report Inputs",
        "",
        "Auto-generated by ad_intel. Feed this alongside E3_creative_angles.md and",
        "the creatives/ folder to your LLM, using PROMPT.md as the instruction.",
        "",
        f"Generated : {generated_at}",
        f"Workdir   : {workdir.resolve()}",
        f"Meta records     : {len(meta_items)}",
        f"Google records   : {len(google_items)}",
        f"LinkedIn records : {len(li_items)}",
        "",
        "---",
        "",
    ]

    # ---- Meta (B4) ---------------------------------------------------
    lines += [
        "## Meta Ad Library (B4)",
        "",
        "Each block: advertiser | ad archive ID | main copy | cards | creative files.",
        "",
    ]

    if not meta_items:
        lines.append("_(no Meta records found)_")
        lines.append("")
    else:
        for rec in meta_items:
            if not isinstance(rec, dict):
                continue
            ad_id = str(rec.get("ad_archive_id") or "unknown")
            snap = rec.get("snapshot") or {}
            page_name = (snap.get("page_name") or "(no name)").strip()
            body_text, card_lines = _meta_copy_parts(snap)
            top_cta = (snap.get("cta_text") or "").strip()

            creatives_saved = [
                f for f in (meta_manifest.get(ad_id) or [])
                if isinstance(f, str) and not f.startswith("FAILED")
            ]

            lines.append(f"### {page_name}  (ad_archive_id: {ad_id})")
            lines.append(f"**Copy:** {_truncate(body_text) if body_text else '(none)'}")
            if top_cta:
                lines.append(f"**CTA:** {top_cta}")
            if card_lines:
                lines.append("**Cards:**")
                lines.extend(card_lines)
            if creatives_saved:
                lines.append(f"**Creatives:** {', '.join(creatives_saved)}")
            else:
                lines.append("**Creatives:** (none saved / no manifest entry)")
            lines.append("")

    # ---- Google (B5) ------------------------------------------------
    lines += [
        "---",
        "",
        "## Google Ads Transparency (B5)",
        "",
        ("Duration = lastShownAt − firstShownAt in days. "
         "Longer duration = more tested = stronger profitability signal."),
        "",
        "| # | Advertiser | Format | Duration (days) | OCR Text | Creative files |",
        "|---|-----------|--------|-----------------|----------|----------------|",
    ]

    if not google_items:
        lines.append("| — | (no Google records found) | | | | |")
    else:
        for i, rec in enumerate(google_items, 1):
            if not isinstance(rec, dict):
                continue
            advertiser = (rec.get("advertiserName") or "(none)").strip()
            fmt = (rec.get("format") or "(none)").strip()
            creative_id = str(rec.get("creativeId") or "unknown")
            duration = _compute_duration_days(
                rec.get("firstShownAt"), rec.get("lastShownAt")
            )
            duration_str = str(duration) if duration is not None else "(unknown)"

            ocr_text = google_ocr.get(creative_id, "")
            ocr_cell = _truncate(ocr_text, 80) if ocr_text else "(no OCR)"

            creatives_saved = [
                f for f in (google_manifest.get(creative_id) or [])
                if isinstance(f, str) and not f.startswith("FAILED")
            ]
            creative_cell = ", ".join(creatives_saved) if creatives_saved else "(none)"

            lines.append(
                f"| {i} | {_cell(advertiser)} | {_cell(fmt)} | {duration_str} "
                f"| {_cell(ocr_cell)} | {_cell(creative_cell)} |"
            )

    lines += ["", "---", ""]

    # ---- LinkedIn (LI) ----------------------------------------------
    lines += [
        "",
        "## LinkedIn Ad Library (LI)",
        "",
        ("Real impression data (totalImpressionsMax) where LinkedIn reports it — "
         "a direct performance signal unavailable from Google. "
         "Ads ranked by impressions descending within each advertiser; "
         "falls back to duration when impressions are null (common for VIDEO ads). "
         "Duration = lastShownAt − firstShownAt."),
        "",
    ]

    if not li_items:
        lines.append(
            "_(no LinkedIn records found — this advertiser may not run LinkedIn "
            "campaigns, which is common for B2C / local businesses)_"
        )
        lines.append("")
    else:
        # Group by advertiser name
        by_advertiser: dict[str, list[dict]] = {}
        for rec in li_items:
            if not isinstance(rec, dict):
                continue
            name = (rec.get("advertiserName") or "(unknown)").strip()
            if name not in by_advertiser:
                by_advertiser[name] = []
            by_advertiser[name].append(rec)

        def _li_sort_key(rec):
            imp = rec.get("totalImpressionsMax")
            if imp is not None:
                return (1, int(imp))  # prefer impressions signal
            days = _compute_duration_days(
                rec.get("firstShownAt"), rec.get("lastShownAt")
            )
            return (0, days or 0)   # fallback: duration in days

        for advertiser in sorted(by_advertiser):
            ads_sorted = sorted(by_advertiser[advertiser], key=_li_sort_key, reverse=True)
            lines.append(f"### {advertiser}")
            lines.append("")
            lines.append(
                "| # | Ad ID | Headline | Body (snippet) | Format "
                "| Duration | Impressions (max) | Creative files |"
            )
            lines.append(
                "|---|-------|---------|----------------|--------"
                "|----------|-------------------|----------------|"
            )
            for i, rec in enumerate(ads_sorted, 1):
                ad_id = str(rec.get("adId") or "unknown")
                headline = _cell(rec.get("headline") or "(none)")
                # Flatten newlines — LinkedIn body copy contains \n; raw newlines
                # break Markdown table rows.
                raw_body = (rec.get("body") or "").replace("\n", " ").replace("\r", "")
                body_snippet = _cell(_truncate(raw_body, 120))
                fmt = _cell(rec.get("format") or "(none)")
                dur_days = _compute_duration_days(
                    rec.get("firstShownAt"), rec.get("lastShownAt")
                )
                duration_str = f"{dur_days} days" if dur_days is not None else "(unknown)"
                imp_max = rec.get("totalImpressionsMax")
                imp_str = f"{imp_max:,}" if imp_max is not None else "(n/a)"
                li_creatives_saved = [
                    f for f in (li_manifest.get(ad_id) or [])
                    if isinstance(f, str) and not f.startswith("FAILED")
                ]
                creative_cell = _cell(
                    ", ".join(li_creatives_saved) if li_creatives_saved else "(none)"
                )
                lines.append(
                    f"| {i} | {ad_id} | {headline} | {body_snippet} | {fmt} "
                    f"| {duration_str} | {imp_str} | {creative_cell} |"
                )
            lines.append("")

    lines += ["---", ""]

    digest_text = "\n".join(lines) + "\n"
    (report_dir / "DATA_DIGEST.md").write_text(digest_text, encoding="utf-8")

    # ------------------------------------------------------------------
    # 4. Write PROMPT.md
    # ------------------------------------------------------------------
    prompt_lines = [
        "# PROMPT — Instructions for Your LLM",
        "",
        "Paste this file's contents as the first message to your LLM,",
        "with E3_creative_angles.md and DATA_DIGEST.md as attached context.",
        "The creatives/ folder (at ../creatives/ relative to this file) holds",
        "the downloaded ad images and videos, referenced by filename in DATA_DIGEST.md.",
        "",
        "---",
        "",
        "You are a senior performance-marketing analyst.",
        "",
        "You have been given:",
        "1. **E3_creative_angles.md** — the report template with six sections to fill.",
        "2. **DATA_DIGEST.md** — a data export: every Meta ad with its copy and creative",
        "   filenames; every Google ad with advertiser, format, run-duration (days),",
        "   OCR'd copy, and creative filenames; every LinkedIn ad with advertiser,",
        "   headline, body snippet, format, duration, real impression counts, and",
        "   creative filenames.",
        "3. **creatives/** — the downloaded ad images/videos, named as in DATA_DIGEST.md.",
        "",
        "**Your task:** write the E3 report by filling the template's sections from the data.",
        "",
        "**Section-by-section guidance:**",
        "",
        "- **Section 1 (Angle taxonomy A–H):** read all ad copy in DATA_DIGEST.md. Group",
        "  ads by the angle they lead with (urgency, social proof, price/value, identity,",
        "  fear/pain, aspiration, novelty, authority, etc.). Label A–H. Per angle: frequency",
        "  (% of ads), one verbatim example, which advertisers use it.",
        "- **Section 2 (Duration-weighted ranking):** use the Google duration column and",
        "  LinkedIn impression counts (totalImpressionsMax where available).",
        "  Longer-running ads signal a tested, profitable angle. Rank angles by average",
        "  duration across ads using them. Note weak signals (long span, tiny creative count).",
        "- **Section 3 (Platform comparison):** Meta vs Google vs LinkedIn.",
        "  Who advertises where? Which angles dominate each platform?",
        "  LinkedIn is B2B-first, so note if competitors only appear on one platform.",
        "  Where is the gap the client can own?",
        "- **Section 4 (Gap analysis):** if you do not have pain-point or trust-signal",
        "  data from the other tools in this package, mark this section:",
        "  **'Requires the pain-and-messaging and trust-signal tools'**",
        "  and complete what you can from the ad data alone (angles nobody runs).",
        "- **Section 5 (Headline bank):** extract the strongest hooks verbatim. Note",
        "  anti-patterns (arms races, generic claims, over-claims the client can't back).",
        "- **Section 6 (Creative brief):** if seasonal search data is absent, mark timing as",
        "  **'Requires SEO seasonality data'**. Complete positioning and the top",
        "  3 angles to test from the data you have.",
        "",
        "Close with 3 to 5 numbered actionable takeaways.",
        "",
        "Write in plain English. Be specific: quote copy verbatim, name advertisers,",
        "cite durations. Do not invent data not present in DATA_DIGEST.md.",
    ]

    (report_dir / "PROMPT.md").write_text("\n".join(prompt_lines) + "\n", encoding="utf-8")

    # ------------------------------------------------------------------
    # 5. Summary line
    # ------------------------------------------------------------------
    print(
        f"[E3] Report bundle assembled: {len(meta_items)} Meta ads, "
        f"{len(google_items)} Google ads, {len(li_items)} LinkedIn ads "
        f"→ {report_dir.resolve()}"
    )
