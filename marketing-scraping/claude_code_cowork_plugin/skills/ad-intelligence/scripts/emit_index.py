"""
emit_index.py -- write frontend_index.json for an ad-intelligence run.

The Local Viewer contract (see marketing-viewer/references/DATA_MODEL.md):
each run emits ONE normalized frontend_index.json that the viewer renders, so the
frontend never parses raw actor shapes. This step computes NOTHING new -- it reshapes
the pipeline's own outputs (per-platform run_02_full.json + the creative manifests
written by creatives.py) into the shared canonical envelope. Reads only files already
on disk; spends no money; makes no network call.

Ads are the one 1:1 surface: creatives are already downloaded to creatives/<platform>/,
so the envelope's media[] points at LOCAL files (no CDN dependency).

Reads the SAME paths e3_report.py reads, so the index and the report never diverge on
where the data lives.

Self-test:  python3 scripts/emit_index.py   (in-memory, no files/network)
Re-index:   python3 scripts/emit_index.py --workdir <run> [--config <file>]   (free, disk only)
"""

import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import resolve as RS  # noqa: E402  google_ad_owners: whose Google ads are the competitors'

SCHEMA_VERSION = 1
TOOL_ID = "ad"          # the index's "tool" field; reindex refuses another tool's run

# platform -> (ads run file, creatives subdir)   -- mirrors e3_report.py exactly.
_PLATFORMS = {
    "meta": ("B4_meta_ad_library/run_02_full.json", "creatives/meta"),
    "google": ("B5_google_ads/run_02_full.json", "creatives/google"),
    "linkedin": ("LI_linkedin_ads/run_02_full.json", "creatives/linkedin"),
}

# Candidate native-id keys, in priority order (Meta uses ad_archive_id; the
# manifests are keyed by whichever id the downloader used).
_ID_KEYS = ("ad_archive_id", "adId", "creativeId", "id")


def _read_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return default


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _native_id(rec):
    for k in _ID_KEYS:
        v = rec.get(k)
        if v not in (None, ""):
            return str(v)
    return None


def _media_from_manifest(rec_id, manifest, creatives_subdir):
    """Turn a manifest entry (id -> [filename|'FAILED: ...']) into media[] with
    run-relative local paths. Skips failed fetches. Infers type from extension."""
    if rec_id is None:
        return []
    files = manifest.get(rec_id) or manifest.get(str(rec_id)) or []
    out = []
    for fn in files:
        if not isinstance(fn, str) or fn.startswith("FAILED"):
            continue
        ext = fn.lower().rsplit(".", 1)[-1] if "." in fn else ""
        mtype = "video" if ext in ("mp4", "mov", "webm") else "image"
        out.append({"type": mtype, "path": f"{creatives_subdir}/{fn}"})
    return out


def _first(rec, *keys):
    for k in keys:
        v = rec.get(k)
        if v not in (None, ""):
            return v
    return None


def _snap(rec):
    """The Meta snapshot sub-object (where Meta hides body/title/cta/link/media).
    Google and LinkedIn have no snapshot, so this is {} for them and the code below
    falls through to their top-level keys."""
    s = rec.get("snapshot")
    return s if isinstance(s, dict) else {}


def _epoch_seconds(*vals):
    """First value that looks like a Unix epoch -> int seconds. Accepts int/float or an
    all-digit string; treats 13-digit values as milliseconds. None if nothing usable.
    Meta ships start_date/end_date as epochs; Google ships firstShownAt/lastShownAt as
    epochs; LinkedIn ships neither."""
    for v in vals:
        if isinstance(v, bool):  # bool is an int subclass; never a date
            continue
        n = None
        if isinstance(v, (int, float)):
            n = float(v)
        elif isinstance(v, str) and v.strip().isdigit():
            n = float(v.strip())
        if n is None or n <= 0:
            continue
        if n > 1e12:  # milliseconds
            n /= 1000.0
        return int(n)
    return None


def _norm_date(*vals):
    """First usable date among vals, normalized to 'YYYY-MM-DD'. Accepts epoch (int or
    all-digit str), a 'YYYY-MM-DD HH:MM:SS' string (Meta's *_formatted), or an ISO string.
    Non-dates ('unknown', '', None) collapse to None so the viewer shows an honest blank
    instead of the literal word 'unknown' or a raw 10-digit epoch. Presentation (relative
    phrasing, 'Jun 2026') is the frontend's job; emit only canonicalizes the value."""
    for v in vals:
        if v in (None, ""):
            continue
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().isdigit()):
            secs = _epoch_seconds(v)
            if secs is not None:
                try:
                    return datetime.fromtimestamp(secs, timezone.utc).strftime("%Y-%m-%d")
                except (OverflowError, OSError, ValueError):
                    pass
            continue
        if isinstance(v, str):
            s = v.strip()
            if len(s) >= 10 and s[4] == "-" and s[7] == "-" and s[:4].isdigit():
                return s[:10]  # date part of 'YYYY-MM-DD...' or 'YYYY-MM-DD HH:MM:SS'
    return None


def _ad_format(rec, snap, media):
    """Canonical format label, always set when derivable. Order: an explicit top-level
    label (Google 'TEXT', LinkedIn 'VIDEO'/'IMAGE'), then the actual downloaded media
    (Meta has no top-level format -- this is why every Meta card read blank before), then
    Meta's snapshot.display_format as a last resort so even a Meta ad with no creative on
    disk still carries a format. Computed here so the viewer never has to re-derive it."""
    fmt = _first(rec, "format", "creativeTypeLabel", "creativeType")
    if fmt:
        return str(fmt).upper()
    if any(m["type"] == "video" for m in media):
        return "VIDEO"
    if any(m["type"] == "image" for m in media):
        return "IMAGE"
    df = snap.get("display_format")
    return str(df).upper() if df else None


