#!/usr/bin/env python3
"""
viewer_ctl.py -- the agent's ONE way to work with the local app (Python stdlib only).

The agent reads data freely (frontend_index.json is plain JSON), but every write and every
"what does the user see" question goes through this tool, so the safe way is also the easy
way. It never edits scraped data, an index or a report; it writes only the app's own files
(<run>/.viewer/annotations.json and views.json) through viewer_store.py, the same module the
app's server uses, with the same checks and lock.

Every command prints JSON on stdout. A refusal prints {"error": "..."} and exits 2.

--root is optional: without it, the app serves marketing-research/ in the current folder
(the research tools' default). If that folder does not exist, the command refuses with a
plain sentence telling the agent to ask the user (never to search for it).

    status        --root R                 is the app running for this folder? its address
    start         --root R                 start it in the background (or reuse the running one)
    stop          --root R                 stop the app started for this folder
    list-runs     --root R                 the runs the app shows (id, tool, subject, counts)
    current-view  --root R                 what the user has open: page, run, filters, visible records
    query         --root R --run ID [filters] [--limit N]
                                           the records the app would show for these filters
    url           --root R --run ID [filters]
                                           the address that opens the app on exactly that view
    open-default-browser --root R [--run ID [filters]]
                                           open the app in the user's own browser (fallback)
    views         --root R --run ID        the run's saved views
    save-view     --root R --run ID --title T [--note N] (--uids a,b | --uids-file F | [filters])
                                           save a named view the user can open in the app
    delete-view   --root R --run ID --view VIEW_ID
    mark          --root R --run ID --uid U [--favorite | --unfavorite] [--note TEXT | --clear-note]
    reindex       --root R --run ID [--config F]
                                           rebuild the run's index from disk (free, no scrape)
    compare       --root R --run ID [--against BASELINE_RUN_ID]
                                           what changed since the last run (plain code)

Filters (the app's own, same meaning):  --platform P  --format F  --advertiser A  --duration D
  --sentiment positive|negative|neutral  --subreddit S  --text WORDS  --favorites  --view VIEW_ID
  --change new|changed|gone              filter to records in that change category (query/url)
  --vs BASELINE_RUN_ID                   explicit baseline for --change (query/url)
Each list filter repeats (--platform maps --platform reddit). A value that does not exist in
the run is refused with the valid values, because the app would silently ignore it and the
user would see everything instead of what the agent promised.
"""

import argparse
import calendar
import json
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote, urlencode

# Python floor: a Mac's own python3 is 3.9. On anything older, say so in one
# plain line instead of failing later with a traceback.
if sys.version_info < (3, 9):
    sys.exit("This tool needs Python 3.9 or newer, and this is Python %d.%d. "
             "A Mac already has a new enough one: /usr/bin/python3" % sys.version_info[:2])

sys.path.insert(0, str(Path(__file__).resolve().parent))
import viewer_store as S  # noqa: E402
import viewer_server as V  # noqa: E402  (discover_runs, _safe_under_root, running_server)
import compare as _compare  # noqa: E402  (compare_runs -- one module for all callers)

_SKILLS_DIR = Path(__file__).resolve().parents[2]
_EMITTERS = {"ad": "ad-intelligence", "pain": "pain-and-messaging", "search": "search-intelligence"}
_SENTIMENT = {"positive": "high", "negative": "low", "neutral": "neutral",
              "high": "high", "low": "low", "unbanded": "unbanded"}
_DUR_BANDS = ("u1w", "1to4w", "1to3m", "3mplus", "unknown")


class CtlError(Exception):
    pass


# Carry error captured during the current command's _run() call (fail loud).
# Set to the exception message when ensure_carried raises; None otherwise.
# Reset to None at the start of every main() invocation.
_carry_error = None


def out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


# ---- locating things ---------------------------------------------------------
# The folder the three research tools write their runs to by default (their SKILL.md:
# "<parent> defaults to marketing-research/ in the current working directory").
DEFAULT_ROOT_NAME = "marketing-research"
NO_ROOT_MSG = ("No research folder was found here: ask the user where their research "
               "folder is and pass it with --root, and never search the disk for it.")


def _root(a):
    """The folder the app serves.

    --root given: used exactly as before. --root omitted: marketing-research/ in the
    current working folder, the tools' own default parent. WHY a default and not a
    required flag: when every command needed a path, the agent's first job was to guess
    one, and a guessing model searches the disk (`find /`, `find ~`). Finding the
    folder by convention is not a judgment call, so code does it; when the convention
    does not hold, the user is asked (never a search).
    """
    if getattr(a, "root", None) is None:
        root = (Path.cwd() / DEFAULT_ROOT_NAME).resolve()
        if not root.is_dir():
            raise CtlError(NO_ROOT_MSG)
        return root
    root = Path(a.root).expanduser().resolve()
    if not root.is_dir():
        raise CtlError(f"--root is not a folder: {root}")
    return root


