"""
Subject resolver — turns the config's subject input into the
review_targets.json list of Maps place URLs that B2b consumes.

Three modes (auto-detected by which key is non-empty, in priority order):

  MODE 1 (competitor_place_urls non-empty):
    Write those URLs straight into B2a_maps_places/review_targets.json.
    pain_intel.py skips B2a entirely.  ($0 spend on discovery.)

  MODE 2 (competitor_names non-empty):
    (a) Before B2a: build_mode2_cfg() returns a cfg copy with the names
        injected into maps_search_queries; pain_intel.py writes that to
        a temp config and calls run_phase B2a with it.
    (b) After B2a: select_mode2_targets() reads B2a output, picks ONE place
        per name (highest title-token overlap, aggregator-guarded), and writes
        review_targets.json with ALL named competitors — no review-count cap
        (mode-2 keeps every resolved name, mode-3 caps by review count).

  MODE 3 (else — maps_search_queries or vertical+city):
    B2a runs with the config's existing search queries; postprocess._pp_b2a
    handles the bridge (sort by review count, cap at review_target_count).
    subjects.py is not called for mode 3.

Why subjects.py owns modes 1 and 2 but not 3:
  Mode 3 is the category-discovery path; _pp_b2a already
  implements it correctly.  Extending _pp_b2a to also handle mode 2 would
  conflate two distinct concerns (named-competitor best-match vs. category
  top-N) in the same function.  Keeping them separate makes each path unit-
  testable in isolation — important because a broken bridge produces a silent
  empty corpus.

Field-name note: the B2a fields this module reads -- `searchString`, `url`,
`title`, `reviewsCount` -- were CONFIRMED present in the real compass/
crawler-google-places output in live testing. The selector logic itself is
covered by 23 unit tests. The multi-key fallbacks handle actor-version variance
gracefully.
"""

import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Known Maps aggregator / directory title keywords.
# If a place's title contains any of these, it is not a business's own listing.
# The list is intentionally conservative: false positives (wrongly skipping a
# real competitor) are worse than false negatives (keeping a rare aggregator
# for manual review).
# ---------------------------------------------------------------------------
_AGGREGATOR_TITLE_TOKENS: frozenset = frozenset({
    "yelp", "angi", "angie", "homeadvisor", "thumbtack", "houzz",
    "porch", "bark", "tripadvisor", "google my business",
    "yellowpages", "whitepages", "superpages", "manta", "bbb",
    "checkatrade", "rated people", "trustpilot", "indeed", "glassdoor",
    "sitejabber", "foursquare", "zomato",
})


# ---------------------------------------------------------------------------
# Trustpilot subject resolution
# ---------------------------------------------------------------------------

