"""
adrecords.py -- what a scraped item IS, decided once for every reader of the ad tool.

Two things in a scraper's output have to be read the same way by the run's console,
the report bundle, the app's index, the closing lines and the creative downloader.
When each reader worked it out for itself they disagreed, so they live here, in one
place, and every reader imports them.

1. Is this item an ad, or a status record from the scraper?
   In a real run, a Facebook page with no active ads made the Meta scraper return ONE
   dataset item that is not an ad: {"error": "Ads not found", "errorCode":
   "ADS_NOT_FOUND", "url": <the page>, "pageInfo": {"ad_status": "This Page isn't
   currently running ads.", ...}}. Every reader counted it as an ad: "1 records
   saved", "Loaded 1 Meta ad record(s)", "1 ad: Meta 1", a blank block in the digest
   and a card in the app with no creative. The scraper's own statement ("this page is
   not running ads") appeared nowhere the user reads. The raw run file keeps the
   record untouched (raw files keep every record); this module only decides what is
   counted and shown, and what the user is told instead.

   Only the Meta scraper's shape is known, so only Meta items are tested. The Google
   and LinkedIn scrapers' status items have no known shape on file; none is invented.

2. Reading what the LinkedIn scraper reports for impressions and dates.
   It gives impressions as a BAND string ("5k-10k", "100k-150k") and dates as text
   ("Sep 14, 2026"), not numbers and epochs. Code that assumed numbers crashed the
   whole report step in a real run, and a date it could not read silently lost the
   ad's duration. The band is shown as LinkedIn gives it and never turned into one
   made-up number; a number is derived only to sort.

Stdlib only; Python 3.9.
"""

import math
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, unquote, unquote_plus

# --- 1. ad or status record -------------------------------------------------

# The one error code whose meaning is known: the library says the page runs no ads.
META_NO_ADS_CODE = "ADS_NOT_FOUND"


def _text(value):
    """A short plain string from any scraped value ('' for None, flags, lists, dicts)."""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (str, int, float)):
        try:
            return " ".join(str(value).split())
        except ValueError:  # an integer with thousands of digits will not print
            return ""
    return ""


# What a real Meta ad carries and a status record does not. The real runs on file show
# every ad with its archive id, its snapshot and its start and end dates (309 of 309 ads),
# and the one status shape on file ({"error", "errorCode", "url", "pageInfo"}) with none of
# them. The keys are compared with the case and underscores dropped, so a new spelling of
# the same word (adArchiveID, ad_archive_id, adId, id) is still the same key.
_AD_CONTENT_KEYS = frozenset({
    # the ad's own id, under any spelling the scrapers' versions use, and a bare "id"
    "adarchiveid", "adid", "id", "collationid",
    # what was shown: the snapshot, the copy, the creatives
    "snapshot", "body", "text", "adtext", "headline", "title", "caption", "description",
    "cards", "images", "videos", "creatives", "imageurl", "videourl", "imageurls",
    "videourls", "mediaurl", "creativeurl", "thumbnailurl",
    # when it ran
    "startdate", "enddate", "startdateformatted", "enddateformatted",
})


def _has_content(value):
    """True for a value that holds something: a text that is not empty, blank or a zero
    ("0"), a number that is not a flag or a zero, a non-empty list or dict. Meta's `ad_id`
    is often present and empty on a real ad, and a status record may carry a key with
    nothing in it, or a zero for a date it does not have, so those are no content."""
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, str):
        t = value.strip()
        if not t:
            return False
        try:
            return float(t) != 0       # "0", "00", "0.0"
        except ValueError:
            return True
    if isinstance(value, (int, float)):
        return value != 0      # a zero is how a record says "nothing" (startDate: 0)
    if isinstance(value, (list, tuple, dict)):
        return len(value) > 0
    return False


def _has_ad_content(rec):
    """True when the item carries ANY ad content: an id under any spelling (a bare `id`
    included), a snapshot, ad text, creative links or start and end dates."""
    for k, v in rec.items():
        if (isinstance(k, str) and k.lower().replace("_", "") in _AD_CONTENT_KEYS
                and _has_content(v)):
            return True
    return False


