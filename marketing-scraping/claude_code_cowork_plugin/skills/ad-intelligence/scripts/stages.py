"""
Stage registry — the config-driven definition of every Apify stage the
ad-intelligence tool runs.

Four stages this tool supports:
  B1  google-search-scraper      — SERP, used for competitor DISCOVERY and for
                                    resolve→validate→confirm (name/domain →
                                    verified domain / Facebook Page / LinkedIn
                                    name), the pre-flight before any paid stage.
  B4  facebook-ads-library       — Meta Ad Library, by ADVERTISER PAGE (primary;
                                    resolved Facebook Page URLs) or by keyword
                                    (explicit `meta_search_terms` sweep only).
  B5  google-ads-scraper         — Google Ads Transparency, by VERIFIED domain.
  LI  linkedin-ads-scraper       — LinkedIn Ad Library, by full company name,
                                    worldwide (no country filter by default).

Each stage knows: which actor to call, how to BUILD its Apify input from the
config + the resolution report on disk (competitors/resolution.json,
competitors/domains.json), and how many records to EXPECT (for the pre-run
budget gate). run_phase.py consumes this registry. Nothing here spends money —
run_phase.py does, one stage at a time.

Supports both `country_codes` (list, preferred) and the legacy `country_code`
scalar. `country_codes` overrides `country_code`; both keys are back-compat.
The keyword-sweep B4 and B5 produce one URL per (country × term|domain); by-Page
B4 uses a single `scrapePageAds.countryCode` ("ALL" when multi-country). LI is
keyed by advertiser name and does not filter by country unless asked.
"""

import json
import re
from pathlib import Path
from urllib.parse import quote, unquote


def _q(value) -> str:
    """One value made safe to place in an address.

    Every character that is not a letter, digit or one of "_.-~" is
    percent-encoded, so a value can never end itself early or add a parameter:
    "AT&T internet" was sent as q=AT plus a stray "T internet" parameter, a
    search for something the user did not ask about. Ordinary domains and
    country codes come back unchanged.
    """
    return quote(str(value), safe="")


# --- small helpers to read the competitor list from the workdir ---------------

def _read_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.I)


def _bare_domain(d):
    """Normalize a URL or domain string to a bare lower-case host: no scheme (in any
    case), credentials, port, path or query, no leading www., no trailing dot. ''
    for anything that is not text.

    WHY one reading, case-insensitive: this used to replace only a lower-case
    "https://", so a `use` of "HTTPS://acme.com" became the site "https:", was written
    into domains.json and searched on Google (an empty result read as "runs no
    Google ads"). resolve.normalize_domain reads the same field the same way."""
    if not isinstance(d, str) or not d.strip():
        return ""
    h = _SCHEME_RE.sub("", d.strip()).lower()
    h = re.split(r"[/?#]", h, 1)[0].split("@")[-1]
    h = h.split(":")[0].rstrip(".")
    return h[4:] if h.startswith("www.") else h


_NOT_IN_A_HOST = re.compile(r"[\s\x00-\x1f\x7f%/\\@?#]")
_BLANK_IN_HOST = re.compile(r"[\s\x00-\x1f\x7f]")


def site_problem(value):
    """'' when `value` (a website as a person or a file wrote it) can be read as ONE
    website or is blank, else a short phrase saying why it cannot.

    A percent-escape is read ONCE, here, and only when what it spells is a plain
    website name (acme-example%2Ecom is acme-example.com). An escape that spells a '/',
    an '@', a space, a control character or another '%' is refused: the buyer's
    meaning cannot be told from a mistake, and the tool never searches a guess.

    WHY one place for it: the plan, the search addresses, the record of what was
    searched and every reader of `use` must see the SAME website. Each used to
    read the text its own way, so a `use` of "acme-example.com%2Fhome" was searched
    as that literal text while the record, which decoded it, said acme-example.com had
    been searched, and the closing lines then called a search this run had just paid
    for "not searched". "%00" gave a host with a NUL in it, and "%2540" a user@host
    form that credited one site's ads to another."""
    if not isinstance(value, str) or not value.strip():
        return ""
    text = value.strip()
    if "%" in text:
        decoded = unquote(text)
        if _NOT_IN_A_HOST.search(decoded):
            return ("it holds a percent-escape that spells something other than a plain "
                    "website name (a '/', an '@', a space, a control character or another '%')")
        text = decoded
    if _BLANK_IN_HOST.search(_bare_domain(text)):
        return "it holds a space or a control character"
    return ""


def site_host(value):
    """The one reading of a website: bare lower-case host (_bare_domain after the one
    percent-decode site_problem allows), '' when blank, not text, or refused by
    site_problem. Decodes exactly once: a doubly escaped value is refused, never
    decoded twice."""
    if site_problem(value):
        return ""
    text = value.strip() if isinstance(value, str) else ""
    return _bare_domain(unquote(text) if "%" in text else text)


def _derive_company_name(domain):
    """
    Derive a human-readable company name from a competitor domain.

    E.g. 'acmeplumbing.com' → 'Acmeplumbing', 'best-drains.com' → 'Best Drains'.

    Algorithm: strip scheme/www → remove TLD → split on hyphens/dots →
    Title Case each token → join with space.
    Used by build_LI when `competitor_names` is not supplied — a rough
    derived name is better than nothing, and the user can override via
    competitor_names in the config.
    """
    bare = _bare_domain(domain)
    if not bare:
        return domain
    # Strip TLD: drop the last dot-segment (e.g. "acmeplumbing.com" → "acmeplumbing")
    parts = bare.split(".")
    name_part = parts[0] if len(parts) == 1 else ".".join(parts[:-1])
    # Split remaining on hyphens and dots, Title Case each token
    tokens = [t for t in name_part.replace(".", "-").split("-") if t]
    return " ".join(t.capitalize() for t in tokens)


