"""
Deterministic core of the linkedin-follower-tracker, Claude-native version.

This is a faithful port of the n8n Code nodes (the executable spec) into pure
Python functions: no network, no side effects, no PhantomBuster, no LLM. Every
function here corresponds to a named node in the upstream n8n workflow (the
executable spec this port reproduces), and the port is asserted against that
spec by `tests/test_pipeline.py`.

The three network/judgment steps (PhantomBuster launches, the classification
subagent) live elsewhere; this module is the part that is fully testable offline,
which per the project's test strategy is the majority of the code and all of the risk that a
free, repeatable test can catch.

Ports, node by node:
  normalise_key / normalise_store  <- the `normalise` helper in the diff nodes
  parse_phantom_output             <- "Parse Phantom Output"
  find_new_followers               <- "Find New Followers"
  find_lost_followers              <- "Find Lost Followers"
  prepare_enrichment               <- "Prepare Enrichment Data"  (join deviation, see below)
  extract_classification           <- "Extract Classifications"  (tolerant, code owns contract)
  build_label_counts               <- the label loop in "Collect Summary"
  collect_summary                  <- "Collect Summary" (dynamic version)
  MASTER_COLUMNS / read_master / write_master / apply_diff  <- the Google Sheets nodes,
                                       reimplemented over a local CSV
"""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Any


# ---------------------------------------------------------------------------
# URL normalisation. TWO normalizers on purpose:
#   - normalise_key: for MATCHING. lowercase + strip trailing slash. Mirrors the
#     n8n `normalise` used by Find New/Lost Followers.
#   - normalise_store: for STORAGE. strip trailing slash only, NO lowercasing,
#     because a LinkedIn URN (the ACoAA... token) is case-sensitive and lowercasing
#     the stored value would corrupt identity. Mirrors n8n Prepare Enrichment Data,
#     which strips the slash but does not lowercase before writing profileUrl.
# ---------------------------------------------------------------------------

def normalise_key(url: Any) -> str:
    """Matching key: lowercase, trimmed, trailing slashes stripped."""
    return re.sub(r"/+$", "", (url or "").strip().lower())


def normalise_store(url: Any) -> str:
    """Storage form: trimmed, trailing slashes stripped, case preserved."""
    return re.sub(r"/+$", "", (url or "").strip())


# The LinkedIn URN embedded in a follower/profile URL. The collector returns
# followers as https://www.linkedin.com/in/ACoAA<urn>, so the URN is right there in
# the path — and it is the one identifier that survives the round trip through
# PhantomBuster's lead storage (stored as linkedin_profile_urn), where a vanity slug
# may be canonicalised. Case-sensitive: the URN token is not lowercased.
_URN_RE = re.compile(r"/in/(ACoAA[A-Za-z0-9_\-]+)")


def extract_urn(url: Any) -> str:
    """The ACoAA... URN token from a profile URL, or '' if the URL is not URN-form
    (e.g. a vanity /in/jane-doe URL, which has no URN to extract)."""
    if not url:
        return ""
    m = _URN_RE.search(str(url))
    return m.group(1) if m else ""


def match_key(record: Any) -> str:
    """
    The canonical identity key used to join a follower to its enrichment, robust to
    URL-form differences. Prefers the URN — from an explicit linkedin_profile_urn /
    linkedinProfileUrn / profileUrn field if present, else extracted from any
    URL-bearing field. Falls back to normalise_key(url) only when no URN exists
    (vanity-only URLs). Using the URN means the collector's URN-form URL, the
    org-storage lead's linkedin_profile_urn, and the synthetic scraper row's URL all
    resolve to the SAME key, so the join holds across all three.
    """
    if isinstance(record, dict):
        urn = (record.get("linkedinProfileUrn") or record.get("linkedin_profile_urn")
               or record.get("profileUrn"))
        if not urn:
            urn = extract_urn(record.get("profileUrl") or record.get("profileLink") or "")
        if urn:
            return str(urn)
        return normalise_key(record.get("profileUrl") or record.get("profileLink"))
    # a bare string URL
    return extract_urn(record) or normalise_key(record)


