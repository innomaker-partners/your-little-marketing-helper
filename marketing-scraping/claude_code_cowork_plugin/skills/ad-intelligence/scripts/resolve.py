"""
resolve.py — general, evidence-first competitor resolver for ad-intelligence.

Turns ANY competitor input (a company name, a domain, a URL, in any case) into
ranked, evidence-carrying candidate identifiers for each ad source:

  - a canonical DOMAIN            -> Google Ads Transparency (B5)
  - a Facebook PAGE url           -> Meta Ad Library by-advertiser (B4)
  - a LinkedIn COMPANY name       -> LinkedIn Ad Library (LI)

Design principles (frozen — do not weaken without a deliberate decision):
  * Propose ranked candidates with evidence; NEVER silently trust a supplied
    value and NEVER silently "fix" it. A human confirms at the resolution-report
    gate before any spend.
  * No blanket transforms. A supplied domain is COMPARED to the resolved one and
    a mismatch is SURFACED — hyphens/format are never auto-stripped (plenty of
    real companies keep hyphens: coca-cola.com, best-drains.com are correct).
  * There is no general case, only facets of edge-cases: the resolver ranks by
    general signals (name-token match, position, extra-token penalty) and defers
    the decision to the human. It is not tuned to any one company.
  * The rankers are pure functions — they issue NO network calls and spend
    nothing. ad_intel.py fetches SERP results (via the paid B1 stage) and passes
    them here. The ONE exception is `enrich_linkedin_ids` (near the bottom): the
    LinkedIn Ad Library scraper scopes to an advertiser only by NUMERIC company
    id, so name → slug → id needs one unauthenticated GET of the public company
    page. It is deliberately quarantined from the rankers and never touched by
    the offline tests; it costs nothing (no paid actor, no login).

Input shape: apify/google-search-scraper `organicResults` — a list of
{title, url, displayedUrl, position} dicts already fetched for a query.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import resolver  # noqa: E402  (shared B1 readers: load_b1_records/index_by_term/organic_of)
from resolver import _displayed_domain, _is_directory  # noqa: E402
from postprocess import _domain  # noqa: E402
from stages import meta_words  # noqa: E402  (the one reading of the search words)


# ---------------------------------------------------------------------------
# Facebook path prefixes that are NOT an advertiser Page (so we never mistake
# an ad-library link, a group, or a login page for the company's page). General.
# ---------------------------------------------------------------------------
_FB_NON_PAGE: frozenset[str] = frozenset({
    "ads", "groups", "events", "watch", "marketplace", "gaming", "pages",
    "profile.php", "people", "pg", "help", "login", "business", "sharer",
    "story.php", "photo.php", "permalink.php", "media", "hashtag", "search",
})


# ---------------------------------------------------------------------------
# Small general helpers
# ---------------------------------------------------------------------------

def _ensure_scheme(url: str) -> str:
    s = (url or "").strip()
    if not s:
        return ""
    if "://" not in s:
        s = "http://" + s.lstrip("/")
    return s


def _first_url(rec: dict) -> str:
    """The candidate URL for a SERP result: displayedUrl when it is a real URL,
    else url/link (skipping google.com/goto redirect blobs)."""
    displayed = rec.get("displayedUrl") or ""
    if displayed.strip().lower().startswith("http") and " › " not in displayed:
        return displayed.strip()
    url = rec.get("url") or rec.get("link") or ""
    if url and "google.com/goto" not in url and "/url?" not in url:
        return url
    # Breadcrumb displayedUrl (host › crumb › crumb): rebuild host-only URL.
    if displayed:
        host = _displayed_domain(displayed)
        if host:
            return "https://" + host
    return url or ""


_BARE_DOMAIN_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z]{2,})+(?:/.*)?$",
    re.I,
)


def _looks_like_bare_domain(s: str) -> bool:
    """True if s parses as a domain/URL rather than a company name. General:
    a name like 'Acme Plumbing' (spaces, no dot) is NOT a domain."""
    s = (s or "").strip()
    if not s or " " in s:
        return False
    return bool(_BARE_DOMAIN_RE.match(s))


def normalize_domain(raw: str) -> str:
    """Lowercase; strip scheme, credentials, port, www., path, query, trailing dot.
    Registrable name is left INTACT — no hyphen or character surgery."""
    if not raw:
        return ""
    host = urlparse(_ensure_scheme(raw)).netloc or ""
    host = host.split("@")[-1].split(":")[0].lower()
    if host.startswith("www."):
        host = host[4:]
    return host.rstrip(".")


def classify_input(raw: str) -> dict:
    """Classify a competitor input as a domain/URL or a company name. General."""
    s = (raw or "").strip()
    if not s:
        return {"kind": "empty", "name": "", "domain": ""}
    if "://" in s or s.lower().startswith("www.") or _looks_like_bare_domain(s):
        return {"kind": "domain", "name": "", "domain": normalize_domain(s)}
    return {"kind": "name", "name": s, "domain": ""}


def _tokens(s: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if t]


def _core_tokens(identity: str) -> set[str]:
    """The tokens that must appear for a result to be about this competitor.
    For a domain input, the registrable label only (basecamp.com -> {basecamp});
    the TLD ('com') is not evidence. For a name, the name's tokens."""
    cls = classify_input(identity)
    if cls["kind"] == "domain":
        label = (cls["domain"] or "").split(".")[0]
        return set(_tokens(label)) | ({label.replace("-", "")} if label else set())
    return set(_tokens(cls["name"]))


def _tied(identity: str, title: str, slug: str) -> bool:
    """True when the title or slug carries at least one core token of the
    competitor (or the slug contains the squashed label, e.g. 'acmeplumbing')."""
    core = _core_tokens(identity)
    if not core:
        return False
    tt = set(_tokens(f"{title} {slug.replace('.', ' ')}"))
    squashed = re.sub(r"[^a-z0-9]", "", (slug or "").lower())
    return bool(core & tt) or any(len(c) >= 4 and c in squashed for c in core)


def _name_score(name: str, text: str) -> float:
    """Token-overlap score between a company name and a candidate title/slug.

    General signal, not company-specific:
      + fraction of the NAME's tokens found in the candidate text
      - a small penalty per EXTRA token in the candidate

    The penalty is what makes a bare-brand page ("Acme Plumbing") outrank a regional
    page ("Acme Plumbing Salt Lake City UT") or a franchise directory — without any
    company-specific rule. It generalizes to every multi-location brand.
    """
    nt = set(_tokens(name))
    if not nt:
        return 0.0
    tt = set(_tokens(text))
    overlap = len(nt & tt) / len(nt)
    extra = len(tt - nt)
    return round(overlap - 0.05 * extra, 4)


# ---------------------------------------------------------------------------
# Per-target candidate ranking (all pure, all general)
# ---------------------------------------------------------------------------

def rank_domain_candidates(organic: list, identity: str) -> list[dict]:
    """Rank non-directory company domains from SERP organic results.
    Returns [{domain, title, position, score}] best-first, deduped by domain."""
    scored: list[dict] = []
    for r in organic or []:
        if not isinstance(r, dict):
            continue
        d = _displayed_domain(r.get("displayedUrl") or "")
        if not d:
            u = r.get("url") or r.get("link") or ""
            ud = _domain(u)
            if ud and "google.com" not in ud:
                d = ud
        if not d or _is_directory(d):
            continue
        title = r.get("title") or ""
        pos = int(r.get("position") or 999)
        score = _name_score(identity, f"{title} {d}") - 0.02 * pos
        scored.append({"domain": d, "title": title, "position": pos, "score": round(score, 4)})
    scored.sort(key=lambda c: -c["score"])
    seen: set[str] = set()
    out: list[dict] = []
    for c in scored:
        if c["domain"] in seen:
            continue
        seen.add(c["domain"])
        out.append(c)
    return _apex_first(out)


def _apex_first(cands: list[dict]) -> list[dict]:
    """Never rank a subdomain above its own apex when both are candidates.

    WHY (seen in a real UK run): the name search returned a
    "Revolut Brazil" help page first, so help.revolut.com (0.61) outranked
    revolut.com (0.56) and became the Google target. A help, support, docs or
    status host is never the advertiser's domain when its apex is right there.
    General rule, no word list: a candidate that ends in "." + another
    candidate yields to it. The apex takes the place of its best-ranked
    subdomain; the subdomain stays as an alternate, and scores are unchanged
    so the report still shows what the search said."""
    doms = [c["domain"] for c in cands]
    by_dom = {c["domain"]: c for c in cands}

    def _apex(d: str) -> str:
        parents = [p for p in doms if d.endswith("." + p)]
        return min(parents, key=len) if parents else d

    out: list[dict] = []
    placed: set[str] = set()
    for c in cands:
        for d in (_apex(c["domain"]), c["domain"]):
            if d not in placed:
                placed.add(d)
                out.append(by_dom[d])
    return out


def rank_facebook_pages(organic: list, name: str) -> list[dict]:
    """Rank facebook.com advertiser-Page candidates. Skips non-page FB paths.
    Regional/location pages sink via the extra-token penalty. General."""
    scored: list[dict] = []
    for r in organic or []:
        if not isinstance(r, dict):
            continue
        url = _first_url(r)
        if not url or not normalize_domain(url).endswith("facebook.com"):
            continue
        path = urlparse(_ensure_scheme(url)).path.strip("/")
        if not path:
            continue
        slug = path.split("/")[0]
        if slug.lower() in _FB_NON_PAGE:
            continue
        title = r.get("title") or ""
        # The title is the page's name only when the result IS the page (bare
        # facebook.com/<slug>). On a post (/<slug>/posts/...) the title is the
        # post's text, which may mention the brand on someone else's page (real
        # case: ClutchPointsFootball's post linking basecamp.com), so there
        # only the slug counts.
        is_page = "/" not in path
        if not _tied(name, title if is_page else "", slug):
            continue  # no evidence this page belongs to the competitor
        score = _name_score(name, f"{title} {slug.replace('.', ' ')}")
        scored.append({
            "url": f"https://www.facebook.com/{slug}",
            "slug": slug, "title": title, "score": score,
        })
    scored.sort(key=lambda c: -c["score"])
    seen: set[str] = set()
    out: list[dict] = []
    for c in scored:
        k = c["slug"].lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(c)
    return out


def _page_named_as(identity: str, cand: dict) -> bool:
    """True when a Facebook page candidate is named exactly as the competitor:
    its address (slug) or the page-name part of its title is the competitor's
    name or website name, spaces and punctuation dropped.

    WHY: a page tied to the competitor only by a shared word is a
    namesake as often as not. Real Basecamp rows: "Patagonian
    BaseCamp Lodge" (0.35) and "Air Force BASECAMP | Dhaka" (0.30) would be
    used whenever the real page ("Basecamp (@basecamphq)", 0.45) is missing
    from the search. The real pages of Revolut (/revolut), HubSpot (/hubspot,
    whose title is a post's text), Acme Plumbing (/AcmePlumbing) and Basecamp
    ("Basecamp (@basecamphq)") are all named exactly. The title's page name is
    the text before " | ", " - ", " · " or "(@handle)"."""
    cls = classify_input(identity)
    if cls["kind"] == "domain":
        keys = {_squash_words(_registrable_label(cls["domain"]))}
    else:
        keys = {_squash_words(cls["name"])}
    keys.discard("")
    if not keys:
        return False
    if _squash_words(cand.get("slug") or "") in keys:
        return True
    title = re.split(r"\(@|\s[|·\-–—]\s", cand.get("title") or "")[0]
    return _squash_words(title) in keys