def _error_text(value):
    """The words of an error field, or '' when it holds none. Only a non-empty string
    is an error: False, 0 and None are how a scraper says "no error" and must never
    turn a real ad into a status record."""
    if isinstance(value, str):
        return " ".join(value.split())
    return ""


def is_meta_status_record(rec):
    """True when a Meta scraper item is a status/error record, not an ad.

    A status record has a status marker (an `errorCode` or an `error` that is a
    non-empty string) and NO ad content at all. An item that carries any ad content
    (see _AD_CONTENT_KEYS) is an ad whatever else it holds, even with an error string
    on it (a real ad can carry a note such as "partial creative"): the rule can only
    ever set aside a record that has nothing of an ad in it, so it can never turn a real
    ad into a silent zero. An error field that is a flag or a zero ("error": false,
    "errorCode": 0) is no marker at all."""
    if not isinstance(rec, dict):
        return False
    if _has_ad_content(rec):
        return False
    return bool(_error_text(rec.get("errorCode")) or _error_text(rec.get("error")))


def split_meta_items(items):
    """(ads, status_records) of a Meta scraper's items, order kept. Anything that is
    not a dict is in neither (it is not an ad and has nothing to say)."""
    ads, status = [], []
    for it in items if isinstance(items, list) else []:
        if is_meta_status_record(it):
            status.append(it)
        elif isinstance(it, dict):
            ads.append(it)
    return ads, status


# The first part of a facebook.com address that is not a page's own name: Facebook's
# own functions (the link redirector l.php, the sharer, search, hashtags, videos, login,
# settings ...) and the address forms that give the page by number. Read as a name, two
# unrelated addresses would share one ("l.php") and tie one page's status to another's.
_NOT_A_PAGE_NAME = frozenset({
    "profile.php", "pages", "people", "p", "pg", "groups", "public", "watch", "ads", "share",
    "sharer", "sharer.php", "permalink.php", "story.php", "photo", "photo.php", "photos",
    "events", "marketplace", "login", "login.php", "l.php", "hashtag", "search", "business",
    "reel", "reels", "video", "videos", "video.php", "notes", "media", "home.php", "help",
    "policies", "settings", "gaming", "dialog", "plugins", "tr", "privacy", "about", "careers",
    "stories", "live", "fundraisers", "messages", "notifications", "friends", "bookmarks",
    "saved", "recover", "checkpoint", "flx", "ajax", "api", "connect", "offers", "jobs",
    "learn", "terms", "oauth", "whatsapp", "instagram", "index.php", "ads_manager",
})
# A page's number inside an address name ("Acme-Plumbing-<id>") has to be long: every one of
# the 50 real Meta page ids on file has 12 to 16 digits (12: 8 pages, 15: 39, 16: 3), while a
# name can end in a year, a street number or a postcode ("Acme-Plumbing-2024"). 10 leaves
# room under the shortest real id and is far above those.
_MIN_NAME_ID_DIGITS = 10
# facebook.com and fb.com, with the sub-domains that serve a page (not l. / lm. link
# redirectors, developers., about. ...); scheme optional.
_FB_ADDRESS = re.compile(
    r"^(?:https?://)?(?:(?:www|m|web|mbasic|touch|mobile|business)\.)?(?:facebook\.com|fb\.com)"
    r"(?::\d+)?/([^?#]*)(?:\?([^#]*))?", re.IGNORECASE)


def _clean_id(value):
    """A page id as one comparable string: whole digits without leading zeros ('055' and
    '55' are one page). '' for none, for an id of only zeros ('0', '000': no page has it)
    and for anything that is not whole digits."""
    s = _text(value)
    if not s.isdigit():
        return ""
    return s.lstrip("0")


def normalize_page_id(value):
    """A page id for comparing: _clean_id for whole digits; any other text is returned as
    it is ('' when there is none); an all-zero id is no id."""
    s = _text(value)
    if s.isdigit():
        return s.lstrip("0")
    return s


