"""
identity.py -- stable cross-run identity for pain-index records.

The uid in a record is stable within one run, but for Google Maps records emitted by older
versions, it is a list position (maps:0, maps:1, ...), not a native id.
Downstream code ("what changed", "carry marks forward") must know
whether a uid can be used to match a record across months.

This module provides ONE function:

    record_identity(run_dir, rec) -> str | None

It returns the stable uid when one exists, and None when no stable cross-run
identity can be established.

Rules:

  1. uid_native: True  ->  return rec["uid"] as-is.  The emitter already used the
     native id, so the uid is already stable.

  2. uid_native: False ->  return None.  The uid is positional; it cannot be
     matched across runs.

  3. uid_native absent (old index, no marker):
     - Non-maps platforms: if the uid's shape matches the positional fallback
       pattern (<platform>:<raw_ref.index>), return None.  Any platform can
       produce a positional uid when the raw actor output carries no native id
       field (e.g. reddit:7 from a post at index 7).  A uid that does NOT look
       positional (e.g. trustpilot:abc123 or reddit:t3_xyz) is returned as-is.
     - maps: read the raw record at rec["raw_ref"] and, if (a) the raw record
       has a non-empty reviewId and (b) the raw record's text matches the index
       record's text (content guard against a rewritten raw file), return
       maps:<reviewId>.  On any mismatch or I/O error, return None.

Callers must treat the absent uid_native field as unknown, never as native.  This
function implements that rule explicitly: it does the safe thing for each case,
rather than letting callers make per-case decisions.

Stdlib only.  Read-only.
"""

import json
import os
from pathlib import Path

# Module-level cache: (resolved_path_str, st_mtime_ns, st_size) -> list|None
# Keyed by file identity AND modification fingerprint so a rewritten raw file
# (after a re-scrape into the same folder) is re-read instead of served stale.
_raw_cache = {}


def _load_raw(run_dir, relative_file):
    """Load a raw file from disk, caching by (path, mtime_ns, size) to detect rewrites."""
    path = (Path(run_dir) / relative_file).resolve()
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key in _raw_cache:
        return _raw_cache[key]
    result = None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        result = data if isinstance(data, list) else None
    except (ValueError, OSError):
        result = None
    _raw_cache[key] = result
    return result


def clear_cache():
    """Clear the raw-file cache.  Call between runs in tests to avoid stale data."""
    _raw_cache.clear()


def record_identity(run_dir, rec):
    """Return the stable cross-run uid for rec, or None if no identity exists.

    run_dir: the run folder (str or Path).
    rec:     a record dict from that run's frontend_index.json.

    Returns a uid string, or None.  Never raises; returns None on any I/O error.
    """
    if not isinstance(rec, dict):
        return None
    uid = rec.get("uid")
    if not uid:
        return None

    platform = rec.get("platform")
    uid_native = rec.get("uid_native")   # True / False / absent

    # Case 1: explicitly native.
    if uid_native is True:
        return uid

    # Case 2: explicitly positional.
    if uid_native is False:
        return None

    # Case 3: uid_native absent -- old index, no marker.
    if platform != "maps":
        # Any platform can produce a positional fallback (<platform>:<index>).
        # If the uid matches that shape, decline to trust it -- at worst we say
        # "cannot compare" for a record that was in fact stable; the inverse
        # (treating a positional uid as stable) silently matches the wrong review.
        ref = rec.get("raw_ref")
        if isinstance(ref, dict) and isinstance(ref.get("index"), int):
            if uid == f"{platform}:{ref['index']}":
                return None
        return uid

    # Maps, old index: look up reviewId in the raw file via raw_ref.
    raw_ref = rec.get("raw_ref")
    if not isinstance(raw_ref, dict):
        return None
    relative_file = raw_ref.get("file")
    index = raw_ref.get("index")
    if not relative_file or not isinstance(index, int):
        return None

    raw_list = _load_raw(run_dir, relative_file)
    if not isinstance(raw_list, list) or index < 0 or index >= len(raw_list):
        return None

    raw_rec = raw_list[index]
    if not isinstance(raw_rec, dict):
        return None
    review_id = raw_rec.get("reviewId")
    if review_id in (None, ""):
        return None

    # Content guard.  If the raw file was rewritten after the index was made,
    # the record at this position may be a completely different review.  Verify the
    # text the emitter copied into the index matches the text in the raw record.
    # On a mismatch, decline -- returning None is always safe; silently matching
    # the wrong review is not.
    if (raw_rec.get("text") or "") != (rec.get("text") or ""):
        return None

    return f"maps:{review_id}"