def extract_linkedin_company(organic: list, name: str) -> dict | None:
    """Find a linkedin.com/company/<slug> result and read the full company name
    from its title. Returns {name, url, slug, score} or None. General."""
    best: dict | None = None
    for r in organic or []:
        if not isinstance(r, dict):
            continue
        url = _first_url(r)
        if not url or not normalize_domain(url).endswith("linkedin.com"):
            continue
        m = re.search(r"/company/([^/?#]+)", urlparse(_ensure_scheme(url)).path)
        if not m:
            continue
        slug = m.group(1)
        title = r.get("title") or ""
        # Titles look like "Acme Plumbing and Drain Service | LinkedIn".
        clean = re.split(r"\s*[|\-–—]\s*LinkedIn", title)[0].strip() or slug
        if not _tied(name, clean, slug):
            continue  # no evidence this company page belongs to the competitor
        score = _name_score(name, clean)
        cand = {"name": clean, "url": f"https://www.linkedin.com/company/{slug}",
                "slug": slug, "score": score}
        if best is None or score > best["score"]:
            best = cand
    return best


def domain_agreement(supplied: str, resolved: str) -> str:
    """'match' | 'mismatch' | 'no_supplied' | 'unresolved'.
    Normalized-exact comparison — a format difference (a hyphen) is a REAL
    mismatch to surface for confirmation, never a thing to auto-correct."""
    s = normalize_domain(supplied or "")
    rv = normalize_domain(resolved or "")
    if not s:
        return "no_supplied"
    if not rv:
        return "unresolved"
    return "match" if s == rv else "mismatch"


# ---------------------------------------------------------------------------
# Whose Google ads are these?
#
# WHY: the Google Ads Transparency search by website returns every ad that
# points to that website, whoever paid for it. Real runs: of the 10
# ads found for hubspot.com only 5 were by "Hubspot, Inc."; the rest were by
# Cardone Training Technologies, Redial BPO and others (businesses whose ads
# link to pages hosted on hubspot.com). For revolut.com, 1 of 83 was by IHG
# Hotels & Resorts. The report then read them all as the competitor's ads.
#
# The evidence is the advertiser on the record: `advertiserName` (Google's
# verified advertiser name) and `advertiserId` (its account id), and the
# website that was searched (the `domain` in `originUrl`). An advertiser is
# the competitor's when its name carries the competitor's name or website
# name as whole words ("Revolut Ltd", "Hubspot, Inc.", "M J Services Inc DBA
# Best Drains Plumbing"), or when the user listed it for that competitor
# (`google_advertisers` in the competitor's competitors/resolution.json
# entry). A name is judged once per advertiser id, so every ad of an
# advertiser gets the same answer.
#
# Every other ad is set aside: not shown as the competitor's, and counted with
# its advertiser names. The tool does not guess further. A company that
# advertises under another legal name (Basecamp's ads are by "37signals LLC";
# a franchisor's by its parent group) is set aside too, named, and the user can
# add that name. Setting a real ad aside is visible and fixable; showing
# another company's ad as the competitor's is the wrong-target failure.
# Filtering happens after the paid call: what is scraped, and what it costs,
# is unchanged, and the raw file keeps every record.
# ---------------------------------------------------------------------------

def _registrable_label(domain: str) -> str:
    """The name part of a website: revolut.com -> revolut, help.revolut.com ->
    revolut, bbc.co.uk -> bbc, acme-plumbing.com -> acme-plumbing."""
    labels = [x for x in normalize_domain(domain).split(".") if x]
    if len(labels) >= 3 and len(labels[-1]) == 2 and len(labels[-2]) <= 3:
        labels = labels[:-2]          # co.uk, com.au, ...
    elif len(labels) >= 2:
        labels = labels[:-1]          # .com, .io, ...
    return labels[-1] if labels else ""


def _squash_words(s: str) -> str:
    return "".join(_tokens(s))


def google_searched_domain(rec: dict) -> str:
    """The website a Google ad was found by (the `domain` of the search URL the
    ad came from), '' when the record does not say."""
    from urllib.parse import parse_qs
    for key in ("originUrl", "url"):
        q = parse_qs(urlparse(str(rec.get(key) or "")).query)
        d = (q.get("domain") or [""])[0]
        if d:
            return normalize_domain(d)
    return ""


def _owner_names(entry: dict, searched: str) -> tuple:
    """(words that identify the competitor, advertiser names the user listed)."""
    keys = set()
    if entry.get("kind") == "name":
        keys.add(_squash_words(entry.get("input") or ""))
    dom = entry.get("domain") or {}
    for d in (searched, dom.get("use"), dom.get("supplied")):
        if d:
            keys.add(_squash_words(_registrable_label(d)))
    listed = {_squash_words(n) for n in (dom.get("google_advertisers") or [])
              if isinstance(n, str)}
    keys.discard("")
    listed.discard("")
    return keys, listed


def _name_is(adv_name: str, keys: set, listed: set) -> bool:
    """True when the advertiser name is a listed name, or carries one of the
    keys as whole words (any run of its words, spaces and punctuation dropped:
    "Best Drains" carries "bestdrains", "Acme Plumbing Services" carries
    "acmeplumbing"; "Revolution Bars" does not carry "revolut")."""
    toks = _tokens(adv_name)
    if not toks:
        return False
    if "".join(toks) in listed:
        return True
    for i in range(len(toks)):
        for j in range(i + 1, len(toks) + 1):
            if "".join(toks[i:j]) in keys:
                return True
    return False


def _who(entry: dict, searched: str) -> str:
    return (entry or {}).get("input") or searched or "the competitor"


# Who an ad whose search website is not recorded was checked against: google_ad_owners pools the
# names of every competitor whose website
# was searched, so "the competitor" named no one that was actually checked. The
# heading over the labelled section ("not confirmed as the competitor's") stays true
# over such a label: an ad not confirmed as any of them is not confirmed as the one
# competitor a reader has in mind either.
ANY_COMPETITOR = "any of the competitors whose websites were searched"


def google_ad_owners(items: list, entries: list) -> list:
    """One verdict per record of the Google ads file, in order:
      "own"     the advertiser is the competitor (see the block above);
      "other"   the advertiser's name does not match the competitor;
      "unnamed" the record carries no advertiser name or id to judge by.
    Pure. `entries` is competitors/resolution.json ([] when absent: the
    searched website's own name is then the only key)."""
    by_dom = {}
    for e in entries or []:
        if isinstance(e, dict):
            d = normalize_domain((e.get("domain") or {}).get("use") or "")
            if d:
                by_dom.setdefault(d, e)
    groups = {}   # (searched, advertiser key) -> [names]
    keyed = []
    for rec in items or []:
        rec = rec if isinstance(rec, dict) else {}
        searched = google_searched_domain(rec)
        name = str(rec.get("advertiserName") or "").strip()
        aid = str(rec.get("advertiserId") or "").strip()
        akey = aid or ("name:" + _squash_words(name) if _squash_words(name) else "")
        keyed.append((searched, akey))
        if akey:
            groups.setdefault((searched, akey), []).append(name)
    verdict_of = {}
    for (searched, akey), names in groups.items():
        if searched in by_dom:
            keys, listed = _owner_names(by_dom[searched], searched)
        elif searched:
            keys, listed = _owner_names({}, searched)
        else:   # the record does not say which website found it: any competitor
            keys, listed = set(), set()
            for e in by_dom.values():
                k, l = _owner_names(e, "")
                keys |= k
                listed |= l
        own = any(_name_is(n, keys, listed) for n in names if n)
        verdict_of[(searched, akey)] = "own" if own else (
            "other" if any(names) else "unnamed")
    return [verdict_of[k] if k[1] else "unnamed" for k in keyed]


def google_set_aside_lines(items: list, entries: list, verdicts: list = None) -> list:
    """Plain sentences naming the Google ads that are not shown as a
    competitor's, one per website searched ([] when none are set aside).
    Counts come from google_ad_owners, never from the caller."""
    if verdicts is None:
        verdicts = google_ad_owners(items, entries)
    by_dom = {normalize_domain((e.get("domain") or {}).get("use") or ""): e
              for e in (entries or []) if isinstance(e, dict)}
    per = {}      # searched -> {"total", "other": {name: n}, "unnamed"}
    order = []
    for rec, v in zip(items or [], verdicts):
        rec = rec if isinstance(rec, dict) else {}
        s = google_searched_domain(rec)
        if s not in per:
            per[s] = {"total": 0, "other": {}, "unnamed": 0}
            order.append(s)
        p = per[s]
        p["total"] += 1
        if v == "other":
            n = str(rec.get("advertiserName") or "").strip()
            p["other"][n] = p["other"].get(n, 0) + 1
        elif v == "unnamed":
            p["unnamed"] += 1
    lines = []
    for s in order:
        p = per[s]
        n_other = sum(p["other"].values())
        if not n_other and not p["unnamed"]:
            continue
        # An ad whose search website is not recorded was
        # checked against every competitor whose website was searched
        # (google_ad_owners), not against one, so its line names no single competitor.
        # by_dom.get("") is a competitor with no website, which was not checked at all.
        who = _who(by_dom.get(s), s) if s else ANY_COMPETITOR
        not_by = f"ads by {who}" if s else "ads by any competitor"
        where = f"found for {s}" if s else "whose search website is not recorded"
        if n_other:
            names = ", ".join(f"{n} ({c})" for n, c in
                              sorted(p["other"].items(), key=lambda kv: (-kv[1], kv[0])))
            lines.append(
                f"{n_other} of {p['total']} Google ads {where} came from advertisers whose "
                f"name does not match {who}: {names}. They are not shown as {not_by}.")
                # The how-to (GOOGLE_CONFIRM_HOW) is no longer at the end of
                # each line. It was once put here (the older "add
                # that name to google_advertisers in its entry" contradicted the
                # labelled section); but every place that shows these lines also shows
                # it once (the app's labelled section, the report's closing section,
                # the run's console after the lines), and with one line per website
                # searched the same sentence was read three times on one page, which reads as three instructions.
        if p["unnamed"]:
            lines.append(
                f"{p['unnamed']} of {p['total']} Google ads {where} carry no advertiser "
                f"name, so the tool cannot tell whose they are. They are not shown as "
                f"{not_by}.")
    return lines


def google_set_aside_sentence(verdicts: list) -> str:
    """One plain sentence for every place a Google count is read briefly (the
    run's closing lines, the notification, the app), '' when nothing is set
    aside. A filter that hides ads without saying so
    turns into a confident wrong number wherever the shorter count is read.
    The counts come from google_ad_owners, never from the caller."""
    other = sum(1 for v in verdicts or [] if v == "other")
    unnamed = sum(1 for v in verdicts or [] if v == "unnamed")
    n = other + unnamed
    if not n:
        return ""
    why = ("the advertiser's name does not match the competitor" if not unnamed
           else "the advertiser's name is missing" if not other
           else "the advertiser's name does not match the competitor or is missing")
    return (f"{n} Google {'ad was' if n == 1 else 'ads were'} set aside because {why}; "
            f"the report names them.")