def _id_from_last_part(part):
    """The page id at the end of an address part: a part of only digits ('123456' in
    pages/<Name>/123456), or '<Name>-<digits>' with at least _MIN_NAME_ID_DIGITS digits."""
    if part.isdigit() and len(part) <= 30:
        return _clean_id(part)
    m = re.search(r"-(\d+)$", part)
    if m and _MIN_NAME_ID_DIGITS <= len(m.group(1)) <= 30:
        return _clean_id(m.group(1))
    return ""


def _address_name(part):
    """The page name in the first part of an address: percent-decoded once, '' when it is
    one of Facebook's functions or does not look like a page name."""
    name = unquote(part)
    if (not name or name.lower() in _NOT_A_PAGE_NAME or name.lower().endswith(".php")
            or re.search(r"[/?&=#]", name)):
        return ""
    return name


def facebook_page_ref(url):
    """(page id, address name) a facebook.com address names, either may be ''.

    'facebook.com/AcmePlumbing' -> ('', 'AcmePlumbing'). An address that gives the page
    by number carries its id and NO name: 'facebook.com/profile.php?id=55' -> ('55', ''),
    'facebook.com/pages/Acme-Plumbing/55', 'facebook.com/pages/category/Plumber/Acme-<id>/'
    and the current form 'facebook.com/p/Acme-Plumbing-<id>/' -> (<id>, ''), and an ad library
    address for one page ('facebook.com/ads/library/?view_all_page_id=55') -> ('55', '').
    'facebook.com/pg/AcmePlumbing/ads' is the page AcmePlumbing. Hosts facebook.com, fb.com
    and their www, m, web, mbasic, touch, mobile and business sub-domains are read alike
    (a link redirector such as l.facebook.com is no page, and its target is not read), and
    the query key is read in any case. A name is percent-decoded once. Facebook's own
    functions (l.php, sharer.php, search, hashtag, videos ... see _NOT_A_PAGE_NAME) and
    'profile.php' are never a name: two different pages would then share one, and the
    month-to-month comparison would take one page's status for the other's. An id in an
    address name needs at least _MIN_NAME_ID_DIGITS digits ('Acme-Plumbing-2024' is a name
    with a year). Ids come back without leading zeros, and an all-zero id is no id."""
    m = _FB_ADDRESS.match(url.strip()) if isinstance(url, str) else None
    if not m:
        return "", ""
    parts = [x for x in m.group(1).split("/") if x]
    query = {k.lower(): v for k, v in parse_qsl(m.group(2) or "", keep_blank_values=False)}
    first = parts[0].lower() if parts else ""
    page_id = ""
    if first == "profile.php":
        v = query.get("id", "").strip()
        page_id = _clean_id(v) if len(v) <= 30 else ""
    elif first == "ads" and len(parts) > 1 and parts[1].lower() == "library":
        v = query.get("view_all_page_id", "").strip()
        page_id = _clean_id(v) if len(v) <= 30 else ""
    elif first in ("pages", "people", "p") and len(parts) > 1:
        page_id = _id_from_last_part(parts[-1])
    if first == "pg":
        name = _address_name(parts[1]) if len(parts) > 1 else ""
    else:
        name = _address_name(parts[0]) if parts else ""
    return page_id, name


# The address forms that stand for a page by its number. When the number is rejected
# (all zeros, not a number) what is left, "facebook.com/profile.php", names no page.
_BY_NUMBER = ("profile.php", "pages", "people", "p", "pg")


