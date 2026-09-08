"""
Name-to-domain resolver for the ad-intelligence tool.

Handles "mode 2" competitor identification: the user supplies company names
(e.g. "Acme Plumbing") and this module resolves them to domains by searching
Google (via the existing B1/apify/google-search-scraper stage) and picking
the top organic result that is NOT a directory, social, or aggregator site.

Workflow (called by ad_intel.py, not run_phase.py):
  1. resolver.write_name_queries(names, workdir)
       -> writes B1_serp_competitors/queries.json so build_B1 uses the names as
          search queries instead of discovery seed terms.
  2. ad_intel.py runs B1 (via run_phase.py --only B1 ...)
  3. resolver.resolve_from_serp(names, workdir)
       -> reads B1 output, picks the best organic result per name, writes
          competitors/domains.json for B5 to consume.

Field-name assumptions (confirmed against real apify/google-search-scraper output):
  - Each record: searchQuery.term (str) — the query string, matches the name exactly.
  - Each record: organicResults (list of dicts with "displayedUrl", "url", "title", "position").
  - Falls back to "results" key if "organicResults" is absent (actor version variance).
  - displayedUrl carries the true destination host; `url` is a google.com/goto redirect
    for most results (opaque protobuf blob, not decodable to a clean domain reliably).

These assumptions are marked [VERIFY] so you can confirm against a live run
if the actor schema drifts.
"""

import json
import sys
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Shared helpers — import from postprocess to avoid duplication.
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from postprocess import _domain, _SKIP_DOMAINS  # noqa: E402


# ---------------------------------------------------------------------------
# Extra directory / aggregator patterns not covered by _SKIP_DOMAINS.
# These are domains we commonly see outrank a company's own site when
# searching by company name. The list is intentionally conservative —
# false positives (wrongly skipping a real competitor site) are worse
# than false negatives (keeping a rare aggregator for manual review).
# ---------------------------------------------------------------------------

_EXTRA_SKIP_DOMAINS: frozenset[str] = frozenset({
    "yellowpages.com", "superpages.com", "manta.com", "bbb.org",
    "yelp.co.uk", "yell.com", "yp.com", "angieslist.com",
    "homeadvisor.com", "porch.com", "fixr.com", "taskrabbit.com",
    "amazon.com", "ebay.com", "etsy.com", "craigslist.org",
    "glassdoor.com", "indeed.com", "monster.com", "zoominfo.com",
    "dnb.com", "opencorporates.com", "corporationwiki.com",
    "bizapedia.com", "businessinsider.com", "bloomberg.com",
    "forbes.com", "inc.com", "entrepreneur.com",
    # App-store listings are not the company's own site.
    "apps.apple.com", "play.google.com",
})


def _displayed_domain(displayed_url: str) -> str:
    """Extract a bare domain from a displayedUrl value.

    The apify/google-search-scraper actor returns displayedUrl in three forms:
      - Clean URL:    'https://www.mrrooter.com'
      - Breadcrumb:   'https://www.trustpilot.com › ... › Plumber'
      - Junk:         '3,4K+ followers', '10+ comments  ·  8 months ago', ''

    Strategy: split on ' › ' (space-laquo-space) and take the first segment
    (the URL part). If that segment does not start with 'http', it is not a URL
    (e.g. '3,4K+ followers', '10+ comments  ·  8 months ago') → return ''.
    Otherwise pass through _domain() which strips scheme/www/path.
    If the resulting domain contains no dot, return '' as a final sanity guard.
    """
    if not displayed_url:
        return ""
    raw = displayed_url.split(" › ")[0].strip()
    if not raw.startswith("http"):
        return ""
    d = _domain(raw)
    return d if "." in d else ""


def _is_directory(domain: str) -> bool:
    """Return True if `domain` looks like a directory / aggregator, NOT a
    company's own site.

    Two guards:
    1. Membership in _SKIP_DOMAINS or _EXTRA_SKIP_DOMAINS.
    2. Heuristic: domain contains a keyword fragment that strongly signals
       a listing service rather than an operator. Only common, unambiguous
       fragments are used to avoid false positives.
    """
    if not domain:
        return True  # empty domain is unusable
    d = domain.lower()
    # Exact match OR any SUBDOMAIN of a skip domain. A social/directory subdomain
    # (my.linkedin.com, business.linkedin.com, m.facebook.com) is never a
    # company's own site — without the subdomain check a `site:linkedin.com`
    # resolution query leaks linkedin.com subdomains into domain candidates, and
    # the company name in their titles scores them high enough to win.
    if any(d == s or d.endswith("." + s) for s in _SKIP_DOMAINS) or \
       any(d == s or d.endswith("." + s) for s in _EXTRA_SKIP_DOMAINS):
        return True
    # Heuristic keyword fragments (require whole-word-ish boundaries via "-")
    _DIRECTORY_FRAGMENTS = (
        "directory", "directories", "listings", "yellowpages",
        "superpages", "whitepages", "findlocal",
    )
    for frag in _DIRECTORY_FRAGMENTS:
        if frag in d:
            return True
    return False


