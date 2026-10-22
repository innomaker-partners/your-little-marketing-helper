"""
compare.py -- cross-run comparison for the local viewer.

The ONE comparison module.  Used by:
  - GET /api/compare?run=<id>[&against=<id>]  (viewer_server.py)
  - viewer_ctl.py compare
  - the app (via the API; the app never recomputes in JavaScript)

Design rule (FROZEN): "what changed" is plain code, with no model.
The comparison lives in one viewer module, used by the server API, the app
and the viewer_ctl compare command.  Why: a count the user checks must be
reproducible, and it must be the same in the app and in the agent's answer.

Stdlib only.  Read-only (never writes any file).

Public API:
    compare_runs(root, run_id, against=None) -> dict

    The output format is documented in DATA_MODEL.md.
"""

import json
import os
from pathlib import Path
from urllib.parse import unquote

# Keys excluded from settings comparison (they change per-run by design, not
# by the user changing the research scope).
_SETTINGS_IGNORE_KEYS = frozenset(["repeat_of", "subject"])
# Prefixes of budget/spend keys (these change per-run from the standing-approval
# mechanism and are not "settings" the user deliberately changed).
_SETTINGS_BUDGET_PREFIXES = ("budget", "ceiling", "max_total_charge")

# Platform display names for human-readable output.  Matches PLATFORM in
# app.js -- keep in sync when adding new platforms.
_PLAT_LABEL = {
    "meta": "Meta", "google": "Google", "linkedin": "LinkedIn",
    "trustpilot": "Trustpilot", "g2": "G2", "capterra": "Capterra",
    "reddit": "Reddit", "maps": "Google Maps", "appstore": "App Store",
    "googleplay": "Google Play", "semrush": "Semrush",
    "google_serp": "Google",
}


def _rec_noun(tool, count):
    """Singular or plural noun for a record, appropriate for the tool type."""
    if tool == "pain":
        return "review" if count == 1 else "reviews"
    if tool == "ad":
        return "ad" if count == 1 else "ads"
    return "record" if count == 1 else "records"


# --------------------------------------------------------------------------
# Key fields that define "changed" per tool.
# Adjustable: documented here AND in DATA_MODEL.md.  A future maintainer may add
# fields; doing so requires updating DATA_MODEL.md.
# --------------------------------------------------------------------------
# pain: star rating and review text.
_PAIN_KEY_FIELDS = ("data.stars", "text")
# ad: running/ended status, last_shown date, headline, ad body text.
_AD_KEY_FIELDS = ("tags.active", "data.last_shown", "data.headline", "text")
# search keyword: volume, cpc, difficulty.
_SEARCH_KW_KEY_FIELDS = ("data.volume", "data.cpc_usd", "data.keyword_difficulty")
# search serp: the ordered list of result domains (position change counts).
_SEARCH_SERP_KEY_FIELDS = ("data.results_domains",)   # virtual: derived below
# domain records are summary roll-ups recomputed from serp; not compared field-by-field.

_CONFIG_NAMES = ("PAIN_CONFIG.json", "AD_INTEL_CONFIG.json", "SEARCH_CONFIG.json")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _get_nested(obj, dotted_key):
    """Get obj["a"]["b"] from "a.b".  Returns None if any key is missing."""
    parts = dotted_key.split(".", 1)
    if not isinstance(obj, dict):
        return None
    v = obj.get(parts[0])
    if len(parts) == 1:
        return v
    return _get_nested(v, parts[1])


def _key_values(rec, tool):
    """Return a dict of {field: value} for the key fields of this tool/kind."""
    kind = rec.get("kind")
    if tool == "pain":
        fields = _PAIN_KEY_FIELDS
    elif tool == "ad":
        fields = _AD_KEY_FIELDS
    elif tool == "search":
        if kind == "keyword":
            fields = _SEARCH_KW_KEY_FIELDS
        elif kind == "serp":
            # Virtual field: extract the ordered list of domain names from data.results.
            results = (_get_nested(rec, "data.results") or [])
            domains = [r.get("domain") for r in results if isinstance(r, dict)]
            return {"data.results_domains": domains}
        else:
            # domain records: not compared field-by-field
            return {}
    else:
        return {}
    return {f: _get_nested(rec, f) for f in fields}


def _changed_fields(rec_a, rec_b, tool):
    """Return sorted list of key-field names that differ between two records."""
    va = _key_values(rec_a, tool)
    vb = _key_values(rec_b, tool)
    return sorted(k for k in va if va.get(k) != vb.get(k))


def _read_config(run_dir):
    """Return the config dict for a run, or {} if not found."""
    for name in _CONFIG_NAMES:
        p = Path(run_dir) / name
        if p.exists():
            return _read_json(p, {}) or {}
    return {}


def _is_budget_key(key):
    k = key.lower()
    return any(k.startswith(p) for p in _SETTINGS_BUDGET_PREFIXES)


def _compare_configs(cfg_a, cfg_b):
    """Return (settings_differ, differing_top_level_keys).

    Ignores repeat_of, subject, and any key whose name starts with a
    budget/spend prefix.  Both configs are treated as dicts; missing keys
    are equivalent to None.
    """
    ignore = _SETTINGS_IGNORE_KEYS
    keys = (set(cfg_a) | set(cfg_b)) - ignore
    keys = {k for k in keys if not _is_budget_key(k)}
    differing = sorted(k for k in keys if cfg_a.get(k) != cfg_b.get(k))
    return bool(differing), differing


# --------------------------------------------------------------------------
# Baseline selection
# --------------------------------------------------------------------------

def _load_index(run_dir):
    """Load and return the frontend_index.json for a run, or None."""
    return _read_json(Path(run_dir) / "frontend_index.json")


def _run_sort_key(run_dir, run_id, index):
    """Ordering key for a run: (run_date, run_version, generated_at, run_id)."""
    name = Path(run_dir).name if run_id else run_id
    date, version = _parse_run_folder(name)
    gen = index.get("generated_at") if isinstance(index, dict) else ""
    if date is None and gen and isinstance(gen, str) and len(gen) >= 10:
        candidate = gen[:10]
        if _DATE_RE.match(candidate):
            date = candidate
    return (date or "0000-00-00", version, gen or "", run_id)


import re
_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_VN_RE = re.compile(r'^v\d+$', re.IGNORECASE)


def _parse_run_folder(name):
    """Parse (date_str, version_int) from a run folder name."""
    parts = name.split("_")
    if len(parts) < 3 or parts[0] not in ("adintel", "ad", "pain", "search"):
        return None, 1
    for i, part in enumerate(parts):
        if _DATE_RE.match(part):
            after = parts[i + 1:]
            version = 1
            if after and _VN_RE.match(after[0]):
                try:
                    version = int(after[0][1:])
                except ValueError:
                    pass
            return part, version
    return None, 1


_PARTIAL_BASELINE_SENTENCE = (
    "Note: the earlier month used for this comparison stopped before it finished, so "
    "it holds only part of its records. A record it does not have may simply not have "
    "been collected, so read the new and no-longer-there counts with that in mind.")


# The same note for the month being looked at, when it is the one that stopped.
# Only the "no longer there" count is made unsafe by it: a record this month does not
# have may simply not have been collected. The new and changed counts describe records
# this month does have, so they stay as they are.
_PARTIAL_RUN_SENTENCE = (
    "Note: this month stopped before it finished, so it holds only part of its "
    "records. A record it does not have may simply not have been collected, so read the "
    "no-longer-there count with that in mind.")


def _skipped_sentence(skipped, against_id):
    """Said when the earlier month the comparison would have used (the one this
    month repeats, or a more recent month of the same project) stopped before it
    finished and so was passed over, and an older month was used instead."""
    many = len(skipped) > 1
    names = ", ".join(skipped[:-1]) + " and " + skipped[-1] if many else skipped[0]
    return (f"Note: {names} stopped before it finished, so "
            f"{'they were' if many else 'it was'} not used as the earlier month. "
            f"This month is compared with {against_id} instead.")


def _is_partial(run_dir):
    """True when the run's own record of how it ended says it stopped before it
    finished (notification.json, status "incomplete"): the month holds only part of
    its records.  A scheduled run writes it; so does an attended run stopped at the
    spend cap.  A missing, unreadable or other-status file reads as not
    partial.  The same test as viewer_server._read_notification, kept here because
    this module is stdlib-only and viewer_server imports it, not the other way."""
    n = _read_json(Path(run_dir) / "notification.json", None)
    return isinstance(n, dict) and n.get("status") == "incomplete"


def _find_baseline(root, run_id, index, skipped=None):
    """Find the baseline run (see DATA_MODEL.md, baseline selection).

    Returns (against_run_id, basis) where basis is one of:
      "repeat_of"    -- the run's own repeat_of field
      "same_project" -- the previous run of the same tool+subject by run order
      None           -- no baseline found (against_run_id is also None)

    A month that stopped before it finished
    (_is_partial) is never chosen here, by either rule.  WHY: its records are only
    part of the month, so against it the next full month reads "329 new", and a
    comparison that meets it reads the records it never collected as "no longer
    there"; neither is true.  repeat_of that names such a month falls through to the
    same-project rule, which also skips it.  An explicit --against still compares
    (the user asked), and compare_runs says then that the month is partial.

    `skipped`, when a list is given, receives the ids of the stopped months
    this call passed over (the one repeat_of names, and the months of the same project
    between the run and the baseline it chose, or all of the run's earlier months when
    it chose none), so the answer can say which month was not used and why instead of
    "there is no earlier month".  A stopped month of another project is not one of them.
    """
    root = Path(root).resolve()

    # Check repeat_of
    repeat_of = None
    if isinstance(index, dict):
        repeat_of = index.get("repeat_of") or None
    if repeat_of is None:
        cfg = _read_config(root / run_id)
        if isinstance(cfg, dict):
            repeat_of = cfg.get("repeat_of") or None
    if repeat_of:
        candidate = root / repeat_of
        if candidate.is_dir() and not _is_partial(candidate):
            return repeat_of, "repeat_of"
        if candidate.is_dir() and skipped is not None:
            skipped.append(repeat_of)
        # repeat_of points to a missing folder, or to a month that stopped early;
        # fall through to same_project, which skips stopped months too.

    # Find previous run with same tool+subject
    if not isinstance(index, dict):
        return None, None
    tool = index.get("tool")
    subject = index.get("subject")
    if not tool or not subject:
        return None, None

    # Walk the root for candidate runs
    candidates = []
    stopped = []   # Months of this project that stopped early (never chosen)
    for entry in os.scandir(root):
        if not entry.is_dir():
            continue
        if entry.name in (".viewer", ".git", "__pycache__", "node_modules"):
            continue
        if entry.name == run_id:
            continue
        cidx = _load_index(entry.path)
        if not isinstance(cidx, dict):
            continue
        if cidx.get("tool") != tool or cidx.get("subject") != subject:
            continue
        if _is_partial(entry.path):   # A stopped month is never a baseline
            stopped.append((entry.name, cidx))
            continue
        candidates.append((entry.name, cidx))

    # Sort by the run ordering key; take the latest one that comes BEFORE run_id
    my_key = _run_sort_key(root / run_id, run_id, index)
    key_of = lambda t: _run_sort_key(root / t[0], t[0], t[1])  # noqa: E731
    earlier = sorted((t for t in candidates if key_of(t) < my_key), key=key_of)
    chosen = earlier[-1] if earlier else None
    if skipped is not None:
        # The stopped months of this project that sit between the baseline
        # (or the start, when none was chosen) and this run, newest first.
        passed = sorted((t for t in stopped
                         if key_of(t) < my_key and (chosen is None or key_of(t) > key_of(chosen))),
                        key=key_of, reverse=True)
        for t in passed:
            if t[0] not in skipped:
                skipped.append(t[0])
    if chosen is None:
        return None, None
    return chosen[0], "same_project"