def google_own_lines(items: list, entries: list, verdicts: list = None) -> list:
    """Plain lines naming, per website searched, the advertiser names whose
    Google ads were counted as the competitor's own, with counts ([] when none).

    WHY: the whole-word rule counts "HubSpot Partner
    Agency X" as HubSpot's. The tool does not guess partners (no guessing
    rules); it names every advertiser it counted, so a reader can see a
    partner among them and fix it in competitors/resolution.json."""
    if verdicts is None:
        verdicts = google_ad_owners(items, entries)
    by_dom = {normalize_domain((e.get("domain") or {}).get("use") or ""): e
              for e in (entries or []) if isinstance(e, dict)}
    per, order = {}, []
    for rec, v in zip(items or [], verdicts):
        if v != "own":
            continue
        rec = rec if isinstance(rec, dict) else {}
        s = google_searched_domain(rec)
        if s not in per:
            per[s] = {}
            order.append(s)
        n = str(rec.get("advertiserName") or "").strip() or "(no name)"
        per[s][n] = per[s].get(n, 0) + 1
    lines = []
    for s in order:
        who = _who(by_dom.get(s), s)
        names = ", ".join(f"{n} ({c})" for n, c in
                          sorted(per[s].items(), key=lambda kv: (-kv[1], kv[0])))
        where = f" (found for {s})" if s and s != who else ""
        lines.append(f"Google ads counted as {who}'s{where}: {names}.")
    return lines


# How the user makes set-aside ads count as a competitor's. Shown
# wherever the labelled ads are shown (the app and the report), so it is written
# for the user, not for Claude: the user asks, and Claude makes the edit where the
# marketing-viewer SKILL.md says (entry["domain"]["google_advertisers"], the one
# place _owner_names reads), then rebuilds with viewer_ctl.py reindex (free, reads
# only what is on disk). WHY: the first wording told the reader to
# edit the file themselves and did not say where inside the entry; followed
# literally, it changed nothing and the rebuild said nothing. The rebuild does not
# rewrite report/, so the sentence says that too. The
# report step, run again, rewrites report/E3_creative_angles.md from its template
# (e3_report.assemble_report), so a written report is gone and has to be written
# again; the sentence says so plainly instead of "keeps its old wording until".
# Each place that shows it says it once (it no longer closes every line of
# google_set_aside_lines), so every place says one thing, one time.
GOOGLE_CONFIRM_HOW = (
    "To count an advertiser's ads as a competitor's, ask Claude to add the advertiser's "
    "name, as written here, to that competitor's Google advertisers and to rebuild the "
    "app view for this run (free, nothing is scraped). This does not change the written "
    "report. Running the report step again creates the written report again from the "
    "start, so it then has to be written again.")


def _near_key(k) -> bool:
    """True for a near-miss spelling of "google_advertisers" (case, "-", "_" or a
    space, singular). The exact key is not a near miss."""
    s = re.sub(r"[\s_\-]", "", str(k).lower())
    return s in ("googleadvertisers", "googleadvertiser")


def google_advertisers_misplaced(entries) -> list:
    """Plain sentences, one per "google_advertisers" key in competitors/resolution.json
    that the tool does not read ([] when every such key is where it is read).

    WHY (the silent-zero class): the tool reads advertiser names only
    from entry["domain"]["google_advertisers"], a list of names (_owner_names). A key
    put anywhere else (directly in the entry, the literal reading of the old confirm
    sentence, or under "facebook"), or a single name written as text instead of a
    list, changed nothing, and the rebuild reported success. This says so, naming the
    competitor and where the key belongs. It only warns: which ads are set aside is
    unchanged (the matching rule above is not touched). Pure.

    No warning on what the tool reads correctly. A null,
    an empty list, or a list holding a blank name is read as "no names" (_owner_names
    uses `or []` and drops blanks), so it is a correct file and gives no warning: a
    warning that fires on a correct file teaches users to ignore warnings.
    A near-miss spelling of the key ("Google_Advertisers",
    "google-advertisers", "google_advertiser") and a list whose entries are not names
    (objects, numbers) were read as nothing and said nothing; both are named now."""
    out = []

    def where(path):
        return ("under " + " > ".join(f'"{p}"' for p in path)) if path else "directly in the entry"

    def walk(node, path, who):
        if isinstance(node, dict):
            for k, v in node.items():
                here = path + [str(k)]
                if k != "google_advertisers" and _near_key(k):
                    out.append(
                        f"In competitors/resolution.json, the entry for {who} has "
                        f"\"{k}\" {where(path)}, a spelling the tool does not read, so no "
                        f"advertiser was added. Write it exactly as \"google_advertisers\" "
                        f"inside the entry's \"domain\", as \"domain\": {{\"use\": ..., "
                        f"\"google_advertisers\": [...]}}, then rebuild the index.")
                if k == "google_advertisers":
                    if here == ["domain", "google_advertisers"]:
                        if isinstance(v, list) and any(
                                n is not None and not isinstance(n, str) for n in v):
                            out.append(
                                f"In competitors/resolution.json, \"google_advertisers\" for "
                                f"{who} lists something other than names, which the tool "
                                f"does not read. Write each advertiser as a name in quotes, "
                                f"for example \"google_advertisers\": [\"Advertiser name\"], "
                                f"then rebuild the index.")
                        # null, "" and other empty values are read as no names (`or []`
                        # in _owner_names), so only a value with content can be wrong.
                        elif v and not isinstance(v, list):
                            out.append(
                                f"In competitors/resolution.json, \"google_advertisers\" for "
                                f"{who} is not a list of names, so the tool does not read it. "
                                f"Write it as a list, for example \"google_advertisers\": "
                                f"[\"Advertiser name\"], then rebuild the index.")
                    else:
                        out.append(
                            f"In competitors/resolution.json, the entry for {who} has "
                            f"\"google_advertisers\" {where(path)}, where the tool does "
                            f"not read it, so no advertiser was added. Move it inside the "
                            f"entry's \"domain\", as \"domain\": {{\"use\": ..., "
                            f"\"google_advertisers\": [...]}}, then rebuild the index.")
                walk(v, here, who)
        elif isinstance(node, list):
            for v in node:
                walk(v, path, who)

    if isinstance(entries, list):
        for e in entries:
            if isinstance(e, dict):
                dom = e.get("domain") if isinstance(e.get("domain"), dict) else {}
                walk(e, [], str(e.get("input") or dom.get("use") or "a competitor"))
    elif isinstance(entries, dict):
        # A file that is one object instead of a list of entries: nothing in it is read.
        if "google_advertisers" in json.dumps(entries):
            out.append("competitors/resolution.json is not a list of competitor entries, so "
                       "the tool does not read the \"google_advertisers\" in it.")
    return out


def google_set_aside_labels(items: list, entries: list, verdicts: list = None) -> list:
    """The label over the set-aside Google ads, one per website searched, in the
    order the websites first appear ([] when none are set aside):
      {"searched": website or "", "competitor": who, "label": plain sentences,
       "advertisers": [[name, count], ...] (most ads first), "unnamed": count}.

    WHY ("label them"): a set-aside ad may be the
    competitor's own under another legal name (Basecamp's ads are by "37signals
    LLC"), so the user sees these ads, but only under this label: where they came
    from, who placed them, and that they are not confirmed as the competitor's.
    Pure; the counts come from google_ad_owners, never from the caller."""
    if verdicts is None:
        verdicts = google_ad_owners(items, entries)
    by_dom = {normalize_domain((e.get("domain") or {}).get("use") or ""): e
              for e in (entries or []) if isinstance(e, dict)}
    per, order = {}, []
    for rec, v in zip(items or [], verdicts):
        # Count what is shown under the label: an entry of the raw file that is not
        # an ad record (null) has no card (emit_index.labelled_set_aside skips it),
        # so it is not in the label's count either ("These 2 Google
        # ads" over 1 card). set_aside["count"] keeps counting it, as before.
        if v == "own" or not isinstance(rec, dict):
            continue
        s = google_searched_domain(rec)
        if s not in per:
            per[s] = {"other": {}, "unnamed": 0}
            order.append(s)
        if v == "other":
            n = str(rec.get("advertiserName") or "").strip()
            per[s]["other"][n] = per[s]["other"].get(n, 0) + 1
        else:
            per[s]["unnamed"] += 1
    out = []
    for s in order:
        p = per[s]
        # As in google_set_aside_lines, the text for an ad
        # whose search website is not recorded names no single competitor. The
        # "competitor" field keeps its value (the brief changes the text only).
        who = _who(by_dom.get(s), s)
        not_who = who if s else ANY_COMPETITOR
        whose_ad = f"{who}'s" if s else "any competitor's"
        where = (f"when searching {s}" if s
                 else "in a Google search whose website is not recorded")
        advs = sorted(p["other"].items(), key=lambda kv: (-kv[1], kv[0]))
        parts = []
        n_other = sum(c for _, c in advs)
        if n_other:
            names = ", ".join(f"{n} ({c})" for n, c in advs)
            one = n_other == 1
            whose = ("an advertiser whose name does" if len(advs) == 1
                     else "advertisers whose names do")
            parts.append(
                f"{'This Google ad' if one else f'These {n_other} Google ads'} came up "
                f"{where}, but {'it was' if one else 'they were'} placed by {names}, "
                f"{whose} not match {not_who}. {'It is' if one else 'They are'} not confirmed "
                f"as {whose_ad} {'ad' if one else 'ads'} and {'is' if one else 'are'} not "
                f"included in the run's ad counts.")
        if p["unnamed"]:
            k = p["unnamed"]
            one = k == 1
            parts.append(
                f"{'This Google ad' if one else f'These {k} Google ads'} came up "
                f"{where}, but {'it carries' if one else 'they carry'} no advertiser name, "
                f"so the tool cannot tell whose {'it is' if one else 'they are'}. "
                f"{'It is' if one else 'They are'} not confirmed as {whose_ad} "
                f"{'ad' if one else 'ads'} and {'is' if one else 'are'} not included in the "
                f"run's ad counts.")
        out.append({"searched": s, "competitor": who, "label": " ".join(parts),
                    "advertisers": [[n, c] for n, c in advs], "unnamed": p["unnamed"]})
    return out


# ---------------------------------------------------------------------------
# Assemble one resolution entry, and format the human-facing report
# ---------------------------------------------------------------------------

# What the report says about Meta for a competitor with no Facebook page.
# WHY ("honest label"): the Meta stage searches by
# Facebook page, or by `meta_search_terms` only when NO competitor has a page
# (stages.build_B4); it never searches a competitor by its name. An earlier
# label promised a "keyword sweep" that never ran, so the user approved a run
# believing Meta was searched and then read the empty result as "they run no
# Meta ads". The flag is stored in resolution.json, so it must hold whatever
# the settings say: it states only that the competitor's own Meta ads are not
# searched without its page, which is true in every case.
_META_NO_PAGE_FLAG = ("no Facebook Page found: this competitor's own Meta ads are "
                      "not searched unless you add its Facebook page")
# The old text, still inside resolution.json files written before item f (a
# repeat reuses last month's file). Shown as the new text, never as written.
_LEGACY_META_NO_PAGE_FLAG = ("no Facebook Page found — Meta falls back to keyword sweep "
                             "unless a Page is supplied/confirmed")

# The flags below lost their em-dash and the word "stage" (plain words
# for a public tool). WHY the old words still need mapping: a repeat reuses last
# month's resolution.json, whose flags hold them, and the report shows a stored
# entry's flags. Each old text is matched exactly and shown in the new words.
_NO_DOMAIN_FLAG = ("no domain resolved: this competitor's Google ads are not searched "
                   "unless you add its website")