def _page_label(rec):
    """(how the place a status record is about is named, whether it is a keyword
    search, the words that introduce it in a sentence): 'facebook.com/AcmePlumbing' from
    the record's url, else the page name the scraper gave, else 'page id N', else a plain
    fallback. The ad library's own search url is named as the search it is, unless it asks
    for one page by number, which is that page. An address that is only a by-number form
    whose number was rejected ('facebook.com/profile.php?id=0') names no page and is not
    used as one."""
    url = _text(rec.get("url"))
    if "/ads/library" in url:
        page_id, _name = facebook_page_ref(url)
        if page_id:
            label = f"facebook.com/ads/library/?view_all_page_id={page_id}"
            return label, False, f"the page {label}"
        m = re.search(r"[?&]q=([^&]+)", url)
        if m:
            return f"the search for '{unquote_plus(m.group(1))}'", True, ""
        return "a Meta ad library search", True, ""
    if url:
        label = re.sub(r"^https?://(www\.)?", "", url).split("?")[0].split("#")[0].rstrip("/")
        page_id, _name = facebook_page_ref(url)
        if page_id and label.lower().endswith("/profile.php"):
            label = f"{label}?id={page_id}"       # the number IS the page's name here
        bare = False
        fb = _FB_ADDRESS.match(url.strip())
        if fb and not page_id:
            parts = [x for x in fb.group(1).split("/") if x]
            bare = len(parts) <= 1 and bool(parts) and parts[0].lower() in _BY_NUMBER
        if label and not bare:
            return label, False, f"the page {label}"
    info = rec.get("pageInfo")
    name = _text(info.get("page_name")) if isinstance(info, dict) else ""
    if name:
        return name, False, f"the page {name}"
    info_id = normalize_page_id(info.get("page_id")) if isinstance(info, dict) else ""
    if info_id:
        label = f"page id {info_id}"             # named by the one thing it gave
        return label, False, label
    label = "a page the scraper did not name"
    return label, False, label


def meta_status_note(rec):
    """What the user reads for one Meta status record.

    Returns {"kind", "where", "reason", "sentence"}:
      kind "no_ads": errorCode ADS_NOT_FOUND. The library itself says the page is not
        running ads; that is stronger than the tool's usual "came back empty
        (unconfirmed)", so it is said as a statement, attributed to the library.
      kind "error": any other error (page not found, blocked, rate-limited, an
        unknown code). Its ads are UNKNOWN, so the wording never claims "not running
        ads" and gives the scraper's own error text.
    reason: the clause for a gap line (no leading source name, no full stop);
    sentence: the same as a stand-alone line."""
    where, is_search, phrase = _page_label(rec)
    code = _error_text(rec.get("errorCode"))
    err = _error_text(rec.get("error"))
    info = rec.get("pageInfo") if isinstance(rec.get("pageInfo"), dict) else {}
    detail = ""
    if code.upper() == META_NO_ADS_CODE:
        said = _text(info.get("ad_status"))
        said = said[:200].rstrip(" .")
        quote = f': "{said}."' if said else ""
        if is_search:
            reason = f"{where} found no ads (the library said so{quote})"
        else:
            reason = f"{phrase} is not currently running ads (the library said so{quote})"
        kind = "no_ads"
    else:
        shown = err or code or "no error text"
        if err and code and err.lower() != code.lower():
            shown = f"{err}; code {code}"
        detail = shown
        reason = (f"the scraper returned an error for {where} ({shown}), so its ads are "
                  f"unknown (this does not say whether it runs ads)")
        kind = "error"
    # How the page can be recognised in another month's ads: the page id and name the
    # scraper gave (else the id the address carries), and the page's address name
    # (facebook.com/<name>). The comparison between months ties last month's ads to this
    # month's status record through them. A record whose own page id and whose address
    # give DIFFERENT ids contradicts itself: it is tied to no page (its ids are kept in
    # "conflict" so the sentence can say so), never to whichever id was read first.
    slug, url_id = "", ""
    if not is_search:
        url_id, slug = facebook_page_ref(_text(rec.get("url")))
    info_id = normalize_page_id(info.get("page_id"))
    page_id, page_name, conflict = info_id or url_id, _text(info.get("page_name")), []
    if info_id and url_id and info_id != url_id:
        conflict = [info_id, url_id]
        page_id, page_name, slug = "", "", ""
    return {"kind": kind, "where": where, "reason": reason, "detail": detail,
            "search": is_search, "page_id": page_id, "page_name": page_name,
            "page_slug": slug, "conflict": conflict,
            "requested": "" if is_search else requested_key(rec.get("url")),
            "sentence": f"Meta ad library: {reason}."}


