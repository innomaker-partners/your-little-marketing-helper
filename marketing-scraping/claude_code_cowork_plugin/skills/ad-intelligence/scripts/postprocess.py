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


# --- a step stopped by the budget cap, in plain words -------------------------------
# WHY: a record said only "budget_stop", so the closing lines told the buyer "the run
# reached the budget cap" and offered one remedy, to raise the cap. In a real buyer's run
# $0.0362 of a $3.00 cap had been spent when a step whose forecast was $6.00 was refused:
# the cap was never reached, the forecast was over it, and nothing was charged for the
# step. run_phase._annotate_budget_stop now keeps the figures in the record
# (forecast_usd, run_spent_usd, cap_usd; first_pass for a small first search), and these
# functions say what the record says. A record without them (written before they were
# kept, or a stop on spend already made) gets no sentence from here: the readers fall
# back to words that are true of both. "Reached the cap" is left to the stop that is that.
# The phrases are the cause only, without a subject, so the closing lines, the app and the
# report can each put their own subject and ending around the same words.

# The setting that makes each step smaller: the remedy beside "raise the cap".
STEP_SIZE_SETTING = {
    "B1": "list fewer competitor_names or seed_terms in your settings",
    "B4": "lower meta_limit_per_source in your settings",
    "B5": "lower google_max_items in your settings",
    "LI": "lower linkedin_max_items in your settings",
}

def _usd_words(x):
    """A dollar figure the way a stop sentence prints it: two decimals from a dollar up
    (and for 0), four below, so $0.0362 is not read as $0.04."""
    x = float(x)
    return f"${x:.2f}" if x >= 1 or x == 0 else f"${x:.4f}"


def budget_figures(e):
    """(forecast, spent, cap) of a budget_stop record as floats, or None when the record
    does not carry all three as finite amounts (a stop on spend already made, or a
    record written before the figures were kept)."""
    try:
        f, s, c = (float(e[k]) for k in ("forecast_usd", "run_spent_usd", "cap_usd"))
    except (KeyError, TypeError, ValueError):
        return None
    if any(x != x or x in (float("inf"), float("-inf")) or x < 0 for x in (f, s, c)):
        return None
    return f, s, c


def make_step_smaller(stage):
    """The remedy beside "raise the cap", naming the step's own setting when known."""
    how = STEP_SIZE_SETTING.get(stage)
    return f"make the step smaller ({how})" if how else "make the step smaller"


def minimum_limit(e):
    """The smallest limit the scraper accepts, in dollars, from a budget_stop record that was
    written because that limit, not the step's size, did not fit what was left of the cap
    (run_phase._cap_for); None for every other stop."""
    try:
        m = float(e.get("min_usd"))
    except (TypeError, ValueError):
        return None
    return m if m == m and 0 < m < float("inf") else None


def _left_words(left, need):
    """What is left, as a stop sentence prints it: rounded DOWN, never up, with as many
    decimals as _usd_words gives (more when the shorter figure would read the same as the
    smallest limit). Rounded to nearest, 0.4996 left of a 0.50 minimum read "less than
    $0.5000, and only $0.5000 was left", which contradicts itself."""
    left = max(0.0, float(left))
    shown = _usd_words(need)
    for digits in ((2, 4, 6, 8) if left >= 1 else (4, 6, 8)):
        scale = 10 ** digits
        text = f"${int(left * scale + 1e-6) / scale:.{digits}f}"
        if text != shown:
            return text
    return text


def ceiling_limit(e):
    """The approved run ceiling, in dollars, from a budget_stop record that run_phase._cap_for
    wrote because that ceiling (not the budget cap in the settings) was what ran out; None
    for every other stop."""
    try:
        c = float(e.get("ceiling_usd"))
    except (TypeError, ValueError):
        return None
    return c if c == c and 0 < c < float("inf") else None


def limit_words(e, cap):
    """The limit a stop sentence names as the one that ran out: the approved ceiling when the
    record says so, else the budget cap in the user's settings."""
    from budget import cap_text
    ceiling = ceiling_limit(e)
    if ceiling:
        return f"your approved ceiling (${cap_text(ceiling)})"
    return f"the budget cap in your settings (${cap_text(cap)})"


def raise_what(e):
    """What the user raises to get the step started: the approved ceiling when that is what ran
    out (raising budget_caps would change nothing then), else budget_caps in the settings."""
    return "the approved ceiling" if ceiling_limit(e) else "budget_caps in your settings"


def or_smaller(e, stage=None):
    """', or make the step smaller (...)' beside 'raise the cap', or '' for a stop caused by the
    scraper's smallest limit: a smaller step does not lower that limit, so it is not offered."""
    if minimum_limit(e):
        return ""
    return ", or " + make_step_smaller(stage if stage is not None else e.get("stage"))


def refused_before_start(e):
    """Why a step was not started, from its budget_stop record, or None when the record
    carries no figures: its cost forecast plus what the run had spent is more than the
    cap. Nothing was launched, so nothing was charged for the step."""
    fig = budget_figures(e)
    if not fig or e.get("part_way"):
        return None
    from budget import cap_text
    f, s, c = fig
    m = minimum_limit(e)
    if m:
        return (f"was not started, because the scraper cannot start for less than "
                f"{_usd_words(m)} (the smallest limit it accepts) and only "
                f"{_left_words(c - s, m)} of {limit_words(e, c)} was left")
    return (f"was not started, because its cost forecast (at most {_usd_words(f)}) plus "
            f"what this run had already spent ({_usd_words(s)}) is more than the budget "
            f"cap in your settings (${cap_text(c)})")


