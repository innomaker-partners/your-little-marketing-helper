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
    real companies keep hyphens: coca-cola.com, mr-rooter.com are correct).
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
# Assemble one resolution entry, and format the human-facing report
# ---------------------------------------------------------------------------

def resolve_competitor(raw: str, organic: list) -> dict:
    """Given a raw input and its SERP organic results, return a resolution entry
    with a chosen value + ranked candidates + flags for every target. Pure.

    The `use` fields are DEFAULTS for a non-interactive run; the `flags` and
    `candidates` exist so the resolution report can ask the human to confirm or
    correct before any spend. On a supplied-vs-resolved domain mismatch the
    default is the EVIDENCE-BACKED resolved domain (the supplied one is unproven),
    but the mismatch is always flagged — never silently applied.
    """
    cls = classify_input(raw)
    identity = cls["name"] or cls["domain"]

    domains = rank_domain_candidates(organic, identity)
    resolved_domain = domains[0]["domain"] if domains else ""
    fb = rank_facebook_pages(organic, identity)
    li = extract_linkedin_company(organic, identity)
    agree = domain_agreement(cls["domain"], resolved_domain)

    flags: list[str] = []
    if agree == "mismatch":
        flags.append(
            f"domain mismatch: supplied {cls['domain']!r} != resolved "
            f"{resolved_domain!r} — confirm the real advertiser domain before "
            f"the Google scrape"
        )
    if not resolved_domain and cls["kind"] != "domain":
        flags.append("no domain resolved — Google Ads Transparency will be empty")
    if not fb:
        flags.append("no Facebook Page found — Meta falls back to keyword sweep "
                     "unless a Page is supplied/confirmed")
    if not li:
        flags.append("no LinkedIn company found — LinkedIn stage will be empty")

    if agree == "match":
        use_domain = cls["domain"]
    elif agree == "mismatch":
        use_domain = resolved_domain          # evidence-backed default; flagged above
    else:  # no_supplied / unresolved
        use_domain = resolved_domain or cls["domain"]

    return {
        "input": raw,
        "kind": cls["kind"],
        "domain": {
            "use": use_domain, "supplied": cls["domain"],
            "resolved": resolved_domain, "agreement": agree,
            "candidates": domains[:3],
        },
        "facebook": {"use": fb[0]["url"] if fb else "", "candidates": fb[:4]},
        "linkedin": {
            "use": li["name"] if li else "",
            "url": li["url"] if li else "", "candidate": li,
        },
        "flags": flags,
    }


def format_resolution_report(entries: list[dict]) -> str:
    """Human-facing report shown at the dry-run gate before any spend."""
    lines = ["", "=" * 64, "RESOLUTION REPORT — confirm before any paid scrape", "=" * 64]
    any_flag = False
    for e in entries:
        lines.append(f"\n▸ input: {e['input']!r}  (read as {e['kind']})")
        dom = e["domain"]
        tag = {"match": "✓ matches supplied", "mismatch": "⚠ MISMATCH vs supplied",
               "no_supplied": "resolved from name", "unresolved": "⚠ NOT resolved"}[dom["agreement"]]
        lines.append(f"    Google  domain   : {dom['use'] or '(none)'}   [{tag}]")
        if dom["candidates"]:
            alt = ", ".join(f"{c['domain']}({c['score']})" for c in dom["candidates"])
            lines.append(f"              candidates: {alt}")
        fb = e["facebook"]
        lines.append(f"    Meta    page     : {fb['use'] or '(none — keyword sweep)'}")
        if len(fb["candidates"]) > 1:
            alt = ", ".join(c["slug"] for c in fb["candidates"][1:])
            lines.append(f"              alternates: {alt}")
        li = e["linkedin"]
        cid = li.get("company_id")
        # company_id is filled by enrich_linkedin_ids AFTER resolution; when a
        # name resolved but the numeric id did not, say so — the LI stage needs
        # the id and would otherwise be silently empty.
        if li["use"] and "company_id" in li:
            id_tag = f"  [id {cid}]" if cid else "  ⚠ NO numeric id — LinkedIn stage will be empty"
        else:
            id_tag = ""
        lines.append(f"    LinkedIn company : {li['use'] or '(none)'}{id_tag}")
        for f in e["flags"]:
            any_flag = True
            lines.append(f"    ⚠ {f}")
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


def resolve_all(identities: list[str], workdir: str | Path) -> list[dict]:
    """Resolve each raw competitor identity (a name OR a domain, any case) using
    the B1 SERP results already fetched to <workdir>/B1_serp_competitors/.
    Returns one resolution entry per identity (see resolve_competitor).

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
        entries.append(resolve_competitor(raw, organic))
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
# for when the page markup omits f_C.
_LI_ID_PATTERNS = (
    r"f_C=(\d+)",
    r"urn:li:fsd_company:(\d+)",
    r"urn%3Ali%3Afsd_company%3A(\d+)",
    r'"companyId":\s*(\d+)',
)
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
    from collections import Counter
    req = urllib.request.Request(
        f"https://www.linkedin.com/company/{slug}",
        headers={"User-Agent": _LI_UA, "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            html = r.read().decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001  (network/parse — non-fatal; caller flags the miss)
        return ""
    # Try each pattern in trust order. Take the FIRST pattern that yields ids, and
    # accept it when it is unambiguous: a single distinct id (regardless of how
    # many times it appears — the markup may embed it once or many times), OR a
    # strict plurality winner. Genuine ambiguity (two distinct ids tied for the
    # top) returns '' so the human confirms rather than the code guessing. The
    # earlier "count >= 2" rule was wrong — it was calibrated against one page's
    # incidental repetition and rejected the correct id when it appeared once.
    for pat in _LI_ID_PATTERNS:
        counts = Counter(re.findall(pat, html))
        if not counts:
            continue
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


def build_identities(cfg: dict) -> list[str]:
    """The unified competitor identity list from config, order-preserving and
    de-duped. Names and domains are BOTH accepted (there is no separate 'mode' —
    every supplied competitor, however typed, is resolved the same way)."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in list(cfg.get("competitor_names") or []) + list(cfg.get("competitor_domains") or []):
        key = (raw or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(raw.strip())
    return out


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

    print(f"\n{'='*50}\nResults: {PASS} passed, {FAIL} failed")
    if FAIL:
        sys.exit(1)