def requested_key(value):
    """An address as one comparable string, for telling whether a status record and an ad
    came from the same request: lower case, no scheme, no 'www.' / 'm.', no trailing
    slash, percent-decoded once. '' for none. Whatever page id the scraper put on the ad,
    the address it was asked for is the same one."""
    s = unquote(_text(value)).strip().lower()
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^(?:www|m)\.", "", s)
    s = s.split("#")[0].rstrip("/")
    return s if "/" in s else ""


# Where a scraper keeps the address an item was requested for. The Meta ad items on file
# keep it in "url" (the keyword-search address for a search); the others are the names
# other scrapers of the same kind use.
_REQUEST_KEYS = ("url", "inputUrl", "input_url", "requestedUrl", "requested_url")


def _ad_requested(ad):
    for k in _REQUEST_KEYS:
        v = requested_key(ad.get(k))
        if v:
            return v
    return ""


def _name_key(value):
    return "".join(ch for ch in _text(value).lower() if ch.isalnum())


def _address_forms(value):
    """The comparable forms of an address name: letters and digits only, as it is and
    percent-decoded once more. An address copied through a second encoding step reads
    'Acme%2520Plumbing' after one decode; the form decoded once more is only ever
    compared for equality, never shown or used as the name."""
    forms = {_name_key(value), _name_key(unquote(_text(value)))}
    return forms - {""} - {_name_key(x) for x in _NOT_A_PAGE_NAME}


def page_relation(ids, names, slugs, o_ids, o_names, o_slugs):
    """How two descriptions of a page relate: "same", "differs" or "other".
    Page ids decide when both carry one. Else address names decide when both carry one:
    an address name belongs to exactly one page, so two different ones are two pages,
    and a shared display name must not join them. Where the address names differ but
    the names otherwise look alike the relation is "differs": the tool cannot say
    whether it is one page under two addresses (a renamed page) or two pages, and says
    "cannot tell". With an address name on only one side a shared name decides."""
    if ids and o_ids:
        return "same" if ids & o_ids else "other"
    if slugs and o_slugs:
        if slugs & o_slugs:
            return "same"
        return "differs" if names & o_names else "other"
    return "same" if names & o_names else "other"


def _ad_page_keys(ad):
    """({page ids}, {page names, letters and digits only}, {address-name forms}) a raw
    Meta ad carries: its page id (else the one in its page address) and the page's names
    (snapshot page name, page name, the address name)."""
    snap = ad.get("snapshot") if isinstance(ad.get("snapshot"), dict) else {}
    uri_id, uri_name = facebook_page_ref(snap.get("page_profile_uri"))
    pid = ""
    for v in (ad.get("page_id"), ad.get("pageId"), snap.get("page_id")):
        pid = _clean_id(v)
        if pid:
            break
    pid = pid or uri_id
    slugs = _address_forms(uri_name)
    names = {_name_key(x) for x in (snap.get("page_name"), ad.get("page_name"))} - {""}
    names = (names | slugs) - {_name_key(x) for x in _NOT_A_PAGE_NAME}
    return ({pid} if pid else set()), names, slugs


def note_is_about_ad(note, ad):
    """True when a status note is about the page of this raw Meta ad (see page_relation:
    ids, then address names, then names). A keyword search belongs to no one page.
    (marketing-viewer/compare.py ties notes to indexed ads the same way;
    a recurring test keeps the two in step.)"""
    if note.get("search") or not isinstance(ad, dict):
        return False
    ids, names, slugs = _ad_page_keys(ad)
    n_ids = {normalize_page_id(note.get("page_id"))} - {""}
    n_slugs = _address_forms(note.get("page_slug"))
    n_names = (n_slugs | {_name_key(note.get("page_name"))}) - {""}
    n_names -= {_name_key(x) for x in _NOT_A_PAGE_NAME}
    return page_relation(ids, names, slugs, n_ids, n_names, n_slugs) == "same"


