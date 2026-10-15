#!/usr/bin/env python3
"""
viewer_server.py -- the Local Viewer's backend server (Python stdlib only).

Design: the data contract is in ../references/DATA_MODEL.md.
Serves a static frontend + a tiny read/annotate API over a marketing-scraping working
directory (the "db route" the agent chose). It is strictly READ + ANNOTATE:
it never scrapes, never spends money, never edits scraped records or reports.

Stdlib only (http.server, json, pathlib) so a stranger's install needs no pip.
Binds to 127.0.0.1 only.

Usage:
    python3 viewer_server.py --root <path> [--port 0] [--no-open]

  --root     directory to serve (recursively discovers run dirs beneath it).
             Default: current working directory. This is the scope / "db route".
  --port     0 (default) picks a free port.
  --no-open  do not open a browser (useful for tests / headless).

Endpoints:
    GET  /                         -> static frontend (static/ next to this script's skill)
    GET  /api/registry             -> [{run_id, tool, subject, generated_at, counts, indexed}]
    GET  /api/index?run=<run_id>   -> that run's frontend_index.json
    GET  /api/annotations?run=<id> -> that run's .viewer/annotations.json ({} if none)
    POST /api/annotations?run=<id> {"kind", "uid", "value"}
                                   -> set/clear ONE mark (viewer_store.set_mark); returns all marks
    GET  /api/views?run=<id>       -> {"views": [...]} saved views of that run
    GET  /api/changes?run=<id>     -> change stamps of that run's annotations / views / index,
                                      so the app picks up the agent's writes without a reload
    GET  /api/compare?run=<id>[&against=<id>]
                                   -> compare_runs output: new/gone/changed counts per platform
    POST /api/view  {...}          -> the app reports what the user has open -> <root>/.viewer/current_view.json
    POST /api/open  {"url": ...}   -> open a web (or the maker's mailto) link in the user's default browser
    GET  /files?path=<rel>         -> a file under --root (creatives, report .md); path-guarded

Every POST is JSON only, from localhost only (see _post_body). All writes go through
viewer_store.py, the same module the agent's viewer_ctl.py uses.

While running, <root>/.viewer/server.json says where the app lives (url, port, pid); it is
removed on exit. Starting a second server on a folder that already has a live one prints the
running one's address and exits, so an agent never stacks up duplicate servers.
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote, quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import viewer_store as S  # noqa: E402  (sibling module; the one writer of .viewer/ files)
import compare as _compare  # noqa: E402  (compare_runs -- one module for all callers)

# Dirs we never descend into while discovering runs.
_IGNORE_DIRS = {".git", ".tokensave", "node_modules", "__pycache__", ".viewer"}
# A dir is a "run" if it has one of these (frontend_index.json preferred).
_RUN_MARKERS = ("frontend_index.json", "AD_INTEL_CONFIG.json",
                "PAIN_CONFIG.json", "SEARCH_CONFIG.json")
# Documented config file names per skill (SKILL.md Step 0 is the contract).
# Only these exact names are looked up; no other variants (e.g. PAIN_CONFIG.acme-fitness.json)
# were found in the codebase and none are documented in any SKILL.md.
_CONFIG_NAMES = ("PAIN_CONFIG.json", "AD_INTEL_CONFIG.json", "SEARCH_CONFIG.json")
_TOOL_BY_PREFIX = {"adintel": "ad", "ad": "ad", "pain": "pain", "search": "search"}

_STATIC_DIR = (Path(__file__).resolve().parent.parent / "static")
_MAX_POST_BYTES = 1024 * 1024
_POST_READ_TIMEOUT = 10  # seconds to receive a POST body (at most 1 MB, from this machine)

# Links the frontend may ask this server to open in the user's default browser
# (POST /api/open). Embedded browsers, such as the Claude desktop app's browser pane,
# silently drop target=_blank links; this server runs on the user's own machine, so it
# can hand the link to their real browser instead. Any web link is allowed, because the
# viewer links out to whatever was scraped (ads, reviews, posts, search results, report
# sources). Email is limited to the maker's contact address.
_OPEN_MAILTO = "info@innomakerpartners.com"
# RFC 3986 reserved + unreserved characters, plus "%" so existing escapes stay as they are.
_URL_SAFE = ":/?#[]@!$&()*+,;=%-._~"


def _openable(url):
    """Return the URL to hand to the OS, or None if it must not be opened.

    http(s) links anywhere, without user:password@; mailto only to _OPEN_MAILTO, with
    subject/body only (a ?to=, ?cc= or ?bcc= would add recipients to the draft).
    Control characters are refused. Every other character outside _URL_SAFE (quotes,
    backslash, backtick, angle brackets, spaces, non-ASCII) is percent-encoded rather than
    refused: scraped URLs can legitimately contain them, browsers treat the encoded form
    as the same address, and the URL goes to the OS (osascript / xdg-open / startfile),
    which must only ever see plain ASCII."""
    if not isinstance(url, str) or not url or len(url) > 16000:
        return None
    if any(ord(c) < 32 or ord(c) == 127 for c in url):
        return None
    url = quote(url, safe=_URL_SAFE)
    try:
        u = urlparse(url)
    except ValueError:
        return None
    if u.scheme in ("http", "https"):
        return url if u.hostname and u.username is None and u.password is None else None
    if u.scheme == "mailto":
        ok = u.path == _OPEN_MAILTO and set(parse_qs(u.query)) <= {"subject", "body"}
        return url if ok else None
    return None


def _read_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return default


_NOTIFICATION_STATUSES = ("done", "refused", "incomplete")


def _read_notification(run_dir):
    """The run's own record of how it ended (notification.json), or None.

    Written by the entry scripts and repeat.py for scheduled (unattended) runs:
    status "done" | "refused" | "incomplete", a one-line message and a report.
    WHY the viewer reads it instead of inferring from the folder's contents: the
    run already recorded why it ended. Without it, a refused month (a config and
    this file, nothing scraped) looked like an old run that was never indexed.
    Anything unreadable or with an unknown status is ignored (None)."""
    n = _read_json(Path(run_dir) / "notification.json", None)
    if not isinstance(n, dict) or n.get("status") not in _NOTIFICATION_STATUSES:
        return None
    return {"status": n["status"],
            "message": str(n.get("message") or ""),
            "report": str(n.get("report") or "")}


# What each tool's report step found when it checked the written reports'
# quotes (pain) or numbers (search) against the collected data. The app shows it on
# the run page and above a report from an earlier version (which has no block at its
# top). WHY the tool's own `report.py check --json` and not a copy of its parsing
# here: one wording and one count, written by the code that wrote the report. It
# reads the report files at request time, so it is right for any run folder,
# whatever age its frontend_index.json is, and for reports written after the index.
_SKILLS_DIR = Path(__file__).resolve().parents[2]
_REPORT_CHECKERS = {"pain": "pain-and-messaging", "search": "search-intelligence"}
_REPORT_CHECK_CACHE = {}
_REPORT_CHECK_LOCK = threading.Lock()


def _report_check(run_dir, tool):
    """{"folder_line", "files": [...]} from the tool's report check, {"error": ...}
    when it could not be read, or None for a tool whose reports are not checked.
    Cached until a report file in the folder changes (size or time)."""
    skill = _REPORT_CHECKERS.get(tool)
    if not skill:
        return None
    run_dir = Path(run_dir)
    try:
        stamp = tuple(sorted((str(p.relative_to(run_dir)), p.stat().st_mtime_ns, p.stat().st_size)
                             for p in list(run_dir.glob("*.md")) + list(run_dir.glob("report/*.md"))))
    except OSError:   # a file went away while we looked: read afresh, never from cache
        stamp = (object(),)
    key = (str(run_dir), tool, stamp)
    with _REPORT_CHECK_LOCK:
        if key in _REPORT_CHECK_CACHE:
            return _REPORT_CHECK_CACHE[key]
    script = _SKILLS_DIR / skill / "scripts" / "report.py"
    try:
        # A timeout is right here: this reads two or three local files and spends
        # nothing; it is not a model call.
        r = subprocess.run([sys.executable, str(script), "check", "--workdir", str(run_dir),
                            "--json"], capture_output=True, text=True, timeout=60)
        out = json.loads(r.stdout) if r.returncode == 0 else None
        if not (isinstance(out, dict) and isinstance(out.get("files"), list)):
            out = {"error": f"the check ended with code {r.returncode}"}
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        out = {"error": str(e)[:200]}
    with _REPORT_CHECK_LOCK:
        _REPORT_CHECK_CACHE[key] = out
    return out


def _tool_of(dirname, index):
    if isinstance(index, dict) and index.get("tool"):
        return index["tool"]
    return _TOOL_BY_PREFIX.get(dirname.split("_", 1)[0], "unknown")


def discover_runs(root):
    """Recursively find run dirs under root. Does not descend INTO a run dir
    (its stage subfolders are not runs). Returns registry rows.

    Each row carries:
      run_date    -- YYYY-MM-DD from the folder name; else from generated_at; else null
      run_version -- int from _vN suffix; 1 when no suffix
      run_seq     -- 0-based position among runs with the same tool+subject in run order
                     (date, then version, then generated_at as fallback)
    These let the app determine the newest run per project without re-sorting."""
    root = Path(root).resolve()
    rows = []
    for dirpath, dirnames, filenames in os.walk(root):
        # prune ignored dirs in place
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]
        fset = set(filenames)
        marker = next((m for m in _RUN_MARKERS if m in fset), None)
        if not marker:
            continue
        d = Path(dirpath)
        run_id = str(d.relative_to(root)) if d != root else "."
        indexed = "frontend_index.json" in fset
        index = _read_json(d / "frontend_index.json", {}) if indexed else {}

        run_date, run_version = _parse_run_folder(d.name)
        if run_date is None:
            # Fallback: extract YYYY-MM-DD from generated_at if it looks like a date
            gen = index.get("generated_at") if indexed else None
            if gen and isinstance(gen, str) and len(gen) >= 10:
                candidate = gen[:10]
                if _DATE_RE.match(candidate):
                    run_date = candidate

        config_subject = _read_config_subject(d)
        # repeat_of: index is authoritative (written at re-index time); fallback to config
        repeat_of = None
        if indexed and isinstance(index, dict):
            repeat_of = index.get("repeat_of") or None
        if repeat_of is None:
            cfg = _read_config_if_present(d)
            if isinstance(cfg, dict):
                repeat_of = cfg.get("repeat_of") or None
        rows.append({
            "run_id": run_id,
            "tool": _tool_of(d.name, index),
            "subject": _corrected_subject(d.name, index if indexed else {}, config_subject),
            "generated_at": index.get("generated_at") if indexed else None,
            "counts": index.get("counts") if indexed else None,
            "indexed": indexed,
            "run_date": run_date,
            "run_version": run_version,
            "run_seq": 0,  # placeholder; _assign_run_seq fills in the real value
            "repeat_of": repeat_of,
            # How a scheduled run ended, passed forward as the run wrote it (or None).
            "notification": _read_notification(d),
        })
        # do not descend into a run dir
        dirnames[:] = []

    _assign_run_seq(rows)
    rows.sort(key=lambda r: (r["tool"], r["run_id"]))
    return rows


_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_VN_RE = re.compile(r'^v\d+$', re.IGNORECASE)


def _parse_run_folder(name):
    """Parse a run folder name: <prefix>_<slug>_<YYYY-MM-DD>[_v<N>].

    Returns (date_str, version) where date_str is YYYY-MM-DD and version is an int
    (1 when there is no _vN suffix).  Returns (None, 1) for unconventional names.

    This is the single authority for run_date and run_version; caller resolves
    the fallback to generated_at when date_str is None."""
    parts = name.split("_")
    if len(parts) < 3 or parts[0] not in ("adintel", "ad", "pain", "search"):
        return None, 1
    for i, part in enumerate(parts):
        if _DATE_RE.match(part):
            after = parts[i + 1:]
            version = 1
            if after and _VN_RE.match(after[0]):
                try:
                    version = int(after[0][1:])  # strip leading 'v'/'V'
                except ValueError:
                    pass
            return part, version
    return None, 1


def _subject_from_dirname(name):
    """Best-effort subject from a run dir named <prefix>_<slug>_<YYYY-MM-DD>[_vN].
    The slug may contain underscores: everything between the prefix and the date.
    Unconventional names (no date found, empty slug, unrecognized prefix) return as-is."""
    parts = name.split("_")
    if len(parts) >= 3 and parts[0] in ("adintel", "ad", "pain", "search"):
        for i, part in enumerate(parts):
            if _DATE_RE.match(part):
                slug_parts = parts[1:i]
                if slug_parts:
                    return " ".join(slug_parts).replace("-", " ").title()
                break
    return name


def _corrected_subject(dirname, index, config_subject=None):
    """Return the subject to use for this run, correcting stale buggy subjects.

    The old function used parts[1:-1], which included the date for
    _vN folders (the version became the last part, so the date was not dropped).
    This produced subjects like "Acme Fitness 2026 09 28" for pain_acme-fitness_2026-09-28_v2.

    Guard: only correct when ALL of:
      - the folder has a _vN suffix (last segment matches v<N> and a date was found)
      - the stored index subject equals exactly what the old buggy function returned
      - the run has no explicit config subject (a config subject proves the stored
        value is the user's deliberate choice, even if it coincidentally matches the old
        buggy output, so never correct it)
    Otherwise trust the stored subject as-is (a user-set subject must never be overwritten).

    When there is no stored index subject, config_subject is the next fallback,
    before the folder-name derivation.  Precedence: index > config > folder name."""
    stored = index.get("subject") if isinstance(index, dict) else None
    if stored:
        run_date, _ = _parse_run_folder(dirname)
        last_part = dirname.split("_")[-1]
        has_vn = run_date is not None and bool(_VN_RE.match(last_part))
        if has_vn:
            # An explicit config subject is evidence of deliberate intent; keep stored.
            if config_subject:
                return stored
            # Reconstruct what the old buggy function would have returned
            parts = dirname.split("_")
            old_buggy = (" ".join(parts[1:-1]).replace("-", " ").title()
                         if len(parts) >= 3 else dirname)
            if stored == old_buggy:
                return _subject_from_dirname(dirname)
        return stored
    # No stored index subject: config subject before folder name.
    if config_subject:
        return config_subject
    return _subject_from_dirname(dirname)


def _read_config_subject(run_dir):
    """Read the subject from a run's config file if one is present and has a non-empty subject.

    Tries each name in _CONFIG_NAMES.  Returns the subject string, or None when:
    - no config file is present in that directory;
    - the file exists but is unreadable or malformed (never breaks the registry);
    - the parsed object has no non-empty string 'subject' field.

    WHY this order: _CONFIG_NAMES lists all three known names.  A run dir holds at most
    one config (one skill per run), so the loop terminates on the first file found."""
    for name in _CONFIG_NAMES:
        p = Path(run_dir) / name
        if not p.exists():
            continue
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None   # malformed or unreadable: fall back to folder name
        s = cfg.get("subject") if isinstance(cfg, dict) else None
        return s if (s and isinstance(s, str)) else None
    return None


def _read_config_if_present(run_dir):
    """Return the parsed config dict for a run, or None.

    Tries each name in _CONFIG_NAMES; returns None when no config file exists,
    the file is unreadable, or the content is not a JSON object.

    WHY separate from _read_config_subject: discover_runs needs the whole config
    dict (to read repeat_of and potentially other P2 fields), not just the subject."""
    for name in _CONFIG_NAMES:
        p = Path(run_dir) / name
        if not p.exists():
            continue
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
        return obj if isinstance(obj, dict) else None
    return None


def _run_sort_key(row):
    """Ordering key: (run_date, run_version, generated_at, run_id).

    Used by _assign_run_seq to order runs within a (tool, subject) group.
    run_date=None sorts before any real date (null date means no convention:
    generated_at is the only clue, which sorts last when its date is latest).
    Folder date is primary; never changes.  generated_at is the fallback
    only because it changes on every reindex, so it must not be primary.
    run_id is a final tie-breaker so the order is always deterministic even
    when date, version and generated_at all happen to match (e.g. a _v01
    folder and a no-suffix folder both parse to run_version=1 with equal
    generated_at).  The run order is FROZEN; run_id only breaks residual ties."""
    date = row.get("run_date") or "0000-00-00"
    ver = row.get("run_version") or 1
    gen = row.get("generated_at") or ""
    rid = row.get("run_id") or ""
    return (date, ver, gen, rid)


def _assign_run_seq(rows):
    """Assign run_seq: 0-based position in run order within each (tool, subject) group.

    WHY run_seq: the app needs a stable integer to determine which run is "newest"
    for a project without re-sorting on the client.  The field is recomputed on
    every registry call, so it reflects any new run that appears on disk."""
    groups = {}
    for idx, row in enumerate(rows):
        key = (row["tool"], row["subject"])
        groups.setdefault(key, []).append(idx)
    for indices in groups.values():
        ordered = sorted(indices, key=lambda i: _run_sort_key(rows[i]))
        for seq, idx in enumerate(ordered):
            rows[idx]["run_seq"] = seq


def _safe_under_root(root, rel):
    """Resolve rel against root and return the path ONLY if it stays inside root.
    Rejects absolute paths and .. traversal. Returns None if unsafe."""
    if rel is None:
        return None
    rel = unquote(rel).lstrip("/")
    if not rel or rel == ".":
        return Path(root).resolve()
    candidate = (Path(root).resolve() / rel).resolve()
    try:
        candidate.relative_to(Path(root).resolve())
    except ValueError:
        return None
    return candidate


_CT = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
       ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
       ".md": "text/markdown; charset=utf-8", ".png": "image/png", ".jpg": "image/jpeg",
       ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
       ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
       ".woff2": "font/woff2", ".woff": "font/woff"}


def make_handler(root):
    root = Path(root).resolve()

    class Handler(BaseHTTPRequestHandler):
        server_version = "MarketingViewer/1.0"

        def log_message(self, *a):  # quieter console
            pass

        # -- helpers -------------------------------------------------------
        def _send(self, code, body=b"", ctype="application/json; charset=utf-8"):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

        @staticmethod
        def _parse_range(rng, size):
            """Parse a single 'bytes=start-end' range against a known size.
            Returns (start, end) inclusive, or None if unsatisfiable/malformed."""
            if not rng or not rng.startswith("bytes="):
                return None
            spec = rng.split("=", 1)[1].split(",", 1)[0].strip()
            if "-" not in spec:
                return None
            a, b = spec.split("-", 1)
            try:
                if a == "":                       # suffix: last N bytes
                    n = int(b)
                    if n <= 0:
                        return None
                    start, end = max(0, size - n), size - 1
                else:
                    start = int(a)
                    end = int(b) if b else size - 1
            except ValueError:
                return None
            if start > end or start >= size:
                return None
            return start, min(end, size - 1)

        def _stream(self, f, length):
            """Write `length` bytes from an open file in chunks, so a big mp4 never loads
            whole into memory. Swallows a client disconnect (browser seek closes the socket)."""
            remaining = length
            try:
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _serve_file(self, path):
            if not path or not path.is_file():
                return self._json(404, {"error": "not found"})
            ctype = _CT.get(path.suffix.lower(), "application/octet-stream")
            try:
                size = path.stat().st_size
                # Range support is what makes <video> seekable/scrubbable. Without a 206 +
                # Accept-Ranges, browsers can only play an mp4 straight through, never seek.
                rng = self._parse_range(self.headers.get("Range"), size)
                if rng is not None:
                    start, end = rng
                    length = end - start + 1
                    self.send_response(206)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                    self.send_header("Content-Length", str(length))
                    self.end_headers()
                    if self.command != "HEAD":
                        with path.open("rb") as f:
                            f.seek(start)
                            self._stream(f, length)
                    return
                if self.headers.get("Range"):        # present but unsatisfiable
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(size))
                self.end_headers()
                if self.command != "HEAD":
                    with path.open("rb") as f:
                        self._stream(f, size)
            except OSError:
                self._json(500, {"error": "read failed"})

        def _run_dir(self, qs):
            run = (qs.get("run") or [None])[0]
            if run is None:
                self._json(400, {"error": "missing ?run="}); return None
            d = _safe_under_root(root, run)
            if d is None or not d.is_dir():
                self._json(404, {"error": "run not found"}); return None
            return d

        # -- routes --------------------------------------------------------
        def do_GET(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            route = u.path

            if route == "/api/registry":
                return self._json(200, discover_runs(root))

            if route == "/api/server":
                # Who this server is, in its own words: stop/reuse trust this, never the
                # server.json file alone (a copied or edited file could name another process).
                return self._json(200, {"root": str(root), "pid": os.getpid()})

            if route == "/api/index":
                d = self._run_dir(qs)
                if d is None:
                    return
                idx = _read_json(d / "frontend_index.json", None)
                if idx is None:
                    # A refused or stopped scheduled month has nothing to re-index;
                    # say what the run itself recorded instead (same error key, so
                    # the app's handling is unchanged).
                    n = _read_notification(d)
                    if n and n["status"] in ("refused", "incomplete"):
                        return self._json(404, {"error": "run not indexed",
                                                "notification": n,
                                                "hint": "this scheduled month did not finish; "
                                                        "there is nothing to re-index"})
                    return self._json(404, {"error": "run not indexed",
                                            "hint": "re-index it for free: viewer_ctl.py reindex"})
                # What the automated check found in the written reports,
                # read from the report files now (never stored in the index).
                if isinstance(idx, dict) and idx.get("reports"):
                    rc = _report_check(d, idx.get("tool"))
                    if rc is not None:
                        idx["report_check"] = rc
                return self._json(200, idx)

            if route == "/api/annotations":
                d = self._run_dir(qs)
                if d is None:
                    return
                try:
                    S.ensure_carried(root, d)   # Carry marks forward on first read
                except Exception as e:
                    # Log carry exceptions so they surface in server stderr; the
                    # annotations response still succeeds (carry is non-fatal for reads).
                    print(f"[carry] {e}", file=sys.stderr)
                return self._json(200, _read_json(d / ".viewer" / "annotations.json", {}) or {})

            if route == "/api/views":
                d = self._run_dir(qs)
                if d is None:
                    return
                try:
                    S.ensure_carried(root, d)   # Carry marks forward on first read
                except Exception as e:
                    print(f"[carry] {e}", file=sys.stderr)  # Log to stderr
                return self._json(200, {"views": S.load_views(d)})

            if route == "/api/carried":
                d = self._run_dir(qs)
                if d is None:
                    return
                # Attempt carry before reading the marker; expose exceptions to the app.
                carry_err = None
                try:
                    S.ensure_carried(root, d)
                except Exception as e:
                    carry_err = str(e)
                marker = S.read_carried_marker(d)
                # Carry exception with no marker: return error so the app can display it.
                if carry_err is not None and marker is None:
                    return self._json(200, {"carried": False, "error": carry_err})
                if marker is None:
                    return self._json(200, {"carried": False})
                # Error markers (cycle, missing folder) must be surfaced as
                # carried=False so paintCarriedNotice shows the error notice.
                if marker.get("error"):
                    return self._json(200, {"carried": False, **marker})
                return self._json(200, {"carried": True, **marker})

            if route == "/api/changes":
                d = self._run_dir(qs)
                if d is None:
                    return
                v = d / S.VIEWER_DIR
                return self._json(200, {"annotations": S.stamp(v / "annotations.json"),
                                        "views": S.stamp(v / "views.json"),
                                        "index": S.stamp(d / "frontend_index.json")})

            if route == "/api/compare":
                run_id = (qs.get("run") or [None])[0]
                if not run_id:
                    return self._json(400, {"error": "missing ?run="})
                run_dir = _safe_under_root(root, run_id)
                if run_dir is None or not run_dir.is_dir():
                    return self._json(404, {"error": "run not found"})
                against = (qs.get("against") or [None])[0]
                if against is not None:
                    against_dir = _safe_under_root(root, against)
                    if against_dir is None or not against_dir.is_dir():
                        return self._json(404, {"error": "against run not found"})
                try:
                    result = _compare.compare_runs(root, run_id,
                                                   against=against if against else None)
                except Exception as exc:  # noqa: BLE001
                    return self._json(500, {"error": str(exc)})
                return self._json(200, result)

            if route == "/files":
                p = _safe_under_root(root, (qs.get("path") or [None])[0])
                if p is None:
                    return self._json(403, {"error": "path outside root"})
                return self._serve_file(p)

            # static frontend
            rel = route.lstrip("/") or "index.html"
            fp = _safe_under_root(_STATIC_DIR, rel)
            if fp is None:
                return self._json(403, {"error": "bad path"})
            if fp.is_dir():
                fp = fp / "index.html"
            if fp.is_file():
                return self._serve_file(fp)
            # SPA-ish fallback to index.html
            idxhtml = _STATIC_DIR / "index.html"
            if idxhtml.is_file():
                return self._serve_file(idxhtml)
            return self._json(404, {"error": "frontend not built",
                                    "hint": "static/index.html missing"})

        def do_HEAD(self):
            self.do_GET()

        def _post_body(self):
            """Read a POST's JSON object body, or send the refusal and return None.

            Only the viewer itself may POST. JSON only: a cross-site page cannot send
            application/json without a CORS preflight, which this server never answers.
            Host check: blocks DNS rebinding (an attacker's domain re-pointed at 127.0.0.1
            would arrive with its own name in Host). A missing or negative Content-Length
            reads nothing instead of waiting for bytes that never come."""
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            if host not in ("127.0.0.1", "localhost"):
                self._json(403, {"error": "bad host"}); return None
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                self._json(415, {"error": "expected application/json"}); return None
            try:
                length = max(0, int(self.headers.get("Content-Length") or 0))
            except ValueError:
                self._json(400, {"error": "bad Content-Length"}); return None
            if length > _MAX_POST_BYTES:
                self._json(413, {"error": "body too large"}); return None
            # A client that declares more bytes than it sends would hold this thread
            # forever; a read deadline (latency-bound, so a timeout is right) ends it.
            self.connection.settimeout(_POST_READ_TIMEOUT)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except TimeoutError:
                self._json(408, {"error": "body not received in time"}); return None
            except ValueError:
                self._json(400, {"error": "bad JSON"}); return None
            if not isinstance(body, dict):
                self._json(400, {"error": "body must be a JSON object"}); return None
            return body

        def do_POST(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            if u.path not in ("/api/open", "/api/annotations", "/api/view"):
                return self._json(404, {"error": "unknown endpoint"})
            body = self._post_body()
            if body is None:
                return
            if u.path == "/api/open":
                return self._open_link(body)
            if u.path == "/api/view":
                try:
                    S.write_current_view(root, body)
                except S.StoreError as e:
                    return self._json(400, {"error": str(e)})
                return self._json(200, {"ok": True})
            d = self._run_dir(qs)
            if d is None:
                return
            try:
                ann = S.set_mark(d, body.get("kind"), body.get("uid"), body.get("value"))
            except S.StoreError as e:
                return self._json(400, {"error": str(e)})
            return self._json(200, {"annotations": ann,
                                    "stamp": S.stamp(d / S.VIEWER_DIR / "annotations.json")})

        def _open_link(self, body):
            url = body.get("url")
            safe_url = _openable(url)
            if safe_url is None:
                return self._json(403, {"error": "link not allowed"})
            # False when the machine has no usable browser (e.g. a headless sandbox); the
            # frontend then falls back to opening the link itself.
            return self._json(200, {"opened": bool(webbrowser.open(safe_url))})

    return Handler


def running_server(root):
    """The live server already serving THIS folder, or None.

    server.json only says where to look. The server at that address must itself answer
    /api/server with this same root; its own pid (not the file's) is what is returned, so
    `stop` can never be pointed at another process by a stale, copied or edited file."""
    info = S.read_server_info(root)
    if not isinstance(info, dict) or not isinstance(info.get("url"), str):
        return None
    try:
        with urllib.request.urlopen(info["url"].rstrip("/") + "/api/server", timeout=1) as r:
            me = json.loads(r.read() or b"{}")
    except (OSError, ValueError):
        return None
    if not isinstance(me, dict) or not isinstance(me.get("pid"), int):
        return None
    if Path(str(me.get("root", ""))).resolve() != Path(root).resolve():
        return None
    return {**info, "pid": me["pid"]}


def serve(root, port=0, host="127.0.0.1", open_browser=True):
    root = Path(root).resolve()
    live = running_server(root)
    if live:
        print(f"Marketing Viewer already running for {root}")
        print(f"  {live['url']}")
        return live["url"]
    handler = make_handler(root)
    httpd = ThreadingHTTPServer((host, port), handler)
    actual = httpd.server_address[1]
    url = f"http://{host}:{actual}/"
    n = len(discover_runs(root))
    pid = os.getpid()
    S.write_server_info(root, {"url": url, "port": actual, "host": host, "pid": pid,
                               "root": str(root), "started_at": S.now_iso()})
    # A plain kill (SIGTERM) must still remove server.json: turn it into a normal exit.
    try:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    except ValueError:      # not the main thread (tests): the caller cleans up
        pass
    print(f"Marketing Viewer serving {root}")
    print(f"  {n} run(s) discovered")
    print(f"  {url}")
    print("  Ctrl-C to stop.")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping.")
    finally:
        httpd.server_close()
        S.clear_server_info(root, pid)
    return url


def main():
    ap = argparse.ArgumentParser(description="Local Viewer server for marketing-scraping runs.")
    ap.add_argument("--root", default=".", help="directory to serve (the scope / db route)")
    ap.add_argument("--port", type=int, default=0, help="0 = pick a free port")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-open", action="store_true", help="do not open a browser")
    args = ap.parse_args()
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"error: --root is not a directory: {root}", file=sys.stderr)
        sys.exit(2)
    serve(root, port=args.port, host=args.host, open_browser=not args.no_open)


if __name__ == "__main__":
    main()