def _run(root, run_id, need_index=True):
    global _carry_error
    d = V._safe_under_root(root, run_id)
    if d is None or not d.is_dir() or (d == root and run_id != "."):
        raise CtlError(f"no run folder '{run_id}' under {root}")
    if need_index:
        idx = S.read_json(d / "frontend_index.json", None)
        if not isinstance(idx, dict):
            # A scheduled month that was refused or stopped has no index because it
            # collected nothing (or only part): re-indexing is not the answer there.
            n = V._read_notification(d)
            if n and n["status"] == "refused":
                raise CtlError(f"run '{run_id}' did not run: its scheduled run was refused "
                               f"before collecting anything, so it has no records and "
                               f"nothing was spent ({n['message'].rstrip('.')}). Use an earlier run.")
            if n and n["status"] == "incomplete":
                raise CtlError(f"run '{run_id}' stopped before it finished, so it has no "
                               f"results to show ({n['message'].rstrip('.')}). Use an earlier run.")
            raise CtlError(f"run '{run_id}' has no index yet; run: viewer_ctl.py reindex "
                           f"--root \"{root}\" --run \"{run_id}\" (free)")
        try:
            marker = S.ensure_carried(root, d)  # Carry marks forward on first read
            # Surface permanent error markers (cycle, missing folder) via carry_error.
            # ensure_carried does not raise for these -- it writes a marker and returns it.
            if marker is not None and marker.get("error"):
                _carry_error = marker.get("error_plain") or marker.get("error")
        except Exception as e:
            _carry_error = str(e)  # Surface carry exceptions via carry_error field
        return d, idx
    return d, None


# ---- the app's filter rules, mirrored (app.js: recFormats, durationBand, hay, ...) ------
def _formats(r):
    t, d = r.get("tags") or {}, r.get("data") or {}
    if isinstance(t.get("formats"), list):
        return t["formats"]
    outl = []
    ex = d.get("format") or t.get("format")
    if ex:
        outl.append(ex)
    else:
        media = r.get("media") or []
        if any(m.get("type") == "video" for m in media):
            outl.append("VIDEO")
        elif any(m.get("type") == "image" for m in media):
            outl.append("IMAGE")
    cc = d.get("card_count")          # app.js: a number card_count, else the cards list's length
    cards = d.get("cards")
    slots = cc if isinstance(cc, (int, float)) and not isinstance(cc, bool) \
        else (len(cards) if isinstance(cards, list) else 0)
    if str(d.get("display_format") or "").upper() == "DCO":
        outl.append("DYNAMIC")
    elif slots > 1:
        outl.append("CAROUSEL")
    return outl


def _duration_band(r):
    d = (r.get("data") or {}).get("duration_days")
    if not isinstance(d, (int, float)) or isinstance(d, bool) or d < 0:
        return "unknown"
    return "u1w" if d < 7 else "1to4w" if d < 30 else "1to3m" if d < 90 else "3mplus"


def _polarity(r):
    t = r.get("tags") or {}
    return t.get("polarity") or t.get("banding")


def _subreddit(r):
    s = (r.get("data") or {}).get("subreddit")
    if not s:
        return None
    s = str(s)
    return s[2:] if s[:2].lower() == "r/" else s


def _tombstone(r):
    # Single definition lives in compare.py; this import keeps them in sync.
    # Lazy import avoids a circular dependency at module load time.
    import compare as _cmp
    return _cmp._tombstone(r)


def _hay(r):
    d = r.get("data") or {}
    parts = [r.get("text"), r.get("subject"), d.get("headline"), d.get("advertiser_name"),
             d.get("business_name"), d.get("place_name"), d.get("cta_text"), r.get("platform")]
    return " ".join(str(p) for p in parts if p).lower()


def _filters(a):
    return {
        "platforms": a.platform or [], "formats": a.format or [], "advertisers": a.advertiser or [],
        "durations": a.duration or [], "subreddits": [s[2:] if s[:2].lower() == "r/" else s
                                                      for s in (a.subreddit or [])],
        "polarities": [_SENTIMENT.get(s.lower(), s) for s in (a.sentiment or [])],
        "q": (a.text or "").strip().lower(), "favOnly": bool(a.favorites), "view": a.view or "",
        "change": getattr(a, "change", "") or "", "vs": getattr(a, "vs", "") or "",
    }


_NO_FILTERS = {"platforms": [], "formats": [], "advertisers": [], "durations": [],
               "subreddits": [], "polarities": [], "q": "", "favOnly": False, "view": "",
               "change": "", "vs": ""}


def _base_records(idx, ann):
    """What the app lists before any filter: trashed records never; on the review wall,
    deleted Reddit posts never. The search page lists everything."""
    recs = idx.get("records") or []
    tool = idx.get("tool")
    if tool == "search":
        return list(recs)
    trashed = ann["trashed"]
    return [r for r in recs if not trashed.get(r.get("uid"))
            and not (tool == "pain" and _tombstone(r))]


