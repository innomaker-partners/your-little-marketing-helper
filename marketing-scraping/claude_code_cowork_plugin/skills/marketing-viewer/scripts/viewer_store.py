#!/usr/bin/env python3
"""
viewer_store.py -- the ONE module that writes the local app's own files.

Two writers exist: the app (through viewer_server.py) and the agent (through viewer_ctl.py).
Both go through these functions, so every write gets the same checks, the same per-run lock
and the same atomic rename. Nothing here ever touches scraped data, an index or a report:
the only files written are

    <run>/.viewer/annotations.json    favorites, notes, trashed (per record uid)
    <run>/.viewer/views.json          saved views (named sets of record uids)
    <root>/.viewer/current_view.json  what the user has open right now (written by the server)
    <root>/.viewer/server.json        where the running app lives (port, pid)

Why a lock AND an atomic rename: the rename means a reader never sees a half-written file;
the lock means two writers (a click in the app and an agent command in the same second)
cannot both read the old file and each write back a version missing the other's change.

Stdlib only. Self-test:  python3 viewer_store.py   (temp folder, no network)
"""

import json
import os
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

VIEWER_DIR = ".viewer"
MARK_KINDS = ("favorites", "notes", "trashed")
AGENT_MARK_KINDS = ("favorites", "notes")   # the agent never hides a record from the user
MAX_NOTE = 4000
MAX_TITLE = 120
MAX_VIEW_NOTE = 2000
MAX_VIEW_UIDS = 5000
MAX_VIEWS = 200
MAX_CURRENT_VIEW_BYTES = 256 * 1024
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")   # control chars except \t \n \r
_tmp_counter = 0
_tmp_counter_lock = threading.Lock()


class StoreError(ValueError):
    """A write was refused. The message is written for the person reading it."""


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return default


def write_json_atomic(path, obj):
    """Write to a unique temp file in the same folder, then rename over the target."""
    global _tmp_counter
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _tmp_counter_lock:
        _tmp_counter += 1
        n = _tmp_counter
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{n}.tmp")
    try:
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


