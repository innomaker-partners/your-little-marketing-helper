"""
resolve.py — general, evidence-first competitor resolver for ad-intelligence.

Turns ANY competitor input (a company name, a domain, a URL, in any case) into
ranked, evidence-carrying candidate identifiers for each ad source:

  - a canonical DOMAIN            -> Google Ads Transparency (B5)
  - a Facebook PAGE url           -> Meta Ad Library by-advertiser (B4)
  - a LinkedIn COMPANY name       -> LinkedIn Ad Library (LI)

Design principles (frozen — do not weaken without a deliberate decision):
  * Propose ranked candidates with evidence; NEVER silently "fix" a supplied
    value. A human confirms at the resolution-report gate before any spend. A
    website the user typed is used as typed (see resolve_competitor for why) and
    the search's best match is shown beside it; a company given by name alone has
    only the search to go on.
  * No blanket transforms. A supplied domain is COMPARED to the resolved one and
    the difference is SURFACED — hyphens/format are never auto-stripped (plenty of
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
from stages import meta_words, site_host  # noqa: E402  (the one reading of the search words; the one reading of a typed website)


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
    if not raw or not isinstance(raw, str):
        return ""      # a corrupt file can hold a list or a number where text belongs
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
    if ("://" in s or s.lower().startswith("www.")
            or (_looks_like_bare_domain(s) and not _whole_entry_site_refused(s))):
        return {"kind": "domain", "name": "", "domain": normalize_domain(s)}
    return {"kind": "name", "name": s, "domain": ""}


def _whole_entry_site_refused(s: str) -> bool:
    """True when the entry `s` is one dotted word that looks like a website but whose
    name part (the label left of the ending) is a company form, a generic word or
    shorter than three characters: "Inc.Co", "Ltd.co", "Pty.Ltd", "co.uk", "Est.io".

    WHY the same rule as for a bracket (_judge_token): as a whole entry such a word
    was searched on Google as a website, and its ads (a stranger's, or none) were
    shown under the buyer's competitor with no sign anything was wrong. It is far
    more often a company form written with a dot than a website. It is then read as a
    NAME and said so (_read_entry). A platform address is not refused here: it keeps its own
    notice. "www." or "https://" in front always makes it a website.
    WHY the case guard is NOT applied to a whole entry (it is, in a bracket): a
    bracket follows a name, so "Name (St.Louis)" has a reading that is not a website
    and mixed case is the sign of it. A whole entry has no other reading, and
    "AcmePlumbing.com" or "Acme.Co.Uk" typed alone is a website; sending it to a
    name search, whose best match can be a namesake, is the wrong-target risk the
    typed website exists to avoid. The name-part rule needs no case: a company form is
    refused whatever its case."""
    s = (s or "").strip()
    if not s or " " in s or "://" in s or s.lower().startswith("www."):
        return False
    if not _looks_like_bare_domain(s):
        return False
    host = normalize_domain(s)
    return bool(host) and not _platform_label(host) and not _name_part_usable(host)


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


def rank_facebook_pages(organic: list, name: str, site: str = "") -> list[dict]:
    """Rank facebook.com advertiser-Page candidates. Skips non-page FB paths.
    Regional/location pages sink via the extra-token penalty. General.

    site: the website the user gave together with a company NAME (see
    pair_competitors). A page is then also a candidate, and scored, by the
    website's name, because many small companies title their page with their
    website ("Acme.com (@handle)") and share no word with their registered
    name. This only widens who can be OFFERED; which page is USED is still
    decided by _page_named_as, so a namesake is not used because of it."""
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
        shown = title if is_page else ""
        if not (_tied(name, shown, slug) or (site and _tied(site, shown, slug))):
            continue  # no evidence this page belongs to the competitor
        text = f"{title} {slug.replace('.', ' ')}"
        score = _name_score(name, text)
        if site:
            score = max(score, _name_score(site, text))
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


def _page_named_as(identity: str, cand: dict, site: str = "") -> bool:
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
    the text before " | ", " - ", " · " or "(@handle)".

    The website counts when written in full ("Acme.com", "acme.com"): a small
    company often names its page by its website, and in a real run a page found
    as a candidate was not used because "Acme.com (@handle)" squashes to
    "acmecom", which is not the bare label "acme". The full website is as
    specific as the bare label, so this loosens nothing for a namesake.

    Keys by input:
      a website typed alone     the label and the website in full;
      a name (with or without   the name and, when the user gave one, the
      a website the user gave)  website in full. NOT the bare label: in a real
                                buyer's run the label matched the address of a
                                club with the same initials. When the user has
                                told us the real name, the bare label is the
                                weaker evidence. (A name that spells the label,
                                like Acme Plumbing and acmeplumbing.com, still
                                has it as its own name.)
    Namesakes cannot be told apart by score (a "Basecamp Lodge" page scores
    0.95 against "basecamp"), so exact naming stays the only gate."""
    cls = classify_input(identity)
    # `sites` are websites kept WITH their dots and compared that way. WHY: squashed
    # ("acmesacom"), "Acme S.A. Com" and "Acmesa-com" read as the website acmesa.com,
    # and a page named like that is as often a namesake as the company. The
    # website counts only as written: lower case, spaces removed, dots kept.
    if cls["kind"] == "domain":
        keys = {_squash_words(_registrable_label(cls["domain"]))}
        sites = {cls["domain"]}
    else:
        keys = {_squash_words(cls["name"])}
        sites = {normalize_domain(site)} if site else set()
    # A generic or very short word is no evidence of whose page this is (the same
    # refusal as the Google owner keys, _GENERIC_KEYS); the full website, dots
    # kept, still is.
    keys = {k for k in keys if _usable_key(k)}
    sites.discard("")
    if not keys and not sites:
        return False
    slug = cand.get("slug") or ""
    # An ADDRESS has no spaces to mistake: facebook.com/acmesacom is a page address
    # the company chose for acmesa.com, so the address may drop the dot.
    if _squash_words(slug) in keys | {_squash_words(x) for x in sites} or slug.lower() in sites:
        return True
    # The page name is the text before the first separator. A title that STARTS
    # with a separator ("|Acme.com | Facebook") has an empty first piece, so
    # leading separators are dropped first.
    title = re.sub(r"^[\s|·\-–—]+", "", cand.get("title") or "")
    title = re.split(r"\(@|\s[|·\-–—]\s", title)[0]
    return _squash_words(title) in keys or re.sub(r"\s+", "", title).lower() in sites


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
# the competitor's when its name STARTS with the name the user gave, as whole
# words ("Revolut Ltd", "Hubspot, Inc.", "M J Services Inc DBA Best Drains
# Plumbing" for "Best Drains"), or is the website's name part alone, apart from
# trailing company words, when the user gave only a website ("Roto-Rooter
# Services Co" for roto-rooter.com); or when the user listed it for that
# competitor (`google_advertisers` in the competitor's competitors/resolution.json
# entry). Anchored on purpose: "contains" counted "Joe Bloggs Plumbing Ltd" as
# plumbing.com's own (_name_is). A label made of two or more whole words
# ("mrrooter") may also start the name, since every advertiser in the Google pool
# already advertises the searched website. A name is judged once per advertiser id, so every ad of an
# advertiser gets the same answer.
#
# Every other ad is set aside: not shown as the competitor's, and counted with
# its advertiser names. The tool does not guess further. A company that
# advertises under another legal name (Basecamp's ads are by "37signals LLC";
# a franchisor's by its parent group) is set aside too, named, and the user can
# add that name. Setting a real ad aside is visible and fixable; showing
# another company's ad as the competitor's is the wrong-target failure.
#
# What counts as the competitor's NAME, per input (see _owner_names): a company
# given by name, or by name together with its website ("Acme Strategy Advisors
# (acmesa.com)", see pair_competitors), is known by that name; every website
# searched, used or typed adds its name part ("acmesa"), which counts as the
# advertiser's whole name ("Acmesa.com LLC" is "acmesa" once the company words
# are dropped; "Acmesa Plumbing" is not), or, for a label of two or more words,
# as the start of it (_name_is). A
# company typed as a website alone is known only by
# the name part, so an ad it places under its full registered name ("Acme Strategy
# Advisors" for acmesa.com) is set aside: in a real buyer's run that was the
# result, and the fix is to give the name with the website, which is why the
# docs recommend that form whenever the user knows both. The config's `subject`
# is deliberately NOT a key: it is a display label that can be a market phrase
# ("emergency plumber") or cover several competitors, and as a key it would count
# a lead-generation advertiser called "Emergency Plumber Co" as the competitor's
# own, silently. An advertiser that is a legal entity or a person with a
# different name (a holding company, the owner's own name) stays set aside and
# named; the user adds it with `google_advertisers`. That trade is on purpose:
# a real ad set aside is visible and fixable, another company's ad counted as
# the competitor's is silent.
# Filtering happens after the paid call: what is scraped, and what it costs,
# is unchanged, and the raw file keeps every record.
# ---------------------------------------------------------------------------

# Second-level words that sit under a country ending (bbc.co.uk, acme.com.au,
# acme.org.uk): the name part is the label to the LEFT of these, not them.
_SECOND_LEVEL = frozenset("co com org net gov edu ac mil or ne gob gouv sch nom ltd plc".split())

# Words that cannot tell one company from another: company forms, website words
# and everyday words that are also common endings. WHY refused as an owner key:
# the key decides which Google advertisers count as THE COMPETITOR'S OWN ads. A
# key of "ltd" matches every advertiser called "<anything> Ltd", so a stranger's
# ads were counted as the competitor's with no sign anything was wrong, which is the
# wrong-target failure. A key shorter than three characters ("go", "ab") is as bad.
# "est" is "establishment", a company form in some regions (Est. in a name), so
# "Est.io" is read as a name, not as a website.
# The cost of refusing is a visible set-aside note and, where the company really is
# called that, `google_advertisers` (the name to count) fixes it.
_GENERIC_KEYS = frozenset("""
ltd inc co corp corporation llc llp plc gmbh sa ag bv nv pty pvt est limited company
group holdings holding partners associates enterprises international intl global
shop store stores app apps go get my the web online home info services service
solutions solution site website official net com org biz digital agency studio
media team uk us usa eu london
""".split())


# The words that only say what legal form a business has. Narrower than _GENERIC_KEYS
# on purpose: "Group" and "Holdings" say the name belongs to a larger body, so a name
# that ends in them is not the business a short website label stands for (see the
# initials match in _name_is).
_COMPANY_FORMS = frozenset("""
ltd inc co corp corporation llc llp plc gmbh sa ag bv nv pty pvt est limited company
""".split())


def _usable_key(key: str) -> bool:
    return len(key) >= 3 and key not in _GENERIC_KEYS


def _registrable_label(domain: str) -> str:
    """The name part of a website: revolut.com -> revolut, help.revolut.com ->
    revolut, bbc.co.uk -> bbc, acme-plumbing.com -> acme-plumbing, shop.abc.de ->
    abc. WHY the label just left of the public ending, never a subdomain: the
    subdomain of a big site ("shop", "app", "go") became the competitor's name and
    matched every advertiser called that."""
    labels = [x for x in _site_form(domain).split(".") if x]
    if len(labels) < 2:
        return labels[0] if labels else ""
    cut = 2 if (len(labels) >= 3 and len(labels[-1]) == 2
                and labels[-2] in _SECOND_LEVEL) else 1
    return labels[-cut - 1]


def _squash_words(s: str) -> str:
    return "".join(_tokens(s))


def _core_squash(toks: list) -> str:
    """The words of a name run together, less the legal-form words that trail it
    ("Acme Strategy Advisors LLC" and "Acme Strategy Advisors" are both
    "acmestrategyadvisors"). Used for the names the buyer LISTED (google_advertisers)
    and for the advertiser name they are compared with. WHY only _COMPANY_FORMS, not
    _GENERIC_KEYS: a listed name is an explicit claim of exactly that name. Dropping
    "Holdings" and "Group" as well squashed a listed "Acme Holdings Group LLC" to
    "acme", so every "Acme", "Acme Holdings" and "Acme Group" in the pool counted as
    the competitor's. WHY the forms at all: a listed name was compared whole, so
    "Acme Strategy Advisors" never matched the advertiser "Acme Strategy Advisors LLC"
    the buyer was looking at, and the remedy did nothing with no sign why."""
    core = list(toks)
    while len(core) > 1 and core[-1] in _COMPANY_FORMS:
        core.pop()
    return "".join(core)

def google_searched_domain(rec: dict) -> str:
    """The website a Google ad was found by (the `domain` of the search URL the
    ad came from), '' when the record does not say."""
    from urllib.parse import parse_qs
    for key in ("originUrl", "url"):
        q = parse_qs(urlparse(str(rec.get(key) or "")).query)
        d = (q.get("domain") or [""])[0]
        if d:
            # parse_qs undoes the address's own encoding; _site_form reads what is
            # left as a typed website, with the ONE decode it allows (a doubly
            # encoded acme%252Ecom still reads as acme.com). An address that
            # spells a path, a login or a NUL reads as no website at all, never as
            # another site's.
            return _site_form(d)
    return ""


def _raw_owner_keys(entry: dict, searched: str) -> list:
    """[(key, named)] for every name the competitor could be known by, before any
    is refused. `named` is True for the name the user gave or the company is
    called, False for a key that only comes from a website's name part."""
    keys = []
    if entry.get("kind") == "name":
        keys.append((_squash_words(entry.get("input") or ""), True))
    dom = entry.get("domain")
    dom = dom if isinstance(dom, dict) else {}
    for d in (searched, dom.get("use"), dom.get("supplied")):
        if d and isinstance(d, str):
            keys.append((_squash_words(_registrable_label(d)), False))
    return [(k, n) for k, n in keys if k]


def _owner_names(entry: dict, searched: str) -> tuple:
    """({key: named}, advertiser names the user listed).
    A generic or very short key is refused (_GENERIC_KEYS): it would count the
    ads of every advertiser that shares the word as the competitor's own. A key
    that is both a given name and a website's name part counts as named."""
    keys = {}
    for k, named in _raw_owner_keys(entry, searched):
        if _usable_key(k):
            keys[k] = keys.get(k, False) or named
    dom = entry.get("domain")
    dom = dom if isinstance(dom, dict) else {}
    ga = dom.get("google_advertisers")
    # One name written as text is ONE name. Iterating the text gave one-letter
    # "names", so an advertiser called "A" became the competitor's and the name the
    # buyer wrote matched nothing.
    names = [ga] if isinstance(ga, str) else (ga if isinstance(ga, list) else [])
    listed = {_core_squash(_tokens(n)) for n in names if isinstance(n, str)}
    listed.discard("")
    return keys, listed


def _merge_owner_names(into_keys: dict, into_listed: set, keys: dict, listed: set) -> None:
    for k, named in keys.items():
        into_keys[k] = into_keys.get(k, False) or named
    into_listed |= listed


# Where a legal entity says what it trades as: "M J Services Inc DBA Best Drains
# Plumbing". The name after the marker is the business the public knows.
_TRADING_AS = (("dba",), ("d", "b", "a"), ("t", "a"), ("trading", "as"))


def _name_starts(adv_name: str) -> list:
    """The word lists an advertiser name is judged from: the name itself and the
    name after each "DBA" / "t/a" / "trading as" marker, each without a leading
    "the"."""
    toks = _tokens(adv_name)
    starts = [toks]
    for i in range(len(toks)):
        for mark in _TRADING_AS:
            if tuple(toks[i:i + len(mark)]) == mark and i + len(mark) < len(toks):
                starts.append(toks[i + len(mark):])
    return [t[1:] if t and t[0] == "the" else t for t in starts if t]


def _name_is(adv_name: str, keys: dict, listed: set) -> bool:
    """True when the advertiser name is a listed name, or is the competitor by
    one of its keys. WHY the match is anchored and not "contains": a key found
    anywhere in a name counted "Joe Bloggs Plumbing Ltd" as the owner of
    plumbing.com and "Summit Harbour Hotels" as the owner of summit.com, so a stranger's
    ads were shown as the competitor's with no sign anything was wrong (the same
    hole was in the first release). A name is judged from where it STARTS
    (_name_starts: the name, and what follows "DBA" / "trading as"):
      - a NAMED key (the name the user gave) matches the first whole words:
        "Best Drains Ltd", "The Best Drains Co" and "M J Services Inc DBA Best
        Drains Plumbing" carry "bestdrains"; "Quillonade Bars" does not carry
        "quillon", and "Joe's Best Drains" does not either.
      - a key that comes ONLY from a website's name part (the user typed a website
        and no name) matches in one of two ways. A key that is ONE ordinary word
        ("best", "plumbing", "dental") must be the WHOLE name, once the trailing
        company-form and generic words are dropped ("Plumbing Ltd" is "plumbing";
        "Joe Bloggs Plumbing Ltd" and "HubSpot Partner Agency X" are not "plumbing"
        or "hubspot"). A key that spans TWO OR MORE whole words of the advertiser's
        name ("Mr. Rooter" is "mrrooter") also matches at the START of the name,
        so a franchisee ("Mr. Rooter Plumbing of Abilene") is the competitor's.
    WHY the second way is safe enough: the Google ads step searches by the
    verified website, so every advertiser in the pool has already placed ads that
    point at that website. A stranger is counted only if it did that AND its name
    starts with the same two or more whole words as a coined label. That is strong
    evidence of the competitor or its franchisee. Known residue: a label that is
    itself two ordinary words ("greenenergy.com") would also count "Green Energy
    Partners Ltd" if that advertiser's ads point at the website. Setting every
    franchisee aside instead (the whole-name rule alone) turned the 5 real
    franchisee ads of the development target into 0, which is the opposite defect
    for a buyer who types only a website. This match is used only for Google ads;
    Meta and LinkedIn ownership is decided by page and company id, never by name.
    - a website-only key also matches a name written as its first k whole words
      followed by the INITIAL LETTER of every word that remains, once the trailing
      legal-form words (_COMPANY_FORMS) are dropped: "acmesa" is "Acme Strategy
      Advisors LLC" (acme + s + a). WHY: a firm is often known by a short coined
      label that is its name with the tail abbreviated, and typed alone (no name)
      its own ads were set aside as a stranger's. The same evidence as above makes
      it safe: the pool was searched by the verified website. The guards: the
      leading words run together are at least 3 characters and not a generic word
      (a label cannot be "plumbing" plus initials of a stranger's tail), at least 2
      initials are used (one initial is too easy to hit by chance), and ALL the
      remaining words must be abbreviated, so "Acme Strategy Advisors Group
      Holdings" (a larger body, initials "sagh") is not "acmesa". Known residue: a
      stranger whose name abbreviates to the same label, whose ads also point at
      the website, would count; the set-aside note and `google_advertisers` are the
      remedy there too.
    An advertiser that sits under another name is set aside and named, and
    `google_advertisers` fixes it."""
    if _core_squash(_tokens(adv_name)) in listed:
        return True
    for toks in _name_starts(adv_name):
        if _core_squash(toks) in listed:
            return True
        for j in range(1, len(toks) + 1):
            k = "".join(toks[:j])
            if k in keys and (keys[k] or j >= 2):
                return True
        core = list(toks)
        while len(core) > 1 and core[-1] in _GENERIC_KEYS:
            core.pop()
        if "".join(core) in keys:
            return True
        forms = list(toks)
        while len(forms) > 1 and forms[-1] in _COMPANY_FORMS:
            forms.pop()
        for k in range(1, len(forms) - 1):
            lead = "".join(forms[:k])
            if len(lead) >= 3 and lead not in _GENERIC_KEYS:
                if keys.get(lead + "".join(w[0] for w in forms[k:])) is False:
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
    # website -> EVERY entry that uses it. WHY a list: two competitors can share
    # one website (two names for one company, or a parent and its brand). Keeping
    # only the first entry made the second's name a stranger on that website, so
    # its own ads there were set aside as "another advertiser's".
    by_dom = {}
    for e in entries or []:
        if isinstance(e, dict):
            d = _site_form((e.get("domain") or {}).get("use"))
            if d:
                by_dom.setdefault(d, []).append(e)
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
            keys, listed = {}, set()
            for e in by_dom[searched]:
                _merge_owner_names(keys, listed, *_owner_names(e, searched))
        elif searched:
            keys, listed = _owner_names({}, searched)
        else:   # the record does not say which website found it: any competitor
            keys, listed = {}, set()
            for es in by_dom.values():
                for e in es:
                    _merge_owner_names(keys, listed, *_owner_names(e, ""))
        own = any(_name_is(n, keys, listed) for n in names if n)
        verdict_of[(searched, akey)] = "own" if own else (
            "other" if any(names) else "unnamed")
    return [verdict_of[k] if k[1] else "unnamed" for k in keyed]


# The remedy for a set-aside advertiser by editing the file and running again. It is
# part of the line for a competitor with NO usable key (that line is the only
# explanation the buyer gets), and the run's console says it ONCE after the
# ordinary set-aside lines (GOOGLE_LIST_REMEDY_CONSOLE). WHY the console: a company
# that advertises under its full legal name, the case that brings a buyer here most,
# reaches the buyer as the ordinary line, and the console used to give only the
# ask-Claude route (GOOGLE_CONFIRM_HOW), not the file the tool actually reads. WHY
# not on every ordinary line: those lines are shared with the app, the digest and
# the report, each of which gives its own way to confirm once, and a line per
# website repeated the instruction until it read as several.
GOOGLE_LIST_REMEDY = (
    "If that is wrong, list its advertiser names under google_advertisers in its entry "
    "in competitors/resolution.json and run again; the saved Google ads are sorted "
    "again, nothing is searched again.")
GOOGLE_LIST_REMEDY_CONSOLE = (
    "Or, from the command line: list the advertiser's name under google_advertisers in "
    "that competitor's entry in competitors/resolution.json and run again; the saved "
    "Google ads are sorted again, nothing is searched again.")


def google_set_aside_lines(items: list, entries: list, verdicts: list = None) -> list:
    """Plain sentences naming the Google ads that are not shown as a
    competitor's, one per website searched ([] when none are set aside).
    Counts come from google_ad_owners, never from the caller."""
    if verdicts is None:
        verdicts = google_ad_owners(items, entries)
    by_dom = {_site_form((e.get("domain") or {}).get("use")): e
              for e in (entries or []) if isinstance(e, dict)}
    # Websites whose competitor has no usable owner key (every key refused as
    # generic or too short, see _GENERIC_KEYS) and no listed advertiser name:
    # {website: the refused words}. Their ads are set aside, and this says why.
    no_key = {}
    for site in {google_searched_domain(r if isinstance(r, dict) else {})
                 for r in items or []}:
        if not site:
            continue
        ents = [e for e in (entries or []) if isinstance(e, dict)
                and _site_form((e.get("domain") or {}).get("use")) == site]
        raw, usable, listed = [], False, False
        for e in (ents or [{}]):
            k, l = _owner_names(e, site)
            usable = usable or bool(k)
            listed = listed or bool(l)
            raw += [w for w, _ in _raw_owner_keys(e, site)]
        if not usable and not listed and raw:
            no_key[site] = ", ".join(f"'{w}'" for w in dict.fromkeys(raw)) + (
                " is too generic or too short to tell companies apart"
                if len(set(raw)) == 1 else " are too generic or too short to tell companies apart")
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
        if s and s in no_key:
            lines.append(
                f"{who} has no name the tool can safely match advertisers against "
                f"({no_key[s]}), so none of its Google ads found for {s} can be confirmed "
                f"as its own; all are set aside. {GOOGLE_LIST_REMEDY}")
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
    by_dom = {_site_form((e.get("domain") or {}).get("use")): e
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
    from entry["domain"]["google_advertisers"], a list of names (_owner_names; one name
    written as text counts as a list of one). A key put anywhere else (directly in the
    entry, the literal reading of the old confirm sentence, or under "facebook"), or a
    value that is neither a list nor text, changed nothing, and the rebuild reported
    success. This says so, naming the
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
                        elif v and not isinstance(v, (list, str)):
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
    by_dom = {_site_form((e.get("domain") or {}).get("use")): e
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


def resolve_competitor(raw: str, organic: list, supplied_domain: str = "",
                       unused_bracket: str = "") -> dict:
    """Given a raw input and its SERP organic results, return a resolution entry
    with a chosen value + ranked candidates + flags for every target. Pure.

    The `use` fields are DEFAULTS for a non-interactive run; the `flags` and
    `candidates` exist so the resolution report can ask the human to confirm or
    correct before any spend.

    A website the user TYPED (as a domain input, or together with a name) stands
    as `domain.use`. When the search's best website differs it is kept as
    `resolved` and in `candidates`, and the report shows it as information the
    user can act on; it is not applied. WHY: the earlier rule used the search's
    best website whenever it differed, on the reasoning that a typed value is
    unproven. In a real buyer's run 5 of 14 typed websites were replaced by a
    third-party page that carried the brand in its title (a jobs board, a trade
    news article, a company database), one website resolved to itself on one run
    and to a jobs board 40 minutes later (Google's ranking drifts), and the
    buyer then had to correct each one by hand. A buyer's typed website is wrong
    far less often than the top search result for a small company is, and a
    typo is still caught, because the difference is shown beside it. A company
    given by NAME ONLY has no typed value: the search is the only evidence there,
    so its best website is used. That result can vary between runs for the same
    reason, which is why the confirmation gate exists.

    supplied_domain: the domain the user gave for this same company when it
    was given both as a name and as a domain (see pair_competitors). It is
    treated exactly like a domain input.

    unused_bracket: a website-looking bracket the buyer typed that was NOT taken
    as the website (unused_bracket_sites). Kept as `domain.typed_unused` so the
    report, the closing lines and the digest can say that the website searched
    came from a name lookup and was not what the buyer typed.
    """
    cls = classify_input(raw)
    identity = cls["name"] or cls["domain"]
    supplied = cls["domain"] or normalize_domain(supplied_domain)
    # The website the user gave alongside a NAME (a domain input is its own identity).
    site = supplied if cls["kind"] == "name" else ""

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
    fb = [c for c in rank_facebook_pages(organic, identity, site) if c["score"] > 0]
    # Namesakes: a page whose name only CONTAINS the brand word
    # ("Patagonian BaseCamp Lodge", "Air Force BASECAMP | Dhaka") is not used
    # on its own; it stays a possible page for the user to confirm. Only a page
    # named exactly as the competitor is used (_page_named_as), best score first.
    named = [c for c in fb if _page_named_as(identity, c, site)]
    fb = named + [c for c in fb if c not in named]
    li = extract_linkedin_company(organic, identity)
    agree = domain_agreement(supplied, resolved_domain)

    flags: list[str] = []
    if not resolved_domain and cls["kind"] != "domain" and not supplied:
        flags.append(_NO_DOMAIN_FLAG)
    if not named:
        flags.append(_META_NO_PAGE_FLAG)
    if not li:
        flags.append(_NO_LINKEDIN_FLAG)

    # `agreement` records how the typed website compares with the search's best;
    # it is a fact about the search and is never edited. The report compares the
    # typed website with `use` (what will be searched), see _domain_view.
    use_domain = supplied or resolved_domain

    dom_entry = {
        "use": use_domain, "supplied": supplied,
        "resolved": resolved_domain, "agreement": agree,
        "candidates": domains[:3],
    }
    if unused_bracket and not supplied:
        dom_entry["typed_unused"] = unused_bracket
    return {
        "input": raw,
        "kind": cls["kind"],
        "domain": dom_entry,
        "facebook": {"use": named[0]["url"] if named else "", "candidates": fb[:4]},
        "linkedin": {
            "use": li["name"] if li else "",
            "url": li["url"] if li else "", "candidate": li,
        },
        "flags": flags,
    }


def _domain_view(dom: dict) -> tuple:
    """(tag, note, mismatch_flag) for the Google website line of the report,
    read from the entry AS IT STANDS in resolution.json.

    WHY: the report is printed again whenever a resolution is reused, after the
    user may have edited `use` by hand. In a real run the buyer corrected `use`
    back to the website they had typed and the re-printed report still said
    "MISMATCH vs supplied", because it read the `agreement` field written at
    lookup time (typed website against the search's best), not the website that
    will actually be searched. The comparison that matters is the typed website
    against `use`.

    tag            the bracket after the website;
    note           a plain line about the search's best match when it differs from
                   the website in use ('' when there is nothing to say);
    mismatch_flag  a warning when the website in use is NOT the one the user typed
                   ('' otherwise)."""
    use = _site_form(dom.get("use"))
    supplied = normalize_domain(dom.get("supplied") or "")
    resolved = normalize_domain(dom.get("resolved") or "")
    stored = dom.get("agreement") or "no_supplied"
    typed = dom.get("typed_unused")
    if (not supplied and isinstance(typed, str) and typed
            and normalize_domain(typed) != use):
        # The buyer typed a website in brackets that was not taken as the website;
        # `use` is what a name lookup found. A plain "resolved from name" hid that,
        # and a namesake's website was searched under the buyer's eyes.
        return (f"⚠ resolved from name; you wrote {typed!r} in brackets, it was not used as the "
                f"website", "",
                f"you wrote {typed!r} in brackets but it was not used as the website. "
                f"Google will search {use or 'nothing'!r}, which the name lookup found and "
                f"could not confirm. Check `use` in competitors/resolution.json")
    if not supplied:
        return ({"match": "✓ matches supplied", "mismatch": "⚠ MISMATCH vs supplied",
                 "no_supplied": "resolved from name",
                 "unresolved": "⚠ NOT resolved"}.get(stored, "resolved from name"), "", "")
    if not use:
        # `use` blanked: the competitor is left out of the Google search (a user
        # who blanks it does not want that site searched). "will use ''" read as
        # if a website named '' would be searched.
        return ("⚠ MISMATCH vs supplied", "",
                f"domain mismatch: you gave {supplied!r} but `use` is blank, so Google will "
                f"not be searched for this competitor. Put the website in `use` in "
                f"competitors/resolution.json to search it")
    if use != supplied:
        return ("⚠ MISMATCH vs supplied", "",
                f"domain mismatch: you gave {supplied!r} but the Google search will use "
                f"{use!r}. Confirm which website is right before the Google scrape")
    if not resolved:
        return ("⚠ NOT resolved", "", "")
    if resolved == supplied:
        return ("✓ matches supplied", "", "")
    # The typed website stands, but the search found a different one: a ✓ here
    # would read as "the search agrees", which it does not. The ⚠ marks that
    # the buyer's word is being taken over the search's, so a typo in the
    # typed website (widgetise.com for widgetize.com) is not passed as checked.
    return (f"⚠ the website you gave; the search's best match is {resolved}",
            f"the search's best match was {resolved}; the website you gave ({supplied}) "
            f"is used. If {resolved} is the right one, change `use` in "
            f"competitors/resolution.json",
            "")


def website_note_lines(entries: list, searched=None) -> list[str]:
    """Plain sentences for competitors whose website Google searched although it
    is not the confirmed company website, so a wrong or mistyped website cannot
    yield an empty or foreign result that is read as "that company runs no Google
    ads". Three cases, each only for a website that Google DID search:
      - the buyer's typed website, where the search's best match was another one;
      - the buyer's typed website, where the search found nothing for it;
      - a website the name lookup chose, because the website the buyer typed in
        brackets was not used.
    WHY a line beyond the report's ⚠ marker: the marker is in the confirmation
    report, which a reviewed, non-interactive run never shows to a person; this
    goes where the results are read (the closing lines and the data digest).
    `searched`: the websites Google actually searched, from the record of the
    search itself (None = all), so a competitor held back is never claimed."""
    out = []
    keep = None if searched is None else {normalize_domain(x) for x in searched}
    for e in entries:
        dom = e.get("domain")
        if not isinstance(dom, dict):
            continue
        use = _site_form(dom.get("use"))
        supplied = normalize_domain(dom.get("supplied") or "")
        resolved = normalize_domain(dom.get("resolved") or "")
        typed = dom.get("typed_unused")
        if not use or (keep is not None and use not in keep):
            continue
        if supplied and use == supplied and resolved and resolved != supplied:
            out.append(f"Google searched {use} as you gave it; the search's best match for it "
                       f"was {resolved}. If that is the company's site, change `use` in "
                       f"competitors/resolution.json.")
        elif supplied and use == supplied and not resolved:
            out.append(f"Google searched {use} as you gave it; the search found nothing for "
                       f"it. Check the spelling, and change `use` in "
                       f"competitors/resolution.json if it is wrong.")
        elif (not supplied and isinstance(typed, str) and typed
              and normalize_domain(typed) != use):
            out.append(f"You wrote {typed!r} in brackets for {e.get('input') or use}, but it "
                       f"was not used as the website. Google searched {use}, which the "
                       f"tool found by looking up the name and could not confirm. If that "
                       f"is not the company's site, change `use` in "
                       f"competitors/resolution.json.")
    return list(dict.fromkeys(out))


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
        tag, dom_note, dom_flag = _domain_view(dom)
        lines.append(f"    Google  domain   : {dom['use'] or '(none)'}   [{tag}]")
        if dom_note:
            lines.append(f"              {dom_note}")
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
                      "  ⚠ NO numeric LinkedIn company id found, so its LinkedIn ads are not searched"
                      + linkedin_id_remedy(li))
        else:
            id_tag = ""
        lines.append(f"    LinkedIn company : {li['use'] or '(none)'}{id_tag}")
        # The website comparison is read from the entry as it stands (_domain_view),
        # so a stored "domain mismatch" flag is replaced by it. An entry with no
        # typed website has nothing to recompute from and shows its flags as stored.
        recompute = bool((dom.get("supplied") or "").strip())
        flags = [_shown_flag(f) for f in e["flags"]
                 if not (recompute and f.startswith("domain mismatch:"))]
        if dom_flag:
            flags.insert(0, dom_flag)
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
                supplied: dict | None = None, unused: dict | None = None) -> list[dict]:
    """Resolve each raw competitor identity (a name OR a domain, any case) using
    the B1 SERP results already fetched to <workdir>/B1_serp_competitors/.
    Returns one resolution entry per identity (see resolve_competitor).
    `supplied` maps a name identity to the domain the user also gave for it
    (supplied_domains(cfg)); that domain is checked against the resolved one.
    `unused` maps an identity to the bracket website that was typed but not used
    (unused_bracket_sites(cfg)).

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
        entries.append(resolve_competitor(raw, organic, (supplied or {}).get(raw, ""),
                                          (unused or {}).get(raw, "")))
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


def linkedin_id_remedy(li: dict) -> str:
    """The plain remedy, in brackets, for a LinkedIn company with no numeric id
    ('' when the entry does not say why, e.g. a file from an older version).
    Every sentence is true of the stored cause (see LI_ID_FETCH_FAILED)."""
    why = (li or {}).get("id_lookup")
    if why == LI_ID_FETCH_FAILED:
        return (" (the company page could not be fetched, so trying again later may work; "
                "or add `company_id` to this entry in competitors/resolution.json)")
    if why == LI_ID_PAGE_UNSETTLED:
        # Not "trying again will not help": a 200 response is also what LinkedIn
        # returns when it shows a sign-in page in place of the company page, and
        # that can differ from one run to the next.
        return (" (the company page was read but did not give one company id; LinkedIn "
                "sometimes shows a sign-in page instead of the company page, so a later "
                "run may work; or add `company_id` to this entry in "
                "competitors/resolution.json)")
    if why == LI_ID_NO_PAGE:
        return (" (this entry has a LinkedIn company name but no company page address, so "
                "there was no page to read; add the page address as `url`, or add "
                "`company_id`, to this entry in competitors/resolution.json)")
    return ""


def _linkedin_slug(entry: dict) -> str:
    """The LinkedIn vanity slug for a resolution entry ('' if none)."""
    li = entry.get("linkedin") or {}
    cand = li.get("candidate") or {}
    if cand.get("slug"):
        return cand["slug"]
    m = re.search(r"/company/([^/?#]+)", li.get("url") or "")
    return m.group(1) if m else ""


# Why a LinkedIn company id is empty, stored beside it as linkedin.id_lookup.
# The two causes have different remedies, so one flag for both sent a buyer to
# add the id by hand when waiting would have worked, or to retry when the page
# had already been read and does not settle it:
#   fetch_failed     the company page could not be fetched (blocked, timed out,
#                    network error): try again later, or add company_id by hand;
#   page_unsettled   the page was read (HTTP 200) but does not name one id for
#                    the company (none found, a list of several, or a conflict).
#                    NOT "retrying will not help": LinkedIn answers 200 with a
#                    sign-in page too, which can differ run to run; so a later
#                    run may work, or add company_id by hand;
#   no_page          the entry has a company name but no slug or url, so there
#                    was no page to fetch: give the page address (url) or the id.
LI_ID_FETCH_FAILED = "fetch_failed"
LI_ID_PAGE_UNSETTLED = "page_unsettled"
LI_ID_NO_PAGE = "no_page"


def linkedin_company_id_lookup(slug: str, timeout: int = 20) -> tuple:
    """(id, why) for a LinkedIn company vanity slug. `why` is '' when an id was
    found, else LI_ID_FETCH_FAILED or LI_ID_PAGE_UNSETTLED (above); a missing
    slug is LI_ID_NO_PAGE, since there is no page to fetch (and none was read). Network (one
    GET of the public company page, no auth); a deterministic call, so it is
    safe to time out."""
    if not slug:
        return "", LI_ID_NO_PAGE
    if slug.isdigit():
        return slug, ""
    import urllib.request
    req = urllib.request.Request(
        f"https://www.linkedin.com/company/{slug}",
        headers={"User-Agent": _LI_UA, "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            html = r.read().decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001  (network/parse: non-fatal; caller flags the miss)
        return "", LI_ID_FETCH_FAILED
    found = _linkedin_id_from_html(html)
    return found, ("" if found else LI_ID_PAGE_UNSETTLED)


def fetch_linkedin_company_id(slug: str, timeout: int = 20) -> str:
    """Resolve a LinkedIn company vanity slug to its NUMERIC company id via the
    public company page (no auth). Returns '' on any failure. A numeric slug is
    passed through unchanged. Deterministic HTTP GET — safe to time out.
    (linkedin_company_id_lookup also says WHY an id is empty.)"""
    return linkedin_company_id_lookup(slug, timeout)[0]


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
        li["company_id"], why = (linkedin_company_id_lookup(slug) if slug
                                 else ("", LI_ID_NO_PAGE))
        if why:
            li["id_lookup"] = why     # why the id is empty (see LI_ID_FETCH_FAILED)
        else:
            li.pop("id_lookup", None)
        e["linkedin"] = li
    return entries


def refresh_linkedin_ids(entries: list[dict]) -> bool:
    """For a REUSED resolution: look the LinkedIn company id up again where the stored
    entry has none and that can have changed, and drop a stored cause that no longer
    applies. Returns True when an entry changed (the caller writes the file).

    WHY: the id lookup ran only when a competitor was resolved the first time, yet
    the report tells the buyer "add the page address as `url`" (no page) and "trying
    again later may work" (page could not be fetched, or did not settle). On a
    plain rerun neither did anything, and the old cause was printed again even
    after `url` or `company_id` had been added. The lookup reads one free public
    page (no Apify charge), so repeating it is cheap and makes the remedy true.
    A `company_id` the buyer typed is never replaced; it only clears the stale cause."""
    changed = False
    retry = (LI_ID_FETCH_FAILED, LI_ID_PAGE_UNSETTLED, LI_ID_NO_PAGE)
    for e in entries:
        li = e.get("linkedin")
        if not isinstance(li, dict) or not li.get("use"):
            continue
        if str(li.get("company_id") or "").strip():
            if li.pop("id_lookup", None) is not None:
                changed = True
            continue
        why = li.get("id_lookup")
        if why not in retry:
            continue
        slug = _linkedin_slug(e)
        if not slug:
            continue          # still no page address to read: nothing has changed
        cid, new_why = linkedin_company_id_lookup(slug)
        li["company_id"] = cid
        if new_why:
            li["id_lookup"] = new_why
        else:
            li.pop("id_lookup", None)
        if cid or new_why != why:
            changed = True
    return changed


def write_entries_only(entries: list[dict], workdir: str | Path) -> None:
    """Write competitors/resolution.json alone (domains.json and each entry's
    `written` stay as they are). For a change to an entry that is not about which
    website Google searches."""
    (Path(workdir) / "competitors" / "resolution.json").write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


def _site_form(value) -> str:
    """A website as the Google step compares it: no scheme, no www, no path,
    lower case ('' when blank). The one reading shared with the Google stage
    (stages.site_host), so a scheme in capitals, a percent-escape or a value that is
    not text is read the same way by the writer and the reader."""
    return site_host(value)


def write_resolution(entries: list[dict], workdir: str | Path) -> Path:
    """Write the two files the downstream stages read:
      competitors/resolution.json — full entries (build_B4 reads FB pages,
                                     build_LI reads LinkedIn names, report reads all)
      competitors/domains.json    — de-duped list of chosen `use` domains, kept for
                                     reference and for repeats. The Google stage reads
                                     the `use` values in resolution.json, not this file
                                     (stages._curated_domains), so an edit here alone
                                     changes nothing.
    Returns the competitors/ directory path."""
    comp = Path(workdir) / "competitors"
    comp.mkdir(parents=True, exist_ok=True)
    # `written` = the website this call puts in domains.json for the entry. WHY:
    # later, when `use` and domains.json differ, it is the only way to tell "the
    # user edited `use`" (domains.json still holds what was written) from "the
    # user edited domains.json" (it no longer does); the first is a clear
    # instruction, the second leaves two files disagreeing and the tool must
    # not guess which website to search (stages.google_domain_plan).
    for e in entries:
        dom = e.get("domain")
        if isinstance(dom, dict):
            dom["written"] = _site_form(dom.get("use"))
    (comp / "resolution.json").write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")

    domains: list[str] = []
    seen: set[str] = set()
    for e in entries:
        d = _site_form((e.get("domain") or {}).get("use"))
        if d and d not in seen:
            seen.add(d)
            domains.append(d)
    (comp / "domains.json").write_text(
        json.dumps(domains, ensure_ascii=False, indent=2), encoding="utf-8")
    return comp


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# A trailing "(website)" on a name: "Acme Strategy Advisors (acmesa.com)". No
# whitespace inside the brackets, so "Acme (see https://x)" is not read as one.
_TRAILING_SITE_RE = re.compile(r"^(?P<name>.*\S)\s*\(\s*(?P<site>[^()\s]+)\s*\)$")
_LONE_SITE_RE = re.compile(r"^\(\s*(?P<site>[^()\s]+)\s*\)$")
_ANY_DOTTED_BRACKET_RE = re.compile(r"\(\s*(?P<site>[^()\s]*\.[^()\s]*)\s*\)")

# Website endings accepted in a bracket without "www." or "https://". Every
# two-letter country code is accepted (so acme.co.uk and acme.com.au pass through
# uk and au), and so are these generic endings: about 200 of the most common ones
# registered for company websites. WHY an allow-list with a case guard (not "any
# dotted word"): "Pty.Ltd", "St.Louis" and "Inc.Co" read as domains to a pattern that
# only wants letters after a dot, and a name was then cut and a stranger's site
# searched. WHY a list that is long, not short: the buyer's typed website must win
# whenever it plausibly is one; a real "acme.shop" left out of a short list became a
# NAME, was resolved by search, and a namesake's website was searched instead.
# Anything left out is not lost: "www." or "https://" in front always works.
_WEBSITE_ENDINGS = frozenset("""
com net org info biz xyz online site top shop store app dev io ai co club vip live icu
cyou sbs cfd rest buzz fun space website tech pro mobi name asia blog cloud digital
agency studio solutions services group company business consulting marketing media
design network systems works zone team today world life email tools expert guru ninja
center centre city academy training capital partners ventures holdings management
properties realty homes house builders construction contractors engineering repair care
clinic dental health fitness yoga church school university education events tickets
travel tours hotel bank money insurance tax cash credit loans finance fund legal law
lawyer attorney reviews rocks social video photo photography gallery fashion style beauty
salon boutique furniture kitchen garden florist restaurant pizza coffee bar beer wine
food farm pet dog auto cars car taxi parts tires plumbing energy solar software games
game news press page link click work wiki art cafe london nyc berlin paris tokyo
amsterdam vegas miami wales scot cymru ltd inc llc gmbh limited international global
institute foundation community support help guide directory exchange market markets
trade trading shopping sale deals discount coupons bid win bet casino poker bingo dentist
doctor hospital medical pharmacy vet technology codes domains hosting host coach cab
""".split())

# Hosts that are where a company has a PROFILE, not its website. A bracket (or a
# plain entry) holding one of these used to be taken as the company's website, so
# Google searched facebook.com's ads and ads from other advertisers were counted as
# the competitor's. Directory hosts come from resolver._is_directory.
# 1. brand names: the host is <brand>.<com | cc | co.cc | com.cc> with any
#    subdomains in front (google.co.uk, m.facebook.com, pinterest.com.au).
#    WHY every country variant is refused for every brand: none of these is ever a
#    company's own website, so refusing one costs a notice (and "https://" cannot
#    override it), while accepting one searches a platform and counts the ads of every
#    advertiser on it. facebook.com.au is not a real second host (it redirects), but
#    a buyer who pastes it still means a Facebook address.
_PLATFORM_BRANDS = {
    "facebook": "Facebook", "instagram": "Instagram", "linkedin": "LinkedIn",
    "twitter": "X (Twitter)", "youtube": "YouTube", "tiktok": "TikTok",
    "threads": "Threads", "snapchat": "Snapchat", "pinterest": "Pinterest",
    "whatsapp": "WhatsApp", "telegram": "Telegram", "google": "Google",
}
# 2. exact hosts (and their subdomains): short links and aliases of those platforms.
_PLATFORM_HOSTS = {
    "x.com": "X (Twitter)", "t.co": "X (Twitter)", "fb.com": "Facebook",
    "fb.me": "Facebook", "fb.watch": "Facebook", "instagr.am": "Instagram",
    "ig.me": "Instagram", "lnkd.in": "LinkedIn", "youtu.be": "YouTube",
    "wa.me": "WhatsApp", "t.me": "Telegram", "pin.it": "Pinterest",
    "goo.gl": "Google", "g.co": "Google", "g.page": "Google",
    "threads.net": "Threads", "threads.com": "Threads",
}
# 3. link shorteners and link-in-bio pages: they stand for whatever page the link
#    points at, so they name nobody's website.
_LINK_HOSTS = frozenset({
    "bit.ly", "linktr.ee", "tinyurl.com", "ow.ly", "buff.ly", "rebrand.ly", "is.gd",
    "cutt.ly", "bio.link", "lnk.bio", "beacons.ai", "carrd.co", "campsite.bio", "s.id",
})
# 4. profile and publishing sites where a company has a page, not its website.
_PROFILE_HOSTS = frozenset({
    "medium.com", "crunchbase.com", "wikipedia.org", "github.com", "substack.com",
})


def _is_country_tail(tail: list) -> bool:
    if tail == ["com"]:
        return True
    if len(tail) == 1:
        return len(tail[0]) == 2 and tail[0].isalpha()
    return len(tail) == 2 and tail[0] in ("co", "com") and len(tail[1]) == 2


def _platform_label(host: str) -> str:
    """Where the address is ('Facebook', 'bit.ly (a link shortener or link-in-bio page)',
    'yelp.com') for a social, platform, link-shortener, profile or directory host
    or any subdomain of one, else ''. Read as 'an address on <label>'."""
    h = (host or "").lower().strip(".")
    labels = h.split(".")
    for i, lab in enumerate(labels[:-1]):
        if lab in _PLATFORM_BRANDS and _is_country_tail(labels[i + 1:]):
            return _PLATFORM_BRANDS[lab]
    for dom, label in _PLATFORM_HOSTS.items():
        if h == dom or h.endswith("." + dom):
            return label
    for dom in _LINK_HOSTS:
        if h == dom or h.endswith("." + dom):
            return f"{dom} (a link shortener or link-in-bio page)"
    for dom in _PROFILE_HOSTS:
        if h == dom or h.endswith("." + dom):
            return f"{dom} (a profile or publishing site, not a company website)"
    return h if h and _is_directory(h) else ""


def _case_ok(tok: str) -> bool:
    """The LAST label (the ending) is written in one case: .com or .COM, not .Com.
    WHY the ending only: a real address is often written in CamelCase in marketing
    copy ("AcmePlumbing.com", "Acme.com", "BestDrains.co.uk"), and treating it as a
    name sent a name lookup, whose best match can be a namesake, in place of the
    website the buyer typed. A company form or a place ("Pty.Ltd", "St.Louis",
    "Inc.Co") ends in a capitalised word, so it still reads as a name."""
    head = re.split(r"[/?#]", tok, maxsplit=1)[0]     # a path may have capitals: acme.com/Home
    last = head.rsplit(".", 1)[-1]
    return last == last.lower() or last == last.upper()


def _website_ending_ok(host: str) -> bool:
    last = (host or "").rsplit(".", 1)[-1].lower()
    return last in _WEBSITE_ENDINGS or (len(last) == 2 and last.isalpha())


def _name_part_usable(host: str) -> bool:
    """False when the website's name part (the label left of the public ending) is a
    company-form or generic word or shorter than three characters: "Ltd.co",
    "Co.uk", "Inc.co", "Corp.ai", "no.de". WHY at the door and not only as an owner
    key: such a token is far more often a company form written with a dot than a
    website, and as a website it would search a stranger's site (the owner-key
    refusal is only the backstop)."""
    return _usable_key(_squash_words(_registrable_label(host)))


def _judge_token(tok: str) -> tuple:
    """(verdict, host, platform) for the text of a bracket that has a dot.
    verdict: 'site' (a website), 'platform' (an address on a platform, link or
    directory host), 'rejected' (a dotted word that is not taken as a website)."""
    cls = classify_input(tok)
    host = cls["domain"] if cls["kind"] == "domain" else ""
    explicit = "://" in tok or tok.lower().startswith("www.")
    if not host:
        return "rejected", host, ""
    shaped = explicit or (_website_ending_ok(host) and _case_ok(tok))
    if not shaped:
        return "rejected", host, ""
    # A platform address is named as such whatever its name part (x.com, fb.me, t.co
    # are short on purpose); only a website is held to the name-part rule.
    plat = _platform_label(host)
    if plat:
        return "platform", host, plat
    if not explicit and not _name_part_usable(host):
        return "rejected", host, ""
    return "site", host, ""


def _read_entry(raw: str) -> dict:
    """Read one competitor entry. Keys: kind ('plain' | 'site' | 'platform' |
    'rejected' | 'skip'), name (the identity to use), site (the website a bracket
    gave), token (the bracket text, for 'rejected'), notice (plain words, or '').

    Nothing typed is dropped silently: every bracket that holds a dot and is not
    taken as the website either gives a notice or is read sensibly (a full stop
    after the bracket, a doubled bracket, a bracket that is the whole entry)."""
    s = (raw or "").strip()
    # Stray punctuation after the bracket ("Acme (acme.com).") and a doubled bracket.
    m = re.match(r"^(.*\))\s*[.,;:!]+$", s)
    if m:
        s = m.group(1)
    s = re.sub(r"\(\s*\(([^()]*)\)\s*\)\s*$", r"(\1)", s)
    out = {"kind": "plain", "name": s, "site": "", "token": "", "notice": ""}
    lone = _LONE_SITE_RE.match(s)
    m = _TRAILING_SITE_RE.match(s) if not lone else None
    if lone or m:
        tok = (lone or m).group("site")
        name = "" if lone else m.group("name").strip()
        if "." not in tok:
            if lone:
                out["name"] = tok     # "(Acme)": the brackets held the whole entry
            return out
        verdict, host, plat = _judge_token(tok)
        if lone:
            if verdict == "site":
                out.update(kind="plain", name=host)
            elif verdict == "platform":
                out.update(kind="skip", notice=(
                    f"'{s}' is an address on {plat}, not a company's website, so it was "
                    f"skipped. To include the company, give its name and website: "
                    f"'<company name> (<website>)'."))
            else:
                out.update(kind="rejected", token=tok,
                           notice=_rejected_notice(s, tok, s, lone=True))
            return out
        if classify_input(name)["kind"] != "name":
            # "acme.com (acme.com)": a website with its own website in brackets.
            if verdict == "site" and host == classify_input(name)["domain"]:
                out.update(name=host)
            else:
                out["notice"] = (f"'{s}': the entry has a website before the bracket and "
                                 f"another thing in it, so it was used as written. Give "
                                 f"one website per entry, or a company name followed by "
                                 f"its website in brackets.")
            return out
        if verdict == "site":
            out.update(kind="site", name=name, site=host)
        elif verdict == "platform":
            out.update(kind="platform", name=name, notice=(
                f"'{s}': the bracket text '{tok}' is an address on {plat}, not the company's "
                f"website, so only '{name}' is used. To give the website, put it in the bracket: "
                f"'{name} (<website>)'."))
        else:
            out.update(kind="rejected", token=tok, notice=_rejected_notice(s, tok, name))
        return out
    # A dotted bracket that is not at the end ("Acme (acme.com) Ltd"): it is not
    # the website (the name is not known to end there), but a typed website must
    # not vanish: say so, and carry it as unused so the Google line warns too.
    for mm in _ANY_DOTTED_BRACKET_RE.finditer(s):
        tok = mm.group("site")
        verdict, host, plat = _judge_token(tok)
        if verdict in ("site", "platform"):
            out.update(kind="rejected", token=tok, notice=(
                f"'{s}': the bracket text '{tok}' is not at the end of the entry, so it "
                f"was not used as the website. The entry is used as written and the "
                f"company will be looked up by search, the website it finds shown as "
                f"unconfirmed. To give the website, put it last: 'Name (website)'."))
            break
    if out["kind"] == "plain" and not out["notice"] and _whole_entry_site_refused(s):
        # One dotted word typed alone that is a company form or a generic or very
        # short word with an ending ("Inc.Co", "Pty.Ltd"): read as a NAME, and said
        # so, because it used to be searched on Google as a website.
        label = _registrable_label(normalize_domain(s))
        out["notice"] = (
            f"'{s}': read as a company name, not a website, because the word before the "
            f"ending ('{label}') is a company form, a generic word or shorter than three "
            f"characters, which is far more often a name written with a dot than a "
            f"website. The company will be looked up by search and the website it finds "
            f"shown as unconfirmed. If it is a website, write it with https:// in front: "
            f"'https://{s.lower()}'.")
    return out


def _rejected_notice(s: str, tok: str, name: str, lone: bool = False) -> str:
    if lone:
        # The brackets ARE the whole entry ("(Pty.Ltd)"): there is no name before them
        # to put a website after, so the advice is a bare website or a name first.
        return (f"'{s}': the bracket text '{tok}' was not used as a website (a website is "
                f"read from its ending, such as .com, .shop or .agency or a two-letter "
                f"country code, written all in lower case or all in capitals, and the name "
                f"before the ending is not a company form or generic word such as Ltd, Inc "
                f"or Co; www. or https:// in front always works). The entry is used as "
                f"written and the company will be looked up by search, the website it "
                f"finds shown as unconfirmed. To give the website, write it with "
                f"https:// and no brackets, 'https://{tok.lower()}', or put a company "
                f"name before it, '<company name> (https://{tok.lower()})'.")
    return (f"'{s}': the bracket text '{tok}' was not used as the website (a bracket is "
            f"read as a website when its ending is a common website ending such as .com, "
            f".shop or .agency or a two-letter country code, written all in lower case or "
            f"all in capitals, and the name before the ending is not a company form or "
            f"generic word such as Ltd, Inc or Co; www. or https:// in front always "
            f"works). The entry is used as written, brackets included: the company will "
            f"be looked up by search and the website it finds shown as unconfirmed, with "
            f"a warning on its Google line. To give the website, start it with https://, "
            f"as in '{name} (https://{tok.lower()})'.")


def parse_competitor_entry(raw: str) -> tuple:
    """(name, website, notice) for one competitor entry.

    'Acme Strategy Advisors (acmesa.com)' -> ('Acme Strategy Advisors', 'acmesa.com', '').
    Anything else is the entry as written with no website and, where the buyer
    is likely to be surprised, a notice (plain words) saying what was done.

    WHY: a name and a website are the same company only when the name spells the
    website's first label (pair_competitors), so a company whose name does not
    ("Acme Strategy Advisors" and acmesa.com) could not be given as ONE
    competitor. The buyer gave the website alone, and then every lookup that
    needs the company's name (the search, the Facebook page, the LinkedIn
    company, the Google advertiser name) was working from the website's label.
    An explicit pair is unambiguous, which positional pairing of the two lists
    is not (lists of equal length may name different companies).

    Guards, each against reading ordinary names as pairs: the bracket must be the
    WHOLE trailing parenthetical, hold a single token with no spaces, and what
    stands before it must be a name, not a domain. The token must be a website
    (_judge_token): it has "www." or a scheme ("https://..."), or it is written in
    one case and ends in one of _WEBSITE_ENDINGS or a two-letter country code. So
    "Acme (UK) Ltd" (not trailing), "Best Drains (Chicago)" (no dot), "Acme (see
    https://x)" (spaces inside), "Acme (St.Louis)" and "Acme Holdings (Ltd.co)"
    (mixed case) stay names; the last two get a notice. A token that is a social,
    platform, link-shortener or directory address ("facebook.com/acme") is not the
    company's website: the entry becomes the plain name and the notice says how to
    give the website. See _read_entry for the punctuation it forgives."""
    r = _read_entry(raw)
    if r["kind"] in ("site", "platform"):
        return r["name"], r["site"], r["notice"]
    return r["name"], "", r["notice"]


def unused_bracket_sites(cfg: dict) -> dict:
    """{identity: the bracket text that was NOT used as its website} for entries
    whose dotted bracket was rejected (see _read_entry). The identity is the entry as
    written, which is what resolution.json stores as `input`.

    WHY carried to the resolution entry: such a company is looked up by its name, and
    the website the search finds can be a namesake's. Without this the report tagged
    the line as an ordinary "resolved from name" and the buyer, who had typed the
    right website, was given no sign it was ignored."""
    out = {}
    for raw in list(cfg.get("competitor_names") or []) + list(cfg.get("competitor_domains") or []):
        r = _read_entry(raw)
        if r["kind"] == "rejected":
            out[r["name"]] = r["token"]
    return out


def split_name_and_site(raw: str) -> tuple:
    """('Acme Strategy Advisors', 'acmesa.com') for 'Acme Strategy Advisors
    (acmesa.com)', else (raw stripped, ''). See parse_competitor_entry, which
    also says why an entry was read the way it was."""
    name, site, _ = parse_competitor_entry(raw)
    return name, site


def pair_competitors(cfg: dict, notices: list | None = None) -> list[tuple[str, str]]:
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
    domain as the supplied domain, which is the website used for Google.

    A company whose name does not spell its website is given as one entry,
    "Acme Strategy Advisors (acmesa.com)" (parse_competitor_entry): the same shape
    as an automatic pair. A website that is part of such an entry is not also a
    second competitor if it appears in the domains list too.

    Not paired by list position: a names list and a domains list of the same
    length may name different companies, and merging those would drop a
    competitor without a word. A name and domain that do not match this way
    stay two competitors, as before; the resolution report shows both.

    `notices`, when given, collects the plain sentences for what was changed or
    set aside while reading the list (competitor_input_notices prints them):
    a bracket that was not a website, a social address skipped, a second
    website for the same name that was not used. Nothing is dropped without one."""
    say = notices.append if notices is not None else (lambda _t: None)
    items: list[list] = []           # [identity, explicit website or '']
    index: dict[str, int] = {}
    for raw in list(cfg.get("competitor_names") or []) + list(cfg.get("competitor_domains") or []):
        r = _read_entry(raw)
        ident, site = r["name"], r["site"]
        if r["notice"]:
            say(r["notice"])
        if r["kind"] == "skip":
            continue
        key = ident.lower()
        if not key:
            continue
        if not site:
            plain = classify_input(ident)
            plat = _platform_label(plain["domain"]) if plain["kind"] == "domain" else ""
            if plat:
                # A profile address typed as a competitor: Google would search the
                # platform, and every advertiser there would be "the competitor".
                say(f"'{ident}' is an address on {plat}, not a company's website, so it was "
                    f"skipped. To include the company, give its name and website: "
                    f"'<company name> (<website>)'.")
                continue
        if key in index:
            have = items[index[key]]
            if site and not have[1]:
                have[1] = site
            elif site and site != have[1]:
                say(f"'{have[0]}' was given with two websites, {have[1]} and {site}. A "
                    f"competitor has one website, so only {have[1]} is used and {site} "
                    f"is not searched. To search both, give each its own name (add a "
                    f"word that tells them apart, such as the city or country): "
                    f"'{have[0]} <city or country> ({site})'.")
            continue
        index[key] = len(items)
        items.append([ident, site])
    classes = [classify_input(it[0]) for it in items]
    explicit = {it[1] for it in items if it[1]}
    paired: dict[int, int] = {}      # name index -> domain index
    taken: set[int] = set()          # domain indexes already paired
    for j, cj in enumerate(classes):
        # A website given inside a name entry is that competitor's: a bare copy of
        # it in the domains list is the same company, not a second one.
        if cj["kind"] == "domain" and not items[j][1] and cj["domain"] in explicit:
            taken.add(j)
    for i, ci in enumerate(classes):
        if ci["kind"] != "name" or not _squash(ci["name"]) or items[i][1]:
            continue
        for j, cj in enumerate(classes):
            if (cj["kind"] == "domain" and j not in taken and cj["domain"]
                    and _squash(cj["domain"].split(".")[0]) == _squash(ci["name"])):
                paired[i] = j
                taken.add(j)
                break
    out = []
    for i, it in enumerate(items):
        if i in taken:
            continue
        site = it[1] or (classes[paired[i]]["domain"] if i in paired else "")
        out.append((it[0], site))
    return out