def _competitor_domains(workdir):
    """Domains from a B1 discovery run (competitor_domains.json). The fallback
    pool when the user supplied no list; prefer the resolved list below."""
    f = Path(workdir) / "B1_serp_competitors" / "competitor_domains.json"
    return _read_json(f, []) or []


def _site_key(value):
    """The comparable form of a website as written in a file: bare host, lower
    case, '' when blank or not text. WHY lower case: a buyer who types ACME.com in
    `use` and a file that holds acme.com name the same website; comparing the raw
    text called that a disagreement (and searched the capitalised form)."""
    return site_host(value)


def _use_of(entry):
    d = entry.get("domain")
    return _site_key(d.get("use")) if isinstance(d, dict) else ""


def resolution_shape_problem(data):
    """'' when `data` (the parsed competitors/resolution.json) is a list of competitor
    records in the shape the tool writes, else a short phrase naming what is wrong.

    WHY checked, entry by entry: a hand edit can leave valid JSON in the wrong shape
    ("domain": "acme.com" instead of a record, `use` written as a list). Read as
    if it were right, that crashed with a traceback, or searched the text
    "['acme.com']" as a website. A wrong shape is the user's damaged file, named
    plainly, never guessed at."""
    if not isinstance(data, list):
        return "it is not a list of competitors"
    for i, e in enumerate(data, 1):
        if not isinstance(e, dict):
            return f"entry {i} is not a competitor record"
        who = str(e.get("input") or f"entry {i}")
        dom = e.get("domain")
        if dom is not None and not isinstance(dom, dict):
            return f"the `domain` of '{who}' is not a record"
        use = (dom or {}).get("use")
        if use is not None and not isinstance(use, str):
            return f"the `use` of '{who}' is not text (one website, as text, is expected)"
        ga = (dom or {}).get("google_advertisers")
        if ga is not None and not isinstance(ga, (list, str)):
            return (f"the `google_advertisers` of '{who}' is neither a list of advertiser "
                    f"names nor one name as text")
        why = site_problem(use)
        if why:
            return f"the `use` of '{who}' ({use!r}) cannot be read as one website: {why}"
    return ""


def resolution_file_problem(path):
    """'' when the file at `path` (competitors/resolution.json) is absent or is a list
    of competitor records in the shape the tool writes, else a short phrase naming
    what is wrong: not valid JSON, empty, or one of resolution_shape_problem's.

    WHY one check before any spend: an unreadable file was taken for "no resolution
    yet" or "the competitor list changed", the competitors were looked up again
    (paid) and the buyer's hand edits were overwritten. A damaged edit is evidence
    of what the buyer meant that the tool cannot read, so it stops the run and says
    so; it never acts against it."""
    p = Path(path)
    if not p.exists():
        return ""
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return "the file cannot be opened as text"
    if not text.strip():
        return "the file is empty"
    try:
        data = json.loads(text)
    except ValueError:
        return "it is not valid JSON, often from a stray comma or quote"
    return resolution_shape_problem(data)


def _listed_domains(workdir):
    """The websites competitors/domains.json lists: (state, list, reason).

    state:
      'missing'     there is no file (the tool writes it);
      'empty'       a readable list with nothing in it (names no website, so it is
                    not a competing opinion about them; the tool writes it again);
      'unreadable'  the file exists but cannot be read as a list of websites (a
                    stray comma or quote, an empty file, a record in place of a
                    list, an entry that is not a website); `reason` says which;
      'listed'      the websites, de-duped, in order.
    Entries may be {"url": ...} objects or plain strings.

    WHY 'missing' means only "no file": an unreadable file used to be called
    missing, which the plan read as "nothing disagrees", so Google searched the
    resolver's pick although the buyer had edited this file to name another, and the
    tool then overwrote the file. A damaged edit is evidence of intent that cannot
    be read, so it is its own state and nothing is searched or written over it."""
    f = Path(workdir) / "competitors" / "domains.json"
    if not f.exists():
        return "missing", [], ""
    try:
        text = f.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return "unreadable", [], "the file cannot be opened as text"
    if not text.strip():
        return "unreadable", [], "the file is empty"
    try:
        listed = json.loads(text)
    except ValueError:
        return "unreadable", [], "it is not valid JSON, often from a stray comma or quote"
    if not isinstance(listed, list):
        return "unreadable", [], "it is not a list of websites"
    if not listed:
        return "empty", [], ""
    out = []
    for i, t in enumerate(listed, 1):
        raw = t.get("url") if isinstance(t, dict) else t
        if not isinstance(raw, str):
            return "unreadable", [], f"entry {i} is not a website"
        b = _site_key(raw)
        if b and b not in out:
            out.append(b)
    return "listed", out, ""