# --------------------------------------------------------------------------
# Core comparison
# --------------------------------------------------------------------------

def _platforms_in_run(records):
    """Return the set of platforms that appear in a list of records."""
    return {r.get("platform") for r in records if isinstance(r, dict) and r.get("platform")}


def _set_aside_ids(index):
    """{(platform, uid)} of the ads a run set aside as another advertiser's
    (the ad tool's index "set_aside" entry), and {platform: count}.

    WHY: a set-aside ad is not in the index's records, so a
    comparison that only reads records reports an ad that moved from shown to
    set aside as "no longer there", and the reverse (after the user lists an
    advertiser name) as "new". Neither is true: the filter changed, not the
    competitor's advertising. The uids are the ones these ads would have if
    shown, so they match the other month's records directly. An index from
    an earlier version (count and lines, no uids) gives counts only."""
    ids, counts = set(), {}
    sa = index.get("set_aside") if isinstance(index, dict) else None
    if isinstance(sa, dict):
        for p, v in sa.items():
            if not isinstance(v, dict):
                continue
            if isinstance(v.get("count"), int) and v["count"] > 0:
                counts[p] = v["count"]
            for u in v.get("uids") or []:
                if isinstance(u, str) and u:
                    ids.add((p, u))
    return ids, counts


def _trashed_uids(run_dir):
    """Return the set of uids trashed in a run (from .viewer/annotations.json)."""
    ann = _read_json(Path(run_dir) / ".viewer" / "annotations.json", {}) or {}
    trashed = ann.get("trashed") or {}
    return {uid for uid, v in trashed.items() if v}


def _tombstone(r):
    """Return True for records the app suppresses from the review wall.

    Matches viewer_ctl._tombstone and app.js isRedditTombstone exactly:
    - platform must be "reddit"
    - tags.removed is True (strict boolean, not merely truthy), OR
    - text is exactly "[removed]" or "[deleted]" (case-insensitive, stripped)

    Caller is responsible for the tool guard: apply only when tool == "pain",
    because the tombstone filter only runs on the pain/review-wall page.

    This is the SINGLE definition of this rule.  viewer_ctl imports it from here
    so there is no second copy to drift.
    """
    return (r.get("platform") == "reddit"
            and ((r.get("tags") or {}).get("removed") is True
                 or (r.get("text") or "").strip().lower() in ("[removed]", "[deleted]")))


def _domain_name(rec):
    """The domain a search "domain" row stands for (uid domain:<name>, text
    <name>, as search emit_index writes it)."""
    uid = rec.get("uid") or ""
    if uid.startswith("domain:"):
        return uid[len("domain:"):]
    return rec.get("text")


def _has_results(serp_rec):
    """True if a search-results record names at least one ranking domain.
    A record with no results is a pull that came back empty (a failed or empty pull,
    or a keyword Google has nothing for): the tool knows nothing about the ranking."""
    return bool(_domains_ranked({"k": serp_rec}, ["k"]))


def _domains_ranked(serp_by_uid, keyword_uids):
    """{domain} named in the results of the given search-results records
    (data.results[].domain), for the keywords in keyword_uids only."""
    out = set()
    for uid in keyword_uids:
        for x in ((serp_by_uid.get(uid) or {}).get("data") or {}).get("results") or []:
            if isinstance(x, dict) and x.get("domain"):
                out.add(x["domain"])
    return out


# --------------------------------------------------------------------------
# What a source's own library said about a page (index "source_notes")
# --------------------------------------------------------------------------
# WHY: the Meta scraper answers a page it cannot show ads for with ONE status item
# instead of ads: "Ads not found" (the library itself says the page is not running
# ads) or an error (rate limited, blocked, page not found: the ads are UNKNOWN). The
# ad tool keeps those items out of the records and writes them to the index as
# "source_notes" (emit_index). A comparison that only reads records saw a month with
# no ads from that page and called last month's ads "no longer there": for an ERROR
# that is a false statement, because the tool never learned what the page runs. So:
#   - an error this month for a page: last month's ads from that page are "cannot
#     tell", not "gone" (and the mirror: an error last month for a page, this
#     month's ads from it are "cannot tell", not "new");
#   - "Ads not found" for a page: the library said so, so last month's ads from it
#     really are no longer running; counted as gone, with the library's statement
#     attributed to it.
# An ad is tied to a page by the page fields the index carries (data.page_id,
# data.page_slug, data.advertiser_name). When BOTH sides carry a page id the ids decide:
# two pages can share a name (and a name match must never turn page 55's error into a
# statement about page 77). Where an ad cannot be tied to a page the library answered
# for, and any note exists that cannot be tied to some other page (an error, or an
# "Ads not found" with no page named), the ad is "cannot tell" too. That is the safe
# direction: "no longer there" is a statement about a competitor's market and a false one
# is worse than a visible "cannot tell".

def _alnum(value):
    """Lower-case letters and digits only, so 'Acme Plumbing' and 'AcmePlumbing' meet."""
    return "".join(ch for ch in value.lower() if ch.isalnum()) if isinstance(value, str) else ""


def _source_notes(index):
    """{platform: [note, ...]} for an index's source_notes (emit_index), unreadable
    entries skipped. Each note has kind "no_ads" | "error" and the page fields."""
    out = {}
    raw = index.get("source_notes") if isinstance(index, dict) else None
    for n in raw if isinstance(raw, list) else []:
        if (isinstance(n, dict) and n.get("kind") in ("no_ads", "error")
                and isinstance(n.get("platform"), str) and n["platform"]):
            out.setdefault(n["platform"], []).append(n)
    return out


# First parts of a Facebook address that are no page's name (its own functions, and the
# address forms that give a page by number), in letters and digits only. Applied to the
# address names of notes and of indexed ads, never to an advertiser's own name.
_NOT_A_NAME = frozenset("".join(ch for ch in x if ch.isalnum()) for x in (
    "profile.php", "pages", "people", "p", "pg", "groups", "public", "watch", "ads", "share",
    "sharer", "sharer.php", "permalink.php", "story.php", "photo", "photo.php", "photos",
    "events", "marketplace", "login", "login.php", "l.php", "hashtag", "search", "business",
    "reel", "reels", "video", "videos", "video.php", "notes", "media", "home.php", "help",
    "policies", "settings", "gaming", "dialog", "plugins", "tr", "privacy", "about", "careers",
    "stories", "live", "fundraisers", "messages", "notifications", "friends", "bookmarks",
    "saved", "recover", "checkpoint", "flx", "ajax", "api", "connect", "offers", "jobs",
    "learn", "terms", "oauth", "whatsapp", "instagram", "index.php", "ads_manager"))


def _id_key(value):
    """A page id as one comparable string: whole digits with leading zeros dropped ('055'
    is page 55); an id of only zeros is no id ('' for it and for none)."""
    if value in (None, "") or isinstance(value, bool):
        return ""
    s = str(value).strip()
    return s.lstrip("0") if s.isdigit() else s


def _forms(value):
    """The comparable forms of an address name: letters and digits only, as it is and
    percent-decoded once more. An address that went through a second encoding step reads
    'Acme%2520Plumbing' after the one decode that is done on reading; the form decoded
    once more is only ever compared for equality, never shown or used as the name."""
    return {_alnum(value), _alnum(unquote(value) if isinstance(value, str) else value)} - {""} - _NOT_A_NAME


def _rec_page(rec):
    """({page ids}, {page names, alphanumeric}) an index record carries."""
    data = rec.get("data") if isinstance(rec.get("data"), dict) else {}
    tags = rec.get("tags") if isinstance(rec.get("tags"), dict) else {}
    ids = {_id_key(data.get("page_id"))} - {""}
    names = {_alnum(data.get("advertiser_name")), _alnum(tags.get("advertiser"))}
    names |= _forms(data.get("page_slug"))
    return ids, names - {""}


def _rec_slugs(rec):
    """{forms of the address name} an index record carries (data.page_slug)."""
    data = rec.get("data") if isinstance(rec.get("data"), dict) else {}
    return _forms(data.get("page_slug"))


def _relation(ids, names, slugs, o_ids, o_names, o_slugs):
    """How two descriptions of a page relate: "same", "differs" or "other".
    Page ids decide when both carry one (a shared name counts for nothing). Else address
    names decide when both carry one: an address name belongs to exactly one page, so two
    different ones are two pages and a shared display name must not join them. Where the
    address names differ but the names otherwise look alike the relation is "differs":
    the tool cannot say whether it is one page under two addresses (a renamed page) or
    two pages, so it says "cannot tell". With an address name on only one side a shared
    name decides. (ad-intelligence/scripts/adrecords.py page_relation is the same rule for
    raw ads; a recurring test keeps the two in step.)"""
    if ids and o_ids:
        return "same" if ids & o_ids else "other"
    if slugs and o_slugs:
        if slugs & o_slugs:
            return "same"
        return "differs" if names & o_names else "other"
    return "same" if names & o_names else "other"


def _note_view(note):
    n_ids = {_id_key(note.get("page_id"))} - {""}
    n_slugs = _forms(note.get("page_slug"))
    n_names = (n_slugs | {_alnum(note.get("page_name"))}) - {""} - _NOT_A_NAME
    return n_ids, n_names, n_slugs