def build_lead_inputs(new_followers: list[dict]) -> list[dict]:
    """
    Turn new-follower records into org-storage lead payloads for `leads/save-many`.
    Each carries the required `linkedinProfileUrl` (case-preserved, slash-stripped)
    and, when the URL is URN-form, `linkedinProfileUrn` — the field the enrichment
    list filters on. A follower with no usable URL is dropped (it could not be
    scraped anyway); the caller can compare counts to notice any drop.
    """
    out = []
    for f in new_followers:
        url = normalise_store(f.get("profileLink") or f.get("profileUrl") or "")
        if not url:
            continue
        lead = {"linkedinProfileUrl": url}
        urn = extract_urn(url)
        if urn:
            lead["linkedinProfileUrn"] = urn
        out.append(lead)
    return out


# ---------------------------------------------------------------------------
# "Parse Phantom Output": collector output -> a flat list of follower dicts.
# The collector returns one item per follower when it is inline; when it is a
# single wrapper object we dig the follower array out of the known shapes.
# ---------------------------------------------------------------------------

def parse_phantom_output(items: list[dict]) -> list[dict]:
    """
    items: the raw collector output, as a list of {json:...}-unwrapped dicts
    (i.e. each element is already the follower dict, or a single wrapper dict).

    Returns the list of follower dicts. Never raises: an unrecognised single
    payload yields [] (the n8n node's debug fallback), so downstream sees "no new
    followers" rather than a crash.
    """
    if not items:
        return []
    if len(items) > 1:
        # One item per follower — the common inline case.
        return list(items)

    payload = items[0] or {}

    # Path 1: top-level resultObject as a JSON string.
    ro = payload.get("resultObject")
    if isinstance(ro, str):
        try:
            parsed = json.loads(ro)
            if isinstance(parsed, list):
                return parsed
        except (ValueError, TypeError):
            pass

    # Path 2: nested data.resultObject (string or already-parsed).
    data = payload.get("data") or {}
    ro2 = data.get("resultObject")
    if ro2 is not None:
        try:
            parsed = json.loads(ro2) if isinstance(ro2, str) else ro2
            if isinstance(parsed, list):
                return parsed
        except (ValueError, TypeError):
            pass

    # Path 3 / 4: top-level or nested result array.
    if isinstance(payload.get("result"), list):
        return payload["result"]
    if isinstance(data.get("result"), list):
        return data["result"]

    # Path 5: a single follower dict delivered without a wrapper (has profileLink).
    if payload.get("profileLink"):
        return [payload]

    # Debug fallback: unrecognised shape -> empty, never crash.
    return []


# ---------------------------------------------------------------------------
# "Find New Followers" / "Find Lost Followers": set difference on the URN key.
# current followers carry `profileLink`; master rows carry `profileUrl`.
# ---------------------------------------------------------------------------

def find_new_followers(current: list[dict], master_rows: list[dict]) -> list[dict]:
    """Followers present now but not in the master. Keyed on normalise_key."""
    master_keys = {
        normalise_key(r.get("profileUrl")) for r in master_rows
    }
    master_keys.discard("")
    return [
        f for f in current
        if normalise_key(f.get("profileLink")) not in master_keys
    ]


def find_lost_followers(current: list[dict], master_rows: list[dict]) -> list[dict]:
    """
    Master rows whose person is no longer in the current follower list. Returns
    the master row dicts themselves (so the caller can mark them), plus each row's
    matching key. Keyed on normalise_key.
    """
    current_keys = {
        normalise_key(f.get("profileLink")) for f in current
    }
    current_keys.discard("")
    lost = []
    for r in master_rows:
        key = normalise_key(r.get("profileUrl"))
        if key and key not in current_keys:
            lost.append(r)
    return lost


# ---------------------------------------------------------------------------
# "Prepare Enrichment Data": attach the scraper's company/profile fields to each
# new follower. DEVIATION FROM n8n: n8n joins by
# array index (fragile — assumes the scraper preserves input order). We join by
# the normalised URN key instead: order-independent, and a missing match is visible
# rather than silently mislabeling the next person.
# ---------------------------------------------------------------------------