def _ad_formats(data):
    """Every format an ad counts under, as a LIST: a Meta carousel is a carousel AND an image
    (or video) ad, so one value would hide either the creative type or the carousel. The
    creative type is data["format"] (already media- and display_format-aware, see _ad_format).
    Then DYNAMIC when Meta calls it DCO (dynamic creative: its versions sit in the cards field
    but it is not a swipeable carousel; a 1-version DCO is still dynamic), else CAROUSEL when
    the ORIGINAL slot count is above one (card_count is counted before identical copy is
    deduped, so an all-identical carousel still counts). Same rules as the viewer's
    recFormats(), moved here so the agent reading the index counts what the user sees."""
    out = []
    if data.get("format"):
        out.append(data["format"])
    slots = data.get("card_count")
    if slots is None:
        slots = len(data.get("cards") or [])
    if str(data.get("display_format") or "").upper() == "DCO":
        out.append("DYNAMIC")
    elif slots > 1:
        out.append("CAROUSEL")
    return list(dict.fromkeys(out))           # de-dupe, keep order


def _variant_copy(rec):
    """Google transparency ad copy. Google keeps no top-level body; the copy lives in
    variants[].textContent as a ' - '-joined string of headline/description/display-url, in
    which EMPTY parts are the literal token 'null' and the whole value sometimes carries a
    stray leading '[' (a stringified-list artifact from the actor). This is why Google TEXT
    ads read blank: the raw value was either 'null - null - null' or unparsed.

    Clean each variant (strip brackets, drop 'null'/empty parts), dedupe identical variants
    (variant 0/1 are usually the same creative), join distinct ones with a newline. Returns ''
    when no real copy survives -- most transparency TEXT ads genuinely have none, and an honest
    blank beats surfacing 'null - null - null' as if it were the advertiser's copy. Splitting on
    ' - ' (space-dash-space) is safe: the ad's real hyphenates ('Immediate-Response') carry no
    surrounding spaces, so a real segment is never split."""
    variants = rec.get("variants")
    if not isinstance(variants, list):
        return ""
    seen = []
    for v in variants:
        if not isinstance(v, dict):
            continue
        t = v.get("textContent")
        if not isinstance(t, str):
            continue
        s = t.strip().lstrip("[").rstrip("]").strip()
        parts = [p.strip() for p in s.split(" - ")]
        parts = [p for p in parts if p and p.lower() != "null"]
        cleaned = " - ".join(parts)
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return "\n".join(seen)


def _body_text(rec, snap):
    """The ad's body copy. LinkedIn/Google keep it top-level (or not at all); Meta nests it
    at snapshot.body.text. Handles both a {'text': ...} dict and a bare string. Falls back to
    Google's variants[].textContent (cleaned) when nothing else carries copy."""
    body = _first(rec, "body", "bodyText", "text")
    if isinstance(body, dict):
        body = body.get("text")
    if not body:
        sb = snap.get("body")
        if isinstance(sb, dict):
            body = sb.get("text")
        elif isinstance(sb, str):
            body = sb
    if not body:
        body = _variant_copy(rec)  # Google transparency ads (no other copy field)
    return body or ""


def _cards(snap):
    """Meta carousel cards -> a list of per-card {title, body, cta_text, link_url, caption}.

    Cards live at snapshot.cards (never top-level 'cards', which is why the old card_count read
    blank for every Meta ad). This surfaces the TEXT of each card: the sequence of messages and
    per-card destinations is the marketing signal a carousel carries.

    Text only, on purpose. The card images ARE downloaded (they land in the ad's media[]), but
    creatives.py flattens every card's URLs into one indexed list and the manifest records no
    card boundary -- so there is no reliable card->file mapping to recover here. Re-deriving
    creatives.py's exact URL ordering would be fragile (silent wrong-image-under-wrong-card if
    that ordering ever changes); recording the card index at download time would be correct but
    needs a re-scrape. So the viewer shows the card copy here and the downloaded creatives in the
    ad's existing gallery, rather than guessing a mapping."""
    cards = snap.get("cards")
    if not isinstance(cards, list):
        return []
    out = []
    for c in cards:
        if not isinstance(c, dict):
            continue
        card = {}
        for out_key, in_key in (("title", "title"), ("body", "body"),
                                ("cta_text", "cta_text"), ("link_url", "link_url"),
                                ("caption", "caption")):
            v = c.get(in_key)
            if isinstance(v, str):
                v = v.strip()
            if v and v != "None":
                card[out_key] = v
        if card:
            out.append(card)
    return out


def _dedupe_cards(cards):
    """Collapse cards whose COPY is identical (same title/body/cta_text/link_url/caption),
    preserving first-seen order. A carousel whose 3 slots carry the exact same message is a
    carousel in format only -- showing three identical cards as if the sequence meant something
    is misleading, so the viewer shows the distinct copy once and states the original slot count
    (card_count) beside it. Text-only, so two slots differing only by image collapse here -- that
    is correct for a copy view (the images live in the ad's creative gallery)."""
    seen = set()
    out = []
    for c in cards:
        key = tuple(c.get(k) for k in ("title", "body", "cta_text", "link_url", "caption"))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _original_ad_url(rec, platform):
    """Link to the ORIGINAL ad in its library, not the advertiser's landing page.
    Meta: ad_library_url (the specific ad; top-level 'url' is a whole-library search).
    LinkedIn: adLibraryUrl. Google: the transparency 'url' (per-creative)."""
    url = _first(rec, "ad_library_url", "adLibraryUrl")
    if not url and platform == "google":
        url = _first(rec, "url", "previewUrl")
    return url


