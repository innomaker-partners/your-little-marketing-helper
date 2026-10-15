"""
Derived artifacts between stages — the "glue" that turns one stage's raw output
into the next stage's input. Called by run_phase.py after a full run.

For this tool the only glue is B1 -> competitor domains (discovery / name
resolution). Actor output field names vary; parsers try the common keys and
degrade gracefully (empty artifact) rather than crashing the pipeline.
"""

import json
from pathlib import Path
from urllib.parse import urlparse


def _read_json(path, default=None):
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def _latest_run(stage_dir):
    """The full run's raw JSON (falls back to the test run)."""
    for name in ("run_02_full.json", "run_01_test.json"):
        p = Path(stage_dir) / name
        if p.exists():
            return _read_json(p, [])
    return []


def _domain(url):
    try:
        net = urlparse(url if url.startswith("http") else f"https://{url}").netloc.lower()
        return net[4:] if net.startswith("www.") else net
    except Exception:
        return ""


# Marketplaces / socials / directories that aren't a competitor's own site.
# Extended with common business directories to prevent mistaking a directory
# listing for a competitor's official domain (used by the name->domain resolver).
_SKIP_DOMAINS = {
    "facebook.com", "instagram.com", "youtube.com", "google.com", "goo.gl",
    "linkedin.com", "tiktok.com", "wikipedia.org", "yelp.com", "x.com",
    "twitter.com", "pinterest.com", "reddit.com",
    # directories / aggregators
    "bark.com", "thumbtack.com", "angi.com", "houzz.com", "trustpilot.com",
    "yell.com", "checkatrade.com", "clutch.co", "g2.com", "capterra.com",
}


def _drop_stale_outputs(base, rel_paths, label, what):
    """Remove the derived files a step writes, and say so, when the step's latest
    result gave nothing to write them from. The same helper as in the search and pain
    tools' postprocess.py (written first for the search tool, generalised, then brought here
    for the competitor lookup, which used to turn an error object into
    an empty list).

    base: the folder the paths are under (here, the step's folder).
    rel_paths: this step's own derived files, relative to base. Never the step's raw
        result, run_02_full.json or run_01_test.json: that is the step's result as it
        stands, and it is not touched.
    label: the step's function name, for the console line. what: the plain name of
        what was removed.

    WHY: the file is written from the step's raw result and from nothing else. When
    the step returns an error, there is nothing to write, and an earlier run's file
    would stay in place looking like this run's. A step that is not run again keeps
    its file (this is called only by the step itself, after a run)."""
    removed = False
    for rel in rel_paths:
        p = Path(base) / rel
        try:
            p.unlink()
        except FileNotFoundError:
            continue  # nothing left over: the usual case
        except OSError as exc:
            print(f"{label}: WARNING: could not remove the {what} an earlier run "
                  f"left in this folder ({exc}); they may be old")
            continue
        removed = True
    if removed:
        print(f"{label}: removed the {what} an earlier run left in this folder, "
              "so they cannot be mistaken for this run's")


def drop_stage_outputs(stage, stage_dir, workdir):  # noqa: ARG001 (same signature in all three tools)
    """Remove the files an earlier run's step left behind, for a step that ended
    without a result in this run (run_phase calls it where a step is not
    run, fails, or is stopped, and only when this run would have run it again). The
    step's raw run files are its result as it stands and are never touched.

    The ad tool has one such file: the competitor list of the competitor lookup (B1).
    The Google ads step falls back on it when no resolved list exists, so an old one
    would send paid pulls to an earlier run's competitors. The platform steps write
    no derived file of their own: their run files are the result."""
    if stage == "B1":
        _drop_stale_outputs(stage_dir, ["competitor_domains.json"], "competitor lookup",
                            "competitor lists")


# --- the steps of a run that did not finish ----------------------
# The same block, word for word, in the postprocess.py of all three tools (a test
# pins that): it reads the records run_phase already wrote and nothing else.

_ENDED_HOW = {"ABORTED": "was stopped", "FAILED": "failed",
              "TIMED-OUT": "ran out of time", "TIMED_OUT": "ran out of time"}


def _record_of(path):
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def only_error_items(items):
    """A sentence for a step whose dataset is nothing but error items, else None.
    Apify reports a failed request as an item {"#error": true,
    "#debug": {"errorMessage": ...}} inside the dataset; a good item carries
    "#error": false (recorded search results do). Such a dataset is a failed step, never
    an empty answer. A dataset with some good items among the error items is not this
    (it is treated as the results it holds)."""
    if not isinstance(items, list) or not items:
        return None
    if not all(isinstance(i, dict) and i.get("#error") is True for i in items):
        return None
    dbg = items[0].get("#debug")
    why = dbg.get("errorMessage") if isinstance(dbg, dict) else None
    why = f" ({str(why).strip()[:150]})" if why and str(why).strip() else ""
    return f"the scraper returned only an error and no results{why}"


