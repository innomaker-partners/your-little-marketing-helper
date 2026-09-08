"""
End-to-end runner for the linkedin-follower-tracker Claude port.

Chains the five stages the n8n workflow performs, and can run them all or one at a
time (incremental). It runs against the synthetic PhantomBuster, so a full rehearsal
costs nothing and touches no real account:

    # full end-to-end rehearsal against the real local HTTP endpoint, sample data
    python3 run_pipeline.py --backend http --mode sample --workdir ./_run --stage all

    # production-scale rehearsal against the in-process look-alike
    python3 run_pipeline.py --backend fake --mode scale --workdir ./_run_scale --stage all

    # one stage at a time (reuses the artifacts already in the workdir)
    python3 run_pipeline.py --stage diff --workdir ./_run

Backends:
    fake  - the in-process look-alike (fastest; no network)
    http  - starts a real local HTTP endpoint and talks to it over the wire (closest
            to the real run; exercises the actual HTTP + large-output download)
    real  - the real PhantomBuster. GUARDED: refused unless PB_LIVE=1 is set, because
            it spends money and is meant to be the one deliberate production run.

Stages persist JSON artifacts in the workdir, so incremental runs chain naturally.
The classifier has two modes: a deterministic DEV STUB (`--classifier stub`) that
returns the same '{"classification": "..."}' shape the real subagent does — so the
tolerant parse (pipeline.extract_classification / coerce_label) is exercised offline —
and the real Haiku claude-CLI subagent (`--classifier claude`, in classifier.py), which
is wired and verified to return clean labels. The stub is the free rehearsal path; the
live run uses `--classifier claude`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import pipeline as P
import notify
from pb_client import PhantomBusterClient, HttpTransport, PBStop
from pb_fake import FakeTransport
from dev_seed import (build_fake, load_scenario, COLLECTOR_AGENT_ID, SCRAPER_AGENT_ID)

# The session cookie is no longer passed in — the client fetches the freshest one live
# from each agent (agents/fetch) right before launch, exactly as n8n's "Get an agent"
# node does. So every backend passes None here and lets the client resolve it.
DYNAMIC_SESSION = None

# Mass-loss circuit-breaker threshold. If a single run would mark MORE than this fraction
# of the currently-active followers as lost, stage_report STOPs before writing master.csv
# or notifying. This is the second line of defence behind the tested large-output parse:
# the 4392->1 collector-collapse class of failure makes the diff flip nearly every existing
# follower to "lost", which would silently corrupt the master and fire a false mass-loss
# report. The number is DERIVED, not calibrated: real follower counts move slowly between
# runs, so a genuine interval loses a small fraction while a collapse loses ~100% — 0.5
# sits far above real churn and far below a collapse, so it catches the disaster without
# tripping on a normal run. Skipped when the active baseline is empty (a cold-start first
# run, where everyone is new and nobody can be lost).
MAX_LOST_FRACTION = 0.5


# ---------------------------------------------------------------------------
# Per-subject durable directory + intake + seed mode
# ---------------------------------------------------------------------------

def make_slug(label: str) -> str:
    """Compute the per-subject directory slug from a human label.

    Lowercase, collapse runs of non-alphanumeric characters to a single hyphen,
    strip leading/trailing hyphens.

    Examples:
        "ACME CEO"  -> "acme-ceo"
        "Beta Corp" -> "beta-corp"
        " Foo  42 " -> "foo-42"
    """
    s = label.lower()
    s = re.sub(r'[^a-z0-9]+', '-', s)
    return s.strip('-')


def subject_dir_for(parent: str, subject_label: str) -> Path:
    """Compute the full per-subject dir path: <parent>/linkedin-follower-tracker/<slug>/."""
    return Path(parent) / "linkedin-follower-tracker" / make_slug(subject_label)


def _compute_work_dir(dest_dir: Path) -> Path:
    """Compute the deterministic per-subject temp work directory.

    Pattern: <system-tmpdir>/linkedin-follower-tracker/<slug>/
    where slug = last path component of dest_dir (the slug created by make_slug).

    The path is deterministic — same dest always maps to the same work dir — so the
    skill can find it for salvage even after a crash. Created fresh each run is fine;
    its contents are ephemeral and a crash leaving stale files there is harmless.
    """
    slug = dest_dir.name
    return Path(tempfile.gettempdir()) / "linkedin-follower-tracker" / slug


def _init_subject_scaffold(subject_dir: Path) -> None:
    """Create the durable per-subject directory tree and copy example configs if absent.

    Directory layout created:
        <subject_dir>/
            .linkedin-follower-tracker/
                config.json      (copied from config/FOLLOWER_CONFIG.example.json if absent)
                taxonomy.json    (copied from config/taxonomy.example.json if absent)
            captures/            (durable money-salvage dir)
    """
    hidden = subject_dir / ".linkedin-follower-tracker"
    hidden.mkdir(parents=True, exist_ok=True)
    (subject_dir / "captures").mkdir(parents=True, exist_ok=True)

    cfg_dir = Path(__file__).resolve().parent.parent / "config"
    for src_name, dst_name in [
        ("FOLLOWER_CONFIG.example.json", "config.json"),
        ("taxonomy.example.json", "taxonomy.json"),
    ]:
        dst = hidden / dst_name
        if not dst.exists():
            src = cfg_dir / src_name
            if src.exists():
                dst.write_bytes(src.read_bytes())

    print(f"  scaffolded: {subject_dir}")


def _config_is_filled(cfg: dict) -> bool:
    """True when the config holds a real collector_id (not the placeholder 'YOUR_...')."""
    cid = str(cfg.get("collector_id", "")).strip()
    return bool(cid) and not cid.upper().startswith("YOUR_")


def stage_seed(subject_dir: Path, client, collector_id, cookie) -> list:
    """Collect current followers and write them as master.csv baseline. STOP — seed only.

    This is the ONLY sanctioned way to establish a per-subject baseline. It explicitly
    skips diff, enrich, classify, report, and push — those stages run only in a normal
    (post-seed) run. A seeded.json marker records the seed so operators can verify it
    happened and when.

    The collector emits `profileLink`; master rows must carry `profileUrl` because
    find_lost_followers keys on that field for every subsequent diff. We derive profileUrl
    from profileLink here so the master is immediately diff-ready without enrichment.
    """
    followers = client.collect_followers(collector_id, cookie)
    for f in followers:
        if not f.get("profileUrl") and f.get("profileLink"):
            f["profileUrl"] = P.normalise_store(f["profileLink"])
    _master_path(subject_dir).write_text(P.write_master(followers))
    seeded_marker = {
        "seeded": True,
        "ts": round(time.time(), 1),
        "count": len(followers),
    }
    hidden = subject_dir / ".linkedin-follower-tracker"
    hidden.mkdir(parents=True, exist_ok=True)
    (hidden / "seeded.json").write_text(json.dumps(seeded_marker, indent=2))
    print(f"  SEEDED: wrote master.csv with {len(followers)} followers")
    print(f"  seed: skipping diff / enrich / classify / report / push (seed-only run)")
    return followers


# ---------------------------------------------------------------------------
# Naming conventions. Deployment-specific labels (csv_name, enrich_list_name) may carry
# literal tokens that are substituted with the run's date/time here, at the runner
# boundary — the same place csv_name's {date} has always been resolved — so the client
# stays account-agnostic and never computes a clock value itself. csv_name uses {date}
# only (n8n parity); the enrichment list uses {date} {time} so every kept list is
# uniquely, traceably named and a same-day re-run can never collide with an existing one.
# ---------------------------------------------------------------------------

def _stamp_name(name: str, now) -> str:
    return (name.replace("{date}", now.strftime("%Y-%m-%d"))
                .replace("{time}", now.strftime("%H:%M:%S")))


# ---------------------------------------------------------------------------
# Backend wiring
# ---------------------------------------------------------------------------

def make_backend(backend: str, mode: str, running_polls: int, *,
                 collector_runtime: float = 0.0, scraper_runtime: float = 0.0,
                 poll_interval: float = 0.05, on_poll=None, on_capture=None,
                 csv_name: str | None = None, enrich_list_name: str | None = None,
                 config_path: str | None = None):
    """Returns (client, collector_id, scraper_id, cookie, cleanup)."""
    realistic = collector_runtime > 0 or scraper_runtime > 0

    if backend == "fake":
        fake = build_fake(load_scenario(mode), running_polls=running_polls,
                          collector_runtime=collector_runtime,
                          scraper_runtime=scraper_runtime)
        client = PhantomBusterClient(
            FakeTransport(fake),
            sleep=(time.sleep if realistic else (lambda s: None)),
            poll_interval=poll_interval, on_poll=on_poll, on_capture=on_capture,
            csv_name=csv_name, enrich_list_name=enrich_list_name)
        return client, COLLECTOR_AGENT_ID, SCRAPER_AGENT_ID, DYNAMIC_SESSION, (lambda: None)

    if backend == "http":
        from dev_pb_server import DevPBServer
        srv = DevPBServer(mode=mode, running_polls=running_polls,
                          collector_runtime=collector_runtime,
                          scraper_runtime=scraper_runtime).start()
        client = PhantomBusterClient(
            HttpTransport(api_key="dev-ignored", base_url=srv.base_url),
            poll_interval=poll_interval, on_poll=on_poll, on_capture=on_capture,
            csv_name=csv_name, enrich_list_name=enrich_list_name)  # real HTTP, real sleep
        return client, COLLECTOR_AGENT_ID, SCRAPER_AGENT_ID, DYNAMIC_SESSION, srv.stop

    if backend == "real":
        if os.environ.get("PB_LIVE") != "1":
            sys.exit("REFUSED: --backend real spends money. Set PB_LIVE=1 only for the "
                     "deliberate production run, and supply a FOLLOWER_CONFIG.json and "
                     "a PHANTOMBUSTER_API_KEY in scripts/.env.")
        # Credentials + per-subject config come from scripts/.env + FOLLOWER_CONFIG.json,
        # both validated and exited with a value-free message if any required value is
        # missing. user_agent and adds_per_launch are carried into the client so both
        # launches ride the matched UA and the scraper batch always covers every profile.
        from config import get_pb_config
        cfg = get_pb_config(config_path=config_path)
        print("Live config loaded (values redacted):")
        for k, v in cfg.redacted().items():
            print(f"  {k}: {v}")
        # Naming conventions come from FOLLOWER_CONFIG.json, not hardcoded here, so the
        # shipped code stays account-agnostic. csv_name carries {date}; enrich_list_name
        # carries {date} {time}. Both are stamped once with THIS run's clock.
        from datetime import datetime
        now = datetime.now()
        stamped_csv = _stamp_name(cfg.csv_name, now)
        client = PhantomBusterClient(
            HttpTransport(api_key=cfg.api_key),
            poll_interval=(poll_interval if poll_interval > 1 else 30.0),
            on_poll=on_poll,  # real run: poll every ~30s over a ~1h wait
            on_capture=on_capture,  # capture-once: paid handles/results land on disk
            user_agent=cfg.user_agent, adds_per_launch=cfg.adds_per_launch,
            identity_id=cfg.identity_id, csv_name=stamped_csv,
            enrich_list_name=_stamp_name(cfg.enrich_list_name, now))
        # Session cookie is fetched live from each agent, not read from config.
        return (client, cfg.collector_id, cfg.scraper_id, DYNAMIC_SESSION, (lambda: None))

    raise ValueError(f"unknown backend {backend!r}")


# ---------------------------------------------------------------------------
# Taxonomy + the dev classifier stub
# ---------------------------------------------------------------------------

def load_taxonomy(taxonomy_path=None) -> dict:
    """Load the classifier taxonomy. When taxonomy_path is given and exists, use it.
    Falls back to the shipped config/taxonomy.example.json so a bare dev/test run (no
    --taxonomy arg) still works without configuration."""
    if taxonomy_path:
        p = Path(taxonomy_path)
        if p.exists():
            return json.loads(p.read_text())
    cfg_dir = Path(__file__).resolve().parent.parent / "config"
    for name in ("taxonomy.json", "taxonomy.example.json"):
        p = cfg_dir / name
        if p.exists():
            return json.loads(p.read_text())
    return {"labels": ["Other"], "fallback": "Other"}


def dev_classify_answer(record: dict) -> str:
    """DEV STUB standing in for the claude-CLI subagent. Returns the SAME shape the
    real subagent will ('{"classification": "..."}') so the parse path is real."""
    text = " ".join(str(record.get(k, "")) for k in
                    ("companyIndustry", "linkedinCompanyIndustry", "linkedinJobTitle",
                     "companyName")).lower()
    if any(w in text for w in ("software", "tech", " it ", "saas")):
        label = "Tech"
    elif "insurance" in text:
        label = "Insurance"
    elif any(w in text for w in ("manufactur", "industrial")):
        label = "Manufacturing"
    elif any(w in text for w in ("marketing", "advertis")):
        label = "Marketing"
    else:
        label = "Other"
    return json.dumps({"classification": label})


# ---------------------------------------------------------------------------
# Stages. Each reads/writes JSON in the workdir so single-stage runs chain.
# ---------------------------------------------------------------------------

def _read(workdir, name, default=None):
    p = Path(workdir, name)
    return json.loads(p.read_text()) if p.exists() else default


def _write(workdir, name, obj):
    Path(workdir, name).write_text(json.dumps(obj, indent=2))


# ---------------------------------------------------------------------------
# Keep-alive + wake signalling.
#
# Perspective A (the process stays alive): the poll heartbeat below fires on every
# status check during the long phantom wait, printing a line AND updating status.json,
# so an observer (or a waking agent) can see the run is alive and how far along it is.
#
# Perspective B (the conversation is woken): this script is meant to run as a
# BACKGROUND task; its completion is what wakes the orchestrating Claude conversation
# (in Claude Code, the background-task completion notification). For an unattended run
# where the agent may have to re-check on a schedule instead, the script also drops a
# durable run_complete.json marker the waking agent reads to learn the outcome without
# having watched the whole run.
# ---------------------------------------------------------------------------

def write_status(workdir, **fields):
    fields["ts"] = round(time.time(), 1)
    _write(workdir, "status.json", fields)


def make_heartbeat(workdir):
    t0 = time.time()

    def heartbeat(info):
        wall = round(time.time() - t0, 1)
        print(f"    ...alive | {info['agent_id']} poll #{info['poll']} "
              f"status={info['status']} phantom_elapsed={info.get('elapsed')}s "
              f"wall={wall}s", flush=True)
        write_status(workdir, state="waiting", agent=info["agent_id"],
                     poll=info["poll"], status=info["status"],
                     phantom_elapsed=info.get("elapsed"), wall_elapsed=wall)
    return heartbeat


def make_capture_sink(workdir):
    """The on-disk half of the capture-once rule: the client
    hands each paid handle/result to this sink the instant it arrives, and we write it
    to workdir/captures/<name>.json. If a launch happens and the run then crashes, these
    files are the salvage — the container id / list id to find the paid run in the PB
    console, plus whatever data was already fetched — so no paid result is ever lost to
    an exception. Each name maps to one file, so re-captures overwrite cleanly and no
    partial read-modify-write can corrupt the record."""
    cap_dir = Path(workdir, "captures")

    def sink(name, value):
        cap_dir.mkdir(parents=True, exist_ok=True)
        (cap_dir / f"{name}.json").write_text(
            json.dumps({"name": name, "ts": round(time.time(), 1), "value": value},
                       indent=2))
        shape = f"{len(value)} records" if isinstance(value, list) else "handle"
        print(f"    [capture] {name} ({shape}) -> captures/{name}.json", flush=True)
    return sink


def _master_path(workdir):
    return Path(workdir, "master.csv")


def _atomic_write_master(dest, text: str) -> None:
    """Write master.csv atomically: write to a temp file in dest, then os.replace.

    Same-directory temp ensures the rename (os.replace) is an atomic rename on POSIX
    and Windows — no partial file is ever visible at the final path. A crash after the
    write but before the replace leaves a stale .master_tmp_*.csv in dest; that is
    harmless and can be cleaned up manually. A crash during the write leaves the prior
    master intact at its path.
    """
    dest_path = _master_path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(dest_path.parent),
                                    prefix=".master_tmp_", suffix=".csv")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp_path, str(dest_path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _backup_master(dest, work) -> None:
    """Copy current dest/master.csv to work/master.PRE-WRITE-backup.csv.

    Called immediately BEFORE _atomic_write_master overwrites dest. The backup lives in
    the temp work dir so it is ephemeral and does not pollute the durable subject dir.
    If the master write fails, the backup at this deterministic path allows restoring
    the prior master by hand. If dest/master.csv does not exist (cold start or new
    subject), there is nothing to back up and this is a no-op.
    """
    src = _master_path(dest)
    if not src.exists():
        return
    Path(work).mkdir(parents=True, exist_ok=True)
    backup = Path(work, "master.PRE-WRITE-backup.csv")
    backup.write_bytes(src.read_bytes())


def init_workdir(workdir, mode, *, dest=None, backend: str = "fake", cold_start: bool = False):
    """
    Establish the master.csv baseline the diff runs against. Three cases, because the
    baseline is the ONE thing a wrong value silently corrupts (a bad baseline makes the
    real followers diff against invented rows and enriches thousands by mistake):

      1. A master.csv already exists -> use it untouched (an incremental run: the real
         baseline you placed, or a prior run's accumulated master).
      2. Absent, synthetic backend (fake/http) -> seed the scenario's prior state, so a
         free rehearsal starts from a realistic prior-run shape.
      3. Absent, real backend -> a money run. NEVER seed synthetic data here. Refuse
         loudly unless cold_start says this is a deliberate first-ever run, in which
         case seed an EMPTY master so every current follower is new and this run
         establishes the baseline (a full first-run enrichment — real cost on the whole
         following, which is why it must be asked for explicitly and cannot be rehearsed).

    `dest` is the durable subject dir where master.csv lives. When None (backward
    compat / --workdir-only mode), dest == workdir and nothing changes.
    """
    if dest is None:
        dest = workdir
    Path(workdir).mkdir(parents=True, exist_ok=True)
    if dest != workdir:
        Path(dest).mkdir(parents=True, exist_ok=True)
    mp = _master_path(dest)
    if mp.exists():
        if cold_start:
            print(f"  note: --cold-start ignored — a baseline already exists at {mp}; "
                  f"running incrementally against it.")
        return

    if backend == "real":
        if not cold_start:
            # Fail loud (like a missing credential): a real run with no baseline stops
            # BEFORE anything launches, rather than seeding synthetic rows that would
            # diff against the real following and enrich it wholesale by mistake.
            raise SystemExit(
                f"REFUSED: no master.csv baseline in {workdir} for a real run.\n"
                f"An incremental run needs the real prior follower baseline — e.g. the "
                f"current master exported from the existing n8n/Google-Sheet flow — "
                f"placed at:\n    {mp}\n"
                f"If this is the FIRST-EVER run with no prior baseline, pass --cold-start "
                f"to start from empty and establish it. Cold start treats EVERY current "
                f"follower as new: a full first-run enrichment + classification, at real "
                f"PhantomBuster cost on the whole following. Refusing to auto-seed "
                f"synthetic data on a money run.")
        mp.write_text(P.write_master([]))
        print("  COLD START: no prior baseline; seeded an EMPTY master.csv. This run "
              "will treat EVERY current follower as new — a full first-run enrichment + "
              "classification (real PhantomBuster cost on the whole following). Intended "
              "only for the very first run.")
        return

    prev = load_scenario(mode)["master_prev"]
    mp.write_text(P.write_master(prev))
    print(f"  seeded master.csv from '{mode}' prior state: {len(prev)} rows")


def stage_collect(workdir, client, collector_id, cookie):
    followers = client.collect_followers(collector_id, cookie)
    _write(workdir, "current_followers.json", followers)
    print(f"  collected {len(followers)} current followers")
    return followers


def stage_diff(workdir, *, dest=None):
    """workdir = ephemeral work dir; dest = durable subject dir (master.csv lives there).
    When dest is None (backward compat / --workdir-only), dest == workdir."""
    if dest is None:
        dest = workdir
    current = _read(workdir, "current_followers.json", [])
    master = P.read_master(_master_path(dest).read_text())
    new = P.find_new_followers(current, master)
    lost = P.find_lost_followers(current, master)
    _write(workdir, "new_followers.json", new)
    _write(workdir, "lost_followers.json", lost)
    print(f"  diff: {len(new)} new, {len(lost)} lost (against {len(master)} master rows)")
    return new, lost


def stage_enrich(workdir, client, scraper_id, cookie, *,
                 stop_before_scrape=False, resume_scrape=False):
    new = _read(workdir, "new_followers.json", [])
    # Build org-storage lead inputs (url + URN) and let the client run the
    # save-many -> list -> scrape -> read-back flow. It returns the enriched leads.
    lead_inputs = P.build_lead_inputs(new)
    # Completeness gate #0 (before any money): build_lead_inputs drops a new follower
    # only if it has NO profile URL at all — which would silently exclude a real person
    # from a paid, client-facing enrichment. Fail loud rather than under-deliver; the
    # collector always yields URL-bearing rows, so a shortfall is a genuine anomaly to
    # inspect (in new_followers.json), not a normal path.
    if len(lead_inputs) != len(new):
        raise SystemExit(
            f"REFUSED: {len(new) - len(lead_inputs)} of {len(new)} new followers have no "
            f"profile URL and cannot be enriched. Inspect {workdir}/new_followers.json "
            f"before spending on a scrape that would silently skip them.")

    prepared_path = Path(workdir, "enrich_prepared.json")

    # --- resume: run ONLY the paid scrape against a list prepared earlier -------------
    if resume_scrape:
        if not prepared_path.exists():
            raise SystemExit(
                f"REFUSED: --resume-scrape but no prepared list at {prepared_path}. "
                f"Run the prepare step (--stage enrich --stop-before-scrape) first.")
        prep = json.loads(prepared_path.read_text())
        list_id, expected = prep["list_id"], prep["expected"]
        print(f"  resuming: scraping prepared list {list_id} ({expected} leads)...",
              flush=True)
        scraped = client.scrape_enrichment_list(scraper_id, list_id,
                                                session_cookie=cookie, expected=expected)
        prepared_path.unlink()  # local marker consumed; the list itself is KEPT (dated) on PB
        enriched = P.prepare_enrichment(new, scraped)
        _write(workdir, "enrichment.json", enriched)
        print(f"  enriched {len(enriched)} new followers "
              f"({len(scraped)} enriched leads via org-storage list)")
        return enriched

    # --- prepare-only: build + verify the list, then STOP before the paid launch ------
    if stop_before_scrape:
        if not lead_inputs:
            print("  no new followers to enrich; nothing to prepare.")
            return []
        list_id = client.prepare_enrichment_list(lead_inputs)
        _write(workdir, "enrich_prepared.json",
               {"list_id": list_id, "expected": len(lead_inputs),
                "ts": round(time.time(), 1)})
        print(f"  PREPARED org-storage list {list_id}: VERIFIED to resolve to all "
              f"{len(lead_inputs)} new followers (the prepare summary above reports how "
              f"many were newly saved vs already leads).")
        print(f"  PAUSED before the paid scraper launch. The list is active on "
              f"PhantomBuster; its id is in captures/enrich_list_id.json + enrich_prepared.json.")
        print(f"  Resume the paid step with: --stage enrich --resume-scrape")
        return []

    # --- default: prepare + scrape in one call (the all-at-once path) ------------------
    scraped = client.enrich_profiles(scraper_id, lead_inputs, cookie) if lead_inputs else []
    enriched = P.prepare_enrichment(new, scraped)
    _write(workdir, "enrichment.json", enriched)
    print(f"  enriched {len(enriched)} new followers "
          f"({len(scraped)} enriched leads via org-storage list)")
    return enriched


def stage_classify(workdir, classifier_mode="stub", model="haiku", taxonomy_path=None):
    enriched = _read(workdir, "enrichment.json", [])
    tax = load_taxonomy(taxonomy_path)
    if classifier_mode == "claude":
        # The real labeler: a Haiku subagent per follower (subscription, no API).
        import classifier as C
        labels = C.classify_followers(enriched, tax, model=model)
    else:
        # Offline deterministic stub, returning the same shape the real subagent does
        # so the parse path is identical either way.
        labels = []
        for rec in enriched:
            answer = dev_classify_answer(rec)
            label = P.extract_classification(answer)
            labels.append(P.coerce_label(label, tax.get("labels"),
                                         tax.get("fallback", "Other")))
    out = [{**rec, "classification": lab} for rec, lab in zip(enriched, labels)]
    _write(workdir, "classified.json", out)
    counts = P.build_label_counts(out, active_only=False)
    print(f"  classified {len(out)} followers ({classifier_mode}): {counts}")
    return out


# ---------------------------------------------------------------------------
# Append-idempotency guard.
#
# apply_diff APPENDS new-follower records unconditionally. Running stage_report
# twice against the same workdir (a crash-resume or an accidental re-invoke of
# --stage report) re-reads the same classified.json and appends the SAME batch
# again — silently doubling every new follower in master.csv. On a real subject
# dir that is the one artifact we exist to maintain; there is no backup.
#
# The guard keys on the SCRAPER CONTAINER ID — the identity of the paid work —
# rather than the date. A legitimate second scrape on the same day gets a new
# container ID and is allowed to proceed. Only a literal replay of the SAME
# scrape's output against the SAME master is refused.
# ---------------------------------------------------------------------------

def _read_scraper_container_id(workdir) -> "str | None":
    """Read the scraper container ID from the run's captures/ directory.
    Falls back to the collector container ID when no scraper capture exists.
    Returns None when neither capture is found (guard skipped — no stable key)."""
    for name in ("scraper_container_id", "collector_container_id"):
        cap = Path(workdir, "captures", f"{name}.json")
        if cap.exists():
            try:
                data = json.loads(cap.read_text())
                val = data.get("value")
                if val:
                    return str(val)
            except (ValueError, OSError):
                pass
    return None


def _done_marker_path(workdir, container_id: str) -> Path:
    """Path of the idempotency marker for one specific scrape container:
    <workdir>/.linkedin-follower-tracker/runs/<container-id>.done"""
    return Path(workdir, ".linkedin-follower-tracker", "runs",
                f"{container_id}.done")


def _content_based_marker_key(workdir) -> "str | None":
    """Defence-in-depth fallback (Fix B): when no capture file holds a stable container
    id, key the marker on sha256 of classified.json (first 16 hex chars). Same content
    → same key → same marker → guard still fires on retry. Returns None when
    classified.json is absent (nothing to key on; with Fix A the data is safe anyway)."""
    import hashlib
    classified_path = Path(workdir, "classified.json")
    if not classified_path.exists():
        return None
    content = classified_path.read_bytes()
    if not content or not content.strip():
        return None
    return "content_" + hashlib.sha256(content).hexdigest()[:16]


def _check_append_idempotency(workdir, *, dest=None) -> "str | None":
    """Guard: exit non-zero if this scrape's container already updated the master.
    Must be called BEFORE apply_diff. On success the caller passes the returned
    container_id to _write_done_marker AFTER all stage_report writes succeed.
    Returns the marker key (container id, or content-based fallback), or None.

    Two-tier key selection:
      1. Scraper/collector container id from captures/ — the paid-work identity.
      2. sha256 of classified.json (Fix B) — when captures are absent (deleted,
         pre-client paths). With Fix A (idempotent apply_diff) the data is already
         safe; this restores the loud refusal UX for subsequent re-runs.

    captures/ and done-markers live in dest (durable); classified.json lives in
    workdir (ephemeral). When dest is None (backward compat), dest == workdir."""
    if dest is None:
        dest = workdir
    marker_key = _read_scraper_container_id(dest)
    if not marker_key:
        # Fix B: content-based fallback when captures are absent.
        # classified.json is in work (ephemeral), not dest.
        marker_key = _content_based_marker_key(workdir)
    if not marker_key:
        return None  # no stable key at all: guard skips (Fix A still protects data)
    marker = _done_marker_path(dest, marker_key)
    if marker.exists():
        print(
            f"REFUSED: this scrape ({marker_key}) has already updated the master; "
            f"refusing to append again. Run a new scrape to get a new container ID.",
            flush=True)
        sys.exit(1)
    return marker_key


def _write_done_marker(workdir, container_id: "str | None") -> None:
    """Write the idempotency marker. Called LAST in stage_report, only on full
    success. A run that crashes before this call leaves no marker, so a legitimate
    retry is not refused."""
    if not container_id:
        return
    marker = _done_marker_path(workdir, container_id)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"container_id": container_id,
                                  "ts": round(time.time(), 1)}))


def stage_report(workdir, mode, taxonomy_path=None, subject_label=None, *, dest=None):
    """workdir = ephemeral work dir (classified, lost, status, summary, run_complete,
    master pre-write backup); dest = durable subject dir (master.csv, Followers csv,
    notification files, captures, idempotency markers). When dest is None (backward
    compat / --workdir-only), dest == workdir and behaviour is unchanged."""
    if dest is None:
        dest = workdir
    # idempotency guard: refuse if this scrape container already updated the master.
    # Fires BEFORE any read or write so a refused run touches nothing.
    _report_container_id = _check_append_idempotency(workdir, dest=dest)
    classified = _read(workdir, "classified.json", [])
    lost = _read(workdir, "lost_followers.json", [])
    master = P.read_master(_master_path(dest).read_text())
    # Mass-loss circuit-breaker (money path). `master` here is the PRE-diff baseline (this
    # stage writes the updated master below), so we can measure what this run is about to do
    # before it does it. A collapsed collector pull marks almost every active follower as
    # lost; catch that here — before apply_diff writes master.csv and before notify fires a
    # (false) mass-loss report. Conditional on a non-empty active baseline so a cold-start
    # (empty master, everyone new, nobody lost) is never gated. `newly_lost` counts only the
    # active followers this run flips to lost (already-lost rows carry a lost_on and are
    # excluded), so an accumulated history of past losses does not inflate the ratio.
    active_before = sum(1 for r in master if P._is_active(r))
    newly_lost = sum(1 for r in lost if P._is_active(r))
    if active_before > 0 and newly_lost > MAX_LOST_FRACTION * active_before:
        raise PBStop(
            f"mass-loss guard: this run would mark {newly_lost}/{active_before} active "
            f"followers as lost ({newly_lost / active_before:.0%} > "
            f"{MAX_LOST_FRACTION:.0%}). Refusing to write master.csv or notify — a "
            f"collapsed collector pull looks exactly like this. Inspect "
            f"{workdir}/current_followers.json and {workdir}/new_followers.json before "
            f"committing this diff; the enrichment already fetched is under {workdir}/.")
    # lost_on is the date a follower was first seen to have unfollowed. It MUST be the
    # actual run date — a hardcoded date would stamp every future run's lost followers
    # with a stale, wrong day. apply_diff stays pure (date injected, not read from a
    # clock inside it) so it remains deterministically testable; the clock lives here,
    # at the runner boundary. apply_diff is idempotent, so an already-marked row keeps
    # its original date.
    from datetime import date
    run_date = date.today().isoformat()
    # scrapeRound (master column B): the run's date in n8n's Hungarian day-first format
    # `dd.MM.yyyy.` (verified against the workflow's "Save Classifications" node and the
    # historical master, whose dominant value is e.g. "07.10.2025."). The port had left it
    # BLANK on every appended follower — stamp this run's new followers here so column B is
    # populated in both the master and the Followers-{date} export (apply_diff copies these
    # records, so the stamp carries into the master's appended rows too).
    scrape_round = date.today().strftime("%d.%m.%Y.")
    for rec in classified:
        rec["scrapeRound"] = scrape_round
    final = P.apply_diff(master, classified, lost, lost_on=run_date)
    # pre-write backup: copy current dest master to temp work dir BEFORE overwriting.
    # A crash during the atomic write leaves the durable master intact (the temp file is
    # discarded); the backup at this deterministic path also allows manual restore.
    _backup_master(dest, workdir)
    # atomic master write: write to a temp file in dest, then os.replace.
    # The durable master is never left in a partial/corrupt state.
    _atomic_write_master(dest, P.write_master(final))
    # Per-run new-followers export, matching the n8n "Create New Tab" convention: n8n
    # creates a sheet titled "Followers - {yyyy-MM-dd}" holding exactly this run's new
    # followers, enriched then classified, before appending them to Master. `classified`
    # IS that set (same MASTER_COLUMNS as the master), so write it out under the same name.
    # A standing deliverable, not a debug artifact — the enriched+labelled new followers on
    # their own, without diffing the 4k-row master. Date is the report/finalize day (same as
    # lost_on); for a normal same-day run this equals n8n's tab-creation $now.
    # Followers csv is DURABLE — written to dest.
    followers_name = f"Followers - {run_date}.csv"
    Path(dest, followers_name).write_text(P.write_master(classified))
    summary = P.collect_summary(final, classified, lost_count=len(lost),
                                taxonomy=load_taxonomy(taxonomy_path))
    # subject_label (from FOLLOWER_CONFIG.json) is added to the summary so the notification
    # message can prefix it (e.g. "ACME CEO — LinkedIn followers: ..."). When absent the
    # generic "LinkedIn followers" prefix is used — backward-compatible with no config.
    if subject_label:
        summary["subject_label"] = subject_label
    # summary.json is ephemeral — written to work. Notification files are DURABLE — to dest.
    _write(workdir, "summary.json", summary)
    text = notify.deliver(summary, Path(dest))
    print(f"  master.csv now {len(final)} rows ({sum(1 for r in final if P._is_active(r))} active)")
    print(f"  wrote '{followers_name}' ({len(classified)} new followers, enriched + classified)")
    print("  --- notification ---")
    for line in text.splitlines():
        print(f"    {line}")
    # The Claude app notification itself is sent by the CALLER — the light skill, or the
    # orchestrating Claude session that launched this run as a background task — because a
    # subprocess can't call a Claude tool (see notify.py). Surface the exact one-liner to
    # push here (it also sits in notification.json['message']) so the waking deliverer
    # sends it verbatim without re-deriving it.
    print(f"  NOTIFY (send as Claude app notification): "
          f"{notify.build_notification_message(summary)}")
    # idempotency marker: written LAST, only on full success. A crash anywhere above
    # (master write, Followers csv write, notify) leaves no marker, so a legitimate retry
    # is not refused. The ordering guarantee: this line is the final side-effect before
    # return; all prior writes have already succeeded or raised.
    # marker is DURABLE — written to dest (inside dest/.linkedin-follower-tracker/runs/).
    _write_done_marker(dest, _report_container_id)
    return summary


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="linkedin-follower-tracker runner")
    ap.add_argument("--backend", choices=["fake", "http", "real"], default="fake")
    ap.add_argument("--mode", choices=["sample", "scale"], default="sample")
    ap.add_argument("--workdir", default="./_run")
    ap.add_argument("--stage", choices=["collect", "diff", "enrich", "classify", "report", "all"],
                    default="all")
    ap.add_argument("--running-polls", type=int, default=1)
    ap.add_argument("--classifier", choices=["stub", "claude"], default="stub",
                    help="stub = offline deterministic; claude = real Haiku subagent")
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--collector-runtime", type=float, default=0.0,
                    help="seconds the synthetic collector phantom takes (models ~1h, compressed)")
    ap.add_argument("--scraper-runtime", type=float, default=0.0,
                    help="seconds the synthetic scraper phantom takes (models ~40-45m, compressed)")
    ap.add_argument("--poll-interval", type=float, default=0.05,
                    help="seconds between status checks while waiting for a phantom")
    ap.add_argument("--cold-start", action="store_true",
                    help="first-ever real run with NO prior baseline: start from an "
                         "empty master and establish it. Treats every current follower "
                         "as new — a full first-run enrichment + classification at real "
                         "cost. Ignored if a baseline already exists; irrelevant to the "
                         "synthetic backends.")
    ap.add_argument("--stop-before-scrape", action="store_true",
                    help="enrich stage only: save the leads, create + VERIFY the "
                         "org-storage list, then STOP before the paid scraper launch. "
                         "The list is left active; resume with --resume-scrape.")
    ap.add_argument("--resume-scrape", action="store_true",
                    help="enrich stage only: skip prepare and run ONLY the paid scraper "
                         "launch against the list prepared earlier "
                         "(workdir/enrich_prepared.json).")
    ap.add_argument("--config", default=None,
                    help="path to FOLLOWER_CONFIG.json for this subject. Required for "
                         "--backend real. Optional for fake/http: when given, csv_name, "
                         "enrich_list_name, and subject_label are read from it. "
                         "Default: <workdir>/.linkedin-follower-tracker/config.json "
                         "(used automatically when the file exists).")
    ap.add_argument("--taxonomy", default=None,
                    help="path to taxonomy JSON for the classifier. Default: the shipped "
                         "config/taxonomy.example.json (used automatically when no "
                         "taxonomy.json exists in the config directory).")
    # per-subject durable directory + intake + seed mode
    ap.add_argument("--subject-dir", default=None,
                    help="per-subject durable directory (contains .linkedin-follower-tracker/ "
                         "with config.json + taxonomy.json, plus master.csv and run outputs). "
                         "When given, takes precedence over --workdir for all path resolution.")
    ap.add_argument("--parent", default=None,
                    help="parent directory; combined with --subject to compute --subject-dir "
                         "as <parent>/linkedin-follower-tracker/<slug>/.")
    ap.add_argument("--subject", default=None,
                    help="subject label (e.g. 'ACME CEO'); combined with --parent to compute "
                         "the per-subject directory slug.")
    ap.add_argument("--init", action="store_true",
                    help="seed mode: scaffold the per-subject dir, copy example configs if "
                         "absent, then collect current followers and write master.csv. Stops "
                         "after seeding — does NOT run diff, enrich, classify, report, or "
                         "push. Re-run without --init for all subsequent tracking runs.")
    args = ap.parse_args()

    # The prepare/scrape seam is meaningful only for the enrich stage, and the two flags
    # are opposite halves of it — never both at once.
    if (args.stop_before_scrape or args.resume_scrape) and args.stage != "enrich":
        sys.exit("--stop-before-scrape / --resume-scrape apply only to --stage enrich")
    if args.stop_before_scrape and args.resume_scrape:
        sys.exit("--stop-before-scrape and --resume-scrape are mutually exclusive")

    # resolve effective subject dir (from --subject-dir or --parent+--subject).
    # When set, it overrides args.workdir for all subsequent path resolution so the rest of
    # the function works unchanged — every reference to args.workdir reaches the subject dir.
    effective_subject_dir: Path | None = None
    if args.subject_dir:
        effective_subject_dir = Path(args.subject_dir).resolve()
    elif args.parent and args.subject:
        effective_subject_dir = subject_dir_for(args.parent, args.subject).resolve()
    elif args.parent or args.subject:
        sys.exit("--parent and --subject must be used together to compute the subject dir path")

    if effective_subject_dir is not None:
        # Override workdir so config lookups and guard checks resolve into the subject dir.
        args.workdir = str(effective_subject_dir)

    # compute the two roots: dest (durable subject dir) and work (ephemeral temp).
    # In --workdir-only mode, work == dest == args.workdir — fully unchanged behaviour.
    # In --subject-dir mode, work is a deterministic temp dir; dest is the subject dir.
    if effective_subject_dir is not None:
        _work_dir = _compute_work_dir(effective_subject_dir)
        _work_dir.mkdir(parents=True, exist_ok=True)
    else:
        _work_dir = Path(args.workdir)
    _work = str(_work_dir)   # ephemeral: status, staging JSONs, backup, run_complete
    _dest = args.workdir     # durable: master.csv, Followers csv, captures, notifications

    # --init (seed mode) — scaffold dir, copy example configs, collect baseline, STOP.
    # Must run BEFORE init_workdir (which would refuse on a missing master in subject-dir mode).
    if args.init:
        if effective_subject_dir is None:
            sys.exit("--init requires --subject-dir, or --parent + --subject "
                     "(to compute the per-subject directory path)")
        print(f"[init] seed mode — subject dir: {effective_subject_dir}")
        _init_subject_scaffold(effective_subject_dir)

        hidden_cfg = effective_subject_dir / ".linkedin-follower-tracker" / "config.json"
        _seed_cfg: dict = {}
        if hidden_cfg.exists():
            _seed_cfg = json.loads(hidden_cfg.read_text(encoding="utf-8"))

        if not _config_is_filled(_seed_cfg):
            print(f"\n  Config placeholder detected — fill in your PhantomBuster agent IDs:")
            print(f"    {hidden_cfg}")
            print("  Then re-run with --init to seed the baseline.")
            return

        # Config is filled: collect followers and write master.csv baseline.
        from datetime import datetime as _dt2
        _now2 = _dt2.now()
        _seed_csv = _stamp_name(_seed_cfg.get("csv_name", "") or "followers - {date}", _now2)
        _seed_enrich = _stamp_name(
            _seed_cfg.get("enrich_list_name", "") or
            "linkedin-follower-tracker enrichment {date} {time}", _now2)
        _seed_client, _seed_coll_id, _seed_scraper_id, _seed_cookie, _seed_cleanup = \
            make_backend(args.backend, args.mode, args.running_polls,
                         collector_runtime=args.collector_runtime,
                         scraper_runtime=args.scraper_runtime,
                         poll_interval=args.poll_interval,
                         on_poll=make_heartbeat(_work),   # status.json → ephemeral work
                         on_capture=make_capture_sink(_dest),  # captures → durable dest
                         csv_name=_seed_csv, enrich_list_name=_seed_enrich,
                         config_path=str(hidden_cfg))
        try:
            stage_seed(effective_subject_dir, _seed_client, _seed_coll_id, _seed_cookie)
        finally:
            _seed_cleanup()
        print("SEEDED -- master.csv established. Run without --init for subsequent tracking.")
        return

    # Resolve the config path: use --config if given, else try the conventional location
    # inside the workdir. Reading per-subject values early (before init_workdir) so they
    # are available for all backends, not just real.
    config_path = args.config
    if not config_path:
        default_cfg = Path(args.workdir) / ".linkedin-follower-tracker" / "config.json"
        if default_cfg.exists():
            config_path = str(default_cfg)

    # Read per-subject values for non-real backends (csv_name, enrich_list_name,
    # subject_label). For the real backend these come via get_pb_config() inside
    # make_backend; here we read them only for the fake/http paths so the config file
    # is honoured even when no API key is needed.
    _fcfg: dict = {}
    if config_path and Path(config_path).exists():
        _fcfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    from datetime import datetime as _dt
    _now = _dt.now()
    _csv_name = _stamp_name(_fcfg.get("csv_name", "") or "followers - {date}", _now)
    _enrich_list_name = _stamp_name(
        _fcfg.get("enrich_list_name", "") or
        "linkedin-follower-tracker enrichment {date} {time}", _now)
    _subject_label = str(_fcfg.get("subject_label", "")).strip() or None

    # when subject-dir is active, resolve taxonomy from the hidden state dir if the
    # user hasn't supplied one explicitly on the command line.
    if effective_subject_dir and not args.taxonomy:
        sd_tax = effective_subject_dir / ".linkedin-follower-tracker" / "taxonomy.json"
        if sd_tax.exists():
            args.taxonomy = str(sd_tax)

    # subject-dir mode requires an existing master.csv — it must have been seeded with
    # --init first.  We never auto-populate a real tracked-subject dir with synthetic data
    # (that would silently corrupt the baseline the first normal run diffs against).
    # This check fires before init_workdir, which for fake/http backends would otherwise
    # silently seed scenario data into the subject dir.
    if effective_subject_dir:
        Path(args.workdir).mkdir(parents=True, exist_ok=True)
        if not _master_path(args.workdir).exists():
            raise SystemExit(
                f"REFUSED: no master.csv baseline in {args.workdir}.\n"
                f"Seed the baseline first with:\n"
                f"    python3 run_pipeline.py --init --subject-dir {args.workdir} "
                f"--backend {args.backend}")
        # master.csv exists — init_workdir will return immediately (existing-baseline branch).

    # pass dest so init_workdir seeds/finds master.csv in the durable dir, and creates
    # the ephemeral work dir. In --workdir-only mode, _work == _dest so this is a no-op.
    init_workdir(_work, args.mode, dest=_dest, backend=args.backend,
                 cold_start=args.cold_start)
    write_status(_work, state="starting", stage=args.stage, backend=args.backend)

    needs_client = args.stage in ("collect", "enrich", "all")
    client = collector_id = scraper_id = cookie = None
    cleanup = lambda: None
    if needs_client:
        client, collector_id, scraper_id, cookie, cleanup = make_backend(
            args.backend, args.mode, args.running_polls,
            collector_runtime=args.collector_runtime,
            scraper_runtime=args.scraper_runtime,
            poll_interval=args.poll_interval,
            on_poll=make_heartbeat(_work),          # status.json → ephemeral work
            on_capture=make_capture_sink(_dest),    # captures → durable dest
            csv_name=_csv_name, enrich_list_name=_enrich_list_name,
            config_path=config_path)

    try:
        print(f"Running stage='{args.stage}' backend='{args.backend}' mode='{args.mode}'",
              flush=True)
        summary = None
        if args.stage in ("collect", "all"):
            print("[collect] launching phantom, then staying alive until it finishes...",
                  flush=True)
            stage_collect(_work, client, collector_id, cookie)  # → ephemeral work
        if args.stage in ("diff", "all"):
            print("[diff]", flush=True)
            stage_diff(_work, dest=_dest)  # master read from dest, outputs to work
        if args.stage in ("enrich", "all"):
            if args.stop_before_scrape:
                print("[enrich] preparing org-storage list, will PAUSE before the paid "
                      "scrape...", flush=True)
            elif args.resume_scrape:
                print("[enrich] resuming — launching scraper against the prepared list...",
                      flush=True)
            else:
                print("[enrich] launching scraper phantom, staying alive until it "
                      "finishes...", flush=True)
            stage_enrich(_work, client, scraper_id, cookie,  # → ephemeral work
                         stop_before_scrape=args.stop_before_scrape,
                         resume_scrape=args.resume_scrape)
        if args.stage in ("classify", "all"):
            print("[classify]", flush=True)
            stage_classify(_work, args.classifier, args.model,  # → ephemeral work
                           taxonomy_path=args.taxonomy)
        if args.stage in ("report", "all"):
            print("[report]", flush=True)
            summary = stage_report(_work, args.mode,  # ephemerals in work, durables to dest
                                   taxonomy_path=args.taxonomy,
                                   subject_label=_subject_label,
                                   dest=_dest)
        # Wake signal: durable marker + a clear line. In Claude Code the background-task
        # completion notification wakes the conversation; the marker lets a scheduled
        # re-check learn the outcome without having watched the run.
        # run_complete.json is ephemeral — written to work.
        _write(_work, "run_complete.json",
               {"done": True, "ts": round(time.time(), 1), "summary": summary})
        write_status(_work, state="done", summary=summary)  # status.json → ephemeral work
        print("DONE -- WAKE: run complete, orchestrator may resume "
              "(run_complete.json written)", flush=True)
    except PBStop as e:
        # Money-path rule: stop and report, never auto-retry. Any paid handle/result
        # captured before the failure is on disk under captures/ — name them in the STOP
        # record so the operator knows exactly what is recoverable by hand.
        # captures are DURABLE — look in _dest, not _work.
        cap_dir = Path(_dest, "captures")
        captured = sorted(p.name for p in cap_dir.glob("*.json")) if cap_dir.is_dir() else []
        write_status(_work, state="stopped", error=str(e), captured=captured)
        _write(_work, "run_complete.json",
               {"done": True, "stopped": True, "error": str(e),
                "captured": captured, "ts": round(time.time(), 1)})
        print(f"STOPPED (money-path rule): {e} -- WAKE: run halted", flush=True)
        if captured:
            print(f"  salvage: captures/ holds {', '.join(captured)} — paid handles/"
                  f"results to recover from the PhantomBuster console (do NOT re-launch)",
                  flush=True)
        sys.exit(2)
    finally:
        cleanup()


if __name__ == "__main__":
    main()
