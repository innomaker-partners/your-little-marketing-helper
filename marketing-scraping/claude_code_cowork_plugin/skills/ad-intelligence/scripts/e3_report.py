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

import adrecords as AR  # ad or scraper status record; LinkedIn bands and dates
import resolve as RS  # google_ad_owners: whose Google ads are the competitors'
import emit_index as EI  # labelled_set_aside: the set-aside ads the app shows
import stages as ST  # linkedin_where: the LinkedIn country filter build_LI sends


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

    Accepts Unix timestamps (int or numeric str — as returned by the Google actor),
    ISO 8601 date strings, and text dates such as "Sep 14, 2026" (the LinkedIn
    actor's documented format; adrecords.parse_shown_date). Returns an int or None
    when either date cannot be read.

    WHY text dates: only epochs and ISO text were read, so every LinkedIn ad, whose
    dates are text, silently showed "(unknown)" for its duration, and the ranking
    that falls back on duration for an ad with no impressions band had nothing to rank by.
    """
    dt_first = AR.parse_shown_date(first_shown)
    dt_last = AR.parse_shown_date(last_shown)
    if dt_first and dt_last and dt_last >= dt_first:
        return (dt_last - dt_first).days
    return None


def _t(value, default=""):
    """A stripped string from any scraped value, `default` when there is no text.

    WHY: `(rec.get(k) or "").strip()` assumes a string. A scraper that sends a number
    or a list in a text field crashed the whole report step, and with it the
    DATA_DIGEST, PROMPT and the app's index, for every advertiser in the run."""
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, (str, int, float)):
        return str(value).strip() or default
    return default


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
        body_text = _t(body.get("text"))
    elif isinstance(body, str):
        body_text = body.strip()

    card_lines = []
    cards = snapshot.get("cards")
    for i, card in enumerate(cards if isinstance(cards, list) else []):
        if not isinstance(card, dict):
            continue
        parts = []
        t = _t(card.get("title"))
        b = _t(card.get("body"))
        cta = _t(card.get("cta_text"))
        url = _t(card.get("link_url"))

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


# "the" competitor's, as the app's heading and every label under it say.
# An ad whose search website is recorded was checked against that one competitor
# only (resolve.google_ad_owners), and the label above its table names it; "a
# competitor's" read as a check against every competitor in the run.
GOOGLE_NOT_CONFIRMED_HEADING = "## Google ads not confirmed as the competitor's"


def _labelled_section(labelled):
    """The report's section of set-aside Google ads, under their label.

    WHY ("label them"): a set-aside ad may be the competitor's
    own under another legal name, so the reader sees what it says, but never as a
    competitor's ad. The section is written here, by code, from the same data the
    app shows (emit_index.labelled_set_aside): a model's summary has turned "found
    near X" into "X's" before, and a code-written section cannot drift. It goes at
    the end of DATA_DIGEST.md and of the E3_creative_angles.md scaffold, apart from
    every table of a competitor's ads. [] when nothing was set aside."""
    if not labelled or not labelled.get("ads"):
        return []
    out = [GOOGLE_NOT_CONFIRMED_HEADING, "",
           "Listed for reading only. These Google ads came up when searching a "
           "competitor's website, but the advertiser's name does not match that "
           "competitor. They are not part of any table, count or finding elsewhere in "
           "this report.", ""]
    ads = labelled.get("ads") or []
    for lab in labelled.get("labels") or []:
        rows = [a for a in ads
                if (a.get("set_aside") or {}).get("searched", "") == lab.get("searched", "")]
        if not rows:
            continue
        out += [lab.get("label") or "", "",
                "| # | Advertiser (as Google names it) | Format | Shown | Copy | Original ad "
                "| Creative files |",
                "|---|---|---|---|---|---|---|"]
        for i, a in enumerate(rows, 1):
            d = a.get("data") or {}
            first, last = d.get("first_shown"), d.get("last_shown")
            shown = (f"{first} to {last}" if first and last and first != last
                     else first or last or "(not given)")
            copy = " ".join(str(a.get("text") or "").split())
            url = d.get("ad_url") or ""
            files = ", ".join(Path(m.get("path", "")).name for m in a.get("media") or []
                              if isinstance(m, dict) and m.get("path"))
            out.append(
                f"| {i} | {_cell(EI.set_aside_name(d.get('advertiser_name')))} "
                f"| {_cell(d.get('format') or '(none)')} | {_cell(shown)} "
                f"| {_cell(_truncate(copy, 160)) if copy else '(no copy)'} "
                f"| {f'[open]({_cell(url)})' if url.startswith('http') else '(none)'} "
                f"| {_cell(files) if files else '(none)'} |")
        out.append("")
    if labelled.get("confirm"):
        out += [labelled["confirm"], ""]
    return out