def competitor_input_notices(cfg: dict) -> list[str]:
    """The plain sentences for everything the competitor lists changed or set
    aside on reading (see pair_competitors), de-duplicated, in list order. Empty
    for an ordinary list. Printed once, before the competitors are resolved."""
    out: list[str] = []
    pair_competitors(cfg, out)
    return list(dict.fromkeys(out))


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


def check_resolution_match(identities: list, stored_entries, supplied: dict | None = None) -> dict:
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

    WHY the supplied website is compared too (`supplied`: {identity: website the
    config gives for it}, as supplied_domains returns it): the stored entry was
    resolved and confirmed for ONE website. If the buyer later changes the
    website in the config (a typo fixed, a different company's site), the name
    is the same, so a names-only comparison reused the old resolution and
    Google searched the website that was confirmed before. The new website
    needs its own confirmation, so it counts as a change: looked up again and
    shown. `supplied` None keeps the comparison to names.
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

    if current == stored and supplied is not None:
        want = {k.strip().lower(): normalize_domain(v) for k, v in supplied.items()}
        changed = []
        for e in stored_entries:
            key = e["input"].strip().lower()
            cls = classify_input(e["input"])
            expected = want.get(key) or (cls["domain"] if cls["kind"] == "domain" else "")
            dom = e.get("domain")
            if not isinstance(dom, dict) or "supplied" not in dom:
                # An older entry never recorded the website the buyer gave. Missing is
                # "not known", not "no website": calling it a change re-resolved the
                # competitor (paid) and overwrote the buyer's edits. Only an explicit
                # difference is a change.
                continue
            had = normalize_domain(dom.get("supplied") or "")
            if expected != had:
                changed.append((e["input"], had, expected))
        if changed:
            def _site(d):
                return d if d else "no website"
            parts = "; ".join(f"{n}: {_site(h)} is now {_site(w)}" for n, h, w in changed)
            return {"match": False, "added": [], "removed": [], "changed": changed,
                    "notice": (
                        f"The website you gave changed ({parts}). "
                        f"{'That competitor' if len(changed) == 1 else 'Those competitors'} "
                        f"will be looked up again and shown for confirmation before anything "
                        f"is scraped, because the confirmed resolution was for the old "
                        f"website{'' if len(changed) == 1 else 's'}.")}
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
    # The typed domain stands (the search's best match for a small company is wrong
    # more often than a typed domain is); the difference is shown beside it.
    print("\n=== CASE 2: supplied domain != resolved -> kept, difference shown ===")
    o2 = organic(
        ("Widgetize | Home", G, "https://www.widgetize.com"),
        ("Widgetize | Facebook", G, "https://www.facebook.com/Widgetize"),
    )
    e2 = resolve_competitor("widgetise.com", o2)   # user typed the WRONG domain
    check("C2 agreement is mismatch", e2["domain"]["agreement"], "mismatch")
    truthy("C2 no warning flag: the typed domain is what will be searched",
           not any("mismatch" in f for f in e2["flags"]))
    check("C2 the typed domain is used", e2["domain"]["use"], "widgetise.com")
    check("C2 the search's best match is kept for the human", e2["domain"]["resolved"], "widgetize.com")
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
        # Supplied domain best-drains.com vs resolved bestdrains.com is a REAL difference
        # (a hyphen) — surfaced beside the typed domain, never auto-corrected.
        check("C9 supplied-domain hyphen mismatch flagged",
              entries[1]["domain"]["agreement"], "mismatch")

        comp = write_resolution(entries, _wd)
        wrote_domains = json.loads((comp / "domains.json").read_text())
        wrote_res = json.loads((comp / "resolution.json").read_text())
        check("C9 domains.json = chosen use-domains (the typed hyphenated domain stands)",
              wrote_domains, ["acmeplumbing.com", "best-drains.com"])
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
    truthy("report says the search found another site and the typed one is used",
           "the search's best match was widgetize.com; the website you gave (widgetise.com) is used"
           in rep)
    truthy("report shows no MISMATCH warning for a typed domain that is in use", "MISMATCH" not in rep)
    truthy("report shows a resolved domain", "brandco.com" in rep)
    e2_edited = json.loads(json.dumps(e2))
    e2_edited["domain"]["use"] = "widgetize.com"      # the user changed `use` by hand
    rep2 = format_resolution_report([e2_edited])
    truthy("an edited `use` that differs from the typed domain is warned about",
           "MISMATCH" in rep2 and "you gave 'widgetise.com' but the Google search will use "
           "'widgetize.com'" in rep2)
    e2_back = json.loads(json.dumps(e2_edited))
    e2_back["domain"]["use"] = "widgetise.com"        # ... and changed it back
    e2_back["flags"] = ["domain mismatch: supplied 'widgetise.com' != resolved 'widgetize.com'"
                        ". Confirm the real advertiser domain before the Google scrape"]
    truthy("after `use` is set back to the typed domain no mismatch is printed, "
           "even beside a stored mismatch flag",
           "MISMATCH" not in format_resolution_report([e2_back])
           and "domain mismatch" not in format_resolution_report([e2_back]))

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
            {"input": "Best Drains", "kind": "name", "domain": {"use": "bestdrains.com"}}]
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
    # A website typed alone: a label of two or more words ("bestdrains") also starts a trading
    # name, so the DBA name counts; a one-word label ("hubspot") is only the WHOLE name, so a
    # partner agency is another advertiser.
    check("C17 a website typed alone: a two-word label counts a DBA trading name",
          google_ad_owners(g, ents[:3] + [{"input": "bestdrains.com", "kind": "domain",
                                           "domain": {"use": "bestdrains.com"}}])[9:11],
          ["own", "own"])
    check("C17 a website typed alone: a one-word label does not claim a partner agency",
          google_ad_owners([gad("hubspot.com", "HubSpot Partner Agency X", "AR9")], ents[:2]), ["other"])
    ents[2]["domain"]["google_advertisers"] = ["37signals LLC"]
    check("C17 a name the user lists counts as the competitor's",
          google_ad_owners(g, ents)[8], "own")

    # --- CASE 18: a company given as "Name (website)" is ONE competitor whose name
    # drives every lookup, while the typed website drives Google. ----------------
    print("\n=== CASE 18: Name (website) pairs ===")
    _pc = {"competitor_names": ["Acme Strategy Advisors (acmesa.com)", "Acme (UK) Ltd",
                                "Best Drains (Chicago)"],
           "competitor_domains": ["acmesa.com"]}
    check("C18 explicit pair: one identity (the name), its website supplied; ordinary "
          "brackets stay names; the same website in the domains list is not a second competitor",
          pair_competitors(_pc),
          [("Acme Strategy Advisors", "acmesa.com"), ("Acme (UK) Ltd", ""), ("Best Drains (Chicago)", "")])
    check("C18 old configs: a name and a domain that spell alike still pair",
          pair_competitors({"competitor_names": ["Acme Plumbing"], "competitor_domains": ["acmeplumbing.com"]}),
          [("Acme Plumbing", "acmeplumbing.com")])
    o18 = organic(
        ("Acme Strategy Advisors", G, "https://www.jobsboard.com"),
        ("Acme Strategy Advisors | acmesa.com", G, "https://www.acmesa.com"),
        ("Acmesa.com (@acmesaofficial) - Mentions", G, "https://www.facebook.com/acmesaofficial"),
        ("Acme Strategy Advisors | LinkedIn", G, "https://www.linkedin.com/company/acme-strategy"))
    e18 = resolve_competitor("Acme Strategy Advisors", o18, "acmesa.com")
    check("C18 the typed website is used even when a third-party site ranks first",
          (e18["domain"]["use"], e18["domain"]["resolved"]), ("acmesa.com", "jobsboard.com"))
    check("C18 the page named by the website in full is used",
          e18["facebook"]["use"], "https://www.facebook.com/acmesaofficial")
    check("C18 the LinkedIn company is found by the name", e18["linkedin"]["use"], "Acme Strategy Advisors")

    # --- CASE 19: what a bracket may hold; social addresses; a changed website ---
    print("\n=== CASE 19: bracket rules, social addresses, changed website ===")
    check("C19 .co.uk, an all-capitals .COM and a full URL are websites",
          [split_name_and_site(f"Acme ({t})")[1] for t in ("acme.co.uk", "ACME.COM", "https://Acme.io/x")],
          ["acme.co.uk", "acme.com", "acme.io"])
    check("C20 a real ending outside the old short list is a website (acme.shop)",
          split_name_and_site("Acme Plumbing (acme.shop)"), ("Acme Plumbing", "acme.shop"))
    check("C20 a mixed-case dotted word is a name, not a website (Ltd.co, Inc.Co, Co.uk)",
          [parse_competitor_entry(f"Acme Holdings ({t})")[1] for t in ("Ltd.co", "Inc.Co", "Co.uk")],
          ["", "", ""])
    check("C20 platform aliases and shorteners are not websites",
          [parse_competitor_entry(f"Acme ({t})")[1] for t in
           ("fb.com", "instagr.am", "lnkd.in", "youtu.be", "google.co.uk", "bit.ly", "linktr.ee")],
          [""] * 7)
    check("C20 the label left of the public ending is the name part, never a subdomain",
          [_registrable_label(d) for d in ("shop.abc.de", "app.hey.io", "go.ab.co", "bbc.co.uk", "acme.com.au")],
          ["abc", "hey", "ab", "bbc", "acme"])
    check("C19 Pty.Ltd, St.Louis and v2.0 are not: the entry stays as written, with a notice",
          [(parse_competitor_entry(f"Acme ({t})")[:2], bool(parse_competitor_entry(f"Acme ({t})")[2]))
           for t in ("Pty.Ltd", "St.Louis", "v2.0")],
          [((f"Acme ({t})", ""), True) for t in ("Pty.Ltd", "St.Louis", "v2.0")])
    check("C19 a Facebook address in the bracket leaves the plain name",
          parse_competitor_entry("Acme (facebook.com/acme)")[:2], ("Acme", ""))
    check("C19 a social address typed as a competitor is skipped, not searched",
          pair_competitors({"competitor_domains": ["facebook.com", "bestdrains.com"]}),
          [("bestdrains.com", "")])
    check("C19 a changed typed website is a mismatch",
          check_resolution_match(["Acme"], [{"input": "Acme", "domain": {"supplied": "a.com"}}],
                                 {"Acme": "b.com"})["match"], False)

    print(f"\n{'='*50}\nResults: {PASS} passed, {FAIL} failed")
    if FAIL:
        sys.exit(1)