def meta_status_notes(status_records, ads=None):
    """One note per status record; the same statement about the same place once. Two
    records about the same address that tie it to different pages are both kept: the month
    comparison reads a disagreement between them as 'cannot tell'.

    ads: the real Meta ads of the same run. When the library said a page is not running ads
    and this run also holds ads of that page, or ads asked for at the same address whatever
    page id they carry, the ads win: that note becomes kind "both"
    (the library answered both ways), whose sentence says the ads are used and the
    statement is not. It is told, never used as 'this page runs no ads'."""
    out, seen = [], set()
    for rec in status_records or []:
        n = meta_status_note(rec)
        key = (n["kind"], n["where"], n["reason"], n["page_id"], n["page_name"], n["page_slug"],
               tuple(n["conflict"]))
        if key in seen:
            continue
        seen.add(key)
        if n["kind"] == "no_ads" and ads and not n["search"]:
            # The ads win over "this page runs no ads": for the page the note names, and for
            # any ad that came from the same requested address, whatever page id it carries
            # (the scraper can answer one address with a status item and ads of a page whose
            # id is not the note's).
            by_page = [a for a in ads if note_is_about_ad(n, a)]
            by_request = [a for a in ads if a not in by_page and n["requested"]
                          and _ad_requested(a) == n["requested"]]
            same = by_page + by_request
            k = len(same)
            if k:
                where = n["where"]
                pids = []
                for a in by_request:       # the page ids the note does not account for
                    for pid in sorted(_ad_page_keys(a)[0]):
                        if pid not in pids and pid != normalize_page_id(n["page_id"]):
                            pids.append(pid)
                other = pids
                ads_word = f"ad{'' if k == 1 else 's'}"
                if other:
                    ids = ", ".join(other[:3]) + (f" and {len(other) - 3} more" if len(other) > 3 else "")
                    held = (f"this run also holds {k} {ads_word} for that address, from page id "
                            f"{ids}" + (f" (the statement is about page id {n['page_id']})"
                                        if n["page_id"] else ""))
                else:
                    held = f"this run also holds {k} {ads_word} from it"
                n = dict(n, kind="both", ad_count=k,
                         reason=(f"the library answered both ways for {where}: it said the page is "
                                 f"not currently running ads, and {held}, so the {ads_word} "
                                 f"{'is' if k == 1 else 'are'} used and the statement is not"))
                n["sentence"] = f"Meta ad library: {n['reason']}."
        out.append(n)
    return out


def records_saved_line(stage, items, batches=None):
    """The run_phase console line saying how many records a step saved, in the words
    it always used, except that a Meta step whose file also holds scraper status
    records says how many are ads and what the rest are. The file itself keeps every
    record, and billing never reads this number (it comes from Apify's charges)."""
    tail = f" across {batches} batches" if batches else ""
    if stage == "B4":
        ads, status = split_meta_items(items)
        if status:
            s = len(status)
            one = s == 1
            return (f"full: {len(ads)} ad{'' if len(ads) == 1 else 's'} saved{tail}; the file "
                    f"also keeps {s} status "
                    f"record{'' if one else 's'} from the scraper "
                    f"({'it is not an ad and is' if one else 'they are not ads and are'} "
                    f"not counted)")
    return f"full: {len(items)} records saved{tail}"


# --- 2. LinkedIn impression bands and dates ----------------------------------

_NUM = r"(\d+(?:\.\d+)?)\s*([kKmMbB])?"
_MULT = {"k": 1e3, "m": 1e6, "b": 1e9}
_RANGE = re.compile(rf"^{_NUM}\s*(?:-|\u2013|\u2014|to)\s*{_NUM}$", re.IGNORECASE)
_OPEN_TOP = re.compile(rf"^(?:{_NUM}\s*\+|(?:>|>=|\u2265)\s*{_NUM})$")
_BELOW = re.compile(rf"^(?:<|<=|\u2264)\s*{_NUM}$")
_PLAIN = re.compile(rf"^{_NUM}$")