def lead_freshness(record: Any) -> int:
    """
    A monotonic 'how fresh is this lead' score: the latest editionsHistory timestamp.
    PhantomBuster stamps every edition of a lead (collector, scraper, outreach phantom)
    with an integer epoch-MILLISECOND `timestamp` (verified live 2026-09-01: keys
    {mainAgentId, timestamp, type}). Returns 0 when a record carries no history.

    Used to pick the FRESHEST of several records for the same person, so a re-enriched
    lead's newest data wins over an older edition — never the arbitrary 'first' one.
    """
    if not isinstance(record, dict):
        return 0
    best = 0
    for ed in record.get("editionsHistory") or []:
        if not isinstance(ed, dict):
            continue
        ts = ed.get("timestamp")
        if isinstance(ts, bool):
            continue
        if isinstance(ts, (int, float)):
            best = max(best, int(ts))
        elif isinstance(ts, str) and ts.strip().isdigit():
            best = max(best, int(ts.strip()))
    return best


# The collector-sourced master columns, exactly as the n8n "Append New Followers"
# Google-Sheets node maps them (verified against the workflow JSON): the collector's
# own follower fields, renamed to their master-column names. This is the FIRST of n8n's
# two write phases; the scraper enrichment is overlaid on top (Prepare Enrichment Data ->
# "Update New Tab with Enrichment"). The Claude port originally dropped this phase, which
# left fullName / the _from_input names / followers / isFollowing / timestamp of download
# BLANK for every new follower. Blank-safe: a field the collector omitted maps to None
# (written as an empty cell), matching n8n's empty-cell behaviour.
def _collector_columns(follower: dict) -> dict:
    return {
        "fullName": follower.get("fullName"),
        "firstName_from_input": follower.get("firstName"),
        "lastName_from_input": follower.get("lastName"),
        "followers": follower.get("followers"),
        "isFollowing": follower.get("isFollowing"),
        "timestamp of download": follower.get("timestamp"),
    }


def prepare_enrichment(new_followers: list[dict], scraper_rows: list[dict]) -> list[dict]:
    """
    Returns one enrichment record per new follower, in new_followers order.

    Each record is the COLLECTOR's own columns (name/followers/isFollowing/download
    timestamp) overlaid with the matched SCRAPER row — so the scraper wins every field
    it carries (scraper primacy) and `profileUrl` is the stored (case-preserving,
    slash-stripped) form of the follower's URL. This mirrors the n8n original's two-phase
    write to one row: "Append New Followers" (collector columns) then "Update New Tab with
    Enrichment" (scraper columns). A new follower with no scraper match still yields its
    collector columns + profileUrl (no company fields) — visible, not silently dropped.
    """
    # Index scraper/enrichment rows by the canonical URN-preferring key, so the join
    # holds whether the row is a synthetic scraper record (URN-form profileUrl) or a
    # real org-storage lead (linkedin_profile_urn field). Keep the FRESHEST record per
    # key, not the first: a person can have more than one lead record (a re-enrichment,
    # or an older edition left by an outreach phantom), and 'first' would arbitrarily
    # pick a stale one. Freshness = latest editionsHistory timestamp (lead_freshness).
    by_key: dict[str, dict] = {}
    for row in scraper_rows:
        k = match_key(row)
        if not k:
            continue
        if k not in by_key or lead_freshness(row) > lead_freshness(by_key[k]):
            by_key[k] = row

    out = []
    for follower in new_followers:
        raw = follower.get("profileLink") or follower.get("profileUrl") or ""
        key = match_key(follower)
        store = normalise_store(raw)
        match = by_key.get(key, {})
        # Collector columns first, then the scraper row wins every field it carries
        # (scraper primacy). The two column sets are disjoint in practice except
        # profileUrl, which is pinned to the stored form last.
        record = {**_collector_columns(follower), **match}
        record["profileUrl"] = store  # overrides any trailing-slash form from the scraper
        out.append(record)
    return out