def _ad_envelope(rec, platform, subject, run_file, raw_index, manifest, creatives_subdir):
    """Map ONE ad record -> the shared envelope. Pure; no I/O.
    Uses canonical field names (the LI aliases from postprocess._pp_li are already
    applied on disk, so body/format/firstShownAt/etc. are present for LinkedIn too)."""
    rec_id = _native_id(rec)
    uid = f"{platform}:{rec_id if rec_id else raw_index}"
    snap = _snap(rec)
    media = _media_from_manifest(rec_id, manifest, creatives_subdir)
    body = _body_text(rec, snap)
    advertiser = _first(rec, "advertiserName", "page_name", "pageName") or snap.get("page_name")

    # Dates: normalize to ISO for display AND keep the epoch for duration/sort. Meta gives
    # both an epoch (start_date) and a human string (start_date_formatted); Google gives an
    # epoch (firstShownAt); LinkedIn gives neither, so these stay absent for it.
    first_shown = _norm_date(rec.get("start_date_formatted"), rec.get("start_date"),
                             _first(rec, "firstShownAt", "firstShownDate"))
    last_shown = _norm_date(rec.get("end_date_formatted"), rec.get("end_date"),
                            _first(rec, "lastShownAt", "lastShownDate"))
    first_ts = _epoch_seconds(rec.get("start_date"), _first(rec, "firstShownAt"))
    last_ts = _epoch_seconds(rec.get("end_date"), _first(rec, "lastShownAt"))
    duration_days = None
    if first_ts and last_ts and last_ts >= first_ts:
        duration_days = round((last_ts - first_ts) / 86400)

    data = {}
    def put(key, val):
        if val not in (None, ""):
            data[key] = val

    put("advertiser_name", advertiser)
    put("headline", _first(rec, "title", "headline") or snap.get("title"))
    put("cta_text", _first(rec, "cta_text", "ctaText") or snap.get("cta_text")
        or (rec.get("ctas") or [None])[0])
    put("format", _ad_format(rec, snap, media))
    # The advertiser's landing page: only what the ad itself carries. The Google
    # actor puts `originUrl` on every record, and it is the scraper's own search
    # address (adstransparency.google.com/?region=..&domain=..), not a page the ad
    # sends anyone to. It used to be the last fallback here, so every Google ad's
    # "landing" link opened Google's search page for the advertiser (150 of 150
    # Google items in the real runs; Meta and LinkedIn items never carry it).
    # The Google actor gives no destination, so a Google ad has none, and the
    # app shows no landing link for it. The ad's own page stays under ad_url.
    put("link_url", _first(rec, "link_url", "linkUrl") or snap.get("link_url"))
    put("ad_url", _original_ad_url(rec, platform))  # the ad in its library (the "original")
    put("caption", snap.get("caption"))       # the display domain Meta shows under the ad
    put("first_shown", first_shown)
    put("last_shown", last_shown)
    put("first_shown_ts", first_ts)
    put("last_shown_ts", last_ts)
    put("duration_days", duration_days)
    # Impressions: Meta ships a RANGE STRING at impressions_with_index.impressions_text (e.g.
    # "1K-5K"). The old totalImpressionsMax/totalImpressions keys do not exist in any of these
    # actor shapes (0/99 across the calibration run) -- that lookup was a dead path that never
    # populated. Google/LinkedIn expose no usable single impressions value. Null for small
    # advertisers (as in the calibration run), so the field is simply absent then -- honest.
    iwi = rec.get("impressions_with_index")
    if isinstance(iwi, dict):
        put("impressions_text", iwi.get("impressions_text"))
    put("native_id", rec_id)                  # lets the viewer distinguish near-identical ads
    cards = _cards(snap)                       # Meta carousel: per-card copy (see _cards)
    if cards:
        # Show DISTINCT copy once; keep the original slot count so the viewer can say
        # "3-card carousel" and flag "3 identical cards" when the dedup collapsed them.
        data["cards"] = _dedupe_cards(cards)
        data["card_count"] = len(cards)
    # Meta's own format label: IMAGE / VIDEO / DCO / CAROUSEL. Absent for Google/LinkedIn
    # (put drops None). Passed forward so the viewer never has to re-derive it from cards
    # presence -- that heuristic misidentifies DCO ads (dynamic creative, not swipeable
    # carousels) as carousels.
    put("display_format", snap.get("display_format"))

    # Scale / placement / targeting -- present in the actor shapes but never emitted before.
    put("link_description", snap.get("link_description"))        # Meta secondary copy (20/59)
    put("page_like_count", snap.get("page_like_count"))         # Meta follower count = scale
    pubp = rec.get("publisher_platform")                        # TOP-LEVEL on the record, not snapshot
    if isinstance(pubp, list) and pubp:
        data["publisher_platforms"] = pubp                      # FB / IG / Threads / ...
    if isinstance(snap.get("page_categories"), list) and snap["page_categories"]:
        put("page_category", snap["page_categories"][0])        # Meta advertiser category
    put("paid_by", _first(rec, "paidBy"))                       # LinkedIn payer (vs the brand)
    aud = rec.get("audienceSelections")                          # Google targeting dimensions
    if isinstance(aud, list):
        names = [a.get("name") for a in aud if isinstance(a, dict) and a.get("name")]
        if names:
            data["audience_selections"] = names

    tags = {}
    if data.get("format"):
        tags["format"] = data["format"]
    formats = _ad_formats(data)
    if formats:
        tags["formats"] = formats
    if data.get("advertiser_name"):
        tags["advertiser"] = data["advertiser_name"]
    # Active state: Meta ships a real is_active bool; others are inferred from date presence.
    if isinstance(rec.get("is_active"), bool):
        tags["active"] = "active" if rec["is_active"] else "ended"
    elif first_shown or last_shown:
        tags["active"] = "known_dates"

    return {
        "uid": uid,
        "kind": "ad",
        "platform": platform,
        "subject": advertiser or subject,
        "text": body,
        "date": last_shown or first_shown,
        "url": data.get("link_url"),
        "lang": rec.get("language") or rec.get("lang"),
        "media": media,
        "tags": tags,
        "raw_ref": {"file": run_file, "index": raw_index},
        "data": data,
    }