# ---------------------------------------------------------------------------
# Shared B1-dataset reader — the ONE place that knows the SERP output layout.
# Both resolve_from_serp (this module) and resolve.resolve_all (the unified
# resolver) consume B1 through these helpers so the actor's field shape is
# never re-derived in two places (a drift there = the silent-zero bug class).
# ---------------------------------------------------------------------------

def load_b1_records(workdir: str | Path) -> list[dict]:
    """Load the B1 SERP dataset (full run preferred, test fallback). Returns []
    with a warning when no B1 output exists yet."""
    b1_dir = Path(workdir) / "B1_serp_competitors"
    for fname in ("run_02_full.json", "run_01_test.json"):
        p = b1_dir / fname
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                print(f"resolver: loaded {len(raw)} B1 records from {p.name}")
                return raw if isinstance(raw, list) else []
            except Exception as e:  # noqa: BLE001
                print(f"resolver: WARNING — could not parse {p}: {e}")
                return []
    print("resolver: WARNING — no B1 output found.")
    return []


def index_by_term(records: list[dict]) -> dict[str, dict]:
    """Index B1 records by their lowercased search term. One record per query;
    lowercased to tolerate case drift between the queries we wrote and what the
    actor echoes back (searchQuery.term)."""
    index: dict[str, dict] = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        sq = rec.get("searchQuery")  # [VERIFY] actor field name
        term = (sq.get("term") if isinstance(sq, dict) else None) or ""
        if term:
            index[term.lower().strip()] = rec
    return index


