"""
Config loader + empirical cost model for the search-intelligence tool.

SEARCH_CONFIG.json (copy from SEARCH_CONFIG.example.json) is the single file that
drives every stage. This module loads + validates it and exposes the empirical
Apify rates measured by direct measurement (verify current pricing at apify.com/pricing).

Stdlib only — no pip install needed.

COST HONESTY: this tool meters per keyword and a real run EXCEEDS Apify's free
monthly credit — a card is required. The forecast-before-spend gate prints the
estimate before every run; QUICKSTART states this up front. This tool is
funded-not-free, and it never calls itself "free".
"""

import json
import math
import os
import sys
from pathlib import Path

# Python floor: a Mac's own python3 is 3.9. On anything older, say so in one
# plain line instead of failing later with a traceback.
if sys.version_info < (3, 9):
    sys.exit("This tool needs Python 3.9 or newer, and this is Python %d.%d. "
             "A Mac already has a new enough one: /usr/bin/python3" % sys.version_info[:2])

# ---------------------------------------------------------------------------
# Empirical per-unit Apify cost (USD), measured on pilot test runs INCLUDING
# platform overhead. Treat as a STARTING estimate — your first test run
# recalibrates the rate for your own market/account (run_phase.py does this
# automatically). Only the three stages this tool uses.
# ---------------------------------------------------------------------------
#
# FLOOR RULE: every rate here is at
# least the highest price observed for it, on any Apify plan tier: the forecast
# is a spending gate and must never be low, so a user on a cheaper plan sees an
# estimate a little above their bill rather than one below it. Never lower a
# rate. Observed = a real run's pricingInfo, or the actor's published pricing
# (its FREE tier, the highest). A development test holds the observed table with its sources and
# fails if a rate drops below it.
# ---------------------------------------------------------------------------
EMPIRICAL_RATES = {
    # stage -> (actor, unit, usd_per_unit, unverified?)
    # A1 keyword expansion. Unit is the expanded keyword. (The est_count
    # heuristic in build_A1 assumes ~30/seed, which over-forecasts vs the
    # observed ~13/seed - conservative for the gate.) memo23/google-suggest-
    # scraper replaced the withdrawn crawlerbros actor. Its one
    # row price is $0.0005 ("apify-default-dataset-item"): a real run (pricingInfo) and the public
    # actor record (pricingInfos)
    # show the same single price, with no plan tiers, so there is no higher tier
    # to take. The old actor's $0.002 does not carry over: a rate floor belongs
    # to its actor.
    "A1": ("memo23/google-suggest-scraper", "keyword", 0.0005, False),
    # A2 keyword volume/difficulty/CPC (Semrush scrape). The paid runs were priced $0.005 per
    # keyword; the FREE tier is $0.015 (was
    # 0.0048, below both). This actor caps at ~100 keywords/run — run_phase's
    # batching (STAGES["A2"]["batch"]) chunks larger lists automatically.
    "A2": ("pro100chok/semrush-scraper", "keyword", 0.015, False),
    # B1 SERP. $0.0076/query verified on the kit/ad-intel pilots (same actor).
    # One results page per query; highest observed page price $0.0045 (FREE
    # tier), so 0.0076 stays (above every observed price).
    "B1": ("apify/google-search-scraper", "query", 0.0076, False),
}

# ---------------------------------------------------------------------------
# Fixed charges per actor RUN ("start" events), priced on top of the per-unit
# rates above. WHY: a paid validation run billed more
# than its forecast because these were left out: Trustpilot's $0.005 start,
# Reddit's $0.02-per-GB init, and the per-GB apify-actor-start of the Maps,
# G2, Capterra and keyword-expander actors. With them out, a small stage's own
# start fee alone could break its spend cap (forecast x CHARGE_CEILING_FACTOR:
# Trustpilot at 5 reviews or fewer, Capterra at 12 or fewer), so it stopped at once.
# Source: each actor's pricing on Apify and the ledgers of paid runs. A per-GB event is charged
# once per GB of the
# memory the run is given, minimum one; the tool requests run_phase's default
# memory (DEFAULT_MEMORY_MB) unless a stage sets less (STAGE_MEMORY_MB), so it
# is priced at that. Apify gave some actors less than requested (the ledgers
# show G2 2, Capterra 1, Reddit 2 events at 4096 MB), so these figures are an
# upper bound: a forecast is a spending gate and must not be low.
# stage -> (event name, USD per event, charged per GB of run memory?)
# ---------------------------------------------------------------------------
START_EVENTS = {
    "A1": ("apify-actor-start", 0.005, True),  # memo23: "one event per GB, minimum one"
    "B1": ("actor-start",       0.001, False),  # "Flat fee for starting an Actor run"
    # A2 (semrush): no start charge.
}
# A1 asks 512 MB: the only size measured (one start event, $0.005, on a real run). At 4096 MB the
# start fee would be about 4 times larger,
# which nobody measured. Mirrors stages.STAGES["A1"]["memory_mb"] (the memory the
# run is actually given); a test holds the two together.
STAGE_MEMORY_MB = {"A1": 512}