def unfinished_steps(workdir):
    """The steps of this run that did not finish, each as one plain sentence:
    [{"step": <name>, "sentence": <text>}] in step order, [] when every step that ran
    finished.

    WHY: a step that failed, was stopped by the budget cap, or whose scraper run did
    not finish left no results, and everything downstream (the index, the written
    report's facts, the app, the month comparison) then read that as "found nothing".
    run_phase already records each such step next to its output
    (.run_incomplete.json, .run_notice.json); the closing lines of the entry script
    name them. This reads the same records once, so that every other place repeats one
    sentence and none of them works it out again. An honest empty result (the step ran
    and found nothing) has no record and is not listed; a step the settings leave out
    is not listed either (the closing lines say that).

    The sentence for a failed or budget-stopped step also says that any results shown
    for it are an earlier run's, when the folder still holds a finished result file for
    it: the failed step wrote none of its own. A step whose record says it saved its
    result before it stopped (result_saved) is the exception: the result in
    the folder is this run's, and the sentence says it may be incomplete.

    A step a later run skipped carries the record kind
    "kept_after_failed_attempt" (run_phase._note_kept_after_failed_attempt). It is not
    an unfinished step: its finished result is in the folder, and the sentence says
    the latest attempt to fetch it again failed. It is listed under "kept_step", not
    "step": the month comparison takes "step" to mean records may be missing."""
    import stages as _stages
    out = []
    for key, spec in _stages.STAGES.items():
        d = Path(workdir) / spec["folder"]
        name = spec.get("name") or key
        inc = _record_of(d / ".run_incomplete.json") or {}
        by_kind = {}
        for e in inc.get("entries") or []:
            if isinstance(e, dict):
                by_kind.setdefault(e.get("kind"), e)
        earlier = (" Any results shown for this step are from an earlier run."
                   if (d / "run_02_full.json").exists() else "")
        missing = " That is not the same as finding nothing."
        k = by_kind.get("kept_after_failed_attempt")
        if k:
            if k.get("result_saved"):
                s = (f"{name}: the latest attempt to fetch it again saved its results and "
                     f"then stopped with an error, so what is shown for it comes from that "
                     f"attempt and may be incomplete.")
            else:
                why = ("stopped with an error" if k.get("was") != "budget_stop" else
                       "started, but the run reached the budget cap in your settings "
                       "part-way through it and nothing it collected was kept"
                       if k.get("part_way") else
                       "did not run, because the run reached the budget cap in your settings")
                s = (f"{name}: the latest attempt to fetch it again {why}, so the results "
                     f"shown for it were kept from an earlier fetch.")
            out.append({"kept_step": name, "sentence": s})
            continue
        if (by_kind.get("step_failed") or {}).get("result_saved"):
            s = (f"{name} stopped with an error after it had saved this run's results, "
                 f"so what is shown for it comes from this run but may be incomplete.")
        elif "step_failed" in by_kind:
            s = (f"{name} stopped with an error before it finished, so its results "
                 f"are missing from this run.{missing}{earlier}")
        elif "budget_stop" in by_kind:
            if by_kind["budget_stop"].get("part_way"):
                s = (f"{name} started, but the run reached the budget cap in your "
                     f"settings part-way through it, and nothing it collected was "
                     f"kept, so its results are missing from this run.{missing}{earlier}")
            else:
                s = (f"{name} did not run, because the run reached the budget cap in "
                     f"your settings, so its results are missing from this "
                     f"run.{missing}{earlier}")
        elif "run_ended" in by_kind:
            e = by_kind["run_ended"]
            how = _ENDED_HOW.get(str(e.get("status")), "did not finish")
            s = (f"{name}: the scraper run {how} before it finished, so what it "
                 f"collected is partial.{missing}" if e.get("records")
                 else f"{name}: the scraper run {how} before it finished and "
                      f"collected nothing, so its results are missing from this "
                      f"run.{missing}")
        elif _record_of(d / ".run_notice.json"):
            s = (f"{name}: the scraper reported that it did not work, so its "
                 f"results are missing from this run.{missing}")
        else:
            continue
        out.append({"step": name, "sentence": s})
    return out