def _names(items):
    """'a', 'b' -- competitor inputs or search words, quoted as the run quotes them."""
    return ", ".join(f"'{str(x).strip()}'" for x in items)


def _meta_coverage_digest(cov):
    """DATA_DIGEST lines saying what Meta did and did not search ([] when every
    competitor was searched by its own Facebook page).

    WHY: a report written from this digest said a
    competitor "has a visible Meta blind spot" when no competitor page was on file
    and Meta was searched by the user's words. A competitor whose own Meta ads were
    not searched has an UNKNOWN Meta presence, and ads found by search words belong
    to whoever used those words. The facts come from ad_intel (where the run
    decided what Meta searches), not re-derived here."""
    cov = cov or {}
    not_searched = list(cov.get("not_searched") or [])
    words = list(cov.get("words_used") or [])
    if not not_searched:
        return []
    many = len(not_searched) > 1
    lines = ["## What Meta searched", ""]
    if words:
        lines.append(
            f"No competitor has a Facebook page on file, so no competitor's own Meta ads were "
            f"searched: {_names(not_searched)}. Meta was searched by the search words "
            f"{_names(words)} instead. The Meta records below are ads by any advertiser that "
            f"used those words; they are not these competitors' own ads.")
    elif cov.get("any_page"):
        lines.append(
            f"{_names(not_searched)} {'have' if many else 'has'} no Facebook page on file, so "
            f"{'their' if many else 'its'} own Meta ads were not searched. The Meta records "
            f"below come from the other competitors' Facebook pages.")
    else:
        lines.append(
            f"No competitor has a Facebook page on file and no search words were set, so Meta "
            f"was not searched for {_names(not_searched)}.")
    lines += ["",
              f"The Meta presence of {_names(not_searched)} is UNKNOWN, not absent: this run "
              f"did not look. It is not evidence that {'they run' if many else 'it runs'} no "
              f"Meta ads.",
              "", "---", ""]
    return lines