def _note_relation(note, rec):
    """"same" | "differs" | "other": the page a note is about, against a record's page."""
    if note.get("search"):
        return "other"          # a keyword search belongs to no one page
    ids, names = _rec_page(rec)
    n_ids, n_names, n_slugs = _note_view(note)
    return _relation(ids, names, _rec_slugs(rec), n_ids, n_names, n_slugs)


def _note_matches(note, rec):
    """True when the page a note is about is the page of this record."""
    return _note_relation(note, rec) == "same"


def _placed(note):
    """True when a note says which page it is about (a page id, address name or page
    name) without contradicting itself. A note that does not is about some page the
    index cannot name."""
    if note.get("search") or note.get("conflict"):
        return False
    n_ids, n_names, _slugs = _note_view(note)
    return bool(n_ids or n_names)


def _unplaced(notes):
    """The notes that may be about ANY page: every error (an error is about whichever
    page was asked, and one that matched no ad may still be that ad's page) and every
    "Ads not found" that names no page. Keyword-search notes belong to no page."""
    return [n for n in notes if n.get("kind") == "error"
            or (n.get("kind") == "no_ads" and not n.get("search") and not _placed(n))]


def _page_was_answered(rec, pool):
    """True when the other month (pool: its real records) holds an ad from the page of
    this record, i.e. the library did answer for that page."""
    ids, names = _rec_page(rec)
    if not ids and not names:
        return False
    slugs = _rec_slugs(rec)
    for other in pool:
        o_ids, o_names = _rec_page(other)
        if _relation(ids, names, slugs, o_ids, o_names, _rec_slugs(other)) == "same":
            return True
    return False


def _signature(note):
    """Which page a note says it is about: (id, address name, page name, contradiction)."""
    return (_id_key(note.get("page_id")), _alnum(note.get("page_slug")),
            _alnum(note.get("page_name")), tuple(note.get("conflict") or ()))


def _settle(notes):
    """Notes about the same address (same kind, same place) that do not say the same
    thing about which page it is disagree. They are all read as 'about no page we can
    name', whichever came first: the answer then cannot depend on the order the scraper
    returned them in."""
    sigs = {}
    for n in notes:
        w = n.get("where")
        if w and w != "a page the scraper did not name" and not n.get("search"):
            sigs.setdefault((n.get("kind"), w), set()).add(_signature(n))
    out = []
    for n in notes:
        if len(sigs.get((n.get("kind"), n.get("where")), ())) > 1:
            n = dict(n, page_id="", page_slug="", page_name="", conflict=None, disputed=True)
        out.append(n)
    return out


def _read_through_notes(recs, notes, pool, universe=()):
    """Sort records with no counterpart in the other month by what that month's library
    said. Returns (stated, unknown): stated = [(rec, note)] whose page the library said
    runs no ads; unknown = [(rec, note or None)] whose page it errored on, or that a
    statement could be about but cannot be tied to (note None when the record could not
    be tied to a page the library answered for and a note exists that may be about it:
    see _unplaced). Every other record is in neither: no note says anything about it.

    pool: the other month's real records. A page that shows ads in it answered, so a
    statement 'this page runs no ads' is not used for it (the ads win).
    universe: every record of that platform in the month the records are from. A
    statement that gives no page id and whose name fits ads of more than one page (two
    franchise locations sharing one display name) is about no page we can name: its
    ads are 'cannot tell'."""
    notes = _settle(notes)
    errors = [n for n in notes if n.get("kind") == "error"]
    loose = _unplaced(notes)
    ambiguous = {}
    for n in notes:
        if n.get("kind") == "no_ads" and _placed(n) and not _id_key(n.get("page_id")):
            ids = set()
            for u in list(universe) + list(recs):
                if _note_matches(n, u):
                    ids |= _rec_page(u)[0]
            if len(ids) > 1:
                ambiguous[id(n)] = dict(n, ambiguous=sorted(ids))
    by_where = lambda n: (n.get("where") or "", id(n))   # noqa: E731
    no_ads = sorted((n for n in notes if n.get("kind") == "no_ads" and _placed(n)), key=by_where)
    differs = {}
    stated, unknown = [], []
    for rec in recs:
        hits = sorted((n for n in errors if _note_matches(n, rec)), key=by_where)
        if hits:
            unknown.append((rec, hits[0]))
            continue
        said = sorted((n for n in notes if n.get("kind") == "no_ads" and _note_matches(n, rec)),
                      key=by_where)
        if said:
            if _page_was_answered(rec, pool):
                continue                       # the page shows ads: the ads win
            amb = next((ambiguous[id(n)] for n in said if id(n) in ambiguous), None)
            if amb is not None:
                unknown.append((rec, amb))
            else:
                stated.append((rec, said[0]))
        elif not _page_was_answered(rec, pool):
            doubt = next((n for n in no_ads if _note_relation(n, rec) == "differs"), None)
            if doubt is not None:
                if id(doubt) not in differs:
                    differs[id(doubt)] = dict(doubt, differs=True)
                unknown.append((rec, differs[id(doubt)]))
            elif loose:
                unknown.append((rec, None))
    return stated, unknown


def _where(note):
    return note.get("where") or note.get("page_name") or "a page"


def _named(notes):
    """' (a, b and 2 more)' naming the pages of some notes, or '' when none of them
    names its page (a note the scraper gave no address for says nothing to name)."""
    names = []
    for x in notes:
        w = x.get("where")
        if w and w != "a page the scraper did not name" and w not in names:
            names.append(w)
    if not names:
        return ""
    return " (" + ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "") + ")"


def _library_sentences(tool, platform, stated, unknown, notes, side, rest=0, only=False,
                       rest_recs=()):
    """The plain sentences for what the library said, one per page (and one for ads that
    could not be tied to a page). side "gone": the notes are THIS month's and the records
    are the previous month's ads missing this month; side "new": the notes are the
    previous month's and the records are this month's ads missing then.
    rest: records no note says anything about (only said for a platform that has
    nothing else to be compared with).
    rest_recs: those records, to tell whether the page the library answered for is
    certainly another page (page ids on both sides and none shared) or only one the tool
    could not tie to theirs (a page's address name often differs from its display name).
    only: the platform has no ads in the other month, so none of these are in the
    counts; the sentence says so, so "5 ads are no longer running" is not read next to
    "0 ads are no longer there" as a contradiction."""
    label = _plat_name(platform) or "The source"
    noun = lambda n: _rec_noun(tool, n)          # noqa: E731
    notes = _settle(notes)
    out = []

    def group(pairs):
        by = {}
        for rec, note in pairs:
            by.setdefault(id(note) if note is not None else 0, [note, 0])[1] += 1
        return list(by.values())

    for note, n in group(stated):
        if side == "gone":
            out.append(f"{label}'s ad library says {_where(note)} is not currently running "
                       f"ads; its {n} {noun(n)} from the previous month "
                       f"{_plural(n, 'is', 'are')} no longer running.")
        else:
            out.append(f"In the previous month {label}'s ad library said {_where(note)} was "
                       f"not running ads; its {n} {noun(n)} shown this month "
                       f"{_plural(n, 'is', 'are')} new.")
    for note, n in group(unknown):
        if note is not None and note.get("ambiguous"):
            ids = note["ambiguous"]
            which = (", ".join(ids[:3]) + (f" and {len(ids) - 3} more" if len(ids) > 3 else ""))
            if side == "gone":
                out.append(f"{label}'s ad library said {_where(note)} is not currently running "
                           f"ads, but that address fits ads from more than one page (page ids "
                           f"{which}); {n} {label} {noun(n)} from the previous month "
                           f"{_plural(n, 'is', 'are')} left out of the count of those no longer "
                           f"there: it is not known which page the library meant.")
            else:
                out.append(f"In the previous month {label}'s ad library said {_where(note)} was "
                           f"not running ads, but that address fits ads from more than one page "
                           f"(page ids {which}); {n} {label} {noun(n)} shown this month "
                           f"{_plural(n, 'is', 'are')} not counted as new: it is not known which "
                           f"page the library meant.")
        elif note is not None and note.get("differs"):
            if side == "gone":
                out.append(f"{label}'s ad library said {_where(note)} is not currently running "
                           f"ads, but the address of the page of {n} {label} {noun(n)} from the "
                           f"previous month is a different one, though the names look alike; "
                           f"{_plural(n, 'it is', 'they are')} left out of the count of those no "
                           f"longer there: it is not known whether {_plural(n, 'it is', 'they are')} "
                           f"the same page.")
            else:
                out.append(f"In the previous month {label}'s ad library said {_where(note)} was "
                           f"not running ads, but the address of the page of {n} {label} "
                           f"{noun(n)} shown this month is a different one, though the names "
                           f"look alike; {_plural(n, 'it is', 'they are')} not counted as new: it "
                           f"is not known whether {_plural(n, 'it is', 'they are')} the same page.")
        elif note is not None:
            detail = f" ({note['detail']})" if note.get("detail") else ""
            if side == "gone":
                out.append(f"{label}'s ad library returned an error for {_where(note)} this "
                           f"month{detail}, so its {n} {noun(n)} from the previous month "
                           f"{_plural(n, 'is', 'are')} left out of the count of those no longer "
                           f"there: it is not known whether {_plural(n, 'it is', 'they are')} "
                           f"still running.")
            else:
                out.append(f"In the previous month {label}'s ad library returned an error "
                           f"for {_where(note)}{detail}, so its {n} {noun(n)} shown this "
                           f"month {_plural(n, 'is', 'are')} not counted as new: it is not "
                           f"known whether {_plural(n, 'it was', 'they were')} running then.")
        else:
            errs = [x for x in notes if x.get("kind") == "error"]
            nfs = [x for x in _unplaced(notes) if x.get("kind") == "no_ads"]
            if errs:
                named = _named(errs)
                if side == "gone":
                    out.append(f"{n} {label} {noun(n)} from the previous month could not be "
                               f"tied to a page the ad library answered for, and the library "
                               f"returned an error this month{named}, so "
                               f"{_plural(n, 'it is', 'they are')} left out of the count of "
                               f"those no longer there: it is not known whether "
                               f"{_plural(n, 'it is', 'they are')} still running.")
                else:
                    out.append(f"{n} {label} {noun(n)} shown this month could not be tied to "
                               f"a page the ad library answered for in the previous month, "
                               f"and the library returned an error then{named}, so "
                               f"{_plural(n, 'it is', 'they are')} not counted as new: it is "
                               f"not known whether {_plural(n, 'it was', 'they were')} "
                               f"running then.")
            if nfs:
                # What the buyer is told about the pages: a note with an address says
                # which address and why the tool could not tie it to a page; only a note
                # with no address at all "did not say which page".
                why, nameless, seen_w = [], False, set()
                for x in nfs:
                    w = x.get("where")
                    if w in seen_w:
                        continue
                    seen_w.add(w)
                    if not w or w == "a page the scraper did not name":
                        nameless = True
                    elif x.get("disputed"):
                        why.append(f"for {w}, where the library's records disagree about which "
                                   f"page it is")
                    elif x.get("conflict"):
                        a_id, b_id = x["conflict"][0], x["conflict"][1]
                        why.append(f"for {w}, whose record gives page {a_id} while its address "
                                   f"gives page {b_id}")
                    else:
                        why.append(f"for {w}, which the tool could not tie to a page id")
                paren = f" ({'; '.join(why[:3])}{'; and more' if len(why) > 3 else ''})" if why else ""
                tail = ", without saying which page" if nameless else ""
                if side == "gone":
                    out.append(f"{label}'s ad library said a searched page is not currently "
                               f"running ads{paren}{tail}; {n} {label} "
                               f"{noun(n)} from the previous month could not be tied to a page "
                               f"it answered for, so {_plural(n, 'it is', 'they are')} left "
                               f"out of the count of those no longer there: it is not known "
                               f"whether {_plural(n, 'it is', 'they are')} still running.")
                else:
                    out.append(f"In the previous month {label}'s ad library said a searched "
                               f"page was not running ads{paren}{tail}; "
                               f"{n} {label} {noun(n)} shown this month could not be tied to "
                               f"a page it answered for, so {_plural(n, 'it is', 'they are')} "
                               f"not counted as new: it is not known whether "
                               f"{_plural(n, 'it was', 'they were')} running then.")
    if rest:
        placed_notes = [x for x in notes if _placed(x)]
        placed = _named(placed_notes)
        if placed:
            # The library did answer, but for a page that is not tied to these ads. It is
            # called "a different page" only when the page ids show it; an address name is
            # not enough (the same page can sit under a name and an address that differ).
            n_ids = set()
            for x in placed_notes:
                n_ids |= _note_view(x)[0]
            r_ids = [_rec_page(r)[0] for r in rest_recs]
            different = bool(n_ids and r_ids and all(i for i in r_ids)
                             and not (n_ids & set().union(*r_ids))
                             and all(_note_view(x)[0] for x in placed_notes))
            what = _plural(rest, "its page", "their pages")
            other = (f"the library answered for another page{placed}, which is a different page "
                     f"from {what}" if different else
                     f"the library answered for {placed.strip(' ()')}, which the tool could not "
                     f"tie to {what}")
            if side == "gone":
                out.append(f"{rest} other {label} {noun(rest)} from the previous month "
                           f"{_plural(rest, 'was', 'were')} not found this month; {other}.")
            else:
                out.append(f"{rest} other {label} {noun(rest)} shown this month had no "
                           f"counterpart in the previous month; {other}.")
        elif side == "gone":
            out.append(f"{rest} other {label} {noun(rest)} from the previous month "
                       f"{_plural(rest, 'was', 'were')} not found this month, and the "
                       f"library said nothing about {_plural(rest, 'its page', 'their pages')}.")
        else:
            out.append(f"{rest} other {label} {noun(rest)} shown this month had no "
                       f"counterpart in the previous month, and the library said nothing "
                       f"about {_plural(rest, 'its page', 'their pages')} then.")
    if only and stated:
        out.append("These are not in the counts above, which cover only sources that have "
                   "ads in both months.")
    return out