DEFAULT_MEMORY_MB = 4096  # run_phase._mem_for's default when a stage sets none


def start_fee_usd(stage, memory_mb=None):
    """USD one run of this stage's actor is charged for starting, before any
    record. 0.0 for an actor with no start charge."""
    ev = START_EVENTS.get(stage)
    if not ev:
        return 0.0
    _event, usd, per_gb = ev
    if not per_gb:
        return usd
    mem = memory_mb or STAGE_MEMORY_MB.get(stage, DEFAULT_MEMORY_MB)
    return usd * max(1, -(-int(mem) // 1024))

# Result volumes ran high in the pilot; pad every forecast.
SAFETY_MARGIN = 1.2

# Spend ceiling: the hard cap passed to Apify as maxTotalChargeUsd -- and the
# threshold for our own poll-and-abort -- is forecast x this factor. The forecast
# already carries SAFETY_MARGIN (1.2); this rides on top, so the ceiling sits
# above a normal run's variance yet still catches a runaway (a forecast that is
# >1.5x too low is stopped). maxTotalChargeUsd is a real platform hard cap,
# verified live: a run stopped at $0.0596 under a $0.06 cap.
CHARGE_CEILING_FACTOR = 1.5

REQUIRED_KEYS = ["country_code"]


def repo_default_config_path():
    """SEARCH_CONFIG.json sitting next to the module's parent folder."""
    return Path(__file__).resolve().parent.parent / "SEARCH_CONFIG.json"


def load_config(path=None):
    """Load + validate a SEARCH_CONFIG.json."""
    p = Path(path) if path else repo_default_config_path()
    if not p.exists():
        raise SystemExit(
            f"No config at {p}. Copy SEARCH_CONFIG.example.json to "
            f"SEARCH_CONFIG.json and fill it in."
        )
    with open(p, encoding="utf-8") as f:
        cfg = json.load(f)

    missing = [k for k in REQUIRED_KEYS if not cfg.get(k)]
    if missing:
        raise SystemExit(f"Config {p} is missing required keys: {', '.join(missing)}")

    # Defaults for optional keys.
    cfg.setdefault("language", "en")
    cfg.setdefault("seed_terms", [])          # input mode 1: keywords you already know
    cfg.setdefault("vertical", "")            # input mode 2: a market label to expand from
    cfg.setdefault("location", "")            # optional: localizes the SERP (→ UULE)
    cfg.setdefault("geo_uule", "")            # precomputed UULE string; blank => country-only SERP
    cfg.setdefault("keyword_cap", 300)        # full-run cap on the expanded keyword universe
    cfg.setdefault("serp_top_n", 100)         # how many top keywords get a SERP pull
    # 8.0 holds the forecast of a run at these defaults (300 keywords, 100
    # SERP pulls: $6.730 at the floor rates above with ten seed terms, the test
    # run of each step included; it was $6.498 before the test runs
    # were counted and $7.053 until the keyword-ideas actor changed); a default that refused its own
    # default-sized run teaches
    # raising caps blindly.
    cfg.setdefault("budget_caps", {"phase1": 8.0})
    cfg.setdefault("stages", [])              # task allow-list; empty => run all defaults
    _read_numbers(cfg, p, _NUMBER_SETTINGS)
    _read_budget_caps(cfg, p, 8.0)  # the default set above
    return cfg


# --- number settings ------------------------------------
# The same three functions in all three tools' config.py (test_client_drift).
# What the user wrote for a number setting that counts as "not set".
_BLANK = (None, "")


def _read_number(cfg, key, default, path, low=0, high=None, whole=True, why="", name=None):
    """Read one number setting the way the steps use it, before anything is spent.

    WHY (for meta_limit_per_source and now every number setting): a blank value crashed its
    step at run time ("invalid literal for
    int() with base 10: ''") and the run carried on without that step, so the user
    believed it had run. Blank or null keeps the default. A missing key is left
    missing: each step reads its own default, as before. A value that is not a
    usable number is refused here, in one plain sentence, before any step runs.
    A usable value is kept exactly as written, so every working config sends the
    same input as before: a whole number may be written as a number, as 25.0 or as
    digits in quotes (the steps turn it into a number themselves). A decimal
    setting written in quotes is stored as the number, because the code that
    reads it uses it as a number without converting it (and crashed on text).
    low/high: the smallest and largest value accepted. 0 stays accepted wherever
    the steps accepted it before: a new meaning is not this function's to give.
    why: an extra reason the sentence gives. name: how the sentence names the
    setting (default: the key)."""
    if key not in cfg:
        return
    v = cfg[key]
    if v in _BLANK or (isinstance(v, str) and not v.strip()):
        cfg[key] = default
        return
    n = None
    if isinstance(v, bool):
        n = None
    elif isinstance(v, int):
        n = v
    elif isinstance(v, float) and math.isfinite(v):
        n = v if not whole else (int(v) if v.is_integer() else None)
    elif isinstance(v, str):
        s = v.strip()
        if whole:
            n = int(s) if s.isascii() and s.isdigit() else None
        else:
            try:
                n = float(s)
            except ValueError:
                n = None
            if n is not None and not math.isfinite(n):
                n = None
    if n is None or n < low or (high is not None and n > high):
        label = name or key
        kind = "a whole number" if whole else "a number"
        span = f"from {low} to {high}" if high is not None else f"of {low} or more"
        raise SystemExit(f"Config {path}: {label} is {v!r}. {label} must be {kind} {span}"
                         + (f"; {why}" if why else "")
                         + ". Nothing was run and nothing was spent.")
    if not whole and isinstance(v, str):
        cfg[key] = n


def _read_numbers(cfg, path, settings):
    """Read every number setting of this tool (settings: key -> (default, low,
    high, whole, why), the tool's own _NUMBER_SETTINGS) with _read_number."""
    for key, (default, low, high, whole, why) in settings.items():
        _read_number(cfg, key, default, path, low, high, whole, why)


def _read_budget_caps(cfg, path, default):
    """Read budget_caps, the spending cap in US dollars, before anything is spent.
    Blank keeps {"phase1": default}; a blank phase1 keeps the default; anything
    else that is not a dict, or a phase1 that is not a number of 0 or more, is
    refused in one plain sentence (it crashed the first paid step before)."""
    caps = cfg.get("budget_caps")
    if caps in _BLANK or (isinstance(caps, str) and not caps.strip()):
        cfg["budget_caps"] = {"phase1": default}
        return
    if not isinstance(caps, dict):
        raise SystemExit(f"Config {path}: budget_caps is {caps!r}. budget_caps must be "
                         f"written like {{\"phase1\": {default}}}, the most this run may "
                         f"spend in US dollars. Nothing was run and nothing was spent.")
    _read_number(caps, "phase1", default, path, whole=False, name="budget_caps phase1")


# Every number setting of this tool: key -> (default, low, high, whole, why).
# Each default is the one the step that reads the setting uses (stages.py,
# postprocess.py, extractor.py); 0 stays accepted, as it was.
_NUMBER_SETTINGS = {
    "keyword_cap": (300, 0, None, True, ""),
    "serp_top_n": (100, 0, None, True, ""),
    "opportunity_kd_max": (40, 0, None, False, ""),
}

def get_token():
    """
    Resolve the Apify token, in order:
      1. APIFY_TOKEN environment variable. Populate it however you like -- e.g.
         `export APIFY_TOKEN=...`, or `set -a; source scripts/.env; set +a` after
         filling scripts/.env. This module does NOT read scripts/.env itself;
         sourcing it is simply one way to put the value in the environment.
      2. ~/.apify/auth.json -- the file `apify login` writes and that
         persist_token() updates. If you've run `apify login`, or saved a token
         once via `python3 config.py --persist-token`, this just works with no
         re-entry on later runs.
    """
    tok = os.environ.get("APIFY_TOKEN", "").strip()
    if tok:
        try:
            # save once so later runs in this env don't re-ask; never over a
            # different saved login, which is then kept and named.
            if persist_token(tok)[1] == "kept":
                print(KEPT_LOGIN_LINE, file=sys.stderr)
        except Exception:  # noqa: BLE001 — persistence is a convenience, never fatal
            pass
        return tok

    auth_path = Path.home() / ".apify" / "auth.json"
    if auth_path.exists():
        try:
            tok = (json.loads(auth_path.read_text()).get("token") or "").strip()
        except (json.JSONDecodeError, OSError):
            tok = ""
        if tok:
            return tok

    raise SystemExit(
        "No Apify token found. Either run `apify login`, or copy scripts/.env.example "
        "to scripts/.env, fill APIFY_TOKEN, and `set -a; source scripts/.env; set +a`."
    )


def actor_runs(spec, actor_input):
    """How many actor runs run_phase._run_one launches for this stage's full
    input: one per batch of spec["batch"]["size"] when the batched array is
    longer than one batch (the same test _run_one makes), else one.

    WHY: every run pays its own start charge, so the
    forecast and the spend cap price one start fee per run. Since 82a40f7 the
    Google ad stage (B5) launches ceil(domains / B5_BATCH_SIZE) runs."""
    batch = (spec or {}).get("batch")
    arr = (actor_input or {}).get(batch["key"]) if batch else None
    if batch and isinstance(arr, list) and len(arr) > batch["size"]:
        return -(-len(arr) // batch["size"])
    return 1


def estimate(stage, expected_count, rate_override=None, runs=1):
    """Return (usd_estimate, unit, rate_used, unverified).

    runs: how many actor runs the work is spread over (actor_runs); each pays
    the stage's start charge once."""
    if stage not in EMPIRICAL_RATES:
        raise KeyError(f"Unknown step {stage!r}. Known: {', '.join(EMPIRICAL_RATES)}")
    _actor, unit, rate, unverified = EMPIRICAL_RATES[stage]
    rate_used = rate_override if rate_override is not None else rate
    # A run's start charge is exact, so it carries no safety margin; a stage
    # with nothing to do launches no run and pays none.
    start = start_fee_usd(stage) * max(1, int(runs)) if expected_count > 0 else 0.0
    return expected_count * rate_used * SAFETY_MARGIN + start, unit, rate_used, unverified


# What a run says when APIFY_TOKEN differs from the login
# already saved in ~/.apify/auth.json. Neither value is ever printed.
KEPT_LOGIN_LINE = ("A different Apify login is already saved here (~/.apify/auth.json), "
                   "so it was kept; the token set in APIFY_TOKEN is used for this run only.")


def _replace_auth_file(path, text):
    """Write `text` to the auth file at `path` whole or not at all: into a temporary file in the same
folder, then os.replace. Before,
    the file was written in place, so a run killed mid-write left the user's own
    Apify login file (which their `apify` command line uses too) half-written.
    WHY realpath: when auth.json is a link (a dotfile manager), the file it points
    to is replaced, as the in-place write did, never the link itself. The new file
    is mode 600 from the start, as the old code made it right after writing."""
    import tempfile
    real = os.path.realpath(str(path))
    fd, tmp = tempfile.mkstemp(prefix=".auth.", suffix=".tmp", dir=os.path.dirname(real))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, real)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def persist_token(token, auth_path=None):
    """Save an Apify token into ~/.apify/auth.json (mode 600), non-destructively.

    Reads any existing auth.json (the file `apify login` writes) and updates ONLY
    the `token` field, so other keys the CLI stores are preserved -- never a blind
    overwrite. get_token() reads this same file, so a token saved here is reused by
    every later run with no re-entry. Never logs the token.

    It never replaces a DIFFERENT saved token. Before, a run
    with another account's token in APIFY_TOKEN overwrote the saved login, so later
    runs and the user's own `apify` command line silently used the other account,
    against the decided design ("without disturbing an existing `apify login`",
    docs/SETUP.md). Returns (path, outcome):
      "saved" -- no token was saved (no file, an unreadable file, or no token key):
                 written, as before;
      "same"  -- this token is already saved: nothing is written;
      "kept"  -- a different token is saved: it is kept, nothing is written, and
                 the caller uses `token` for this run only.
    """
    token = (token or "").strip()
    if not token:
        raise SystemExit("persist_token: empty token, nothing to save.")
    auth_path = Path(auth_path) if auth_path else Path.home() / ".apify" / "auth.json"
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if auth_path.exists():
        try:
            existing = json.loads(auth_path.read_text())
            if isinstance(existing, dict):
                data = existing
        except (json.JSONDecodeError, OSError):
            data = {}  # unreadable/corrupt: start clean rather than fail the save
    saved = data.get("token")
    if isinstance(saved, str) and saved.strip():
        return auth_path, ("same" if saved.strip() == token else "kept")
    data["token"] = token
    _replace_auth_file(auth_path, json.dumps(data, indent=2))
    try:
        auth_path.chmod(0o600)
    except OSError:
        pass  # best-effort on filesystems without POSIX modes
    return auth_path, "saved"


def _mask_token(token):
    """A safe-to-print token fingerprint. NEVER print the token itself."""
    t = (token or "").strip()
    return f"****{t[-4:]}" if len(t) >= 4 else "****"


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="config utilities (token persistence).")
    ap.add_argument(
        "--persist-token", action="store_true",
        help="Save the Apify token into ~/.apify/auth.json so later runs reuse it with "
             "no re-entry. Reads the token from the APIFY_TOKEN env var -- never the "
             "command line, so it cannot leak via the process list. When APIFY_TOKEN is "
             "not set and a person is at a terminal, it asks for the token (hidden). "
             "(Normal runs also persist the token automatically on first use; this is "
             "the explicit form.)",
    )
    _args = ap.parse_args()
    if _args.persist_token:
        _tok = os.environ.get("APIFY_TOKEN", "").strip()
        if not _tok and sys.stdin.isatty():
            # Typing `export APIFY_TOKEN=...` into a Terminal writes the token to the shell's
            # history file in plain text, and a profile edit reaches only windows opened
            # after it (in a real buyer's run neither was explained). Asked for here, the
            # token is typed or pasted hidden, so it reaches no history and no process list,
            # and the window needs no setup. Only when a person is at a terminal: a script or
            # test with no APIFY_TOKEN and no terminal still gets the sentence further down.
            import getpass
            try:
                _tok = getpass.getpass("Paste your Apify API token (nothing is shown as you "
                                       "paste), then press Return: ").strip()
            except (EOFError, KeyboardInterrupt):
                _tok = ""
            if not _tok:
                raise SystemExit("No token was entered, so nothing was saved.")
        if not _tok:
            raise SystemExit(
                "Set APIFY_TOKEN in the environment first "
                "(not by typing it into Terminal, which keeps it in your shell history), "
                "or run this command in a Terminal window, where it asks for the token "
                "hidden and leaves no history. That is Route 1 in the plugin's SETUP.md, "
                "\"save it once with the plugin's own script\"."
            )
        # A person who pastes the whole `export APIFY_TOKEN=...` line (it is what the
        # older instructions had them type) means the value after the "=": take it,
        # then check its shape. In a real run a sentence typed here was saved and
        # reported as a success, and every later run failed to sign in. The same check
        # is made on a token that came from APIFY_TOKEN: in a real run a variable that
        # held a sentence was saved by this command and reported as "Saved Apify token
        # ****ence". (Only this explicit save is checked. Using the variable for a run is
        # a separate decision and is not changed here.)
        if _tok.startswith("export "):
            _tok = _tok[len("export "):].strip()
        if _tok.startswith("APIFY_TOKEN="):
            _tok = _tok[len("APIFY_TOKEN="):].strip()
        # A shell line ends in ";" after its closing quote, so the ";" comes off before
        # the quotes do. Only a matching pair is removed; a stray quote stays and fails
        # the shape check below.
        _tok = _tok.rstrip("; \t")
        if len(_tok) >= 2 and _tok[0] == _tok[-1] and _tok[0] in "\"'":
            _tok = _tok[1:-1]
        # Whatever is left must be exactly one token: the prefix once, then only ASCII
        # letters and digits. A token pasted twice, a trailing ";" or invisible
        # character, or the bare prefix would otherwise be saved and reported as a
        # success, and every later run would fail to sign in. Apify's docs give no
        # length or character set for a token, so the 20-character floor after the
        # prefix is a deliberately low guard against a truncated paste, not a documented
        # length; a real token is longer.
        _body = _tok[len("apify_api_"):]
        if not (_tok.startswith("apify_api_") and _tok.count("apify_api_") == 1
                and len(_body) >= 20 and _body.isascii() and _body.isalnum()):
            raise SystemExit(
                "That does not look like an Apify API token, so nothing was saved. Your "
                "token is on the Apify Console under Settings > API & Integrations and "
                "starts with apify_api_. Run the command again to try again.")
        _p, _outcome = persist_token(_tok)
        if _outcome == "kept":
            raise SystemExit(
                f"A different Apify login is already saved in {_p}, so it was kept and "
                "nothing was saved. A run with APIFY_TOKEN set uses that token for that "
                "run only. To make it the saved login instead, log in with it using "
                "`apify login`.")
        print(f"Saved Apify token {_mask_token(_tok)} to {_p} (mode 600)."
              if _outcome == "saved" else
              f"Apify token {_mask_token(_tok)} was already saved in {_p}.")
        print(
            "Later runs in THIS environment now reuse it with no re-entry. In local "
            "Claude Code that is permanent; in a Cowork sandbox it lasts the "
            "conversation (a brand-new conversation asks once -- the sandbox is fresh)."
        )
    else:
        ap.print_help()