def organic_of(rec: dict | None) -> list[dict]:
    """The organic-results list of one B1 record ([] if absent). The actor uses
    `organicResults`; older builds used `results` — tolerate both."""
    if not isinstance(rec, dict):
        return []
    return rec.get("organicResults") or rec.get("results") or []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def write_name_queries(names: list[str], workdir: str | Path) -> Path:
    """Write B1_serp_competitors/queries.json so build_B1 uses the company
    names as search queries.

    Returns the path of the written file.
    """
    workdir = Path(workdir)
    q_dir = workdir / "B1_serp_competitors"
    q_dir.mkdir(parents=True, exist_ok=True)
    out = q_dir / "queries.json"
    out.write_text(
        json.dumps({"queries": list(names)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"resolver: wrote {len(names)} name queries -> {out}")
    return out


def resolve_from_serp(names: list[str], workdir: str | Path) -> list[dict]:
    """Read B1 output and resolve each name to a competitor domain.

    Reads: <workdir>/B1_serp_competitors/run_02_full.json (falls back to
           run_01_test.json if the full run hasn't been saved yet).
    Writes: <workdir>/competitors/domains.json (bare domain strings list),
            which is the file _curated_domains / build_B5 consumes.

    Returns a list of {"name": str, "domain": str | None, "note": str}.
    The `domain` is None when no non-directory result was found; the `note`
    explains why so the operator can verify manually before spending on B5.
    """
    workdir = Path(workdir)

    # --- load + index B1 output (shared readers — single source of truth) -----
    raw = load_b1_records(workdir)
    index = index_by_term(raw)

    # --- resolve each name ----------------------------------------------------
    results: list[dict] = []
    resolved_domains: list[str] = []

    for name in names:
        rec = index.get(name.lower().strip())
        if rec is None:
            results.append({
                "name": name,
                "domain": None,
                "note": "no B1 record matched this query — verify the B1 run completed",
            })
            continue

        organic: list[dict] = organic_of(rec)

        chosen_domain: str | None = None
        note: str = ""
        skipped: list[str] = []

        for r in organic:
            if not isinstance(r, dict):
                continue
            # [VERIFY] displayedUrl carries the true destination host; `url` is a
            # google.com/goto redirect for most results.
            displayed = r.get("displayedUrl") or ""
            d = _displayed_domain(displayed)
            if not d:
                # Fallback: use `url` only if it is not a Google redirect.
                url = r.get("url") or r.get("link") or ""
                url_domain = _domain(url)
                if url_domain and "google.com" not in url_domain:
                    d = url_domain
            if not d:
                continue
            if _is_directory(d):
                skipped.append(d)
                continue
            # First non-directory result wins.
            chosen_domain = d
            if skipped:
                skipped_str = ", ".join(skipped[:3])
                note = (
                    f"resolved from organic result #{len(skipped) + 1} "
                    f"(skipped directories: {skipped_str})"
                )
            else:
                note = "resolved from top organic result"
            break

        if chosen_domain is None:
            if skipped:
                skipped_str = ", ".join(skipped[:3])
                note = (
                    f"all {len(organic)} organic results were directories "
                    f"({skipped_str}, …) — verify manually"
                )
            else:
                note = "no organic results in B1 output — verify manually"

        results.append({"name": name, "domain": chosen_domain, "note": note})
        if chosen_domain:
            resolved_domains.append(chosen_domain)

    # --- write competitors/domains.json for build_B5 / _curated_domains ------
    comp_dir = workdir / "competitors"
    comp_dir.mkdir(parents=True, exist_ok=True)
    domains_path = comp_dir / "domains.json"
    domains_path.write_text(
        json.dumps(resolved_domains, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        f"resolver: {len(resolved_domains)}/{len(names)} names resolved "
        f"-> {domains_path}"
    )

    return results


# ---------------------------------------------------------------------------
# Inline verification test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import shutil
    import tempfile

    # Synthetic B1 fixture — mirrors the apify/google-search-scraper schema:
    # each result carries a `displayedUrl` (the true
    # destination, possibly as a breadcrumb or junk like 'followers') and a `url` that
    # is a google.com/goto redirect for most organic results.
    #
    # Cases exercised:
    #   (a) junk 'followers' displayedUrl at position 1 — must be skipped
    #   (b) directory breadcrumb (trustpilot) at position 2 — must be skipped
    #   (c) clean company domain at position 3 — wins
    #   (d) clean company domain at position 1 — wins immediately (Best Drains)
    #   (e) all results are directories / junk — domain=None (All-Blocked Drains)
    FIXTURE: list[dict] = [
        {
            "searchQuery": {"term": "Acme Plumbing", "device": "DESKTOP", "page": 1},
            "organicResults": [
                {
                    # (a) Junk displayedUrl — social follower count, not a URL. Must skip.
                    "title": "Acme Plumbing (@AcmePlumbing)",
                    "url": "https://www.google.com/goto?url=CAES_FAKE_REDIRECT_1==",
                    "displayedUrl": "47.8K+ followers",
                    "position": 1,
                    "type": "organic",
                },
                {
                    # (b) Breadcrumb directory — trustpilot skipped by _SKIP_DOMAINS.
                    "title": "Acme Plumbing Reviews",
                    "url": "https://www.google.com/goto?url=CAES_FAKE_REDIRECT_2==",
                    "displayedUrl": "https://www.trustpilot.com › ... › Plumber",
                    "position": 2,
                    "type": "organic",
                },
                {
                    # (c) Clean company domain — wins.
                    "title": "Acme Plumbing | Plumbing & Water Services",
                    "url": "https://www.google.com/goto?url=CAES_FAKE_REDIRECT_3==",
                    "displayedUrl": "https://www.acmeplumbing.com",
                    "position": 3,
                    "type": "organic",
                },
            ],
        },
        {
            "searchQuery": {"term": "Best Drains Plumbing", "device": "DESKTOP", "page": 1},
            "organicResults": [
                {
                    # (d) Clean company domain at position 1 — resolved immediately.
                    "title": "Best Drains Plumbing – Official Site",
                    "url": "https://www.google.com/goto?url=CAES_FAKE_REDIRECT_4==",
                    "displayedUrl": "https://www.bestdrains.com",
                    "position": 1,
                    "type": "organic",
                },
                {
                    # Directory breadcrumb — thumbtack skipped by _SKIP_DOMAINS.
                    "title": "Best Drains on Thumbtack",
                    "url": "https://www.google.com/goto?url=CAES_FAKE_REDIRECT_5==",
                    "displayedUrl": "https://www.thumbtack.com › k › best-drains › near-me",
                    "position": 2,
                    "type": "organic",
                },
            ],
        },
        {
            "searchQuery": {"term": "All-Blocked Drains Ltd", "device": "DESKTOP", "page": 1},
            "organicResults": [
                {
                    # Directory breadcrumb — bark.com skipped by _SKIP_DOMAINS.
                    "title": "All-Blocked Drains Ltd | Bark.com",
                    "url": "https://www.google.com/goto?url=CAES_FAKE_REDIRECT_6==",
                    "displayedUrl": "https://www.bark.com › en › gb › company",
                    "position": 1,
                    "type": "organic",
                },
                {
                    # (e) Junk displayedUrl — comment count, not a URL. Must skip.
                    "title": "All-Blocked Drains – Community Post",
                    "url": "https://www.google.com/goto?url=CAES_FAKE_REDIRECT_7==",
                    "displayedUrl": "10+ comments  ·  8 months ago",
                    "position": 2,
                    "type": "organic",
                },
                # No non-directory, non-junk result → tests "verify manually" path → domain=None
            ],
        },
    ]

    # Write fixture and run
    tmpdir = tempfile.mkdtemp(prefix="resolver_test_")
    try:
        b1_dir = Path(tmpdir) / "B1_serp_competitors"
        b1_dir.mkdir()
        (b1_dir / "run_01_test.json").write_text(
            json.dumps(FIXTURE, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        names = ["Acme Plumbing", "Best Drains Plumbing", "All-Blocked Drains Ltd"]
        write_name_queries(names, tmpdir)

        print("\n--- resolve_from_serp output ---")
        resolved = resolve_from_serp(names, tmpdir)
        for r in resolved:
            print(f"  {r['name']!r:30s}  domain={r['domain']!r:25s}  note={r['note']!r}")

        print("\n--- competitors/domains.json ---")
        domains_path = Path(tmpdir) / "competitors" / "domains.json"
        print(json.dumps(json.loads(domains_path.read_text()), indent=2))

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