def compare_runs(root, run_id, against=None):
    """Compare run_id against a baseline run.

    Args:
        root:     path to the viewer root (str or Path)
        run_id:   the run to analyse (relative to root)
        against:  explicit baseline run id (relative to root), or None for auto

    Returns a dict with keys:
        run           -- the run being analysed
        against       -- the baseline used (None if no baseline)
        basis         -- "repeat_of" | "same_project" | "explicit" | None
        tool          -- "pain" | "ad" | "search"
        settings_differ -- bool
        differing_settings_keys -- list of differing top-level config keys
        platforms_only_in_one_run -- platforms present in only one run
        by_platform   -- per-platform breakdown (see DATA_MODEL.md)
        totals        -- aggregate counts across all shared platforms
        against_partial -- True, present only when the baseline was chosen by hand
                          (against=) and stopped before it finished; the summary
                          then opens with a sentence saying so
        run_partial   -- True, present only when this run itself stopped before it
                          finished; the summary then opens with a sentence
                          saying the no-longer-there count may be only a gap
        skipped_partial -- list of run ids, present only when the baseline was chosen
                          automatically and stopped months were passed over,
                          or when every earlier month was; the summary says so
    """
    root = Path(root).resolve()
    run_dir = root / run_id

    # Load this run's index
    index = _load_index(run_dir)
    tool = (index.get("tool") if isinstance(index, dict) else None)

    # Resolve baseline
    skipped = []   # Stopped months passed over while choosing automatically
    if against is not None:
        basis = "explicit"
        against_id = against
        against_dir = root / against
        if not against_dir.is_dir():
            against_id = None
            basis = None
    else:
        against_id, basis = _find_baseline(root, run_id, index, skipped)

    # If no baseline, return quickly
    if against_id is None:
        sizes = _month_sizes_no_baseline(tool, run_id, index, skipped if against is None else None)
        out = {
            "run": run_id,
            "against": None,
            "basis": None,
            "tool": tool,
            "settings_differ": False,
            "differing_settings_keys": [],
            "platforms_only_in_one_run": [],
            "summary": _summary(tool, sizes, None),
            "month_sizes": sizes,
            "by_platform": {},
            "totals": {"new": 0, "gone": 0, "changed": 0, "unchanged": 0, "not_comparable": 0},
        }
        if against is None and skipped:
            out["skipped_partial"] = list(skipped)   # (present only then)
        return out

    against_dir = root / against_id
    against_index = _load_index(against_dir)

    # Settings comparison
    cfg_a = _read_config(run_dir)
    cfg_b = _read_config(against_dir)
    settings_differ, differing_keys = _compare_configs(cfg_a, cfg_b)

    # Records
    this_records = (index.get("records") or []) if isinstance(index, dict) else []
    that_records = (against_index.get("records") or []) if isinstance(against_index, dict) else []

    # --------------------------------------------------------------------------
    # Record pre-processing (parity: chip count must equal click count).
    #
    # Two layers:
    #   1. Tombstone filter (pain + reddit only, matching viewer_ctl._tombstone and
    #      app.js isRedditTombstone): deleted Reddit posts are invisible on the
    #      review wall.  Removed from both sides before any counting.
    #   2. Trash filter: records the user hid.
    #
    # The identity-set and the output-list must be filtered differently:
    #   this_full  -- tombstone-only filter.  Used for `this_sids` so that a record
    #                 the user trashed in THIS run is not miscounted as "gone" (B1).
    #   this_show  -- tombstone + trash filter.  Used for new/changed/unchanged output.
    #   that_full  -- tombstone-only.  Used for `that_sids` (a record trashed in the
    #                 baseline must not be called "new" in this run).
    #   that_show  -- tombstone + baseline trash filter.  Used for gone output, because
    #                 the "gone" click opens the baseline where trashed records are
    #                 hidden -- the chip count must equal what the click shows (B2).
    # --------------------------------------------------------------------------
    this_trashed = _trashed_uids(run_dir)
    that_trashed = _trashed_uids(against_dir)

    def _is_tomb(r):
        return tool == "pain" and _tombstone(r)

    this_full = [r for r in this_records if isinstance(r, dict) and not _is_tomb(r)]
    this_show = [r for r in this_full if r.get("uid") not in this_trashed]
    that_full = [r for r in that_records if isinstance(r, dict) and not _is_tomb(r)]
    that_show = [r for r in that_full if r.get("uid") not in that_trashed]

    # Determine shared vs exclusive platforms (use full lists for correct platform coverage)
    this_platforms = _platforms_in_run(this_full)
    that_platforms = _platforms_in_run(that_full)
    shared_platforms = this_platforms & that_platforms
    only_in_this = this_platforms - that_platforms
    only_in_that = that_platforms - this_platforms
    platforms_only_in_one = sorted(
        [{"platform": p, "in_run": "this"} for p in only_in_this]
        + [{"platform": p, "in_run": "baseline"} for p in only_in_that],
        key=lambda x: x["platform"]
    )
    # Ads each month set aside as another advertiser's (see _set_aside_ids).
    this_aside, this_aside_n = _set_aside_ids(index)
    that_aside, that_aside_n = _set_aside_ids(against_index)
    for item in platforms_only_in_one:
        # The month without results on this platform may have had some and set
        # them all aside: then "not collected" would be false (the app reads this).
        n = (this_aside_n if item["in_run"] == "baseline" else that_aside_n).get(item["platform"])
        if n:
            item["set_aside_in_other_run"] = n
    # What each month's library said about pages it could not show ads for (the index's
    # source_notes), and the sentences that follow from it (see _source_notes).
    this_notes = _source_notes(index)
    that_notes = _source_notes(against_index)
    library_sentences = []
    # uids of the previous month's ads left out of "no longer there", and of this
    # month's ads left out of "new", because the library errored on their page.
    that_lib_unknown, this_lib_unknown = set(), set()
    for item in platforms_only_in_one:
        # A platform in only one month, where the other month's library answered with a
        # status message instead of ads: "not collected in this run" would be false
        # (the library was asked and answered), so the sentences say what it said.
        plat = item["platform"]
        if item["in_run"] == "baseline" and this_notes.get(plat):
            recs = [r for r in that_show if r.get("platform") == plat]
            notes, side = this_notes[plat], "gone"
        elif item["in_run"] == "this" and that_notes.get(plat):
            recs = [r for r in this_show if r.get("platform") == plat]
            notes, side = that_notes[plat], "new"
        else:
            continue
        universe = [r for r in (that_full if item["in_run"] == "baseline" else this_full)
                    if r.get("platform") == plat]
        stated, unknown = _read_through_notes(recs, notes, [], universe)
        used = {r.get("uid") for r, _n in stated} | {r.get("uid") for r, _n in unknown}
        left = [r for r in recs if r.get("uid") not in used]
        said = _library_sentences(tool, plat, stated, unknown, notes, side, len(left),
                                  only=True, rest_recs=left)
        if said:
            item["library_said"] = said
    # uids of this month's records that the previous month set aside, and of the
    # previous month's records that this month set aside.
    this_from_aside, that_to_aside = set(), set()
    # Uids of this month's search-results records whose keyword the previous
    # month did not pull, and of the previous month's whose keyword this month did not.
    this_not_pulled_before, that_not_pulled_now = set(), set()
    # Uids of this month's domain rows that are not called new, and of the
    # previous month's that are not called gone, because the two months pulled different
    # keywords (or a pull came back empty) and no ranking for the domain was found on a
    # keyword with search results in both months.
    this_dom_not_compared, that_dom_not_compared = set(), set()

    # Filter records to shared platforms
    this_shared_full = [r for r in this_full if r.get("platform") in shared_platforms]
    this_shared_show = [r for r in this_show if r.get("platform") in shared_platforms]
    that_shared_full = [r for r in that_full if r.get("platform") in shared_platforms]
    that_shared_show = [r for r in that_show if r.get("platform") in shared_platforms]

    # Build identity maps using identity.py
    sys_path_inject = str(Path(__file__).resolve().parent)
    import sys
    if sys_path_inject not in sys.path:
        sys.path.insert(0, sys_path_inject)
    import identity as _identity  # noqa: E402

    # Identity maps use FULL records (including trashed) so identity matching is complete.
    this_id_map = {}   # uid -> stable_id
    for r in this_shared_full:
        uid = r.get("uid")
        if uid:
            this_id_map[uid] = _identity.record_identity(run_dir, r)

    that_id_map = {}   # uid -> stable_id (baseline)
    for r in that_full:          # all baseline records (wider than just shared)
        uid = r.get("uid")
        if uid:
            that_id_map[uid] = _identity.record_identity(against_dir, r)

    # Reverse maps: stable_id -> record (for key-field comparison)
    this_by_sid = {}
    for r in this_shared_full:
        uid = r.get("uid")
        sid = this_id_map.get(uid)
        if sid is not None:
            this_by_sid[sid] = r

    that_by_sid = {}
    for r in that_shared_full:
        uid = r.get("uid")
        sid = that_id_map.get(uid)
        if sid is not None:
            that_by_sid[sid] = r

    # Build per-platform breakdown
    by_platform = {}
    totals = {"new": 0, "gone": 0, "changed": 0, "unchanged": 0, "not_comparable": 0}

    for platform in sorted(shared_platforms):
        # this run: full (for sids), show (for output lists)
        this_plat_full = [r for r in this_shared_full if r.get("platform") == platform]
        this_plat_show = [r for r in this_shared_show if r.get("platform") == platform]
        # baseline: full (for sids), show (for gone list)
        that_plat_full = [r for r in that_shared_full if r.get("platform") == platform]
        that_plat_show = [r for r in that_shared_show if r.get("platform") == platform]

        new_list = []
        gone_list = []
        changed_list = []
        unchanged_list = []
        not_comparable_count = 0
        from_aside_list = []   # Shown now, set aside the previous month
        to_aside_list = []     # Shown the previous month, set aside now

        # SID sets: built from FULL records so trash state does not distort them.
        this_sids = {this_id_map[r["uid"]] for r in this_plat_full
                     if this_id_map.get(r.get("uid")) is not None}
        that_sids = {that_id_map[r["uid"]] for r in that_plat_full
                     if that_id_map.get(r.get("uid")) is not None}

        # This run's SHOW records: new / changed / unchanged / not_comparable
        for r in this_plat_show:
            uid = r.get("uid")
            sid = this_id_map.get(uid)
            if sid is None:
                not_comparable_count += 1
                continue
            if sid not in that_sids:
                if (platform, sid) in that_aside:
                    from_aside_list.append({"uid": uid})
                    this_from_aside.add(uid)
                else:
                    new_list.append({"uid": uid})
            else:
                baseline_rec = that_by_sid.get(sid)
                diff = _changed_fields(r, baseline_rec, tool) if baseline_rec else []
                if diff:
                    entry = {"uid": uid, "changed_fields": diff}
                    # Ad-specific label: active -> ended means "stopped running"
                    if tool == "ad" and "tags.active" in diff:
                        old_active = _get_nested(baseline_rec, "tags.active")
                        new_active = _get_nested(r, "tags.active")
                        if old_active == "active" and new_active == "ended":
                            entry["label"] = "stopped running"
                    changed_list.append(entry)
                else:
                    unchanged_list.append({"uid": uid})

        # Baseline SHOW records: gone (visible in baseline, not in this run).
        # SHOW = baseline trash excluded, matching what the "gone" click opens.
        for r in that_plat_show:
            uid = r.get("uid")
            sid = that_id_map.get(uid)
            if sid is None:
                continue   # not_comparable in baseline: not counted as gone
            if sid not in this_sids:
                if (platform, sid) in this_aside:
                    to_aside_list.append({"uid": uid})
                    that_to_aside.add(uid)
                else:
                    gone_list.append({"uid": uid})

        # ----------------------------------------------------------------------
        # E1: positional-uid guard (FROZEN).
        #
        # When a platform has records without stable identities, we cannot
        # reliably assert "gone" (a baseline record not found in this_sids could
        # simply have a positional uid in this run) or "new" (a this-run record
        # not found in that_sids could correspond to a positional baseline record).
        #
        # Rule: use FULL lists (before trash) for the check, for the same reason
        # this_sids uses them (B1): trash state must not influence identity-set
        # membership.  changed/unchanged for records that DO match remain valid.
        # ----------------------------------------------------------------------
        this_positional_count = sum(
            1 for r in this_plat_full
            if this_id_map.get(r.get("uid")) is None
        )
        that_positional_count = sum(
            1 for r in that_plat_full
            if that_id_map.get(r.get("uid")) is None
        )

        cannot_tell = []
        if this_positional_count > 0:
            cannot_tell.append("gone")
        if that_positional_count > 0:
            cannot_tell.append("new")

        # Suppress the affected lists so chip counts equal click counts.
        if "gone" in cannot_tell:
            gone_list = []
        if "new" in cannot_tell:
            new_list = []

        # ------------------------------------------------------------------
        # ("Pick by volume each month"): a search run
        # pulls Google search results for a limited set of keywords, and a monthly
        # repeat re-picks them, so a keyword can be pulled in one month and not the
        # other.  The app matches a search-results record by its keyword
        # (google_serp:<keyword>), so such a keyword would read "gone" (or "new") when
        # the tool only did not look.  For a ranking "gone" means the domains stopped
        # ranking; here it would be a false statement.  "Not pulled" is decided from
        # the two months' own records: a serp record that is in one month's records
        # and not in the other's (the identity sets above are built from the full
        # lists, so a trashed record still counts as there).  A keyword pulled in
        # both months is untouched: if it lost a domain it is "changed".
        # Guards: the search tool only, and the serp kind only.  Reviews, ads, Semrush
        # keyword rows and domain roll-up rows keep new / gone exactly as before.
        # Done after the positional guard on purpose: where the matching is unsure
        # the lists are already empty, and nothing is claimed.
        # ------------------------------------------------------------------
        not_pulled_now_list = []      # in the previous month's pulls, not in this month's
        not_pulled_before_list = []   # in this month's pulls, not in the previous month's
        dom_not_compared_now_list = []      # See below
        dom_not_compared_before_list = []
        if tool == "search":
            this_kind = {r.get("uid"): r.get("kind") for r in this_plat_show}
            that_kind = {r.get("uid"): r.get("kind") for r in that_plat_show}
            keep_new, keep_gone = [], []
            for e in new_list:
                (not_pulled_before_list if this_kind.get(e["uid"]) == "serp" else keep_new).append(e)
            for e in gone_list:
                (not_pulled_now_list if that_kind.get(e["uid"]) == "serp" else keep_gone).append(e)
            new_list, gone_list = keep_new, keep_gone
            this_not_pulled_before.update(e["uid"] for e in not_pulled_before_list)
            that_not_pulled_now.update(e["uid"] for e in not_pulled_now_list)

            # --------------------------------------------------------------
            # The same false statement one level up.  A domain row
            # (domain:<name>) is a roll-up over the keywords a month pulled, so when
            # the two months pulled different keywords a domain that ranked only on a
            # swapped-out keyword read "gone", and one that ranks only on a newly
            # pulled keyword read "new": the tool's choice moved, not the market.
            # Decided from the two months' own search-results records (their
            # data.results name the ranking domains; the domain rows are built from
            # the same results, so this reproduces them): a domain row is "gone" only
            # if the previous month's results name it on a keyword with results in BOTH
            # months, and "new" only if this month's results name it on such a keyword.
            # Otherwise it is listed apart, with no claim.  Guard: only when the two
            # months pulled different keywords, or a keyword had results in one month
            # and none in the other; when they pulled the same ones with results in
            # both, the domain rows keep new / gone exactly as before.  A domain in
            # both months' rows is untouched (it ranks in both, whatever was pulled).
            # --------------------------------------------------------------
            this_serp = {r["uid"]: r for r in this_plat_full
                         if r.get("kind") == "serp" and r.get("uid")}
            that_serp = {r["uid"]: r for r in that_plat_full
                         if r.get("kind") == "serp" and r.get("uid")}
            # A keyword counts as shared only when its results are not
            # empty in both months.  A shared keyword whose pull came back empty in
            # one month (a failed or empty pull) would otherwise make every domain
            # that ranked on it the month before read "gone": the tool got nothing
            # back, which is not the same as the domains having left.  The test is
            # also made when both months pulled the same keywords, because an empty
            # pull makes the two months differ without changing the set.  When the
            # same keywords were pulled and all of them have results in both months,
            # nothing changes (the guard).
            this_ok = {u for u, r in this_serp.items() if _has_results(r)}
            that_ok = {u for u, r in that_serp.items() if _has_results(r)}
            if set(this_serp) != set(that_serp) or this_ok != that_ok:
                shared_kw = this_ok & that_ok
                this_rank = _domains_ranked(this_serp, shared_kw)
                that_rank = _domains_ranked(that_serp, shared_kw)
                this_name = {r.get("uid"): _domain_name(r) for r in this_plat_show
                             if r.get("kind") == "domain"}
                that_name = {r.get("uid"): _domain_name(r) for r in that_plat_show
                             if r.get("kind") == "domain"}
                keep_new, keep_gone = [], []
                for e in new_list:
                    if e["uid"] in this_name and this_name[e["uid"]] not in this_rank:
                        dom_not_compared_before_list.append(e)
                    else:
                        keep_new.append(e)
                for e in gone_list:
                    if e["uid"] in that_name and that_name[e["uid"]] not in that_rank:
                        dom_not_compared_now_list.append(e)
                    else:
                        keep_gone.append(e)
                new_list, gone_list = keep_new, keep_gone
                this_dom_not_compared.update(e["uid"] for e in dom_not_compared_before_list)
                that_dom_not_compared.update(e["uid"] for e in dom_not_compared_now_list)

        # ------------------------------------------------------------------
        # What the library said about a page (see _source_notes).  Only when a
        # month's index carries notes for this platform, so every other comparison
        # is unchanged.  Done after the positional guard on purpose: where the
        # matching is unsure the lists are already empty, and nothing is claimed.
        # Previous month's ads missing now: the library errored on their page this
        # month -> "cannot tell" (left out of gone); it said the page runs no ads
        # -> stay gone, with its statement attributed.  The mirror for new.
        # ------------------------------------------------------------------
        lib_unknown_now_list, lib_unknown_before_list, lib_said = [], [], []
        if this_notes.get(platform) and gone_list:
            by_uid = {r.get("uid"): r for r in that_plat_show}
            stated, unknown = _read_through_notes(
                [by_uid[e["uid"]] for e in gone_list if e["uid"] in by_uid],
                this_notes[platform], this_plat_full, that_plat_full)
            unk = {r["uid"] for r, _n in unknown}
            lib_unknown_now_list = [e for e in gone_list if e["uid"] in unk]
            gone_list = [e for e in gone_list if e["uid"] not in unk]
            that_lib_unknown.update(unk)
            lib_said += _library_sentences(tool, platform, stated, unknown,
                                           this_notes[platform], "gone")
        if that_notes.get(platform) and new_list:
            by_uid = {r.get("uid"): r for r in this_plat_show}
            stated, unknown = _read_through_notes(
                [by_uid[e["uid"]] for e in new_list if e["uid"] in by_uid],
                that_notes[platform], that_plat_full, this_plat_full)
            unk = {r["uid"] for r, _n in unknown}
            lib_unknown_before_list = [e for e in new_list if e["uid"] in unk]
            new_list = [e for e in new_list if e["uid"] not in unk]
            this_lib_unknown.update(unk)
            lib_said += _library_sentences(tool, platform, stated, unknown,
                                           that_notes[platform], "new")
        library_sentences.extend(lib_said)

        # Human-readable reason shown in the strip per platform.
        # Uses the tool-appropriate noun so "reviews", "ads", or "records"
        # matches the context the user is looking at.
        plat_display = _PLAT_LABEL.get(
            platform,
            (platform[0].upper() + platform[1:]) if platform else platform,
        )
        if this_positional_count > 0 and that_positional_count > 0:
            noun_this = _rec_noun(tool, this_positional_count)
            noun_that = _rec_noun(tool, that_positional_count)
            cannot_tell_reason = (
                f"{this_positional_count} {plat_display} {noun_this} in this run and "
                f"{that_positional_count} in the previous run could not be matched to "
                "each other, so new and removed counts cannot be determined."
            )
        elif this_positional_count > 0:
            noun = _rec_noun(tool, this_positional_count)
            cannot_tell_reason = (
                f"{this_positional_count} {plat_display} {noun} in this run could not be "
                "matched to the previous run, so we can't count which ones are no "
                "longer there."
            )
        elif that_positional_count > 0:
            noun = _rec_noun(tool, that_positional_count)
            cannot_tell_reason = (
                f"{that_positional_count} {plat_display} {noun} in the previous run "
                "could not be matched, so we can't count which ones are new."
            )
        else:
            cannot_tell_reason = ""

        by_platform[platform] = {
            "new": new_list,
            "gone": gone_list,
            "changed": changed_list,
            "unchanged": unchanged_list,
            "not_comparable": not_comparable_count,
            "cannot_tell": cannot_tell,
            "cannot_tell_reason": cannot_tell_reason,
            "cannot_tell_counts": {
                "gone": this_positional_count,
                "new": that_positional_count,
            },
        }
        # Present only when an ad moved, so other comparisons are unchanged.
        if from_aside_list:
            by_platform[platform]["shown_again"] = from_aside_list
        if to_aside_list:
            by_platform[platform]["set_aside_now"] = to_aside_list
        # Present only when a keyword was pulled in one month and not the other.
        if not_pulled_now_list:
            by_platform[platform]["not_pulled_now"] = not_pulled_now_list
        if not_pulled_before_list:
            by_platform[platform]["not_pulled_before"] = not_pulled_before_list
        # Present only when the two months pulled different keywords and a
        # domain row had no ranking on a keyword both pulled.
        if dom_not_compared_now_list:
            by_platform[platform]["domains_not_compared_now"] = dom_not_compared_now_list
        if dom_not_compared_before_list:
            by_platform[platform]["domains_not_compared_before"] = dom_not_compared_before_list
        # Present only when the library errored on a page in one month (see above).
        if lib_unknown_now_list:
            by_platform[platform]["library_unknown_now"] = lib_unknown_now_list
        if lib_unknown_before_list:
            by_platform[platform]["library_unknown_before"] = lib_unknown_before_list
        if lib_said:
            by_platform[platform]["library_said"] = lib_said
        totals["new"] += len(new_list)
        totals["gone"] += len(gone_list)
        totals["changed"] += len(changed_list)
        totals["unchanged"] += len(unchanged_list)
        totals["not_comparable"] += not_comparable_count

    # A platform whose ads were ALL set aside in one month is not shared,
    # so its records never reach the loop above. Match them here, by the same ids.
    if that_aside:
        for r in this_show:
            p = r.get("platform")
            if p in shared_platforms or not r.get("uid"):
                continue
            if (p, _identity.record_identity(run_dir, r)) in that_aside:
                this_from_aside.add(r["uid"])
    if this_aside:
        for r in that_show:
            p = r.get("platform")
            if p in shared_platforms or not r.get("uid"):
                continue
            if (p, that_id_map.get(r["uid"])) in this_aside:
                that_to_aside.add(r["uid"])

    month_sizes = _month_sizes(
        tool, run_id, against_id, this_records, that_records, _is_tomb,
        this_trashed, that_trashed, shared_platforms, this_id_map, that_id_map,
        by_platform, totals, this_from_aside, that_to_aside, this_aside_n, that_aside_n,
        this_not_pulled_before, that_not_pulled_now,
        this_dom_not_compared, that_dom_not_compared,
        this_lib_unknown, that_lib_unknown, set(this_notes), set(that_notes))

    out = {
        "run": run_id,
        "against": against_id,
        "basis": basis,
        "tool": tool,
        "settings_differ": settings_differ,
        "differing_settings_keys": differing_keys,
        "platforms_only_in_one_run": platforms_only_in_one,
        "summary": _summary(tool, month_sizes, totals, len(this_from_aside), len(that_to_aside),
                            len(this_not_pulled_before), len(that_not_pulled_now),
                            len(this_dom_not_compared), len(that_dom_not_compared),
                            platforms_only_in_one, library_sentences),
        "month_sizes": month_sizes,
        "by_platform": by_platform,
        "totals": totals,
    }
    if this_from_aside or that_to_aside:
        # Present only when an ad moved between shown and set aside.
        out["set_aside_moves"] = {"shown_again": len(this_from_aside),
                                  "set_aside_now": len(that_to_aside)}
    if this_not_pulled_before or that_not_pulled_now:
        # Present only when a search keyword was pulled in one month and not the
        # other (like set_aside_moves); never part of new or gone.
        out["not_pulled_moves"] = {"not_pulled_now": len(that_not_pulled_now),
                                   "not_pulled_before": len(this_not_pulled_before)}
    if this_dom_not_compared or that_dom_not_compared:
        # Present only when the two months pulled different keywords and a
        # domain row had no ranking on a keyword with search results in both months; never
        # new or gone.
        out["domains_not_compared_moves"] = {
            "domains_not_compared_now": len(that_dom_not_compared),
            "domains_not_compared_before": len(this_dom_not_compared)}
    # A step of a month that did not finish (it failed, the budget cap stopped
    # it, or its scraper run did not work) left no records, which reads as "no longer
    # there" or "new". The index carries the step names (emit_index "unfinished_steps");
    # this says what that does to the counts, in the summary and in one key for the app.
    # A month the notification already calls stopped early keeps the sentences below.
    note = _unfinished_note(
        [] if _is_partial(run_dir) else _unfinished_names(index),
        [] if _is_partial(against_dir) else _unfinished_names(against_index))
    if note:
        out["unfinished_note"] = note
        out["summary"] = note + " " + out["summary"]
    if basis == "explicit" and _is_partial(against_dir):
        # The user chose a month that stopped early, so the comparison
        # runs, and the answer says so in the sentence they quote (key present only
        # then, like set_aside_moves).
        out["against_partial"] = True
        out["summary"] = _PARTIAL_BASELINE_SENTENCE + " " + out["summary"]
    if basis != "explicit" and skipped:
        # The month the comparison would have used stopped early and was
        # passed over; say which one, so "compared with the previous run" is not read
        # as the previous month.
        out["skipped_partial"] = list(skipped)
        out["summary"] = _skipped_sentence(skipped, against_id) + " " + out["summary"]
    if _is_partial(run_dir):
        # The month being looked at is the one that stopped, so a record the
        # earlier month has and this one lacks may simply not have been collected.
        out["run_partial"] = True
        out["summary"] = _PARTIAL_RUN_SENTENCE + " " + out["summary"]
    return out


