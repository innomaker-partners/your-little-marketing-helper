#!/usr/bin/env python3
"""
Stage runner — runs a set of Apify stages SEQUENTIALLY, test-first, budget-gated,
auto-logging. Runs this tool's Apify stages (B2a places, B2b reviews, and review
sources). The higher-level pain/messaging flow
(subject-mode resolution, the corpus-derived extractor, the E1/E2 report) is
coordinated on top of this in a separate entry point; this module is the
reusable "run one Apify stage safely" engine plus a thin CLI for isolated
single-stage runs.

    python3 run_phase.py 1 --config ../PAIN_CONFIG.json --workdir <run>
    python3 run_phase.py 1 --only B2a --test-only --config ... --workdir ...
    python3 run_phase.py 1 --dry-run --config ... --workdir ...   # plan + inputs, spend nothing

For each stage: build TEST input (3-5 items) → check budget → run → recompute the
per-unit rate from ACTUAL test cost → estimate the full run → check budget → run
full → save output → append RUN_LOG.md + commands.sh. Each run is charged to the
budget the moment it ends, before its output is fetched.
"""

import argparse
import hashlib
import json
import re as _re
import sys
from collections import defaultdict
from datetime import date as _date_cls, timedelta as _timedelta
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import config as C
import apify_client as A
from budget import Budget, BudgetExceeded, cap_text, run_ledger_spent
from runlog import append_run_log, append_command
from stages import STAGES, PHASE_ORDER
import stages as S
import postprocess as P
from repeat import _FINGERPRINT_EXCLUDE as _REPEAT_FP_EXCLUDE, _read_config_if_present as _read_run_config


# ---------------------------------------------------------------------------
# Incremental review scraping (FROZEN)
# ---------------------------------------------------------------------------
# Which stages support date-filtered incremental scraping (FROZEN).
# Do NOT add Reddit (corpus meaning would change), AS/GP/CP (no date field).
# The filter_field, boundary semantics, target_field, and id_field are all
# confirmed against real actor schemas and the fake_apify implementation.
_D2_INCREMENTAL = {
    "B2b": {
        "filter_field":   "reviewsStartDate",   # inclusive (date >= cutoff)
        "id_field":       "reviewId",
        "target_field":   "url",                # place URL — in every B2b record
        "held_raw_file":  "run_02_full.json",   # B2b has NO postprocess; raw IS the output
        "n_cfg_key":      "max_reviews_per_place",
        "n_default":      20,
        "extra_margin_days": 0,
        # Key in the actor input whose list length = expected number of distinct targets.
        # Used by the attribution guard in _do_incremental_merge.
        "n_targets_input_key": "startUrls",
        # Entity fields present in every raw record that reflect the place/company's
        # current aggregate state.  Refreshed on carried records per target when the
        # target has ≥1 new review (entity field refresh, FROZEN).
        "entity_fields": ["totalScore", "reviewsCount"],
        # The actor's own id of the place in each record, and its name: used to
        # match a zero-new place to its start URL for the entity refresh
        # and to name it to the user when it cannot be matched.
        "identity_field": "placeId",
        "name_field":     "title",
    },
    "TP": {
        "filter_field":   "publishedAfter",     # EXCLUSIVE (date > cutoff); one extra day back
        "id_field":       "reviewId",           # intact in run_02_full.raw.json (not a PII field)
        "target_field":   "companyDomain",      # domain string in every raw TP record
        "held_raw_file":  "run_02_full.raw.json",  # redacted PII; reviewId + companyDomain intact
        "n_cfg_key":      "trustpilot_max_reviews",
        "n_default":      100,
        "extra_margin_days": 1,                 # compensate for exclusive boundary
        "n_targets_input_key": "companyUrls",
        "entity_fields": ["companyTrustScore", "companyTotalReviews"],
        "identity_field": None,             # the domain itself is the identity
        "name_field":     "companyName",
    },
    "G2": {
        "filter_field":   "fromDate",           # inclusive (date >= cutoff)
        "id_field":       "reviewId",
        "target_field":   "productSlug",
        "held_raw_file":  "run_02_full.raw.json",
        "n_cfg_key":      "saas_max_reviews",
        "n_default":      100,
        "extra_margin_days": 0,
        "n_targets_input_key": None,            # G2 is always a single-product run
        "entity_fields": [],
    },
}

# Regex patterns for parsing commands.sh written by runlog.append_command.
_CMD_TS_RE   = _re.compile(r'^# (\d{4}-\d{2}-\d{2}) \d{2}:\d{2} UTC')
_CMD_DATA_RE = _re.compile(r"^\s+-d\s+'(.*)'$")
# Matches the [note: ...] suffix that append_command writes when note is given.
# Used by _parse_commands_sh to skip auxiliary runs (e.g. entity-refresh calls)
# so the identity check in _incremental_info always compares against the real
# full-run input, never against a small refresh call (THE TRAP fix).
_CMD_NOTE_RE = _re.compile(r'\[note:\s*([^\]]+)\]')
# A block written since the replay fix sends the token in this header
# line (never ?token= in the address) and writes each ' in its JSON as '\''.
# The header line marks the block's form, so a commands.sh written before the
# fix (and a file holding both forms, after a re-run) parses exactly as before.
_CMD_AUTH_RE = _re.compile(r'^\s+-H\s+"Authorization: Bearer \$APIFY_TOKEN"')