def _check_values(f, base, tool, views):
    """Refuse filter values the app would silently ignore."""
    avail = {
        "platforms": {r.get("platform") for r in base},
        "formats": {x for r in base for x in _formats(r)},
        "advertisers": {(r.get("data") or {}).get("advertiser_name") for r in base},
        "durations": {_duration_band(r) for r in base},
        "polarities": {_polarity(r) for r in base},
        "subreddits": {_subreddit(r) for r in base if r.get("platform") == "reddit"},
    }
    allowed = {"ad": ("platforms", "formats", "advertisers", "durations"),
               "pain": ("platforms", "polarities", "subreddits")}.get(tool, ())
    for key in ("platforms", "formats", "advertisers", "durations", "polarities", "subreddits"):
        if not f[key]:
            continue
        if key not in allowed:
            raise CtlError(f"the {tool} page has no '{key}' filter; it has: "
                           + (", ".join(allowed) or "no filters (search runs: use --text only "
                                                     "with query, not with url)"))
        valid = sorted(v for v in avail[key] if v is not None)
        bad = [v for v in f[key] if v not in avail[key]]
        if bad:
            raise CtlError(f"{key}: {bad} not in this run; valid: {valid}")
    if f["view"] and f["view"] not in {v["id"] for v in views}:
        raise CtlError(f"no saved view '{f['view']}'; views: {[v['id'] for v in views]}")


def _match(r, f, ann, views_by_id):
    if f["favOnly"] and not ann["favorites"].get(r.get("uid")):
        return False
    if f["platforms"] and r.get("platform") not in f["platforms"]:
        return False
    if f["formats"] and not any(x in f["formats"] for x in _formats(r)):
        return False
    if f["advertisers"] and (r.get("data") or {}).get("advertiser_name") not in f["advertisers"]:
        return False
    if f["durations"] and _duration_band(r) not in f["durations"]:
        return False
    if f["polarities"] and _polarity(r) not in f["polarities"]:
        return False
    if f["subreddits"] and (r.get("platform") != "reddit" or _subreddit(r) not in f["subreddits"]):
        return False
    if f["q"] and f["q"] not in _hay(r):
        return False
    if f["view"] and r.get("uid") not in views_by_id[f["view"]]:
        return False
    return True


def run_query(run_dir, idx, f):
    ann = S.load_annotations(run_dir)
    views = S.load_views(run_dir)
    base = _base_records(idx, ann)
    _check_values(f, base, idx.get("tool"), views)
    vb = {v["id"]: set(v.get("uids") or []) for v in views}
    return base, [r for r in base if _match(r, f, ann, vb)]


def _run_size(run_dir, idx):
    """How big one run is, counted in code, in numbers that add up.

    WHY: after a run a real session told the user "129 records: 20 Google Maps
    reviews, 20 Trustpilot reviews, and 15 relevant Reddit posts/comments". 129 was
    the app's count; 20/20/15 were the run log's per-source lines, which count what
    each source returned (Reddit posts only, before comments are added and removed
    posts are left out). Joined, they do not add up. This block gives the size the
    app shows, split by platform from the same records (_base_records, the app's own
    rule), and every record it leaves out with a plain reason:
    shown + sum(left_out_reasons) == records.
    """
    import compare as _cmp
    recs = idx.get("records") or []
    tool = idx.get("tool")
    ann = S.load_annotations(run_dir)
    base = _base_records(idx, ann)
    by_plat = {}
    for r in base:
        k = r.get("platform") or ""
        by_plat[k] = by_plat.get(k, 0) + 1
    shown_ids = {id(r) for r in base}
    rest = [r for r in recs if id(r) not in shown_ids]
    tombs = sum(1 for r in rest if tool == "pain" and _tombstone(r))
    trashed = len(rest) - tombs
    reasons = []
    if tombs:
        reasons.append({"count": tombs, "reason": (
            f"{tombs} removed Reddit {_cmp._plural(tombs, 'post', 'posts')} (deleted by the "
            f"author or a moderator) {_cmp._be(tombs)} not shown")})
    if trashed:
        reasons.append({"count": trashed, "reason": (
            f"{trashed} {_cmp._rec_noun(tool, trashed)} you moved to the trash "
            f"{_cmp._be(trashed)} not shown")})
    n, shown = len(recs), len(base)
    parts = ", ".join(f"{_cmp._plat_name(k) or 'no source'} {v}" for k, v in sorted(by_plat.items()))
    noun = _cmp._rec_noun(tool, n)
    if reasons:
        sentence = (f"This run has {n} {noun}; the app shows {shown} of them"
                    + (f": {parts}." if parts else ".") + " "
                    + "; ".join(r["reason"] for r in reasons) + ".")
    else:
        sentence = (f"This run has {n} {noun}, all shown" + (f": {parts}." if parts else "."))
    return {"run": Path(run_dir).name, "records": n, "shown": shown,
            "shown_by_platform": dict(sorted(by_plat.items())), "left_out": n - shown,
            "left_out_reasons": reasons, "sentence": sentence}


def app_url(base_url, run_id, f):
    params = []
    for key in ("platforms", "formats", "advertisers", "durations", "polarities", "subreddits"):
        params += [(key, v) for v in f[key]]
    if f["q"]:
        params.append(("q", f["q"]))
    if f["favOnly"]:
        params.append(("fav", "1"))
    if f["view"]:
        params.append(("view", f["view"]))
    change = f.get("change") or ""
    vs = f.get("vs") or ""
    if change:
        params.append(("change", change))
    # Every change link names the other month of its comparison. For
    # "gone" the page opens the baseline and vs is the newer run; for "new" and
    # "changed" the page opens this run and vs is its baseline. WHY on all three: a
    # link without it lets the app pick its own baseline, the wrong-target path.
    if vs and change:
        params.append(("vs", vs))
    frag = "#/run/" + quote(run_id, safe="-_.!~*'()")
    if params:
        frag += "?" + urlencode(params)
    return base_url.rstrip("/") + "/" + frag


