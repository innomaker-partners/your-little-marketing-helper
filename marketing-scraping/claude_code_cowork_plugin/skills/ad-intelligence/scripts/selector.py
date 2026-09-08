"""
OCR scope selector — picks which Google ads to transcribe.

Google Ads Transparency never exposes impression counts, so ad DURATION
(lastShownAt - firstShownAt, in seconds) is the best available proxy for
performance: an ad that kept running for 400 days was profitable enough not
to kill. OCR is fast but still takes wall-clock time; concentrating it on
the longest-running 20% of ELIGIBLE ads targets the copy worth reading.

Eligibility: an ad must have at least one real downloadable image
(has_downloadable_image from creatives). Ads whose previewUrl is a JS
renderer (content.js) are excluded before ranking — they produce no image
to transcribe. This keeps the selection pure: every returned creativeId
maps to an image the downloader will actually save.

Both timestamps arrive as Unix-epoch-second STRINGS and must be cast to int.

Stdlib only — no pip install needed.
"""

import math

# has_downloadable_image is imported from the shared scripts path
# (ad_intel.py inserts scripts/ into sys.path before importing this module).
from creatives import has_downloadable_image


def _duration_seconds(item: dict) -> int:
    """
    Compute ad duration in seconds from a Google Ads Transparency record.

    Casts firstShownAt / lastShownAt from string to int (the actor emits
    epoch-second strings). Returns max(0, last - first). Returns 0 for any
    unparseable or missing value so those ads sort to the bottom of the ranking.
    """
    try:
        first = int(item.get("firstShownAt", 0) or 0)
        last  = int(item.get("lastShownAt",  0) or 0)
        return max(0, last - first)
    except (TypeError, ValueError):
        return 0


def top_creative_ids_by_duration(
    google_items: list,
    fraction: float = 0.2,
    min_keep: int = 1,
) -> set:
    """
    Return the set of creativeId values for the top `fraction` of image-eligible
    ads by duration.

    Eligibility: has_downloadable_image() must be True — ads with only a JS-
    renderer previewUrl (content.js) are excluded before ranking. This ensures
    every returned id maps to an image the downloader will actually save.

    Ranking is by _duration_seconds descending over the eligible set; ties
    broken by list order. Eligible ads with 0 / unparseable duration sort last
    but are still eligible.

    n = max(min_keep, ceil(len(eligible) * fraction))

    Args:
        google_items: list of Google Ads Transparency records (dicts).
        fraction:     Fraction of ELIGIBLE records to keep (0.0–1.0). Default 0.2.
        min_keep:     Minimum ids to return. Prevents an empty set on tiny lists.

    Returns:
        set of str — creativeId values matching the manifest key produced by
        creatives.download_google_creatives.
    """
    eligible = [
        item for item in google_items
        if isinstance(item, dict)
        and (item.get("creativeId") or item.get("id"))
        and has_downloadable_image(item)
    ]
    if not eligible:
        return set()

    ranked = sorted(eligible, key=_duration_seconds, reverse=True)
    n = max(min_keep, math.ceil(len(ranked) * fraction))
    selected = ranked[:n]
    return {str(item.get("creativeId") or item.get("id")) for item in selected}

