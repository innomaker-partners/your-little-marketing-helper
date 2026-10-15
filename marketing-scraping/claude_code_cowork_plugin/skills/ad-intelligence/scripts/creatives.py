"""
Download ad creative images/videos found in Meta Ad Library and Google Ads
scrape results. Must be called promptly after scraping — Meta fbcdn.net URLs
are time-limited signed URLs that expire within minutes.

Saves files under:
    <out_dir>/meta/<ad_archive_id>_<i>.<ext>
    <out_dir>/google/<advertiserId>_<creativeId>_<i>.<ext>

Writes a manifest JSON beside each batch so callers can audit what saved vs.
what failed. Failed fetches are logged in the manifest as "FAILED: <reason>"
and never raise — the batch continues regardless.

Repeat-run reuse:
    When prev_creatives_dir is supplied (a path to the previous run's
    creatives/ folder), each platform's download function reads a
    sources.json sidecar from the previous run and hard-links (or copies)
    any creative whose URL path has not changed.  This avoids re-downloading
    gigabytes of video that the previous run already saved.

    sources.json maps filename -> URL path (no query string, no host).
    reuse.json records per-run counts: linked, copied, downloaded, failed.

    A previous run with no sources.json (made before this change) reuses
    nothing and downloads normally.

Re-running the same run folder:
    A run that stopped part-way is finished by running the same command on the
    same folder. The scraped records are reused, but their CDN links expire
    within days, so downloading again turned every creative into FAILED in the
    manifest while the files sat on disk with nothing pointing at them (the app
    went from 64 of 70 ads with images to 0 of 70). Before any download, a slot
    whose file is already in this folder is kept and recorded as saved, unless
    this folder's own sources.json says that file came from a different link
    (then the slot holds another creative, and it is fetched again).

File names from scraped ids:
    Ids come from the scraped data, not from us. An id like
    "../../../x" wrote outside the run folder. safe_name() keeps letters,
    digits, "_", "." and "-" and turns everything else into "_"; ordinary ids
    (digits, or Google's AR.../CR... ids) are unchanged. Earlier runs saved
    files under the raw id, so the reuse lookups also try the raw name when it
    differs and is a single file name; a creative an ordinary id would keep is
    never lost because its id needed cleaning.

URL-path identity, per platform:
    Meta:     path is stable across scrapes; query string carries signed
              tokens (_nc_sid, etc.) that change each scrape.
    Google:   path is the entire identity (tpc.googlesyndication.com
              /archive/simgad/<id>); no query string at all.
    LinkedIn: path is stable; query string carries a far-future expiry
              (e=2147483647) and a signing token (t=) that appears non-
              rotating in both fixture data and real actor output.

    All three platforms: reuse is safe when path matches; query string
    is never used as identity.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

_TIMEOUT = 20  # seconds per request
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _ext_from_url(url: str, content_type: str = "", default: str = ".jpg") -> str:
    """Derive a file extension from a URL path, falling back to content-type."""
    path = urlparse(url).path
    suffix = Path(path).suffix.lower()
    # Accept common media suffixes; reject query-string artefacts
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mov", ".webm"}:
        return suffix
    if content_type:
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if ext:
            return ext
    return default


_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]")
_FETCHABLE_SCHEMES = ("http", "https")


def safe_name(raw) -> str:
    """A scraped id made safe to use in a file name.

    Keeps letters, digits, "_", "." and "-"; anything else becomes "_", so the
    name cannot hold a folder separator and cannot leave the run folder. A
    leading "." becomes "_" too, which refuses "." and ".." and keeps the file
    from being hidden (the app and a file browser would not show it); "" becomes
    "_". Ordinary ids (all digits, or Google's AR.../CR... ids) come back
    unchanged.

    When the name had to change, 8 characters of the raw id's sha256 are added,
    so two different ids ("a b" and "a&b") never share one file and one ad's
    image is never shown under another ad.
    """
    raw = str(raw)
    name = _UNSAFE_NAME_CHARS.sub("_", raw)
    name = re.sub(r"^\.+", lambda m: "_" * len(m.group(0)), name) or "_"
    if name != raw:
        name += "_" + hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:8]
    return name


def _single_name(name: str) -> bool:
    """True when *name* is one plain file name (no folder part)."""
    return bool(name) and "/" not in name and "\0" not in name and name not in (".", "..")


def _fetch(url: str, dest: str) -> None:
    """
    GET *url* with a browser-like User-Agent, write bytes to *dest*.

    Writes via a temporary file in the same directory, then os.replace onto
    the final name.  This means the folder entry for *dest* is replaced
    atomically: any previous hard link to a file at that path is broken
    rather than written-through.  A hard-linked previous-run creative is
    therefore never modified even when the new run downloads over the same
    filename.

    Raises OSError / urllib.error.URLError on any failure so callers can
    catch and log rather than crash.

    Only http and https links are fetched: a scraped link is data,
    and a file:// link would copy a file from this computer into the run
    folder. Any other scheme, including one reached by a redirect, is refused
    with a ValueError, which the caller records as a failed download.
    """
    scheme = urlparse(url).scheme.lower()
    if scheme not in _FETCHABLE_SCHEMES:
        raise ValueError("only http and https links are downloaded, and this "
                         + (f"is a {scheme}: link" if scheme else "link names neither"))
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        final = urlparse(resp.geturl() or url).scheme.lower()
        if final not in _FETCHABLE_SCHEMES:
            raise ValueError(f"the link redirected to a {final} link, which is not downloaded")
        data = resp.read()
    if not data:
        raise OSError("Empty response body")
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file then os.replace so the folder entry changes
    # atomically, breaking any hard link rather than writing through it.
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(dest_path.parent), prefix=".tmp_")
    try:
        # Use os.fdopen so the buffered writer guarantees all bytes are written
        # (os.write may write fewer bytes than asked on macOS for large buffers,
        # silently truncating the file).  os.fdopen takes ownership of tmp_fd,
        # so we must not close tmp_fd separately after this point.
        with os.fdopen(tmp_fd, "wb") as fh:
            fh.write(data)
        tmp_fd = -1  # fdopen took ownership; prevent double-close in except
        # Give the file the mode a plain open/write would give (0666 & ~umask).
        # mkstemp creates 0600; leaving that would be a silent regression for
        # users who share run folders or run tools under a different user.
        _mask = os.umask(0)
        os.umask(_mask)
        os.chmod(tmp_path, 0o666 & ~_mask)
        os.replace(tmp_path, str(dest_path))
    except Exception:
        if tmp_fd != -1:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _save(url: str, dest_no_ext: str, default_ext: str) -> str:
    """
    Fetch *url* and write to *dest_no_ext* + derived extension.
    Returns the saved filename on success, or "FAILED: <reason>" on error.
    """
    try:
        # HEAD first (optional) is skipped to avoid extra round-trips on
        # expiring Meta URLs — derive ext from URL only.
        ext = _ext_from_url(url, default=default_ext)
        dest = dest_no_ext + ext
        _fetch(url, dest)
        return Path(dest).name
    except Exception as exc:
        return f"FAILED: {exc}"


def _try_reuse(prev_file: Path, dest: str, counts: dict) -> str | None:
    """
    Hard-link *prev_file* to *dest*, falling back to an atomic copy on OSError.
    Returns the destination filename on success, or None when both fail.

    Both the hard-link and the copy go through a temp-name + os.replace so
    that if *dest* already exists (e.g. a hard link to a THIRD run's file
    created by a later month that already linked to it), the existing entry is
    atomically replaced rather than written-through.  This is the same
    contract as _fetch.

    Nothing opens *prev_file* for writing.  Fallback order:
    link -> copy -> caller falls back to a fresh download.
    """
    dest_path = Path(dest)
    if not prev_file.exists():
        return None
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    # Hard link to a temp name then os.replace onto the final name.
    # This avoids FileExistsError when dest already exists and also avoids
    # writing through any existing hard link at dest.
    tmp_link = dest_path.parent / (f".tmp_link_{dest_path.name}")
    try:
        # Remove stale temp name if present from a previous interrupted run.
        try:
            os.unlink(str(tmp_link))
        except FileNotFoundError:
            pass
        os.link(str(prev_file), str(tmp_link))
        os.replace(str(tmp_link), str(dest_path))
        print(f"   [reuse:linked] {dest_path.name}")
        counts["linked"] += 1
        return dest_path.name
    except OSError:
        try:
            os.unlink(str(tmp_link))
        except OSError:
            pass

    # Atomic copy fallback: copy to a temp file, chmod to umask default,
    # then os.replace.  Same safety guarantee as the hard-link path.
    tmp_fd, tmp_path = None, None
    try:
        import shutil
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(dest_path.parent), prefix=".tmp_copy_"
        )
        os.close(tmp_fd)
        tmp_fd = None
        shutil.copy2(str(prev_file), tmp_path)
        _mask = os.umask(0)
        os.umask(_mask)
        os.chmod(tmp_path, 0o666 & ~_mask)
        os.replace(tmp_path, str(dest_path))
        print(f"   [reuse:copied] {dest_path.name}")
        counts["copied"] += 1
        return dest_path.name
    except OSError:
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return None


def _load_prev_sources(prev_platform_dir: Path | None) -> dict:
    """
    Read sources.json from a previous run's platform folder.
    Returns an empty dict when the file does not exist or cannot be parsed.
    A previous run made before this change has no sources.json; that is safe
    and causes a normal full download.
    """
    if prev_platform_dir is None:
        return {}
    path = prev_platform_dir / "sources.json"
    if not path.exists():
        return {}
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


def _load_own_sources(dest_root: Path) -> dict:
    """This folder's own sources.json from an earlier run of the same folder
    ({} when there is none). Read before the run rewrites it."""
    return _load_prev_sources(dest_root)


def _slot(platform: str, label: str, raw_stem: str, i: int, url: str, default_ext: str,
          dest_root: Path, prev_platform_dir, prev_sources: dict, own_sources: dict,
          sources: dict, counts: dict) -> str:
    """Fill one creative slot and return its manifest entry (a file name, or
    "FAILED: <reason>"). Shared by the three platforms, in this order:

      1. kept: the slot's file is already in this folder (a re-run of the same
         folder), so it is recorded as saved and nothing is fetched. Guard:
         when this folder's sources.json says the file came from a different
         link path, it is another creative, and the slot is fetched again.
      2. reused: an earlier month's run saved the same link path.
      3. downloaded, or FAILED when the fetch fails.
    Both lookups (1 and 2) try the clean name first, then the raw-id name when
    it differs and is one plain file name (earlier runs saved under raw ids).
    """
    url_path = urlparse(url).path  # stable identity: path only
    ext = _ext_from_url(url, default=default_ext)
    stem = safe_name(raw_stem)
    name = f"{stem}_{i}{ext}"
    names = [name]
    raw_name = f"{raw_stem}_{i}{ext}"
    if raw_name != name and _single_name(raw_name):
        names.append(raw_name)
    dest = dest_root / name

    for n in names:
        f = dest_root / n
        if own_sources.get(n, url_path) != url_path:
            continue  # this folder recorded another link for that file
        try:
            on_disk = f.is_file() and f.stat().st_size > 0
        except OSError:
            on_disk = False
        if on_disk:
            sources[n] = url_path
            counts["kept"] += 1
            print(f"   {platform} {label}[{i}] → kept (already saved in this folder)")
            return n

    if prev_platform_dir is not None:
        for n in names:
            if prev_sources.get(n) != url_path:
                continue
            outcome = _try_reuse(prev_platform_dir / n, str(dest), counts)
            if outcome is not None:
                sources[outcome] = url_path
                print(f"   {platform} {label}[{i}] → reused")
                return outcome
            # Both link and copy failed; fall through to a fresh download.

    outcome = _save(url, str(dest_root / f"{stem}_{i}"), default_ext)
    if not outcome.startswith("FAILED"):
        sources[outcome] = url_path
        counts["downloaded"] += 1
    else:
        counts["failed"] += 1
    status = "saved" if not outcome.startswith("FAILED") else outcome
    print(f"   {platform} {label}[{i}] → {status}")
    return outcome


# ---------------------------------------------------------------------------
# Meta Ad Library
# ---------------------------------------------------------------------------

def _meta_creative_urls(snapshot: dict) -> list[tuple[str, str]]:
    """
    Return (url, default_ext) pairs from a snapshot dict.
    Covers cards[].original_image_url / resized_image_url / video_* plus
    top-level images[] and videos[].
    """
    seen: set[str] = set()
    results: list[tuple[str, str]] = []

    def _add(url, ext):
        if url and url not in seen:
            seen.add(url)
            results.append((url, ext))

    for card in snapshot.get("cards") or []:
        _add(card.get("original_image_url"), ".jpg")
        # resized_image_url often duplicates original — include anyway so
        # callers can choose; dedup via seen-set handles identical URLs.
        _add(card.get("resized_image_url"), ".jpg")
        _add(card.get("video_hd_url"), ".mp4")
        _add(card.get("video_sd_url"), ".mp4")
        _add(card.get("video_preview_image_url"), ".jpg")

    for img in snapshot.get("images") or []:
        if not isinstance(img, dict):
            continue
        _add(img.get("original_image_url"), ".jpg")
        _add(img.get("resized_image_url"), ".jpg")

    for vid in snapshot.get("videos") or []:
        if not isinstance(vid, dict):
            continue
        _add(vid.get("video_hd_url"), ".mp4")
        _add(vid.get("video_sd_url"), ".mp4")
        _add(vid.get("video_preview_image_url"), ".jpg")

    return results


def download_meta_creatives(
    meta_items: list[dict],
    out_dir: str,
    prev_creatives_dir: str | None = None,
) -> dict:
    """
    Fetch all creative images/videos from a list of Meta Ad Library records.

    Args:
        meta_items:        list of ad records as returned by the Apify actor.
        out_dir:           root output directory. Files go under <out_dir>/meta/.
        prev_creatives_dir: path to the previous run's creatives/ folder, or
                           None for a fresh run.  When given, creatives whose
                           URL path matches the previous run's sources.json are
                           hard-linked (or copied) instead of downloaded.

    Returns:
        manifest dict  {ad_archive_id: [filename_or_"FAILED: reason", ...]}
        Also writes the manifest to <out_dir>/meta/manifest.json.
        Also writes <out_dir>/meta/sources.json  (filename -> URL path).
        Also writes <out_dir>/meta/reuse.json    (linked/copied/downloaded/failed counts).
    """
    dest_root = Path(out_dir) / "meta"
    dest_root.mkdir(parents=True, exist_ok=True)

    # Reuse setup
    prev_platform_dir = Path(prev_creatives_dir) / "meta" if prev_creatives_dir else None
    prev_sources = _load_prev_sources(prev_platform_dir)
    own_sources = _load_own_sources(dest_root)
    reuse_counts: dict[str, int] = {"linked": 0, "copied": 0, "downloaded": 0, "failed": 0,
                                    "kept": 0}

    manifest: dict[str, list[str]] = {}
    sources: dict[str, str] = {}  # filename -> URL path (no query, no host)

    for record in meta_items:
        if not isinstance(record, dict):
            continue
        ad_id = str(record.get("ad_archive_id", "unknown"))
        snapshot = record.get("snapshot") or {}
        urls = _meta_creative_urls(snapshot)

        # Reuse identity = (ad_id, slot_i, url_path). We do NOT gate on the
        # config fingerprint: a changed competitor list still shares the
        # unchanged competitors' ads, and the URL path check keeps it safe.
        results = [
            _slot("meta", ad_id, ad_id, i, url, default_ext, dest_root,
                  prev_platform_dir, prev_sources, own_sources, sources, reuse_counts)
            for i, (url, default_ext) in enumerate(urls)
        ]

        manifest[ad_id] = results

    manifest_path = dest_root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (dest_root / "sources.json").write_text(
        json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (dest_root / "reuse.json").write_text(
        json.dumps(reuse_counts, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    saved = sum(
        1
        for v in manifest.values()
        for s in v
        if not s.startswith("FAILED")
    )
    print(f"   → Meta: {saved}/{sum(len(v) for v in manifest.values())} creatives saved")
    return manifest


# ---------------------------------------------------------------------------
# Google Ads Transparency
# ---------------------------------------------------------------------------

def _is_real_image_url(url) -> bool:
    """
    Return True only for URLs that resolve to an actual image file.

    Google Ads Transparency has two kinds of previewUrl:
      - Real images: tpc.googlesyndication.com/archive/simgad/<id>  (no ext, but real JPEG)
      - JS renderers: displayads-formats.googleusercontent.com/.../content.js?...
                      (downloads JavaScript bytes, not an image)

    Heuristic, in order:
      1. Falsy/empty → False
      2. 'content.js' anywhere in the URL → False (JS renderer)
      3. 'simgad' or '/archive/' in URL → True (known real-image patterns)
      4. Path before '?' ends with a known image extension → True
      5. Otherwise → False (conservative; better to skip than to OCR JS)
    """
    if not url:
        return False
    if "content.js" in url:
        return False
    if "simgad" in url or "/archive/" in url:
        return True
    path = url.split("?")[0].lower()
    return path.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp"))


def has_downloadable_image(record: dict) -> bool:
    """
    Return True if a Google Ads record has at least one real downloadable image.

    Checks previewUrl first, then variants[].images[]. Shares the
    _is_real_image_url predicate so the download filter and the selector
    use an identical definition of 'eligible'.
    """
    if _is_real_image_url(record.get("previewUrl")):
        return True
    for variant in (record.get("variants") or []):
        if not isinstance(variant, dict):
            continue
        for img in (variant.get("images") or []):
            if isinstance(img, str):
                if _is_real_image_url(img):
                    return True
            elif isinstance(img, dict):
                url = img.get("url") or img.get("imageUrl") or img.get("src")
                if _is_real_image_url(url):
                    return True
    return False


def _google_creative_urls(record: dict) -> list[tuple[str, str]]:
    """
    Return (url, default_ext) pairs from a Google Ads record.
    Covers top-level previewUrl and variants[].images[].
    Only includes URLs that pass _is_real_image_url — content.js renderer
    URLs are silently skipped so they are never saved as bogus .jpg files.
    """
    seen: set[str] = set()
    results: list[tuple[str, str]] = []

    def _add(url, ext):
        if url and _is_real_image_url(url) and url not in seen:
            seen.add(url)
            results.append((url, ext))

    _add(record.get("previewUrl"), ".jpg")

    for variant in record.get("variants") or []:
        if not isinstance(variant, dict):
            continue
        for img in variant.get("images") or []:
            # images[] contains plain URL strings in the observed actor output
            if isinstance(img, str):
                _add(img, ".jpg")
            elif isinstance(img, dict):
                # guard for future actor schema changes
                url = img.get("url") or img.get("imageUrl") or img.get("src")
                _add(url, ".jpg")

    return results


def download_google_creatives(
    google_items: list[dict],
    out_dir: str,
    prev_creatives_dir: str | None = None,
) -> dict:
    """
    Fetch all creative images from a list of Google Ads Transparency records.

    Args:
        google_items:      list of ad records as returned by the Apify actor.
        out_dir:           root output directory. Files go under <out_dir>/google/.
        prev_creatives_dir: path to the previous run's creatives/ folder, or None.

    Returns:
        manifest dict  {creativeId: [filename_or_"FAILED: reason", ...]}
        Also writes the manifest to <out_dir>/google/manifest.json.
        Also writes <out_dir>/google/sources.json  (filename -> URL path).
        Also writes <out_dir>/google/reuse.json    (linked/copied/downloaded/failed counts).
    """
    dest_root = Path(out_dir) / "google"
    dest_root.mkdir(parents=True, exist_ok=True)

    prev_platform_dir = Path(prev_creatives_dir) / "google" if prev_creatives_dir else None
    prev_sources = _load_prev_sources(prev_platform_dir)
    own_sources = _load_own_sources(dest_root)
    reuse_counts: dict[str, int] = {"linked": 0, "copied": 0, "downloaded": 0, "failed": 0,
                                    "kept": 0}

    manifest: dict[str, list[str]] = {}
    sources: dict[str, str] = {}

    for record in google_items:
        if not isinstance(record, dict):
            continue
        advertiser_id = str(record.get("advertiserId", "unknown"))
        creative_id = str(record.get("creativeId", "unknown"))
        key = creative_id  # unique per record; use as manifest key

        urls = _google_creative_urls(record)

        results = [
            _slot("google", creative_id, f"{advertiser_id}_{creative_id}", i, url,
                  default_ext, dest_root, prev_platform_dir, prev_sources, own_sources,
                  sources, reuse_counts)
            for i, (url, default_ext) in enumerate(urls)
        ]

        manifest[key] = results

    manifest_path = dest_root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (dest_root / "sources.json").write_text(
        json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (dest_root / "reuse.json").write_text(
        json.dumps(reuse_counts, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    saved = sum(
        1
        for v in manifest.values()
        for s in v
        if not s.startswith("FAILED")
    )
    print(f"   → Google: {saved}/{sum(len(v) for v in manifest.values())} creatives saved")
    return manifest


# ---------------------------------------------------------------------------
# LinkedIn Ad Library
# ---------------------------------------------------------------------------

def _linkedin_creative_urls(record: dict) -> list[tuple[str, str]]:
    """
    Return (url, default_ext) pairs from a LinkedIn ad record.

    Covers the three media fields the actor populates:
      imageUrl    — single image (always present for image ads, also used as
                    the video thumbnail cover for VIDEO / AI format ads)
      imageUrls   — list of images (used when the ad contains multiple images)
      videoUrl    — video stream URL (present only for VIDEO / AI format ads)

    All URLs are media.licdn.com or dms.licdn.com. Some carry real expiry
    tokens (e=<epoch>) so download immediately after scraping.
    Dedupes via seen-set so imageUrl appearing again inside imageUrls is only
    fetched once.
    """
    seen: set[str] = set()
    results: list[tuple[str, str]] = []

    def _add(url, ext):
        if url and url not in seen:
            seen.add(url)
            results.append((url, ext))

    _add(record.get("imageUrl"), ".jpg")
    for url in record.get("imageUrls") or []:
        if isinstance(url, str):
            _add(url, ".jpg")
    _add(record.get("videoUrl"), ".mp4")

    return results


def download_linkedin_creatives(
    li_items: list[dict],
    out_dir: str,
    prev_creatives_dir: str | None = None,
) -> dict:
    """
    Fetch all creative images/videos from a list of LinkedIn Ad Library records.

    Args:
        li_items:          list of ad records as returned by scrapesage/linkedin-ad-library-scraper.
        out_dir:           root output directory. Files go under <out_dir>/linkedin/.
        prev_creatives_dir: path to the previous run's creatives/ folder, or None.

    Returns:
        manifest dict  {adId: [filename_or_"FAILED: reason", ...]}
        Also writes the manifest to <out_dir>/linkedin/manifest.json.
        Also writes <out_dir>/linkedin/sources.json  (filename -> URL path).
        Also writes <out_dir>/linkedin/reuse.json    (linked/copied/downloaded/failed counts).

    Impression/duration fields (totalImpressionsMax, firstShownAt, lastShownAt)
    are often null for VIDEO ads — the download never crashes on missing values;
    those are handled by e3_report when building the digest.
    """
    dest_root = Path(out_dir) / "linkedin"
    dest_root.mkdir(parents=True, exist_ok=True)

    prev_platform_dir = Path(prev_creatives_dir) / "linkedin" if prev_creatives_dir else None
    prev_sources = _load_prev_sources(prev_platform_dir)
    own_sources = _load_own_sources(dest_root)
    reuse_counts: dict[str, int] = {"linked": 0, "copied": 0, "downloaded": 0, "failed": 0,
                                    "kept": 0}

    manifest: dict[str, list[str]] = {}
    sources: dict[str, str] = {}

    for record in li_items:
        if not isinstance(record, dict):
            continue
        ad_id = str(record.get("adId", "unknown"))
        urls = _linkedin_creative_urls(record)

        results = [
            _slot("linkedin", ad_id, ad_id, i, url, default_ext, dest_root,
                  prev_platform_dir, prev_sources, own_sources, sources, reuse_counts)
            for i, (url, default_ext) in enumerate(urls)
        ]

        manifest[ad_id] = results

    manifest_path = dest_root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (dest_root / "sources.json").write_text(
        json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (dest_root / "reuse.json").write_text(
        json.dumps(reuse_counts, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    saved = sum(
        1
        for v in manifest.values()
        for s in v
        if not s.startswith("FAILED")
    )
    print(f"   → LinkedIn: {saved}/{sum(len(v) for v in manifest.values())} creatives saved")
    return manifest
