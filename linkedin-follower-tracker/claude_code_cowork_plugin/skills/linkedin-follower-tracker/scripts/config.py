"""
Credential + launch config for the live PhantomBuster run.

Two sources of configuration are combined here:

  scripts/.env (gitignored)
    PHANTOMBUSTER_API_KEY   required   X-Phantombuster-Key on every API call
    LINKEDIN_COOKIE         optional   override the live-fetched li_at session cookie
    LINKEDIN_USER_AGENT     optional   override the UA matched to the cookie's browser
    PB_IDENTITY_ID          optional   override the PhantomBuster identity id

  FOLLOWER_CONFIG.json (per-subject, in the run directory — see config/FOLLOWER_CONFIG.example.json)
    collector_id  required   agent id of the follower-collector phantom
    scraper_id    required   agent id of the profile-scraper phantom
    csv_name      optional   collector output-CSV label; may contain `{date}` token
    enrich_list_name optional org-storage enrichment list name; may contain `{date}` and `{time}`
    adds_per_launch optional  scraper batch size (default 500)
    subject_label optional   free-text label for the notification message (e.g. "ACME CEO")

Nothing here is needed for the synthetic backends (fake/http) — they invent their own
ids and ignore the cookie — so the offline rehearsal never touches this path. It matters
only for `--backend real`, the one run that spends money, which is exactly where a blank
key or a missing agent id must fail loudly and early rather than mid-launch.

Stdlib only. Never echoes, logs, or commits a value — see `redacted()` for the only
representation of a config that is allowed to be printed.

The session (cookie / userAgent / identityId) is NOT required here: the client fetches
the freshest session live from each agent (agents/fetch) right before every launch, so
nothing session-related needs to be stored. The LINKEDIN_* / PB_IDENTITY_ID vars remain
only as OPTIONAL overrides for edge cases (e.g. debugging), never the normal path.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


def default_env_path() -> Path:
    """scripts/.env, next to this module."""
    return Path(__file__).resolve().parent / ".env"


def load_env(path: str | os.PathLike | None = None, *, override: bool = False) -> int:
    """
    Parse a KEY=VALUE .env file into os.environ and return how many keys were set.

    Missing file -> 0 (silent): the offline backends need no .env, so its absence is
    normal, not an error. Only get_pb_config() decides a value is *required*.

    Precedence: by default an already-set environment variable WINS over the file
    (override=False). That is deliberate — it lets a one-off `PB_LIVE=1
    PHANTOMBUSTER_API_KEY=… python3 …` invocation take effect without editing the
    file, matching how every dotenv loader behaves.
    """
    p = Path(path) if path else default_env_path()
    if not p.exists():
        return 0
    count = 0
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")  # tolerate KEY="value" and KEY='value'
        if not key:
            continue
        if override or key not in os.environ:
            os.environ[key] = value
            count += 1
    return count


def _load_follower_config(config_path: str | os.PathLike | None) -> dict:
    """
    Read the FOLLOWER_CONFIG.json file and return its contents as a dict.

    Missing file -> {} (silent): the offline backends need no config file, so its
    absence is normal when not running the real backend. get_pb_config() validates
    the required keys and exits with a clear message if any are missing.
    """
    if not config_path:
        return {}
    p = Path(config_path)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class PBConfig:
    api_key: str
    collector_id: str
    scraper_id: str
    cookie: str | None
    user_agent: str | None
    identity_id: str | None
    adds_per_launch: int
    # Naming conventions and subject identity — per-subject labels, not secrets.
    # Kept in FOLLOWER_CONFIG.json (not hardcoded in the shipped code) so an account's
    # own convention lives beside its agent ids, and the distributable tool ships with
    # only generic placeholders. csv_name may contain a literal `{date}` token,
    # substituted with the run date (yyyy-mm-dd). enrich_list_name additionally supports
    # a `{time}` token (hh:mm:ss), so every kept enrichment list gets a unique, traceable
    # name and a same-day re-run cannot collide.
    csv_name: str
    enrich_list_name: str
    # Optional free-text label for the notification message (e.g. "ACME CEO").
    # When absent the notification uses the generic "LinkedIn followers" prefix.
    subject_label: str | None

    def redacted(self) -> dict:
        """The ONLY printable view of the config: presence, not values. Secrets are
        shown as their length so an operator can sanity-check 'the cookie is there
        and looks the right size' without the value ever entering a log or transcript."""
        def mark(v: str | None) -> str:
            return f"set ({len(v)} chars)" if v else "MISSING"
        return {
            "PHANTOMBUSTER_API_KEY": mark(self.api_key),
            "collector_id": self.collector_id or "MISSING",   # ids are not secret
            "scraper_id": self.scraper_id or "MISSING",
            # session is fetched live per launch; these are optional overrides only
            "LINKEDIN_COOKIE": mark(self.cookie) if self.cookie else "(fetched live)",
            "LINKEDIN_USER_AGENT": mark(self.user_agent) if self.user_agent else "(fetched live)",
            "PB_IDENTITY_ID": self.identity_id or "(fetched live)",
            "adds_per_launch": self.adds_per_launch,
            # naming conventions are labels, not secrets — shown in full
            "csv_name": self.csv_name,
            "enrich_list_name": self.enrich_list_name,
            "subject_label": self.subject_label or "(not set)",
        }