# ---------------------------------------------------------------------------
# "Extract Classifications": pull the single label out of the classifier's answer.
# In the Claude port the classifier is a `claude`-CLI subagent, so the answer is
# free text, not the n8n LangChain envelope. Code owns the output contract:
# this parse is tolerant and degrades to '' — never
# raises — on anything malformed. The caller maps '' to the taxonomy's catch-all.
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def extract_classification(raw: Any) -> str:
    """
    Accepts whatever the subagent returned (dict, JSON string, bare label,
    prose-wrapped JSON, code-fenced JSON, empty) and returns a single label
    string, or '' if nothing usable is present.
    """
    if raw is None:
        return ""

    # Already a dict: take the first value (mirrors n8n's Object.values(...)[0]).
    if isinstance(raw, dict):
        return _first_value(raw)

    if not isinstance(raw, str):
        raw = str(raw)

    text = raw.strip()
    if not text:
        return ""

    # Strip a code fence if present, keep its contents.
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()

    # Try to locate and parse a JSON object anywhere in the text.
    obj_match = _OBJ_RE.search(text)
    if obj_match:
        try:
            parsed = json.loads(obj_match.group(0))
            if isinstance(parsed, dict):
                val = _first_value(parsed)
                if val:
                    return val
        except (ValueError, TypeError):
            pass

    # Bare label: a short single line with no JSON. Strip surrounding quotes.
    first_line = text.splitlines()[0].strip().strip('"').strip("'")
    # Guard against a giant blob leaking in as a "label".
    if first_line and len(first_line) <= 120:
        return first_line
    return ""


def _first_value(d: dict) -> str:
    for v in d.values():
        return str(v).strip() if v is not None else ""
    return ""


def coerce_label(label: str, allowed: list[str] | None, fallback: str) -> str:
    """
    Snap an extracted label to the taxonomy. If `allowed` is given and the label
    is not one of them (case-insensitive), return `fallback`. If `allowed` is
    None, return the label as-is (or `fallback` when empty).
    """
    if not label:
        return fallback
    if allowed is None:
        return label
    lut = {a.lower(): a for a in allowed}
    return lut.get(label.lower(), fallback)


# ---------------------------------------------------------------------------
# "Collect Summary": dynamic per-label counts plus the change totals. This
# version derives the label keys from the taxonomy instead of hardcoding them.
# A hardcoded version would bake in keys like total_insurance_profiles /
# total_manufacturing_profiles, which break the moment the taxonomy changes;
# that is the pattern we must NOT reproduce.
# ---------------------------------------------------------------------------

def _is_active(row: dict) -> bool:
    """A row is a current follower iff it is not marked lost."""
    return not str(row.get("lost_on", "") or "").strip()


def build_label_counts(master_final_rows: list[dict], active_only: bool = True) -> dict[str, int]:
    """
    Count every distinct non-empty `classification` across the master.

    Labels that differ ONLY by case are the same segment and are folded together:
    a stray case-variant (e.g. a legacy "not target segment" row alongside the
    canonical "Not target segment", or a future classifier that emits "insurance")
    must not split a bucket in the report. Grouping is on casefold(); the bucket is
    shown under its DOMINANT original spelling (most frequent, ties broken by first
    appearance) so the majority casing wins and a single odd row cannot rename a
    segment. Buckets are emitted in first-appearance order for stable output.

    active_only (default True) reconciles decision 7 with n8n semantics: n8n
    DELETES lost followers before counting, so they never appear in the report.
    The CSV port RETAINS them (marked with lost_on) for history, so to reproduce
    n8n's numbers exactly, marked rows are excluded from the counts. Pass
    active_only=False to count the whole retained history instead.
    """
    # casefold key -> {original spelling: count}; insertion order preserves first-seen.
    variants: dict[str, dict[str, int]] = {}
    first_seen: dict[str, int] = {}
    for i, row in enumerate(master_final_rows):
        if active_only and not _is_active(row):
            continue
        label = str(row.get("classification", "") or "").strip()
        if not label:
            continue
        key = label.casefold()
        spellings = variants.setdefault(key, {})
        spellings[label] = spellings.get(label, 0) + 1
        first_seen.setdefault(key, i)

    counts: dict[str, int] = {}
    for key in sorted(variants, key=lambda k: first_seen[k]):
        spellings = variants[key]
        # Dominant spelling: max count; on a tie, dict order (first-seen) wins.
        display = max(spellings, key=lambda s: spellings[s])
        counts[display] = sum(spellings.values())
    return counts