_NO_LINKEDIN_FLAG = "no LinkedIn company found: this competitor's LinkedIn ads are not searched"
_LEGACY_FLAGS = {
    "no domain resolved — Google Ads Transparency will be empty": _NO_DOMAIN_FLAG,
    "no LinkedIn company found — LinkedIn stage will be empty": _NO_LINKEDIN_FLAG,
    _LEGACY_META_NO_PAGE_FLAG: _META_NO_PAGE_FLAG,
}
# The hyphen that earlier versions put in place of the
# em-dash is the same pause in another glyph, so the flags now use a colon. Files
# written by those versions hold the hyphen form; they are shown in the new words.
_LEGACY_HYPHEN_FLAGS = {
    ("no Facebook Page found - this competitor's own Meta ads are not searched "
     "unless you add its Facebook page"): _META_NO_PAGE_FLAG,
    ("no domain resolved - this competitor's Google ads are not searched unless "
     "you add its website"): _NO_DOMAIN_FLAG,
    "no LinkedIn company found - this competitor's LinkedIn ads are not searched":
        _NO_LINKEDIN_FLAG,
}
_LEGACY_FLAGS.update(_LEGACY_HYPHEN_FLAGS)
# The mismatch flag names the two domains, so only its old ending is replaced.
_LEGACY_MISMATCH_END = " — confirm the real advertiser domain before the Google scrape"
_LEGACY_MISMATCH_END_HYPHEN = " - confirm the real advertiser domain before the Google scrape"
_MISMATCH_END = ". Confirm the real advertiser domain before the Google scrape"


def _shown_flag(flag: str) -> str:
    """A stored flag in the words this version shows (see _LEGACY_FLAGS)."""
    if flag in _LEGACY_FLAGS:
        return _LEGACY_FLAGS[flag]
    if flag.startswith("domain mismatch:"):
        for end in (_LEGACY_MISMATCH_END, _LEGACY_MISMATCH_END_HYPHEN):
            if flag.endswith(end):
                return flag[:-len(end)] + _MISMATCH_END
    return flag


def _fb_page(entry: dict) -> str:
    """The Facebook page the Meta stage will search for this entry ('' = none).
    Same reading as stages._resolved_facebook_pages."""
    return ((entry.get("facebook") or {}).get("use") or "").strip()


def resolve_competitor(raw: str, organic: list, supplied_domain: str = "") -> dict:
    """Given a raw input and its SERP organic results, return a resolution entry
    with a chosen value + ranked candidates + flags for every target. Pure.

    The `use` fields are DEFAULTS for a non-interactive run; the `flags` and
    `candidates` exist so the resolution report can ask the human to confirm or
    correct before any spend. On a supplied-vs-resolved domain mismatch the
    default is the EVIDENCE-BACKED resolved domain (the supplied one is unproven),
    but the mismatch is always flagged — never silently applied.

    supplied_domain: the domain the user gave for this same company when it
    was given both as a name and as a domain (see pair_competitors). It is
    checked against the resolved domain exactly like a domain input.
    """
    cls = classify_input(raw)
    identity = cls["name"] or cls["domain"]
    supplied = cls["domain"] or normalize_domain(supplied_domain)

    domains = rank_domain_candidates(organic, identity)
    resolved_domain = domains[0]["domain"] if domains else ""
    # A page with a score at or below zero is never used, not even as
    # an alternate. WHY (real paid runs): the resolver proposed
    # facebook.com/BFMTV for Basecamp at -0.05, and Meta would have searched a
    # French TV channel as Basecamp. A score at or below zero means the page's
    # name carries less of the competitor's name than it carries other words,
    # which is no evidence that the page is theirs. With no page left, the
    # report and the run say plainly that no Facebook page was found and what
    # Meta searches instead (see _META_NO_PAGE_FLAG).
    fb = [c for c in rank_facebook_pages(organic, identity) if c["score"] > 0]
    # Namesakes: a page whose name only CONTAINS the brand word
    # ("Patagonian BaseCamp Lodge", "Air Force BASECAMP | Dhaka") is not used
    # on its own; it stays a possible page for the user to confirm. Only a page
    # named exactly as the competitor is used (_page_named_as), best score first.
    named = [c for c in fb if _page_named_as(identity, c)]
    fb = named + [c for c in fb if c not in named]
    li = extract_linkedin_company(organic, identity)
    agree = domain_agreement(supplied, resolved_domain)

    flags: list[str] = []
    if agree == "mismatch":
        flags.append(
            f"domain mismatch: supplied {supplied!r} != resolved "
            f"{resolved_domain!r}{_MISMATCH_END}"
        )
    if not resolved_domain and cls["kind"] != "domain" and not supplied:
        flags.append(_NO_DOMAIN_FLAG)
    if not named:
        flags.append(_META_NO_PAGE_FLAG)
    if not li:
        flags.append(_NO_LINKEDIN_FLAG)

    if agree == "match":
        use_domain = supplied
    elif agree == "mismatch":
        use_domain = resolved_domain          # evidence-backed default; flagged above
    else:  # no_supplied / unresolved
        use_domain = resolved_domain or supplied

    return {
        "input": raw,
        "kind": cls["kind"],
        "domain": {
            "use": use_domain, "supplied": supplied,
            "resolved": resolved_domain, "agreement": agree,
            "candidates": domains[:3],
        },
        "facebook": {"use": named[0]["url"] if named else "", "candidates": fb[:4]},
        "linkedin": {
            "use": li["name"] if li else "",
            "url": li["url"] if li else "", "candidate": li,
        },
        "flags": flags,
    }