# Required from the environment (secrets only — ids and labels live in the config JSON).
_REQUIRED_ENV = {
    "PHANTOMBUSTER_API_KEY": "your PhantomBuster API key (Console -> Settings -> API)",
}

# Required in the FOLLOWER_CONFIG.json file (agent ids that are per-subject, not secrets).
_REQUIRED_CFG = {
    "collector_id": "the follower-collector phantom's agent id",
    "scraper_id": "the profile-scraper phantom's agent id",
}


def get_pb_config(env_path: str | os.PathLike | None = None,
                  config_path: str | os.PathLike | None = None) -> PBConfig:
    """
    Load scripts/.env (for the API key) and FOLLOWER_CONFIG.json (for per-subject values),
    then return a validated PBConfig, or exit with a clear, value-free message naming
    exactly which keys are missing. Called only by the real backend — the money path —
    so the failure has to be unmissable and must never leak a partial value.
    """
    load_env(env_path)

    # Per-subject config: agent ids, naming conventions, optional label.
    cfg = _load_follower_config(config_path)

    missing_env = [k for k in _REQUIRED_ENV if not os.environ.get(k, "").strip()]
    if missing_env:
        lines = "\n".join(f"  - {k}: {_REQUIRED_ENV[k]}" for k in missing_env)
        raise SystemExit(
            "Missing required environment credential for the live run:\n"
            f"{lines}\n\n"
            "Fix: copy scripts/.env.example to scripts/.env and fill these in. "
            "The .env is gitignored and never committed."
        )

    missing_cfg = [k for k in _REQUIRED_CFG if not str(cfg.get(k, "")).strip()]
    if missing_cfg:
        lines = "\n".join(f"  - {k}: {_REQUIRED_CFG[k]}" for k in missing_cfg)
        cfg_hint = (f" (at {config_path})" if config_path else
                    " (no --config path was given)")
        raise SystemExit(
            f"Missing required fields in FOLLOWER_CONFIG.json{cfg_hint}:\n"
            f"{lines}\n\n"
            "Fix: copy config/FOLLOWER_CONFIG.example.json to your run directory's "
            "config.json and fill in the agent ids."
        )

    adds_raw = cfg.get("adds_per_launch")
    try:
        adds = int(adds_raw) if adds_raw is not None else 500
    except (ValueError, TypeError):
        raise SystemExit(f"adds_per_launch must be an integer, got {adds_raw!r}")

    csv_name = str(cfg.get("csv_name", "")).strip() or "followers - {date}"
    enrich_list_name = (str(cfg.get("enrich_list_name", "")).strip()
                        or "linkedin-follower-tracker enrichment {date} {time}")
    subject_label = str(cfg.get("subject_label", "")).strip() or None

    return PBConfig(
        api_key=os.environ["PHANTOMBUSTER_API_KEY"].strip(),
        collector_id=str(cfg["collector_id"]).strip(),
        scraper_id=str(cfg["scraper_id"]).strip(),
        cookie=(os.environ.get("LINKEDIN_COOKIE", "").strip() or None),
        user_agent=(os.environ.get("LINKEDIN_USER_AGENT", "").strip() or None),
        identity_id=(os.environ.get("PB_IDENTITY_ID", "").strip() or None),
        adds_per_launch=adds,
        csv_name=csv_name,
        enrich_list_name=enrich_list_name,
        subject_label=subject_label,
    )