def collect_summary(master_final_rows: list[dict], new_records: list[dict],
                    lost_count: int, taxonomy: dict, *, reactivated_count: int = 0) -> dict:
    """
    Ports the Collect Summary node with dynamic label keys, plus the per-segment breakdown.
    new_connections_total is the count of CURRENT (active, not-lost)
    followers — matching n8n, which had already deleted the lost rows by this point;
    net_increase_total is new minus lost; label_counts is the dynamic, active-only total
    per label.

    The added fields answer "how did the TARGET segments move this run": new_label_counts
    is THIS run's new followers per label; in_target_active_total is the active total across
    every in-target label. "In-target" is defined generically as every taxonomy label except
    the `fallback` (the catch-all, e.g. "Not target segment"), so it stays correct for any
    user-defined taxonomy — no hardcoded segment names. `labels`/`fallback_label` are carried
    so the report can render stable per-segment lines (including a 0) in taxonomy order.
    """
    labels = list(taxonomy.get("labels", []))
    fallback = taxonomy.get("fallback", labels[-1] if labels else "Other")
    fold = fallback.casefold()
    active_total = sum(1 for r in master_final_rows if _is_active(r))
    label_counts = build_label_counts(master_final_rows, active_only=True)
    # new followers are all active (brand new, no lost_on), so active_only doesn't matter.
    new_label_counts = build_label_counts(new_records, active_only=False)
    in_target_active_total = sum(c for lab, c in label_counts.items()
                                 if lab.casefold() != fold)
    return {
        "new_connections_total": active_total,
        "net_increase_total": len(new_records) - lost_count,
        "lost_connections": lost_count,
        "reactivated_count": reactivated_count,
        "label_counts": label_counts,
        "new_label_counts": new_label_counts,
        "in_target_active_total": in_target_active_total,
        "labels": labels,
        "fallback_label": fallback,
    }


# ---------------------------------------------------------------------------
# The local CSV master. Replaces the Google Sheet, plus a
# `lost_on` column (decision 7): lost followers are MARKED with a date, not
# deleted, keeping the history the n8n version throws away.
# ---------------------------------------------------------------------------

# The 43 columns from the n8n master header (Build Header Item), verbatim and in
# order, then `lost_on` appended by the Claude port.
MASTER_COLUMNS: list[str] = [
    "classification", "scrapeRound", "fullName", "firstName_from_input",
    "lastName_from_input", "profileUrl", "followers", "isFollowing",
    "timestamp of download", "companyIndustry", "companyName", "companyWebsite",
    "linkedinCompanyUrl", "linkedinCompanySlug", "linkedinCompanyId",
    "linkedinDescription", "linkedinHeadline", "linkedinIsHiringBadge",
    "linkedinIsOpenToWorkBadge", "linkedinJobDateRange", "linkedinJobLocation",
    "linkedinPreviousJobDescription", "linkedinJobTitle", "linkedinProfileId",
    "linkedinProfileSlug", "linkedinProfileUrl", "linkedinProfileUrn",
    "linkedinProfileImageUrl", "linkedinSkillsLabel", "location",
    "mutualConnectionsUrl", "connectionsUrl", "linkedinCompanyName",
    "linkedinCompanyDescription", "linkedinCompanyTagline",
    "linkedinCompanyFollowerCount", "linkedinCompanyWebsite",
    "linkedinCompanyEmployeesCount", "linkedinCompanySize",
    "linkedinCompanyIndustry", "linkedinCompanyHeadquarter",
    "linkedinCompanySpecialities", "linkedinCompanyFounded",
    "lost_on",
]