# The name a set-aside ad is listed under when its record carries no advertiser
# name. The app's card says the same words.
SET_ASIDE_NO_NAME = "(no advertiser name given)"


def set_aside_name(name):
    """The name a set-aside ad is listed under: the advertiser name the source gave,
    or SET_ASIDE_NO_NAME when it gave none. A name of only spaces counts as none
    (it gave a blank card header and a blank report cell;
    resolve.google_ad_owners already strips it and calls the ad unnamed). For set-aside
    ads only: a shown ad keeps its own fallback to the competitor."""
    return name if str(name or "").strip() else SET_ASIDE_NO_NAME


def labelled_set_aside(ads, entries, owner, subject, run_file, manifest, creatives_subdir,
                       platform="google"):
    """The set-aside Google ads themselves, for showing under their label.

    WHY ("label them"): a set-aside ad may be the competitor's
    own under another legal name ("37signals LLC" for Basecamp, a parent group for a
    franchise), and a count with a name did not let the user read what the ads say.
    They are shown, but only here: this lives inside the index's "set_aside" entry,
    never in "records", so no count, filter, favourite or month-to-month comparison
    can include them (nothing is presented as the competitor's unless it is confirmed). Each ad
    is the same envelope a shown ad gets
    (_ad_envelope), plus "set_aside": {"reason": "other" | "unnamed", "searched":
    the website whose search found it}. The labels and the way to confirm come from
    resolve.py, so the app and the report say the same words.
    Pure; no I/O. Returns {"labels": [...], "confirm": sentence, "ads": [...]}.

    "subject" of a labelled ad is the advertiser name the source gave, or
    SET_ASIDE_NO_NAME, never the run's subject: a shown ad with no name falls back
    to the competitor because it is confirmed as theirs; a set-aside one is not
    (an unnamed ad found by searching one website was headed with another competitor's name in
    the app while the report said "(no name)")."""
    out_ads = []
    for i, rec in enumerate(ads):
        if not isinstance(rec, dict) or owner[i] == "own":
            continue
        env = _ad_envelope(rec, platform, subject, run_file, i, manifest, creatives_subdir)
        env["subject"] = set_aside_name((env.get("data") or {}).get("advertiser_name"))
        env["set_aside"] = {"reason": owner[i], "searched": RS.google_searched_domain(rec)}
        out_ads.append(env)
    return {"labels": RS.google_set_aside_labels(ads, entries, owner),
            "confirm": RS.GOOGLE_CONFIRM_HOW,
            "ads": out_ads}