def rebuild_dropped_outputs(stage, stage_dir, workdir, cfg):  # noqa: ARG001 (same signature in all three tools)
    """Build the competitor list again from the lookup result the folder kept, for a lookup run_phase
skips as complete; it spends nothing. WHY and when:
    the search tool's postprocess.rebuild_dropped_outputs. A kept result of nothing but
    errors has nothing to build from. The platform steps keep no file of their own."""
    if stage != "B1" or (Path(stage_dir) / "competitor_domains.json").exists():
        return
    entries = (_record_of(Path(stage_dir) / ".run_incomplete.json") or {}).get("entries")
    if not any(isinstance(e, dict) and e.get("kind") in
               ("step_failed", "budget_stop", "kept_after_failed_attempt")
               for e in entries or []):
        return
    try:
        kept = _latest_run(stage_dir)
        if not isinstance(kept, list) or only_error_items(kept):
            return
        _pp_b1(stage_dir)
    except Exception as exc:  # noqa: BLE001
        print(f"   could not build the competitor list again from the result this folder "
              f"kept ({exc}); it stays missing")
        return
    print("   built the competitor list again from the result this folder kept; "
          "nothing was charged")


def postprocess(stage, stage_dir, workdir, cfg):
    if stage == "B1":
        _pp_b1(stage_dir)
    elif stage == "LI":
        _pp_li(stage_dir)


# --- B1 -> competitor domains (discovery + resolution) ------------------------

def _pp_b1(stage_dir):
    items = _latest_run(stage_dir)
    # An error object is not "no competitors found". Iterating it read its
    # keys as rows, wrote an empty list and printed "0 competitor domains extracted",
    # so a failed lookup read as an honest empty answer. The search and pain steps skip
    # it, name it, and remove the file an earlier run left; this does the same. An
    # empty LIST (the lookup ran and found nothing) is still written as an empty list.
    if not isinstance(items, list):
        print(f"   _pp_b1: WARNING: actor returned a non-list payload "
              f"({type(items).__name__!r}); likely an error object, so no competitor "
              f"list was written (this is not an empty answer)")
        _drop_stale_outputs(stage_dir, ["competitor_domains.json"], "competitor lookup",
                            "competitor lists")
        return
    domains = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        results = it.get("organicResults") or it.get("results") or []  # VERIFY
        for r in results:
            url = (r or {}).get("url") or (r or {}).get("link")
            d = _domain(url or "")
            if d and d not in _SKIP_DOMAINS:
                domains[d] = domains.get(d, 0) + 1
    ranked = [d for d, _ in sorted(domains.items(), key=lambda kv: -kv[1])]
    (Path(stage_dir) / "competitor_domains.json").write_text(
        json.dumps(ranked, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"   → {len(ranked)} competitor domains extracted")


# --- LI -> canonical field names (scrapesage actor) ---------------------------
# The LI stage uses scrapesage/linkedin-ad-library-scraper (numeric-id scoping). scrapesage
# names some fields differently; the downstream report (e3_report.py) and creative
# downloader (creatives.py) read the canonical names below. This adapter adds the
# canonical alias to each record IN PLACE, non-destructively — the raw field is
# kept, and an alias is written only when the source key is present and the
# canonical key is absent. So a record that already uses a canonical name (or a
# future actor change) is left untouched rather than clobbered.
#
# source (scrapesage)   -> canonical (report/creatives)
_LI_ALIASES = {
    "bodyText": "body",
    "creativeTypeLabel": "format",
    "creativeType": "format",
    "firstShownDate": "firstShownAt",
    "lastShownDate": "lastShownAt",
    "totalImpressions": "totalImpressionsMax",
}


def _pp_li(stage_dir):
    """Add canonical field aliases to every LinkedIn ad record, in place, for each
    run file present. Idempotent: re-running never overwrites an existing canonical
    value. Reports how many records/fields were bridged so a mapping miss is loud,
    not silent (the whole point of the rebuild)."""
    touched_files = 0
    for name in ("run_01_test.json", "run_02_full.json"):
        p = Path(stage_dir) / name
        if not p.exists():
            continue
        items = _read_json(p, [])
        if not isinstance(items, list):
            continue
        bridged = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            for src, canon in _LI_ALIASES.items():
                if it.get(src) not in (None, "") and it.get(canon) in (None, ""):
                    it[canon] = it[src]
                    bridged += 1
            # imageUrl (singular) is the first of imageUrls, for the creative digest.
            imgs = it.get("imageUrls")
            if isinstance(imgs, list) and imgs and it.get("imageUrl") in (None, ""):
                it["imageUrl"] = imgs[0]
                bridged += 1
        p.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        touched_files += 1
        print(f"   → LinkedIn {name}: {len(items)} records, {bridged} canonical fields bridged")
    if not touched_files:
        print("   → LinkedIn: no run files to post-process")