def format_resolution_report(entries: list[dict], meta_search_terms=None) -> str:
    """Human-facing report shown at the dry-run gate before any spend.

    meta_search_terms: the config's `meta_search_terms`. The Meta line of a
    competitor without a Facebook page depends on it and on the other
    competitors, because that is what decides what the Meta stage searches
    (stages.build_B4): pages first; the search words only when no competitor
    has a page; nothing otherwise. Every competitor that Meta will not search
    is named again at the end, so the user never approves a run believing it.

    A word that is only whitespace, or null, counts as no word
    (stages.meta_words, the same reading as build_B4). When Meta is searched by the words
    because no competitor has a page, the competitors are named at the end too:
    their own Meta ads are still not searched. Words that are set but not used
    are named whenever a competitor has a page, not only in the mixed case.
    """
    terms = meta_words(meta_search_terms)
    any_page = any(_fb_page(e) for e in entries)
    meta_unsearched = [] if (not any_page and terms) else \
        [e["input"] for e in entries if not _fb_page(e)]
    own_unsearched = [e["input"] for e in entries if not _fb_page(e)] \
        if (not any_page and terms) else []
    lines = ["", "=" * 64, "RESOLUTION REPORT: confirm before any paid scrape", "=" * 64]
    any_flag = False
    for e in entries:
        also = (e.get("domain") or {}).get("supplied") if e.get("kind") == "name" else ""
        also = f", also given as the domain {also}" if also else ""
        lines.append(f"\n▸ input: {e['input']!r}  (read as {e['kind']}{also})")
        dom = e["domain"]
        tag = {"match": "✓ matches supplied", "mismatch": "⚠ MISMATCH vs supplied",
               "no_supplied": "resolved from name", "unresolved": "⚠ NOT resolved"}[dom["agreement"]]
        lines.append(f"    Google  domain   : {dom['use'] or '(none)'}   [{tag}]")
        if dom["candidates"]:
            alt = ", ".join(f"{c['domain']}({c['score']})" for c in dom["candidates"])
            lines.append(f"              candidates: {alt}")
        fb = e["facebook"]
        if _fb_page(e):
            meta_line = fb["use"]
        elif any_page:
            meta_line = ("(none): NOT searched on Meta. Meta searches only the Facebook "
                         "pages found for the other competitors; add this one's page to "
                         "include it")
        elif terms:
            meta_line = ("(none): Meta is searched by your search words instead ("
                         + ", ".join(terms) + "): that finds any advertiser using those "
                         "words, not this competitor's own page")
        else:
            meta_line = ("(none): NOT searched on Meta, because no Facebook page and no Meta "
                         "search words are set; add its page, or set search words, to "
                         "include it")
        lines.append(f"    Meta    page     : {meta_line}")
        if _fb_page(e):
            alt = ", ".join(c["slug"] for c in fb["candidates"] if c.get("url") != _fb_page(e))
            if alt:
                lines.append(f"              alternates: {alt}")
        else:
            # Pages that carry the name but are not named exactly as the
            # competitor (possible namesakes) are offered, never used on their own.
            # A page scoring at or below zero (an old resolution file) is not offered.
            maybe = [c["slug"] for c in fb["candidates"] if (c.get("score") or 0) > 0]
            if maybe:
                lines.append("              possible pages, not used because none is named "
                             "exactly as this competitor (add one if it is theirs): "
                             + ", ".join(maybe))
        li = e["linkedin"]
        cid = li.get("company_id")
        # company_id is filled by enrich_linkedin_ids AFTER resolution; when a
        # name resolved but the numeric id did not, say so — the LI stage needs
        # the id and would otherwise be silently empty.
        if li["use"] and "company_id" in li:
            id_tag = (f"  [id {cid}]" if cid else
                      "  ⚠ NO numeric LinkedIn company id found, so its LinkedIn ads are not searched")
        else:
            id_tag = ""
        lines.append(f"    LinkedIn company : {li['use'] or '(none)'}{id_tag}")
        flags = [_shown_flag(f) for f in e["flags"]]
        # A stored entry whose page was removed by hand carries no flag; the
        # missing page still needs its warning.
        if not _fb_page(e) and _META_NO_PAGE_FLAG not in flags:
            flags.append(_META_NO_PAGE_FLAG)
        for f in flags:
            any_flag = True
            lines.append(f"    ⚠ {f}")
    if meta_unsearched:
        any_flag = True
        lines.append("")
        lines.append("⚠ NOT searched on Meta (no Facebook page): "
                     + ", ".join(f"'{n}'" for n in meta_unsearched) + ".")
        lines.append("  Their Meta ads will be missing from the results. That is a gap in "
                     "the search, not a sign they run no Meta ads.")
    if own_unsearched:
        any_flag = True
        lines.append("")
        lines.append("⚠ Own Meta ads not searched (no Facebook page): "
                     + ", ".join(f"'{n}'" for n in own_unsearched) + ".")
        lines.append("  Meta is searched by your search words instead ("
                     + ", ".join(terms) + "), which finds any advertiser using those words, "
                     "not these competitors' own ads. That is a gap in the search, not a "
                     "sign they run no Meta ads.")
    if any_page and terms and meta_unsearched:
        lines.append("  Your Meta search words are not used: Meta searches by Facebook "
                     "page whenever any competitor has one.")
    elif any_page and terms:
        any_flag = True
        lines.append("")
        lines.append("⚠ Your Meta search words are not used (" + ", ".join(terms)
                     + "): Meta searches each competitor by its Facebook page, and every "
                     "competitor has one.")
    lines.append("\n" + "-" * 64)
    lines.append("CONFIRM these targets (especially any ⚠) before approving spend."
                 if any_flag else "All targets resolved cleanly.")
    lines.append("=" * 64)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Bridge to the pipeline: read the B1 SERP dataset on disk (via resolver's
# shared readers — NOT re-derived here) and resolve every competitor identity,
# then materialize the per-target files the stages consume. Impure only in that
# it touches the workdir; the ranking logic above stays pure.
# ---------------------------------------------------------------------------

# Per competitor we run a SMALL set of SERP queries, not one. A bare-name query
# reliably surfaces the company DOMAIN, but Google frequently returns the
# advertiser's Facebook/LinkedIn profiles as a follower-count blob
# ("17.4L+ followers") with an opaque redirect URL — no extractable facebook.com
# link at all (observed live for HubSpot; the same "followers" junk appeared for
# Acme Plumbing, which only resolved because a clean facebook.com result ALSO
# happened to rank). A `site:` scoped query forces Google to return clean
# facebook.com / linkedin.com URLs, so Page/company resolution stops depending on
# luck. This is the mechanism the F0 design specified.
_QUERY_SUFFIXES: tuple[str, ...] = ("", " site:facebook.com", " site:linkedin.com")


def build_query_plan(identities: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    """For each identity, the SERP queries that resolve its targets: the bare
    identity (domain + general evidence), a facebook-scoped query (advertiser
    Page), and a linkedin-scoped query (full registered name). Returns
    (flat de-duped query list to send to B1, {identity -> its query strings})."""
    queries: list[str] = []
    plan: dict[str, list[str]] = {}
    seen: set[str] = set()
    for raw in identities:
        qs = [(raw + suf).strip() for suf in _QUERY_SUFFIXES]
        plan[raw] = qs
        for q in qs:
            if q and q.lower() not in seen:
                seen.add(q.lower())
                queries.append(q)
    return queries, plan


def write_queries(identities: list[str], workdir: str | Path) -> Path:
    """Write the resolution query plan to B1_serp_competitors/queries.json so the
    B1 stage searches each competitor's domain + Facebook + LinkedIn."""
    queries, _ = build_query_plan(identities)
    q_dir = Path(workdir) / "B1_serp_competitors"
    q_dir.mkdir(parents=True, exist_ok=True)
    out = q_dir / "queries.json"
    out.write_text(json.dumps({"queries": queries}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"resolve: wrote {len(queries)} resolution queries "
          f"({len(identities)} competitor(s) × {len(_QUERY_SUFFIXES)}) -> {out}")
    return out


def resolve_all(identities: list[str], workdir: str | Path,
                supplied: dict | None = None) -> list[dict]:
    """Resolve each raw competitor identity (a name OR a domain, any case) using
    the B1 SERP results already fetched to <workdir>/B1_serp_competitors/.
    Returns one resolution entry per identity (see resolve_competitor).
    `supplied` maps a name identity to the domain the user also gave for it
    (supplied_domains(cfg)); that domain is checked against the resolved one.

    Aggregates the organic results across ALL of an identity's queries (bare +
    facebook-scoped + linkedin-scoped), de-duped, before ranking — so domain,
    Facebook Page and LinkedIn name are each drawn from the query best suited to
    surface them. Identities with no matching B1 record resolve against [] —
    every target comes back empty and flagged, never silently dropped."""
    records = resolver.load_b1_records(workdir)
    index = resolver.index_by_term(records)
    _, plan = build_query_plan(identities)
    entries: list[dict] = []
    for raw in identities:
        organic: list[dict] = []
        seen_urls: set[str] = set()
        for q in plan.get(raw, [raw]):
            rec = index.get((q or "").lower().strip())
            for r in resolver.organic_of(rec):
                if not isinstance(r, dict):
                    continue
                key = f"{r.get('displayedUrl') or ''}|{r.get('url') or ''}|{r.get('title') or ''}"
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                organic.append(r)
        entries.append(resolve_competitor(raw, organic, (supplied or {}).get(raw, "")))
    return entries


# ---------------------------------------------------------------------------
# LinkedIn numeric company-id resolution (network — deliberately SEPARATE from
# the pure rankers and the offline tests). The LinkedIn Ad Library scraper scopes
# to an advertiser ONLY by NUMERIC company id (keyword/slug matching is fuzzy and
# returns OTHER advertisers). The public company page embeds the id (e.g. in
# `f_C=<id>` job links), readable without auth — so name → slug → numeric id needs
# no paid actor and no login.
# ---------------------------------------------------------------------------
# Ordered by trust. `f_C=<id>` is the id on the company's OWN "see all employees"
# link — structurally tied to THIS company, so it is checked first and, if it
# yields an unambiguous id, wins outright. The urn/companyId forms are fallbacks
# for when the page markup omits f_C. A `f_C` value can be a LIST of ids joined by
# a comma or its encoded form (`f_C=646286%2C70711`: the company plus an
# affiliated one), so the pattern captures the whole list and the reader splits
# it. It must never stop at the first id: that returned 646286 for Acme Plumbing,
# whose own company is 70711, and the run showed zero LinkedIn ads on a page that
# looked fine. A comma may be followed by whitespace, literal or encoded
# (`646286, 70711`, `646286%2C%2070711`, `646286%2C+70711`): the list is still a
# list, and reading it as one id was the same wrong target by another road. The whitespace is
# accepted only straight after a comma, never between two
# bare numbers, so `f_C=111 222` stays one id followed by prose.
_LI_ID_PATTERNS = (
    r"f_C=(\d+(?:(?:(?i:%2C)|,)(?:%20|\+|\s)*\d+)*)",
    r"urn:li:fsd_company:(\d+)",
    r"urn%3Ali%3Afsd_company%3A(\d+)",
    r'"companyId":\s*(\d+)',
)
# What separates the ids of a `f_C` list: a comma (plain or encoded) and any
# whitespace after it.
_LI_ID_SEPARATOR = r"(?:(?i:%2C)|,)(?:%20|\+|\s)*"
# The page's OWN organization is read from ONE place: the top-card menu entry the
# page puts on its own company (data-semaphore-content-urn="urn:li:organization:N"
# on a tag whose data-semaphore-tracking-prefix or data-tracking-control-name starts
# with `top-card`). It is the one signal that says which id in a `f_C` list is this
# company's. It is NOT read from every `urn:li:organization:` on the page: a feed
# post, an affiliated or similar company, or a showcase block names other
# organizations in the same markup, and taking one of those as "own" returned the
# affiliate's id. Both saved real pages
# (Acme Plumbing, HubSpot) carry exactly this tag, with the company's own id.
_LI_TOP_CARD_ORG = re.compile(
    r"""data-semaphore-content-urn\s*=\s*["']urn:li:organization:(\d+)["']""")
_LI_TOP_CARD_SCOPE = re.compile(
    r"""(?:data-semaphore-tracking-prefix|data-tracking-control-name)\s*=\s*["']top-card""")
_LI_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _linkedin_slug(entry: dict) -> str:
    """The LinkedIn vanity slug for a resolution entry ('' if none)."""
    li = entry.get("linkedin") or {}
    cand = li.get("candidate") or {}
    if cand.get("slug"):
        return cand["slug"]
    m = re.search(r"/company/([^/?#]+)", li.get("url") or "")
    return m.group(1) if m else ""


def fetch_linkedin_company_id(slug: str, timeout: int = 20) -> str:
    """Resolve a LinkedIn company vanity slug to its NUMERIC company id via the
    public company page (no auth). Returns '' on any failure. A numeric slug is
    passed through unchanged. Deterministic HTTP GET — safe to time out."""
    if not slug:
        return ""
    if slug.isdigit():
        return slug
    import urllib.request
    req = urllib.request.Request(
        f"https://www.linkedin.com/company/{slug}",
        headers={"User-Agent": _LI_UA, "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            html = r.read().decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001  (network/parse — non-fatal; caller flags the miss)
        return ""
    return _linkedin_id_from_html(html)


def _top_card_org_ids(html: str) -> set:
    """The numeric ids the page names as ITS OWN organization, and only those: the
    `data-semaphore-content-urn="urn:li:organization:<id>"` of a tag in the page's top
    card (its tracking prefix or control name starts with `top-card`). Any other
    organization urn on the page (a feed post, an affiliate, a similar company, a
    showcase block) is somebody else's and is not read. Pure."""
    own: set = set()
    for m in _LI_TOP_CARD_ORG.finditer(html):
        # The tag the attribute sits in: from the nearest `<` before it to the first `>`
        # after it (attribute values on these tags hold no `<` or `>`). The attribute
        # must sit INSIDE one real tag: a `>` between that `<`
        # and the attribute means the tag was already closed and the text is page text
        # (a script string, say), not an attribute, so it does not count. Before, the
        # text of a script after a closed tag counted as that tag's attributes, and a
        # script could name another organization as the page's own.
        start = html.rfind("<", 0, m.start())
        if start < 0 or ">" in html[start:m.start()]:
            continue
        end = html.find(">", m.end())
        tag = html[start:end if end >= 0 else len(html)]
        if _LI_TOP_CARD_SCOPE.search(tag):
            own.add(m.group(1))
    return own


def _linkedin_id_from_html(html: str) -> str:
    """The numeric company id a public LinkedIn company page is ABOUT, or '' when
    the page does not settle it. Pure: no network, so it is tested on saved pages.

    Take the FIRST pattern (in trust order) that yields ids. A `f_C` value that is
    a list contributes EVERY id in it as a candidate, never only the first.
      * The page names its own organization in its top card (_top_card_org_ids):
        the answer is the one candidate that is also that organization. No candidate
        matching it means the page disagrees with itself; several matching means
        they cannot be told apart. Both return '' (the caller flags the miss and
        a human confirms).
      * The page names no organization in its top card: a list is ambiguous (which
        of its ids is this company?) and returns ''. Otherwise the earlier rule
        stands: one distinct id wins however often it appears (the markup may embed
        it once or many times), a strict plurality wins, a tie returns ''. The
        earlier "count >= 2" rule was wrong: it was calibrated against one page's
        incidental repetition and rejected the correct id when it appeared once.
    Never a guess: where the evidence conflicts and nothing settles it, ''. A page
    with one plain `f_C` id (HubSpot 68529) resolves as before, because its own
    organization is that same id."""
    from collections import Counter
    own = _top_card_org_ids(html)
    for pat in _LI_ID_PATTERNS:
        counts: Counter = Counter()
        has_list = False
        for value in re.findall(pat, html):
            ids = {i for i in re.split(_LI_ID_SEPARATOR, value) if i}
            has_list = has_list or len(ids) > 1
            counts.update(ids)
        if not counts:
            continue
        if own:
            settled = sorted(set(counts) & own)
            return settled[0] if len(settled) == 1 else ""
        if has_list:
            return ""  # a list and no own organization to pick from — do not guess
        ranked = counts.most_common()
        if len(ranked) == 1 or ranked[0][1] > ranked[1][1]:
            return ranked[0][0]
        return ""  # ambiguous within this pattern — do not guess
    return ""


def enrich_linkedin_ids(entries: list[dict]) -> list[dict]:
    """Fill entry['linkedin']['company_id'] for entries that resolved a LinkedIn
    company, by fetching the public company page. Network; called by the
    caller AFTER resolve_all (never by the pure rankers or the tests).
    A miss leaves company_id='' and the report/stage flag it — never a guess."""
    for e in entries:
        li = e.get("linkedin") or {}
        if not li.get("use"):
            continue
        slug = _linkedin_slug(e)
        li["company_id"] = fetch_linkedin_company_id(slug) if slug else ""
        e["linkedin"] = li
    return entries


def write_resolution(entries: list[dict], workdir: str | Path) -> Path:
    """Write the two files the downstream stages read:
      competitors/resolution.json — full entries (build_B4 reads FB pages,
                                     build_LI reads LinkedIn names, report reads all)
      competitors/domains.json    — de-duped list of chosen `use` domains, the
                                     existing input build_B5 already consumes.
    Returns the competitors/ directory path."""
    comp = Path(workdir) / "competitors"
    comp.mkdir(parents=True, exist_ok=True)
    (comp / "resolution.json").write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")

    domains: list[str] = []
    seen: set[str] = set()
    for e in entries:
        d = (e.get("domain") or {}).get("use") or ""
        if d and d not in seen:
            seen.add(d)
            domains.append(d)
    (comp / "domains.json").write_text(
        json.dumps(domains, ensure_ascii=False, indent=2), encoding="utf-8")
    return comp


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def pair_competitors(cfg: dict) -> list[tuple[str, str]]:
    """The competitors in the config as (identity, supplied_domain) pairs,
    order-preserving and de-duped case-insensitively.

    WHY (seen in a real UK run): competitor_names ["Revolut"] with
    competitor_domains ["revolut.com"] became TWO competitors: 6 searches
    instead of 3, and the Google stage searched two domains for one company.
    The same company given both ways is ONE competitor. A name and a domain
    are the same company when the name, stripped to letters and digits,
    equals the domain's first label stripped the same way ("Revolut" and
    revolut.com, "Acme Plumbing" and acmeplumbing.com, "Coca-Cola" and
    coca-cola.com). The pair keeps the NAME as the identity (a name search
    finds the Facebook page and LinkedIn company better) and carries the
    domain as the supplied domain, checked against the resolved one.

    Not paired by list position: a names list and a domains list of the same
    length may name different companies, and merging those would drop a
    competitor without a word. A name and domain that do not match this way
    stay two competitors, as before; the resolution report shows both."""
    raws: list[str] = []
    seen: set[str] = set()
    for raw in list(cfg.get("competitor_names") or []) + list(cfg.get("competitor_domains") or []):
        key = (raw or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            raws.append(raw.strip())
    classes = [classify_input(r) for r in raws]
    paired: dict[int, int] = {}      # name index -> domain index
    taken: set[int] = set()          # domain indexes already paired
    for i, ci in enumerate(classes):
        if ci["kind"] != "name" or not _squash(ci["name"]):
            continue
        for j, cj in enumerate(classes):
            if (cj["kind"] == "domain" and j not in taken and cj["domain"]
                    and _squash(cj["domain"].split(".")[0]) == _squash(ci["name"])):
                paired[i] = j
                taken.add(j)
                break
    return [(r, classes[paired[i]]["domain"] if i in paired else "")
            for i, r in enumerate(raws) if i not in taken]


def build_identities(cfg: dict) -> list[str]:
    """The unified competitor identity list from config, order-preserving and
    de-duped. Names and domains are BOTH accepted (there is no separate 'mode' —
    every supplied competitor, however typed, is resolved the same way). A
    company given both as a name and as its domain is one identity (the name);
    see pair_competitors."""
    return [ident for ident, _ in pair_competitors(cfg)]


def supplied_domains(cfg: dict) -> dict[str, str]:
    """{name identity: the domain also supplied for it} (see pair_competitors)."""
    return {ident: dom for ident, dom in pair_competitors(cfg) if dom}


def check_resolution_match(identities: list, stored_entries) -> dict:
    """Compare the current competitor identities (from the config) with the
    'input' values in a stored resolution.json.

    Returns:
      {'match': True,  'added': [], 'removed': [], 'notice': ''}
          — current identities and stored inputs are the same set.
      {'match': False, 'added': [...], 'removed': [...], 'notice': '<plain text>'}
          — they differ; 'notice' names added/removed and says re-resolution
            will be shown for confirmation.

    WHY set comparison:
      The order of competitors in the config does not change what is scraped.
      Two runs with the same competitors in a different order ask the same question.

    WHY 'input' must be present in every stored entry:
      An entry without 'input' was written by an older pipeline version that did not
      record the original competitor string.  We cannot compare it to the current
      config, so we treat the file as not matching and trigger re-resolution.
      A guess here is exactly how the wrong target gets scraped silently — the class
      of failure this check exists to prevent.
    """
    if not isinstance(stored_entries, list):
        return {
            "match": False, "added": list(identities), "removed": [],
            "notice": (
                "Stored resolution file is not a list, so all competitors will be "
                "looked up again and shown for confirmation."
            ),
        }

    missing = [e for e in stored_entries
               if not isinstance(e, dict) or "input" not in e]
    if missing:
        n = len(missing)
        return {
            "match": False, "added": list(identities), "removed": [],
            "notice": (
                f"Stored resolution file has {n} "
                f"{'entries' if n != 1 else 'entry'} without an 'input' field "
                f"(older file format), so all competitors will be looked up again "
                f"and shown for confirmation."
            ),
        }

    current = {i.strip().lower() for i in identities}
    stored  = {e["input"].strip().lower() for e in stored_entries}

    if current == stored:
        return {"match": True, "added": [], "removed": [], "notice": ""}

    added   = [i for i in identities if i.strip().lower() not in stored]
    removed = [e["input"] for e in stored_entries
               if e["input"].strip().lower() not in current]

    parts = []
    if added:
        parts.append("added: " + ", ".join(added))
    if removed:
        parts.append("removed: " + ", ".join(removed))
    notice = (
        "Competitor list changed (" + "; ".join(parts) + "). "
        "The new list will be looked up and shown for confirmation before anything is scraped."
    )
    return {"match": False, "added": added, "removed": removed, "notice": notice}


# ---------------------------------------------------------------------------
# Inline test matrix — DIVERSE archetypes, not tuned to any one company.
# Run: python3 resolve.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    PASS = FAIL = 0

    def check(label, got, expected):
        global PASS, FAIL
        ok = got == expected
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            print(f"        got:      {got!r}\n        expected: {expected!r}")
        PASS, FAIL = PASS + (1 if ok else 0), FAIL + (0 if ok else 1)

    def truthy(label, cond):
        global PASS, FAIL
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")
        PASS, FAIL = PASS + (1 if cond else 0), FAIL + (0 if cond else 1)

    def organic(*rows):
        return [{"title": t, "url": u, "displayedUrl": d, "position": i + 1}
                for i, (t, u, d) in enumerate(rows)]

    G = "https://www.google.com/goto?url=BLOB=="  # opaque redirect (real SERP shape)

    # --- normalize_domain: general, no character surgery -------------------
    print("\n=== normalize_domain (no hyphen/char surgery) ===")
    check("strip scheme+www+path", normalize_domain("https://www.AcmePlumbing.com/x"), "acmeplumbing.com")
    check("hyphen PRESERVED", normalize_domain("Best-Drains.COM"), "best-drains.com")
    check("bare passthrough", normalize_domain("coca-cola.com"), "coca-cola.com")

    # --- classify_input: name vs domain -----------------------------------
    print("\n=== classify_input ===")
    check("multi-word NAME is a name, not a domain", classify_input("Acme Plumbing")["kind"], "name")
    check("dotted string is a domain", classify_input("acmeplumbing.com")["kind"], "domain")
    check("URL is a domain", classify_input("https://acme.io/pricing")["kind"], "domain")
    check("multi-word name", classify_input("Ben & Jerry's")["kind"], "name")

    # --- CASE 1: franchise B2C with regional FB pages ---------------------
    # General property under test: the BARE-BRAND page outranks regional pages,
    # via the extra-token penalty — no company-specific rule.
    print("\n=== CASE 1: franchise w/ regional pages -> corporate page wins ===")
    o1 = organic(
        ("BrandCo (@BrandCo)", G, "12K followers"),                       # junk
        ("BrandCo | Official Site", G, "https://www.brandco.com"),         # domain
        ("BrandCo | Denver CO | Facebook", G, "https://www.facebook.com/BrandCoDenverCO"),
        ("BrandCo | Facebook", G, "https://www.facebook.com/BrandCo"),     # corporate
        ("BrandCo Services LLC | LinkedIn", G, "https://www.linkedin.com/company/brandco-services"),
        ("BrandCo Reviews", G, "https://www.trustpilot.com › review › brandco.com"),  # directory
    )
    e1 = resolve_competitor("BrandCo", o1)
    check("C1 domain resolved (directory skipped)", e1["domain"]["resolved"], "brandco.com")
    check("C1 corporate FB page wins over regional", e1["facebook"]["use"], "https://www.facebook.com/BrandCo")
    truthy("C1 regional page present as alternate",
           any("Denver" in c["slug"] for c in e1["facebook"]["candidates"]))
    check("C1 LinkedIn full name from title", e1["linkedin"]["use"], "BrandCo Services LLC")

    # --- CASE 2: supplied domain MISMATCHES resolved (a typo or redirect case) --
    # No auto-fix: the mismatch is flagged; the evidence-backed domain is the default.
    print("\n=== CASE 2: supplied domain != resolved -> flagged, not auto-fixed ===")
    o2 = organic(
        ("Widgetize | Home", G, "https://www.widgetize.com"),
        ("Widgetize | Facebook", G, "https://www.facebook.com/Widgetize"),
    )
    e2 = resolve_competitor("widgetise.com", o2)   # user typed the WRONG domain
    check("C2 agreement is mismatch", e2["domain"]["agreement"], "mismatch")
    truthy("C2 mismatch is flagged", any("MISMATCH" in f or "mismatch" in f for f in e2["flags"]))
    check("C2 default uses evidence-backed resolved domain", e2["domain"]["use"], "widgetize.com")
    check("C2 supplied value preserved for the human", e2["domain"]["supplied"], "widgetise.com")

    # --- CASE 3: supplied domain MATCHES -> clean, no flag ----------------
    print("\n=== CASE 3: supplied domain matches resolved ===")
    o3 = organic(("Acme | Site", G, "https://www.acme.io"),
                 ("Acme | LinkedIn", G, "https://linkedin.com/company/acme"))
    e3 = resolve_competitor("acme.io", o3)
    check("C3 agreement is match", e3["domain"]["agreement"], "match")
    check("C3 uses the supplied (confirmed) domain", e3["domain"]["use"], "acme.io")

    # --- CASE 4: legitimately hyphenated brand keeps its hyphen -----------
    print("\n=== CASE 4: legit hyphenated brand (no blanket strip) ===")
    o4 = organic(("Coca-Cola | Official", G, "https://www.coca-cola.com"),
                 ("Coca-Cola | Facebook", G, "https://www.facebook.com/CocaColaUnitedStates"))
    e4 = resolve_competitor("Coca-Cola", o4)
    check("C4 hyphen preserved in resolved domain", e4["domain"]["resolved"], "coca-cola.com")

    # --- CASE 5: no FB page and no LinkedIn -> honest flags, no fake pick --
    print("\n=== CASE 5: missing targets are flagged, never fabricated ===")
    o5 = organic(("Local Plumb Co", G, "https://www.localplumbco.com"))
    e5 = resolve_competitor("Local Plumb Co", o5)
    check("C5 no FB page -> empty use", e5["facebook"]["use"], "")
    check("C5 no LinkedIn -> empty use", e5["linkedin"]["use"], "")
    truthy("C5 both misses are flagged",
           any("Facebook" in f for f in e5["flags"]) and any("LinkedIn" in f for f in e5["flags"]))

    # --- CASE 6: all organic are directories -> no domain, flagged --------
    print("\n=== CASE 6: directory-only SERP -> unresolved domain flagged ===")
    o6 = organic(("X on Yelp", G, "https://www.yelp.com › biz › x"),
                 ("X on Angi", G, "https://www.angi.com › x"))
    e6 = resolve_competitor("Some Brand", o6)
    check("C6 no domain resolved", e6["domain"]["resolved"], "")
    truthy("C6 unresolved-domain flagged", any("Google" in f for f in e6["flags"]))

    # --- CASE 7: ampersand name tokenizes correctly -----------------------
    print("\n=== CASE 7: ampersand/punctuation name ===")
    o7 = organic(("Ben & Jerry's | Facebook", G, "https://www.facebook.com/benjerry"),
                 ("Ben & Jerry's", G, "https://www.benjerry.com"))
    e7 = resolve_competitor("Ben & Jerry's", o7)
    check("C7 FB page found despite punctuation", e7["facebook"]["use"], "https://www.facebook.com/benjerry")

    # --- CASE 8: ad-library / group FB links are NOT treated as pages ------
    print("\n=== CASE 8: FB non-page paths rejected ===")
    o8 = organic(
        ("Ads about Foo", G, "https://www.facebook.com/ads/library/?q=foo"),
        ("Foo group", G, "https://www.facebook.com/groups/12345"),
        ("Foo | Facebook", G, "https://www.facebook.com/FooOfficial"),
    )
    e8 = resolve_competitor("Foo", o8)
    check("C8 ad-library/group skipped; real page chosen",
          e8["facebook"]["use"], "https://www.facebook.com/FooOfficial")

    # --- CASE 9: end-to-end disk bridge (build_identities/resolve_all/write) --
    # Exercises the real integration seam: a B1 dataset on disk (the exact shape
    # resolver.load_b1_records reads) -> resolve_all -> write_resolution -> the
    # files build_B4/B5/LI consume. Mixed name+domain input, one unmatched query.
    print("\n=== CASE 9: disk bridge build_identities -> resolve_all -> write ===")
    import tempfile as _tf
    import shutil as _sh

    def _b1(term, *rows):
        return {"searchQuery": {"term": term, "page": 1},
                "organicResults": [{"title": t, "url": G, "displayedUrl": d, "position": i + 1}
                                   for i, (t, d) in enumerate(rows)]}

    _cfg = {"competitor_names": ["Acme Plumbing", "Acme Plumbing"],  # dup collapses
            "competitor_domains": ["Best-Drains.com"]}           # domain kept, hyphen intact
    ids = build_identities(_cfg)
    check("C9 identities de-duped + combined", ids, ["Acme Plumbing", "Best-Drains.com"])

    _wd = _tf.mkdtemp(prefix="resolve_all_test_")
    try:
        b1dir = Path(_wd) / "B1_serp_competitors"
        b1dir.mkdir(parents=True)
        dataset = [
            _b1("Acme Plumbing",
                ("Acme Plumbing | Plumbing & Drains", "https://www.acmeplumbing.com"),
                ("Acme Plumbing | Facebook", "https://www.facebook.com/AcmePlumbing"),
                ("Acme Plumbing Drain Service | LinkedIn",
                 "https://www.linkedin.com/company/acme-plumbing")),
            _b1("best-drains.com",  # NOTE: query for a domain identity is the domain string
                ("Best Drains Plumbing", "https://www.bestdrains.com"),
                ("Best Drains | Facebook", "https://www.facebook.com/BestDrainsPlumbing")),
        ]
        (b1dir / "run_02_full.json").write_text(json.dumps(dataset), encoding="utf-8")

        entries = resolve_all(ids, _wd)
        check("C9 one entry per identity", len(entries), 2)
        check("C9 name->domain resolved", entries[0]["domain"]["use"], "acmeplumbing.com")
        check("C9 name->FB page resolved", entries[0]["facebook"]["use"],
              "https://www.facebook.com/AcmePlumbing")
        check("C9 name->full LinkedIn name", entries[0]["linkedin"]["use"],
              "Acme Plumbing Drain Service")
        # Supplied domain best-drains.com vs resolved bestdrains.com is a REAL mismatch
        # (a hyphen difference) — surfaced, never auto-corrected.
        check("C9 supplied-domain hyphen mismatch flagged",
              entries[1]["domain"]["agreement"], "mismatch")

        comp = write_resolution(entries, _wd)
        wrote_domains = json.loads((comp / "domains.json").read_text())
        wrote_res = json.loads((comp / "resolution.json").read_text())
        check("C9 domains.json = chosen use-domains", wrote_domains,
              ["acmeplumbing.com", "bestdrains.com"])
        check("C9 resolution.json has both entries", len(wrote_res), 2)

        # Unmatched identity: a query with no B1 record resolves to all-empty+flagged,
        # never dropped.
        orphan = resolve_all(["Nonexistent Brand"], _wd)
        check("C9 orphan still returns an entry", len(orphan), 1)
        truthy("C9 orphan flagged, not silently empty", bool(orphan[0]["flags"]))
    finally:
        _sh.rmtree(_wd, ignore_errors=True)

    # --- CASE 10: targeted site:facebook.com query surfaces a Page the bare
    # query missed (the real HubSpot symptom: bare SERP returns the FB profile
    # only as a "17.4L+ followers" blob with no facebook.com URL) --------------
    print("\n=== CASE 10: FB Page found ONLY via the site:facebook.com query ===")
    check("query plan = bare + fb + li per identity",
          build_query_plan(["Acme"])[0],
          ["Acme", "Acme site:facebook.com", "Acme site:linkedin.com"])
    _wd2 = _tf.mkdtemp(prefix="resolve_fb_test_")
    try:
        b1 = Path(_wd2) / "B1_serp_competitors"
        b1.mkdir(parents=True)
        ds = [
            # bare query: domain resolves; FB present ONLY as a follower blob (junk)
            _b1("Acme",
                ("Acme | Official", "https://www.acme.com"),
                ("Acme", "312K+ followers")),
            # site:facebook.com query: the clean Page URL Google returns when scoped
            _b1("Acme site:facebook.com",
                ("Acme | Facebook", "https://www.facebook.com/AcmeOfficial")),
        ]
        (b1 / "run_02_full.json").write_text(json.dumps(ds), encoding="utf-8")
        e = resolve_all(["Acme"], _wd2)[0]
        check("C10 domain still resolves from bare query", e["domain"]["use"], "acme.com")
        check("C10 FB Page resolved from the targeted query",
              e["facebook"]["use"], "https://www.facebook.com/AcmeOfficial")
        truthy("C10 no spurious FB flag once the Page is found",
               not any("Facebook" in f for f in e["flags"]))
    finally:
        _sh.rmtree(_wd2, ignore_errors=True)

    # --- CASE 11: a site:linkedin.com query must NOT leak linkedin subdomains
    # into the company domain (the real HubSpot bug: my.linkedin.com won). -----
    print("\n=== CASE 11: linkedin.com SUBDOMAINS are not company domains ===")
    truthy("my.linkedin.com is a directory (subdomain of a skip domain)",
           _is_directory("my.linkedin.com"))
    truthy("business.linkedin.com is a directory", _is_directory("business.linkedin.com"))
    truthy("real company domain is NOT a directory", not _is_directory("hubspot.com"))
    _wd3 = _tf.mkdtemp(prefix="resolve_sub_test_")
    try:
        b1 = Path(_wd3) / "B1_serp_competitors"
        b1.mkdir(parents=True)
        ds = [
            _b1("Widgetco", ("Widgetco | Home", "https://www.widgetco.com")),
            # site:linkedin.com results: brand in the title scores high, but the
            # host is a linkedin subdomain — must be skipped for domain ranking.
            _b1("Widgetco site:linkedin.com",
                ("Widgetco | LinkedIn", "https://my.linkedin.com"),
                ("Widgetco Careers | LinkedIn", "https://business.linkedin.com")),
        ]
        (b1 / "run_02_full.json").write_text(json.dumps(ds), encoding="utf-8")
        e = resolve_all(["Widgetco"], _wd3)[0]
        check("C11 domain is the real site, not a linkedin subdomain",
              e["domain"]["use"], "widgetco.com")
    finally:
        _sh.rmtree(_wd3, ignore_errors=True)

    # --- report smoke -----------------------------------------------------
    print("\n=== report renders ===")
    rep = format_resolution_report([e1, e2, e5])
    truthy("report shows MISMATCH warning", "MISMATCH" in rep)
    truthy("report shows a resolved domain", "brandco.com" in rep)

    # --- CASE 12: check_resolution_match ----------------------------------
    print("\n=== CASE 12: check_resolution_match ===")
    _stored_ok = [{"input": "acme-plumbing.com"}, {"input": "bestdrains.com"}]
    _stored_missing = [{"input": "acme-plumbing.com"}, {"domain": {"use": "x.com"}}]

    # Exact match
    m = check_resolution_match(["acme-plumbing.com", "bestdrains.com"], _stored_ok)
    check("C12 exact match -> match=True", m["match"], True)
    check("C12 exact match -> added empty", m["added"], [])
    check("C12 exact match -> removed empty", m["removed"], [])

    # Order-insensitive match
    m2 = check_resolution_match(["bestdrains.com", "acme-plumbing.com"], _stored_ok)
    check("C12 reversed order -> still match", m2["match"], True)

    # Case-insensitive match
    m3 = check_resolution_match(["Acme-Plumbing.com", "BestDrains.com"], _stored_ok)
    check("C12 different case -> still match", m3["match"], True)

    # Added competitor
    m4 = check_resolution_match(
        ["acme-plumbing.com", "bestdrains.com", "terminix.com"], _stored_ok
    )
    check("C12 added -> match=False", m4["match"], False)
    check("C12 added -> added list", m4["added"], ["terminix.com"])
    check("C12 added -> removed empty", m4["removed"], [])
    truthy("C12 added -> notice mentions competitor", "terminix.com" in m4["notice"])

    # Removed competitor
    m5 = check_resolution_match(["acme-plumbing.com"], _stored_ok)
    check("C12 removed -> match=False", m5["match"], False)
    check("C12 removed -> added empty", m5["added"], [])
    check("C12 removed -> removed list", m5["removed"], ["bestdrains.com"])
    truthy("C12 removed -> notice mentions competitor", "bestdrains.com" in m5["notice"])

    # Swapped competitor (added + removed simultaneously)
    m6 = check_resolution_match(["acme-plumbing.com", "terminix.com"], _stored_ok)
    check("C12 swapped -> match=False", m6["match"], False)
    check("C12 swapped -> added", m6["added"], ["terminix.com"])
    check("C12 swapped -> removed", m6["removed"], ["bestdrains.com"])

    # Missing 'input' field in stored entry
    m7 = check_resolution_match(["acme-plumbing.com"], _stored_missing)
    check("C12 missing input -> match=False", m7["match"], False)
    truthy("C12 missing input -> notice mentions older format",
           "older file format" in m7["notice"])

    # Not a list
    m8 = check_resolution_match(["acme-plumbing.com"], {"input": "x"})
    check("C12 non-list stored -> match=False", m8["match"], False)

    # --- CASE 13: a page must be TIED to the competitor ----------------------
    # WHY: in a real paid run, Basecamp (no Facebook page) got
    # facebook.com/BFMTV, a French TV channel, because Google filled the
    # site:facebook.com query with unrelated pages and any page was accepted.
    # Rows below are the saved real SERP rows (diag_fbpage/b1_records_dump.txt).
    print("\n=== CASE 13: unrelated FB/LinkedIn pages are rejected ===")
    bc_fb = [
        ("Meta for Developers: Social technologies", "https://developers.facebook.com/"),
        ("R&F Mall Johor Bahru", "https://www.facebook.com/RnFMallJB/"),
        ("Faseqiatra (@qiatra)", "https://www.facebook.com/qiatra/"),
        ("החדשות - N12 | Neve Ilan", "https://www.facebook.com/N12News/"),
        ("bfmtv", "https://www.facebook.com/BFMTV/"),
        ("Susan Boyle", "https://www.facebook.com/susanboyle/"),
        ("Barbra Streisand (@barbrastreisand)", "https://www.facebook.com/barbrastreisand/"),
        ("Andes Property Inspections | Cuenca", "https://www.facebook.com/mackpropertyinspections/"),
        ("Camping Piscina Natural Sao Jose Da Serra",
         "https://www.facebook.com/campingpiscinanaturalsaojosedaserra/"),
        ("Клинкер.Работа в Бельгии. (@clinkerbelgia)", "https://www.facebook.com/clinkerbelgia/"),
    ]
    o13 = [{"title": t, "url": u, "displayedUrl": "https://www.facebook.com › x", "position": i + 1}
           for i, (t, u) in enumerate(bc_fb)]
    o13.insert(0, {"title": "Basecamp", "url": "https://basecamp.com/",
                   "displayedUrl": "https://basecamp.com", "position": 0})
    e13 = resolve_competitor("basecamp.com", o13)
    check("C13 Basecamp: no unrelated FB page used", e13["facebook"]["use"], "")
    check("C13 Basecamp: no FB candidates survive", e13["facebook"]["candidates"], [])
    truthy("C13 Basecamp: 'no Facebook Page found' flag raised",
           any(f.startswith("no Facebook Page found") for f in e13["flags"]))
    o13h = [{"title": "HubSpot | 2,1M+ followers", "url": "https://www.facebook.com/hubspot/",
             "displayedUrl": "https://www.facebook.com › hubspot", "position": 1},
            {"title": "HubSpot Academy | Cambridge MA", "url": "https://www.facebook.com/thehubspotacademy/",
             "displayedUrl": "https://www.facebook.com › thehubspotacademy", "position": 2},
            {"title": "bfmtv", "url": "https://www.facebook.com/BFMTV/",
             "displayedUrl": "https://www.facebook.com › x", "position": 3}]
    e13h = resolve_competitor("hubspot.com", o13h)
    check("C13 HubSpot still resolves to its own page",
          e13h["facebook"]["use"], "https://www.facebook.com/hubspot")
    truthy("C13 HubSpot: unrelated page not even an alternate",
           all(c["slug"].lower() != "bfmtv" for c in e13h["facebook"]["candidates"]))
    o13l = [{"title": "Andes Property Inspections | LinkedIn",
             "url": "https://www.linkedin.com/company/andes-property-inspections",
             "displayedUrl": "https://www.linkedin.com › company", "position": 1}]
    e13l = resolve_competitor("basecamp.com", o13l)
    check("C13 unrelated LinkedIn company rejected", e13l["linkedin"]["use"], "")
    truthy("C13 unrelated LinkedIn company -> LinkedIn flag",
           any(f.startswith("no LinkedIn company found") for f in e13l["flags"]))

    # --- CASE 14: a brand word in a POST title is not the page's identity ---
    # WHY: a real Basecamp-only paid run proposed
    # facebook.com/ClutchPointsFootball at 0.7 because a post there links to
    # basecamp.com; the real page basecamphq sat at 0.45. Saved real rows from a paid run's
    # B1_serp_competitors.
    print("\n=== CASE 14: post titles naming the brand do not tie a page ===")
    o14 = [{"title": t, "url": u, "displayedUrl": d, "position": i + 1} for i, (t, u, d) in enumerate([
        ("Done brainstorming? Level up that idea with basecamp and by ...",
         "https://www.facebook.com/navigate.smallbusiness/posts/done-brainstorming", "3 reactions"),
        ("Air Force BASECAMP | Dhaka", "https://www.facebook.com/airforcebasecamp/", "60.8K+ followers"),
        ("Basecamp (@basecamphq)", "https://www.facebook.com/basecamphq/", "90+ followers"),
        ("https://3.basecamp.com/5767866/buckets/47021255/ ...",
         "https://www.facebook.com/ClutchPointsFootball/posts/7215ee9c/1076827491601735/", "20+ reactions"),
        ("Visit this link : https://public.3.basecamp.com/p/ ...",
         "https://www.facebook.com/vanuatuqualificationsauthority/posts/visit-this-link-/1", "40+ reactions"),
        ("Patagonian BaseCamp Lodge (@pbclodgechile)", "https://www.facebook.com/pbclodgechile/", "1.5K+ followers"),
    ])]
    e14 = resolve_competitor("basecamp.com", o14)
    slugs14 = [c["slug"] for c in e14["facebook"]["candidates"]]
    truthy("C14 ClutchPointsFootball rejected", "ClutchPointsFootball" not in slugs14)
    truthy("C14 vanuatuqualificationsauthority rejected", "vanuatuqualificationsauthority" not in slugs14)
    check("C14 the real page basecamphq is proposed",
          e14["facebook"]["use"], "https://www.facebook.com/basecamphq")

    # --- CASE 15: a page scoring at or below zero is never used ----
    # WHY: in a real run, BFMTV was proposed for Basecamp at -0.05. A page
    # can carry the brand in its address and still score below zero; that is
    # no evidence it is the competitor's page.
    print("\n=== CASE 15: a page with a score at or below zero is never used ===")
    o15 = [{"title": "Base Camp Kathmandu Trekking and Expedition Pvt Ltd Nepal Himalaya Tours",
            "url": "https://www.facebook.com/basecamptrek/", "displayedUrl": "2K+ followers",
            "position": 1}]
    e15 = resolve_competitor("basecamp.com", o15)
    check("C15 negative-score page not used", e15["facebook"]["use"], "")
    check("C15 negative-score page not even offered", e15["facebook"]["candidates"], [])
    truthy("C15 'no Facebook Page found' flag raised",
           any(f.startswith("no Facebook Page found") for f in e15["flags"]))
    rep15 = format_resolution_report([e15], [])
    truthy("C15 report says Meta is not searched for it",
           "(none): NOT searched on Meta" in rep15)

    # --- CASE 16: namesakes are offered, never used on their own ----
    print("\n=== CASE 16: only a page named exactly as the competitor is used ===")
    o16 = [r for r in o14 if "basecamphq" not in r["url"]]
    e16 = resolve_competitor("basecamp.com", o16)
    check("C16 namesake pages not used when the real page is missing", e16["facebook"]["use"], "")
    check("C16 namesakes kept as possible pages",
          [c["slug"] for c in e16["facebook"]["candidates"]], ["pbclodgechile", "airforcebasecamp"])
    rep16 = format_resolution_report([e16], [])
    truthy("C16 report offers them as possible pages, not used",
           "possible pages, not used" in rep16 and "pbclodgechile" in rep16)
    # False-positive check: the real pages are named exactly and still used.
    o16r = [{"title": "Acme Plumbing & Water Cleanup | Facebook",
             "url": "https://www.facebook.com/AcmePlumbing/", "displayedUrl": "x", "position": 1}]
    check("C16 Acme Plumbing page (by its address) still used",
          resolve_competitor("acme-plumbing.com", o16r)["facebook"]["use"],
          "https://www.facebook.com/AcmePlumbing")
    check("C16 HubSpot page whose title is a post still used (by its address)",
          resolve_competitor("hubspot.com", [{
              "title": "Product updates? noted ✅ See more at www.hubspot.com/spotlight",
              "url": "https://www.facebook.com/hubspot/", "displayedUrl": "x",
              "position": 1}])["facebook"]["use"], "https://www.facebook.com/hubspot")
    check("C16 Revolut page by name still used",
          resolve_competitor("Revolut", [{"title": "Revolut",
                                          "url": "https://www.facebook.com/revolut/?locale=sv_SE",
                                          "displayedUrl": "596.4K+ followers", "position": 5}]
                             )["facebook"]["use"], "https://www.facebook.com/revolut")

    # --- CASE 17: whose Google ads are these -----------------------
    # Rows shaped as the real lexis-solutions/google-ads-scraper records of two paid runs
    # (advertiserName/advertiserId/originUrl).
    print("\n=== CASE 17: Google ads by other advertisers are set aside ===")

    def gad(dom, name, aid):
        return {"advertiserName": name, "advertiserId": aid, "creativeId": "CR" + aid,
                "originUrl": f"https://adstransparency.google.com/?region=GB&domain={dom}"}
    g = ([gad("revolut.com", "Revolut Ltd", "AR1")] * 3
         + [gad("revolut.com", "IHG Hotels & Resorts", "AR2")]
         + [gad("hubspot.com", "Hubspot, Inc.", "AR3")] * 2
         + [gad("hubspot.com", "Cardone Training Technologies", "AR4"),
            gad("hubspot.com", "Redial BPO", "AR5"),
            gad("basecamp.com", "37signals LLC", "AR6"),
            gad("bestdrains.com", "M J Services Inc DBA Best Drains Plumbing", "AR7"),
            gad("bestdrains.com", "Synth Service 0", "AR7"),          # same advertiser id
            gad("revolut.com", "Revolution Bars Group", "AR8"),
            {"originUrl": "https://adstransparency.google.com/?region=GB&domain=revolut.com"}])
    ents = [{"input": "Revolut", "kind": "name", "domain": {"use": "revolut.com"}},
            {"input": "hubspot.com", "kind": "domain", "domain": {"use": "hubspot.com"}},
            {"input": "basecamp.com", "kind": "domain", "domain": {"use": "basecamp.com"}},
            {"input": "bestdrains.com", "kind": "domain", "domain": {"use": "bestdrains.com"}}]
    v17 = google_ad_owners(g, ents)
    check("C17 verdicts", v17, ["own"] * 3 + ["other"] + ["own"] * 2
          + ["other", "other", "other", "own", "own", "other", "unnamed"])
    l17 = google_set_aside_lines(g, ents, v17)
    truthy("C17 Revolut line counts and names IHG",
           any(s.startswith("2 of 6 Google ads found for revolut.com came from advertisers "
                            "whose name does not match Revolut") and "IHG Hotels & Resorts (1)" in s
               for s in l17))
    truthy("C17 the record with no advertiser is named as such",
           any(s.startswith("1 of 6 Google ads found for revolut.com carry no advertiser name")
               for s in l17))
    truthy("C17 HubSpot line", any(s.startswith("2 of 4 Google ads found for hubspot.com") for s in l17))
    truthy("C17 Basecamp's 37signals ads are set aside and named, never guessed",
           any("37signals LLC (1)" in s for s in l17))
    truthy("C17 no line for a website with nothing set aside",
           not any("bestdrains.com" in s for s in l17))
    truthy("C17 no em-dash in the lines", not any("—" in s for s in l17))
    ents[2]["domain"]["google_advertisers"] = ["37signals LLC"]
    check("C17 a name the user lists counts as the competitor's",
          google_ad_owners(g, ents)[9], "own")

    print(f"\n{'='*50}\nResults: {PASS} passed, {FAIL} failed")
    if FAIL:
        sys.exit(1)