def cmd_compare(a):
    """Run the cross-run comparison and return the compare_runs output."""
    root = _root(a)
    run_id = a.run
    d, idx = _run(root, run_id)
    against = getattr(a, "against", None) or None
    result = _compare.compare_runs(root, run_id, against=against)
    live = V.running_server(root)
    # Per-platform counts, counted here in code and placed before the long uid lists.
    # WHY: real sessions answered "what changed?" by counting the uid lists by eye and
    # got Reddit's unchanged count wrong (60 for 62) in two of two samples.  A category
    # in `cannot_tell` is left out, not given as 0, the same rule as `totals`.
    counts = {}
    for plat, pd in result.get("by_platform", {}).items():
        skip = set(pd.get("cannot_tell") or [])
        counts[plat] = {cat: len(pd.get(cat) or []) for cat in
                        ("new", "gone", "changed", "unchanged") if cat not in skip}
        # Ads that moved between shown and set aside, counted the same way
        # (present only when some did; never part of new or gone).
        # Search keywords, and domains, left out of new and gone because
        # the two months pulled different keywords; counted the same way, so the counts
        # match the sentence in `summary`.
        for cat in ("set_aside_now", "shown_again", "not_pulled_now", "not_pulled_before",
                    "domains_not_compared_now", "domains_not_compared_before",
                    "library_unknown_now", "library_unknown_before"):
            if pd.get(cat):
                counts[plat][cat] = len(pd[cat])
    out_obj = {}
    for k, v in result.items():
        if k == "by_platform":
            out_obj["counts_by_platform"] = counts
        out_obj[k] = v
    # Add convenience URLs: one per change category that has records
    if live and result.get("against"):
        against_id = result["against"]
        urls = {}
        for cat in ("new", "changed"):
            has = result["totals"].get(cat, 0) > 0
            if has:
                # vs: the baseline this count was made against
                urls[cat] = app_url(live["url"], run_id, {**_NO_FILTERS, "change": cat,
                                                          "vs": against_id})
        if result["totals"].get("gone", 0) > 0:
            # gone= opens the baseline; vs= tells it which run to compare against
            urls["gone"] = app_url(live["url"], against_id, {**_NO_FILTERS,
                                                              "change": "gone", "vs": run_id})
        out_obj["urls"] = urls
    return out_obj


# ---- commands ------------------------------------------------------------------
def cmd_status(a):
    root = _root(a)
    live = V.running_server(root)
    return {"running": bool(live), "url": live["url"] if live else None, "root": str(root),
            "runs": len(V.discover_runs(root))}


def cmd_start(a):
    root = _root(a)
    live = V.running_server(root)
    if live:
        return {"running": True, "reused": True, "url": live["url"]}
    vdir = root / S.VIEWER_DIR
    vdir.mkdir(exist_ok=True)
    log = open(vdir / "server.log", "a")
    kw = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": log}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200   # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True                   # survives the agent's shell call
    proc = subprocess.Popen([sys.executable, str(Path(V.__file__).resolve()), "--root", str(root),
                             "--no-open"], **kw)
    deadline = time.time() + 15       # an HTTP bind on localhost: latency-bound, so a timeout is right
    while time.time() < deadline:
        live = V.running_server(root)
        if live and live.get("pid") == proc.pid:
            return {"running": True, "reused": False, "url": live["url"], "pid": proc.pid,
                    "log": str(vdir / "server.log")}
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    tail = (vdir / "server.log").read_text(encoding="utf-8", errors="replace")[-800:]
    raise CtlError(f"the app did not start (exit code {proc.poll()}); log tail:\n{tail}")


def cmd_stop(a):
    root = _root(a)
    live = V.running_server(root)
    if not live:
        return {"stopped": False, "reason": "not running"}
    pid = live.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        raise CtlError("server.json has no valid pid; stop it from where it was started")
    import signal
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not V.running_server(root):
            return {"stopped": True, "pid": pid}
        time.sleep(0.1)
    raise CtlError(f"process {pid} did not stop")


def cmd_list_runs(a):
    root = _root(a)
    rows = V.discover_runs(root)
    for r in rows:
        rd = root / r["run_id"]
        r["carried"] = (rd / S.VIEWER_DIR / "carried.json").exists()
    return {"runs": rows}


def cmd_carried(a):
    """Return the carry marker for a run, or {carried: false} if none exists."""
    root = _root(a)
    d, _ = _run(root, a.run)
    marker = S.read_carried_marker(d)
    if marker is None:
        return {"carried": False, "run": a.run}
    # An error marker means the carry did not succeed; report carried=False.
    # carry_error is also merged in by main() via _carry_error (set in _run above).
    if marker.get("error"):
        return {"carried": False, "run": a.run, **marker}
    return {"carried": True, "run": a.run, **marker}