def _unfinished_names(idx):
    """The names of the steps an index says did not finish (emit_index "unfinished_steps")."""
    steps = idx.get("unfinished_steps") if isinstance(idx, dict) else None
    if not isinstance(steps, list):
        return []
    return [str(s["step"]) for s in steps if isinstance(s, dict) and s.get("step")]


def _unfinished_note(this_names, that_names):
    """One plain sentence per month with a step that did not finish, or "" for none.
    Said before the counts: the counts cannot tell a record that is gone
    from one that was not collected."""
    def join(names):
        if len(names) == 1:
            return f"the {names[0]} step"
        return "the steps " + ", ".join(names[:-1]) + " and " + names[-1]
    out = []
    if this_names:
        out.append(f"In this month {join(this_names)} did not finish, so a record it would "
                   f"have found may show as no longer there when it was only not collected.")
    if that_names:
        out.append(f"In the previous month {join(that_names)} did not finish, so a record it "
                   f"would have found may show as new this month when it was only not "
                   f"collected before.")
    return " ".join(out)


# --------------------------------------------------------------------------
# Month sizes that add up with the change counts
# --------------------------------------------------------------------------
# WHY: asked "what changed since last month?", real sessions quoted a month's size
# from list-runs ("out of 130") next to compare's counts, which cover 129 (37 new +
# 92 unchanged): one removed Reddit post is in the index but never compared, because
# the review wall hides it (_tombstone).  A rule in the skill text did not stop it in
# 1 of 2 samples.  So compare says, in code, how big each month is in terms that add
# up: the records compared, and every record left out with a plain reason.
#
# This block only COUNTS; it never changes which records compare_runs counts
# (test_p33_compare pins those).  Each record of a month's index lands in exactly
# one bucket below, in this order, which is the order compare_runs filters in:
#   this month:     unreadable, removed Reddit post, hidden by the user, source not
#                   in the other month, no matchable id, new-but-cannot-tell,
#                   compared (new + changed + unchanged)
#   previous month: unreadable, removed Reddit post, hidden by the user (less the
#                   ones this month's changed/unchanged already count), source not in
#                   the other month, no matchable id, gone-but-cannot-tell, matched to
#                   a record hidden in this month, compared (gone + changed + unchanged)
# Invariant: compared + sum(left_out_reasons) == records, per month.  If a case this
# partition does not foresee ever breaks it (e.g. two records sharing one identity),
# the difference is reported as its own reason instead of being hidden.