def read_master(text: str) -> list[dict]:
    """Parse master CSV text into a list of row dicts. Empty text -> []."""
    if not text or not text.strip():
        return []
    reader = csv.DictReader(io.StringIO(text))
    return [dict(row) for row in reader]


def write_master(rows: list[dict]) -> str:
    """Serialise rows to CSV text with the fixed MASTER_COLUMNS header. Unknown
    keys are dropped; missing keys are written blank. Round-trips commas/newlines
    inside fields via the csv module's quoting."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=MASTER_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in MASTER_COLUMNS})
    return buf.getvalue()


# MASTER_COLUMNS without the retention-marker column — matches the format the owner
# maintains in Google Sheets, where unfollowers are deleted rather than marked.
ACTIVE_MASTER_COLUMNS: list[str] = [c for c in MASTER_COLUMNS if c != "lost_on"]


def write_active_master(rows: list[dict]) -> str:
    """Serialise only the ACTIVE (not-lost) rows to CSV, with the lost_on column dropped.

    An additional standing output alongside the full
    retain-and-mark master — matches the format the owner maintains in Google Sheets
    (unfollowers are excluded entirely; no lost_on column). The full master is unchanged.
    Rows are active when lost_on is empty or absent (_is_active). Unknown keys are dropped;
    missing keys are written blank."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=ACTIVE_MASTER_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        if _is_active(row):
            writer.writerow({c: row.get(c, "") for c in ACTIVE_MASTER_COLUMNS})
    return buf.getvalue()


def apply_diff(
    master_rows: list[dict],
    new_records: list[dict],
    lost_rows: list[dict],
    lost_on: str,
    *,
    reactivate_keys: "list[str] | None" = None,
) -> list[dict]:
    """
    Produce the next master state: mark each lost row with `lost_on` (idempotent —
    a row already marked keeps its first date), clear `lost_on` for returning followers,
    and append the new records. Keyed on normalise_key(profileUrl). Returns a new list;
    does not mutate inputs.

    `lost_on` is passed in (never computed here) so the function stays pure and the
    date is controllable in tests — no clock dependency.

    `reactivate_keys` is computed by stage_diff (which has both the current pull and
    the master in hand) and passed forward here — the diff already knows the current
    follower set, so re-detecting returners downstream would be brittle re-derivation.
    A returning follower is NOT in new_records (find_new_followers excludes all master
    keys) so the reactivation must be handled here against the existing master rows.

    Append is idempotent: a new_record whose key is already present in the
    master is silently dropped — never appended again. In the normal single-run path
    this changes nothing (find_new_followers already excluded every master key before
    stage_diff produced new_records), so the happy path and all existing invariants
    are untouched. The guard fires only on re-entry: if stage_report is re-run after a
    crash that wrote master.csv but before the idempotency marker could be written, the
    master already carries those new followers and this dedup makes the retry a no-op
    rather than a double-append. Key space is ALL master rows (active and lost) — the
    same space find_new_followers uses — so this is consistent with how "new" is defined.
    """
    lost_keys = {normalise_key(r.get("profileUrl")) for r in lost_rows}
    lost_keys.discard("")
    reactivate = set(reactivate_keys) if reactivate_keys else set()
    reactivate.discard("")

    out: list[dict] = []
    # Build master key set over ALL rows (active and lost) BEFORE appending, so a key
    # from any prior run is never duplicated regardless of active/lost status.
    master_keys = {normalise_key(r.get("profileUrl")) for r in master_rows}
    master_keys.discard("")

    for row in master_rows:
        r = dict(row)
        key = normalise_key(r.get("profileUrl"))
        if key in lost_keys and not r.get("lost_on"):
            r["lost_on"] = lost_on
        elif key in reactivate and r.get("lost_on"):
            r["lost_on"] = ""
        out.append(r)

    for rec in new_records:
        if normalise_key(rec.get("profileUrl")) not in master_keys:
            out.append(dict(rec))
    return out