def cmd_current_view(a):
    root = _root(a)
    cv = S.read_current_view(root)
    live = V.running_server(root)
    if not cv:
        return {"running": bool(live), "current_view": None,
                "note": "the app has not reported a view yet (not opened, or opened before this version)"}
    age = None
    try:
        age = int(time.time() - calendar.timegm(time.strptime(cv.get("received_at", ""),
                                                              "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        pass
    return {"running": bool(live), "age_seconds": age, "current_view": cv}


def _apply_change_filter(root, run_id, f, idx):
    """If --change is set, return (effective_run_id, effective_idx, allowed_uids).

    effective_run_id / effective_idx: the run we query records from.
    allowed_uids: set of uids to keep (None = no filter).

    For change=gone we query the BASELINE run's records filtered to the gone
    uids.  For new/changed we query this run's records filtered to those uids.
    Returns (run_id, idx, allowed_uids) -- caller replaces run_dir and idx when
    effective_run_id != run_id.

    Returns a fourth value: the compare_runs result the filter was
    made from, None when there is no --change.  The callers name its baseline in
    their answer and put it in the link, so the month compared is always stated.

    Raises CtlError when --change is requested but there is no baseline.
    """
    change = f.get("change") or ""
    if not change:
        return run_id, idx, None, None

    vs = f.get("vs") or None
    cmp = _compare.compare_runs(root, run_id, against=vs if vs else None)
    if cmp.get("against") is None:
        if vs:
            raise CtlError(f"--vs {vs}: there is no such run in this folder")
        if cmp.get("skipped_partial"):
            # "no previous run" would be false; they exist and were passed over.
            raise CtlError(
                "--change requires a baseline run; the earlier months of this project "
                f"({', '.join(cmp['skipped_partial'])}) stopped before they finished, so none "
                "is used. Pass --vs BASELINE_RUN_ID to compare with one of them anyway")
        raise CtlError(
            "--change requires a baseline run; this run has no previous run (set repeat_of "
            "in the config, or pass --vs BASELINE_RUN_ID)"
        )

    if change == "new":
        uids = {entry["uid"] for plat in cmp["by_platform"].values()
                for entry in plat.get("new", [])}
        return run_id, idx, uids, cmp

    if change == "changed":
        uids = {entry["uid"] for plat in cmp["by_platform"].values()
                for entry in plat.get("changed", [])}
        return run_id, idx, uids, cmp

    if change == "gone":
        # gone uids belong to the baseline run; load the baseline index
        against_id = cmp["against"]
        against_root = Path(root)
        against_dir, against_idx = _run(against_root, against_id)
        gone_uids = {entry["uid"] for plat in cmp["by_platform"].values()
                     for entry in plat.get("gone", [])}
        return against_id, against_idx, gone_uids, cmp

    raise CtlError(f"--change must be new, changed, or gone; got: {change!r}")


def cmd_query(a):
    root = _root(a)
    d, idx = _run(root, a.run)
    f = _filters(a)
    effective_run_id, effective_idx, allowed_uids, cmp = _apply_change_filter(root, a.run, f, idx)
    effective_d = (root / effective_run_id) if effective_run_id != a.run else d
    base, hits = run_query(effective_d, effective_idx, f)
    if allowed_uids is not None:
        hits = [r for r in hits if r.get("uid") in allowed_uids]
    lim = max(0, a.limit)
    slim = [{"uid": r.get("uid"), "platform": r.get("platform"), "kind": r.get("kind"),
             "subject": r.get("subject"), "text": (r.get("text") or "")[:a.chars],
             "tags": r.get("tags"), "date": r.get("date")} for r in hits[:lim]]
    res = {"run": effective_run_id, "tool": effective_idx.get("tool"),
           "total_listed": len(base), "count": len(hits),
           "uids": [r.get("uid") for r in hits], "records": slim,
           "records_truncated": len(hits) > lim}
    if cmp is not None:
        # State the month the change was counted against, so a count is
        # never read without it (for "gone", "run" is the baseline whose records
        # these are, and "compared_run" is the month they are missing from).
        res["comparison"] = {"compared_run": a.run, "against": cmp["against"]}
        notes = []
        if cmp.get("against_partial"):
            res["comparison"]["against_partial"] = True
            notes.append(_compare._PARTIAL_BASELINE_SENTENCE)
        if cmp.get("run_partial"):
            # The compared month itself stopped early. Only the not-in-this-run
            # (gone) count is unsafe, so only a gone query carries the sentence.
            res["comparison"]["run_partial"] = True
            if f.get("change") == "gone":
                notes.append(_compare._PARTIAL_RUN_SENTENCE)
        if cmp.get("skipped_partial"):
            res["comparison"]["skipped_partial"] = cmp["skipped_partial"]
            notes.append(_compare._skipped_sentence(cmp["skipped_partial"], cmp["against"]))
        if notes:
            res["comparison"]["note"] = " ".join(notes)
    return res


def cmd_url(a):
    root = _root(a)
    d, idx = _run(root, a.run)
    f = _filters(a)
    if idx.get("tool") == "search" and f["q"]:
        raise CtlError("the search page has no text filter; open it without --text")
    effective_run_id, effective_idx, allowed_uids, cmp = _apply_change_filter(root, a.run, f, idx)
    effective_d = (root / effective_run_id) if effective_run_id != a.run else d
    base, hits = run_query(effective_d, effective_idx, f)
    if allowed_uids is not None:
        hits = [r for r in hits if r.get("uid") in allowed_uids]
    live = V.running_server(root)
    if not live:
        raise CtlError("the app is not running for this folder; run: viewer_ctl.py start")
    # For gone: the URL opens the baseline run, passing vs= so the app knows which run to compare
    if (f.get("change") or "") == "gone":
        url_f = {**f, "vs": a.run, "change": "gone"}
        return {"url": app_url(live["url"], effective_run_id, url_f), "count": len(hits),
                "run_size": _run_size(d, idx)}
    if cmp is not None:
        # New and changed links carry the baseline the count was made
        # against (the one --vs gave, or the one compare chose), not just gone links.
        f = {**f, "vs": cmp["against"]}
    return {"url": app_url(live["url"], effective_run_id, f), "count": len(hits),
            "run_size": _run_size(d, idx)}


def cmd_open_default(a):
    root = _root(a)
    live = V.running_server(root)
    if not live:
        raise CtlError("the app is not running for this folder; run: viewer_ctl.py start")
    url, size = live["url"], None
    if a.run:
        res = cmd_url(a)
        url, size = res["url"], res["run_size"]
    out = {"url": url, "opened": bool(webbrowser.open(url))}
    if size is not None:
        out["run_size"] = size
    return out


def cmd_views(a):
    d, _ = _run(_root(a), a.run)
    return {"views": [{**{k: v.get(k) for k in ("id", "title", "note", "created_by", "created_at")},
                       "count": len(v.get("uids") or [])} for v in S.load_views(d)]}


def cmd_save_view(a):
    root = _root(a)
    d, idx = _run(root, a.run)
    if idx.get("tool") == "search":
        raise CtlError("saved views work on the ad library and the review wall; the search "
                       "page has no filter panel to show them in")
    if a.uids and a.uids_file:
        raise CtlError("give --uids or --uids-file, not both")
    if a.uids:
        uids = [u.strip() for u in a.uids.split(",") if u.strip()]
    elif a.uids_file:
        raw = Path(a.uids_file).read_text(encoding="utf-8")
        try:
            uids = json.loads(raw)
        except ValueError:
            uids = [u.strip() for u in raw.splitlines() if u.strip()]
    else:
        _, hits = run_query(d, idx, _filters(a))
        uids = [r.get("uid") for r in hits]
    view = S.save_view(d, a.title, uids, note=a.note or "", created_by="agent", view_id=a.replace)
    live = V.running_server(root)
    res = {"saved": {"id": view["id"], "title": view["title"], "count": len(view["uids"])}}
    if live:
        res["url"] = app_url(live["url"], a.run, {**_NO_FILTERS, "view": view["id"]})
    return res


def cmd_delete_view(a):
    d, _ = _run(_root(a), a.run)
    S.delete_view(d, a.view_id)
    return {"deleted": a.view_id}


def cmd_mark(a):
    d, _ = _run(_root(a), a.run)
    done = {}
    if a.favorite or a.unfavorite:
        S.set_mark(d, "favorites", a.uid, True if a.favorite else None, S.AGENT_MARK_KINDS)
        done["favorite"] = bool(a.favorite)
    if a.note is not None or a.clear_note:
        S.set_mark(d, "notes", a.uid, None if a.clear_note else a.note, S.AGENT_MARK_KINDS)
        done["note"] = None if a.clear_note else a.note
    if not done:
        raise CtlError("nothing to do: give --favorite/--unfavorite and/or --note/--clear-note")
    return {"uid": a.uid, **done}


def _is_positional(rec):
    """True when the record's uid was derived from list position, not a native id.

    Checks uid_native first (explicit marker from newer indexes).  Falls back
    to shape matching for old indexes: a uid equal to "<platform>:<index>"
    (where index is raw_ref.index) is positional.
    """
    uid_native = rec.get("uid_native")
    if uid_native is False:
        return True
    if uid_native is True:
        return False
    # uid_native absent (old index): use shape check.
    uid = rec.get("uid") or ""
    ref = rec.get("raw_ref") or {}
    idx = ref.get("index")
    if not isinstance(idx, int):
        return False
    platform = uid.split(":", 1)[0] if ":" in uid else ""
    return uid == f"{platform}:{idx}"


def _build_uid_map_from_raw_ref(old_records, new_records):
    """Build {old_uid: new_uid} for positional records whose uid changed.

    A record is included in the map only when BOTH hold:
      (a) Its old uid was positional (the uid must be positional to be safe
          to remap; a native uid should never change across a simple reindex).
      (b) The old and new record content matches on text, date and platform
          (the raw file may have been rewritten between the old index and
          this reindex; a position match at a changed raw file maps the wrong
          review onto the user's mark).

    When the position check finds no match or the content check fails, a
    second attempt matches the old record against ALL new records by
    (platform, text, date).  Exactly ONE match maps to that record's uid
    (content-based recovery).  Zero or multiple matches leave the record
    unmapped (the mark is orphaned rather than silently mismapped).

    Returns (uid_map, recovered_by_content):
      uid_map            -- {old_uid: new_uid}, only pairs that differ
      recovered_by_content -- count of remaps found via content (not position)
    """
    # Index new records by raw_ref key (position lookup).
    new_by_ref = {}
    for rec in (new_records or []):
        if not isinstance(rec, dict):
            continue
        ref = rec.get("raw_ref")
        if isinstance(ref, dict) and ref.get("file") and isinstance(ref.get("index"), int):
            key = (ref["file"], ref["index"])
            new_by_ref[key] = rec

    # Index new records by content key (recovery lookup).
    # (platform, text, date) -> list of new records.
    new_by_content: dict = {}
    for rec in (new_records or []):
        if not isinstance(rec, dict):
            continue
        ck = (rec.get("platform"), rec.get("text"), rec.get("date"))
        new_by_content.setdefault(ck, []).append(rec)

    uid_map = {}
    recovered_by_content = 0
    for rec in (old_records or []):
        if not isinstance(rec, dict):
            continue
        old_uid = rec.get("uid")
        if not old_uid:
            continue
        # Only remap positional uids.  A native uid should never change
        # across a reindex; if it did, that is a problem to report, not to
        # silently heal.
        if not _is_positional(rec):
            continue
        ref = rec.get("raw_ref")
        if not (isinstance(ref, dict) and ref.get("file") and isinstance(ref.get("index"), int)):
            continue
        new_rec = new_by_ref.get((ref["file"], ref["index"]))
        position_ok = (
            new_rec is not None
            and new_rec.get("uid") not in (None, old_uid)
            and rec.get("text") == new_rec.get("text")
            and rec.get("date") == new_rec.get("date")
            and rec.get("platform") == new_rec.get("platform")
        )
        if position_ok:
            uid_map[old_uid] = new_rec.get("uid")
            continue
        # The content check failed (position absent or different review at
        # that slot).  Try content-based recovery: find ALL new records with
        # the same (platform, text, date).
        ck = (rec.get("platform"), rec.get("text"), rec.get("date"))
        matches = new_by_content.get(ck, [])
        if len(matches) == 1:
            recovered_uid = matches[0].get("uid")
            if recovered_uid and recovered_uid != old_uid:
                uid_map[old_uid] = recovered_uid
                recovered_by_content += 1
        # Zero or 2+ matches: leave unmapped (mark becomes orphaned or stays).
    return uid_map, recovered_by_content


# The files the three index builders (each tool's scripts/emit_index.py) read their
# records from, besides the step outputs. Every step writes <step folder>/
# run_02_full.json once it has fetched, also when it fetched 0 records (run_phase
# _save_dataset; run_phase's own "step done" test is that file). Search's processed
# files come from those step outputs; they are listed so a folder holding only them
# still counts as collected data.
_COLLECTED_EXTRA = ("reddit_reviews/reddit_neutral.json", "metrics/keyword_metrics.json",
                    "serp/rankings.json", "serp/domain_map.json")


def _has_collected_data(run_dir):
    """True when the run folder holds any output of a step that ran."""
    d = Path(run_dir)
    return (any(d.glob("*/run_02_full.json"))
            or any((d / rel).is_file() for rel in _COLLECTED_EXTRA))


def cmd_reindex(a):
    root = _root(a)
    d, _ = _run(root, a.run, need_index=False)
    # A refused scheduled month collected nothing (its folder holds a config and
    # notification.json). Re-indexing it wrote a 0-record index that made it look
    # like a finished, empty month. Refuse before writing anything.
    # A stopped ("incomplete") month may still be re-indexed: it holds real partial
    # results, and re-indexing it is a recovery path; its banner stays in the app.
    n = V._read_notification(d)
    if n and n["status"] == "refused":
        raise CtlError("This month's scheduled run did not start; there is nothing to index.")
    # Guard on the data, not only on the notification. A
    # malformed notification.json, or one with an unknown status, reads as None, and
    # a folder holding only its config was re-indexed into a 0-record month that
    # looked finished. A folder with no collected data at all is refused whatever its
    # notification says. A run whose steps ran and returned 0 records still has its
    # step outputs, so it re-indexes into an honest empty month.
    # WHY a different sentence: this folder may never have been scheduled
    # (a month prepared by hand and not yet run, or an ad run where only the
    # competitor lookup ran), so it must not speak of a scheduled run.
    if not _has_collected_data(d):
        raise CtlError("No ads, reviews or search results have been collected in this "
                       "folder yet, so there is nothing to index.")
    idx = S.read_json(d / "frontend_index.json", {}) or {}
    tool = idx.get("tool") or V._TOOL_BY_PREFIX.get(d.name.split("_", 1)[0])
    if tool not in _EMITTERS:
        raise CtlError(f"cannot tell which tool made '{a.run}' (folder name should start "
                       f"with adintel_, pain_ or search_)")
    script = _SKILLS_DIR / _EMITTERS[tool] / "scripts" / "emit_index.py"
    cmd = [sys.executable, str(script), "--workdir", str(d)]
    if a.config:
        cmd += ["--config", str(Path(a.config).expanduser().resolve())]
    # Capture the old records before running the emitter -- the emitter saves the old index to
    # .viewer/frontend_index.before_reindex.json, but we read them directly here to avoid a
    # second JSON parse after the fact.
    old_records = (idx.get("records") or []) if isinstance(idx, dict) else []
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise CtlError((p.stderr or p.stdout).strip() or f"re-index failed ({p.returncode})")
    new = S.read_json(d / "frontend_index.json", {}) or {}
    new_records = new.get("records") or []
    # Carry marks and view uids across any uid changes caused by the reindex.
    uid_map, recovered_by_content = _build_uid_map_from_raw_ref(old_records, new_records)
    new_index_uids = {r.get("uid") for r in new_records if r.get("uid")}
    # Always call remap_uid_marks so orphans are computed even when uid_map
    # is empty (e.g. every positional record failed the content check and
    # got no reviewId -- marks pointing at the old positional uids are still
    # orphaned and must be reported, not silently swallowed).
    remap_result = S.remap_uid_marks(d, uid_map, new_index_uids=new_index_uids)
    out = {"reindexed": a.run, "tool": tool, "records": len(new_records),
            "uid_changes": len(uid_map),
            "recovered_by_content": recovered_by_content,
            "marks_moved": remap_result["marks_moved"],
            "view_entries_moved": remap_result["view_entries_moved"],
            "orphaned_marks": remap_result["orphaned_marks"],
            "orphaned_uids": remap_result["orphaned_uids"],
            "conflicts": remap_result["conflicts"],
            "previous_index_kept_at": str(d / S.VIEWER_DIR / "frontend_index.before_reindex.json")
            if (d / S.VIEWER_DIR / "frontend_index.before_reindex.json").exists() else None}
    # The index builder's "WARNING: " lines (a "google_advertisers" key
    # the ad tool does not read) are passed on, so the rebuild does not report plain
    # success over an edit that changed nothing. Only present when there is one.
    warnings = [ln[len("WARNING: "):].strip() for ln in (p.stderr or "").splitlines()
                if ln.startswith("WARNING: ")]
    if warnings:
        out["warnings"] = warnings
    return out


def _add_filters(p):
    p.add_argument("--platform", action="append")
    p.add_argument("--format", action="append")
    p.add_argument("--advertiser", action="append")
    p.add_argument("--duration", action="append", choices=_DUR_BANDS)
    p.add_argument("--sentiment", action="append")
    p.add_argument("--subreddit", action="append")
    p.add_argument("--text")
    p.add_argument("--favorites", action="store_true")
    p.add_argument("--view")


def build_parser():
    ap = argparse.ArgumentParser(description="The agent's tool for the marketing-scraping local app.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, run=False, filters=False):
        p = sub.add_parser(name)
        p.add_argument("--root", help="the folder the app serves (default: "
                       "marketing-research/ in the current folder)")
        if run:
            p.add_argument("--run", required=True, help="run id, as list-runs prints it")
        if filters:
            _add_filters(p)
        p.set_defaults(fn=fn)
        return p

    add("status", cmd_status)
    add("start", cmd_start)
    add("stop", cmd_stop)
    add("list-runs", cmd_list_runs)
    add("current-view", cmd_current_view)
    q = add("query", cmd_query, run=True, filters=True)
    q.add_argument("--limit", type=int, default=20, help="records to print in full (all uids always)")
    q.add_argument("--chars", type=int, default=400, help="characters of text per printed record")
    q.add_argument("--change", choices=("new", "changed", "gone"),
                   help="filter to records in this change category (requires a baseline run)")
    q.add_argument("--vs", metavar="BASELINE_RUN_ID",
                   help="explicit baseline for --change; defaults to the run's repeat_of or same-project predecessor")
    u = add("url", cmd_url, run=True, filters=True)
    u.add_argument("--change", choices=("new", "changed", "gone"),
                   help="filter URL to records in this change category")
    u.add_argument("--vs", metavar="BASELINE_RUN_ID",
                   help="explicit baseline for --change")
    o = add("open-default-browser", cmd_open_default, filters=True)
    o.add_argument("--run")
    add("views", cmd_views, run=True)
    s = add("save-view", cmd_save_view, run=True, filters=True)
    s.add_argument("--title", required=True)
    s.add_argument("--note")
    s.add_argument("--uids", help="comma-separated uids")
    s.add_argument("--uids-file", help="JSON list or one uid per line")
    s.add_argument("--replace", metavar="VIEW_ID", help="update this view instead of adding one")
    dv = add("delete-view", cmd_delete_view, run=True)
    dv.add_argument("--view", dest="view_id", required=True)
    m = add("mark", cmd_mark, run=True)
    m.add_argument("--uid", required=True)
    g = m.add_mutually_exclusive_group()
    g.add_argument("--favorite", action="store_true")
    g.add_argument("--unfavorite", action="store_true")
    n = m.add_mutually_exclusive_group()
    n.add_argument("--note")
    n.add_argument("--clear-note", action="store_true")
    r = add("reindex", cmd_reindex, run=True)
    r.add_argument("--config")
    cmp = add("compare", cmd_compare, run=True)
    cmp.add_argument("--against", metavar="BASELINE_RUN_ID",
                     help="explicit baseline; defaults to the run's repeat_of or same-project predecessor")
    add("carried", cmd_carried, run=True)
    return ap


def main(argv=None):
    global _carry_error
    _carry_error = None
    a = build_parser().parse_args(argv)
    try:
        result = a.fn(a)
        # Merge carry error into output so callers (app, agents) see it.
        # The carry is non-fatal (the rest of the command succeeded), but the
        # error is visible so the user understands why marks are missing.
        if _carry_error is not None:
            result["carry_error"] = _carry_error
        out(result)
        return 0
    except (CtlError, S.StoreError) as e:
        out({"error": str(e)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