def google_domain_plan(workdir):
    """Which websites Google will search, and whether the two files that name
    them agree. ONE reading, used by the console, the closing lines, the gate
    and by _curated_domains (so what is said is what B5 does).

    The files: competitors/resolution.json (`domain.use`, which the user is told
    to edit) and competitors/domains.json (a list the tool writes beside it).
    `domain.written` is what the tool last wrote into domains.json for the entry.

    state:
      'none'      no resolution entries (discovery-only runs, older folders)
      'agree'     both files name the same websites (also: domains.json missing
                  or unreadable, which the tool writes again)
      'use_wins'  they differ, but domains.json still holds exactly what the tool
                  wrote, so only `use` was edited: the edit is the user's, and
                  domains.json is stale (sync_google_domains rewrites it)
      'disputed'  they differ and domains.json is NOT what the tool wrote (edited
                  by hand), or the entries carry no `written` (a 1.5.0 run wrote
                  them): either file may be the one the user meant, and the tool
                  cannot tell which. The competitors whose `use` is not in
                  domains.json are not searched.
      'domains_unreadable'
                  domains.json exists but cannot be read as a list of websites (a
                  damaged hand edit): treated as a dispute for EVERY competitor,
                  nothing is searched and the file is not written over;
      'corrupt'   resolution.json is not valid JSON
    WHY a dispute is not decided by a rule: guessing wrong searches a website the
    user did not mean, and an ad found there is counted as the competitor's. A
    competitor left unsearched is visible and fixable; a wrong one is silent.

    Keys: state; raw (the parsed file) and entries (its dict entries); chosen
    (every non-blank `use`, de-duped); searched (what B5 searches, None when
    there are no resolution entries); skipped ([(name, use)] not searched because
    of a dispute); listed (domains.json); unmatched (listed websites no entry
    uses, not searched); blank (competitors whose `use` is blank); error (corrupt only)."""
    plan = {"state": "none", "raw": [], "entries": [], "chosen": [], "searched": None,
            "skipped": [], "listed": [], "unmatched": [], "blank": [], "error": ""}
    try:
        raw = _resolution(workdir)
    except ValueError as exc:
        plan.update(state="corrupt", error=str(exc))
        return plan
    entries = [e for e in raw if isinstance(e, dict)]
    if not entries:
        return plan
    chosen = []
    for e in entries:
        b = _use_of(e)
        if b and b not in chosen:
            chosen.append(b)
    lstate, listed, lreason = _listed_domains(workdir)
    plan.update(raw=raw, entries=entries, chosen=chosen, listed=listed,
                searched=list(chosen))
    if lstate == "unreadable":
        plan.update(state="domains_unreadable", error=lreason, searched=[],
                    skipped=[((e.get("input") or "").strip() or "(unnamed)", _use_of(e))
                             for e in entries if _use_of(e)])
        return plan
    if lstate in ("missing", "empty") or set(listed) == set(chosen):
        plan["state"] = "agree"
        return plan
    doms = [e.get("domain") for e in entries]
    if all(isinstance(d, dict) and "written" in d for d in doms):
        written = {_site_key(d.get("written")) for d in doms} - {""}
        if set(listed) == written:
            plan["state"] = "use_wins"
            return plan
    plan["state"] = "disputed"
    listed_set = set(listed)
    plan["searched"] = [c for c in chosen if c in listed_set]
    plan["skipped"] = [((e.get("input") or "").strip() or "(unnamed)", _use_of(e))
                       for e in entries if _use_of(e) and _use_of(e) not in listed_set]
    plan["unmatched"] = [d for d in listed if d not in set(chosen)]
    # Competitors whose `use` is blank: when domains.json still names a website, that
    # is a dispute too (safe: nothing is searched for them), and the message must say
    # `use` is blank rather than that "no competitor uses" the website.
    plan["blank"] = [((e.get("input") or "").strip() or "(unnamed)") for e in entries
                     if not _use_of(e)]
    return plan


GOOGLE_SEARCHED_FILE = "searched_sites.json"


def _google_output_stamp(workdir):
    """sha256 of B5's saved full output, '' when it cannot be read. The record of what
    was searched carries it, so the record says WHICH output it belongs to."""
    import hashlib
    try:
        return hashlib.sha256((Path(workdir) / "B5_google_ads" / "run_02_full.json")
                              .read_bytes()).hexdigest()
    except OSError:
        return ""


def _google_site_and_region(rec_or_url):
    """(region, site) of a Google search address or of an ad that carries one in its
    originUrl / url; region is '' when the address has none, site '' when the address
    names none or names one that site_problem refuses."""
    from urllib.parse import parse_qs, urlparse
    urls = ([rec_or_url] if isinstance(rec_or_url, str) else
            [rec_or_url.get(k) for k in ("originUrl", "url")] if isinstance(rec_or_url, dict)
            else [])
    for u in urls:
        q = parse_qs(urlparse(str(u or "")).query)
        raw = (q.get("domain") or [""])[0]
        if raw:
            return ((q.get("region") or [""])[0].upper(), site_host(raw))
    return "", ""


def google_not_reached(start_urls, max_items, items, size=None):
    """The websites whose search MAY NOT have been reached: no ad of their own in
    `items` (B5's saved output), and a search address in a batch of searches whose
    ads together filled `max_items`. [] when no batch did.

    WHY: the Google actor's maxItems is a TOTAL for one actor run, not per search
    address (a paid test sent two websites with maxItems 25 and got exactly 25 ads),
    and a run holds up to B5_BATCH_SIZE addresses. When the ads of earlier addresses
    fill it, the rest of the batch can end without being searched, and the tool cannot
    see which; a website with no ads in such a batch is NOT "searched, no ads". The
    order the actor works through its addresses in is not documented, so the safe
    reading is the one that claims least: any website with no ads in a full batch
    may not have been reached. Batches are rebuilt here the way run_phase cuts them
    (consecutive chunks of `size`), and an ad is counted to the address it came
    from (region and website)."""
    return list(google_not_reached_detail(start_urls, max_items, items, size))