def later_step_cause(e, remedy=True):
    """Why a step never started, from its budget_stop record, or None when the record
    does not say an earlier step was stopped first (after_step, written by
    run_phase._note_budget_stop for the steps that come after the stopped one).

    WHY: those steps were never priced on their own, so "the budget cap did not allow
    it, raise the cap or make the step smaller" was a size remedy for a step that was
    never too big. The cause is the earlier step; running that step again, as its own
    line describes, runs this one too. Nothing was launched, so nothing was charged.
    remedy=False leaves the remedy clause out, for a step whose folder still holds a
    finished result: running the folder again skips that step, so it would be untrue."""
    after = e.get("after_step")
    if not after or e.get("part_way"):
        return None
    import stages as _stages
    name = (_stages.STAGES.get(str(after)) or {}).get("name") or str(after)
    why = f"did not run, because an earlier step ({name}) was stopped by the budget cap"
    return why + ("; running that step again, as described above, runs this one too"
                  if remedy else "")


def stopped_after_part(e):
    """Why a step stopped part-way, from its budget_stop record, or None when the record
    carries no forecast (then it was spend already made, and the readers say "reached
    the budget cap", which is true of that). The part that ran was charged and the cost
    estimate for the step, worked out from what that part cost, did not fit the cap.
    That figure is an estimate, not a ceiling, and it is for the step, not for "the
    rest": the sentence says "about", never "at most". A record with first_pass is a
    step whose small first search finished: what it found, and what the full search is
    estimated to cost, are said. The words "found no" are used only when the record
    says that search succeeded: a first search that failed, timed out or was aborted is
    a failed step (step_failed), never a budget stop that "found nothing"."""
    fig = budget_figures(e)
    if not fig or not e.get("part_way"):
        return None
    from budget import cap_text
    f, s, c = fig
    cap = limit_words(e, c)
    m = minimum_limit(e)
    if m:
        try:
            charged = float(e.get("spent_usd") or 0.0)
        except (TypeError, ValueError):
            charged = 0.0
        return (f"started, and the part that ran was charged {_usd_words(charged)}. The next "
                f"run of it could not start: the scraper cannot start for less than "
                f"{_usd_words(m)} (the smallest limit it accepts) and only "
                f"{_left_words(c - s, m)} of {cap} was left, so the rest was not started")
    fp = e.get("first_pass")
    if isinstance(fp, dict):
        try:
            found, paid = int(fp.get("records") or 0), float(fp.get("charged_usd") or 0.0)
        except (TypeError, ValueError):
            found, paid = 0, 0.0
        # The noun comes from the step that wrote the record, never a fixed word:
        # a review or keyword step must not be told it found "ads".
        unit = str(fp.get("unit") or "results")
        if found:
            return (f"started with a small first search that found at least {found} "
                    f"{unit} and was charged {_usd_words(paid)}. From that, the full "
                    f"search is estimated at about {_usd_words(f)}; with the "
                    f"{_usd_words(s)} this run had already spent that is more than "
                    f"{cap}, so the full search was not run")
        if fp.get("succeeded") is True:
            return (f"started with a small first search that found no {unit} for the "
                    f"websites it covered and was charged {_usd_words(paid)}. That says "
                    f"nothing about the other websites, so the full search is still "
                    f"priced at up to {_usd_words(f)}, which with the {_usd_words(s)} "
                    f"this run had already spent is more than {cap}, so the full "
                    f"search was not run")
        return (f"started with a small first search that was charged {_usd_words(paid)} "
                f"and did not give a complete answer, so it says nothing about how many "
                f"{unit} there are. The full search is priced at up to {_usd_words(f)}, "
                f"which with the {_usd_words(s)} this run had already spent is more than "
                f"{cap}, so the full search was not run")
    try:
        charged = float(e.get("spent_usd") or 0.0)
    except (TypeError, ValueError):
        charged = 0.0
    return (f"started, and the part that ran was charged {_usd_words(charged)}. Then its "
            f"cost estimate, worked out from what that part cost (about {_usd_words(f)}), "
            f"plus what this run had spent by then ({_usd_words(s)}), was more than "
            f"{cap}, so the rest was not started")


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
                if k.get("was") != "budget_stop":
                    why = "stopped with an error"
                elif k.get("part_way"):
                    cause = stopped_after_part(k)
                    why = (f"{cause}, and nothing it collected was kept" if cause else
                           "started, but the run reached the budget cap in your settings "
                           "part-way through it and nothing it collected was kept")
                else:
                    later = later_step_cause(k)
                    cause = None if later else refused_before_start(k)
                    why = (later if later else
                           f"{cause} (nothing was charged for it)" if cause else
                           "did not run, because the budget cap in your settings did not "
                           "allow it")
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
            b = by_kind["budget_stop"]
            if b.get("part_way"):
                cause = stopped_after_part(b)
                if cause:
                    s = (f"{name} {cause}, and nothing it collected was kept, so its "
                         f"results are missing from this run.{missing}{earlier}")
                else:
                    s = (f"{name} started, but the run reached the budget cap in your "
                         f"settings part-way through it, and nothing it collected was "
                         f"kept, so its results are missing from this run.{missing}{earlier}")
            else:
                later = later_step_cause(b)
                cause = None if later else refused_before_start(b)
                if later:
                    s = (f"{name} {later}. Nothing was charged for it and its results "
                         f"are missing from this run.{missing}{earlier}")
                elif cause:
                    s = (f"{name} {cause}, so nothing was charged for it and its results "
                         f"are missing from this run.{missing}{earlier}")
                else:
                    s = (f"{name} did not run, because the budget cap in your settings "
                         f"did not allow it, so its results are missing from this "
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