def _meta_coverage_prompt(cov):
    """PROMPT.md rules for a run where some competitor's own Meta ads were not
    searched ([] otherwise). See _meta_coverage_digest for why."""
    cov = cov or {}
    not_searched = list(cov.get("not_searched") or [])
    if not not_searched:
        return []
    lines = [
        "",
        "**What Meta did not search (read this before writing anything about Meta).**",
        f"The own Meta ads of {_names(not_searched)} were not searched, because no",
        "Facebook page was on file (DATA_DIGEST.md, \"What Meta searched\"). Their Meta",
        "presence is UNKNOWN, not absent. Never write or imply that they run no Meta ads,",
        "have a Meta gap or blind spot, are absent from Meta, or should claim or start a",
        "Facebook presence. Say only that this run did not search their own Meta ads.",
    ]
    if cov.get("words_used"):
        lines += [
            f"The Meta records were found by the search words {_names(cov['words_used'])}:",
            "they are ads by any advertiser that used those words, not these competitors'",
            "own ads. Never attribute them to a competitor, and never compute yourself how",
            "many of them, or what share or percentage, are unrelated or belong to any",
            "competitor. About these records as a whole, state only the counts",
            "DATA_DIGEST.md gives.",
        ]
    return lines


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def assemble_report(workdir, cfg, gap_lines=None, meta_coverage=None, website_notes=None):
    """
    Assemble the E3 report bundle into <workdir>/report/.

    gap_lines: the run's own lines naming its gaps (ad_intel._gap_lines), carried
      into DATA_DIGEST.md word for word; [] or None when the run has none.
    website_notes: sentences (resolve.website_note_lines) naming a competitor website
      Google searched as the user typed it although the search's best match was a
      different one; carried into DATA_DIGEST.md word for word.
    meta_coverage: what the Meta stage searched, as ad_intel decided it:
      {"not_searched": [competitor inputs whose own Meta ads were not searched],
       "words_used": [search words Meta was searched by instead], "any_page": bool}.
      Drives the "What Meta searched" section and the PROMPT.md rule.

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
    # The raw file keeps every record. A status record from the Meta scraper ("Ads not
    # found" for a page with no ads) is not an ad: it is neither counted nor listed as
    # one, and what the library said is stated in the Meta section (adrecords).
    meta_items, _meta_status = AR.split_meta_items(
        _read_json(workdir / "B4_meta_ad_library" / "run_02_full.json", []) or [])
    _meta_notes = AR.meta_status_notes(_meta_status)
    google_all = _read_json(workdir / "B5_google_ads" / "run_02_full.json", []) or []
    # Only the competitors' own Google ads go into the digest; the
    # others are counted and named in the Google section (resolve.google_ad_owners).
    _entries = _read_json(workdir / "competitors" / "resolution.json", []) or []
    _g_owner = RS.google_ad_owners(google_all, _entries)
    google_items = [g for g, v in zip(google_all, _g_owner) if v == "own"]
    google_aside = RS.google_set_aside_lines(google_all, _entries, _g_owner)
    li_items = _read_json(workdir / "LI_linkedin_ads" / "run_02_full.json", []) or []
    meta_manifest = _read_json(workdir / "creatives" / "meta" / "manifest.json", {}) or {}
    google_manifest = _read_json(workdir / "creatives" / "google" / "manifest.json", {}) or {}
    li_manifest = _read_json(workdir / "creatives" / "linkedin" / "manifest.json", {}) or {}
    google_ocr = _read_json(workdir / "google_ocr.json", {}) or {}
    # The set-aside Google ads under their label, the same data the app
    # shows (emit_index.labelled_set_aside), for the code-written closing section.
    _not_confirmed = _labelled_section(EI.labelled_set_aside(
        google_all, _entries, _g_owner, "", "B5_google_ads/run_02_full.json",
        google_manifest, "creatives/google") if google_aside else None)

    # ------------------------------------------------------------------
    # 2. Copy the E3 template verbatim
    # ------------------------------------------------------------------
    here = Path(__file__).resolve().parent
    template_path = here.parent / "templates" / "reports" / "E3_creative_angles.md"
    if template_path.exists():
        template_text = template_path.read_text(encoding="utf-8")
    else:
        template_text = (
            "# Creative Angle Audit\n\n"
            "(Template not found. Expected at templates/reports/E3_creative_angles.md)\n"
        )
    # The template's two placeholders are filled here, as the pain report
    # does for its own. They were copied through raw, so the first line the user (and
    # the app) saw was "Creative Angle Audit ({{CLIENT}})" and "Generated: {{DATE}}".
    # The name is the run's subject, the same one the app shows for the run. Only the
    # exact tokens are replaced, so a template without them is written unchanged.
    template_text = (template_text
                     .replace("{{CLIENT}}", str((cfg or {}).get("subject") or EI._subject_from_dirname(workdir)))
                     .replace("{{DATE}}", datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")))
    if _not_confirmed:
        template_text = template_text.rstrip("\n") + "\n\n" + "\n".join(_not_confirmed)

    (report_dir / "E3_creative_angles.md").write_text(template_text, encoding="utf-8")

    # ------------------------------------------------------------------
    # 3. Build DATA_DIGEST.md
    # ------------------------------------------------------------------
    generated_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "# DATA_DIGEST: report inputs",
        "",
        "Written by the ad-intelligence tool. Feed this alongside E3_creative_angles.md and",
        "the creatives/ folder to your LLM, using PROMPT.md as the instruction.",
        "",
        f"Generated : {generated_at}",
        f"Workdir   : {workdir.resolve()}",
        f"Meta records     : {len(meta_items)}"
        + (f"  ({len(_meta_status)} status record{'' if len(_meta_status) == 1 else 's'} "
           f"from the scraper not counted, see the Meta section)" if _meta_status else ""),
        f"Google records   : {len(google_items)}"
        + (f"  ({len(google_all) - len(google_items)} more set aside, see the Google section)"
           if len(google_all) > len(google_items) else ""),
        f"LinkedIn records : {len(li_items)}",
        "",
        "---",
        "",
    ]
    if gap_lines:
        lines += ["## What this run did not cover (the run's own closing lines)", ""]
        lines += list(gap_lines)
        lines += ["", "A source or competitor named here is a gap in what the tool collected,",
                  "not a fact about the market.", "", "---", ""]
    if website_notes:
        lines += ["## Competitor websites to check", ""] + list(website_notes) + ["", "---", ""]
    lines += _meta_coverage_digest(meta_coverage)

    # ---- Meta (B4) ---------------------------------------------------
    lines += [
        "## Meta Ad Library",
        "",
        "Each block: advertiser | ad archive ID | main copy | cards | creative files.",
        "",
    ]

    if not meta_items:
        # "None found" is true only where the library said so; after a scraper error
        # the ads are unknown, which is not the same and must not read as a zero.
        if any(n["kind"] == "error" for n in _meta_notes):
            lines.append("_(no Meta ads came back: the scraper returned an error, so the "
                         "ads are unknown)_")
        else:
            lines.append("_(no Meta ads found)_" if _meta_notes
                         else "_(no Meta records found)_")
        lines.append("")
    else:
        for rec in meta_items:
            if not isinstance(rec, dict):
                continue
            ad_id = str(rec.get("ad_archive_id") or "unknown")
            snap = rec.get("snapshot")
            snap = snap if isinstance(snap, dict) else {}
            page_name = _t(snap.get("page_name"), "(no name)")
            body_text, card_lines = _meta_copy_parts(snap)
            top_cta = _t(snap.get("cta_text"))

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

    if _meta_notes:
        # What the Meta scraper itself said about a page or search. Attributed to the
        # library, in the scraper's words, never turned into a statement about the market.
        lines += ["**What the Meta ad library said (scraper status records, not ads):**", ""]
        lines += [f"- {n['sentence']}" for n in _meta_notes]
        lines += [""]

    # ---- Google (B5) ------------------------------------------------
    lines += [
        "---",
        "",
        "## Google Ads Transparency",
        "",
    ]
    if google_aside:
        # When every Google ad was set aside no table
        # of the competitor's ads follows, so the header must not point "below".
        lines += [("**Set aside (the advertiser's name does not match a competitor, so these "
                   "ads are not listed below):**") if google_items else
                  ("**Set aside (the advertiser's name does not match a competitor, so these "
                   "ads are not shown as any competitor's; no Google ad in this run was "
                   "counted as a competitor's own):**"), ""]
        lines += [f"- {s}" for s in google_aside]
        lines += [""]
    # Name every advertiser counted as a competitor's
    # own, so a partner or reseller counted by the whole-word rule is visible.
    google_own = RS.google_own_lines(google_all, _entries, _g_owner)
    if google_own:
        lines += ["**Counted as the competitor's own (by the advertiser's name, or a name "
                  "listed in \"google_advertisers\"):**", ""]
        lines += [f"- {s}" for s in google_own]
        lines += [""]
    lines += [
        ("Duration = days from the first to the last day Google showed the ad. "
         "Longer duration = more tested = stronger profitability signal."),
        "",
        "| # | Advertiser | Format | Duration (days) | OCR Text | Creative files |",
        "|---|-----------|--------|-----------------|----------|----------------|",
    ]

    if not google_items and google_all:
        # Records were found, so "no Google records found" would be false.
        lines.append("| - | (none counted as a competitor's own; every Google ad found was "
                     "set aside, see the list above) | | | | |")
    elif not google_items:
        lines.append("| - | (no Google records found) | | | | |")
    else:
        for i, rec in enumerate(google_items, 1):
            if not isinstance(rec, dict):
                continue
            advertiser = _t(rec.get("advertiserName"), "(none)")
            fmt = _t(rec.get("format"), "(none)")
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
        "## LinkedIn Ad Library",
        "",
        ("LinkedIn reports impressions as a band (for example 5k-10k), not an exact "
         "count, and only for some ads (many VIDEO ads have none). Bands are shown "
         "here exactly as LinkedIn gives them. Within each advertiser, ads with a band "
         "are listed first, ordered by the top of the band, highest first; ads with no "
         "band follow, ordered by duration. "
         "Duration = days from the first to the last day LinkedIn showed the ad."),
        "",
    ]

    if not li_items:
        # WHY: this line used to read the zero as "this advertiser may not run
        # LinkedIn campaigns", a market claim from an empty result. In a real run (a UK-only search) the
        # empty result came from a search limited to the UK
        # with a scraper in maintenance. The digest feeds the report writer, so it
        # states only what is known: nothing came back, from which countries.
        _where = ST.linkedin_where(cfg or {})
        _from = f" from a search limited to{_where[3:]}" if _where else ""
        lines.append(
            f"_(no LinkedIn records came back{_from}. This is unconfirmed: the tool "
            "cannot tell a real zero from a scraper that fetched nothing, or from a "
            "LinkedIn search that did not run. Never read it as a sign that anyone "
            f"runs no LinkedIn ads{_where}.)_"
        )
        lines.append("")
    else:
        # Group by advertiser name
        by_advertiser: dict[str, list[dict]] = {}
        for rec in li_items:
            if not isinstance(rec, dict):
                continue
            name = _t(rec.get("advertiserName"), "(unknown)")
            if name not in by_advertiser:
                by_advertiser[name] = []
            by_advertiser[name].append(rec)

        def _li_sort_key(rec):
            # LinkedIn gives impressions as a BAND string ("5k-10k"); int() of it
            # raised ValueError in a real run and the whole report step was lost.
            # A band sorts by its top end; anything unreadable counts as "no
            # impressions" and the ad is ranked by duration instead.
            _shown, imp = AR.impression_value(rec.get("totalImpressionsMax"))
            if imp is not None:
                return (1, imp)  # prefer impressions signal
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
                "| Duration | Impressions (LinkedIn's band) | Creative files |"
            )
            lines.append(
                "|---|-------|---------|----------------|--------"
                "|----------|-------------------|----------------|"
            )
            for i, rec in enumerate(ads_sorted, 1):
                ad_id = str(rec.get("adId") or "unknown")
                headline = _cell(_t(rec.get("headline"), "(none)"))
                # Flatten newlines — LinkedIn body copy contains \n; raw newlines
                # break Markdown table rows.
                raw_body = _t(rec.get("body")).replace("\n", " ").replace("\r", "")
                body_snippet = _cell(_truncate(raw_body, 120))
                fmt = _cell(_t(rec.get("format"), "(none)"))
                dur_days = _compute_duration_days(
                    rec.get("firstShownAt"), rec.get("lastShownAt")
                )
                duration_str = f"{dur_days} days" if dur_days is not None else "(unknown)"
                # The band as LinkedIn gave it; never a single made-up number.
                imp_shown, _imp_sort = AR.impression_value(rec.get("totalImpressionsMax"))
                imp_str = _cell(imp_shown) if imp_shown is not None else "(n/a)"
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
    # After every section about a competitor's ads, never inside one.
    if _not_confirmed:
        lines += _not_confirmed + ["---", ""]

    digest_text = "\n".join(lines) + "\n"
    (report_dir / "DATA_DIGEST.md").write_text(digest_text, encoding="utf-8")

    # ------------------------------------------------------------------
    # 4. Write PROMPT.md
    # ------------------------------------------------------------------
    prompt_lines = [
        "# PROMPT: instructions for your LLM",
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
        "1. **E3_creative_angles.md**, the report template with six sections to fill.",
        "2. **DATA_DIGEST.md**, a data export: every Meta ad with its copy and creative",
        "   filenames; every Google ad with advertiser, format, run-duration (days),",
        "   OCR'd copy, and creative filenames; every LinkedIn ad with advertiser,",
        "   headline, body snippet, format, duration, impression bands as LinkedIn",
        "   reports them (not exact counts), and creative filenames.",
        "3. **creatives/**, the downloaded ad images/videos, named as in DATA_DIGEST.md.",
        "",
        "**Your task:** write the creative angle report by filling the template's sections from the data.",
        "",
        "**Section-by-section guidance:**",
        "",
        "- **Section 1 (Angle taxonomy A–H):** read all ad copy in DATA_DIGEST.md. Group",
        "  ads by the angle they lead with (urgency, social proof, price/value, identity,",
        "  fear/pain, aspiration, novelty, authority, etc.). Label A–H. Per angle: frequency",
        "  (% of ads), one verbatim example, which advertisers use it.",
        "- **Section 2 (Duration-weighted ranking):** use the Google duration column and",
        "  LinkedIn impression bands (such as 5k-10k, where LinkedIn reports them).",
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
    prompt_lines += _meta_coverage_prompt(meta_coverage)
    if _meta_notes:
        prompt_lines += [
            "",
            "**What the Meta ad library said (read this before writing anything about Meta).**",
            "DATA_DIGEST.md, in the Meta section, lists what the Meta scraper itself said about",
            "pages it searched: either that the library says a page is not currently running",
            "ads, or that it returned an error, so that page's ads are unknown. You may repeat",
            "the first only as the library's statement about that page. Never turn either into",
            "a claim about the market, and never treat an error as \"no ads\".",
        ]
    if google_aside:
        prompt_lines += [
            "",
            "**Google ads set aside (read this before writing anything about Google).**",
            "Some Google ads found by searching a competitor's website were by advertisers",
            "whose name does not match that competitor. They are not in the Google table and",
            "are not that competitor's ads as far as this run can tell. Never attribute them",
            "to a competitor, never list their advertisers as competitors or new advertisers,",
            "and state their number only as DATA_DIGEST.md gives it.",
        ]
    if _not_confirmed:
        prompt_lines += [
            "The tool lists them under \"" + GOOGLE_NOT_CONFIRMED_HEADING[3:] + "\", the",
            "last section of DATA_DIGEST.md and of E3_creative_angles.md. Keep that section",
            "of E3_creative_angles.md word for word, as the last section of your report.",
            "Never use those ads anywhere else in the report: not in an angle, a ranking, a",
            "count, the platform comparison or the headline bank.",
        ]

    (report_dir / "PROMPT.md").write_text("\n".join(prompt_lines) + "\n", encoding="utf-8")

    # ------------------------------------------------------------------
    # 5. Summary line
    # ------------------------------------------------------------------
    print(
        f"Report bundle assembled: {len(meta_items)} Meta ads"
        + (f" ({len(_meta_status)} scraper status record{'' if len(_meta_status) == 1 else 's'}"
           f" not counted)" if _meta_status else "") + ", "
        f"{len(google_items)} Google ads, {len(li_items)} LinkedIn ads "
        f"→ {report_dir.resolve()}"
    )
