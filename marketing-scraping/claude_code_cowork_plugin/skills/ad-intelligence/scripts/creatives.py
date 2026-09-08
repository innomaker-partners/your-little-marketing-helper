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
"""

import json
import mimetypes
import os
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


def _fetch(url: str, dest: str) -> None:
    """
    GET *url* with a browser-like User-Agent, write bytes to *dest*.
    Raises OSError / urllib.error.URLError on any failure so callers can
    catch and log rather than crash.
    """
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        data = resp.read()
    if not data:
        raise OSError("Empty response body")
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    Path(dest).write_bytes(data)


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


def download_meta_creatives(meta_items: list[dict], out_dir: str) -> dict:
    """
    Fetch all creative images/videos from a list of Meta Ad Library records.

    Args:
        meta_items: list of ad records as returned by the Apify actor.
        out_dir:    root output directory. Files go under <out_dir>/meta/.

    Returns:
        manifest dict  {ad_archive_id: [filename_or_"FAILED: reason", ...]}
        Also writes the manifest to <out_dir>/meta/manifest.json.
    """
    dest_root = Path(out_dir) / "meta"
    dest_root.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, list[str]] = {}

    for record in meta_items:
        if not isinstance(record, dict):
            continue
        ad_id = str(record.get("ad_archive_id", "unknown"))
        snapshot = record.get("snapshot") or {}
        urls = _meta_creative_urls(snapshot)

        results: list[str] = []
        for i, (url, default_ext) in enumerate(urls):
            dest_no_ext = str(dest_root / f"{ad_id}_{i}")
            outcome = _save(url, dest_no_ext, default_ext)
            results.append(outcome)
            status = "saved" if not outcome.startswith("FAILED") else outcome
            print(f"   meta {ad_id}[{i}] → {status}")

        manifest[ad_id] = results

    manifest_path = dest_root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
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


def download_google_creatives(google_items: list[dict], out_dir: str) -> dict:
    """
    Fetch all creative images from a list of Google Ads Transparency records.

    Args:
        google_items: list of ad records as returned by the Apify actor.
        out_dir:      root output directory. Files go under <out_dir>/google/.

    Returns:
        manifest dict  {creativeId: [filename_or_"FAILED: reason", ...]}
        Also writes the manifest to <out_dir>/google/manifest.json.
    """
    dest_root = Path(out_dir) / "google"
    dest_root.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, list[str]] = {}

    for record in google_items:
        if not isinstance(record, dict):
            continue
        advertiser_id = str(record.get("advertiserId", "unknown"))
        creative_id = str(record.get("creativeId", "unknown"))
        key = creative_id  # unique per record; use as manifest key

        urls = _google_creative_urls(record)

        results: list[str] = []
        for i, (url, default_ext) in enumerate(urls):
            dest_no_ext = str(dest_root / f"{advertiser_id}_{creative_id}_{i}")
            outcome = _save(url, dest_no_ext, default_ext)
            results.append(outcome)
            status = "saved" if not outcome.startswith("FAILED") else outcome
            print(f"   google {creative_id}[{i}] → {status}")

        manifest[key] = results

    manifest_path = dest_root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
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


def download_linkedin_creatives(li_items: list[dict], out_dir: str) -> dict:
    """
    Fetch all creative images/videos from a list of LinkedIn Ad Library records.

    Args:
        li_items: list of ad records as returned by scrapesage/linkedin-ad-library-scraper.
        out_dir:  root output directory. Files go under <out_dir>/linkedin/.

    Returns:
        manifest dict  {adId: [filename_or_"FAILED: reason", ...]}
        Also writes the manifest to <out_dir>/linkedin/manifest.json.

    Impression/duration fields (totalImpressionsMax, firstShownAt, lastShownAt)
    are often null for VIDEO ads — the download never crashes on missing values;
    those are handled by e3_report when building the digest.
    """
    dest_root = Path(out_dir) / "linkedin"
    dest_root.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, list[str]] = {}

    for record in li_items:
        if not isinstance(record, dict):
            continue
        ad_id = str(record.get("adId", "unknown"))
        urls = _linkedin_creative_urls(record)

        results: list[str] = []
        for i, (url, default_ext) in enumerate(urls):
            dest_no_ext = str(dest_root / f"{ad_id}_{i}")
            outcome = _save(url, dest_no_ext, default_ext)
            results.append(outcome)
            status = "saved" if not outcome.startswith("FAILED") else outcome
            print(f"   linkedin {ad_id}[{i}] → {status}")

        manifest[ad_id] = results

    manifest_path = dest_root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    saved = sum(
        1
        for v in manifest.values()
        for s in v
        if not s.startswith("FAILED")
    )
    print(f"   → LinkedIn: {saved}/{sum(len(v) for v in manifest.values())} creatives saved")
    return manifest