def _plural(n, one, many):
    return one if n == 1 else many


def _be(n):
    return "is" if n == 1 else "are"


def _plat_name(platform):
    if not platform:
        return ""
    return _PLAT_LABEL.get(platform, platform[0].upper() + platform[1:])


# Said once after the not-pulled sentences.  True of every month the tool made:
# the number of keywords whose search results are pulled is limited, and which ones can
# differ between two months.  It does not name the rule (highest volume), because a
# month made before that rule was set picked them another way.
_NOT_PULLED_WHY = (" Search results are pulled for a limited set of keywords, and which "
                   "ones can change from month to month.")


def _summary(tool, month_sizes, totals, shown_again=0, set_aside_now=0,
             not_pulled_before=0, not_pulled_now=0, dom_before=0, dom_now=0,
             platforms_only=(), library_said=()):
    """One ready-to-quote answer: this month's size, then the change counts.

    WHY: with month_sizes alone, one of two real sessions (p41fix3, sample r2b) still
    wrote "across 130 reviews compared" when 129 were compared: it took the month's
    record count as the denominator.  A sentence composed in code, where the only
    denominator is the compared count, leaves nothing to combine.

    shown_again / set_aside_now: ads that moved between shown and set
    aside as another advertiser's; said after the counts, never counted in them.

    not_pulled_before / not_pulled_now: search keywords whose search results
    one month pulled and the other did not; said after the counts, never counted in them.

    dom_before / dom_now: search domains left out of the new / no-longer-there
    counts because the two months pulled different keywords, or a pull came back empty,
    and no ranking for them was found on a keyword with search results in both months;
    said after the counts, never counted in them.
    """
    this = month_sizes["this_month"]
    if month_sizes.get("previous_month") is None:
        return this["sentence"]
    gone = totals["gone"]
    out = (f"{this['sentence']} Of the {this['compared']} compared: {totals['new']} new, "
           f"{totals['changed']} changed, {totals['unchanged']} unchanged. {gone} "
           f"{_rec_noun(tool, gone)} from the previous month {_be(gone)} no longer there.")
    # A platform that is in one month and not collected at all in the
    # other has no records to count, so "0 reviews ... are no longer there" read as an
    # honest nothing. Said in plain words, in the app's own style ("X was in the previous
    # month but not collected in this run"). A platform whose ads were all set aside in
    # the other month was collected, so it is left to the set-aside sentences below.
    for item in platforms_only or ():
        if item.get("set_aside_in_other_run"):
            continue
        if item.get("library_said"):
            # The other month's library answered with a status message, not ads: it
            # was collected, so "not collected" would be false (see _source_notes).
            out += " " + " ".join(item["library_said"])
            continue
        plat = item.get("platform")
        label = _plat_name(plat) or "A source"
        if plat == "google_serp":
            # The Google ads and the Google search results are different things.
            out += (" Search results were pulled in the previous month but not in this run."
                    if item.get("in_run") == "baseline" else
                    " Search results were pulled in this run but not in the previous month.")
        elif item.get("in_run") == "baseline":
            out += f" {label} was in the previous month but not collected in this run."
        else:
            out += f" {label} was collected in this run but not in the previous month."
    # What the library said about a page whose ads are or are not counted (see
    # _source_notes): said after the counts, never counted in them.
    for sentence in library_said or ():
        out += " " + sentence
    if set_aside_now:
        out += (f" {set_aside_now} {_rec_noun(tool, set_aside_now)} shown in the previous "
                f"month {_plural(set_aside_now, 'is', 'are')} set aside this month, because the "
                f"advertiser's name does not match the competitor; "
                f"{_plural(set_aside_now, 'it is', 'they are')} left out of the count of "
                f"those no longer there.")
    if shown_again:
        out += (f" {shown_again} {_rec_noun(tool, shown_again)} set aside in the previous "
                f"month {_plural(shown_again, 'is', 'are')} shown this month, because the "
                f"advertiser now counts as the competitor's; "
                f"{_plural(shown_again, 'it is', 'they are')} not counted as new.")
    # Said without "new" or "no longer there": the tool did not look, so it
    # does not know whether anything changed.
    if not_pulled_now:
        out += (f" The search results for {not_pulled_now} "
                f"{_plural(not_pulled_now, 'keyword', 'keywords')} were pulled in the "
                f"previous month but not this month, so there is nothing to compare for "
                f"{_plural(not_pulled_now, 'it', 'them')}.")
    if not_pulled_before:
        out += (f" The search results for {not_pulled_before} "
                f"{_plural(not_pulled_before, 'keyword', 'keywords')} were pulled this "
                f"month but not in the previous month, so there is nothing to compare for "
                f"{_plural(not_pulled_before, 'it', 'them')}.")
    # The domains that rank on those keywords.  Same rule: the tool did not
    # look at the same keywords in both months, so it does not know.
    if dom_now:
        out += (f" {dom_now} {_plural(dom_now, 'domain', 'domains')} from the previous month "
                f"{_be(dom_now)} left out of the count of those no longer there, because no "
                f"ranking for {_plural(dom_now, 'it', 'them')} was found on a keyword with "
                f"search results in both months.")
    if dom_before:
        out += (f" {dom_before} {_plural(dom_before, 'domain', 'domains')} found this month "
                f"{_be(dom_before)} not counted as new, because no ranking for "
                f"{_plural(dom_before, 'it', 'them')} was found on a keyword with search "
                f"results in both months.")
    if not_pulled_now or not_pulled_before or dom_now or dom_before:
        out += _NOT_PULLED_WHY
    return out