def _scaled(num, suffix):
    return float(num) * _MULT.get((suffix or "").lower(), 1)


def impression_value(value):
    """(display, sort_number) for what a scraper gave as an impressions value.

    display: the band as the source gave it ('5k-10k'), or a count with thousands
      separators when the source gave a number, or None when there is nothing to show.
    sort_number: a number to sort by, or None when the value is not a recognised band
      or a plain number (None, a list, a dict, a flag, '1e400', free text): the ad is
      then ranked by duration instead, never by a number read out of odd text.

    Recognised bands: 'A-B' (also 'A to B'), 'A+' or '>A' (open at the top), '<A'
    (open at the bottom), and a plain number. The sort number is the UPPER end of a
    closed band: LinkedIn's bands do not overlap, so the upper end orders them exactly
    as the lower end would. An open band has no end of its own, so it is placed half a
    unit beyond its one stated number: '100k+' sorts just above '50k-100k' (which ends
    at 100000) and '<1k' just below '500-1k' (which ends at 1000), as an open top band
    is above the band under it and '<X' is below 'X-Y'."""
    if isinstance(value, bool) or value is None:
        return None, None
    if isinstance(value, (int, float)):
        # A number too large for a float (10**400) or not finite has no sort number.
        try:
            as_float = float(value)
            if not math.isfinite(as_float):
                return None, None
            n = int(value) if as_float.is_integer() else value
            return f"{n:,}", as_float
        except (OverflowError, ValueError):
            return None, None
    if not isinstance(value, str):
        return None, None
    s = " ".join(value.split())
    if not s or len(s) > 40:
        return None, None
    plain = s.replace(",", "")
    m = _RANGE.match(plain)
    if m:
        lo, hi = _scaled(m.group(1), m.group(2)), _scaled(m.group(3), m.group(4))
        return (s, hi) if lo <= hi else (None, None)
    m = _OPEN_TOP.match(plain)
    if m:
        return s, (_scaled(m.group(1), m.group(2)) if m.group(1) is not None
                   else _scaled(m.group(3), m.group(4))) + 0.5
    m = _BELOW.match(plain)
    if m:
        return s, _scaled(m.group(1), m.group(2)) - 0.5
    m = _PLAIN.match(plain)
    if m:
        return s, _scaled(m.group(1), m.group(2))
    return None, None


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_TEXT_DATE = re.compile(r"^([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})$")
_TEXT_DATE_DMY = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})$")
_ISO_FORMATS = ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%f%z",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d")


def parse_shown_date(value):
    """A scraped date as an aware UTC datetime, or None when it cannot be read.

    Reads what the three scrapers give: epoch seconds or milliseconds (a number or an
    all-digit string, Google), ISO text (Meta's formatted dates), and text dates such
    as 'Sep 14, 2026' (the LinkedIn scraper's documented format). Month names are
    matched by their first three letters here, never through the machine's locale. A
    date it cannot read is None, never a guess and never an error: the caller shows
    the duration as unknown."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
            n = float(value)
            if not math.isfinite(n) or n <= 0:
                return None
            if n > 1e12:  # milliseconds
                n /= 1000.0
            return datetime.fromtimestamp(int(n), tz=timezone.utc)
        if not isinstance(value, str):
            return None
        s = value.strip()
        for fmt in _ISO_FORMATS:
            try:
                dt = datetime.strptime(s, fmt)
            except ValueError:
                continue
            return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        m = _TEXT_DATE.match(s)
        if m:
            mon, day, year = m.group(1), m.group(2), m.group(3)
        else:
            m = _TEXT_DATE_DMY.match(s)
            if not m:
                return None
            day, mon, year = m.group(1), m.group(2), m.group(3)
        month = _MONTHS.get(mon[:3].lower())
        if not month:
            return None
        return datetime(int(year), month, int(day), tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