def google_not_reached_detail(start_urls, max_items, items, size=None):
    """{website: the number of OTHER websites whose searches shared its actor run},
    for the websites google_not_reached names, in order. The number goes into the
    sentence the buyer reads ("shared one run with 9 other websites")."""
    size = size or B5_BATCH_SIZE
    try:
        cap = int(max_items)
    except (TypeError, ValueError):
        return {}
    if cap <= 0 or not isinstance(items, list):
        return {}
    pairs = []
    for u in start_urls or []:
        region, site = _google_site_and_region((u or {}).get("url") if isinstance(u, dict) else u)
        pairs.append((region, site))
    per_pair, per_site, has_region = {}, {}, False
    for rec in items:
        region, site = _google_site_and_region(rec)
        if not site:
            continue
        has_region = has_region or bool(region)
        per_pair[(region, site)] = per_pair.get((region, site), 0) + 1
        per_site[site] = per_site.get(site, 0) + 1
    out = {}
    for i in range(0, len(pairs), size):
        chunk = pairs[i:i + size]
        n = [per_pair.get(p, 0) if has_region else per_site.get(p[1], 0) for p in chunk]
        if sum(n) < cap:
            continue
        sites = {p[1] for p in chunk if p[1]}
        for (region, site), k in zip(chunk, n):
            if site and not k and site not in out and not per_site.get(site):
                out[site] = len(sites) - 1
    return out