def _registrable(netloc: str) -> str:
    """Crude registrable domain: netloc minus a leading 'www.' and any deeper
    subdomain, keeping the last two labels (good enough to compare
    acmeplumbing.com vs acme-plumbing.com; not TLD-perfect for co.uk-style suffixes,
    which is fine — a difference is surfaced for the human, never auto-applied)."""
    host = (netloc or "").split("@")[-1].split(":")[0].lower()
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _domain_final(domain: str, timeout: int = 12):
    """Reachability + redirect check for a bare domain. Returns
    (reachable: bool, final_registrable_domain: str). A HEAD that returns ANY
    HTTP status (even 403) means the domain is live; only a DNS/connection error
    means unreachable. Free — hits the company's OWN site, not trustpilot.com
    (which bot-walls a bare GET)."""
    import urllib.request
    import urllib.error
    from urllib.parse import urlparse
    req = urllib.request.Request(f"https://{domain}", method="HEAD",
                                 headers={"User-Agent": _ITUNES_UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, _registrable(urlparse(r.url).netloc)
    except urllib.error.HTTPError as e:
        # Reachable but the server refused HEAD (403/405/etc.) — still a live domain.
        u = getattr(e, "url", "") or f"https://{domain}"
        return True, _registrable(urlparse(u).netloc)
    except Exception:  # noqa: BLE001  (DNS/connection failure => not reachable)
        return False, ""


def resolve_trustpilot(cfg: dict, workdir=None) -> list:
    """Validate supplied Trustpilot targets (VALIDATE-ONLY — no name→domain
    search, by design, since Trustpilot users normally have the
    domain). For each trustpilot_domains entry:
      - full /review/ URL → passed through (kind='url').
      - bare domain → format-checked, then a free reachability + redirect check
        of the company's OWN site. A domain that does not resolve is flagged
        (likely a typo); a domain that redirects to a DIFFERENT registrable
        domain is surfaced (~) so the user confirms which one Trustpilot lists —
        this is exactly the acmeplumbing.com → acme-plumbing.com case that silently
        returned zero before.
    Writes trustpilot_reviews/resolution.json when workdir is given."""
    entries: list = []
    for raw in (cfg.get("trustpilot_domains") or []):
        raw = str(raw).strip()
        if not raw:
            continue
        if "/review/" in raw:
            entries.append({"input": raw, "kind": "url", "url": raw, "domain": "", "note": ""})
            continue
        if not _looks_like_domain(raw):
            entries.append({"input": raw, "kind": "domain", "url": "", "domain": "",
                            "note": "not a domain like vans.com or a /review/ URL — skipped"})
            continue
        reachable, final_dom = _domain_final(raw)
        url = f"https://www.trustpilot.com/review/{raw}"
        if not reachable:
            entries.append({"input": raw, "kind": "domain", "url": "", "domain": raw,
                            "note": "domain does not resolve (likely a typo) — Trustpilot would "
                                    "find nothing"})
        elif final_dom and final_dom != _registrable(raw):
            entries.append({"input": raw, "kind": "domain", "url": url, "domain": raw,
                            "redirect_to": final_dom,
                            "note": f"redirects to {final_dom} — Trustpilot may list this company "
                                    f"under {final_dom}; confirm which domain to use"})
        else:
            entries.append({"input": raw, "kind": "domain", "url": url, "domain": raw, "note": ""})

    if workdir is not None:
        stage_dir = Path(workdir) / "trustpilot_reviews"
        stage_dir.mkdir(parents=True, exist_ok=True)
        (stage_dir / "resolution.json").write_text(
            json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    return entries


def format_trustpilot_report(entries: list) -> str:
    """Human-facing Trustpilot validation report shown at the gate."""
    lines = ["", "-" * 60, "TRUSTPILOT VALIDATION — confirm before the paid scrape", "-" * 60]
    any_flag = False
    for e in entries:
        if e.get("url") and not e.get("note"):
            lines.append(f"  ✓ {e['input']!r} → {e['url']}")
        elif e.get("url") and e.get("redirect_to"):
            any_flag = True
            lines.append(f"  ~ {e['input']!r} → {e['url']}  ({e['note']})")
        else:
            any_flag = True
            lines.append(f"  ⚠ {e['input']!r} → {e.get('note','unresolved')}")
    lines.append("-" * 60)
    lines.append("CONFIRM the targets above (especially any ⚠/~) before approving spend."
                 if any_flag else "All Trustpilot domains validated.")
    return "\n".join(lines)


def resolve_trustpilot_targets(cfg: dict, workdir=None) -> list:
    """companyUrls for the Trustpilot stage. PURE/offline: prefers the persisted
    validation (trustpilot_reviews/resolution.json), else the original format
    normalization (bare domain → /review/ URL, /review/ URL passthrough)."""
    if workdir is not None:
        rp = Path(workdir) / "trustpilot_reviews" / "resolution.json"
        if rp.exists():
            try:
                entries = json.loads(rp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                entries = None
            if entries is not None:
                out, seen = [], set()
                for e in entries:
                    u = str(e.get("url") or "").strip()
                    if u and u not in seen:
                        seen.add(u)
                        out.append(u)
                return out
    urls = []
    for entry in (cfg.get("trustpilot_domains") or []):
        entry = str(entry).strip()
        if not entry:
            continue
        if "/review/" in entry:
            urls.append(entry)
        elif _looks_like_domain(entry):
            urls.append(f"https://www.trustpilot.com/review/{entry}")
        else:
            print(f"subjects [trustpilot]: WARNING -- skipping unrecognized entry: {entry!r} "
                  f"(expected a domain like vans.com or a full /review/ URL)")
    return urls


def _looks_like_domain(s: str) -> bool:
    """Return True if s looks like a bare domain (e.g. vans.com, www.vans.com).

    Conservative pattern: requires at least one dot and no path separators or
    protocol prefixes.  Junk strings, file paths, and http:// URLs that lack
    /review/ are caught as non-domains and skipped with a warning.
    """
    return bool(re.match(
        r'^(?:www\.)?[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?'
        r'(?:\.[a-zA-Z]{2,})+$',
        s,
    ))


# ---------------------------------------------------------------------------
# App Store subject resolution
# ---------------------------------------------------------------------------

def _extract_appstore_id(entry: str) -> str:
    """The numeric App Store id from a bare id or an apps.apple.com URL ('' if none).
    Pure — no network."""
    entry = str(entry).strip()
    m = re.search(r'/id(\d+)', entry)
    if m:
        return m.group(1)
    return entry if re.match(r'^\d+$', entry) else ""


# --- iTunes Search + Lookup (free, official, deterministic) -------------------
# App Store is the ONE pain source with a free first-party resolution API, so it
# does NOT go through the (paid) SERP path the other sources use. Search turns a
# name into a numeric id; Lookup validates a supplied id. Both degrade to [] on
# any network/parse failure — the caller then surfaces the miss (never a guess).

_ITUNES_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"


def _itunes_search(term: str, country: str = "us", limit: int = 5) -> list:
    """iTunes Search for software by name. Returns raw result dicts ([] on failure)."""
    import urllib.request
    import urllib.parse
    if not (term or "").strip():
        return []
    q = urllib.parse.urlencode({"term": term, "entity": "software",
                                "country": country or "us", "limit": limit})
    req = urllib.request.Request(f"https://itunes.apple.com/search?{q}",
                                 headers={"User-Agent": _ITUNES_UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return (json.loads(r.read().decode("utf-8", "ignore")) or {}).get("results", []) or []
    except Exception:  # noqa: BLE001  (network/parse — non-fatal; caller flags the miss)
        return []


def _itunes_lookup(app_id: str, country: str = "us") -> dict | None:
    """iTunes Lookup by numeric id — validates a supplied id resolves to a real
    app. Returns the app dict, or None if the id matches nothing / on failure."""
    import urllib.request
    import urllib.parse
    if not (app_id or "").strip():
        return None
    q = urllib.parse.urlencode({"id": app_id, "country": country or "us"})
    req = urllib.request.Request(f"https://itunes.apple.com/lookup?{q}",
                                 headers={"User-Agent": _ITUNES_UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            results = (json.loads(r.read().decode("utf-8", "ignore")) or {}).get("results", []) or []
    except Exception:  # noqa: BLE001
        return None
    return results[0] if results else None


def _appstore_name_score(query: str, track_name: str, seller: str) -> float:
    """Token-overlap of the query against the app's name+seller, minus a small
    penalty for extra app-name tokens (mirrors the ad-tool ranker). 0..~1."""
    def toks(s):
        return set(re.findall(r"[a-z0-9]+", (s or "").lower()))
    q = toks(query)
    if not q:
        return 0.0
    hay = toks(track_name) | toks(seller)
    overlap = len(q & hay)
    extra = len(toks(track_name) - q)
    return overlap / len(q) - 0.05 * extra


def _entry_from_app(raw_input: str, kind: str, app: dict, query: str) -> dict:
    """Build a resolution entry from an iTunes result dict + its evidence."""
    return {
        "input": raw_input, "kind": kind,
        "id": str(app.get("trackId") or ""),
        "app_name": app.get("trackName") or "",
        "seller": app.get("sellerName") or "",
        "bundle_id": app.get("bundleId") or "",
        "review_count": int(app.get("userRatingCount") or 0),
        "url": app.get("trackViewUrl") or "",
        "score": round(_appstore_name_score(query, app.get("trackName"), app.get("sellerName")), 3),
    }


def resolve_appstore(cfg: dict, workdir=None) -> list:
    """Resolve + VALIDATE App Store targets, evidence-first, before any spend.

    Two inputs, both funnelled through the free iTunes APIs so a non-technical
    user never has to hunt a numeric id and a wrong id never scrapes silently:
      - appstore_names: each name → iTunes Search → best candidate ranked by
        name-token match then review count (the real app has the most reviews).
      - appstore_app_ids: each supplied id/URL → iTunes Lookup → confirmed to a
        real app (its name/seller surfaced), or flagged unresolved.

    Returns a list of entries {input, kind, id, app_name, seller, review_count,
    url, score, candidates?, note?}. When workdir is given, also writes
    appstore_reviews/resolution.json (the report + the confirmed id list the AS
    stage reads) so resolution happens ONCE, inside the confirmation gate, and
    the paid stage reuses it. NEVER silently substitutes — a name that resolves
    to nothing, or an id that matches nothing, is returned with id='' + a note.
    """
    country = cfg.get("appstore_country", "us")
    entries: list = []

    for name in (cfg.get("appstore_names") or []):
        name = str(name).strip()
        if not name:
            continue
        results = _itunes_search(name, country, limit=5)
        if not results:
            entries.append({"input": name, "kind": "name", "id": "", "app_name": "",
                            "seller": "", "review_count": 0, "url": "", "score": 0.0,
                            "note": "no App Store match — verify the app name / country"})
            continue
        cands = [_entry_from_app(name, "name", a, name) for a in results]
        # RANKING (relevance gate, then popularity): among apps whose name/seller
        # actually contains the query (score > 0), the one the user means is
        # overwhelmingly the MOST-REVIEWED — "Notion" is the 90k-rating app, not
        # the 7k-rating "Notion Calendar" that edges it on name-token tidiness.
        # The gate is essential: without it, review_count alone would resolve
        # "Spotify" to YouTube (more ratings, zero name overlap). So relevance
        # decides eligibility; review count decides among the eligible; a tiny
        # name-score difference never overrides a large popularity gap. Fall back
        # to iTunes' own top result only if nothing clears the gate.
        gated = [e for e in cands if e["score"] > 0]
        ranked = sorted(gated or cands,
                        key=lambda e: (e["review_count"], e["score"]), reverse=True)
        best = dict(ranked[0])
        best["candidates"] = [{"app_name": r["app_name"], "seller": r["seller"],
                               "id": r["id"], "review_count": r["review_count"]}
                              for r in ranked[1:3]]
        entries.append(best)

    for raw in (cfg.get("appstore_app_ids") or []):
        raw = str(raw).strip()
        if not raw:
            continue
        app_id = _extract_appstore_id(raw)
        if not app_id:
            entries.append({"input": raw, "kind": "id", "id": "", "app_name": "",
                            "seller": "", "review_count": 0, "url": "", "score": 0.0,
                            "note": "not a numeric id or App Store URL — skipped"})
            continue
        app = _itunes_lookup(app_id, country)
        if app:
            e = _entry_from_app(raw, "id", app, app.get("trackName") or "")
            e["score"] = 1.0  # a validated exact id is maximally confident
            entries.append(e)
        else:
            # id="" so the target is NEVER passed to the paid scraper — a supplied
            # id that does not validate is a failure to surface, not a thing to scrape.
            entries.append({"input": raw, "kind": "id", "id": "", "app_name": "",
                            "seller": "", "review_count": 0, "url": "", "score": 0.0,
                            "note": f"id {app_id} did not resolve to a real App Store app "
                                    f"(check the id / country) — would scrape nothing"})

    if workdir is not None:
        stage_dir = Path(workdir) / "appstore_reviews"
        stage_dir.mkdir(parents=True, exist_ok=True)
        (stage_dir / "resolution.json").write_text(
            json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    return entries


def format_appstore_report(entries: list) -> str:
    """Human-facing App Store resolution report shown at the dry-run gate."""
    lines = ["", "-" * 60, "APP STORE RESOLUTION — confirm before the paid scrape", "-" * 60]
    any_flag = False
    for e in entries:
        if e.get("id") and not e.get("note"):
            lines.append(f"  ✓ {e['input']!r} → id {e['id']}  ({e['app_name']} · "
                         f"{e['seller']} · {e['review_count']:,} ratings)")
            for c in e.get("candidates") or []:
                lines.append(f"      alt: {c['app_name']} · {c['seller']} "
                             f"(id {c['id']}, {c['review_count']:,} ratings)")
        else:
            any_flag = True
            lines.append(f"  ⚠ {e['input']!r} → UNRESOLVED — {e.get('note','no match')}")
    lines.append("-" * 60)
    lines.append("CONFIRM the matches above (especially any ⚠) before approving spend."
                 if any_flag else "All App Store targets resolved and validated.")
    return "\n".join(lines)


def resolve_appstore_targets(cfg: dict, workdir=None) -> list:
    """Numeric App Store ids for the AS stage. PURE and offline by contract — the
    stage must never make network calls or depend on live iTunes state.

    Prefers the persisted resolution (appstore_reviews/resolution.json) written at
    the confirmation gate (that is where names get resolved and ids get validated,
    over the network, once). When no persisted resolution exists, falls back to a
    pure regex extraction of appstore_app_ids so a standalone `run_phase --only AS`
    with supplied ids still works — but it does NOT resolve appstore_names here
    (names require the gate's iTunes lookup; running the full pipeline writes the
    resolution first, so the stage always has it in the normal flow)."""
    if workdir is not None:
        rp = Path(workdir) / "appstore_reviews" / "resolution.json"
        if rp.exists():
            try:
                entries = json.loads(rp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                entries = None
            if entries is not None:
                ids, seen = [], set()
                for e in entries:
                    i = str(e.get("id") or "").strip()
                    if i and i not in seen:
                        seen.add(i)
                        ids.append(i)
                return ids
    # Pure fallback: extract ids from appstore_app_ids only (no network, no names).
    ids, seen = [], set()
    for raw in (cfg.get("appstore_app_ids") or []):
        i = _extract_appstore_id(raw)
        if not i:
            print(f"subjects [appstore]: WARNING -- skipping unrecognized entry: {str(raw).strip()!r} "
                  f"(expected a numeric id like '324684580' or an App Store URL)")
        elif i not in seen:
            seen.add(i)
            ids.append(i)
    return ids


# ---------------------------------------------------------------------------
# Google Play subject resolution
# ---------------------------------------------------------------------------

def _extract_gplay_package(entry: str) -> str:
    """The package name from a bare package or a Play URL ('' if neither). Pure."""
    entry = str(entry).strip()
    m = re.search(r'[?&]id=([^&\s]+)', entry)
    if m:
        return m.group(1)
    return entry if re.match(r'^[a-zA-Z][a-zA-Z0-9_]*(\.[a-zA-Z][a-zA-Z0-9_]*)+$', entry) else ""


# --- Google Play free resolution (public store pages, no paid actor) ----------
# Unlike App Store, Play has no first-party search API — but the public store
# search + details pages ARE readable with a plain GET (probed: 200, not walled),
# so name->package and package-validation are free. G2/Trustpilot/Capterra hard-
# block a bare GET, so THEY need a paid path; Play does not.

def _gplay_search(name: str, country: str = "us", limit: int = 8) -> list:
    """Ordered package ids from a Play store app search ([] on failure/block).
    Google relevance-ranks results, so order is meaningful."""
    import urllib.request
    import urllib.parse
    if not (name or "").strip():
        return []
    q = urllib.parse.urlencode({"q": name, "c": "apps", "hl": "en", "gl": (country or "us").lower()})
    req = urllib.request.Request(f"https://play.google.com/store/search?{q}",
                                 headers={"User-Agent": _ITUNES_UA, "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return []
    return list(dict.fromkeys(re.findall(r'/store/apps/details\?id=([a-zA-Z0-9._]+)', html)))


def _gplay_details(package: str, country: str = "us"):
    """(True, app_name) if a package resolves to a real Play app, (False, '')
    otherwise. Reads og:title on the public details page (name source + validator).
    None on a network error (distinct from 'does not exist')."""
    import urllib.request
    import urllib.parse
    if not (package or "").strip():
        return (False, "")
    q = urllib.parse.urlencode({"id": package, "hl": "en", "gl": (country or "us").lower()})
    import urllib.error
    req = urllib.request.Request(f"https://play.google.com/store/apps/details?{q}",
                                 headers={"User-Agent": _ITUNES_UA, "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return (False, "") if e.code == 404 else None
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r'<meta property="og:title" content="([^"]+)"', html)
    name = re.sub(r"\s*-\s*Apps on Google Play\s*$", "", m.group(1)).strip() if m else ""
    return (bool(name), name)


def resolve_googleplay(cfg: dict, workdir=None) -> list:
    """Resolve + VALIDATE Google Play targets, evidence-first, free.

      - googleplay_names: each name → Play search → the first relevance-ranked
        package whose details-page title matches the query (a real, name-matching
        app). Google already ranks by relevance, so the gate is name-match, not
        popularity (Play search does not expose a review count to rank on).
      - googleplay_app_ids: each supplied package/URL → details page → confirmed
        to a real app (name surfaced) or flagged unresolved.

    Writes googleplay_reviews/resolution.json when workdir is given. Never
    silently substitutes — an unresolved name/package carries id='' + a note.
    """
    country = cfg.get("googleplay_country", "US")
    entries: list = []

    for name in (cfg.get("googleplay_names") or []):
        name = str(name).strip()
        if not name:
            continue
        packages = _gplay_search(name, country)
        chosen = None
        qtoks = set(re.findall(r"[a-z0-9]+", name.lower()))
        for pkg in packages[:6]:
            ok, app_name = (_gplay_details(pkg, country) or (False, ""))
            if not ok:
                continue
            atoks = set(re.findall(r"[a-z0-9]+", app_name.lower()))
            if qtoks & atoks:  # relevance gate: the app name shares a query token
                chosen = {"input": name, "kind": "name", "id": pkg, "app_name": app_name,
                          "url": f"https://play.google.com/store/apps/details?id={pkg}",
                          "score": round(len(qtoks & atoks) / max(1, len(qtoks)), 3)}
                break
        entries.append(chosen or {"input": name, "kind": "name", "id": "", "app_name": "",
                                  "url": "", "score": 0.0,
                                  "note": "no name-matching Play app found (search blocked or "
                                          "name not on Play in this country)"})

    for raw in (cfg.get("googleplay_app_ids") or []):
        raw = str(raw).strip()
        if not raw:
            continue
        pkg = _extract_gplay_package(raw)
        if not pkg:
            entries.append({"input": raw, "kind": "id", "id": "", "app_name": "", "url": "",
                            "score": 0.0, "note": "not a package name or Play URL — skipped"})
            continue
        res = _gplay_details(pkg, country)
        if res is None:
            # Network error — cannot validate; pass the package through unvalidated
            # rather than dropping a probably-good id on a transient failure.
            entries.append({"input": raw, "kind": "id", "id": pkg, "app_name": "",
                            "url": f"https://play.google.com/store/apps/details?id={pkg}",
                            "score": 0.5, "note": "could not validate (Play unreachable) — "
                                                  "passing the supplied package through"})
        elif res[0]:
            entries.append({"input": raw, "kind": "id", "id": pkg, "app_name": res[1],
                            "url": f"https://play.google.com/store/apps/details?id={pkg}",
                            "score": 1.0})
        else:
            # id="" so a package that does not validate is never scraped (a real
            # failure to surface). The network pass-through case ABOVE keeps its id
            # on purpose (transient failure, probably-good user-supplied package).
            entries.append({"input": raw, "kind": "id", "id": "", "app_name": "", "url": "",
                            "score": 0.0, "note": f"package {pkg} did not resolve to a real Play "
                                                  f"app (check the package / country)"})

    if workdir is not None:
        stage_dir = Path(workdir) / "googleplay_reviews"
        stage_dir.mkdir(parents=True, exist_ok=True)
        (stage_dir / "resolution.json").write_text(
            json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    return entries


def format_googleplay_report(entries: list) -> str:
    """Human-facing Google Play resolution report shown at the dry-run gate."""
    lines = ["", "-" * 60, "GOOGLE PLAY RESOLUTION — confirm before the paid scrape", "-" * 60]
    any_flag = False
    for e in entries:
        if e.get("id") and not e.get("note"):
            nm = f"  ({e['app_name']})" if e.get("app_name") else ""
            lines.append(f"  ✓ {e['input']!r} → {e['id']}{nm}")
        elif e.get("id") and e.get("note"):  # passed through unvalidated
            any_flag = True
            lines.append(f"  ~ {e['input']!r} → {e['id']}  ({e['note']})")
        else:
            any_flag = True
            lines.append(f"  ⚠ {e['input']!r} → UNRESOLVED — {e.get('note','no match')}")
    lines.append("-" * 60)
    lines.append("CONFIRM the matches above (especially any ⚠) before approving spend."
                 if any_flag else "All Google Play targets resolved and validated.")
    return "\n".join(lines)


def resolve_googleplay_targets(cfg: dict, workdir=None) -> list:
    """Package names for the GP stage. PURE/offline by contract (like the App
    Store equivalent): prefers the persisted resolution written at the gate,
    else regex-extracts packages from googleplay_app_ids. Does NOT resolve
    googleplay_names here — that needs the gate's network search."""
    if workdir is not None:
        rp = Path(workdir) / "googleplay_reviews" / "resolution.json"
        if rp.exists():
            try:
                entries = json.loads(rp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                entries = None
            if entries is not None:
                ids, seen = [], set()
                for e in entries:
                    i = str(e.get("id") or "").strip()
                    if i and i not in seen:
                        seen.add(i)
                        ids.append(i)
                return ids
    ids, seen = [], set()
    for raw in (cfg.get("googleplay_app_ids") or []):
        i = _extract_gplay_package(raw)
        if not i:
            print(f"subjects [googleplay]: WARNING -- skipping unrecognized entry: {str(raw).strip()!r} "
                  f"(expected a package like 'com.spotify.music' or a Google Play URL)")
        elif i not in seen:
            seen.add(i)
            ids.append(i)
    return ids


# ---------------------------------------------------------------------------
# Reddit subject resolution (Phase D)
# ---------------------------------------------------------------------------

def resolve_reddit_targets(cfg: dict) -> dict:
    """Assemble harshmaur/reddit-scraper target inputs from config.

    Reddit's target types are HETEROGENEOUS -- they map to DIFFERENT actor input
    fields -- so this returns a DICT of input keys, unlike the app resolvers which
    return a flat id list:

      reddit_queries    (list) -> searchTerms      keyword search across Reddit
      reddit_subreddit  (str)  -> withinCommunity  limit that keyword search to
                                  ONE subreddit (bare name, r/name, or full URL)
      reddit_subreddits (list) -> subredditUrls    full-scrape whole subreddits
      reddit_urls       (list) -> startUrls        direct post/subreddit/search
                                  URLs, each wrapped as {"url": ...}

    Only non-empty keys are returned, so the actor receives a minimal input and
    run_phase._has_inputs can tell whether any target was actually supplied
    (an all-empty result means "nothing to scrape" -> the stage is skipped, same
    as an empty companyUrls/appIds for the other sources).
    """
    out = {}
    queries = [str(q).strip() for q in (cfg.get("reddit_queries") or []) if str(q).strip()]
    if queries:
        out["searchTerms"] = queries
    community = str(cfg.get("reddit_subreddit") or "").strip()
    if community:
        out["withinCommunity"] = community
    subs = [str(s).strip() for s in (cfg.get("reddit_subreddits") or []) if str(s).strip()]
    if subs:
        out["subredditUrls"] = subs
    urls = [str(u).strip() for u in (cfg.get("reddit_urls") or []) if str(u).strip()]
    if urls:
        out["startUrls"] = [{"url": u} for u in urls]
    return out


# ---------------------------------------------------------------------------
# G2 / Capterra subject resolution (Phase E)
# ---------------------------------------------------------------------------

def _validate_g2_url(entry: str) -> str:
    """A usable factden startUrl (full G2 product URL or bare slug), or '' if the
    entry is neither. Pure — no network."""
    entry = str(entry).strip()
    low = entry.lower()
    if "g2.com/products/" in low:
        return entry
    if re.match(r"^[a-zA-Z0-9][a-zA-Z0-9\-]{1,79}$", entry):
        return entry
    return ""


def discover_g2_products(name: str, max_products: int = 5):
    """Resolve a G2 product NAME to its canonical slug by running factden in its
    'products' (discover) mode — G2 hard-blocks a free GET, so this is a small
    PAID actor run (each product row costs $0.004). Returns (entry, cost_usd):
    entry is the best-ranked product {input, kind:'name', slug, url, name,
    vendor, review_count, match_type, score, candidates} or an unresolved entry
    with slug=''. On any actor failure raises RuntimeError (money-path: the caller
    STOPS and reports — never silently proceeds).

    Ranking: the actor tags an 'exact' matchType and returns a reviewCount; the
    product the user means is the exact match, or the most-reviewed among the
    name-token matches. This mirrors the App Store relevance-gate-then-popularity
    rule, using signals the discover run already computes."""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    import apify_client as A
    inp = {"mode": "products", "searchQuery": name, "maxProducts": int(max_products)}
    run = A.run_actor("factden/g2-reviews-scraper", inp)
    if (run or {}).get("status") != "SUCCEEDED":
        raise RuntimeError(f"G2 discover for {name!r} did not succeed "
                           f"(status={(run or {}).get('status')}) — STOP, do not retry without approval")
    rows = A.fetch_dataset(run.get("defaultDatasetId")) or []
    cost = A.actual_charge_usd(run)
    qtoks = set(re.findall(r"[a-z0-9]+", name.lower()))

    def score(r):
        atoks = set(re.findall(r"[a-z0-9]+", (str(r.get("name")) + " " + str(r.get("vendorName"))).lower()))
        return len(qtoks & atoks) / max(1, len(qtoks))

    cands = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("slug"):
            continue
        cands.append({
            "slug": r.get("slug"), "url": r.get("url") or f"https://www.g2.com/products/{r.get('slug')}",
            "name": r.get("name") or "", "vendor": r.get("vendorName") or "",
            "review_count": int(r.get("reviewCount") or 0),
            "match_type": (r.get("matchType") or ""), "score": round(score(r), 3),
        })
    # exact match wins; else name-relevant (score>0) most-reviewed; else top row.
    gated = [c for c in cands if c["score"] > 0 or c["match_type"] == "exact"]
    ranked = sorted(gated or cands,
                    key=lambda c: (c["match_type"] == "exact", c["score"], c["review_count"]),
                    reverse=True)
    if not ranked:
        return ({"input": name, "kind": "name", "slug": "", "url": "", "name": "",
                 "vendor": "", "review_count": 0, "match_type": "", "score": 0.0,
                 "note": "no G2 product matched this name"}, cost)
    best = dict(ranked[0]); best["input"] = name; best["kind"] = "name"
    best["candidates"] = [{"name": c["name"], "slug": c["slug"], "review_count": c["review_count"]}
                          for c in ranked[1:3]]
    return (best, cost)


def resolve_g2(cfg: dict, workdir=None, discover: bool = False) -> tuple:
    """Resolve + validate G2 targets. Returns (entries, total_cost_usd).

    g2_urls / bare slugs → format-validated (free). g2_names → resolved to a slug
    only when discover=True (a paid factden discover run per name); when
    discover=False (dry-run) each name is listed as a pending resolution with its
    forecast cost, spending nothing. Writes g2_reviews/resolution.json when
    workdir is given."""
    entries: list = []
    total_cost = 0.0
    for raw in (cfg.get("g2_urls") or []):
        raw = str(raw).strip()
        if not raw:
            continue
        ok = _validate_g2_url(raw)
        if ok:
            entries.append({"input": raw, "kind": "url", "slug": ok, "url": ok, "note": ""})
        else:
            entries.append({"input": raw, "kind": "url", "slug": "", "url": "",
                            "note": "not a g2.com/products URL or a bare slug — skipped"})

    max_products = int(cfg.get("g2_discover_max_products", 5))
    for name in (cfg.get("g2_names") or []):
        name = str(name).strip()
        if not name:
            continue
        if not discover:
            entries.append({"input": name, "kind": "name", "slug": "", "url": "",
                            "note": f"pending discovery (~${max_products * 0.004:.3f}) — "
                                    f"resolved on the real run"})
            continue
        entry, cost = discover_g2_products(name, max_products)
        total_cost += cost
        entries.append(entry)

    if workdir is not None:
        stage_dir = Path(workdir) / "g2_reviews"
        stage_dir.mkdir(parents=True, exist_ok=True)
        (stage_dir / "resolution.json").write_text(
            json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    return entries, total_cost


def format_g2_report(entries: list) -> str:
    """Human-facing G2 resolution report shown at the gate."""
    lines = ["", "-" * 60, "G2 RESOLUTION — confirm before the paid review scrape", "-" * 60]
    any_flag = False
    for e in entries:
        if e.get("slug") and not e.get("note"):
            extra = ""
            if e.get("kind") == "name":
                extra = (f"  ({e.get('name')} · {e.get('vendor')} · {e.get('review_count',0):,} reviews"
                         f"{' · exact' if e.get('match_type') == 'exact' else ''})")
            lines.append(f"  ✓ {e['input']!r} → {e['slug']}{extra}")
            for c in e.get("candidates") or []:
                lines.append(f"      alt: {c['name']} (slug {c['slug']}, {c['review_count']:,} reviews)")
        elif e.get("kind") == "name" and "pending discovery" in (e.get("note") or ""):
            lines.append(f"  … {e['input']!r} → {e['note']}")
        else:
            any_flag = True
            lines.append(f"  ⚠ {e['input']!r} → UNRESOLVED — {e.get('note','no match')}")
    lines.append("-" * 60)
    lines.append("CONFIRM the matches above (especially any ⚠) before approving spend."
                 if any_flag else "All G2 targets resolved.")
    return "\n".join(lines)


def resolve_g2_targets(cfg: dict, workdir=None) -> list:
    """factden startUrls (slugs/URLs) for the G2 review stage. PURE/offline:
    prefers the persisted resolution written at the gate (names already resolved
    to slugs there via the paid discover run), else format-validates g2_urls.
    Never resolves names here — that needs the gate's paid discover run."""
    if workdir is not None:
        rp = Path(workdir) / "g2_reviews" / "resolution.json"
        if rp.exists():
            try:
                entries = json.loads(rp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                entries = None
            if entries is not None:
                out, seen = [], set()
                for e in entries:
                    s = str(e.get("slug") or "").strip()
                    if s and s not in seen:
                        seen.add(s)
                        out.append(s)
                return out
    out, seen = [], set()
    for raw in (cfg.get("g2_urls") or []):
        ok = _validate_g2_url(raw)
        if not ok:
            print(f"subjects [g2]: WARNING -- skipping unrecognized entry: {str(raw).strip()!r} "
                  f"(expected a g2.com/products/<slug> URL or a bare slug like 'slack')")
        elif ok not in seen:
            seen.add(ok)
            out.append(ok)
    return out


def _validate_capterra_url(entry: str) -> str:
    """A usable azzouzana profileUrl from a Capterra product URL or a bare
    p/<id>/<slug> path, or '' if the entry is not a well-formed Capterra product
    URL. Pure — no network (Capterra bot-walls a bare GET, so validation is
    structural: it must be a capterra host with a /p/<numeric-id>/<slug> path,
    which is the shape azzouzana requires)."""
    entry = str(entry).strip()
    low = entry.lower()
    if "capterra." in low and re.search(r"/p/\d+/", entry):
        return entry
    if re.match(r"^p/\d+/", entry):
        return f"https://www.capterra.com/{entry}"
    return ""


def resolve_capterra(cfg: dict, workdir=None) -> list:
    """Validate supplied Capterra product URLs (VALIDATE-ONLY — no name→URL
    search: Capterra hard-blocks a free GET and its actor has no search field,
    so name resolution would need a paid SERP stage deferred by design). Each
    capterra_urls entry is structurally validated (must be a capterra /p/<id>/
    <slug> URL) and surfaced at the gate — a malformed URL is flagged, not
    silently dropped at stage-build time. Writes capterra_reviews/resolution.json."""
    entries: list = []
    for raw in (cfg.get("capterra_urls") or []):
        raw = str(raw).strip()
        if not raw:
            continue
        ok = _validate_capterra_url(raw)
        if ok:
            entries.append({"input": raw, "kind": "url", "url": ok, "note": ""})
        else:
            entries.append({"input": raw, "kind": "url", "url": "",
                            "note": "not a capterra.com/p/<id>/<slug> URL — skipped"})
    if workdir is not None:
        stage_dir = Path(workdir) / "capterra_reviews"
        stage_dir.mkdir(parents=True, exist_ok=True)
        (stage_dir / "resolution.json").write_text(
            json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    return entries


def format_capterra_report(entries: list) -> str:
    """Human-facing Capterra validation report shown at the gate."""
    lines = ["", "-" * 60, "CAPTERRA VALIDATION — confirm before the paid scrape", "-" * 60]
    any_flag = False
    for e in entries:
        if e.get("url") and not e.get("note"):
            lines.append(f"  ✓ {e['input']!r} → {e['url']}")
        else:
            any_flag = True
            lines.append(f"  ⚠ {e['input']!r} → {e.get('note','unresolved')}")
    lines.append("-" * 60)
    lines.append("CONFIRM the URLs above (especially any ⚠) before approving spend."
                 if any_flag else "All Capterra URLs validated.")
    return "\n".join(lines)


def resolve_capterra_targets(cfg: dict, workdir=None) -> list:
    """profileUrls for the Capterra stage. PURE/offline: prefers the persisted
    validation (capterra_reviews/resolution.json), else structural validation
    of capterra_urls."""
    if workdir is not None:
        rp = Path(workdir) / "capterra_reviews" / "resolution.json"
        if rp.exists():
            try:
                entries = json.loads(rp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                entries = None
            if entries is not None:
                out, seen = [], set()
                for e in entries:
                    u = str(e.get("url") or "").strip()
                    if u and u not in seen:
                        seen.add(u)
                        out.append(u)
                return out
    out, seen = [], set()
    for raw in (cfg.get("capterra_urls") or []):
        ok = _validate_capterra_url(raw)
        if not ok:
            print(f"subjects [capterra]: WARNING -- skipping unrecognized entry: {str(raw).strip()!r} "
                  f"(expected a capterra.com/p/<id>/<slug> URL)")
        elif ok not in seen:
            seen.add(ok)
            out.append(ok)
    return out


# ---------------------------------------------------------------------------
# Mode detection
# ---------------------------------------------------------------------------

def detect_mode(cfg: dict) -> str:
    """Return "1", "2", or "3" based on which subject-input key is populated.

    Priority: competitor_place_urls > competitor_names > category/queries.
    Priority: mode 1 (place URLs) > mode 2 (business names) > mode 3 (search queries).
    """
    if cfg.get("competitor_place_urls"):
        return "1"
    if cfg.get("competitor_names"):
        return "2"
    return "3"


# ---------------------------------------------------------------------------
# MODE 1 — Place URLs provided directly
# ---------------------------------------------------------------------------

def resolve_mode1(cfg: dict, workdir) -> list:
    """Write competitor_place_urls into B2a_maps_places/review_targets.json.

    Returns the list of {url} dicts written.  pain_intel.py skips B2a after
    this call — there is nothing to discover when the user already has the URLs.
    """
    urls = cfg.get("competitor_place_urls") or []
    targets = [{"url": u} for u in urls if u]
    stage_dir = Path(workdir) / "B2a_maps_places"
    stage_dir.mkdir(parents=True, exist_ok=True)
    _write_targets(stage_dir, targets)
    print(f"subjects [mode 1]: {len(targets)} place URL(s) written directly -> review_targets.json")
    print("  B2a (Maps discovery) is SKIPPED — URLs are used as-is.")
    return targets


# ---------------------------------------------------------------------------
# MODE 2 — Business names → B2a search → best-match selection
# ---------------------------------------------------------------------------

def build_mode2_cfg(cfg: dict, names: list) -> dict:
    """Return a shallow copy of cfg with maps_search_queries set to the names.

    Appends the city (if any) to each name for Maps search precision.
    pain_intel.py writes this dict to a temp config file and passes it to
    run_phase B2a — that way stages.build_B2a() reads the names as search
    strings without any changes to stages.py.

    Why not mutate cfg in place: the original cfg is used for B2b's config
    path; the patched version is only for the B2a subprocess.
    """
    patched = dict(cfg)
    city = (cfg.get("city") or "").strip()
    if city:
        queries = [f"{n} {city}" for n in names]
    else:
        queries = list(names)
    patched["maps_search_queries"] = queries
    return patched


def select_mode2_targets(names: list, b2a_stage_dir, workdir) -> list:
    """After B2a has run, pick ONE place per name and write review_targets.json.

    Strategy:
      1. Load B2a output (run_02_full.json, fallback run_01_test.json).
      2. Group records by the search string that produced them.  Field name
         is `searchString` per compass actor; falls back to other known keys
         (# VERIFY field names on a live named-competitor run).
      3. For each name, find its candidate group via prefix/substring match
         (handles "Name City" keys when the search string had city appended).
      4. Skip known aggregator titles.
      5. Among remaining candidates, pick the one with the most word-token
         overlap with the name; break ties by highest review count.
      6. Keep ALL named competitors (no review-count cap — mode-2 resolves
         one listing per name and keeps all of them).

    Returns a list of result dicts for the caller to log:
      {"name": str, "url": str | None, "title": str, "note": str}
    """
    b2a_stage_dir = Path(b2a_stage_dir)
    items = _load_b2a_output(b2a_stage_dir)

    # --- group records by search string ----------------------------------------
    # Try multiple field names; compass actor uses "searchString" (# VERIFY).
    grouped: dict = {}  # search_string_lower -> [records]
    for item in items:
        if not isinstance(item, dict):
            continue
        s = (item.get("searchString") or item.get("keyword") or
             item.get("searchQuery") or item.get("query") or "")
        key = str(s).lower().strip()
        grouped.setdefault(key, []).append(item)

    # Fallback: if the actor returned no search-string field, treat all items
    # as candidates for every name (title-overlap matching still picks correctly).
    no_grouping = all(k == "" for k in grouped) and items
    if no_grouping:
        print("subjects [mode 2]: WARNING — no searchString field in B2a output; "
              "falling back to full-list candidate matching (verify searchString field names)")
        ungrouped_all = items
    else:
        ungrouped_all = None

    # --- resolve each name -----------------------------------------------------
    results = []
    targets = []

    for name in names:
        if ungrouped_all is not None:
            candidates = list(ungrouped_all)
        else:
            candidates = _find_candidates(name, grouped)

        # Filter aggregators; keep raw list if everything was an aggregator.
        clean = [c for c in candidates if not _is_maps_aggregator(c)]
        if not clean and candidates:
            print(f"  [warn] all {len(candidates)} candidates for '{name}' look like "
                  "aggregators — keeping them and flagging for manual review")
            clean = candidates

        if not clean:
            results.append({
                "name": name, "url": None, "title": "",
                "note": ("no B2a record matched this name — verify B2a ran and the "
                         "name is findable on Maps"),
            })
            continue

        best = _best_match(name, clean)
        # Field-name fallbacks: # VERIFY field names on a live named-competitor run.
        url = (best.get("url") or best.get("placeUrl") or
               best.get("searchPageUrl") or "")
        title = best.get("title") or best.get("name") or ""
        reviews = int(best.get("reviewsCount") or best.get("reviewCount") or 0)

        if not url:
            results.append({
                "name": name, "url": None, "title": title,
                "note": ("matched place record but no URL extracted — "
                         "verify field names on a live named-competitor run"),
            })
            continue

        targets.append({"url": url})
        is_agg = _is_maps_aggregator(best)
        note = f"matched '{title}' ({reviews} reviews)"
        if is_agg:
            note += " [looks like aggregator — verify manually]"
        results.append({"name": name, "url": url, "title": title, "note": note})

    # Write review_targets.json (ALL matched places — no cap for mode 2).
    stage_dir = Path(workdir) / "B2a_maps_places"
    stage_dir.mkdir(parents=True, exist_ok=True)
    _write_targets(stage_dir, targets)
    resolved_n = sum(1 for r in results if r.get("url"))
    print(f"subjects [mode 2]: {resolved_n}/{len(names)} name(s) resolved "
          f"-> review_targets.json  (all kept, no review-count cap)")
    return results


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _write_targets(stage_dir: Path, targets: list) -> None:
    """Write the {url} list B2b reads."""
    (stage_dir / "review_targets.json").write_text(
        json.dumps(targets, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def _load_b2a_output(stage_dir: Path) -> list:
    """Load B2a output: full run first, test run as fallback."""
    for name in ("run_02_full.json", "run_01_test.json"):
        p = stage_dir / name
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"subjects: WARNING — could not parse {p}: {e}")
    return []


def _find_candidates(name: str, grouped: dict) -> list:
    """Find B2a records whose search string matches this name.

    Tries exact match, then prefix match (handles "Name City" keys), then
    substring.  Returns the first match found in that priority order.
    """
    name_lower = name.lower().strip()
    if name_lower in grouped:
        return list(grouped[name_lower])
    for key, records in grouped.items():
        if key.startswith(name_lower):
            return list(records)
    for key, records in grouped.items():
        if name_lower in key:
            return list(records)
    return []


def _is_maps_aggregator(place: dict) -> bool:
    """Return True if the place title or URL suggests a directory/aggregator."""
    title = str(place.get("title") or place.get("name") or "").lower()
    for tok in _AGGREGATOR_TITLE_TOKENS:
        if tok in title:
            return True
    # A "/maps/search/" URL (not "/maps/place/") is normally a search-result
    # page, not a specific business. BUT the compass crawler-google-places actor
    # emits every REAL place as ".../maps/search/?api=1&query=...&
    # query_place_id=<id>" -- a deep link pinned to one business by its place id,
    # which is exactly the URL B2b accepts. So a query_place_id anchor means this
    # IS a specific place; only a /maps/search/ URL WITHOUT that anchor is an
    # aggregator. (Confirmed against real B2a output: all legitimate places carried
    # query_place_id; without this check they were false-flagged as aggregators,
    # tripping the "all candidates look like aggregators" branch and disabling the
    # filter on every mode-2 run.)
    url = str(place.get("url") or place.get("placeUrl") or "").lower()
    if ("/maps/search/" in url and "/maps/place/" not in url
            and "query_place_id=" not in url):
        return True
    return False


def _token_overlap(name: str, title: str) -> int:
    """Count shared word tokens between a competitor name and a place title."""
    norm = lambda s: set(re.split(r"[\s.\-_,()&/]+", s.lower().strip()))
    return len(norm(name) & norm(title) - {""})


def _best_match(name: str, candidates: list) -> dict:
    """Pick the candidate with the highest title-token overlap with `name`.
    Tiebreak: highest review count (most review-rich location of the business).
    """
    def score(c):
        title = str(c.get("title") or c.get("name") or "")
        overlap = _token_overlap(name, title)
        reviews = int(c.get("reviewsCount") or c.get("reviewCount") or 0)
        return (overlap, reviews)
    return max(candidates, key=score)


# ---------------------------------------------------------------------------
# Inline verification tests (run with: python3 subjects.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import shutil
    import tempfile

    PASS = 0
    FAIL = 0

    def check(label, got, expected):
        global PASS, FAIL
        if got == expected:
            print(f"  PASS  {label}")
            PASS += 1
        else:
            print(f"  FAIL  {label}")
            print(f"        got:      {got!r}")
            print(f"        expected: {expected!r}")
            FAIL += 1

    # ==========================================================================
    # TEST 1 — Mode detection
    # ==========================================================================
    print("\n=== TEST 1: Mode detection ===")
    check("mode 1 (place_urls)",
          detect_mode({"competitor_place_urls": ["https://maps.google.com/maps?cid=1"]}),
          "1")
    check("mode 2 (names)",
          detect_mode({"competitor_place_urls": [], "competitor_names": ["Acme Corp"]}),
          "2")
    check("mode 3 (category)",
          detect_mode({"competitor_place_urls": [], "competitor_names": [],
                       "maps_search_queries": ["fitness gym chicago"]}),
          "3")
    check("mode 3 (all empty)",
          detect_mode({"competitor_place_urls": [], "competitor_names": []}),
          "3")
    # mode-1 takes priority over mode-2 when both non-empty
    check("mode 1 wins over mode 2",
          detect_mode({"competitor_place_urls": ["https://x"], "competitor_names": ["Y"]}),
          "1")

    # ==========================================================================
    # TEST 2 — Mode 1: passthrough writes exact URLs
    # ==========================================================================
    print("\n=== TEST 2: Mode 1 — place-URL passthrough ===")
    tmpdir = tempfile.mkdtemp(prefix="pain_test_m1_")
    try:
        urls = [
            "https://www.google.com/maps/place/?q=place_id:ChIJ_mode1_a",
            "https://www.google.com/maps/place/?q=place_id:ChIJ_mode1_b",
        ]
        cfg1 = {"competitor_place_urls": urls}
        targets = resolve_mode1(cfg1, tmpdir)

        # Assert exact URL list
        check("mode 1: target count", len(targets), 2)
        check("mode 1: first URL", targets[0]["url"], urls[0])
        check("mode 1: second URL", targets[1]["url"], urls[1])

        # Assert review_targets.json content
        rt_path = Path(tmpdir) / "B2a_maps_places" / "review_targets.json"
        written = json.loads(rt_path.read_text())
        check("mode 1: review_targets.json == targets", written, targets)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # ==========================================================================
    # TEST 3 — Mode 2a: build_mode2_cfg injects names as search strings
    # ==========================================================================
    print("\n=== TEST 3: Mode 2a — search-string injection ===")
    cfg2 = {
        "competitor_names": ["Planet Fitness", "Anytime Fitness", "Crunch Fitness"],
        "city": "Chicago",
        "country": "USA",
        "language": "en",
        "maps_search_queries": [],
        "maps_location_query": "Chicago, USA",
    }
    patched = build_mode2_cfg(cfg2, cfg2["competitor_names"])
    check("mode 2a: searchStrings count", len(patched["maps_search_queries"]), 3)
    check("mode 2a: first query has city", patched["maps_search_queries"][0],
          "Planet Fitness Chicago")
    check("mode 2a: last query has city", patched["maps_search_queries"][2],
          "Crunch Fitness Chicago")
    # Original cfg must NOT be mutated
    check("mode 2a: original cfg unchanged", cfg2.get("maps_search_queries"), [])

    # ==========================================================================
    # TEST 4 — Mode 2b: best-match selector picks correct place per name
    #
    # Synthetic B2a fixture (field names assumed from compass/crawler-google-places;
    # marked # VERIFY — real field names confirmed against live output).
    # Cases:
    #   "Planet Fitness": 3 candidates — one aggregator (Yelp listing), one
    #     wrong branch with 50 reviews, one correct match with 340 reviews.
    #     Correct match wins on review count (token overlap is equal after
    #     removing "chicago").
    #   "Anytime Fitness": 2 candidates — correct match wins via title overlap.
    #   "Crunch Fitness": 0 B2a records (no match) — should produce url=None.
    # ==========================================================================
    print("\n=== TEST 4: Mode 2b — best-match selection ===")

    # ASSUMED field names: searchString, url, title, reviewsCount (# VERIFY Phase 3)
    FIXTURE_B2A = [
        # Planet Fitness — aggregator listing (must be skipped)
        {
            "searchString": "Planet Fitness Chicago",  # ASSUMED field name
            "url": "https://www.google.com/maps/place/Yelp-Planet-Fitness",
            "title": "Planet Fitness - Yelp Reviews",  # "yelp" in title -> aggregator
            "reviewsCount": 9999,   # ASSUMED field name
        },
        # Planet Fitness — wrong branch, lower review count
        {
            "searchString": "Planet Fitness Chicago",
            "url": "https://www.google.com/maps/place/Planet+Fitness+South",
            "title": "Planet Fitness - South Chicago",
            "reviewsCount": 50,
        },
        # Planet Fitness — best match, highest review count among non-aggregators
        {
            "searchString": "Planet Fitness Chicago",
            "url": "https://www.google.com/maps/place/Planet+Fitness+Loop",
            "title": "Planet Fitness - Chicago Loop",
            "reviewsCount": 340,
        },
        # Anytime Fitness — first candidate wins (only non-aggregator)
        {
            "searchString": "Anytime Fitness Chicago",
            "url": "https://www.google.com/maps/place/Anytime+Fitness+Chicago",
            "title": "Anytime Fitness Chicago",
            "reviewsCount": 210,
        },
        # Anytime Fitness — lower-scoring duplicate
        {
            "searchString": "Anytime Fitness Chicago",
            "url": "https://www.google.com/maps/place/Anytime+Fitness+North",
            "title": "Anytime Fitness North Chicago",
            "reviewsCount": 90,
        },
        # Crunch Fitness — no records (omitted from fixture) -> should return url=None
    ]

    tmpdir = tempfile.mkdtemp(prefix="pain_test_m2_")
    try:
        b2a_dir = Path(tmpdir) / "B2a_maps_places"
        b2a_dir.mkdir(parents=True)
        (b2a_dir / "run_02_full.json").write_text(
            json.dumps(FIXTURE_B2A, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        names = ["Planet Fitness", "Anytime Fitness", "Crunch Fitness"]
        results = select_mode2_targets(names, b2a_dir, tmpdir)

        # Planet Fitness: must resolve to the Loop branch (highest reviews, non-agg)
        pf = next(r for r in results if r["name"] == "Planet Fitness")
        check("mode 2b: Planet Fitness url (Loop, not Yelp/South)",
              pf["url"],
              "https://www.google.com/maps/place/Planet+Fitness+Loop")

        # Anytime Fitness: must resolve to the higher-review one
        af = next(r for r in results if r["name"] == "Anytime Fitness")
        check("mode 2b: Anytime Fitness url (main branch)",
              af["url"],
              "https://www.google.com/maps/place/Anytime+Fitness+Chicago")

        # Crunch Fitness: no B2a records, so url must be None
        cf = next(r for r in results if r["name"] == "Crunch Fitness")
        check("mode 2b: Crunch Fitness url is None (no B2a record)", cf["url"], None)

        # ALL 3 names kept — review_targets.json has exactly 2 URLs (mode 2 = no cap)
        rt = json.loads((Path(tmpdir) / "B2a_maps_places" / "review_targets.json").read_text())
        check("mode 2b: review_targets.json has 2 entries (PF + AF; CF unresolved)",
              len(rt), 2)
        check("mode 2b: first target URL",
              rt[0]["url"],
              "https://www.google.com/maps/place/Planet+Fitness+Loop")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # ==========================================================================
    # TEST 5 — Mode 3 (postprocess._pp_b2a): top-N by review count
    # ==========================================================================
    print("\n=== TEST 5: Mode 3 — _pp_b2a top-N by review count ===")
    # Import postprocess from the same scripts/ directory.
    _here = Path(__file__).resolve().parent
    sys.path.insert(0, str(_here))
    from postprocess import _pp_b2a  # noqa: E402

    FIXTURE_B2A_CAT = [
        {"url": "https://maps.google.com/place/gym_a", "title": "Gym A", "reviewsCount": 500},
        {"url": "https://maps.google.com/place/gym_b", "title": "Gym B", "reviewsCount": 120},
        {"url": "https://maps.google.com/place/gym_c", "title": "Gym C", "reviewsCount": 890},
        {"url": "https://maps.google.com/place/gym_d", "title": "Gym D", "reviewsCount": 310},
        {"url": "https://maps.google.com/place/gym_e", "title": "Gym E", "reviewsCount": 45},
    ]

    tmpdir = tempfile.mkdtemp(prefix="pain_test_m3_")
    try:
        b2a_dir = Path(tmpdir) / "B2a_maps_places"
        b2a_dir.mkdir(parents=True)
        (b2a_dir / "run_02_full.json").write_text(
            json.dumps(FIXTURE_B2A_CAT, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # Mode 3 cfg: no competitor_names (so _pp_b2a does NOT skip)
        cfg3 = {"review_target_count": 3}
        _pp_b2a(b2a_dir, cfg3)

        rt = json.loads((b2a_dir / "review_targets.json").read_text())
        # Expected top-3 by review count: Gym C (890), Gym A (500), Gym D (310)
        check("mode 3: top-3 count", len(rt), 3)
        check("mode 3: #1 is Gym C (890 reviews)",
              rt[0]["url"], "https://maps.google.com/place/gym_c")
        check("mode 3: #2 is Gym A (500 reviews)",
              rt[1]["url"], "https://maps.google.com/place/gym_a")
        check("mode 3: #3 is Gym D (310 reviews)",
              rt[2]["url"], "https://maps.google.com/place/gym_d")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # ==========================================================================
    # TEST 6 — Mode 2 guard: _pp_b2a skips when competitor_names is set
    # ==========================================================================
    print("\n=== TEST 6: _pp_b2a skips for mode 2 (competitor_names set) ===")
    tmpdir = tempfile.mkdtemp(prefix="pain_test_m2_guard_")
    try:
        b2a_dir = Path(tmpdir) / "B2a_maps_places"
        b2a_dir.mkdir(parents=True)
        (b2a_dir / "run_02_full.json").write_text(
            json.dumps(FIXTURE_B2A_CAT, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # cfg with competitor_names → _pp_b2a must skip (not write review_targets.json)
        cfg_mode2 = {"competitor_names": ["Planet Fitness"], "review_target_count": 3}
        _pp_b2a(b2a_dir, cfg_mode2)
        rt_path = b2a_dir / "review_targets.json"
        check("mode 2 guard: _pp_b2a does NOT write review_targets.json",
              rt_path.exists(), False)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # ==========================================================================
    # TEST 7 — _is_maps_aggregator: a /maps/search/ URL carrying query_place_id
    #          is a REAL place, not an aggregator (Phase-8 live-run regression)
    # ==========================================================================
    print("\n=== TEST 7: _is_maps_aggregator honors query_place_id ===")
    # The exact URL shape compass/crawler-google-places emits for a real place.
    real_place = {
        "title": "Intelligentsia Coffee Broadway Coffeebar",
        "url": ("https://www.google.com/maps/search/?api=1&query="
                "Intelligentsia%20Coffee%20Broadway&query_place_id=ChIJeX1U26bTD4gR"),
    }
    check("T7: real place with query_place_id is NOT an aggregator",
          _is_maps_aggregator(real_place), False)
    # A genuine /maps/search/ directory link WITHOUT a place-id anchor still flags.
    search_page = {
        "title": "coffee shops",
        "url": "https://www.google.com/maps/search/coffee+shops+chicago",
    }
    check("T7: /maps/search/ without query_place_id IS an aggregator",
          _is_maps_aggregator(search_page), True)
    # Title-token detection is unaffected by the URL change.
    yelp_like = {"title": "The 10 Best Coffee Shops in Chicago (Yelp)", "url": ""}
    check("T7: aggregator title still flags regardless of URL",
          _is_maps_aggregator(yelp_like), True)

    # ==========================================================================
    # TEST 8 — resolve_trustpilot_targets: domain normalization
    # ==========================================================================
    print("\n=== TEST 8: resolve_trustpilot_targets ===")

    # Bare domain -> full Trustpilot review URL
    cfg_tp = {"trustpilot_domains": ["vans.com"]}
    result = resolve_trustpilot_targets(cfg_tp)
    check("TP: bare domain normalized to full URL",
          result, ["https://www.trustpilot.com/review/vans.com"])

    # www. prefix kept as-is
    cfg_tp2 = {"trustpilot_domains": ["www.vans.com"]}
    result2 = resolve_trustpilot_targets(cfg_tp2)
    check("TP: www. domain normalized to full URL",
          result2, ["https://www.trustpilot.com/review/www.vans.com"])

    # Full /review/ URL passed through unchanged
    full_url = "https://www.trustpilot.com/review/www.vans.com"
    cfg_tp3 = {"trustpilot_domains": [full_url]}
    result3 = resolve_trustpilot_targets(cfg_tp3)
    check("TP: full /review/ URL passed through unchanged",
          result3, [full_url])

    # Multiple entries (mix of bare and full)
    cfg_tp4 = {"trustpilot_domains": ["nike.com", full_url, "adidas.com"]}
    result4 = resolve_trustpilot_targets(cfg_tp4)
    check("TP: multiple entries resolved correctly",
          result4, [
              "https://www.trustpilot.com/review/nike.com",
              full_url,
              "https://www.trustpilot.com/review/adidas.com",
          ])

    # Junk entries are skipped (printed warning, not raised)
    cfg_tp5 = {"trustpilot_domains": ["not a domain", "vans.com", ""]}
    result5 = resolve_trustpilot_targets(cfg_tp5)
    check("TP: junk entries skipped, valid entry kept",
          result5, ["https://www.trustpilot.com/review/vans.com"])

    # Empty list
    cfg_tp6 = {"trustpilot_domains": []}
    result6 = resolve_trustpilot_targets(cfg_tp6)
    check("TP: empty domains list returns empty list", result6, [])

    # Missing key
    cfg_tp7 = {}
    result7 = resolve_trustpilot_targets(cfg_tp7)
    check("TP: missing trustpilot_domains key returns empty list", result7, [])

    # ==========================================================================
    # TEST 9 — resolve_appstore_targets
    # ==========================================================================
    print("\n=== TEST 9: resolve_appstore_targets ===")

    # Bare numeric id passthrough
    r = resolve_appstore_targets({"appstore_app_ids": ["324684580"]})
    check("AS: bare numeric id", r, ["324684580"])

    # App Store URL: extract digits after /id
    r2 = resolve_appstore_targets({
        "appstore_app_ids": ["https://apps.apple.com/us/app/spotify/id324684580"]
    })
    check("AS: URL -> id extraction", r2, ["324684580"])

    # Another URL format (with review path)
    r3 = resolve_appstore_targets({
        "appstore_app_ids": ["https://itunes.apple.com/us/review?id=389801252&type=Purple%20Software"]
    })
    check("AS: itunes URL with id= param (not /id prefix, should warn+skip)", len(r3), 0)

    # A real /id URL variant
    r4 = resolve_appstore_targets({
        "appstore_app_ids": ["https://apps.apple.com/gb/app/name/id389801252"]
    })
    check("AS: /id URL extraction", r4, ["389801252"])

    # Multiple entries: mix of valid and junk
    r5 = resolve_appstore_targets({
        "appstore_app_ids": ["324684580", "not-an-id", "https://apps.apple.com/us/app/x/id999"]
    })
    check("AS: multiple entries, junk skipped", r5, ["324684580", "999"])

    # Empty list
    r6 = resolve_appstore_targets({"appstore_app_ids": []})
    check("AS: empty list returns empty", r6, [])

    # Missing key
    r7 = resolve_appstore_targets({})
    check("AS: missing key returns empty", r7, [])

    # ==========================================================================
    # TEST 10 — resolve_googleplay_targets
    # ==========================================================================
    print("\n=== TEST 10: resolve_googleplay_targets ===")

    # Bare package name passthrough
    g = resolve_googleplay_targets({"googleplay_app_ids": ["com.spotify.music"]})
    check("GP: bare package name", g, ["com.spotify.music"])

    # Google Play URL: extract id= query param
    g2 = resolve_googleplay_targets({
        "googleplay_app_ids": [
            "https://play.google.com/store/apps/details?id=com.spotify.music&hl=en"
        ]
    })
    check("GP: URL -> package extraction", g2, ["com.spotify.music"])

    # URL without trailing params
    g3 = resolve_googleplay_targets({
        "googleplay_app_ids": [
            "https://play.google.com/store/apps/details?id=com.example.app"
        ]
    })
    check("GP: URL without trailing params", g3, ["com.example.app"])

    # Multiple entries: mix valid and junk
    g4 = resolve_googleplay_targets({
        "googleplay_app_ids": [
            "com.spotify.music",
            "not a package",
            "https://play.google.com/store/apps/details?id=com.example.test&hl=en",
        ]
    })
    check("GP: multiple entries, junk skipped", g4, ["com.spotify.music", "com.example.test"])

    # Empty list
    g5 = resolve_googleplay_targets({"googleplay_app_ids": []})
    check("GP: empty list returns empty", g5, [])

    # Missing key
    g6 = resolve_googleplay_targets({})
    check("GP: missing key returns empty", g6, [])

    # ==========================================================================
    # TEST 11 — resolve_reddit_targets (returns a DICT of input keys)
    # ==========================================================================
    print("\n=== TEST 11: resolve_reddit_targets ===")
    r1 = resolve_reddit_targets({"reddit_queries": ["Spotify", "  "],
                                 "reddit_subreddit": "spotify"})
    check("RD: queries -> searchTerms (blank dropped)", r1.get("searchTerms"), ["Spotify"])
    check("RD: subreddit -> withinCommunity", r1.get("withinCommunity"), "spotify")
    r2 = resolve_reddit_targets({
        "reddit_subreddits": ["spotify", "apple"],
        "reddit_urls": ["https://www.reddit.com/r/example-topic/comments/x/y/"]})
    check("RD: subreddits -> subredditUrls", r2.get("subredditUrls"), ["spotify", "apple"])
    check("RD: urls -> startUrls wrapped as {url}",
          r2.get("startUrls"), [{"url": "https://www.reddit.com/r/example-topic/comments/x/y/"}])
    check("RD: no searchTerms key when queries empty", "searchTerms" in r2, False)
    # Empty -> {} so _has_inputs skips the stage (no target supplied).
    check("RD: all-empty config -> empty dict", resolve_reddit_targets({}), {})
    check("RD: empty arrays -> empty dict",
          resolve_reddit_targets({"reddit_queries": [], "reddit_urls": []}), {})

    # ==========================================================================
    # TEST 12 — resolve_g2_targets (Phase E)
    # ==========================================================================
    print("\n=== TEST 12: resolve_g2_targets ===")
    # Full G2 product URL passthrough
    check("G2: full products URL passthrough",
          resolve_g2_targets({"g2_urls": ["https://www.g2.com/products/slack/reviews"]}),
          ["https://www.g2.com/products/slack/reviews"])
    # Bare slug passthrough
    check("G2: bare slug passthrough",
          resolve_g2_targets({"g2_urls": ["microsoft-teams"]}), ["microsoft-teams"])
    # Mix + junk skipped (a bare domain is NOT a g2 subject)
    check("G2: mix, junk skipped",
          resolve_g2_targets({"g2_urls": [
              "slack", "https://www.g2.com/products/notion/reviews", "not a slug!"]}),
          ["slack", "https://www.g2.com/products/notion/reviews"])
    check("G2: empty list -> empty", resolve_g2_targets({"g2_urls": []}), [])
    check("G2: missing key -> empty", resolve_g2_targets({}), [])

    # ==========================================================================
    # TEST 13 — resolve_capterra_targets (Phase E)
    # ==========================================================================
    print("\n=== TEST 13: resolve_capterra_targets ===")
    check("CP: full product URL passthrough",
          resolve_capterra_targets({"capterra_urls": ["https://www.capterra.com/p/135003/Slack/"]}),
          ["https://www.capterra.com/p/135003/Slack/"])
    # Regional variant (capterra.co.uk) still accepted
    check("CP: regional host accepted",
          resolve_capterra_targets({"capterra_urls": ["https://www.capterra.co.uk/p/999/Foo/"]}),
          ["https://www.capterra.co.uk/p/999/Foo/"])
    # Bare p/<id>/<slug> normalized to full URL
    check("CP: bare p/<id>/<slug> normalized",
          resolve_capterra_targets({"capterra_urls": ["p/135003/Slack"]}),
          ["https://www.capterra.com/p/135003/Slack"])
    # Junk skipped
    check("CP: junk skipped",
          resolve_capterra_targets({"capterra_urls": [
              "https://www.capterra.com/p/135003/Slack/", "https://example.com/nope"]}),
          ["https://www.capterra.com/p/135003/Slack/"])
    check("CP: empty list -> empty", resolve_capterra_targets({"capterra_urls": []}), [])
    check("CP: missing key -> empty", resolve_capterra_targets({}), [])

    # ==========================================================================
    # Summary
    # ==========================================================================
    print(f"\n{'='*50}")
    print(f"Results: {PASS} passed, {FAIL} failed")
    if FAIL:
        sys.exit(1)