def build_index(workdir, cfg):
    """Assemble and write <workdir>/frontend_index.json. Returns the Path."""
    workdir = Path(workdir)
    subject = (cfg or {}).get("subject") or _subject_from_dirname(workdir)

    records = []
    by_platform = {}
    set_aside = {}
    entries = _read_json(workdir / "competitors" / "resolution.json", []) or []
    # A "google_advertisers" key the tool does not read (put directly in
    # the entry, say) changed nothing, and the rebuild said nothing. Say so on stderr,
    # one line each, starting "WARNING: " (viewer_ctl.py reindex passes these lines on
    # as "warnings"). Which ads are set aside is unchanged.
    for w in RS.google_advertisers_misplaced(entries):
        print(f"WARNING: {w}", file=sys.stderr)
    for platform, (run_file, creatives_subdir) in _PLATFORMS.items():
        ads = _read_json(workdir / run_file, [])
        if not isinstance(ads, list):
            ads = []
        manifest = _read_json(workdir / creatives_subdir / "manifest.json", {}) or {}
        by_platform[platform] = 0
        # A Google ad whose advertiser is not the competitor is not
        # shown as the competitor's: it is left out of the index's records, and
        # counted and named under "set_aside" (the same words the report uses);
        # The ads themselves sit there too, under their label.
        owner = RS.google_ad_owners(ads, entries) if platform == "google" else None
        if owner is not None:
            lines = RS.google_set_aside_lines(ads, entries, owner)
            if lines:
                # "summary" is the one sentence the app
                # shows next to the ad count; "uids" are the uids these ads would
                # have if shown (native ids only: a positional uid matches nothing
                # across months), so the month-to-month comparison can tell an ad
                # that moved between shown and set aside from one that is new or
                # no longer there (compare.py).
                set_aside[platform] = {
                    "count": sum(v != "own" for v in owner),
                    "summary": RS.google_set_aside_sentence(owner),
                    "lines": lines,
                    "uids": [f"{platform}:{_native_id(rec)}"
                             for rec, v in zip(ads, owner)
                             if v != "own" and isinstance(rec, dict) and _native_id(rec)],
                    "labelled": labelled_set_aside(ads, entries, owner, subject, run_file,
                                                   manifest, creatives_subdir, platform),
                }
        for i, rec in enumerate(ads):
            if not isinstance(rec, dict):
                continue
            if owner is not None and owner[i] != "own":
                continue
            records.append(
                _ad_envelope(rec, platform, subject, run_file, i, manifest, creatives_subdir)
            )
            by_platform[platform] += 1

    media_local = sum(1 for r in records for m in r["media"])
    index = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_ID,
        "run_id": workdir.name,
        "subject": subject,
        "repeat_of": (cfg or {}).get("repeat_of") or None,
        "generated_at": _now_iso(),
        "config_summary": {
            "download_creatives": (cfg or {}).get("download_creatives"),
            "country": (cfg or {}).get("country") or (cfg or {}).get("country_code"),
        },
        "counts": {"records": len(records), "by_platform": by_platform,
                   "creatives_on_disk": media_local},
        "synthesis": None,  # ad-intelligence has no counted taxonomy file; E3 is the report
        "reports": _discover_reports(workdir),
        "records": records,
    }
    if set_aside:
        index["set_aside"] = set_aside
    # Present only when a step of this run did not finish (failed, stopped
    # by the budget cap, or a scraper run that did not work), so the app and the month
    # comparison can tell that apart from a step that ran and found nothing. The
    # sentences are made once, in postprocess.unfinished_steps; a normal index is
    # byte-identical to what it was.
    import postprocess as _PP  # noqa: E402
    unfinished = _PP.unfinished_steps(workdir)
    if unfinished:
        index["unfinished_steps"] = unfinished
    out = workdir / "frontend_index.json"
    _write_atomic(out, index)
    active_platforms = sum(1 for v in by_platform.values() if v)
    print(f"   emit_index -> frontend_index.json  "
          f"({len(records)} ads across {active_platforms} platform(s); "
          f"{media_local} local creative(s))")
    return out


def _discover_reports(workdir):
    """Link the E3 report (report bundle writes report/E3_creative_angles.md)."""
    candidates = [
        ("E3", "Creative angles", "report/E3_creative_angles.md"),
        ("E3", "Creative angles", "E3_creative_angles.md"),
    ]
    seen, out = set(), []
    for rid, title, rel in candidates:
        if rid in seen:
            continue
        if (Path(workdir) / rel).exists():
            out.append({"id": rid, "title": title, "path": rel})
            seen.add(rid)
    return out


_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_VN_RE = re.compile(r'^v\d+$', re.IGNORECASE)


def _subject_from_dirname(workdir):
    """Best-effort subject from a run dir named <prefix>_<slug>_<YYYY-MM-DD>[_vN].
    The slug may contain underscores: everything between the prefix and the date.
    Unconventional names (no date found, empty slug, unrecognized prefix) return as-is."""
    name = Path(workdir).name
    parts = name.split("_")
    if len(parts) >= 3 and parts[0] in ("adintel", "ad"):
        for i, part in enumerate(parts):
            if _DATE_RE.match(part):
                slug_parts = parts[1:i]
                if slug_parts:
                    return " ".join(slug_parts).replace("-", " ").title()
                break
    return name