def record_google_searched(workdir, sites, b5_input=None):
    """Store, beside B5's saved full output, the websites that full run was asked
    to search, which output it belongs to, and the websites whose search may not
    have been reached (google_not_reached, from `b5_input`, the input B5 was built
    with). Called once, by the run that saved run_02_full.json.

    WHY a file of its own: a website whose search found no ads leaves no trace in
    the saved ads, so the ads alone cannot say it WAS searched, and the closing
    lines then called a website that had just been searched "not searched in
    this folder". The list is written only when the full output was saved by this
    run, so a test pass or a failed run never leaves one.
    WHY the output's hash is stored with it: run_phase.py can be run alone with
    --force and replace the output; a list kept from the earlier output would then
    describe a search that is not the one in the folder. google_searched_complete
    and google_searched_record trust the list only while the hash matches.
    The websites are read with site_host, once, so the list and the plan agree."""
    d = Path(workdir) / "B5_google_ads"
    clean = []
    # A text is ONE website; iterating it would record its letters as websites.
    for x in ([sites] if isinstance(sites, str) else sites or []):
        x = site_host(x)
        if x and x not in clean:
            clean.append(x)
    unreached = {}
    max_items = 0
    if isinstance(b5_input, dict):
        try:
            items = json.loads((d / "run_02_full.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            items = []
        max_items = b5_input.get("maxItems") or 0
        unreached = {x: n for x, n in google_not_reached_detail(
            b5_input.get("startUrls"), max_items, items).items() if x in clean}
    doc = {"searched": [x for x in clean if x not in unreached],
           "may_not_have_been_reached": list(unreached),
           "shared_run_with": unreached,
           "max_items": max_items if unreached else 0,
           "output_sha256": _google_output_stamp(workdir)}
    try:
        (d / GOOGLE_SEARCHED_FILE).write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass   # the record then falls back to the ads alone, and says less


def _google_searched_doc(workdir):
    """The stored record of what was searched, or None when it is missing, damaged,
    of an earlier version, not a list of non-empty strings, or belongs to a
    different output than the one in the folder now."""
    d = Path(workdir) / "B5_google_ads"
    try:
        doc = json.loads((d / GOOGLE_SEARCHED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    for key in ("searched", "may_not_have_been_reached"):
        v = doc.get(key)
        if not isinstance(v, list) or any(not isinstance(x, str) or not x.strip() for x in v):
            return None
    shared = doc.get("shared_run_with")
    if shared is not None and (not isinstance(shared, dict) or any(
            not isinstance(v, int) or isinstance(v, bool) for v in shared.values())):
        return None
    stamp = _google_output_stamp(workdir)
    if not stamp or doc.get("output_sha256") != stamp:
        return None
    return doc


def google_searched_complete(workdir):
    """True when the record of what Google was searched for is COMPLETE: the folder
    holds B5's saved full output and a stored record of its full run that is a list
    of non-empty websites, is not empty, and is stamped with THIS output
    (record_google_searched). A folder from an earlier version has the output only,
    so a website whose search found no ads is simply absent from the record, and
    "absent" must not be read as "not searched". An empty list, a list of anything
    but text, a damaged file and a record stamped for another output are all
    incomplete for the same reason: they are not evidence of what this output's run
    searched.

    WHY not also "covers the plan": the plan is what a rerun WOULD search. A
    website the plan has gained since is exactly what the closing lines must be able
    to call not searched in this folder, and a record that had to cover it could
    never say so."""
    try:
        json.loads((Path(workdir) / "B5_google_ads" / "run_02_full.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    doc = _google_searched_doc(workdir)
    return bool(doc and (doc["searched"] or doc["may_not_have_been_reached"]))


def google_searched_unreached(workdir):
    """The websites whose search may not have been reached in this folder's saved
    full run (google_not_reached), as the stored record names them; [] when there is
    no authentic record. Each is also absent from google_searched_record."""
    doc = _google_searched_doc(workdir)
    out = []
    for x in (doc or {}).get("may_not_have_been_reached", []):
        x = site_host(x)
        if x and x not in out:
            out.append(x)
    return out


def google_unreached_notes(workdir, planned=None):
    """The closing note for each website whose search may not have been reached in this
    folder's saved Google run, as plain sentences ([] when none, or when the stored
    record is not authentic). `planned`: when given, only websites the plan still
    names.

    WHY a note and not a gap: it is not a step that failed, it is a result that must
    not be read as a finding. The closing lines and the data digest carry it beside the
    results, where a competitor with no Google ads would otherwise read as "runs none".
    WHY these remedies (checked against the code): running THIS folder again skips
    its finished Google step, so only a new folder searches again; repeat.py prepare
    copies the settings, where google_max_items can be raised (the limit is per
    actor run) and the steps limited to Google ads; a folder whose only competitor is
    the website gives it a run of its own."""
    doc = _google_searched_doc(workdir)
    if not doc:
        return []
    shared = doc.get("shared_run_with") or {}
    cap = doc.get("max_items") or 0
    out = []
    for x in doc.get("may_not_have_been_reached", []):
        site = site_host(x)
        if not site or (planned is not None and site not in planned):
            continue
        n = shared.get(x)
        with_ = (f"its search shared one run with {n} other website{'s' if n != 1 else ''}"
                 if isinstance(n, int) and n > 0 else "its search shared one run with others")
        out.append(
            f"Google ads for {site} may not have been reached: {with_} and the run's "
            f"limit of {cap} ads was filled without any ad of {site}; its 0 is not a "
            f"finding. To search it, make a new folder from this one with repeat.py "
            f"prepare, keep only {site} as its competitor there, and limit its settings "
            f"to the Google ads step. The other way, raising google_max_items in that "
            f"copy, buys every other website's Google ads again and may still not reach "
            f"{site} if one advertiser has more ads than the new limit. Running this "
            f"folder again skips the finished Google step.")
    return out


def google_searched_record(workdir):
    """The websites Google WAS searched for in this folder, from what B5 SAVED:
    the websites its full run was asked to search and reached (record_google_searched)
    and the search website of every ad in B5_google_ads/run_02_full.json. Empty when
    that file does not exist or cannot be read, because then no full run finished here
    and nothing was searched.

    WHY only the saved output, never B5_google_ads/commands.sh: that file also
    lists the test pass and any run whose output was never saved (a failed or
    stopped one). Reading it made the closing lines say "Google searched <site>"
    for a website that only a discarded test run had touched.

    WHY the record and not the plan: the plan says what a rerun WOULD search.
    After a rerun in a folder whose Google step had already run, B5 is skipped,
    so the plan named a website that was never searched. Whatever the closing
    lines, the digest and the gap lines say about what was searched comes from
    here. Addresses are read with site_host (acme%2Ecom is acme.com): the search
    address is the scraper's and can carry any percent-encoding; one that does not
    spell a plain website is ignored."""
    out = []
    d = Path(workdir) / "B5_google_ads"
    try:
        items = json.loads((d / "run_02_full.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    for x in (_google_searched_doc(workdir) or {}).get("searched", []):
        site = site_host(x)
        if site and site not in out:
            out.append(site)
    for rec in items if isinstance(items, list) else []:
        if not isinstance(rec, dict):
            continue
        site = _google_site_and_region(rec)[1]
        if site and site not in out:
            out.append(site)
    return out


def sync_google_domains(workdir, plan):
    """Make domains.json (and each entry's `written`) say what Google searches,
    for a plan that is not disputed. 'use_wins' and a missing/stale domains.json
    are rewritten from `use`; entries a 1.5.0 run wrote get their `written`, so a
    later edit to one file can be told from an edit to the other (without it, an
    edit of `use` after this run would look like a dispute). A disputed plan is
    left exactly as the user has it: the fix is theirs. Returns True when a file
    was written."""
    if plan.get("state") not in ("agree", "use_wins"):
        return False
    comp = Path(workdir) / "competitors"
    moved = False
    for e in plan["entries"]:
        d = e.get("domain")
        if isinstance(d, dict) and d.get("written") != _use_of(e):
            d["written"] = _use_of(e)
            moved = True
    if moved:  # only `written` changed; the rest of the file is as the user left it
        (comp / "resolution.json").write_text(
            json.dumps(plan["raw"], ensure_ascii=False, indent=2), encoding="utf-8")
    lstate, listed, _ = _listed_domains(workdir)
    stale = lstate in ("missing", "empty") or set(listed) != set(plan["chosen"])
    if stale:
        (comp / "domains.json").write_text(
            json.dumps(plan["chosen"], ensure_ascii=False, indent=2), encoding="utf-8")
    return moved or stale


def _curated_domains(workdir):
    """The competitor domain list B5 scrapes.

    competitors/resolution.json is the source of truth when it has entries: the
    Google websites are its `domain.use` values (google_domain_plan), less any
    competitor whose `use` and domains.json disagree in a way the tool cannot
    settle. WHY resolution.json: it is the file the tool tells the user to check
    and edit at the confirmation gate, and the file the resolution report and the
    Google ownership check read. This function used to read competitors/domains.json
    instead, so a buyer who corrected a wrong `use` (as told) saw a report naming
    one website while Google searched another, with no sign of it.

    With no resolution entries (discovery-only runs, older folders) it falls back
    as before: `competitors/domains.json`, then a raw B1 discovery run.
    Entries may be {"url": ...} objects or plain domain strings."""
    plan = google_domain_plan(workdir)
    if plan["state"] == "corrupt":
        raise ValueError(plan["error"])
    if plan["searched"] is not None:
        return list(plan["searched"])
    resolved = _read_json(Path(workdir) / "competitors" / "domains.json", None)
    raw = ([t.get("url") if isinstance(t, dict) else t for t in resolved]
           if resolved else _competitor_domains(workdir))
    out, seen = [], set()
    for d in raw:
        b = site_host(d)
        if b and b not in seen:
            seen.add(b)
            out.append(b)
    return out


def _resolution(workdir):
    """The resolve→validate→confirm entries written by resolve.write_resolution
    (competitors/resolution.json). [] before resolution has run. Each entry
    carries the chosen `use` identifier + candidates + flags per ad source."""
    try:
        data = _read_json(Path(workdir) / "competitors" / "resolution.json", []) or []
    except ValueError:
        raise ValueError("competitors/resolution.json is not valid JSON, so the tool cannot "
                         "tell which websites to search. Fix the file (a stray comma or quote "
                         "is the usual cause) or delete it, then run the same command on "
                         "this folder.") from None
    problem = resolution_shape_problem(data)
    if problem:
        raise ValueError(f"competitors/resolution.json is not in the shape the tool writes "
                         f"({problem}), so the tool cannot tell which websites to search. "
                         f"Fix the file or delete it, then run the same command on this "
                         f"folder.")
    return data


def _resolved_facebook_pages(workdir):
    """Chosen Facebook Page URLs (one per competitor that resolved to a Page),
    de-duped, order-preserving. Drives Meta by-advertiser scraping."""
    out, seen = [], set()
    for e in _resolution(workdir):
        u = ((e.get("facebook") or {}).get("use") or "").strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _resolved_linkedin_names(workdir):
    """Chosen full registered LinkedIn company names, de-duped. Kept for the
    resolution report and as a legacy fallback only — the LI STAGE now scopes by
    NUMERIC id (see _resolved_linkedin_ids), because name/keyword matching in the
    Ad Library is fuzzy and returns OTHER advertisers."""
    out, seen = [], set()
    for e in _resolution(workdir):
        n = ((e.get("linkedin") or {}).get("use") or "").strip()
        if n and n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


def _resolved_linkedin_ids(workdir):
    """Chosen NUMERIC LinkedIn company ids, de-duped, order-preserving. Filled by
    resolve.enrich_linkedin_ids AFTER resolution (a free GET of the public company
    page). The LinkedIn Ad Library scraper scopes to an advertiser ONLY by numeric
    id; a name/slug search is fuzzy content-matching that surfaces unrelated
    advertisers. [] before enrichment or when no id resolved — the caller
    then skips LI rather than scraping the wrong target."""
    out, seen = [], set()
    for e in _resolution(workdir):
        cid = str((e.get("linkedin") or {}).get("company_id") or "").strip()
        if cid and cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


def meta_words(terms):
    """The search words in `terms` that are real words, in order, as text.

    WHY: a word that is only whitespace (" ")
    used to pass the `if t` filters, so a paid Meta search fired with a blank
    query and the run claimed Meta was searched by the user's words. Blank means
    absent: this is the one reading build_B4, ad_intel and the resolution report
    use. A text word that has letters is kept exactly as written (its search
    input must not change).

    A `null` in the config (None) is absent, as it was
    under base's `if t`; reading it as str(None) == "None" kept it as a word and
    crashed the resolution report. Any value that is not text follows base's `if t`:
    empty (0, false, [], {}) is absent, and one with content (a number, say) is
    kept and returned as its text, which is exactly what base put in the search
    URL, so the paid calls do not move and nothing downstream joins a non-text."""
    return [t if isinstance(t, str) else str(t) for t in (terms or [])
            if (t.strip() if isinstance(t, str) else t)]


def meta_search_words(cfg):
    """The config's `meta_search_terms` that are real words (see meta_words)."""
    return meta_words(cfg.get("meta_search_terms"))


def linkedin_countries(cfg):
    """The LinkedIn country filter exactly as build_LI sends it (`countries`,
    upper-case codes). [] means no filter: every country is searched."""
    return [c.upper() for c in (cfg.get("linkedin_countries") or []) if c]


_COUNTRY_WORDS = {"GB": "the UK", "UK": "the UK", "US": "the US"}


def linkedin_where(cfg):
    """' in the UK' (or ' in the UK and DE') when the LinkedIn search was limited
    to some countries, '' when it was not. WHY: in a real run (linkedin_countries ["gb"]) a
    LinkedIn zero was described as a worldwide search
    and a fact about the company. A zero from a search limited to some countries
    says nothing about the rest, so every zero line names the filter."""
    words = []
    for c in linkedin_countries(cfg):
        w = _COUNTRY_WORDS.get(c, c)
        if w not in words:
            words.append(w)
    if not words:
        return ""
    if len(words) == 1:
        return f" in {words[0]}"
    return " in " + ", ".join(words[:-1]) + " and " + words[-1]


# --- per-stage input builders -------------------------------------------------
# Each returns (actor_input_dict, expected_record_count). `test` truncates inputs.

def build_B1(cfg, workdir, test):
    """SERP. Queries come from an explicit list the caller writes to
    B1_serp_competitors/queries.json (company names for mode-2 resolution, or
    seed terms for mode-3 discovery); falls back to cfg['seed_terms']."""
    qf = Path(workdir) / "B1_serp_competitors" / "queries.json"
    queries = (_read_json(qf, {}) or {}).get("queries", []) or cfg.get("seed_terms", [])
    queries = queries[:5] if test else queries
    inp = {
        "queries": "\n".join(queries),
        "resultsPerPage": 10, "maxPagesPerQuery": 1,
        "countryCode": cfg["country_code"], "languageCode": cfg.get("language", "en"),
    }
    return inp, len(queries)


# How many ads past `limitPerSource` the Meta actor may return, and bill, per
# input URL. Its own input schema says of that field: "Actual number might
# exceed the given limit by upto 30" (curious_coder/facebook-ads-library-scraper). The paid runs
# agree: limit 20 returned and
# billed 29, 30 and 30 ads for one page, limit 100 returned and billed 120.
# WHY it is in the count: build_B4's count is what the forecast, the spend cap
# and the standing-approval gate price, and they promise "at most". Priced at
# the limit alone, a limit-20 forecast of $0.01805 was beaten by a $0.02255 bill.
META_LIMIT_OVERSHOOT = 30


def build_B4(cfg, workdir, test):
    """Meta Ad Library — by ADVERTISER PAGE (primary) or by KEYWORD (explicit sweep).

    Primary path (the company-research intent): scrape the resolved competitors'
    own Facebook Pages. The curious_coder actor's `urls` field accepts a Page URL
    directly (verified against its input schema), and `scrapePageAds.*` (flat
    dot-keys, per the actor's own example) controls the per-page scrape. Country
    rides `scrapePageAds.countryCode`, which accepts a 2-letter code or "ALL" —
    so a single-country run pins the market and a multi-country run uses "ALL"
    (the advertiser's ads across every country) rather than inventing per-URL
    country behavior the actor does not document.

    Keyword sweep (secondary, EXPLICIT opt-in via `meta_search_terms`): the old
    whole-market keyword search. It is no longer the default — an unfiltered
    keyword sweep is exactly what returned ~45 off-target hitchhikers and never
    the target's own ads. It runs only when the user asks for it by supplying
    `meta_search_terms`.

    Returns ({"urls": []}, 0) when NO advertiser page resolved and NO sweep terms
    were supplied — the caller skips B4 in that case rather than silently
    sweeping. Never falls back to a market sweep on its own.
    """
    countries = [c.upper() for c in (cfg.get("country_codes") or [cfg["country_code"]])]
    cap = 20 if test else int(cfg.get("meta_limit_per_source", 200))

    pages = _resolved_facebook_pages(workdir)
    sweep_terms = meta_search_words(cfg)

    # --- primary: by advertiser Page ---
    if pages:
        pages = pages[:3] if test else pages
        country_code = countries[0] if len(countries) == 1 else "ALL"
        inp = {
            "urls": [{"url": p} for p in pages],
            "scrapePageAds.activeStatus": "all",
            "scrapePageAds.sortBy": "impressions_desc",
            "scrapePageAds.countryCode": country_code,
            "limitPerSource": cap,
        }
        return inp, (cap + META_LIMIT_OVERSHOOT) * len(pages)

    # --- secondary: explicit keyword sweep ---
    if sweep_terms:
        sweep_terms = sweep_terms[:3] if test else sweep_terms
        urls = [{"url": f"https://www.facebook.com/ads/library/?active_status=all&ad_type=all"
                        f"&country={_q(cc)}&q={_q(t)}&search_type=keyword_unordered"
                        f"&media_type=all"}
                for cc in countries for t in sweep_terms]
        return ({"urls": urls, "limitPerSource": cap},
                (cap + META_LIMIT_OVERSHOOT) * len(urls))

    # --- no target: never sweep silently ---
    return {"urls": []}, 0


def build_LI(cfg, workdir, test):
    """LinkedIn Ad Library scoped by NUMERIC company id (scrapesage actor).

    Why numeric id and not name (a hard-won lesson): the LinkedIn Ad
    Library's name/keyword/slug search is fuzzy CONTENT matching — searching
    "hubspot" returns thousands of ads, but many belong to OTHER advertisers who
    merely mention HubSpot, and searching "HubSpot"/"HubSpot, Inc." returned a
    silent 0 (name/keyword search is unreliable by design).
    The only precise advertiser scoping the Ad Library offers is the advertiser's
    numeric company id. `companyIds:["68529"]` returns HubSpot's ads and only
    HubSpot's — verified live, 6/6.

    Where the id comes from: resolve.enrich_linkedin_ids fills each entry's
    `linkedin.company_id` after the resolution report, by one free GET of the
    public company page (the id is embedded there — no paid actor, no login). So
    the id is available on disk by the time this stage runs.

    Returns ({"companyIds": []}, 0) when NO numeric id resolved — the caller
    then SKIPS LinkedIn rather than falling back to a fuzzy name search that would
    scrape the wrong advertiser. This is the whole point of the rebuild: no silent
    wrong-target. There is no name/keyword fallback by design.

    enrichAdDetails:True is required — without it the actor returns only ad ids,
    not advertiserName/body/impressions. No OCR needed: copy comes back as text.
    Creatives (imageUrls, videoUrl) are media.licdn.com URLs downloaded right
    after the stage (some carry expiry timestamps).
    """
    ids = _resolved_linkedin_ids(workdir)
    ids = ids[:3] if test else ids
    if not ids:
        # No numeric id => no precise target. Never fall back to fuzzy name search.
        return {"companyIds": []}, 0

    max_ads = 25 if test else int(cfg.get("linkedin_max_items", 100))
    inp = {
        "companyIds": ids,
        "maxAds": max_ads,
        "maxAdsPerQuery": max_ads,
        "enrichAdDetails": True,
    }
    # Country filtering is OPT-IN only (default: worldwide). scrapesage accepts an
    # optional `countries` list; omit it unless the user explicitly asks.
    explicit = [c.upper() for c in (cfg.get("linkedin_countries") or []) if c]
    if explicit:
        inp["countries"] = explicit
    return inp, max_ads * len(ids)


def build_B5(cfg, workdir, test):
    """Google Ads Transparency, one by-domain URL per competitor. `region` rides
    country code(s). Batched (see STAGES["B5"]) — ~50 domain-searches otherwise
    overrun the actor timeout. Ad DURATION (firstShownAt->lastShownAt) is the
    profitability proxy.
    Supports multi-country: `country_codes` list preferred; falls back to
    single `country_code` for back-compat. Produces one URL per (country × domain).
    """
    countries = [c.upper() for c in (cfg.get("country_codes") or [cfg["country_code"]])]
    domains = _curated_domains(workdir)
    domains = domains[:2] if test else domains
    start = [{"url": f"https://adstransparency.google.com/?region={_q(cc)}&domain={_q(d)}"}
             for cc in countries for d in domains]
    # In test mode, cap maxItems — not just the domain count. Without this cap
    # the actor can scrape hundreds of ads instead of the intended test sample
    # (823 ads at $1.10 vs the expected ~$0.04 in one early test run).
    # Keep 25 in test so a sample arrives fast and cheaply.
    # Full-run cap is config-overridable (`google_max_items`). Default 2000.
    max_items = 25 if test else int(cfg.get("google_max_items", 2000))
    # Google Ads Transparency's SearchService RPC blocks DATACENTER IPs with
    # HTTP 429. Passing bare {"useApifyProxy": True} selects the datacenter
    # pool by default, so every request is 429'd (0 ads, ~6 min of retries,
    # wasted compute). The actor's OWN input-schema default is RESIDENTIAL —
    # match it. Where the run is a single country, pin the proxy exit country
    # to the market so requests look local (Apify guidance for this target);
    # with multiple countries in one run we let residential rotate.
    proxy = {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"]}
    if len(countries) == 1:
        proxy["apifyProxyCountry"] = countries[0]
    # Expected count = the configured ceiling: `maxItems` per ACTOR RUN, times the
    # number of runs the stage really makes (startUrls are batched B5_BATCH_SIZE
    # to a run, see STAGES["B5"]). The actor's maxItems is a total per run, not per
    # domain: paid test 05b sent 2 domains with maxItems 25 and got exactly 25 ads.
    # WHY: this count sizes the forecast, the budget gate and the Apify spend cap
    # (run_phase: forecast x CHARGE_CEILING_FACTOR). It used to be a flat 15 per
    # domain, whatever google_max_items said. In a real run (a UK search, google_max_items 100, 1
    # domain): forecast ~15 ads / $0.04, cap $0.07, and
    # the run was stopped over budget at 83 of the 100 ads it was asked for.
    # Counting maxItems per domain instead would inflate an N-domain run N-fold
    # (2 domains at the default 2000 forecast ~$12 and failed the $10 budget).
    # The batch spec's "per_run" flag makes run_phase give every batch one run's
    # share of the cap, so a short last batch can still reach maxItems.
    n_runs = -(-len(start) // B5_BATCH_SIZE)  # ceil: runs run_phase will launch
    return ({"startUrls": start, "maxItems": max_items,
             "proxyConfiguration": proxy}, n_runs * max_items)


# --- registry -----------------------------------------------------------------

# ~50 Google-Ads domain-searches in one run overrun the actor's timeout;
# batch B5's startUrls into chunks of this size.
B5_BATCH_SIZE = 10

STAGES = {
    "B1": {"name": "Google search for competitors", "actor": "apify/google-search-scraper",                "phase": "1", "folder": "B1_serp_competitors", "build": build_B1, "output": "dataset"},
    "B4": {"name": "Meta ad library", "actor": "curious_coder/facebook-ads-library-scraper", "phase": "1", "folder": "B4_meta_ad_library",  "build": build_B4, "output": "dataset", "memory_mb": 1024, "mem_per_url": 512},
    "B5": {"name": "Google ads", "actor": "lexis-solutions/google-ads-scraper",         "phase": "1", "folder": "B5_google_ads",       "build": build_B5, "output": "dataset", "batch": {"key": "startUrls", "size": B5_BATCH_SIZE, "per_run": True},
           # Google Ads Transparency stops when it has no more ads (a run that returns fewer than maxItems
           # has found them all), so run_phase may run a small first pass when the full forecast does not fit the cap.
           "first_pass_when_over_cap": True},
    # LI is NOT in PHASE_ORDER — it is invoked explicitly (like B4/B5)
    # because it is opt-in (include_linkedin config flag) and has no mem_per_url constraint.
    "LI": {"name": "LinkedIn ad library", "actor": "scrapesage/linkedin-ad-library-scraper",     "phase": "1", "folder": "LI_linkedin_ads",    "build": build_LI, "output": "dataset"},
}

# Default run order. B1 (discovery) is invoked on demand, not in the default ad-scrape order.
PHASE_ORDER = {
    "1": ["B4", "B5"],
}

QUESTION = {
    "B1": "Who ranks/advertises for these queries? (discovery + name->domain)",
    "B4": "Who advertises on Meta? What angles? How long running?",
    "B5": "Who bids on Google Ads and for how long (profitability proxy)?",
    "LI": "Who advertises on LinkedIn? What copy/angles? How many impressions?",
}

# Input dependencies. B5 needs a competitor domain list — supplied via config,
# resolver, or B1 discovery (not necessarily a B1 run).
NEEDS = {
    "B1": [], "B4": [], "B5": ["B1"], "LI": [],
}


def selected_stages(cfg):
    """The config's task allow-list, or None to mean 'run all defaults'."""
    sel = cfg.get("stages")
    return set(sel) if sel else None