@contextmanager
def locked(folder):
    """Exclusive lock on <folder>/.lock for the duration of a read-modify-write."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    fh = open(folder / ".lock", "a+")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def stamp(path):
    """A change marker for a file: its mtime in ns, or 0 when absent."""
    try:
        return Path(path).stat().st_mtime_ns
    except OSError:
        return 0


def _clean_text(s, limit, what):
    if not isinstance(s, str):
        raise StoreError(f"{what} must be text")
    s = _CTRL.sub("", s).strip()
    if len(s) > limit:
        raise StoreError(f"{what} is {len(s)} characters; the limit is {limit}")
    return s


# ---- the run's records -------------------------------------------------------
def index_uids(run_dir):
    idx = read_json(Path(run_dir) / "frontend_index.json", None)
    if not isinstance(idx, dict):
        raise StoreError(f"{Path(run_dir).name} has no readable frontend_index.json; "
                         f"re-index it first (free)")
    return [r.get("uid") for r in (idx.get("records") or []) if isinstance(r, dict)]


# ---- annotations -------------------------------------------------------------
def normalize_annotations(a):
    a = a if isinstance(a, dict) else {}
    return {k: (a.get(k) if isinstance(a.get(k), dict) else {}) for k in MARK_KINDS}


def load_annotations(run_dir):
    return normalize_annotations(read_json(Path(run_dir) / VIEWER_DIR / "annotations.json", {}))


def set_mark(run_dir, kind, uid, value, allowed_kinds=MARK_KINDS):
    """Set or clear ONE mark on ONE record. value None / False / "" clears it.
    Setting needs a uid that exists in the run's index; clearing accepts any uid already
    marked (so a mark left behind on a record that is gone can still be removed)."""
    if kind not in allowed_kinds:
        raise StoreError(f"unknown mark '{kind}'; allowed: {', '.join(allowed_kinds)}")
    if not isinstance(uid, str) or not uid:
        raise StoreError("uid must be a non-empty string")
    if kind == "notes" and value is not None:
        value = _clean_text(value, MAX_NOTE, "note")
    elif value not in (None, True, False):
        raise StoreError(f"{kind} takes true or false")
    clearing = value in (None, False, "")
    run_dir = Path(run_dir)
    vdir = run_dir / VIEWER_DIR
    if not clearing and uid not in set(index_uids(run_dir)):
        raise StoreError(f"no record with uid '{uid}' in {run_dir.name}")
    with locked(vdir):
        ann = load_annotations(run_dir)
        raw = read_json(vdir / "annotations.json", {})
        extra = {k: v for k, v in raw.items() if k not in MARK_KINDS} if isinstance(raw, dict) else {}
        if clearing:
            ann[kind].pop(uid, None)
        else:
            ann[kind][uid] = value
        write_json_atomic(vdir / "annotations.json", {**extra, **ann})
    return ann


# ---- saved views -------------------------------------------------------------
def load_views(run_dir):
    v = read_json(Path(run_dir) / VIEWER_DIR / "views.json", {})
    views = v.get("views") if isinstance(v, dict) else None
    return [x for x in (views or []) if isinstance(x, dict) and x.get("id")]


def _slug(title):
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return (s[:40].strip("-") or "view")


def save_view(run_dir, title, uids, note="", created_by="agent", view_id=None):
    """Create a saved view, or replace the one with view_id. Returns the stored view.
    Every uid must exist in the run's index: a view that silently shows fewer records
    than the agent told the user it would is the failure this refuses."""
    title = _clean_text(title, MAX_TITLE, "title")
    if not title:
        raise StoreError("a view needs a title")
    note = _clean_text(note or "", MAX_VIEW_NOTE, "note")
    created_by = "user" if created_by == "user" else "agent"
    if not isinstance(uids, list) or not all(isinstance(u, str) for u in uids):
        raise StoreError("uids must be a list of record uids")
    uids = list(dict.fromkeys(u for u in uids if u))
    if not uids:
        raise StoreError("a view needs at least one record")
    if len(uids) > MAX_VIEW_UIDS:
        raise StoreError(f"{len(uids)} records; a view holds at most {MAX_VIEW_UIDS}")
    run_dir = Path(run_dir)
    known = set(index_uids(run_dir))
    unknown = [u for u in uids if u not in known]
    if unknown:
        raise StoreError(f"{len(unknown)} uid(s) not in {run_dir.name}, e.g. "
                         + ", ".join(unknown[:5]))
    vdir = run_dir / VIEWER_DIR
    with locked(vdir):
        views = load_views(run_dir)
        ids = {v["id"] for v in views}
        now = now_iso()
        if view_id is not None:
            if view_id not in ids:
                raise StoreError(f"no saved view with id '{view_id}'")
            old = next(v for v in views if v["id"] == view_id)
            view = {**old, "title": title, "note": note, "uids": uids, "updated_at": now}
            views = [view if v["id"] == view_id else v for v in views]
        else:
            if len(views) >= MAX_VIEWS:
                raise StoreError(f"this run already has {MAX_VIEWS} saved views; delete some first")
            base = _slug(title)
            vid, n = base, 2
            while vid in ids:
                vid, n = f"{base}-{n}", n + 1
            view = {"id": vid, "title": title, "note": note, "uids": uids,
                    "created_by": created_by, "created_at": now, "updated_at": now}
            views.append(view)
        write_json_atomic(vdir / "views.json", {"views": views})
    return view


def remap_uid_marks(run_dir, uid_map, new_index_uids=None):
    """Rewrite annotations.json and views.json to apply a uid rename map.

    uid_map: {old_uid: new_uid}.  Only uids that differ need to be in the map.
    Uids not present in uid_map are left unchanged.  Orphaned marks (whose uid
    is not in the new index after remapping) are KEPT, never deleted.

    new_index_uids: optional set/frozenset of uids present in the new index.
    When supplied, orphaned_marks and orphaned_uids are computed.  When absent,
    orphaned_marks is 0 and orphaned_uids is [].

    Both files are rewritten in ONE locked operation so a concurrent reader
    never sees annotations updated but views stale.

    Returns {
        "marks_moved": N,          -- mark entries moved to a new uid
        "view_entries_moved": N,   -- uid references in saved views updated
        "orphaned_marks": N,       -- marks/view-uids not in new_index_uids after remap
        "orphaned_uids": [...],    -- the orphaned uids (up to 20, sorted)
        "conflicts": N,            -- moves skipped because new uid already had a mark
    }.

    Conflict rule: when a moved mark's new uid already carries a mark of
    the same kind, the EXISTING mark is kept and the incoming move is dropped.
    The user's newer interaction on the native uid takes precedence.

    Dedup rule: after remapping, each saved view's uid list is deduplicated
    (first occurrence kept) so the view never lists the same record twice.

    If no annotations file exists and no views file exists, creates nothing.
    """
    run_dir = Path(run_dir)
    vdir = run_dir / VIEWER_DIR
    ann_path = vdir / "annotations.json"
    views_path = vdir / "views.json"

    _empty = {"marks_moved": 0, "view_entries_moved": 0,
              "orphaned_marks": 0, "orphaned_uids": [], "conflicts": 0}

    # Skip entirely when nothing exists -- do not create empty files.
    if not ann_path.exists() and not views_path.exists():
        return _empty

    # Map only the entries that actually change.
    changes = {old: new for old, new in uid_map.items() if old != new}
    if not changes:
        # No remaps to apply.  If new_index_uids was supplied, compute orphans
        # read-only (do not write files) so cmd_reindex can report them even
        # when the entire uid_map is empty (e.g. all positional records failed
        # the content check and none of their raw reviewIds were available).
        if new_index_uids is None:
            return _empty
        orphaned_set = set()
        existing_ann = normalize_annotations(
            read_json(ann_path, {}) if ann_path.exists() else {})
        for kind in MARK_KINDS:
            for uid in existing_ann[kind]:
                if uid not in new_index_uids:
                    orphaned_set.add(uid)
        raw_views = read_json(views_path, {}) if views_path.exists() else {}
        existing_views = (raw_views.get("views")
                          if isinstance(raw_views, dict) else None) or []
        for v in existing_views:
            if isinstance(v, dict):
                for uid in v.get("uids") or []:
                    if uid not in new_index_uids:
                        orphaned_set.add(uid)
        return {**_empty, "orphaned_marks": len(orphaned_set),
                "orphaned_uids": sorted(orphaned_set)[:20]}

    with locked(vdir):
        marks_moved = 0
        view_entries_moved = 0
        conflicts = 0

        # -- annotations --
        ann_updated = None
        new_ann = {k: {} for k in MARK_KINDS}
        if ann_path.exists():
            raw_ann = read_json(ann_path, {})
            ann = normalize_annotations(raw_ann)
            extra = ({k: v for k, v in raw_ann.items() if k not in MARK_KINDS}
                     if isinstance(raw_ann, dict) else {})
            for kind in MARK_KINDS:
                # First pass: place marks that are NOT being remapped.  This
                # must precede the second pass so conflict detection sees
                # existing marks regardless of dict iteration order.
                for uid, val in ann[kind].items():
                    if uid not in changes:
                        new_ann[kind][uid] = val
                # Second pass: apply remaps.
                for uid, val in ann[kind].items():
                    if uid in changes:
                        new_uid = changes[uid]
                        # If new_uid already carries a mark of this kind (the user
                        # acted on the native uid directly), keep the existing value.
                        if new_uid in new_ann[kind]:
                            conflicts += 1
                        else:
                            new_ann[kind][new_uid] = val
                            marks_moved += 1
            ann_updated = {**extra, **new_ann}

        # -- views --
        views_updated = None
        new_views_list = []
        if views_path.exists():
            raw_views = read_json(views_path, {})
            views = raw_views.get("views") if isinstance(raw_views, dict) else None
            if isinstance(views, list):
                for v in views:
                    if not isinstance(v, dict):
                        new_views_list.append(v)
                        continue
                    old_uids = v.get("uids") or []
                    # Remap, then deduplicate (a view must not list the same uid twice).
                    seen_uids = set()
                    new_uids = []
                    for uid in old_uids:
                        mapped = changes.get(uid, uid)
                        if mapped not in seen_uids:
                            seen_uids.add(mapped)
                            new_uids.append(mapped)
                        if uid in changes:
                            view_entries_moved += 1
                    new_views_list.append({**v, "uids": new_uids} if new_uids != old_uids else v)
                views_updated = {"views": new_views_list}

        # Orphaned = uids present in marks or views but absent from the new index.
        orphaned_set = set()
        if new_index_uids is not None:
            for kind in MARK_KINDS:
                for uid in new_ann[kind]:
                    if uid not in new_index_uids:
                        orphaned_set.add(uid)
            for v in new_views_list:
                if isinstance(v, dict):
                    for uid in v.get("uids") or []:
                        if uid not in new_index_uids:
                            orphaned_set.add(uid)

        # Atomic writes inside the lock.
        if ann_updated is not None:
            write_json_atomic(ann_path, ann_updated)
        if views_updated is not None:
            write_json_atomic(views_path, views_updated)

    orphaned_list = sorted(orphaned_set)[:20]
    return {
        "marks_moved": marks_moved,
        "view_entries_moved": view_entries_moved,
        "orphaned_marks": len(orphaned_set),
        "orphaned_uids": orphaned_list,
        "conflicts": conflicts,
    }


def delete_view(run_dir, view_id):
    run_dir = Path(run_dir)
    vdir = run_dir / VIEWER_DIR
    with locked(vdir):
        views = load_views(run_dir)
        if view_id not in {v["id"] for v in views}:
            raise StoreError(f"no saved view with id '{view_id}'")
        write_json_atomic(vdir / "views.json", {"views": [v for v in views if v["id"] != view_id]})


# ---- what the user has open (root level; written by the server only) -------------
def write_current_view(root, payload):
    if not isinstance(payload, dict):
        raise StoreError("current view must be a JSON object")
    body = {**payload, "received_at": now_iso()}
    if len(json.dumps(body, ensure_ascii=False)) > MAX_CURRENT_VIEW_BYTES:
        raise StoreError("current view report too large")
    write_json_atomic(Path(root) / VIEWER_DIR / "current_view.json", body)


def read_current_view(root):
    return read_json(Path(root) / VIEWER_DIR / "current_view.json", None)


# ---- where the running app lives -------------------------------------------------
def write_server_info(root, info):
    write_json_atomic(Path(root) / VIEWER_DIR / "server.json", info)


def read_server_info(root):
    return read_json(Path(root) / VIEWER_DIR / "server.json", None)


def clear_server_info(root, pid):
    """Remove server.json only if it still describes this process (a newer server that
    reused the folder keeps its own file)."""
    p = Path(root) / VIEWER_DIR / "server.json"
    info = read_json(p, None)
    if isinstance(info, dict) and info.get("pid") == pid:
        try:
            p.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Carry marks forward once per run chain
# ---------------------------------------------------------------------------

# Config file names (same as in viewer_server.py -- kept in sync by the drift test).
_CARRY_CONFIG_NAMES = ("PAIN_CONFIG.json", "AD_INTEL_CONFIG.json", "SEARCH_CONFIG.json")

# Lazy handle for identity.py (same directory; loaded once).
_identity_mod = None


def _load_identity_module():
    """Load identity.py from the same directory as this file. Returns None on failure."""
    here = Path(__file__).resolve().parent
    import importlib.util
    spec = importlib.util.spec_from_file_location("identity", here / "identity.py")
    if spec is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return None
    return mod


def _get_identity():
    global _identity_mod
    if _identity_mod is None:
        _identity_mod = _load_identity_module()
    return _identity_mod


def _read_repeat_of(run_dir):
    """Return the repeat_of value from the run's index (authoritative) or config (fallback).
    Returns None when neither source carries one."""
    run_dir = Path(run_dir)
    idx = read_json(run_dir / "frontend_index.json", {})
    if isinstance(idx, dict) and idx.get("repeat_of"):
        return str(idx["repeat_of"])
    for name in _CARRY_CONFIG_NAMES:
        p = run_dir / name
        if p.exists():
            cfg = read_json(p, {})
            if isinstance(cfg, dict) and cfg.get("repeat_of"):
                return str(cfg["repeat_of"])
    return None


def _resolve_run_dir(root, run_id):
    """Resolve run_id (a folder name) to an absolute path under root.
    Returns None if run_id is unsafe (empty, absolute, or path-traversal)."""
    if not run_id or not isinstance(run_id, str):
        return None
    rel = run_id.lstrip("/")
    if not rel:
        return None
    candidate = (Path(root).resolve() / rel).resolve()
    try:
        candidate.relative_to(Path(root).resolve())
    except ValueError:
        return None
    return candidate


def _build_identity_maps(run_dir, records):
    """Build (uid_to_identity, identity_to_uids) for a list of records.

    A uid whose record_identity() returns None is excluded from both maps.
    identity_to_uids maps each identity string to a list of uids (usually one;
    more than one means ambiguity in that run).
    """
    id_mod = _get_identity()
    uid_to_ident = {}
    ident_to_uids = {}
    if id_mod is None:
        return uid_to_ident, ident_to_uids
    for rec in (records or []):
        if not isinstance(rec, dict):
            continue
        uid = rec.get("uid")
        if not uid:
            continue
        ident = id_mod.record_identity(run_dir, rec)
        if ident is None:
            continue
        uid_to_ident[uid] = ident
        ident_to_uids.setdefault(ident, []).append(uid)
    return uid_to_ident, ident_to_uids


def read_carried_marker(run_dir):
    """Return the carried.json marker for run_dir, or None if not present."""
    return read_json(Path(run_dir) / VIEWER_DIR / "carried.json", None)


def _short_run_date(run_id):
    """Extract the date fragment (YYYY-MM-DD) from a run_id folder name, or return None."""
    m = re.search(r'_(\d{4}-\d{2}-\d{2})$', run_id or "")
    return m.group(1) if m else None


def _carry_error_plain(error_msg):
    """Convert a raw carry error string to a plain-English sentence for display.

    Keeps the raw message intact in the JSON; this is the human-readable companion.
    """
    if not error_msg:
        return error_msg
    if error_msg.startswith("repeat_of cycle:"):
        # e.g. "repeat_of cycle: pain_a_2026-09-01 -> pain_b_2026-10-01 -> pain_a_2026-09-01"
        cycle_path = error_msg[len("repeat_of cycle:"):].strip()
        nodes = [n.strip() for n in cycle_path.split("->")]
        labels = [_short_run_date(n) or n for n in nodes]
        path_str = ", ".join(labels)
        return (f"these runs repeat each other in a loop ({path_str}), "
                "so it is unclear which came first")
    if error_msg.startswith("previous run folder not found:"):
        run_id_part = error_msg[len("previous run folder not found:"):].strip()
        date = _short_run_date(run_id_part)
        label = f"the {date} run" if date else f"run “{run_id_part}”"
        return f"{label}'s folder is no longer there"
    if error_msg.startswith("previous run has a carry error:"):
        run_id_part = error_msg[len("previous run has a carry error:"):].strip()
        date = _short_run_date(run_id_part)
        label = f"the {date} run" if date else f"run “{run_id_part}”"
        return f"{label} has a loop in its history"
    return error_msg


def _record_text_snippet(rec, max_len=80):
    """Return a short human-readable text snippet from a record, for skip messages."""
    text = (rec.get("text") or rec.get("headline") or rec.get("body") or
            rec.get("title") or rec.get("keyword") or rec.get("name") or "")
    if not isinstance(text, str):
        text = str(text)
    text = text.strip()
    if len(text) > max_len:
        text = text[:max_len].rstrip() + "..."
    return text


def _run_date_label(idx, folder_name=None):
    """Return the human-readable date label for a run index.

    Returns e.g. "2026-09-01" or "2026-09-01 · v2", or None if no run_date.
    Indexes rarely carry run_date, so the folder name is parsed with the viewer's
    single authority (viewer_server._parse_run_folder), the same one runLabel shows.
    """
    if not isinstance(idx, dict):
        return None
    run_date = idx.get("run_date")
    ver = idx.get("run_version", 1)
    if not run_date and folder_name:
        try:
            import viewer_server as _vs  # lazy: viewer_server imports this module
            run_date, ver = _vs._parse_run_folder(folder_name)
        except Exception:
            run_date = None
    if not run_date:
        return None
    return f"{run_date} · v{ver}" if (isinstance(ver, int) and ver > 1) else str(run_date)


def _write_carry_error_marker(run_dir, repeat_of, error_msg):
    """Write a carried.json marker with an error so this run is not re-attempted."""
    vdir = Path(run_dir) / VIEWER_DIR
    with locked(vdir):
        # Re-check under lock.
        if (vdir / "carried.json").exists():
            return
        write_json_atomic(vdir / "carried.json", {
            "carried_from": repeat_of,
            "carried_at": now_iso(),
            "error": error_msg,
            "error_plain": _carry_error_plain(error_msg),
            "favorites_carried": 0, "notes_carried": 0, "trash_carried": 0,
            "views_carried": 0, "conflicts": 0, "skipped": [], "skipped_views": [],
        })


def _do_carry(root, run_dir, prev_dir, repeat_of):
    """Carry marks from prev_dir into run_dir under lock. Writes .viewer/carried.json.

    Returns the marker object.

    All writes (annotations.json, views.json, carried.json) happen inside ONE
    locked block so two concurrent readers carry exactly once.
    """
    run_dir = Path(run_dir)
    prev_dir = Path(prev_dir)
    vdir = run_dir / VIEWER_DIR

    with locked(vdir):
        # Re-check inside lock (another thread may have just carried).
        if (vdir / "carried.json").exists():
            return read_json(vdir / "carried.json", None)

        # Load previous run's records and annotations.
        prev_idx = read_json(prev_dir / "frontend_index.json", {}) or {}
        prev_records = prev_idx.get("records") or []
        prev_ann = normalize_annotations(read_json(prev_dir / VIEWER_DIR / "annotations.json", {}))
        prev_views = load_views(prev_dir)

        # Load new run's records and existing annotations.
        new_idx = read_json(run_dir / "frontend_index.json", {}) or {}
        new_records = new_idx.get("records") or []
        new_ann_raw = read_json(vdir / "annotations.json", {}) if (vdir / "annotations.json").exists() else {}
        new_ann = normalize_annotations(new_ann_raw)
        existing_extra = (
            {k: v for k, v in new_ann_raw.items() if k not in MARK_KINDS}
            if isinstance(new_ann_raw, dict) else {}
        )
        new_views = load_views(run_dir)
        new_uid_set = {r.get("uid") for r in new_records
                       if isinstance(r, dict) and r.get("uid")}

        # Build identity maps for both runs.
        prev_uid_to_ident, prev_ident_to_uids = _build_identity_maps(prev_dir, prev_records)
        new_uid_to_ident, new_ident_to_uids = _build_identity_maps(run_dir, new_records)

        # Unambiguous identity -> new_uid mapping.
        ident_to_new_uid = {}
        ambiguous_in_new = set()
        for ident, uids in new_ident_to_uids.items():
            if len(uids) == 1:
                ident_to_new_uid[ident] = uids[0]
            else:
                ambiguous_in_new.add(ident)

        ambiguous_in_prev = {ident for ident, uids in prev_ident_to_uids.items()
                             if len(uids) > 1}

        # ---- carry marks -------------------------------------------------------
        counts = {"favorites": 0, "notes": 0, "trashed": 0}
        conflicts = 0
        skipped = []
        merged_ann = {k: dict(new_ann[k]) for k in MARK_KINDS}

        # Text snippet lookup: prev_uid -> short human-readable text for skip messages.
        prev_uid_to_rec = {r.get("uid"): r for r in prev_records
                           if isinstance(r, dict) and r.get("uid")}

        for kind in MARK_KINDS:
            for prev_uid, value in prev_ann[kind].items():
                ident = prev_uid_to_ident.get(prev_uid)
                if ident is None:
                    skipped.append({"uid": prev_uid, "kind": kind,
                                    "text": _record_text_snippet(prev_uid_to_rec.get(prev_uid, {})),
                                    "reason": "no stable identity"})
                    continue
                if ident in ambiguous_in_prev:
                    skipped.append({"uid": prev_uid, "kind": kind,
                                    "text": _record_text_snippet(prev_uid_to_rec.get(prev_uid, {})),
                                    "reason": "identity appears more than once in the previous run"})
                    continue
                if ident in ambiguous_in_new:
                    skipped.append({"uid": prev_uid, "kind": kind,
                                    "text": _record_text_snippet(prev_uid_to_rec.get(prev_uid, {})),
                                    "reason": "identity appears more than once in this run"})
                    continue
                new_uid = ident_to_new_uid.get(ident)
                if new_uid is None:
                    skipped.append({"uid": prev_uid, "kind": kind,
                                    "text": _record_text_snippet(prev_uid_to_rec.get(prev_uid, {})),
                                    "reason": "record not in this run"})
                    continue
                # Existing mark in new run always wins.
                if new_uid in merged_ann[kind]:
                    conflicts += 1
                    skipped.append({"uid": prev_uid, "kind": kind, "mapped_to": new_uid,
                                    "text": _record_text_snippet(prev_uid_to_rec.get(prev_uid, {})),
                                    "reason": "existing mark in this run takes precedence"})
                    continue
                merged_ann[kind][new_uid] = value
                counts[kind] += 1

        # ---- carry views -------------------------------------------------------
        views_carried = 0
        skipped_views = []
        new_view_titles = {v["title"] for v in new_views}
        new_views_out = list(new_views)

        for view in prev_views:
            title = view.get("title", "")
            if title in new_view_titles:
                skipped_views.append({"view_id": view.get("id"), "title": title,
                                       "reason": "a view with this title already exists in this run"})
                continue

            old_uids = view.get("uids") or []
            mapped_uids = []
            unmapped_count = 0
            seen_mapped: set = set()
            for prev_uid in old_uids:
                ident = prev_uid_to_ident.get(prev_uid)
                if ident is None:
                    unmapped_count += 1
                    continue
                new_uid = ident_to_new_uid.get(ident)
                if new_uid is None or new_uid not in new_uid_set:
                    unmapped_count += 1
                    continue
                if new_uid not in seen_mapped:
                    seen_mapped.add(new_uid)
                    mapped_uids.append(new_uid)
                # else: duplicate after mapping -- skip silently

            if not mapped_uids:
                skipped_views.append({"view_id": view.get("id"), "title": title,
                                       "reason": "no records from this view are in this run"})
                continue

            # Build the note: original note plus carry provenance.
            note_parts = []
            if view.get("note"):
                note_parts.append(view["note"])
            # Use human-readable date label instead of raw run_id.
            _date_label = _run_date_label(prev_idx, prev_dir.name)
            carry_sentence = (f"Carried from the {_date_label} run."
                              if _date_label else f"Carried from {repeat_of}.")
            if unmapped_count:
                s = "s" if unmapped_count != 1 else ""
                carry_sentence += f" {unmapped_count} record{s} could not be carried."
            note_parts.append(carry_sentence)
            new_note = "\n".join(note_parts)
            if len(new_note) > MAX_VIEW_NOTE:
                new_note = new_note[:MAX_VIEW_NOTE]

            # Assign a unique view id (slug from title, then add suffix if taken).
            base = _slug(title)
            existing_ids = {v["id"] for v in new_views_out}
            vid, n = base, 2
            while vid in existing_ids:
                vid, n = f"{base}-{n}", n + 1

            new_view = {
                "id": vid,
                "title": title,
                "note": new_note,
                "uids": mapped_uids,
                "created_by": view.get("created_by", "user"),
                "created_at": view.get("created_at", now_iso()),
                "updated_at": now_iso(),
                "carried_from": repeat_of,
            }
            new_views_out.append(new_view)
            new_view_titles.add(title)
            views_carried += 1

        total_marks = sum(counts.values())

        # ---- write results -----------------------------------------------------
        if total_marks > 0:
            write_json_atomic(vdir / "annotations.json", {**existing_extra, **merged_ann})

        if views_carried > 0:
            write_json_atomic(vdir / "views.json", {"views": new_views_out})

        # Marker is always written (even when nothing carried) so this run is not re-attempted.
        marker_obj = {
            "carried_from": repeat_of,
            "carried_at": now_iso(),
            "favorites_carried": counts["favorites"],
            "notes_carried": counts["notes"],
            "trash_carried": counts["trashed"],
            "views_carried": views_carried,
            "conflicts": conflicts,
            "skipped": skipped,
            "skipped_views": skipped_views,
        }
        write_json_atomic(vdir / "carried.json", marker_obj)
        return marker_obj


def _ensure_carried_chain(root, run_dir, chain):
    """Recursively ensure run_dir has carried from its repeat_of chain.

    chain: list of resolved path strings of runs currently on the call stack,
           in order from outermost to innermost (cycle guard -- preserves path for
           error reporting).
    """
    key = str(run_dir.resolve())
    if key in chain:
        # Cycle detected.  Build a human-readable path from the first occurrence
        # of key in chain to the current re-entry, then write an error marker so
        # this run (and any callers) know the chain is broken.  We never infer
        # lineage, so stopping here is correct -- don't carry across a cycle edge.
        cycle_idx = chain.index(key)
        cycle_nodes = chain[cycle_idx:] + [key]
        cycle_str = " -> ".join(Path(k).name for k in cycle_nodes)
        _write_carry_error_marker(run_dir, "(cycle)",
                                  f"repeat_of cycle: {cycle_str}")
        return

    vdir = run_dir / VIEWER_DIR
    if (vdir / "carried.json").exists():
        return  # already done

    repeat_of = _read_repeat_of(run_dir)
    if not repeat_of:
        return  # chain ends

    prev_dir = _resolve_run_dir(root, repeat_of)
    if prev_dir is None or not prev_dir.is_dir():
        # Write a marker so this run is not re-attempted.
        _write_carry_error_marker(run_dir, repeat_of,
                                  f"previous run folder not found: {repeat_of}")
        return

    # Recurse into the previous run first (transitive carry).
    _ensure_carried_chain(root, prev_dir, chain + [key])

    # Propagate ONLY repeat_of-cycle errors upward.  A missing-folder error in
    # prev_dir means prev_dir could not RECEIVE from ITS predecessor -- it says
    # nothing about whether prev_dir's own marks can be given to run_dir.
    # "a missing folder stops the chain cleanly" means the chain stops where
    # the folder is absent, not that every downstream run also fails.
    # A cycle error is different: the lineage itself is untrustworthy, so we
    # stop rather than carry marks across a corrupted chain.
    prev_marker = read_carried_marker(prev_dir)
    if prev_marker is not None and prev_marker.get("error"):
        err = prev_marker["error"]
        if err.startswith("repeat_of cycle:") or err.startswith("previous run has a carry error"):
            _write_carry_error_marker(run_dir, repeat_of,
                                      f"previous run has a carry error: {prev_dir.name}")
            return
        # Missing-folder or other non-cycle error: prev_dir still has its own
        # marks.  Fall through and carry normally.

    # Now carry from prev into run.
    _do_carry(root, run_dir, prev_dir, repeat_of)


def ensure_carried(root, run_dir):
    """Carry marks forward from the previous run, once.

    This is a no-op for runs without repeat_of: it creates NO files in that case.
    Idempotent: if .viewer/carried.json already exists, returns its contents.
    Safe for concurrent callers: the double-checked lock ensures exactly one carry.
    Carrying is transitive: before carrying into this run, ensures the previous run
    has carried from its own predecessor.

    Returns the carried.json marker if carries happened (or a marker was written
    earlier), or None when this run has no repeat_of.
    """
    run_dir = Path(run_dir).resolve()
    root = Path(root).resolve()
    vdir = run_dir / VIEWER_DIR

    # Fast path: marker exists (most common case after the first read).
    if (vdir / "carried.json").exists():
        return read_json(vdir / "carried.json", None)

    # No repeat_of -> no-op; no files created.
    if not _read_repeat_of(run_dir):
        return None

    # Run the carry chain (includes cycle guard and transitive carry).
    _ensure_carried_chain(root, run_dir, [])

    return read_json(vdir / "carried.json", None)


# ---------------------------------------------------------------------------
# Inline self-tests -- a temp folder, no network.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    import tempfile
    PASS = FAIL = 0

    def check(label, got, expected):
        global PASS, FAIL
        if got == expected:
            print(f"  PASS  {label}"); PASS += 1
        else:
            print(f"  FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")
            FAIL += 1

    def raises(label, fn, needle):
        global PASS, FAIL
        try:
            fn()
        except StoreError as e:
            if needle in str(e):
                print(f"  PASS  {label}"); PASS += 1
            else:
                print(f"  FAIL  {label}: wrong message {e}"); FAIL += 1
            return
        print(f"  FAIL  {label}: did not raise"); FAIL += 1

    with tempfile.TemporaryDirectory() as td:
        run = Path(td) / "pain_acme_2026-09-01"
        run.mkdir()
        (run / "frontend_index.json").write_text(json.dumps({"tool": "pain", "records": [
            {"uid": "maps:a"}, {"uid": "maps:b"}, {"uid": "reddit:c"}]}))
        # marks
        ann = set_mark(run, "favorites", "maps:a", True)
        check("favorite set", ann["favorites"], {"maps:a": True})
        ann = set_mark(run, "notes", "maps:b", "  price complaint \x07 ")
        check("note stored, trimmed, control char dropped", ann["notes"]["maps:b"], "price complaint")
        check("other kinds untouched by a note", ann["favorites"], {"maps:a": True})
        raises("unknown uid refused", lambda: set_mark(run, "favorites", "maps:zzz", True), "no record")
        raises("agent may not trash", lambda: set_mark(run, "trashed", "maps:a", True, AGENT_MARK_KINDS), "unknown mark")
        raises("note too long refused", lambda: set_mark(run, "notes", "maps:a", "x" * (MAX_NOTE + 1)), "limit")
        raises("favorite needs a bool", lambda: set_mark(run, "favorites", "maps:a", "yes"), "true or false")
        # a stale mark on a uid no longer in the index can still be cleared
        (run / VIEWER_DIR / "annotations.json").write_text(json.dumps(
            {"favorites": {"maps:gone": True}, "notes": {}, "trashed": {}, "custom": 1}))
        ann = set_mark(run, "favorites", "maps:gone", None)
        check("clearing a stale uid works", ann["favorites"], {})
        check("unknown top-level keys preserved",
              read_json(run / VIEWER_DIR / "annotations.json")["custom"], 1)
        # concurrent writers: 40 threads each add a different note; none may be lost
        (run / "frontend_index.json").write_text(json.dumps({"tool": "pain", "records":
            [{"uid": f"maps:{i}"} for i in range(40)]}))
        ts = [threading.Thread(target=set_mark, args=(run, "notes", f"maps:{i}", f"n{i}"))
              for i in range(40)]
        [t.start() for t in ts]; [t.join() for t in ts]
        check("40 concurrent note writes, none lost", len(load_annotations(run)["notes"]), 40)
        check("no temp files left", [p.name for p in (run / VIEWER_DIR).iterdir()
                                     if p.name.endswith(".tmp")], [])
        # views
        v1 = save_view(run, "Pricing complaints", ["maps:1", "maps:2", "maps:1"], note="why")
        check("view id from title", v1["id"], "pricing-complaints")
        check("view uids de-duplicated in order", v1["uids"], ["maps:1", "maps:2"])
        check("view created_by agent", v1["created_by"], "agent")
        v2 = save_view(run, "Pricing complaints", ["maps:3"])
        check("same title -> new id, old kept", v2["id"], "pricing-complaints-2")
        raises("unknown uid in view refused", lambda: save_view(run, "x", ["maps:1", "nope"]), "not in")
        raises("empty view refused", lambda: save_view(run, "x", []), "at least one")
        raises("untitled view refused", lambda: save_view(run, "   ", ["maps:1"]), "needs a title")
        v1b = save_view(run, "Pricing (updated)", ["maps:4"], view_id="pricing-complaints")
        check("update keeps id and created_at", (v1b["id"], v1b["created_at"]), ("pricing-complaints", v1["created_at"]))
        check("two views stored", [v["id"] for v in load_views(run)], ["pricing-complaints", "pricing-complaints-2"])
        delete_view(run, "pricing-complaints-2")
        check("delete view", [v["id"] for v in load_views(run)], ["pricing-complaints"])
        raises("delete unknown view refused", lambda: delete_view(run, "nope"), "no saved view")
        # no index -> refused with a re-index hint
        bare = Path(td) / "pain_bare_2026-09-01"; bare.mkdir()
        raises("run without index refused", lambda: set_mark(bare, "favorites", "x", True), "re-index")
        # server info
        write_server_info(td, {"pid": 123, "port": 1})
        clear_server_info(td, 999)
        check("server.json kept when pid differs", read_server_info(td)["pid"], 123)
        clear_server_info(td, 123)
        check("server.json removed for own pid", read_server_info(td), None)
        # current view
        write_current_view(td, {"route": "#/"})
        check("current view has received_at", "received_at" in read_current_view(td), True)
        raises("oversized current view refused",
               lambda: write_current_view(td, {"x": "y" * (MAX_CURRENT_VIEW_BYTES + 1)}), "too large")

    # remap_uid_marks -- carries marks and view uids across a uid rename.
    with tempfile.TemporaryDirectory() as td2:
        rr = Path(td2) / "pain_acme-fitness_2026-08-28"; rr.mkdir()
        (rr / "frontend_index.json").write_text(json.dumps({"tool": "pain", "records": [
            {"uid": "maps:rid1"}, {"uid": "maps:rid2"}, {"uid": "reddit:r1"}]}))
        vdir2 = rr / VIEWER_DIR; vdir2.mkdir()
        # Set up marks and a view on old positional uids.
        write_json_atomic(vdir2 / "annotations.json", {
            "favorites": {"maps:0": True, "maps:1": True, "reddit:r1": True},
            "notes": {"maps:0": "great quote"},
            "trashed": {},
        })
        write_json_atomic(vdir2 / "views.json", {"views": [
            {"id": "v1", "title": "T", "note": "", "uids": ["maps:0", "maps:1", "reddit:r1"],
             "created_by": "agent", "created_at": "2026-09-25T00:00:00Z",
             "updated_at": "2026-09-25T00:00:00Z"},
        ]})
        new_uids = {"maps:rid1", "maps:rid2", "reddit:r1"}
        result = remap_uid_marks(rr, {"maps:0": "maps:rid1", "maps:1": "maps:rid2"},
                                 new_index_uids=new_uids)
        check("remap marks_moved", result["marks_moved"], 3)   # fav maps:0, fav maps:1, note maps:0
        check("remap view_entries_moved", result["view_entries_moved"], 2)
        # All uids after remap are in the new index -> orphaned = 0.
        check("remap orphaned_marks (all in new index)", result["orphaned_marks"], 0)
        check("remap conflicts (no collisions)", result["conflicts"], 0)
        ann2 = load_annotations(rr)
        check("remap favorite moved", ann2["favorites"].get("maps:rid1"), True)
        check("remap favorite moved 2", ann2["favorites"].get("maps:rid2"), True)
        check("remap non-maps mark kept", ann2["favorites"].get("reddit:r1"), True)
        check("remap note moved", ann2["notes"].get("maps:rid1"), "great quote")
        check("remap old uid gone from favorites", "maps:0" in ann2["favorites"], False)
        vs2 = load_views(rr)
        check("remap view uids updated", vs2[0]["uids"], ["maps:rid1", "maps:rid2", "reddit:r1"])
        # No-op when uid does not change.
        result2 = remap_uid_marks(rr, {"maps:rid1": "maps:rid1"})
        check("remap no-op when uid unchanged", result2["marks_moved"], 0)
        # No-op when no annotation files exist.
        empty = Path(td2) / "pain_empty_2026-09-28"; empty.mkdir()
        (empty / "frontend_index.json").write_text(json.dumps({"tool": "pain", "records": []}))
        result3 = remap_uid_marks(empty, {"maps:0": "maps:rid1"})
        check("remap no-op when no files exist", result3["marks_moved"], 0)
        check("remap creates no files when nothing exists",
              not (empty / VIEWER_DIR / "annotations.json").exists(), True)
        # Orphaned marks (uid not in new index after remap).
        orphan_run = Path(td2) / "pain_orphan_2026-09-28"; orphan_run.mkdir()
        (orphan_run / "frontend_index.json").write_text(
            json.dumps({"tool": "pain", "records": [{"uid": "maps:rid1"}]}))
        vdir_or = orphan_run / VIEWER_DIR; vdir_or.mkdir()
        write_json_atomic(vdir_or / "annotations.json", {
            "favorites": {"maps:0": True, "reddit:r99": True},
            "notes": {}, "trashed": {},
        })
        r_orphan = remap_uid_marks(orphan_run, {"maps:0": "maps:rid1"},
                                   new_index_uids={"maps:rid1"})
        # reddit:r99 not in new_index_uids -> orphaned
        check("remap orphaned_marks count", r_orphan["orphaned_marks"], 1)
        check("remap orphaned_uids list", r_orphan["orphaned_uids"], ["reddit:r99"])
        # Conflict -- new uid already has a mark of the same kind.
        conflict_run = Path(td2) / "pain_conflict_2026-09-28"; conflict_run.mkdir()
        (conflict_run / "frontend_index.json").write_text(
            json.dumps({"tool": "pain", "records": [{"uid": "maps:rid1"}, {"uid": "maps:rid2"}]}))
        vdir_cf = conflict_run / VIEWER_DIR; vdir_cf.mkdir()
        write_json_atomic(vdir_cf / "annotations.json", {
            "favorites": {"maps:0": True, "maps:rid1": True},  # maps:rid1 already favorited
            "notes": {}, "trashed": {},
        })
        r_cf = remap_uid_marks(conflict_run, {"maps:0": "maps:rid1"})
        check("remap conflict kept existing", r_cf["conflicts"], 1)
        check("remap conflict: no mark moved", r_cf["marks_moved"], 0)
        ann_cf = load_annotations(conflict_run)
        # original maps:rid1 favorite must still be there
        check("remap existing favorite preserved", ann_cf["favorites"].get("maps:rid1"), True)
        # View dedup -- two old uids map to same new uid.
        dedup_run = Path(td2) / "pain_dedup_2026-09-28"; dedup_run.mkdir()
        (dedup_run / "frontend_index.json").write_text(
            json.dumps({"tool": "pain", "records": [{"uid": "maps:rid1"}]}))
        vdir_dd = dedup_run / VIEWER_DIR; vdir_dd.mkdir()
        write_json_atomic(vdir_dd / "views.json", {"views": [
            {"id": "v1", "title": "T", "note": "", "uids": ["maps:0", "maps:1"],
             "created_by": "agent", "created_at": "2026-09-25T00:00:00Z",
             "updated_at": "2026-09-25T00:00:00Z"},
        ]})
        r_dd = remap_uid_marks(dedup_run, {"maps:0": "maps:rid1", "maps:1": "maps:rid1"})
        vs_dd = load_views(dedup_run)
        # Both old uids mapped to same new uid; view must list it only once.
        check("remap view dedup", vs_dd[0]["uids"], ["maps:rid1"])

    # ---- ensure_carried self-tests -----------------------------------------
    def _make_run(parent, name, uids, repeat_of=None):
        """Helper: create a minimal run with an index and optional marks."""
        d = parent / name
        d.mkdir(parents=True, exist_ok=True)
        recs = [{"uid": f"trustpilot:{u}", "uid_native": True, "platform": "trustpilot",
                 "text": f"text for {u}", "date": "2026-09-01"} for u in uids]
        (d / "frontend_index.json").write_text(json.dumps({
            "tool": "pain", "records": recs,
            **({"repeat_of": repeat_of} if repeat_of else {}),
        }))
        return d

    with tempfile.TemporaryDirectory() as ec_td:
        root = Path(ec_td)

        # EC1: no repeat_of -> no files created
        no_rof = _make_run(root, "pain_norof_2026-09-01", ["a", "b"])
        result = ensure_carried(root, no_rof)
        check("EC1 no repeat_of returns None", result, None)
        check("EC1 no repeat_of creates no viewer dir",
              not (no_rof / VIEWER_DIR).exists(), True)

        # EC2: basic carry -- favorites, notes, trash, view
        m1 = _make_run(root, "pain_m1_2026-09-01", ["a", "b", "c"])
        m2 = _make_run(root, "pain_m2_2026-10-01", ["a", "b", "d"], repeat_of="pain_m1_2026-09-01")
        # Set marks on m1.
        vdir_m1 = m1 / VIEWER_DIR; vdir_m1.mkdir()
        write_json_atomic(vdir_m1 / "annotations.json", {
            "favorites": {"trustpilot:a": True, "trustpilot:c": True},  # c not in m2
            "notes": {"trustpilot:b": "great quote"},
            "trashed": {"trustpilot:a": True},  # a has both fav and trash on m1
        })
        write_json_atomic(vdir_m1 / "views.json", {"views": [
            {"id": "v1", "title": "My view", "note": "orig note",
             "uids": ["trustpilot:a", "trustpilot:b", "trustpilot:c"],  # c not in m2
             "created_by": "user", "created_at": "2026-09-25T00:00:00Z",
             "updated_at": "2026-09-25T00:00:00Z"},
        ]})
        marker = ensure_carried(root, m2)
        check("EC2 favorites_carried", marker["favorites_carried"], 1)  # a (c not in m2)
        check("EC2 notes_carried", marker["notes_carried"], 1)
        check("EC2 trash_carried", marker["trash_carried"], 1)
        check("EC2 views_carried", marker["views_carried"], 1)
        check("EC2 carried_from", marker["carried_from"], "pain_m1_2026-09-01")
        ann_m2 = load_annotations(m2)
        check("EC2 favorite landed on correct uid", ann_m2["favorites"].get("trustpilot:a"), True)
        check("EC2 note landed on correct uid", ann_m2["notes"].get("trustpilot:b"), "great quote")
        check("EC2 trash carried", ann_m2["trashed"].get("trustpilot:a"), True)
        check("EC2 c not in m2 -> skipped",
              any(s["uid"] == "trustpilot:c" and s["reason"] == "record not in this run"
                  for s in marker["skipped"]), True)
        v_m2 = load_views(m2)
        check("EC2 view carried", len(v_m2), 1)
        check("EC2 view title kept", v_m2[0]["title"], "My view")
        check("EC2 view uids reduced to present", v_m2[0]["uids"], ["trustpilot:a", "trustpilot:b"])
        check("EC2 view note has provenance", "Carried from the 2026-09-01 run." in v_m2[0]["note"], True)
        check("EC2 view note has unmapped count", "1 record could not be carried" in v_m2[0]["note"], True)

        # EC3: idempotent -- second call changes no bytes
        m2_ann_before = (m2 / VIEWER_DIR / "annotations.json").stat().st_mtime_ns
        m2_carried_before = (m2 / VIEWER_DIR / "carried.json").stat().st_mtime_ns
        ensure_carried(root, m2)
        check("EC3 idempotent: annotations unchanged",
              (m2 / VIEWER_DIR / "annotations.json").stat().st_mtime_ns, m2_ann_before)
        check("EC3 idempotent: marker unchanged",
              (m2 / VIEWER_DIR / "carried.json").stat().st_mtime_ns, m2_carried_before)

        # EC4: existing mark wins
        m1b = _make_run(root, "pain_m1b_2026-09-01", ["x", "y"])
        m2b = _make_run(root, "pain_m2b_2026-10-01", ["x", "y"], repeat_of="pain_m1b_2026-09-01")
        vdir_m1b = m1b / VIEWER_DIR; vdir_m1b.mkdir()
        write_json_atomic(vdir_m1b / "annotations.json", {
            "favorites": {"trustpilot:x": True},
            "notes": {"trustpilot:y": "prev note"},
            "trashed": {},
        })
        # Pre-existing mark on m2b
        vdir_m2b = m2b / VIEWER_DIR; vdir_m2b.mkdir()
        write_json_atomic(vdir_m2b / "annotations.json", {
            "favorites": {},
            "notes": {"trustpilot:y": "new note"},  # wins over prev note
            "trashed": {},
        })
        marker_b = ensure_carried(root, m2b)
        ann_m2b = load_annotations(m2b)
        check("EC4 existing note wins", ann_m2b["notes"].get("trustpilot:y"), "new note")
        check("EC4 conflict counted", marker_b["conflicts"], 1)
        check("EC4 favorite still carried", ann_m2b["favorites"].get("trustpilot:x"), True)

        # EC5: no repeat_of prev dir -> marker with error; no marks
        m2c = _make_run(root, "pain_m2c_2026-10-01", ["x"],
                        repeat_of="pain_nosuchrun_2026-09-01")
        result_c = ensure_carried(root, m2c)
        check("EC5 missing prev: error in marker", "error" in (result_c or {}), True)
        check("EC5 missing prev: no annotations file",
              not (m2c / VIEWER_DIR / "annotations.json").exists(), True)

        # EC6: transitive carry m1->m2->m3
        ta = _make_run(root, "pain_ta_2026-09-01", ["p", "q"])
        tb = _make_run(root, "pain_tb_2026-10-01", ["p", "q", "r"],
                       repeat_of="pain_ta_2026-09-01")
        tc = _make_run(root, "pain_tc_2026-11-01", ["p", "r"],
                       repeat_of="pain_tb_2026-10-01")
        vdir_ta = ta / VIEWER_DIR; vdir_ta.mkdir()
        write_json_atomic(vdir_ta / "annotations.json", {
            "favorites": {"trustpilot:p": True},
            "notes": {"trustpilot:q": "from ta"},
            "trashed": {},
        })
        # Open tc directly: should trigger ta->tb->tc chain
        marker_tc = ensure_carried(root, tc)
        # tc repeats tb: p and r are in tc; but first tb must carry from ta.
        # tb's carry: p->p (fav), q->q (note), but q not in tc.
        ann_tc = load_annotations(tc)
        check("EC6 transitive: p favorite reached tc", ann_tc["favorites"].get("trustpilot:p"), True)
        check("EC6 transitive: q not in tc -> skipped",
              ann_tc["notes"].get("trustpilot:q") is None, True)
        check("EC6 tb got a marker", (tb / VIEWER_DIR / "carried.json").exists(), True)

        # EC7: cycle guard -- m1c <-> m2d
        m1c = _make_run(root, "pain_m1c_2026-09-01", ["a"],
                        repeat_of="pain_m2d_2026-10-01")  # cycle!
        m2d = _make_run(root, "pain_m2d_2026-10-01", ["a"],
                        repeat_of="pain_m1c_2026-09-01")
        # Should not loop forever; should write error markers with cycle path
        try:
            ensure_carried(root, m2d)
            check("EC7 cycle guard: did not loop", True, True)
        except RecursionError:
            check("EC7 cycle guard: did not loop", False, True)
        # Check that error marker(s) contain the cycle text
        _ec7_m1 = read_carried_marker(m1c)
        _ec7_m2 = read_carried_marker(m2d)
        _ec7_has_cycle_err = any(
            m is not None and "cycle" in (m.get("error") or "")
            for m in [_ec7_m1, _ec7_m2]
        )
        check("EC7 cycle error marker written with 'cycle' text", _ec7_has_cycle_err, True)

        # EC8: missing predecessor does NOT block carry of prev's own marks.
        # m1e exists then is deleted; m2e had marks set directly; m3e should still
        # receive m2e's marks even though m2e could not receive from m1e.
        m1e = _make_run(root, "pain_m1e_2026-09-01", ["a", "b"])
        m2e = _make_run(root, "pain_m2e_2026-10-01", ["a", "b"],
                        repeat_of="pain_m1e_2026-09-01")
        m3e = _make_run(root, "pain_m3e_2026-11-01", ["a"],
                        repeat_of="pain_m2e_2026-10-01")
        vdir_m2e = m2e / VIEWER_DIR; vdir_m2e.mkdir()
        write_json_atomic(vdir_m2e / "annotations.json", {
            "favorites": {"trustpilot:a": True},
            "notes": {"trustpilot:b": "m2e note"},
            "trashed": {},
        })
        # Delete m1e -- simulates "previous run folder not found"
        import shutil as _shutil; _shutil.rmtree(m1e)
        # Open m3e: m2e gets "folder not found" error; m3e should still carry m2e's favorite
        marker_m3e = ensure_carried(root, m3e)
        check("EC8 m3e marker written despite m1e missing", marker_m3e is not None, True)
        check("EC8 m2e error marker is folder-not-found",
              "not found" in (read_carried_marker(m2e) or {}).get("error", ""), True)
        check("EC8 m3e favorites_carried=1 (from m2e)", (marker_m3e or {}).get("favorites_carried"), 1)
        ann_m3e = load_annotations(m3e)
        check("EC8 m3e has m2e's favorite", ann_m3e["favorites"].get("trustpilot:a"), True)

    print(f"\n  {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