def _month_sizes_no_baseline(tool, run_id, index, skipped=None):
    """No earlier month is used: nothing is compared, and the reason says so.

    When earlier months of this project exist but every one stopped before it
    finished (`skipped`, from _find_baseline), "there is no earlier month" would be
    false; the reason says they stopped, so none is used, and that the user can still
    compare with one by naming it."""
    records = (index.get("records") or []) if isinstance(index, dict) else []
    n = len(records)
    if skipped:
        many = len(skipped) > 1
        why = (f"the earlier month{'s' if many else ''} ({', '.join(skipped)}) stopped "
               f"before {'they' if many else 'it'} finished, so "
               f"{'none is' if many else 'it is not'} used as a baseline (you can still "
               f"compare with {'one of them' if many else 'it'} by naming "
               f"{'it' if many else 'the month'})")
    else:
        why = "there is no earlier month to compare with"
    reasons = [{"count": n, "reason": why}] if n else []
    return {"this_month": {"run": run_id, "records": n, "compared": 0, "left_out": n,
                           "left_out_reasons": reasons,
                           "sentence": (f"This month has {n} {_rec_noun(tool, n)}: none "
                                        f"are compared, because {why}.")},
            "previous_month": None}


def _month_sizes(tool, run_id, against_id, this_records, that_records, is_tomb,
                 this_trashed, that_trashed, shared_platforms, this_id_map,
                 that_id_map, by_platform, totals, this_from_aside=frozenset(),
                 that_to_aside=frozenset(), this_aside_n=None, that_aside_n=None,
                 this_not_pulled_before=frozenset(), that_not_pulled_now=frozenset(),
                 this_dom_not_compared=frozenset(), that_dom_not_compared=frozenset(),
                 this_lib_unknown=frozenset(), that_lib_unknown=frozenset(),
                 this_note_platforms=frozenset(), that_note_platforms=frozenset()):
    """Return {"this_month": {...}, "previous_month": {...} | None} (see above).

    An ad that moved between shown and set aside (compare_runs:
    this_from_aside, that_to_aside, by uid) is its own bucket in its month,
    before "source not in the other month" (a platform can be missing from a
    month because every one of its ads was set aside).

    A search-results record whose keyword the other month did not pull
    (this_not_pulled_before, that_not_pulled_now, by uid) is its own bucket in its
    month, so the sizes still add up and the reason says what it is.

    A search domain row left out of new / no longer there (this_dom_not_compared,
    that_dom_not_compared, by uid) is its own bucket in its month, the same way."""
    this_aside_n = this_aside_n or {}
    that_aside_n = that_aside_n or {}

    def noun(n):
        return _rec_noun(tool, n)

    def month(label, rid, records, compared, reasons, counted_as):
        n = len(records)
        reasons = [r for r in reasons if r["count"]]
        left_out = n - compared
        gap = left_out - sum(r["count"] for r in reasons)
        if gap:
            reasons.append({"count": gap, "reason": (
                f"{gap} {noun(gap)} could not be paired one to one between the two "
                f"months, so the sizes above are off by that many")})
        sentence = (f"{label} has {n} {noun(n)}: {compared} "
                    f"{_be(compared)} compared ({counted_as})")
        if reasons:
            sentence += "; " + "; ".join(r["reason"] for r in reasons)
        return {"run": rid, "records": n, "compared": compared, "left_out": left_out,
                "left_out_reasons": reasons, "sentence": sentence + "."}

    def split(records, trashed):
        """(unreadable, tombstones, hidden, visible) for one month's index."""
        unreadable = sum(1 for r in records if not isinstance(r, dict))
        recs = [r for r in records if isinstance(r, dict)]
        tombs = sum(1 for r in recs if is_tomb(r))
        full = [r for r in recs if not is_tomb(r)]
        hidden = [r for r in full if r.get("uid") in trashed]
        visible = [r for r in full if r.get("uid") not in trashed]
        return unreadable, tombs, hidden, visible

    def moved(visible, uids, plural_reason):
        """[reason] per platform for the visible records that moved."""
        by_plat = {}
        for r in visible:
            if r.get("uid") in uids:
                p = r.get("platform") or ""
                by_plat[p] = by_plat.get(p, 0) + 1
        return [{"count": k, "reason": plural_reason(k, _plat_name(p))}
                for p, k in sorted(by_plat.items())]

    def common(unreadable, tombs, hidden_count, visible, other_label, skip=frozenset(),
               other_aside_n=None, other_note_plats=frozenset()):
        out = [{"count": unreadable, "reason": (
                   f"{unreadable} {_plural(unreadable, 'entry', 'entries')} in the results "
                   f"file could not be read")},
               {"count": tombs, "reason": (
                   f"{tombs} removed Reddit {_plural(tombs, 'post', 'posts')} "
                   f"(deleted by the author or a moderator) {_be(tombs)} not compared")},
               {"count": hidden_count, "reason": (
                   f"{hidden_count} {noun(hidden_count)} you moved to the trash in the app "
                   f"{_be(hidden_count)} not compared")}]
        by_plat = {}
        for r in visible:
            p = r.get("platform")
            if p not in shared_platforms and r.get("uid") not in skip:
                by_plat[p or ""] = by_plat.get(p or "", 0) + 1
        # "this month" / "the previous month" (a doubled "the this month" read wrong).
        who = "this month" if other_label == "this" else "the previous month"
        for p in sorted(by_plat):
            k = by_plat[p]
            if p and (other_aside_n or {}).get(p):
                # The other month found results here and set them all aside.
                out.append({"count": k, "reason": (
                    f"{k} {_plat_name(p)} {noun(k)} {_be(k)} not compared, because "
                    f"{who} shows no {_plat_name(p)} results: it set all of "
                    f"its {_plat_name(p)} {noun(2)} aside, because the advertiser's name "
                    f"does not match the competitor")})
            elif p == "google_serp" and tool == "search":
                # The other month pulled no search results at all (its search
                # step was left out, or found nothing).  Said so, in place of a count of
                # keywords "not pulled": there is no keyword-by-keyword fact to give.
                out.append({"count": k, "reason": (
                    f"{k} search-results {noun(k)} {_be(k)} not compared, because {who} "
                    f"pulled no search results at all")})
            elif p in other_note_plats:
                # The other month asked this library and it answered with a status
                # message instead of ads (compare_runs: _source_notes).
                out.append({"count": k, "reason": (
                    f"{k} {_plat_name(p)} {noun(k)} {_be(k)} not compared, because "
                    f"{who} has no {_plat_name(p)} {noun(2)}: the library answered only with "
                    f"a status message, not ads")})
            elif p:
                out.append({"count": k, "reason": (
                    f"{k} {_plat_name(p)} {noun(k)} {_be(k)} not compared, because "
                    f"{who} has no {_plat_name(p)} results")})
            else:
                out.append({"count": k, "reason": (
                    f"{k} {noun(k)} with no source {_be(k)} not compared")})
        return out

    # ---- this month ---------------------------------------------------
    u, t, hid, vis = split(this_records, this_trashed)
    this_reasons = common(u, t, len(hid), vis, "previous", skip=this_from_aside,
                          other_aside_n=that_aside_n, other_note_plats=that_note_platforms)
    this_reasons += moved(vis, this_from_aside, lambda k, P: (
        f"{k} {P} {noun(k)} {_be(k)} shown this month but {_plural(k, 'was', 'were')} set "
        f"aside in the previous month (the advertiser's name did not match the competitor "
        f"then), so {_plural(k, 'it is', 'they are')} not counted as new"))
    k_np = sum(1 for r in vis if r.get("uid") in this_not_pulled_before)
    this_reasons.append({"count": k_np, "reason": (
        f"{k_np} search-results {noun(k_np)} {_be(k_np)} not compared, because "
        f"{_plural(k_np, 'it was', 'they were')} pulled this month and not in the "
        f"previous month")})
    k_dn = sum(1 for r in vis if r.get("uid") in this_dom_not_compared)
    this_reasons.append({"count": k_dn, "reason": (
        f"{k_dn} {_plural(k_dn, 'domain', 'domains')} {_be(k_dn)} not compared, because no "
        f"ranking for {_plural(k_dn, 'it', 'them')} was found on a keyword with search "
        f"results in both months")})
    k_lu = sum(1 for r in vis if r.get("uid") in this_lib_unknown)
    this_reasons.append({"count": k_lu, "reason": (
        f"{k_lu} {noun(k_lu)} {_be(k_lu)} not counted as new, because in the previous month "
        f"the library returned an error for {_plural(k_lu, 'its page', 'their pages')}")})
    unmatched = totals["not_comparable"]
    this_reasons.append({"count": unmatched, "reason": (
        f"{unmatched} {noun(unmatched)} could not be matched to the previous month, so "
        f"{_plural(unmatched, 'it is', 'they are')} not compared")})
    for p in sorted(by_platform):
        pd = by_platform[p]
        if "new" not in (pd.get("cannot_tell") or []):
            continue
        shown = sum(1 for r in vis if r.get("platform") == p)
        k = (shown - pd["not_comparable"] - len(pd["changed"]) - len(pd["unchanged"])
             - len(pd.get("shown_again") or []))
        this_reasons.append({"count": k, "reason": (
            f"{k} {_plat_name(p)} {noun(k)} {_be(k)} not counted as new, because some "
            f"{_plat_name(p)} {noun(2)} in the previous month could not be matched")})
    this_compared = totals["new"] + totals["changed"] + totals["unchanged"]
    this_month = month("This month", run_id, this_records, this_compared, this_reasons,
                       "new, changed or unchanged")

    if against_id is None:
        return {"this_month": this_month, "previous_month": None}

    # ---- previous month -----------------------------------------------
    # (platform, identity) pairs, because compare_runs matches within a platform.
    this_sids = {(r.get("platform"), this_id_map.get(r.get("uid"))) for r in this_records
                 if isinstance(r, dict) and not is_tomb(r)
                 and r.get("platform") in shared_platforms
                 and this_id_map.get(r.get("uid")) is not None}
    this_show_sids = {(r.get("platform"), this_id_map.get(r.get("uid"))) for r in vis
                      if r.get("platform") in shared_platforms
                      and this_id_map.get(r.get("uid")) is not None}
    u, t, hid, pvis = split(that_records, that_trashed)
    # A record hidden in the previous month but matched by a shown record of this
    # month is counted by this month's changed/unchanged, so it is compared.
    hid_counted = sum(1 for r in hid if r.get("platform") in shared_platforms
                      and (r.get("platform"), that_id_map.get(r.get("uid"))) in this_show_sids)
    prev_reasons = common(u, t, len(hid) - hid_counted, pvis, "this", skip=that_to_aside,
                          other_aside_n=this_aside_n, other_note_plats=this_note_platforms)
    prev_reasons += moved(pvis, that_to_aside, lambda k, P: (
        f"{k} {P} {noun(k)} {_be(k)} set aside this month, because the advertiser's name "
        f"does not match the competitor, so {_plural(k, 'it is', 'they are')} left out of "
        f"the count of those no longer there"))
    k_np = sum(1 for r in pvis if r.get("uid") in that_not_pulled_now)
    prev_reasons.append({"count": k_np, "reason": (
        f"{k_np} search-results {noun(k_np)} {_be(k_np)} not compared, because "
        f"{_plural(k_np, 'it was', 'they were')} pulled in the previous month and not "
        f"this month")})
    k_dn = sum(1 for r in pvis if r.get("uid") in that_dom_not_compared)
    prev_reasons.append({"count": k_dn, "reason": (
        f"{k_dn} {_plural(k_dn, 'domain', 'domains')} {_be(k_dn)} not compared, because no "
        f"ranking for {_plural(k_dn, 'it', 'them')} was found on a keyword with search "
        f"results in both months")})
    k_lu = sum(1 for r in pvis if r.get("uid") in that_lib_unknown)
    prev_reasons.append({"count": k_lu, "reason": (
        f"{k_lu} {noun(k_lu)} {_be(k_lu)} left out of the count of those no longer there, "
        f"because this month the library returned an error for "
        f"{_plural(k_lu, 'its page', 'their pages')}")})
    p_unmatched = gone_unknown = matched_hidden = 0
    for r in pvis:
        p = r.get("platform")
        if (p not in shared_platforms or r.get("uid") in that_to_aside
                or r.get("uid") in that_lib_unknown
                or r.get("uid") in that_not_pulled_now
                or r.get("uid") in that_dom_not_compared):
            continue
        sid = that_id_map.get(r.get("uid"))
        if sid is None:
            p_unmatched += 1
        elif (p, sid) not in this_sids:
            if "gone" in (by_platform.get(p, {}).get("cannot_tell") or []):
                gone_unknown += 1
        elif (p, sid) not in this_show_sids:
            matched_hidden += 1
    prev_reasons += [
        {"count": p_unmatched, "reason": (
            f"{p_unmatched} {noun(p_unmatched)} could not be matched to this month, so "
            f"{_plural(p_unmatched, 'it is', 'they are')} not compared")},
        {"count": gone_unknown, "reason": (
            f"{gone_unknown} {noun(gone_unknown)} {_be(gone_unknown)} left out of the count "
            f"of those no longer there, because some {noun(2)} in this month could not be "
            f"matched")},
        {"count": matched_hidden, "reason": (
            f"{matched_hidden} {noun(matched_hidden)} {_be(matched_hidden)} not compared, "
            f"because {_plural(matched_hidden, 'it matches one', 'they match ones')} "
            f"you moved to the trash in this month")},
    ]
    prev_compared = totals["gone"] + totals["changed"] + totals["unchanged"]
    previous_month = month("The previous month", against_id, that_records, prev_compared,
                           prev_reasons,
                           "no longer there, changed or unchanged")
    return {"this_month": this_month, "previous_month": previous_month}