def _write_atomic(path, index):
    """Write the index to a temp file, then rename it over the old one. The local app may be
    reading frontend_index.json while a re-index runs; a half-written file would show the
    user an empty or broken run."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def reindex(workdir, config_path=None):
    """Rebuild <run>/frontend_index.json from the data already on disk. Free: no network, no
    scrape, no model. For a run indexed by an older version of this skill, or whose index is
    missing, so the local app shows it without paying for a new scrape.

    Settings, in order of precedence: the --config file when given (the same JSON the run
    used), then what the run's existing index already recorded (its subject and
    config_summary), then what the folder itself shows. The previous index is kept at
    .viewer/frontend_index.before_reindex.json, and a rebuild that finds nothing where the
    old index had records is refused and the old index put back, because that means the
    wrong folder or a missing file, not an empty run."""
    workdir = Path(workdir)
    if not workdir.is_dir():
        raise SystemExit(f"No run folder at {workdir}")
    cfg = {}
    if config_path:
        cfg = _read_json(Path(config_path), None)
        if not isinstance(cfg, dict):
            raise SystemExit(f"Could not read the config at {config_path} as a JSON object")
    out = workdir / "frontend_index.json"
    prev = _read_json(out, {}) if out.exists() else {}
    if not isinstance(prev, dict):
        prev = {}
    if prev.get("tool") not in (None, TOOL_ID):
        raise SystemExit(f"{workdir.name} was indexed by the '{prev.get('tool')}' tool, and this "
                         f"is the '{TOOL_ID}' tool's re-indexer: use that tool's emit_index.py")
    if prev.get("subject") and not cfg.get("subject"):
        cfg["subject"] = prev["subject"]
    for k, v in (prev.get("config_summary") or {}).items():
        if v not in (None, "", []) and cfg.get(k) in (None, "", []):
            cfg[k] = v
    backup = None
    if out.exists():
        (workdir / ".viewer").mkdir(exist_ok=True)
        backup = workdir / ".viewer" / "frontend_index.before_reindex.json"
        shutil.copy2(out, backup)
    path = build_index(workdir, cfg)
    new_n = len((_read_json(path, {}) or {}).get("records") or [])
    old_n = len(prev.get("records") or [])
    if new_n == 0 and old_n > 0:
        shutil.copy2(backup, out)
        raise SystemExit(f"Re-index found 0 records where the old index had {old_n}; the old "
                         f"index was kept. Check that this is the right run folder.")
    return path


def _cli(argv):
    import argparse
    ap = argparse.ArgumentParser(
        description="Free re-index of one run for the local app. Run with no arguments to "
                    "run this file's self-tests instead.")
    ap.add_argument("--workdir", required=True, help="the run folder")
    ap.add_argument("--config", default=None, help="the run's config JSON (optional)")
    a = ap.parse_args(argv)
    reindex(a.workdir, a.config)
    return 0


# ---------------------------------------------------------------------------
# Inline verification tests -- in-memory, no files/network/Apify/LLM.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) > 1:              # --workdir <run> [--config <file>]: re-index
        sys.exit(_cli(sys.argv[1:]))
    PASS = FAIL = 0

    def check(label, got, expected):
        global PASS, FAIL
        if got == expected:
            print(f"  PASS  {label}"); PASS += 1
        else:
            print(f"  FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")
            FAIL += 1

    # Meta ad with a downloaded image in the manifest. Impressions live in Meta's real field
    # (impressions_with_index.impressions_text), a RANGE STRING -- not totalImpressionsMax.
    meta = {"ad_archive_id": "123", "advertiserName": "HubSpot", "body": "Try our CRM",
            "title": "Free CRM", "cta_text": "Sign Up", "creativeType": "video",
            "firstShownAt": "2026-07-01", "lastShownAt": "2026-08-01",
            "impressions_with_index": {"impressions_text": "10K-50K", "impressions_index": 3},
            "link_url": "https://hubspot.com"}
    manifest = {"123": ["123_0.jpg", "FAILED: timeout", "123_1.mp4"]}
    env = _ad_envelope(meta, "meta", "HubSpot", "B4_meta_ad_library/run_02_full.json", 0, manifest, "creatives/meta")
    check("kind ad", env["kind"], "ad")
    check("uid uses archive id", env["uid"], "meta:123")
    check("advertiser as subject", env["subject"], "HubSpot")
    check("body -> text", env["text"], "Try our CRM")
    check("format tag uppercased", env["tags"]["format"], "VIDEO")
    check("impressions_text from impressions_with_index", env["data"]["impressions_text"], "10K-50K")
    check("dead impressions_max key not emitted", "impressions_max" in env["data"], False)
    check("media skips FAILED, keeps 2", len(env["media"]), 2)
    check("image path run-relative", env["media"][0], {"type": "image", "path": "creatives/meta/123_0.jpg"})
    check("video type inferred", env["media"][1]["type"], "video")

    # Meta's REAL shape: body/title/cta nested in snapshot, epoch start/end dates, and the
    # per-ad library URL. This is the case the old envelope silently dropped (text="").
    meta2 = {"ad_archive_id": "999", "page_name": "Acme Plumbing", "is_active": False,
             "start_date": 1780297200, "end_date": 1781679600,           # 2026-06-01 .. 2026-06-17
             "start_date_formatted": "2026-06-01 07:00:00",
             "publisher_platform": ["FACEBOOK", "INSTAGRAM", "THREADS"],  # TOP-LEVEL, not in snapshot
             "ad_library_url": "https://www.facebook.com/ads/library/?id=999",
             "snapshot": {"body": {"text": "Emergency plumber, 24/7"}, "title": "Fast drains",
                          "cta_text": "Call now", "caption": "acmeplumbing.com",
                          "link_url": "https://acmeplumbing.com", "display_format": "IMAGE",
                          "videos": [], "link_description": "Fast, upfront pricing",
                          "page_like_count": 418523, "page_categories": ["Plumber", "Local service"],
                          "cards": [
                              {"title": "Drain cleaning", "body": "Same-day service",
                               "cta_text": "Book", "link_url": "https://rr.com/drains",
                               "video_preview_image_url": "None"},
                              {"title": "Water heaters", "body": "Install & repair"},
                              {"caption": "only a caption, no title/body"},  # still a real card
                          ]}}
    e2 = _ad_envelope(meta2, "meta", "Acme Plumbing", "f", 1, {}, "creatives/meta")
    check("meta nested body.text -> text", e2["text"], "Emergency plumber, 24/7")
    check("meta nested headline", e2["data"]["headline"], "Fast drains")
    check("meta nested cta", e2["data"]["cta_text"], "Call now")
    # B2 carousel: cards come from snapshot.cards (not top-level 'cards'), text only.
    check("carousel card_count from snapshot.cards", e2["data"]["card_count"], 3)
    check("carousel card0 title", e2["data"]["cards"][0]["title"], "Drain cleaning")
    check("carousel card0 link", e2["data"]["cards"][0]["link_url"], "https://rr.com/drains")
    check("carousel card1 body", e2["data"]["cards"][1]["body"], "Install & repair")
    check("carousel drops 'None'-string image field (text only)",
          "video_preview_image_url" in e2["data"]["cards"][0], False)
    # Carousel with 3 IDENTICAL slots -> one distinct card shown, original slot count kept.
    meta_dup = {"ad_archive_id": "9", "snapshot": {"body": {"text": "b"}, "cards": [
        {"title": "Same", "body": "One message", "link_url": "https://x.com/a"},
        {"title": "Same", "body": "One message", "link_url": "https://x.com/a"},
        {"title": "Same", "body": "One message", "link_url": "https://x.com/a"}]}}
    edup = _ad_envelope(meta_dup, "meta", "X", "f", 2, {}, "creatives/meta")
    check("identical carousel: card_count keeps original slot count", edup["data"]["card_count"], 3)
    check("identical carousel: only distinct copy shown", len(edup["data"]["cards"]), 1)
    # Partial dedup: 3 slots, 2 distinct -> 2 shown, count still 3.
    meta_part = {"ad_archive_id": "10", "snapshot": {"body": {"text": "b"}, "cards": [
        {"title": "A", "body": "one"}, {"title": "A", "body": "one"}, {"title": "B", "body": "two"}]}}
    epart = _ad_envelope(meta_part, "meta", "X", "f", 3, {}, "creatives/meta")
    check("partial-dup carousel: distinct shown", len(epart["data"]["cards"]), 2)
    check("partial-dup carousel: original count kept", epart["data"]["card_count"], 3)
    # Scale / placement.
    check("meta link_description", e2["data"]["link_description"], "Fast, upfront pricing")
    check("meta page_like_count", e2["data"]["page_like_count"], 418523)
    check("meta publisher_platforms list", e2["data"]["publisher_platforms"], ["FACEBOOK", "INSTAGRAM", "THREADS"])
    check("meta page_category (first)", e2["data"]["page_category"], "Plumber")
    # A Meta ad with no cards must not gain a cards key.
    check("no cards -> no cards key",
          "cards" in _ad_envelope({"ad_archive_id": "7", "snapshot": {"body": {"text": "x"}}},
                                  "meta", "X", "f", 0, {}, "creatives/meta")["data"], False)
    check("meta epoch start -> ISO", e2["data"]["first_shown"], "2026-06-01")
    check("meta epoch end -> ISO", e2["data"]["last_shown"], "2026-06-17")
    check("meta duration days computed", e2["data"]["duration_days"], 16)
    check("meta original-ad url", e2["data"]["ad_url"], "https://www.facebook.com/ads/library/?id=999")
    check("meta landing != ad url", e2["data"]["link_url"], "https://acmeplumbing.com")
    check("meta format from display_format (no creative)", e2["data"]["format"], "IMAGE")
    check("meta is_active False -> ended", e2["tags"]["active"], "ended")
    check("meta native_id kept", e2["data"]["native_id"], "999")

    # Google: format is clean top-level, but firstShownAt/lastShownAt are EPOCHS the viewer
    # was showing raw. Normalize to ISO; transparency 'url' is the original ad.
    g = {"id": "G1", "advertiserName": "Acme", "format": "TEXT",
         "firstShownAt": 1759156928, "lastShownAt": 1787807263,
         "url": "https://adstransparency.google.com/advertiser/AR/creative/CR"}
    genv = _ad_envelope(g, "google", "Acme", "f", 0, {}, "creatives/google")
    check("google epoch -> ISO date", genv["data"]["first_shown"], "2025-09-29")
    check("google transparency url as ad_url", genv["data"]["ad_url"], "https://adstransparency.google.com/advertiser/AR/creative/CR")

    # B3 Google copy: variants[].textContent is a ' - '-joined string; empty parts are 'null'
    # and a stray '[' can lead. Clean, dedupe identical variants, drop all-null (-> blank).
    gcopy = {"id": "G2", "advertiserName": "Acme", "format": "TEXT",
             "variants": [
                 {"images": [], "textContent": "[Prompt Plumbing - Since 1970. Call Now. - bestdrains.com"},
                 {"images": [], "textContent": "Prompt Plumbing - Since 1970. Call Now. - bestdrains.com"},  # dup after strip
                 {"images": [], "textContent": "null - null - null"},  # no real copy
             ]}
    gc = _ad_envelope(gcopy, "google", "Acme", "f", 0, {}, "creatives/google")
    check("google variant copy cleaned + deduped", gc["text"],
          "Prompt Plumbing - Since 1970. Call Now. - bestdrains.com")
    gblank = _ad_envelope({"id": "G3", "format": "TEXT",
                           "variants": [{"textContent": "null - null - null"},
                                        {"textContent": "[null - null - null"}]},
                          "google", "Acme", "f", 1, {}, "creatives/google")
    check("google all-null variants -> honest blank", gblank["text"], "")
    gaud = _ad_envelope({"id": "G4", "format": "TEXT",
                         "audienceSelections": [{"name": "Demographic info"}, {"name": "Geographic locations"}, {}]},
                        "google", "Acme", "f", 2, {}, "creatives/google")
    check("google audience_selections (names only)", gaud["data"]["audience_selections"],
          ["Demographic info", "Geographic locations"])

    # LinkedIn: body/format top-level; NO dates. Honest-unknown must collapse to absent,
    # not the literal string 'unknown' (which the viewer would render as a fake date).
    li = {"adId": "L9", "advertiserName": "Acme", "body": "B2B leads", "format": "video",
          "firstShownAt": "unknown", "adLibraryUrl": "https://www.linkedin.com/ad-library/detail/9",
          "paidBy": "STRIKE EXCHANGE, L.L.C", "ctas": ["Learn more"]}
    lenv = _ad_envelope(li, "linkedin", "Acme", "LI_linkedin_ads/run_02_full.json", 3, {}, "creatives/linkedin")
    check("li uid", lenv["uid"], "linkedin:L9")
    check("li paid_by (payer vs brand)", lenv["data"]["paid_by"], "STRIKE EXCHANGE, L.L.C")
    check("li no media", lenv["media"], [])
    check("li unknown date collapses to absent", "first_shown" in lenv["data"], False)
    check("li format uppercased", lenv["data"]["format"], "VIDEO")
    check("li cta from ctas[0]", lenv["data"]["cta_text"], "Learn more")
    check("li adLibraryUrl as ad_url", lenv["data"]["ad_url"], "https://www.linkedin.com/ad-library/detail/9")

    # Record with no id -> uid falls back to index.
    noid = {"advertiserName": "X", "body": "hi"}
    check("no-id uid uses index", _ad_envelope(noid, "meta", "X", "f", 5, {}, "creatives/meta")["uid"], "meta:5")

    check("subject from dirname", _subject_from_dirname(Path("/x/adintel_hub-spot_2026-09-19")), "Hub Spot")
    # Folder parser: _vN suffix is stripped, multi-word slug is preserved, unconventional falls back
    check("dirname: normal name (no suffix)", _subject_from_dirname(Path("/x/adintel_hubspot_2026-09-28")), "Hubspot")
    check("dirname: _v2 suffix is stripped — same subject as base run", _subject_from_dirname(Path("/x/adintel_hubspot_2026-09-28_v2")), "Hubspot")
    check("dirname: _v12 suffix is stripped", _subject_from_dirname(Path("/x/ad_hub-spot_2026-09-28_v12")), "Hub Spot")
    check("dirname: slug with underscores parsed correctly", _subject_from_dirname(Path("/x/adintel_acme-plumbing_drains_2026-09-28")), "Acme Plumbing Drains")
    check("dirname: unconventional name (no date) returns as-is", _subject_from_dirname(Path("/x/pain_acme-fitness_nodatehereXYZ")), "pain_acme-fitness_nodatehereXYZ")

    # display_format pass-through: the viewer must not re-derive format from cards.
    # (a) DCO ad: emits display_format "DCO" AND still emits cards.
    dco_rec = {"ad_archive_id": "2066067023941388",
               "snapshot": {"display_format": "DCO",
                            "cards": [{"title": "Version A", "body": "copy a"},
                                      {"title": "Version B", "body": "copy b"}]}}
    edco = _ad_envelope(dco_rec, "meta", "DCO Corp", "f", 6, {}, "creatives/meta")
    check("DCO: display_format emitted as 'DCO'", edco["data"].get("display_format"), "DCO")
    check("DCO: cards still emitted alongside display_format", len(edco["data"].get("cards", [])), 2)
    # (b) CAROUSEL ad: emits display_format "CAROUSEL".
    car_rec = {"ad_archive_id": "C5555",
               "snapshot": {"display_format": "CAROUSEL",
                            "cards": [{"title": "Card A"}, {"title": "Card B"}]}}
    ecar = _ad_envelope(car_rec, "meta", "X", "f", 7, {}, "creatives/meta")
    check("CAROUSEL: display_format emitted as 'CAROUSEL'", ecar["data"].get("display_format"), "CAROUSEL")
    # (c) Ad without display_format: key must be absent (put drops None).
    enodf = _ad_envelope({"ad_archive_id": "6666", "snapshot": {"body": {"text": "x"}}},
                         "meta", "X", "f", 8, {}, "creatives/meta")
    check("no display_format in snapshot -> key absent from data", "display_format" in enodf["data"], False)

    # tags.formats: the same list the viewer's recFormats() derives, so the agent counts what
    # the user sees. Creative type first, then DYNAMIC (DCO) or CAROUSEL (>1 original slot).
    check("formats: plain video", env["tags"]["formats"], ["VIDEO"])
    check("formats: 3-slot carousel with display_format IMAGE", e2["tags"]["formats"], ["IMAGE", "CAROUSEL"])
    check("formats: identical 3-slot carousel still a carousel", edup["tags"]["formats"], ["CAROUSEL"])
    check("formats: DCO is DYNAMIC, not CAROUSEL", edco["tags"]["formats"], ["DCO", "DYNAMIC"])
    check("formats: display_format CAROUSEL is not listed twice", ecar["tags"]["formats"], ["CAROUSEL"])
    check("formats: google text ad", genv["tags"]["formats"], ["TEXT"])
    one_dco = _ad_envelope({"ad_archive_id": "11", "snapshot": {"display_format": "dco",
                            "cards": [{"title": "only"}]}}, "meta", "X", "f", 9, {}, "creatives/meta")
    check("formats: 1-version DCO still DYNAMIC (case-insensitive)", one_dco["tags"]["formats"], ["DCO", "DYNAMIC"])
    check("formats: no format known -> no key",
          "formats" in _ad_envelope({"ad_archive_id": "12"}, "meta", "X", "f", 10, {}, "creatives/meta")["tags"], False)

    # P2: repeat_of emitted into index
    with tempfile.TemporaryDirectory() as _td:
        _wd = Path(_td) / "adintel_hubspot_2026-09-28"
        _wd.mkdir()
        (_wd / "AD_INTEL_CONFIG.json").write_text(
            '{"repeat_of":"adintel_hubspot_2026-08-28","subject":"HubSpot"}', encoding="utf-8")
        _idx = json.loads(build_index(_wd, {"repeat_of": "adintel_hubspot_2026-08-28"}).read_text())
        check("repeat_of emitted into index", _idx.get("repeat_of"), "adintel_hubspot_2026-08-28")
        _idx2 = json.loads(build_index(_wd, {}).read_text())
        check("repeat_of is null when absent from cfg", _idx2.get("repeat_of"), None)

    print(f"\n  {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