def _cfg_fingerprint(cfg):
    """SHA-256 config fingerprint using the same logic as repeat.py cmd_fingerprint.
    Reuses _REPEAT_FP_EXCLUDE so the two can never drift apart."""
    filtered = {k: v for k, v in cfg.items()
                if k not in _REPEAT_FP_EXCLUDE and not k.startswith("_")}
    canonical = json.dumps(filtered, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_commands_sh(path, skip_notes=None):
    """Parse commands.sh -> list of (date_str, actor_input_dict), oldest first.

    Each block in commands.sh is:
      # YYYY-MM-DD HH:MM UTC  ·  actor  (run ..., dataset ...)  [note: ...]
      curl ...
        -H '...' \\
        -d '<JSON>'

    The JSON is always compact single-line (json.dumps with no indent).
    Returns [] when the file is missing, unreadable, or entirely unparseable.

    Two forms: a block with the ``-H "Authorization: Bearer
    $APIFY_TOKEN"`` line writes each ' in its JSON as '\'' (shell-safe), which is
    undone here; a block without it (written before the fix) is read raw, as
    it always was.

    skip_notes: if given (a set/frozenset of strings), blocks whose header
    contains ``[note: <value>]`` where <value> is in skip_notes are silently
    excluded from the results.  Use this to prevent auxiliary runs (e.g.
    entity-refresh calls written by _run_zero_target_refresh) from being
    mistaken for the canonical full-run entry when _incremental_info picks
    the last entry to derive the identity fingerprint.

    THE TRAP: the incremental-info parser uses the LAST entry as the full-run
    input for the identity check.  If a refresh call is the last entry, its
    1-item input would permanently fail the identity check next month, causing
    a silent full-scrape fallback every cycle after any zero-new-review month.
    Skipping entries with note="entity-refresh" closes this gap.
    """
    p = Path(path)
    if not p.exists():
        return []
    try:
        # WHY "\n" only: splitlines() also splits on
        # U+2028, U+2029 and U+0085, which json.dumps(ensure_ascii=False) leaves raw
        # inside a -d line. A business name holding one broke its block, and the
        # month fell back to a full re-scrape that costs more. read_text already
        # turns "\r\n" into "\n"; json.dumps escapes every other control character.
        lines = p.read_text(encoding="utf-8").split("\n")
    except OSError:
        return []
    entries = []
    current_ts = None
    current_skip = False
    current_new = False
    for line in lines:
        m = _CMD_TS_RE.match(line)
        if m:
            current_ts = m.group(1)   # YYYY-MM-DD
            current_new = False
            # Detect [note: ...] suffix and set skip flag if the note matches
            current_skip = False
            if skip_notes:
                nm = _CMD_NOTE_RE.search(line)
                if nm and nm.group(1).strip() in skip_notes:
                    current_skip = True
        elif current_ts:
            if _CMD_AUTH_RE.match(line):
                current_new = True
                continue
            dm = _CMD_DATA_RE.match(line)
            if dm:
                if not current_skip:
                    raw = dm.group(1)
                    if current_new:
                        raw = raw.replace("'\\''", "'")
                    try:
                        entries.append((current_ts, json.loads(raw)))
                    except (json.JSONDecodeError, ValueError):
                        pass
                current_ts = None     # reset whether parse succeeded or not
                current_skip = False
    return entries


def _incremental_info(stage, spec, cfg, workdir):
    """Determine whether and how to run this stage incrementally.

    Returns a dict with at minimum:
      mode:   "incremental" | "full"
      reason: human-readable explanation (always present)

    Incremental mode adds:
      cutoff_date, filter_field, filter_value, prev_stage_dir,
      prev_run_name, max_per_target, id_field, target_field, held_raw_file

    Called only when cfg["repeat_of"] is set AND stage is in _D2_INCREMENTAL.
    Every fallback is named and printed by the caller.
    """
    d2 = _D2_INCREMENTAL[stage]   # caller ensures stage is in _D2_INCREMENTAL
    repeat_of = cfg["repeat_of"]

    # Previous run directory must exist as a sibling of workdir
    prev_dir = Path(workdir).parent / repeat_of
    if not prev_dir.is_dir():
        return {"mode": "full", "reason": f"previous run folder not found: {repeat_of}",
                "prev_run_name": repeat_of}

    # Must be a recognisable pain run
    prev_cfg = _read_run_config(prev_dir)
    if prev_cfg is None or not isinstance(prev_cfg, dict):
        return {"mode": "full", "reason": f"previous run has no valid config: {repeat_of}",
                "prev_run_name": repeat_of}

    # Config must not have changed (excluding bookkeeping-only keys).
    # Read the raw config file (as stored on disk by repeat.py prepare) rather than
    # the in-memory cfg, which C.load_config has expanded with ~15 setdefault keys
    # not present in the saved file.  This matches how repeat.py fingerprint --run
    # reads configs, so the two fingerprints are always comparable.
    _curr_raw_path = Path(workdir) / "PAIN_CONFIG.json"
    if _curr_raw_path.exists():
        try:
            _curr_raw = json.loads(_curr_raw_path.read_text(encoding="utf-8"))
        except Exception:
            _curr_raw = cfg  # unreadable: conservative fallback keeps full-mode safe
    else:
        _curr_raw = cfg  # fallback for callers (unit tests) without a saved config file
    curr_fp = _cfg_fingerprint(_curr_raw)
    prev_fp = _cfg_fingerprint(prev_cfg)
    if curr_fp != prev_fp:
        return {"mode": "full", "reason": "config fingerprint changed since previous run",
                "prev_run_name": repeat_of}

    # Previous stage must have completed
    prev_stage_dir = prev_dir / spec["folder"]
    if not (prev_stage_dir / "run_02_full.json").exists():
        return {"mode": "full",
                "reason": f"previous run's {stage} output (run_02_full.json) missing",
                "prev_run_name": repeat_of}

    # Parse the previous run's commands.sh for a timestamp and the full actor input.
    # Skip entity-refresh entries (THE TRAP fix): the identity check must compare
    # against the real full-run input, not the 1-item refresh call written after
    # a zero-new-review month.  Skipping entries with note="entity-refresh" ensures
    # the last visible entry is always the canonical full (or date-filtered) run.
    entries = _parse_commands_sh(prev_stage_dir / "commands.sh",
                                 skip_notes=frozenset({"entity-refresh"}))
    if not entries:
        return {"mode": "full",
                "reason": "previous run's commands.sh missing or unparseable",
                "prev_run_name": repeat_of}

    # Last entry is the full run (test precedes full in every stage)
    last_ts, prev_full_input = entries[-1]

    # Input identity: strip the date-filter field from both sides before comparing.
    # The previous run may itself have been incremental (injected filter_field),
    # so stripping it lets us compare apples to apples.
    ff = d2["filter_field"]
    prev_cmp = {k: v for k, v in prev_full_input.items() if k != ff}
    curr_full_input, _ = spec["build"](cfg, str(workdir), test=False)
    curr_cmp = {k: v for k, v in curr_full_input.items() if k != ff}
    if prev_cmp != curr_cmp:
        return {"mode": "full",
                "reason": "actor input changed since previous run (targets or caps differ)",
                "prev_run_name": repeat_of}

    # Derive the cutoff date
    try:
        scrape_date = _date_cls.fromisoformat(last_ts)
    except ValueError:
        return {"mode": "full",
                "reason": f"unparseable timestamp in commands.sh: {last_ts!r}",
                "prev_run_name": repeat_of}

    # base_cutoff = scrape_date - 1 day (safety margin for time zones / late-night scrapes)
    base_cutoff = scrape_date - _timedelta(days=1)
    # extra_margin_days: 1 for TP's exclusive boundary, 0 for inclusive boundaries
    filter_date  = base_cutoff - _timedelta(days=d2["extra_margin_days"])
    filter_value = filter_date.isoformat()

    n = int(cfg.get(d2["n_cfg_key"], d2["n_default"]))

    # Count expected distinct targets from the current actor input.
    # Used by the attribution guard in _do_incremental_merge.
    ntk = d2.get("n_targets_input_key")
    n_targets = len(curr_full_input.get(ntk, [])) if ntk else None

    return {
        "mode":          "incremental",
        "reason":        "all incremental conditions met",
        "cutoff_date":   base_cutoff.isoformat(),
        "filter_field":  ff,
        "filter_value":  filter_value,
        "prev_stage_dir": prev_stage_dir,
        "prev_run_name": repeat_of,
        "max_per_target": n,
        "n_targets":     n_targets,
        "id_field":      d2["id_field"],
        "target_field":  d2["target_field"],
        "held_raw_file": d2["held_raw_file"],
        "entity_fields": d2.get("entity_fields", []),
        "identity_field": d2.get("identity_field"),
        "name_field":    d2.get("name_field"),
    }


# --- matching a zero-new target to its start URL -------------------
# In a real run (Slack) the Maps reviews actor named the place in its records
# "...query=Slack&query_place_id=<id>" while the start URL in the settings was
# "...query=Slack%20500%20Howard%20St%20San%20Francisco&query_place_id=<id>".
# The refresh compared the two strings, found no start URL, made no call and
# said nothing. A target is now matched by what names the place, not by how
# its URL is spelled: the place id (query_place_id, q=place_id:, or the
# record's own placeId) for Maps, the bare domain for Trustpilot. A target that
# still cannot be matched is recorded and told to the user, never skipped.

def _maps_place_key(url):
    """What a Google Maps URL names: ("place", <place id>) when it carries one
    (query_place_id=<id>, or q=place_id:<id>); otherwise ("url", <the URL with
    its percent-encoding, case of scheme and host, and parameter order
    normalised>), so two spellings of one URL still compare equal."""
    s = str(url or "").strip()
    try:
        parts = urlsplit(s)
        pairs = parse_qsl(parts.query, keep_blank_values=True)
    except ValueError:
        return ("url", s)
    # A trailing slash ("query_place_id=<id>/") is not part of the id.
    for k, v in pairs:
        if k == "query_place_id" and v.strip().rstrip("/"):
            return ("place", v.strip().rstrip("/"))
    for k, v in pairs:
        if k == "q" and v.strip().lower().startswith("place_id:"):
            pid = v.strip()[len("place_id:"):].strip().rstrip("/")
            if pid:
                return ("place", pid)
    norm = [parts.scheme.lower(), parts.netloc.lower(), unquote(parts.path).rstrip("/"),
            sorted((k, v.strip()) for k, v in pairs)]
    return ("url", json.dumps(norm, ensure_ascii=False))


def _tp_domain_key(value):
    """What a Trustpilot target names: its bare domain, from a domain
    ("acmefitness.com") or a review-page URL (".../review/www.acmefitness.com?x=1"),
    lowercased, without "www.", a query, a fragment or a trailing slash."""
    s = str(value or "").strip().lower().split("#")[0].split("?")[0].rstrip("/")
    # The company is the segment right after /review/, whatever follows it
    # (".../review/company.com/extra" names company.com).
    s = s.split("/review/", 1)[1].split("/")[0] if "/review/" in s else s.split("/")[-1]
    if s.startswith("www."):
        s = s[4:]
    return ("domain", s)


def _refresh_key(stage, value):
    if stage == "B2b":
        return _maps_place_key(value)
    if stage == "TP":
        return _tp_domain_key(value)
    return ("raw", str(value))


_REFRESH_INPUT_KEY = {"B2b": ("startUrls", "maxReviews"),
                      "TP":  ("companyUrls", "maxReviewsPerCompany")}


def _match_refresh_targets(stage, base_input, zero_new_targets, target_ids=None):
    """Match each zero-new target to the start entries of base_input that name
    the same place or company (_refresh_key). target_ids: optional
    {target: the actor's own id of it from the held records} (Maps placeId).

    Returns (matched_entries, unmatched_targets): the matching entries in their
    input order, and the targets no entry names."""
    key_name = _REFRESH_INPUT_KEY.get(stage, (None,))[0]
    entries = ((base_input or {}).get(key_name) or []) if key_name else []

    def _val(e):
        return e.get("url") if isinstance(e, dict) else str(e)

    entry_keys = [_refresh_key(stage, _val(e)) for e in entries]
    matched_idx, unmatched = set(), []
    for t in zero_new_targets:
        keys = {_refresh_key(stage, t)}
        tid = (target_ids or {}).get(t)
        if tid:
            keys.add(("place", str(tid)))
        hits = [i for i, k in enumerate(entry_keys) if k in keys]
        if hits:
            matched_idx.update(hits)
        else:
            unmatched.append(t)
    return [e for i, e in enumerate(entries) if i in matched_idx], unmatched


def _refresh_input_for_zero_targets(stage, base_input, zero_new_targets, target_ids=None):
    """Build a minimal refresh actor input for targets that returned 0 new reviews.

    The full incremental run was date-filtered and returned nothing for these
    targets; their entity fields (totalScore, companyTrustScore, etc.) are
    therefore stale in the held corpus.  This input asks the actor to return
    ONE record per zero-new target without any date filter, purely to harvest
    the current entity-field values (totalScore, companyTrustScore, etc.).

    Per-target cap is 1: we need one record per target to read its entity
    fields; serving more wastes money (the record is not added to the output).
    Capterra-style minimum caps (azzouzana: min 10) do NOT apply to these three
    actors (compass/google-maps-reviews-scraper, automation-lab/trustpilot,
    factden/g2-reviews-scraper), so cap=1 is safe.

    Arguments:
      stage:             "B2b" or "TP" (G2 has no entity fields; returns None)
      base_input:        the full actor input WITHOUT any date-filter field
                         (built by spec["build"](cfg, workdir, test=False))
      zero_new_targets:  list of target field values (URL strings for B2b,
                         domain strings for TP) that got 0 new reviews
      target_ids:        optional {target: the actor's own id} (Maps placeId)

    Returns:
      A modified actor input dict, or None when no refresh is needed or
      supported for this stage, or no start entry names any of the targets
      (_run_zero_target_refresh records and tells those).

    Matching: a record's target value is how the ACTOR spells the
    place or company, which need not be how the settings spell it (the Maps
    actor shortens the free-text query of the URL). _match_refresh_targets
    compares what names the place: the place id for Maps, the bare domain for
    Trustpilot.
    """
    if not zero_new_targets:
        return None
    d2 = _D2_INCREMENTAL.get(stage)
    if not d2 or not d2.get("entity_fields"):
        return None   # G2 has no entity_fields; nothing to refresh
    if stage not in _REFRESH_INPUT_KEY:
        return None
    filtered, _unmatched = _match_refresh_targets(stage, base_input, zero_new_targets,
                                                  target_ids)
    if not filtered:
        return None
    key_name, cap_name = _REFRESH_INPUT_KEY[stage]
    return {**base_input, key_name: filtered, cap_name: 1}


def _do_incremental_merge(stage, new_items, inc_info, stage_dir, _out=None):
    """Merge new actor items with held raw items from the previous run.

    Processing order (FROZEN):
      0. Attribution guard: fall back to full scrape if held records cannot be
         matched to their place/company.
      1. Group both lists by target.
      2. Dedup: remove held items whose id appears in the new batch.
      3. Combine: new items first (newest), then deduped held items (next-newest).
      4. Truncate to N per target (newest-first order is preserved by construction).
      5. Entity field refresh: for each target with ≥1 new review, overwrite entity
         fields on carried records with values from the newest fetched record for
         that target.  Zero-new targets keep previous-run entity values; they are
         handled by a subsequent refresh fetch in _run_one (see _run_zero_target_refresh).
      6. Flat-merge all targets.

    Returns (merged_items, new_only_items).
    Writes incremental.json sidecar to stage_dir.

    _out: optional mutable dict.  When given, populated before return with:
      "zero_new_targets" — list of target values that had held records but 0 new
      "all_held_ids"     — frozenset of all review IDs in the held corpus
      "entity_flds"      — entity field names from inc_info
    Used by _run_one to drive the refresh fetch without re-reading the held file.

    Entity fields (confirmed against real actor schemas):
      B2b  — totalScore/reviewsCount are in every B2b review record (confirmed real
             schema). Refreshed on carried records when ≥1 new review per place.
             Zero-new-reviews case: entity fetch runs after merge (see _run_one).
      TP   — companyTrustScore/companyTotalReviews are in every raw record. Same
             logic as B2b.
    """
    prev_stage_dir = inc_info["prev_stage_dir"]
    held_path = prev_stage_dir / inc_info["held_raw_file"]
    id_field     = inc_info["id_field"]
    target_field = inc_info["target_field"]
    max_n        = inc_info["max_per_target"]
    n_targets    = inc_info.get("n_targets")       # None means "not checkable"
    entity_flds  = inc_info.get("entity_fields", [])

    # Load held raw items
    try:
        raw_held = json.loads(held_path.read_text(encoding="utf-8")) if held_path.exists() else []
    except (json.JSONDecodeError, OSError):
        raw_held = []
    if not isinstance(raw_held, list):
        raw_held = []

    # Group by target
    new_by_t  = defaultdict(list)
    held_by_t = defaultdict(list)
    for item in new_items:
        if isinstance(item, dict):
            new_by_t[item.get(target_field)].append(item)
    for item in raw_held:
        if isinstance(item, dict):
            held_by_t[item.get(target_field)].append(item)

    # ---------------------------------------------------------------------------
    # Attribution guard (FROZEN outcome).
    # If held records cannot be reliably matched to their place/company we fall
    # back to treating the fresh full scrape as the output.  Two conditions:
    #   (a) Any record has an empty/None target field value.
    #   (b) The number of distinct non-None targets in held < n_targets (i.e. two
    #       or more places were collapsed into one by a bug in the world/actor).
    # On fallback: write a sidecar with mode="full_fallback" and return the raw
    # new-batch items unchanged — those ARE the full re-scrape.
    # ---------------------------------------------------------------------------
    held_targets_nonempty = {t for t in held_by_t if t}
    fallback_reason: str = ""
    if None in held_by_t or None in new_by_t:
        fallback_reason = "reviews could not be matched to their place/company (empty target field)"
    elif n_targets is not None and len(held_targets_nonempty) < n_targets:
        fallback_reason = (
            f"reviews could not be matched to their place/company "
            f"(held has {len(held_targets_nonempty)} distinct targets, "
            f"expected {n_targets})"
        )
    if fallback_reason:
        sidecar = {
            "mode":       "full_fallback",
            "prev_run":   inc_info["prev_run_name"],
            "reason":     fallback_reason,
            "counts": {
                "new":                   len(new_items),
                "carried":               0,
                "refetched_in_margin":   0,
                "dup_removed":           0,
                "dropped_by_truncation": 0,
                "total_merged":          len(new_items),
            },
        }
        (Path(stage_dir) / "incremental.json").write_text(
            json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"   incremental fallback: {fallback_reason}")
        return new_items, new_items

    # All held IDs across every target (used to classify truly-new vs.
    # safety-margin re-fetches in the new batch).
    all_held_ids = {
        item.get(id_field)
        for items_list in held_by_t.values()
        for item in items_list
        if isinstance(item, dict) and item.get(id_field)
    }

    # Target ordering: previous run's held order first (preserves the
    # required id sequence — zero-new-review targets must NOT be relegated to
    # the end, which the old "new_by_t keys first" approach did).
    # Targets seen in the new batch but absent from held (brand-new places) are
    # appended after all carried targets.
    _seen_targets: set = set()
    ordered_targets: list = []
    for item in raw_held:
        if isinstance(item, dict):
            t = item.get(target_field)
            if t and t not in _seen_targets:
                ordered_targets.append(t)
                _seen_targets.add(t)
    for t in new_by_t:
        if t not in _seen_targets:
            ordered_targets.append(t)

    merged              = []
    new_ids             = set()   # IDs in new batch NOT in held
    carr_ids            = set()   # IDs carried from held into merged output
    refetched_in_margin = 0       # IDs in new batch that were already in held
    dup_count           = 0
    drop_count          = 0
    zero_new_targets    = []

    for target in ordered_targets:
        new_t  = new_by_t.get(target, [])
        held_t = held_by_t.get(target, [])

        new_id_set = {item.get(id_field) for item in new_t if item.get(id_field)}

        # Classify new-batch items: truly new vs. safety-margin re-fetches.
        # Safety margin items (already in held) are still placed first so newest
        # items lead, but they do NOT count as "new" in run_02_new.json.
        for item in new_t:
            iid = item.get(id_field)
            if iid:
                if iid in all_held_ids:
                    refetched_in_margin += 1
                else:
                    new_ids.add(iid)

        # Dedup: drop held items already present in the new batch
        deduped_held = [item for item in held_t if item.get(id_field) not in new_id_set]
        dup_count += len(held_t) - len(deduped_held)

        # Combine: new items first, then held (newest-first by construction)
        combined  = new_t + deduped_held
        truncated = combined[:max_n]
        drop_count += max(0, len(combined) - max_n)

        merged.extend(truncated)

        for item in truncated:
            iid = item.get(id_field)
            if iid and iid in all_held_ids and iid not in new_id_set:
                carr_ids.add(iid)

        if not new_t and held_t:
            zero_new_targets.append(str(target))

    # run_02_new.json contains only genuinely new items (IDs not in the previous
    # run's held set).  Safety-margin re-fetches are present in merged but NOT
    # in new_only, because they were already counted in the previous run.
    new_only = [item for item in merged if item.get(id_field) in new_ids]

    # ---------------------------------------------------------------------------
    # Entity field refresh (FROZEN outcome).
    # For each target with ≥1 fetched record: overwrite entity fields on carried
    # records with values from the newest fetched record for that target.  "Newest"
    # = first record in new_by_t after sorting by id_field insertion order (actors
    # return newest-first, which the harness preserves).
    # Zero-new targets: keep previous-run values; record in sidecar disclosure.
    # ---------------------------------------------------------------------------
    entity_refresh_count = 0
    if entity_flds:
        for target in ordered_targets:
            new_t = new_by_t.get(target, [])
            if not new_t:
                continue
            # Snapshot entity fields from the newest fetched record
            donor = new_t[0]
            entity_snapshot = {f: donor[f] for f in entity_flds if f in donor}
            if not entity_snapshot:
                continue
            # Apply to carried records in the merged output for this target
            for item in merged:
                if item.get(target_field) != target:
                    continue
                iid = item.get(id_field)
                if iid and iid in carr_ids:
                    item.update(entity_snapshot)
                    entity_refresh_count += 1

    # Populate _out for the caller (used by _run_one for the refresh fetch).
    if _out is not None:
        _out["zero_new_targets"] = zero_new_targets
        _out["all_held_ids"]     = frozenset(all_held_ids)
        _out["entity_flds"]      = entity_flds
        # The actor's own id and name of each held target, from its
        # held records, so the refresh matches a target by what names it.
        _idf, _nmf = inc_info.get("identity_field"), inc_info.get("name_field")
        _out["target_ids"], _out["target_names"] = {}, {}
        for _t, _recs in held_by_t.items():
            for _r in _recs:
                if _idf and _r.get(_idf) and str(_t) not in _out["target_ids"]:
                    _out["target_ids"][str(_t)] = str(_r.get(_idf))
                if _nmf and _r.get(_nmf) and str(_t) not in _out["target_names"]:
                    _out["target_names"][str(_t)] = str(_r.get(_nmf))

    # Write sidecar
    sidecar = {
        "mode":       "incremental",
        "prev_run":   inc_info["prev_run_name"],
        "cutoff_date":  inc_info["cutoff_date"],
        "filter_field": inc_info["filter_field"],
        "filter_value": inc_info["filter_value"],
        "counts": {
            "new":                   len(new_ids),
            "carried":               len(carr_ids),
            "refetched_in_margin":   refetched_in_margin,
            "dup_removed":           dup_count,
            "dropped_by_truncation": drop_count,
            "total_merged":          len(merged),
        },
        "new_ids":    sorted(new_ids),
        "carried_ids": sorted(carr_ids),
        # Disclosed: targets with zero new reviews may have stale entity fields
        # until the refresh fetch in _run_one applies updated values.
        "entity_data_disclosure": (
            f"targets with zero new reviews (their business details are refreshed): "
            f"{zero_new_targets}"
            if zero_new_targets else "all targets have at least one new review"
        ),
        "entity_refresh_count": entity_refresh_count,
    }
    (Path(stage_dir) / "incremental.json").write_text(
        json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8")

    return merged, new_only


def _run_zero_target_refresh(stage, spec, base_input, zero_out, inc_info,
                             stage_dir, budget, args):
    """Fetch one record per zero-new-review target to harvest current entity fields.

    Runs AFTER _do_incremental_merge.  For targets that received 0 new reviews in
    the date-filtered run, the held records carry entity fields from the previous
    run (stale).  This function runs ONE extra actor call — same actor, only the
    zero-new targets, cap=1 per target, NO date filter — and returns entity
    snapshots keyed by target field value.

    Arguments:
      stage         "B2b" or "TP"
      spec          stage spec dict (actor name, build fn, …)
      base_input    full actor input WITHOUT date filter field
      zero_out      dict populated by _do_incremental_merge:
                      zero_new_targets, all_held_ids, entity_flds
      inc_info      _incremental_info result dict
      stage_dir     Path to stage output directory
      budget        Budget instance (check + charge on the refresh run)

    Appends to RUN_LOG.md and commands.sh with note="entity-refresh".
    Returns (entity_snapshot_by_target, ref_cost, ref_items_count) or None
    when no refresh needed.

    Raises RuntimeError if a returned review ID is not in the held corpus
    (means the cutoff logic has a bug — the review slipped past the date filter).
    This condition should be impossible; it is checked to enforce the rule loudly.
    """
    zero_new_targets = zero_out.get("zero_new_targets") or []
    all_held_ids     = zero_out.get("all_held_ids") or frozenset()
    entity_flds      = zero_out.get("entity_flds") or []
    target_ids       = zero_out.get("target_ids") or {}
    target_names     = zero_out.get("target_names") or {}

    if not zero_new_targets or not entity_flds or stage not in _REFRESH_INPUT_KEY:
        return None

    # A target no start entry names is recorded and told, never
    # skipped in silence (it keeps last month's figures).
    _matched, unmatched = _match_refresh_targets(stage, base_input, zero_new_targets,
                                                 target_ids)
    if unmatched:
        print(f"   ⚠ entity-refresh: {len(unmatched)} target(s) could not be matched to "
              f"a start entry in the settings, so their current figures were not fetched: "
              f"{unmatched}")
        _record_incomplete(stage_dir, stage, {
            "kind": "refresh_unmatched", "what": "entity refresh",
            "targets": list(unmatched),
            "names": [target_names.get(str(t)) or str(t) for t in unmatched]})

    refresh_inp = _refresh_input_for_zero_targets(stage, base_input, zero_new_targets,
                                                  target_ids)
    if refresh_inp is None:
        return None
    matched_targets = [t for t in zero_new_targets if t not in unmatched]

    n_zero = len(_matched)
    print(f"   entity-refresh: fetching entity fields for {n_zero} zero-new target(s): "
          f"{matched_targets}")

    # Estimate cost: 1 record per start entry at the stage's per-item rate,
    # plus the stage's start fee (config.estimate, since 8fe5ae3). The spend
    # cap below is this x CHARGE_CEILING_FACTOR, so it covers the start fee
    # plus every record: 1.5 x (1.2 x n x rate + fee) >= fee + n x rate.
    _ref_est_usd, _, _, _ = C.estimate(stage, n_zero)
    budget.check(_ref_est_usd)

    ref_run = _launch_recover(
        spec, refresh_inp, stage_dir, "entity_refresh",
        memory_mb=_mem_for(spec, refresh_inp),
        on_status=lambda s: print(f"     [refresh: {s}]"),
        max_charge_usd=_cap_for(args, budget, max(_ref_est_usd * C.CHARGE_CEILING_FACTOR,
                                                  A.actor_min_charge(spec["actor"]))),
    )
    ref_cost  = A.actual_charge_usd(ref_run)  # Counted before the fetch
    budget.charge(ref_cost)
    ref_items = A.fetch_dataset(ref_run.get("defaultDatasetId"))
    _note_run_incomplete(stage_dir, stage, "entity refresh", ref_run, len(ref_items))
    _clear_pending(stage_dir)

    append_run_log(stage_dir, f"{stage} (entity-refresh)", ref_run,
                   len(ref_items), est_usd=_ref_est_usd, note="entity-refresh")
    append_command(stage_dir, spec["actor"], refresh_inp, ref_run,
                   note="entity-refresh",
                   memory_mb=_mem_for(spec, refresh_inp))

    print(f"   entity-refresh: received {len(ref_items)} records, "
          f"reported ${ref_cost:.4f}")

    # Extract entity snapshots per target; check for cutoff bugs.
    id_field     = inc_info["id_field"]
    target_field = inc_info["target_field"]
    identity_fld = inc_info.get("identity_field")
    # A refresh record is attributed to the zero-new target that names
    # the same place or company (the actor may spell it differently again), and
    # the snapshot is keyed by the target value the held records carry.
    _tkeys = {}
    for zt in matched_targets:
        _tkeys[_refresh_key(stage, zt)] = zt
        if target_ids.get(str(zt)):
            _tkeys[("place", str(target_ids[str(zt)]))] = zt
    entity_snapshot_by_target: dict = {}
    for ref_item in ref_items:
        rid = ref_item.get(id_field)
        t   = ref_item.get(target_field)
        _cands = [_refresh_key(stage, t)]
        if identity_fld and ref_item.get(identity_fld):
            _cands.insert(0, ("place", str(ref_item.get(identity_fld))))
        t = next((_tkeys[k] for k in _cands if k in _tkeys), t)
        # Guard: if a returned ID is not in held, a review slipped past
        # the date cutoff — that is a product bug and must fail loud.
        if rid and rid not in all_held_ids:
            msg = (f"entity-refresh: review {rid!r} for target {t!r} was returned "
                   f"but does not appear in the held corpus.  A review slipped past "
                   f"the cutoff, which means the cutoff logic is wrong.  "
                   f"Manual intervention required: this incremental run is invalid.")
            print(f"   ⚠ {msg}", file=sys.stderr)
            raise RuntimeError(msg)
        if t and t not in entity_snapshot_by_target:
            snapshot = {f: ref_item[f] for f in entity_flds if f in ref_item}
            if snapshot:
                entity_snapshot_by_target[t] = snapshot

    # A matched target that got no current figures is recorded too
    # (when the run itself did not finish, its own entry above already says so).
    missing = [zt for zt in matched_targets if zt not in entity_snapshot_by_target]
    if missing and not ref_run.get("_incomplete"):
        print(f"   ⚠ entity-refresh: no current figures came back for {missing}")
        _record_incomplete(stage_dir, stage, {
            "kind": "refresh_missing", "what": "entity refresh",
            "targets": list(missing),
            "names": [target_names.get(str(t)) or str(t) for t in missing]})

    return entity_snapshot_by_target, ref_cost, len(ref_items)


def _apply_zero_target_refresh(merged_items, entity_snapshot_by_target,
                               target_field, id_field, all_held_ids,
                               stage_dir, ref_cost):
    """Apply entity snapshots from a refresh fetch to carried records in merged.

    Called immediately after _run_zero_target_refresh.  Overwrites entity fields
    on carried records (IDs in all_held_ids) for targets that are in the snapshot
    dict.  Updates incremental.json sidecar with a "refresh_fetch" section.

    Returns count of updated records.
    """
    if not entity_snapshot_by_target:
        return 0
    refresh_count = 0
    for item in merged_items:
        t = item.get(target_field)
        if t not in entity_snapshot_by_target:
            continue
        iid = item.get(id_field)
        if iid and iid in all_held_ids:
            item.update(entity_snapshot_by_target[t])
            refresh_count += 1

    # Update sidecar
    sidecar_path = Path(stage_dir) / "incremental.json"
    try:
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        sidecar = {}
    sidecar["refresh_fetch"] = {
        "targets_refreshed": list(entity_snapshot_by_target.keys()),
        "records_updated":   refresh_count,
        "cost_usd":          ref_cost,
    }
    sidecar_path.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    print(f"   entity-refresh: applied to {refresh_count} carried records; "
          f"business details now match the latest scrape")
    return refresh_count


def _save_dataset(stage_dir, tag, items):
    out = Path(stage_dir) / f"run_{tag}.json"
    out.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    if tag == "02_full":
        _STEP_SAVED.append(str(stage_dir))
    return out


def _stage_done(stage_dir, spec):
    """A stage is 'done' once its full output exists — lets a re-run resume
    without re-spending on completed stages."""
    return (stage_dir / "run_02_full.json").exists()


# --- run recovery -----------------------------------------------------------
# A run is CHARGED the instant it launches, but its output only lands on disk
# after a successful fetch. A transient client drop in between loses the run
# handle, _stage_done stays False, and the next call RE-LAUNCHES -- paying twice.
# We persist {runId, datasetId, input_hash, subkey} the instant a run launches
# (via run_actor's on_start), and on re-entry reattach to that live/finished run
# instead of launching again. Apify keeps runs and datasets indefinitely, so the
# result is still there to fetch. The handle is cleared once its output is safely
# consumed. `subkey` ('test' / 'full' / 'batchN') separates runs within a stage;
# only the current in-flight run is tracked (runs are sequential).

def _pending_path(stage_dir):
    return Path(stage_dir) / ".pending_run.json"


def _input_hash(actor_input):
    blob = json.dumps(actor_input, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _write_pending(stage_dir, data):
    _pending_path(stage_dir).write_text(json.dumps(data), encoding="utf-8")


def _read_pending(stage_dir):
    p = _pending_path(stage_dir)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _clear_pending(stage_dir):
    try:
        _pending_path(stage_dir).unlink()
    except OSError:
        pass


# The subkeys of the scraper runs the current step launched or picked up,
# in order. main() empties it before each step; _note_budget_stop reads it.
_STEP_RUNS = []

# Set when this call saved the current step's full result
# (_save_dataset). main() empties it before each step and passes it to
# _note_step_stopped, so a step that stops AFTER saving is recorded as having
# written its result in this call, not left to be guessed from the file.
_STEP_SAVED = []


def _launch_recover(spec, actor_input, stage_dir, subkey, **run_kw):
    """Launch an actor run, but first try to recover a run already launched for
    this exact input (a transient drop must never re-charge). Records the run
    handle at launch (on_start) so a mid-run or mid-fetch drop resumes by id."""
    _STEP_RUNS.append(subkey)
    ih = _input_hash(actor_input)
    pend = _read_pending(stage_dir)
    if pend and pend.get("subkey") == subkey and pend.get("input_hash") == ih:
        rec = A.reattach(pend.get("runId"), on_status=run_kw.get("on_status"))
        if rec is not None:
            print(f"   ↻ recovered run {pend.get('runId')} for {subkey} (no re-charge)")
            return rec
        _clear_pending(stage_dir)  # unrecoverable -> fall through and re-launch
    actor = spec["actor"]

    def _on_start(rid, dsid, _sd=stage_dir, _ih=ih, _sk=subkey, _a=actor):
        _write_pending(_sd, {"runId": rid, "datasetId": dsid, "input_hash": _ih,
                             "subkey": _sk, "actor": _a})
    return A.run_actor(actor, actor_input, on_start=_on_start, **run_kw)


# --- a run that says it did not work ---------------------------------------
# A run can end SUCCEEDED with 0 items because the actor itself did no work. Once,
# scrapesage/linkedin-ad-library-scraper was put in maintenance mode by
# its developer; the run ended SUCCEEDED, exit 0, 0 items, and said so only in its
# statusMessage. The tool then called it "a genuine zero" for HubSpot, which was
# false. The run object already fetched carries that message, so reading it needs
# no new call. The notice is written next to the stage's output, and the entry
# script turns it into a gap for the stage (it knows the source and competitor).
# The word list is adjustable; it only matters for a run that returned 0 items.
_NOTICE_WORDS = ("maintenance", "disabled", "blocked", "deprecated", "suspended",
                 "not returning data", "temporarily unavailable")


def _run_notice(run, n_items):
    """The run's own statusMessage when the run returned 0 items and the message
    says the actor did not work (maintenance, disabled, blocked ...); else None."""
    if n_items:
        return None
    msg = str((run or {}).get("statusMessage") or "").strip()
    low = msg.lower()
    return msg if msg and any(w in low for w in _NOTICE_WORDS) else None


def _write_notice(stage_dir, notices, charged_usd):
    """Write <stage>/.run_notice.json when a run of this stage said it did not
    work; remove a stale one otherwise (a --force re-run that worked)."""
    p = Path(stage_dir) / ".run_notice.json"
    if not notices:
        try:
            p.unlink()
        except OSError:
            pass
        return
    p.write_text(json.dumps({"status_message": notices[0],
                             "charged_usd": round(float(charged_usd or 0.0), 6)},
                            ensure_ascii=False), encoding="utf-8")
    print(f"   \u26a0 the scraper reported it did not work: {notices[0]}")


# --- a run that did not finish its work ---------------------------
# Once, a Trustpilot run of a scheduled repeat ended ABORTED, the
# client printed its "RUN INCOMPLETE" block, and the run's notification still
# said "Run complete" with status done: the block went to the terminal and
# nowhere else. Every stage now writes <stage>/.run_incomplete.json from the
# judgment the block printed (apify_client attaches it to the run as
# run["_incomplete"]), and from any refresh it needed but could not make; the
# entry scripts build a scheduled run's notification from these files. A test
# run is not recorded: its output is not kept, the full run follows it.
_INCOMPLETE_NAME = ".run_incomplete.json"


# The record of a step its settings asked for 0 results (see _run_one).
# It is not a record of a step that did not finish: the entry script reads it to
# name the step in the closing lines, never as an empty result.
_NOT_RUN_BY_SETTING_NAME = ".not_run_by_setting.json"


def _note_not_run_by_setting(stage_dir, stage):
    """Record that this step was not run because its settings ask for 0 results."""
    try:
        (Path(stage_dir) / _NOT_RUN_BY_SETTING_NAME).write_text(
            json.dumps({"stage": stage, "reason": "zero_count"}), encoding="utf-8")
    except OSError:
        pass


def _clear_not_run_by_setting(stage_dir):
    """Remove that record when the step does run (its settings changed)."""
    try:
        (Path(stage_dir) / _NOT_RUN_BY_SETTING_NAME).unlink()
    except OSError:
        pass


def _clear_incomplete(stage_dir):
    """Remove a stage's record from an earlier attempt (a --force re-run)."""
    try:
        (Path(stage_dir) / _INCOMPLETE_NAME).unlink()
    except OSError:
        pass


def _record_incomplete(stage_dir, stage, entry):
    """Add one entry to <stage>/.run_incomplete.json (created on first use)."""
    p = Path(stage_dir) / _INCOMPLETE_NAME
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    doc["stage"] = stage
    doc.setdefault("entries", []).append(entry)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def _note_run_incomplete(stage_dir, stage, what, run, n_items):
    """Record a run whose RUN INCOMPLETE block was printed (run["_incomplete"]).
    what: which run of the stage it was, in plain words ("full run", ...)."""
    info = (run or {}).get("_incomplete")
    if not info:
        return
    _record_incomplete(stage_dir, stage, {"kind": "run_ended", "what": what,
                                          "records": int(n_items or 0), **info})


def _note_step_stopped(stage_dir, stage, kind, cap_usd=None, spent_usd=None,
                       part_way=False, rerun_reuses_run=False,
                       result_saved=False):
    """Record that this step stopped before it finished.
    kind "step_failed": it raised an error; "budget_stop": the run reached the
    budget cap at this step. cap_usd: the cap it was stopped at, so a
    reader can name the figure without working it out again. spent_usd and
    part_way (see _note_budget_stop): the step had already run part of
    its work and been charged for it when the cap stopped it; the record then says
    "part_way", with what budget.charge() counted for the step. rerun_reuses_run:
    a re-run of the folder picks up the scraper run this step left
    instead of launching it again. Without them the record is exactly what it was before.

    WHY: a step that raised printed one warning line, the run went on, and its
    closing lines then read the empty result as "it ran, but nothing came back",
    so the user believed the step had run. The entry script names every recorded
    step where the user reads how the run went (the same record the RUN INCOMPLETE line uses,
    read by _incomplete_lines). A record of the same kind from an
    earlier attempt is replaced, so a step that fails twice is named once; a
    re-run that gets as far as the step's test run clears it (_clear_incomplete). A newer stop
    also replaces the record _note_kept_after_failed_attempt made."""
    p = Path(stage_dir) / _INCOMPLETE_NAME
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    if not isinstance(doc, dict) or not isinstance(doc.get("entries"), list):
        doc = {}
    entries = [e for e in doc.get("entries", [])
               if not (isinstance(e, dict)
                       and e.get("kind") in (kind, "kept_after_failed_attempt"))]
    entry = {"kind": kind}
    if cap_usd is not None:
        entry["cap_usd"] = round(float(cap_usd), 4)
    if float(spent_usd or 0.0) > 0 or part_way:
        entry["part_way"] = True
        entry["spent_usd"] = round(float(spent_usd or 0.0), 6)
        if rerun_reuses_run:
            entry["rerun_reuses_run"] = True
    if result_saved:
        # This call saved the step's result before it stopped, so
        # the result in the folder is this call's, not an earlier run's.
        entry["result_saved"] = True
    doc["stage"] = stage
    doc["entries"] = entries + [entry]
    try:
        Path(stage_dir).mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _note_kept_after_failed_attempt(stage_dir):
    """A run that SKIPS a finished step re-labels the step's stop record.

    WHY: <stage>/.run_incomplete.json is written by the run that failed or was stopped,
    and every later run of the folder read it as its own: a plain re-run that skipped
    the step (so made no attempt at it) was told "this run did not finish" and, after
    a crash that came after the save, that "this run" saved the result. The skipping
    run knows what happened, so it says so in the record, once: kind
    "kept_after_failed_attempt", "was" holding the old kind and every other field kept
    (result_saved, part_way, spent_usd, cap_usd). Readers word that kind as a finished
    result kept from an earlier fetch whose latest attempt to fetch again failed. The
    fact is kept, not cleared. A run that fails the step again replaces it
    (_note_step_stopped)."""
    p = Path(stage_dir) / _INCOMPLETE_NAME
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        stops = [e for e in doc["entries"]
                 if isinstance(e, dict) and e.get("kind") in ("step_failed", "budget_stop")]
        for e in stops:
            e["was"], e["kind"] = e["kind"], "kept_after_failed_attempt"
        if stops:
            p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError):
        pass


def _note_budget_stop(workdir, order, stage, cap_usd, step_spent_usd, step_runs=None,
                      force=False):
    """Record a budget stop for `stage` and every later step of `order`.

    WHY: budget.check() and budget.enforce() can raise INSIDE _run_one after this
    step had already run part of its work and been charged for it: after its test
    pass, when the full estimate recalibrated from the test's actual rate goes over
    the cap; between the batches of a batched step; and (pain) before an entity
    refresh, after the full run had already run. The record said only
    "budget_stop", so the closing lines told the user the step "did not run".
    step_spent_usd: what budget.charge() counted for this step in this call
    (budget.spent now, minus budget.spent before the step started). Every run is counted the
    moment it ends, so this is every run the step was
    billed for, the full run before a refresh included.
    step_runs: the subkeys of the runs the step launched or picked up in
    this call (_STEP_RUNS). Any run makes the record part-way, a $0 one too. When
    the step's .pending_run.json still names a run (its output was not saved: the
    refresh case), a re-run of the folder picks it up without a new charge only if
    it is the first run the step launches again (_launch_recover matches subkey
    and input); otherwise an earlier run of the re-run (its test pass) replaces the
    handle and the run is launched and billed again. The later steps never
    started: they keep the plain record, exactly as before."""
    step_runs = list(step_runs or [])
    for left in order[order.index(stage):]:
        stage_dir = Path(workdir) / STAGES[left]["folder"]
        if _stage_done(stage_dir, STAGES[left]) and not force:
            continue  # Finished and not run again, so not stopped
        if left != stage:
            _note_step_stopped(stage_dir, left, "budget_stop", cap_usd=cap_usd)
            continue
        pending_now = _read_pending(stage_dir) or {}
        _note_step_stopped(stage_dir, left, "budget_stop", cap_usd=cap_usd,
                           spent_usd=max(0.0, float(step_spent_usd or 0.0)),
                           part_way=bool(step_runs),
                           rerun_reuses_run=bool(step_runs)
                           and pending_now.get("subkey") == step_runs[0])


def _run_check_incremental(stage, spec, cfg, workdir):
    """Return (_repeat_of, _inc) for the incremental path (FROZEN).

    Called at the top of _run_one so both --dry-run and the full path see the
    injected filter field.  Pain skill only: _D2_INCREMENTAL maps stage ->
    filter spec; ad/search stubs return (None, None).
    """
    _repeat_of = cfg.get("repeat_of") if isinstance(cfg, dict) else None
    _inc = None
    if _repeat_of and stage in _D2_INCREMENTAL:
        _inc = _incremental_info(stage, spec, cfg, workdir)
        if _inc["mode"] == "incremental":
            print(f"   incremental: cutoff {_inc['cutoff_date']}, "
                  f"{_inc['filter_field']}={_inc['filter_value']}")
        else:
            print(f"   incremental: full scrape ({_inc['reason']})")
    return _repeat_of, _inc


def _run_after_fetch_hook(stage, spec, items, _inc, _repeat_of,
                          base_input, stage_dir, budget, args):
    """Post-fetch hook: incremental merge, zero-target refresh, fallback sidecar.

    FROZEN: merge must happen on the raw fetch, before save and before
    postprocess.  Returns (final_items, new_only_or_None).  new_only is None
    when no incremental merge was performed (full-scrape or non-incremental
    stage); the caller saves 02_new only when new_only is not None.

    Pain skill only: ad/search stubs return (items, None) unchanged.
    """
    if _inc and _inc["mode"] == "incremental":
        _zero_out: dict = {}
        items, new_only = _do_incremental_merge(stage, items, _inc, stage_dir, _zero_out)
        # Refresh fetch for zero-new targets (entity field parity, see
        # _run_zero_target_refresh for full rationale and spend accounting).
        _ref_result = _run_zero_target_refresh(
            stage, spec, base_input, _zero_out, _inc, stage_dir, budget, args)
        if _ref_result is not None:
            _r_snaps, _r_cost, _ = _ref_result
            _apply_zero_target_refresh(
                items, _r_snaps,
                _inc["target_field"], _inc["id_field"],
                _zero_out.get("all_held_ids") or frozenset(),
                stage_dir, _r_cost)
        return items, new_only
    if _inc and _repeat_of:
        (stage_dir / "incremental.json").write_text(
            json.dumps({"mode": "full", "reason": _inc["reason"],
                        "prev_run": _repeat_of}, indent=2, ensure_ascii=False),
            encoding="utf-8")
    return items, None


def _run_after_save_hook(stage, spec, items, base_input, stage_dir, budget, args, cfg, workdir):
    """No-op stub: runs after the step's full result is saved and built. Only the
    search tool uses it (a second request for items that came back empty); this stub keeps
    _run_one AST-identical across all three skill copies."""
    return None


def _run_one(stage, spec, cfg, workdir, budget, args):
    stage_dir = Path(workdir) / spec["folder"]
    stage_dir.mkdir(parents=True, exist_ok=True)

    # Resume-safety: skip a stage that already completed (unless --force).
    if not args.dry_run and _stage_done(stage_dir, spec) and not args.force:
        print(f"\n▶ {spec.get('name', stage)} (step {stage}): already complete, "
              f"skipping (use --force to re-run).")
        # A step whose latest attempt failed keeps its result, and
        # the files built from it may have been removed; build them again, free.
        P.rebuild_dropped_outputs(stage, stage_dir, workdir, cfg)
        _note_kept_after_failed_attempt(stage_dir)
        return

    # --- incremental check (FROZEN) ----------------------------------
    # Compute early so --dry-run can show the injected filter field.
    _repeat_of, _inc = _run_check_incremental(stage, spec, cfg, workdir)

    _est_input, est_count = spec["build"](cfg, str(workdir), test=False)
    # One start charge per actor run the full input launches (config.actor_runs): a batched stage
    # such as Google ads (B5) pays one per batch.
    est_usd, unit, rate, unverified = C.estimate(stage, est_count,
                                                 runs=C.actor_runs(spec, _est_input))
    # The step's plain name, and its code for --only.
    print(f"\n▶ {spec.get('name', stage)} (step {stage})")
    print(f"   full-run forecast: ~{est_count} {unit}s → ~${est_usd:.2f}"
          + ("  [rate UNVERIFIED]" if unverified else ""))
    # A step whose full run counts 0 records is not
    # run. config.estimate prices a count of 0 at $0 ("a stage with nothing to do
    # launches no run"), and a $0 forecast gave the call no spending limit: with
    # google_max_items 0 the Google step sent maxItems 0 with no limit and billed
    # $0.10 under a $0.10 approval that Meta had already used half of. A 0 in a
    # count setting (accepted) means "none": nothing is launched,
    # nothing is spent, and the entry script names the step in its closing lines.
    # A 0 can also empty the full input itself. Search
    # builds its full input by slicing (queries[:serp_top_n], terms[:keyword_cap]),
    # so with a 0 the full input has no entries while the test input (the first 5)
    # still has some. The step then paid for its test run and the full call met
    # run_actor's "no spending limit" refusal: a false error, every month. So the
    # test input is asked too, before any call. Both are empty only when there is
    # nothing upstream to work on, which stays the "no inputs yet" skip below.
    _test_built = None
    if est_count <= 0 and not _has_inputs(stage, _est_input):
        _test_built = spec["build"](cfg, str(workdir), test=True)
    if est_count <= 0 and (_has_inputs(stage, _est_input)
                           or _has_inputs(stage, _test_built[0])):
        print("   your settings ask this step for 0 results, so it is not run and "
              "nothing is spent.")
        if not args.dry_run:
            # A record from an earlier attempt (the
            # step was stopped at the budget cap, or failed) is not true of this
            # run: the step was not run because of the setting, and only that is
            # said. Before, the closing lines gave both reasons.
            _clear_incomplete(stage_dir)
            _note_not_run_by_setting(stage_dir, stage)
            # The files an earlier run of this step left are not this
            # run's; this step is not run, so none were written for it now.
            P.drop_stage_outputs(stage, stage_dir, workdir)
        return
    if not args.dry_run:
        _clear_not_run_by_setting(stage_dir)

    # Dry-run clarity: when the actor's own minimum cap exceeds our forecast x
    # factor, the spend CAP is raised to that minimum so Apify accepts the run.
    # That cap is a ceiling, not the expected cost -- show it so the user does not
    # read the floor as the price.
    _fc_cap = est_usd * C.CHARGE_CEILING_FACTOR
    _fc_floor = A.actor_min_charge(spec["actor"])
    if _fc_floor > _fc_cap:
        print(f"   spend cap: ${max(_fc_floor, 0.01):.2f}, {spec['actor']}'s "
              f"minimum allowed cap (a ceiling, not the expected cost; the run "
              f"still bills ~the forecast)")

    if args.dry_run:
        di, _ = spec["build"](cfg, str(workdir), test=False)
        if _inc and _inc["mode"] == "incremental":
            di = {**di, _inc["filter_field"]: _inc["filter_value"]}
        blob = json.dumps(di, ensure_ascii=False)
        print(f"   input: {blob if len(blob) <= 600 else blob[:600] + ' …'}")
        return

    # --- TEST run ---
    _clear_incomplete(stage_dir)
    test_input, test_count = (_test_built if _test_built is not None
                              else spec["build"](cfg, str(workdir), test=True))
    if not _has_inputs(stage, test_input):
        print(f"   ⤫ no inputs for {spec.get('name', stage)} yet (missing upstream "
              f"output?), skipping.")
        # As above; the step did not run, so its earlier files are not
        # this run's.
        P.drop_stage_outputs(stage, stage_dir, workdir)
        return
    full_preview, _ = spec["build"](cfg, str(workdir), test=False)
    # When the full input is already no larger than a test pass, the test and the
    # full run send the SAME items — running both bills the actor twice for one
    # output, and for per-actor-start pricing that doubles the bill. Skip the
    # test in the normal path; the full-estimate budget gate below still guards
    # the cap, and the run is test-sized so there is nothing to recalibrate.
    # --test-only keeps running its one small pass. _input_size returns None on
    # an unrecognised shape, so we never skip the test when we cannot size it.
    _full_sz = _input_size(spec, full_preview)
    _test_sz = _input_size(spec, test_input)
    skip_test = (not args.test_only) and _full_sz is not None \
        and _test_sz is not None and _full_sz <= _test_sz

    if not skip_test:
        budget.check(est_usd)  # gate on the FULL estimate before spending a cent on the test
        # Spend cap even on the TEST run. A test builds only 3-5 items, but an actor
        # that ignores its own input cap (the live Capterra 3,058-vs-30 case) can
        # burn real money on a "test". Ceiling = the test's own forecast x factor.
        test_ceiling = C.estimate(stage, test_count)[0] * C.CHARGE_CEILING_FACTOR
        print("   running TEST …")
        run = _launch_recover(spec, test_input, stage_dir, "test",
                              memory_mb=_mem_for(spec, test_input),
                              on_status=lambda s: print(f"     [{s}]"),
                              max_charge_usd=_cap_for(args, budget, test_ceiling))
        # Every run is counted the moment its cost is known (the run
        # object that ended), before anything that can stop the step: fetching
        # its dataset can fail, and a later budget check can raise. Counted after
        # them, a billed run was left out of the budget and the run's bill.
        test_cost = A.actual_charge_usd(run)
        budget.charge(test_cost)
        items = A.fetch_dataset(run.get("defaultDatasetId"))
        _clear_pending(stage_dir)  # test output consumed -> nothing to recover
        append_run_log(stage_dir, f"{stage} (test)", run, len(items), est_usd=est_usd, note="test run")
        append_command(stage_dir, spec["actor"], test_input, run,
                       memory_mb=_mem_for(spec, test_input))
        n_test = len(items)
        _notices = [m for m in [_run_notice(run, n_test)] if m]
        _notice_usd = test_cost
        print(f"   test: {n_test} records, reported ${test_cost:.3f} "
              f"(budget spent ${budget.spent:.2f}/{cap_text(budget.cap)})")

        if args.test_only:
            _save_dataset(stage_dir, "01_test", items) if items else None
            return

        # Recompute the full estimate from the ACTUAL test rate.
        if n_test and test_cost:
            actual_rate = test_cost / n_test
            est_usd = est_count * actual_rate * C.SAFETY_MARGIN
            print(f"   recalibrated full estimate from test: ~${est_usd:.2f} "
                  f"(${actual_rate:.5f}/{unit})")
    else:
        print(f"   full input is test-sized ({_full_sz} ≤ test {_test_sz}) "
              f"so the redundant test pass is skipped (it would bill the same run twice).")
    budget.check(est_usd)

    # --- FULL run ---
    if skip_test:
        _notices, _notice_usd = [], 0.0
    # Save base_input (no date filter) so the refresh fetch for zero-new targets
    # can use it without the date filter that was injected for the incremental run.
    base_input, _ = spec["build"](cfg, str(workdir), test=False)
    full_input = base_input
    # Inject date filter for incremental mode (cutoff ensures all new reviews pass)
    if _inc and _inc["mode"] == "incremental":
        full_input = {**base_input, _inc["filter_field"]: _inc["filter_value"]}
        print(f"   incremental: fetching only reviews since {_inc['filter_value']} "
              f"(forecast is the cap maximum)")
    print("   running FULL …")
    batch = spec.get("batch")
    arr = full_input.get(batch["key"]) if batch else None
    if batch and isinstance(arr, list) and len(arr) > batch["size"]:
        # This actor caps the input array per run. Split into chunks, run each,
        # concatenate the datasets into one run_02_full.json.
        size = batch["size"]
        chunks = [arr[i:i + size] for i in range(0, len(arr), size)]
        print(f"   batching {len(arr)} {batch['key']} → {len(chunks)} runs of ≤{size} …")
        items, full_cost = [], 0.0
        for bi, chunk in enumerate(chunks, 1):
            binput = {**full_input, batch["key"]: chunk}
            # this batch's slice of the full spend ceiling, proportional to its
            # share of the work array, so the cap tracks the batch it guards.
            # A stage whose batch spec says "per_run" counts its ceiling per actor
            # run (Google ads: maxItems is a total per run, so the expected count
            # is maxItems x runs). Every run may return a full maxItems, a short
            # last batch included, so each batch gets an equal share: one run's
            # worth. A proportional share gave a 2-URL last batch too little and
            # Apify would stop it over budget before it reached maxItems.
            bshare = (1 / len(chunks)) if batch.get("per_run") else (len(chunk) / len(arr))
            bceiling = est_usd * bshare * C.CHARGE_CEILING_FACTOR
            run = _launch_recover(spec, binput, stage_dir, f"batch{bi}",
                                  memory_mb=_mem_for(spec, binput),
                                  on_status=lambda s, bi=bi: print(f"     [batch {bi}/{len(chunks)}: {s}]"),
                                  max_charge_usd=_cap_for(args, budget, bceiling))
            bcost = A.actual_charge_usd(run)  # Counted before the fetch
            budget.charge(bcost)
            full_cost += bcost
            _notice_usd += bcost
            bitems = A.fetch_dataset(run.get("defaultDatasetId"))
            _note_run_incomplete(stage_dir, stage, f"batch {bi} of {len(chunks)}",
                                 run, len(bitems))
            items.extend(bitems)
            _notices += [m for m in [_run_notice(run, len(bitems))] if m]
            _clear_pending(stage_dir)  # this batch's output consumed
            # Logged before the cap is enforced, so the run log and
            # commands.sh list the batch that crossed the cap too (it was billed).
            append_run_log(stage_dir, f"{stage} (full batch {bi}/{len(chunks)})", run, len(bitems), est_usd=est_usd)
            append_command(stage_dir, spec["actor"], binput, run,
                           memory_mb=_mem_for(spec, binput))
            # Enforce the cap after each batch's charge so an over-budget
            # run aborts before launching the NEXT batch. Safe boundary: the next
            # batch has not yet started; no half-processed data on disk.
            budget.enforce()
        _only_err = P.only_error_items(items)  # As for a single run, below
        if _only_err:
            raise RuntimeError(_only_err)
        # After-fetch hook (FROZEN: merge raw BEFORE save, BEFORE postprocess)
        items, new_only = _run_after_fetch_hook(
            stage, spec, items, _inc, _repeat_of, base_input, stage_dir, budget, args)
        if new_only is not None:
            _save_dataset(stage_dir, "02_new", new_only)
            print(f"   incremental: {len(new_only)} new, {len(items)} total after merge+dedup+truncation")
        _save_dataset(stage_dir, "02_full", items)
        _write_notice(stage_dir, _notices, _notice_usd)
        print(f"   full: {len(items)} records saved across {len(chunks)} batches")
        print(f"   reported ${full_cost:.3f}  ·  spent ${budget.spent:.2f}/{cap_text(budget.cap)}")
    else:
        run = _launch_recover(spec, full_input, stage_dir, "full",
                              memory_mb=_mem_for(spec, full_input),
                              on_status=lambda s: print(f"     [{s}]"),
                              max_charge_usd=_cap_for(args, budget, est_usd * C.CHARGE_CEILING_FACTOR))
        # Counted before the fetch and the after-fetch hook. The hook's
        # entity refresh checks the budget and can stop the step; counted after
        # it, the billed full run was missing from the budget that check compared
        # against and from the run's bill.
        full_cost = A.actual_charge_usd(run)
        budget.charge(full_cost)
        items = A.fetch_dataset(run.get("defaultDatasetId"))
        _only_err = P.only_error_items(items)
        if _only_err:
            # The run was billed and is listed, but it brought no
            # result: nothing is saved over an earlier result, and the step fails.
            _clear_pending(stage_dir)
            append_run_log(stage_dir, f"{stage} (full)", run, len(items), est_usd=est_usd,
                           note="only an error came back")
            append_command(stage_dir, spec["actor"], full_input, run,
                           memory_mb=_mem_for(spec, full_input))
            raise RuntimeError(_only_err)
        _note_run_incomplete(stage_dir, stage, "full run", run, len(items))
        _notices += [m for m in [_run_notice(run, len(items))] if m]
        # After-fetch hook (FROZEN: merge raw BEFORE save, BEFORE postprocess)
        try:
            items, new_only = _run_after_fetch_hook(
                stage, spec, items, _inc, _repeat_of, base_input, stage_dir, budget, args)
        except Exception:
            # The step stops here with the full run billed and counted
            # but its output not saved; the run log and commands.sh still list it.
            append_run_log(stage_dir, f"{stage} (full)", run, len(items), est_usd=est_usd)
            append_command(stage_dir, spec["actor"], full_input, run,
                           memory_mb=_mem_for(spec, full_input))
            raise
        if new_only is not None:
            _save_dataset(stage_dir, "02_new", new_only)
            print(f"   incremental: {len(new_only)} new, {len(items)} total after merge+dedup+truncation")
        _save_dataset(stage_dir, "02_full", items)
        _write_notice(stage_dir, _notices, _notice_usd + full_cost)
        _clear_pending(stage_dir)  # full output saved -> nothing to recover
        append_run_log(stage_dir, f"{stage} (full)", run, len(items), est_usd=est_usd)
        append_command(stage_dir, spec["actor"], full_input, run,
                       memory_mb=_mem_for(spec, full_input))
        print(f"   full: {len(items)} records saved")
        print(f"   reported ${full_cost:.3f}  ·  spent ${budget.spent:.2f}/{cap_text(budget.cap)}")

    # Derived artifacts for downstream stages (B1 -> competitor domains).
    P.postprocess(stage, stage_dir, workdir, cfg)
    # After the result is saved AND built, so a kill in the hook leaves a built step.
    _run_after_save_hook(stage, spec, items, base_input, stage_dir, budget, args, cfg, workdir)


def _real_entries(lst):
    """Filter a list to entries that are 'real' inputs.

    A bare bool(["", "  ", None]) is True, so a simple bool(v) guard passes
    lists of blank/None values and launches a paid actor run on garbage input.
    This helper keeps only entries that are substantive:
      - strings: non-empty after strip()
      - non-strings (dicts like {"url": "..."}, numbers, etc.): truthy value
    None and whitespace-only strings are discarded.
    """
    result = []
    for item in lst:
        if isinstance(item, str):
            if item.strip():
                result.append(item)
        elif item:   # truthy non-string: dict, int, etc.
            result.append(item)
    return result


def _has_inputs(stage, actor_input):
    """Cheap guard: don't launch a stage whose input arrays are empty.
    B2a keys its search on `searchStringsArray`; B2b on `startUrls`;
    TP (Trustpilot) on `companyUrls`; apps on `appIds`.

    RD (Reddit) is special: it has SEVERAL heterogeneous target arrays
    (searchTerms / subredditUrls / startUrls) and is runnable if ANY one is
    non-empty. The generic loop below returns on the first key it sees, which
    cannot express that "any of" semantics -- so Reddit gets its own branch.

    Uses _real_entries() so that lists of blank/None values are
    treated as absent rather than present — preventing garbage input from
    passing the gate and spending money on an empty actor run."""
    if stage == "RD":
        return any(bool(_real_entries(actor_input.get(k) or []))
                   for k in ("searchTerms", "subredditUrls", "startUrls"))
    for key in ("searchStringsArray", "startUrls", "queries", "urls",
                "companyUrls", "appIds", "profileUrls"):
        v = actor_input.get(key)
        if v is not None:
            return bool(_real_entries(v))
    return True


def _input_size(spec, inp):
    """Count of items in the actor's primary work array — the unit a run is
    billed on. Used to detect when a 'full' run is already no larger than a test
    pass, so the test can be skipped instead of billing the same run twice.

    Returns None (meaning "do not skip the test") whenever the primary array is
    ambiguous: an unrecognised shape, or several known target arrays present at
    once (e.g. Reddit's searchTerms / subredditUrls / startUrls, where the
    billed unit is not one single array). Skipping on an input we cannot size
    would drop the test (and its rate recalibration) from large runs too. A
    spec-declared batch key is authoritative and always wins. The key list is
    the union across all supported stages so the shared engine stays correct
    everywhere."""
    if not isinstance(inp, dict):
        return None
    batch = spec.get("batch")
    if batch and isinstance(inp.get(batch["key"]), list):
        return len(inp[batch["key"]])
    keys = ("queries", "keywords", "startUrls", "urls", "companyUrls", "appIds",
            "subredditUrls", "searchTerms", "searchStringsArray", "profileUrls")
    present = [k for k in keys if inp.get(k)]
    if len(present) != 1:
        return None
    v = inp[present[0]]
    if isinstance(v, list):
        return len(v)
    if isinstance(v, str):
        return len([ln for ln in v.splitlines() if ln.strip()])
    return None


def _mem_for(spec, actor_input):
    """Memory (MB) for one actor run.

    Some actors require AT LEAST one input URL per 512 MB of RAM — the Meta ads
    scraper (B4) rejects a single-URL run at the default 1024 MB with
    "You must provide atleast 1 input URL per 512MB of RAM memory." A single
    Meta term in a single country is one URL, a completely normal request, so
    memory must scale to the URL count. Specs that declare `mem_per_url` get
    memory capped at mem_per_url × (input URL count); others use memory_mb.
    """
    base = spec.get("memory_mb", 4096)
    per = spec.get("mem_per_url")
    if not per:
        return base
    n = 0
    for key in ("urls", "startUrls"):
        v = actor_input.get(key)
        if isinstance(v, list):
            n = max(n, len(v))
    return min(base, per * max(1, n))


def _cap_for(args, budget, est_cap):
    """Per-call maxTotalChargeUsd: est_cap, further bounded by the run ceiling.

    WHY: under --standing-approval the user approved a total-run ceiling; that
    ceiling must be enforced even if a single actor's estimated cap (est_usd *
    CHARGE_CEILING_FACTOR) would exceed what remains of the budget.  Without
    --run-ceiling-usd this function is transparent: returns est_cap unchanged.
    """
    ceiling = getattr(args, "run_ceiling_usd", None)
    if ceiling is None:
        return est_cap
    remaining = max(0.01, ceiling - budget.spent)
    return min(est_cap, remaining)


def _drop_unrun_outputs(steps, workdir, args):
    """Remove the files an earlier run left for each of `steps`, which this call did not
    finish: it failed, or the budget cap or a failed start stopped it.

    WHY: a step that does not get a result writes none of its own files, so the ones an
    earlier run of the same folder left stayed and the report, the app and the month
    comparison showed them as this run's ("37 unchanged" for a step that never ran). A
    step that this call would not have run again (it is done and --force was not given)
    keeps its files: they are this folder's result. A dry run changes nothing. The step's
    raw run files are its result as it stands and are never touched
    (postprocess.drop_stage_outputs)."""
    if args.dry_run:
        return
    for st in steps:
        stage_dir = Path(workdir) / STAGES[st]["folder"]
        if _stage_done(stage_dir, STAGES[st]) and not args.force:
            continue
        P.drop_stage_outputs(st, stage_dir, workdir)


def main():
    ap = argparse.ArgumentParser(description="Run pain/messaging Apify steps.")
    ap.add_argument("phase", choices=list(PHASE_ORDER.keys()))
    ap.add_argument("--config", default=None)
    ap.add_argument("--workdir", required=True, help="this run's working folder")
    ap.add_argument("--only", default=None, help="run just one step, by its code: B2a (Google Maps places), "
                         "B2b (Google Maps reviews), TP (Trustpilot), AS (App Store), "
                         "GP (Google Play), RD (Reddit), G2 or CP (Capterra)")
    ap.add_argument("--test-only", action="store_true", help="stop after the test run")
    ap.add_argument("--dry-run", action="store_true", help="print plan + inputs, spend nothing")
    ap.add_argument("--force", action="store_true", help="re-run steps even if already complete")
    ap.add_argument("--run-ceiling-usd", type=float, default=None,
                    help="Hard per-run ceiling (USD); caps the budget and per-call "
                         "maxTotalChargeUsd. Set by entry script under --standing-approval. "
                         "Without this flag, behaviour is identical to today.")
    ap.add_argument("--run-ledger-path", default=None,
                    help="Path to a run-level ledger JSON file. When set, "
                         "budget.spent is initialised from the file's 'spent' value "
                         "(prior steps' cumulative spend for this run) and updated "
                         "after this step completes. Only meaningful with "
                         "--run-ceiling-usd (it enforces the ceiling across the whole "
                         "run, not just within a single subprocess).")
    args = ap.parse_args()

    cfg = C.load_config(args.config)
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    cap = cfg["budget_caps"].get(f"phase{args.phase}", 999.0)
    # --run-ceiling-usd (standing-approval mode): clamp the budget cap to the
    # user-approved ceiling.  WHY min not replace: the config's own cap may be
    # even tighter and must not be silently overridden upward.
    if args.run_ceiling_usd is not None:
        cap = min(cap, args.run_ceiling_usd)
    budget = Budget(args.phase, cap)

    # Run-level spend ledger.  Each stage subprocess is a fresh Budget(spent=0),
    # so without the ledger _cap_for() sees only the current stage's spend, not the
    # whole run.  The ledger file holds {"spent": <cumulative_float>}; we read it
    # before any stage runs to initialise budget.spent with prior stages' spend, and
    # write it back after this subprocess exits.  WHY inside workdir: the ledger is
    # part of the run record (same lifetime as the run folder) and must persist
    # across subprocess boundaries without a shared memory or daemon channel.
    _ledger_path = Path(args.run_ledger_path) if args.run_ledger_path else None
    if _ledger_path and _ledger_path.exists():
        try:
            _prior = json.loads(_ledger_path.read_text(encoding="utf-8"))
            _prior_spent = float(_prior.get("spent", 0.0))
            # NaN, Infinity or a negative figure is a corrupt ledger too.
            # float() accepts them, and then "ceiling - spent" was NaN and the step
            # ran under the smallest cap instead of stopping before any call.
            if _prior_spent != _prior_spent or _prior_spent in (float("inf"), float("-inf")) \
                    or _prior_spent < 0:
                raise ValueError(f"spent is {_prior_spent!r}, not an amount spent")
            budget.spent = _prior_spent
        except (OSError, ValueError, TypeError) as _ledger_exc:
            # A corrupt ledger under --run-ceiling-usd is NOT safe to ignore.
            # Starting at 0 is the LEAST conservative choice -- it lets this stage
            # spend the full ceiling on top of whatever prior stages already spent.
            # The entry script always creates the ledger before the first stage, so
            # a corrupt file means something went wrong.  Stop before any actor call.
            if args.run_ceiling_usd is not None:
                _detail = f"ledger corrupt: {_ledger_exc}"
                print(f"\n⛔ Run-level ledger unreadable: stopping before any actor call.")
                try:
                    _ledger_path.write_text(
                        json.dumps({"spent": 0.0,
                                    "stopped": {"stage": "NONE", "reason": "ledger",
                                                 "detail": _detail}},
                                   ensure_ascii=False),
                        encoding="utf-8",
                    )
                except OSError:
                    pass
                sys.exit(4)
            # Without a ceiling (non-approval path): keep original behaviour.
    elif _ledger_path is not None and args.run_ceiling_usd is not None:
        # Ledger path was set by the entry script but the file is missing.
        # The entry script always creates it before the first stage, so missing
        # means something went wrong.  Stop before any actor call.
        _detail = "ledger file missing"
        print(f"\n⛔ Run-level ledger missing: stopping before any actor call.")
        try:
            _ledger_path.write_text(
                json.dumps({"spent": 0.0,
                            "stopped": {"stage": "NONE", "reason": "ledger",
                                         "detail": _detail}},
                           ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass
        sys.exit(4)

    order = [args.only] if args.only else list(PHASE_ORDER[args.phase])

    # Task selection: if the config has a `stages` allow-list, run only those.
    sel = S.selected_stages(cfg)
    if sel is not None and not args.only:
        order = [s for s in order if s in sel]

    if not order:
        print(f"=== Phase {args.phase}: no selected steps, nothing to run. ===")
        return

    # An interactive run's cap holds for the whole
    # run, not for each step. Every paid step is its own run_phase process, so its
    # budget started at $0: with a $0.10 cap, Meta spent $0.055, Google's check
    # then saw "$0 spent" and billed $0.10 more, and the run billed $0.155. The
    # entry script names this run's ledger (budget.RUN_LEDGER_ENV),
    # which already counts every charge once (budget.charge), and the
    # budget now starts from it. Under a standing approval --run-ledger-path does
    # the same and the variable is not set; a dry run spends nothing.
    # The ledger restarts at $0 for each command (ad_intel, pain_intel and
    # search_intel write it when they start), so the cap is per command run: a
    # by-hand re-run of a folder is a new command with its own cost question,
    # and the steps that finished are skipped and bill nothing.
    _so_far = None
    if _ledger_path is None and not args.dry_run:
        try:
            _so_far = run_ledger_spent()
        except ValueError as _exc:
            # Starting from $0 would hand this step the whole cap again: stop
            # before any paid call, and name every step of this call as stopped.
            print(f"\n⛔ {_exc}, so what this run has already spent is not known. "
                  f"Stopping before any paid call.")
            for _st in order:
                if _stage_done(Path(workdir) / STAGES[_st]["folder"], STAGES[_st]) \
                        and not args.force:
                    continue  # Finished and not run again
                _note_step_stopped(Path(workdir) / STAGES[_st]["folder"], _st, "step_failed")
            _drop_unrun_outputs(order, workdir, args)
            sys.exit(4)
    if _so_far:
        budget.spent = _so_far

    print(f"=== Phase {args.phase} · budget cap ${cap_text(cap)} ===")
    if _so_far:
        print(f"   this run already spent ${_so_far:.4f} in its earlier steps, "
              f"and that counts against this cap")
    _budget_hit = False
    _stopped_info = None  # Set when we stop early; written to ledger for entry script
    for stage in order:
        spec = STAGES[stage]
        # What this step was charged before it started, and
        # the runs it launches, so a budget stop inside it can tell "part-way"
        # from "did not run".
        _spent_before = budget.spent
        _STEP_RUNS.clear()
        _STEP_SAVED.clear()
        try:
            # Enforce the cap before starting a new stage. Safe boundary:
            # nothing from this stage has been written yet.
            budget.enforce()
            _run_one(stage, spec, cfg, workdir, budget, args)
        except BudgetExceeded as e:
            print(f"\n⛔ {e}\n   Stopping. Spent ${budget.spent:.2f}.")
            # Every later step in this call did not run. This
            # step may have run part of its work first (_note_budget_stop).
            if not args.dry_run:
                _note_budget_stop(workdir, order, stage, cap,
                                  budget.spent - _spent_before, _STEP_RUNS,
                                  force=args.force)
                _drop_unrun_outputs(order[order.index(stage):], workdir, args)
            _budget_hit = True
            _stopped_info = {"stage": stage, "reason": "budget",
                             "detail": f"forecast exceeded ${cap_text(cap)} ceiling"}
            break
        except Exception as e:  # noqa: BLE001
            # Under standing-approval a stage exception must propagate as
            # INCOMPLETE (exit 4), not be silently swallowed.  WHY: an unattended
            # scheduled run that encounters an error and continues would report
            # success while skipping data collection silently — violating the "refuses or stops before
            # spending" guarantee.  The original
            # "continue" was safe in interactive mode where the user can see the
            # warning; it is not safe under a standing approval.
            if args.run_ceiling_usd is not None:
                print(f"\n⛔ {spec.get('name', stage)} (step {stage}) failed: {e}\n"
                      f"   Step error under standing approval: stopping.")
                if not args.dry_run:
                    _note_step_stopped(Path(workdir) / spec["folder"], stage, "step_failed",
                                       result_saved=bool(_STEP_SAVED))
                _drop_unrun_outputs(order[order.index(stage):], workdir, args)
                _budget_hit = True
                _stopped_info = {"stage": stage, "reason": "error",
                                 "detail": str(e)[:150]}
                break
            print(f"\n⚠ {spec.get('name', stage)} (step {stage}) failed: {e}\n"
                  f"   Continuing to the next step. The closing lines name this step.")
            # Recorded, so the closing lines name the failed step
            # instead of reading its empty result as "it ran, but nothing came back".
            if not args.dry_run:
                _note_step_stopped(Path(workdir) / spec["folder"], stage, "step_failed",
                                       result_saved=bool(_STEP_SAVED))
            _drop_unrun_outputs([stage], workdir, args)
    print(f"\n=== Phase {args.phase} done · spent ${budget.spent:.2f}/{cap_text(cap)} ===")
    # Write the run-level ledger BEFORE exiting, so the next subprocess in
    # this tool run reads the correct cumulative spend.  Written even on exit 4
    # so that any caller can audit total spend after an INCOMPLETE.
    # Also write stopped reason so the entry script can give an accurate message.
    if _ledger_path is not None:
        try:
            _ledger: dict = {"spent": round(budget.spent, 8)}
            if _stopped_info is not None:
                _ledger["stopped"] = _stopped_info
            _ledger_path.write_text(
                json.dumps(_ledger, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass  # Non-fatal: next subprocess will under-estimate remaining cap
    # Exit 4 (INCOMPLETE) when --run-ceiling-usd is set and the budget was hit
    # mid-run.  WHY only when ceiling is set: without --standing-approval the
    # existing behaviour (exit 0 after budget stop) is preserved byte-for-byte.
    if _budget_hit and args.run_ceiling_usd is not None:
        sys.exit(4)


def _run_has_inputs_tests():
    """Offline unit tests for _has_inputs. No network, no Apify calls."""
    PASS = 0
    FAIL = 0

    def check(label, got, expected):
        nonlocal PASS, FAIL
        if got == expected:
            print(f"  PASS  {label}")
            PASS += 1
        else:
            print(f"  FAIL  {label}")
            print(f"        got:      {got!r}")
            print(f"        expected: {expected!r}")
            FAIL += 1

    print("\n=== _has_inputs tests blank-list bypass ===")

    # --- Cases that MUST return False (would bypass the gate before fix) ---
    check('["", ""] -> False (blank strings)',
          _has_inputs("TP", {"companyUrls": ["", ""]}), False)
    check('["  "] -> False (whitespace-only)',
          _has_inputs("TP", {"companyUrls": ["  "]}), False)
    check('[None] -> False (None entries)',
          _has_inputs("AS", {"appIds": [None]}), False)
    check('[None, None] -> False (all-None list)',
          _has_inputs("AS", {"appIds": [None, None]}), False)
    check('["   "] -> False (whitespace URL for B2b)',
          _has_inputs("B2b", {"startUrls": ["   "]}), False)
    check('["", ""] -> False (empty strings for B2b)',
          _has_inputs("B2b", {"startUrls": ["", ""]}), False)
    check('RD searchTerms=[""] only -> False (blank term)',
          _has_inputs("RD", {"searchTerms": [""], "subredditUrls": [], "startUrls": []}), False)
    check('RD all keys blank -> False',
          _has_inputs("RD", {"searchTerms": [""], "subredditUrls": [""], "startUrls": [""]}), False)
    check('[] -> False (empty list, existing behavior preserved)',
          _has_inputs("TP", {"companyUrls": []}), False)

    # --- Cases that MUST return True (valid inputs must still pass) ---
    check('[{"url": "https://example.com"}] -> True (dict entry)',
          _has_inputs("B2b", {"startUrls": [{"url": "https://example.com"}]}), True)
    check('["324684580"] -> True (valid appId string)',
          _has_inputs("AS", {"appIds": ["324684580"]}), True)
    check('["https://trustpilot.com/review/acme.com"] -> True (valid URL)',
          _has_inputs("TP", {"companyUrls": ["https://trustpilot.com/review/acme.com"]}), True)
    check('RD valid searchTerm -> True',
          _has_inputs("RD", {"searchTerms": ["CRM software"], "subredditUrls": [], "startUrls": []}),
          True)
    check('RD valid subredditUrl only -> True',
          _has_inputs("RD", {"searchTerms": [], "subredditUrls": ["r/sales"], "startUrls": []}),
          True)
    check('Unknown stage / key not in list -> True (default passthrough)',
          _has_inputs("XX", {"unknownKey": []}), True)
    check('Mixed valid+blank: ["", "real-id"] -> True (at least one real entry)',
          _has_inputs("AS", {"appIds": ["", "real-id"]}), True)

    print(f"\nResults: {PASS} passed, {FAIL} failed")
    return FAIL


def _run_recovery_tests():
    """Offline tests for run recovery. No network: A.reattach and A.run_actor
    are monkeypatched with fakes so we test the routing, not Apify."""
    import tempfile
    PASS = 0
    FAIL = 0

    def check(label, got, expected):
        nonlocal PASS, FAIL
        if got == expected:
            print(f"  PASS  {label}")
            PASS += 1
        else:
            print(f"  FAIL  {label}  got={got!r} expected={expected!r}")
            FAIL += 1

    print("\n=== run recovery tests ===")
    with tempfile.TemporaryDirectory() as d:
        sd = Path(d)
        check("_input_hash order-stable", _input_hash({"a": 1, "b": 2}),
              _input_hash({"b": 2, "a": 1}))
        check("_input_hash input-sensitive",
              _input_hash({"a": 1}) != _input_hash({"a": 2}), True)
        check("no pending initially", _read_pending(sd), None)
        _write_pending(sd, {"runId": "x", "subkey": "full", "input_hash": "h"})
        check("pending roundtrip", _read_pending(sd)["runId"], "x")
        _clear_pending(sd)
        check("clear removes pending", _read_pending(sd), None)

        spec = {"actor": "acme/act"}
        inp = {"q": 1}
        ih = _input_hash(inp)
        calls = {"reattach": 0, "run_actor": 0}
        _real_reattach, _real_run_actor = A.reattach, A.run_actor
        try:
            # HIT: pending matches and reattach recovers -> no launch.
            _write_pending(sd, {"runId": "r9", "datasetId": "d9", "input_hash": ih,
                                "subkey": "full", "actor": "acme/act"})
            A.reattach = lambda rid, **kw: (
                calls.__setitem__("reattach", calls["reattach"] + 1)
                or {"status": "SUCCEEDED", "id": rid})
            A.run_actor = lambda *a, **k: (
                calls.__setitem__("run_actor", calls["run_actor"] + 1)
                or {"status": "SUCCEEDED"})
            _launch_recover(spec, inp, sd, "full")
            check("recover HIT reattaches", calls["reattach"], 1)
            check("recover HIT does not launch", calls["run_actor"], 0)

            # MISS: pending present but reattach returns None -> re-launch.
            calls["reattach"] = calls["run_actor"] = 0
            A.reattach = lambda rid, **kw: (
                calls.__setitem__("reattach", calls["reattach"] + 1) or None)
            _write_pending(sd, {"runId": "r9", "datasetId": "d9", "input_hash": ih,
                                "subkey": "full", "actor": "acme/act"})
            _launch_recover(spec, inp, sd, "full")
            check("recover MISS re-launches", calls["run_actor"], 1)

            # SUBKEY/HASH MISMATCH must not reattach a different run's handle.
            calls["reattach"] = calls["run_actor"] = 0
            A.reattach = lambda rid, **kw: (
                calls.__setitem__("reattach", calls["reattach"] + 1)
                or {"status": "SUCCEEDED", "id": rid})
            _write_pending(sd, {"runId": "rOther", "datasetId": "d", "input_hash": ih,
                                "subkey": "test", "actor": "acme/act"})
            _launch_recover(spec, inp, sd, "full")  # subkey 'full' != pending 'test'
            check("subkey mismatch does NOT reattach", calls["reattach"], 0)
            check("subkey mismatch launches fresh", calls["run_actor"], 1)

            # No pending -> launch, and on_start persists the handle for next time.
            _clear_pending(sd)

            def _fake_run_actor(actor, actor_input, on_start=None, **kw):
                on_start("rNEW", "dNEW")  # simulate the launch callback firing
                return {"status": "SUCCEEDED", "id": "rNEW"}
            A.run_actor = _fake_run_actor
            _launch_recover(spec, inp, sd, "full")
            pend = _read_pending(sd)
            check("launch persists runId at on_start", pend["runId"], "rNEW")
            check("pending records subkey", pend["subkey"], "full")
            check("pending records input_hash", pend["input_hash"], ih)
        finally:
            A.reattach, A.run_actor = _real_reattach, _real_run_actor

    print(f"\nResults: {PASS} passed, {FAIL} failed")
    return FAIL


if __name__ == "__main__":
    import sys as _sys
    # If called with --test or no args while not being a phase run, run tests.
    if len(_sys.argv) == 1 or (len(_sys.argv) == 2 and _sys.argv[1] == "--test"):
        fails = _run_has_inputs_tests()
        fails += _run_recovery_tests()
        _sys.exit(0 if fails == 0 else 1)
    main()
