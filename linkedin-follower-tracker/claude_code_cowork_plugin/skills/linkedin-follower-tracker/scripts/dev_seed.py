"""
Seed data for the synthetic PhantomBuster used by the fake and HTTP rehearsal backends.

Turns synthetic fixture data into the three datasets the look-alike serves:
  - collector "current followers"  (what the follower collector returns)
  - scraper   "enrichment records" (what the profile scraper returns)
  - previous  "master"             (the starting state, as if from a prior run)

Two modes, because they answer different questions:

  - "sample" (default): the committed synthetic fixtures. Small (3 new, 1 lost, 2
    unchanged), self-consistent, and runnable by anyone with the repo. Best for a
    quick end-to-end rehearsal and for CI-style checks.

  - "scale": production-volume. Builds a large, self-consistent scenario (a realistic
    volume of 4069 prior / 247 new / 58 lost followers) so timing, large-output
    downloads, and memory use can be rehearsed at real size. Padding people are
    generated deterministically and CLEARLY MARKED synthetic (names/URNs prefixed
    "syn"), so no fabricated row is ever mistaken for a real one. Field shapes come
    from the committed sample fixtures; only the bulk is padded and labelled.
"""

from __future__ import annotations

import json
from pathlib import Path

from pb_fake import PhantomBusterFake, FakeAgent
from pipeline import extract_urn

_HERE = Path(__file__).resolve().parent
FIXTURES = _HERE.parent / "tests" / "fixtures"

COLLECTOR_AGENT_ID = "DEV_COLLECTOR"
SCRAPER_AGENT_ID = "DEV_SCRAPER"


def _load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Sample scenario: the committed fixtures. Master rows already carry the sheet
# column names; collector/scraper carry their phantom shapes.
# ---------------------------------------------------------------------------

def sample_scenario() -> dict:
    return {
        "collector": _load(FIXTURES / "collector_current.json", []),
        "scraper": _load(FIXTURES / "scraper_enrichment.json", []),
        "master_prev": _load(FIXTURES / "master_prev.json", []),
    }


# ---------------------------------------------------------------------------
# Scale scenario: production-volume, synthetic data, realistic field shapes from fixtures.
# ---------------------------------------------------------------------------

def scale_scenario(existing: int = 4000, new: int = 247, lost: int = 58) -> dict:
    """
    Build a large, self-consistent scenario:
      - `existing` prior followers in the master,
      - of those, `lost` are absent from the current pull (they unfollowed),
      - `new` genuinely new people appear in the current pull.
    Current pull = (existing - lost) + new. Enrichment covers the `new`.

    All data is deterministically generated using the committed sample fixtures as
    field templates. Padding is clearly marked synthetic.
    """
    real_lost_urls: list[str] = []
    collector_template = _load(FIXTURES / "collector_current.json", [])
    scraper_template = _load(FIXTURES / "scraper_enrichment.json", [])

    # Existing followers (the prior master). Give the first `lost` of them the real
    # lost URLs where we have them, so the lost set is genuine.
    master_prev = []
    existing_urls = []
    for i in range(existing):
        if i < len(real_lost_urls) and i < lost:
            url = real_lost_urls[i]
        else:
            url = f"https://www.linkedin.com/in/ACoAA_syn_exist_{i}"
        existing_urls.append(url)
        master_prev.append(_master_row(collector_template, i, url, existing=True))

    # Current pull: keep everyone except the first `lost`, then add `new` newcomers.
    kept = existing_urls[lost:]
    current = [_collector_rec(collector_template, i, url)
               for i, url in enumerate(kept)]
    new_urls = [f"https://www.linkedin.com/in/ACoAA_syn_new_{j}" for j in range(new)]
    current += [_collector_rec(collector_template, existing + j, url)
                for j, url in enumerate(new_urls)]

    scraper = [_scraper_rec(scraper_template, j, url) for j, url in enumerate(new_urls)]

    return {"collector": current, "scraper": scraper, "master_prev": master_prev}


def _template_at(template: list[dict], i: int) -> dict:
    return dict(template[i % len(template)]) if template else {}


def _collector_rec(template, i, url) -> dict:
    rec = _template_at(template, i)
    rec.update({
        "profileLink": url,
        "fullName": f"Syn Follower {i}",
        "firstName": "Syn", "lastName": f"Follower{i}",
        "isFollowing": False,
    })
    return rec


def _scraper_rec(template, j, url) -> dict:
    rec = _template_at(template, j)
    rec.update({
        "profileUrl": url,
        # Keep the synthetic identity self-consistent: the URN must match this row's
        # URL, or the URN-keyed join silently drops it. The fixture template carries
        # a real URN/id, so overwrite the URN from the synthetic URL and drop the id.
        "linkedinProfileUrn": extract_urn(url),
        "firstName": "Syn", "lastName": f"New{j}",
        "companyName": f"Syn Company {j}",
        "companyIndustry": ["Software", "Insurance", "Manufacturing", "Marketing & Advertising"][j % 4],
        "linkedinJobTitle": ["Head of Sales", "Analyst", "VP Marketing", "Student"][j % 4],
        # Deep company-page fields — present ONLY in the scraper's container output, never
        # in the org-storage lead read-back. Seeded so the dev/HTTP e2e demonstrates them
        # flowing into the master (the 2026-09-02 read-back fix).
        "linkedinCompanyName": f"Syn Company {j} Inc",
        "linkedinCompanyFollowerCount": 1000 + j,
    })
    rec.pop("linkedinProfileId", None)
    return rec


def _master_row(template, i, url, existing=True) -> dict:
    return {
        "classification": ["Buyer", "Not a fit"][i % 2],
        "scrapeRound": "2026-08-01",
        "fullName": f"Syn Existing {i}",
        "profileUrl": url,
        "isFollowing": "false",
        "companyName": f"Syn Company {i}",
        "lost_on": "",
    }


# ---------------------------------------------------------------------------
# Build a seeded look-alike from a scenario.
# ---------------------------------------------------------------------------

def build_fake(scenario: dict, *, running_polls: int = 1,
               collector_large: bool = True, scraper_large: bool = False,
               collector_runtime: float = 0.0, scraper_runtime: float = 0.0,
               file_url_base: str = "pbfake://files/") -> PhantomBusterFake:
    """
    Register the collector and scraper agents on a fresh look-alike.
    collector_large defaults True because real collector outputs are typically large
    (delivered by URL); scraper output is typically inline.
    collector_runtime / scraper_runtime (seconds) model the real phantom durations
    (~1h collector, ~40-45m scraper), compressed; 0 = instant (poll-count timeline).
    """
    fake = PhantomBusterFake(running_polls=running_polls, file_url_base=file_url_base)
    fake.register(FakeAgent(COLLECTOR_AGENT_ID, "collector", scenario["collector"],
                            large=collector_large, runtime_seconds=collector_runtime,
                            session_cookie="DEV_FRESH_COLLECTOR_COOKIE",
                            user_agent="DevUA/collector"))
    fake.register(FakeAgent(SCRAPER_AGENT_ID, "scraper", scenario["scraper"],
                            large=scraper_large, runtime_seconds=scraper_runtime,
                            session_cookie="DEV_FRESH_SCRAPER_COOKIE",
                            user_agent="DevUA/scraper", identity_id="DEV_IDENTITY"))
    return fake


def load_scenario(mode: str = "sample", **kwargs) -> dict:
    if mode == "scale":
        return scale_scenario(**kwargs)
    return sample_scenario()
