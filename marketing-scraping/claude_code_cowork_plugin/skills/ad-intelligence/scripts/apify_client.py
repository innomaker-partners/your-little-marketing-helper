"""
Apify access via the REST API (v2).

Originally this shelled out to the `apify` CLI (`apify call`). That was a mistake:
`apify call` BLOCKS for the actor's entire runtime and, on the pinned/old CLI
build, kept the process open well after the run had already SUCCEEDED — so the
the caller blocked for the full actor runtime, and `usageTotalUsd` was read
before it settled (causing an underreport in the run logs).

Now we drive the REST API directly: START a run (returns instantly with an id),
POLL the run object until its status is terminal, then re-read the settled usage.
The run executes on Apify regardless of this process — polling is the *only* thing
we do locally, so nothing "hangs" and status is always ground truth.

Auth: the token the CLI already stored at `apify login` (~/.apify/auth.json), or
`APIFY_TOKEN` / `APIFY_API_TOKEN` from the environment. No CLI binary required.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.apify.com/v2"
# Terminal run states (Apify uses TIMED-OUT; accept the underscore spelling too).
TERMINAL = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT", "TIMED_OUT"}


class ApifyCliError(RuntimeError):
    """Kept for name-compatibility with the previous CLI-based client."""


def _token():
    """Resolve the Apify token, and persist it on first use.

    Order: env (APIFY_TOKEN / APIFY_API_TOKEN) -> ~/.apify/auth.json (the file
    `apify login` writes). When the token comes from the environment it is saved to
    ~/.apify/auth.json, so later runs in this environment never ask again. In local
    Claude Code that home file is permanent; in a Cowork sandbox it lasts the
    conversation -- a brand-new conversation is a fresh sandbox, so the token is
    asked once there (a platform limit, not a bug). First readable token wins.
    """
    for k in ("APIFY_TOKEN", "APIFY_API_TOKEN"):
        v = os.environ.get(k)
        if v and v.strip():
            tok = v.strip()
            _persist_token_home(tok)  # save once so later runs in this env don't re-ask
            return tok
    try:
        with open(os.path.expanduser("~/.apify/auth.json")) as f:
            tok = (json.load(f).get("token") or "").strip()
        if tok:
            return tok
    except Exception:  # noqa: BLE001 — absent/unreadable just means "not here"
        pass
    return None


# Said once per process when APIFY_TOKEN differs from the saved login.
_KEPT_LOGIN_SAID = []


def _persist_token_home(tok):
    """Best-effort save of the token to ~/.apify/auth.json -- the standard `apify
    login` location that _token() reads on later runs, so the token is entered at
    most once per environment. Non-destructive (keeps any other keys the CLI
    stored), idempotent (skips a rewrite when unchanged), mode 600. NEVER raises and
    NEVER logs the token: persistence is a convenience, never a reason to fail a run
    or leak a credential.

    It never replaces a DIFFERENT saved token. Before, a run
    with another account's token in APIFY_TOKEN overwrote the saved login, so later
    runs and the user's own `apify` command line silently used the other account,
    against the decided design ("without disturbing an existing `apify login`",
    docs/SETUP.md). The saved login is kept, this run still uses `tok` (_token()
    returns it), and one plain line on stderr says so, without either value."""
    try:
        path = os.path.expanduser("~/.apify/auth.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    existing = json.load(f)
                if isinstance(existing, dict):
                    data = existing
            except Exception:  # noqa: BLE001 — corrupt/unreadable: start clean
                data = {}
        saved = data.get("token")
        # Stripped on both sides: a saved token with a
        # trailing space is the same login, as config.persist_token already says.
        if isinstance(saved, str) and saved.strip() == str(tok).strip():
            return  # already saved; don't rewrite
        if isinstance(saved, str) and saved.strip():
            if not _KEPT_LOGIN_SAID:
                _KEPT_LOGIN_SAID.append(True)
                print("A different Apify login is already saved here (~/.apify/auth.json), "
                      "so it was kept; the token set in APIFY_TOKEN is used for this run "
                      "only.", file=sys.stderr)
            return
        data["token"] = tok
        _replace_auth_file(path, json.dumps(data, indent=2))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass  # best-effort on filesystems without POSIX modes
    except Exception:  # noqa: BLE001 — never break a run over persistence
        pass


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


def cli_available():
    """Pre-flight auth check (name kept for run_phase's gate). True when a usable
    Apify token is available — the client talks REST, so the CLI binary itself is
    no longer required, only the token that `apify login` stored."""
    return bool(_token())


def _req(method, path, body=None, want_binary=False, dest_path=None, timeout=120):
    """One Apify REST call. `path` starts with '/'. Returns parsed JSON, or writes
    bytes to dest_path (binary), or returns raw bytes (want_binary)."""
    tok = _token()
    if not tok:
        raise ApifyCliError(
            "No Apify token. Run `apify login` (stores ~/.apify/auth.json) or set APIFY_TOKEN."
        )
    sep = "&" if "?" in path else "?"
    url = f"{API}{path}{sep}token={tok}"
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if dest_path:
                with open(dest_path, "wb") as f:
                    f.write(resp.read())
                return None
            raw = resp.read()
            return raw if want_binary else json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        raise ApifyCliError(f"Apify API {method} {path.split('?')[0]} -> {e.code}: {detail}")
    except urllib.error.URLError as e:
        # Transient client-side failure (ECONNRESET, DNS, timeout) -- the request
        # may or may not have reached Apify. Callers must NOT blindly retry a
        # mutating POST (it could double-launch a paid run); idempotent GETs are
        # retried in the poll loop. Raised as ApifyCliError so it surfaces
        # uniformly rather than crashing the run with a raw urllib error.
        raise ApifyCliError(f"Apify API {method} {path.split('?')[0]} -> transient: {e.reason}")


def run_info(run_id):
    """Full run object (id, status, defaultDatasetId, defaultKeyValueStoreId,
    usageTotalUsd)."""
    return _req("GET", f"/actor-runs/{run_id}")["data"]


def actual_charge_usd(run):
    """The USD a run actually BILLS the user — correct across pricing models.

    What `usageTotalUsd` holds was settled from real records (33 runs of 12 actors read back
    from Apify): once a run's charges are final it
    is the run's whole bill to the user, and it INCLUDES the event charges. On
    all 30 runs of actors that absorb platform usage it equalled the priced
    events exactly; on the 3 runs of actors that pass platform usage through it
    was the priced events plus a small compute remainder ($0.000207, $0.000207,
    $0.013846). So:

      * PAY_PER_COMPUTE actors: usageTotalUsd is the bill.
      * PAY_PER_EVENT, platform absorbed by developer: bill = event charges.
      * PAY_PER_EVENT + `isPPEPlatformUsagePaidByUser`: bill = usageTotalUsd
        (events + compute). The old "events + usageTotalUsd" counted the events
        twice: $0.1404 for a keyword-expander run Apify billed $0.0703. While
        usageTotalUsd still lags the ledger it can read below the events, so the
        figure is max(events, usageTotalUsd), never below the events.

    lexis-solutions/google-ads-scraper uses the third mode
    ("high compute costs are now billed as Apify platform usage,
    passed through at cost; per-ad fee reduced"). Charging bare usageTotalUsd
    there UNDER-reports by the whole per-ad portion — the budget would think it
    spent less than it did and could overrun the cap.

    Event cost is computed from `chargedEventCounts` (the live charging ledger,
    populated as items are scraped — reliable at terminal, unlike the USD
    `eventUsage` field which settles minutes later) times the per-event prices in
    `pricingInfo.pricingPerEvent`. Falls back to usageTotalUsd if the pricing
    block is missing, so this never charges $0 by accident.
    """
    platform = float(run.get("usageTotalUsd") or 0.0)
    pi = run.get("pricingInfo") or {}
    if pi.get("pricingModel") != "PAY_PER_EVENT":
        return platform  # pay-per-compute (or unknown): platform usage is the bill
    prices = {
        k: float((v or {}).get("eventPriceUsd") or 0.0)
        for k, v in (pi.get("pricingPerEvent", {}).get("actorChargeEvents", {}) or {}).items()
    }
    counts = run.get("chargedEventCounts") or {}
    if not prices or not counts:
        # A PAY_PER_EVENT run with an empty ledger would silently return ~$0,
        # blinding the budget gate. Print a loud warning so a human notices; do
        # NOT invent a charge — warn + return the fallback so the uncertainty is visible.
        print(
            f"[apify_client] WARNING: PAY_PER_EVENT run {run.get('id', '?')}: "
            f"could not price event charges "
            f"(prices_populated={bool(prices)}, counts_populated={bool(counts)}). "
            f"Falling back to usageTotalUsd (${platform:.4f}), which is the "
            f"developer-absorbed compute cost, NOT the event charges the user owes. "
            f"The budget figure for this run is UNRELIABLE and likely UNDER-REPORTS "
            f"the true bill. Verify the true cost on the Apify console.",
            file=sys.stderr,
        )
        return platform  # can't price the events -> fall back rather than under-bill
    events = sum(prices.get(k, 0.0) * n for k, n in counts.items())
    if pi.get("isPPEPlatformUsagePaidByUser"):
        # usageTotalUsd already includes the events (see above): adding them
        # again double-counts. max() keeps a lagging usageTotalUsd from reading
        # below the events it will include.
        return max(events, platform)
    return events


def _charge_settled(run):
    """True once the run's billing fields are populated enough for
    actual_charge_usd to price it WITHOUT falling back to an unsettled number.

    Apify populates the billing ledger a moment AFTER a run reaches a terminal
    state. This settlement is not instant: an actor can reach a terminal state
    with empty chargedEventCounts, causing actual_charge_usd to fall back to
    the (also-unsettled) usageTotalUsd and under-charge the budget by the whole
    per-event portion (observed: $0.065 recorded vs $0.231 real on one run).

    - PAY_PER_EVENT: settled once the per-event prices are present AND the charged
      counts sum to > 0 (that is exactly what actual_charge_usd needs to avoid its
      fallback or a premature $0 when the ledger reads zeros for a beat).
    - Any other known model: usageTotalUsd is the bill, so settled once it is
      non-zero.
    - pricingInfo not populated yet: not settled -- keep waiting.
    """
    pi = run.get("pricingInfo") or {}
    model = pi.get("pricingModel")
    if model == "PAY_PER_EVENT":
        counts = run.get("chargedEventCounts") or {}
        prices = (pi.get("pricingPerEvent", {}) or {}).get("actorChargeEvents") or {}
        # Require a POSITIVE charged sum, not just a non-empty dict. Apify can
        # expose chargedEventCounts as {"<event>": 0} at terminal for a beat
        # before the ledger populates -- observed live on
        # thewolves/appstore-reviews-scraper (a real run: 0 at return,
        # 100 items / $0.01 a short time later). bool(counts) is True for that
        # zeros-dict, so the old check left the settle loop and actual_charge_usd
        # priced the run at $0.00 when it truly billed $0.01. A run that genuinely
        # charged nothing AND has no start fee sums to 0 and waits out
        # settle_max_s, then returns 0 -- the correct figure, just later.
        # Start-fee actors carry start>=1 from the first read, so this check
        # alone passes while the per-record charges are still missing: it
        # is necessary, not sufficient. _charge_complete decides "final".
        total = sum(v for v in counts.values() if isinstance(v, (int, float)))
        return bool(prices) and total > 0
    if model:
        return float(run.get("usageTotalUsd") or 0.0) > 0.0
    return False


# The environment variable naming the file where a run's entry script collects
# the paid steps whose charges were still changing when the tool stopped waiting
# (see _settle_charge). The entry script reads it to say, next to the bill, that
# the bill may be incomplete. Unset (run_phase run by hand): the warning printed
# below the step is the only notice.
CHARGE_NOTES_ENV = "MARKETING_SCRAPING_CHARGE_NOTES"

# Events that known actors charge once per record they write to their dataset.
# A finished ledger holds at least as many of one of them as the dataset holds
# records; fewer means Apify has not recorded them all yet. From the pricing of
# the actors the three tools use: apify-default-dataset-item
# is the platform's own per-record event; review-scraped (Google Maps reviews),
# review (Trustpilot), result (Reddit: "when a post / comment is saved to the
# dataset"), search-page-scraped (Google Search: one record per results page),
# result-item (Google ads), ad (LinkedIn ads) and place-scraped (Google Maps place
# discovery) are the actors' own.
# An actor can price more than one of these (the Maps place-discovery actor also
# prices review-scraped, an add-on the tool never asks for), so the check takes
# the HIGHEST count among them: without place-scraped here, a real place-discovery run
# (place-scraped 1, review-scraped 0, 1 record) read as never final and would have been flagged
# as a possibly low bill.
_ITEM_EVENTS = frozenset({
    "apify-default-dataset-item", "review-scraped", "review", "result",
    "search-page-scraped", "result-item", "ad", "place-scraped",
})


def _is_start_event(name):
    """True for an event charged once when a run starts (apify-actor-start,
    actor-start, start, Reddit's init), not per record."""
    n = str(name).lower()
    return "start" in n or n == "init"


def _ledger(run):
    """(prices, counts, events_usd) of a pay-per-event run."""
    pi = run.get("pricingInfo") or {}
    prices = {
        k: float((v or {}).get("eventPriceUsd") or 0.0)
        for k, v in ((pi.get("pricingPerEvent") or {}).get("actorChargeEvents") or {}).items()
    }
    counts = run.get("chargedEventCounts") or {}
    events = sum(prices.get(k, 0.0) * n for k, n in counts.items()
                 if isinstance(n, (int, float)))
    return prices, counts, events


def _charge_complete(prev, run, item_count):
    """True once the run's charges have stopped changing, so actual_charge_usd
    gives the run's FINAL bill.

    WHY (seen in a paid validation run): Apify fills a finished run's ledger a few
    seconds late, and not all at once. _charge_settled alone ("prices present and
    counts > 0") accepted the first read that had anything in it, which was
    often only the start fee: Capterra read $0.005 of $0.041 (start 1, records 0
    of 40), Google Search read $0.006 of $0.0085 (pages 2 of 3), the keyword
    expander $0.040 of $0.0703. The printed bill and the budget both used those
    low figures. So the charge is final only when ALL of these hold:
      - _charge_settled(run): prices present and something charged;
      - the ledger and usageTotalUsd are unchanged since the previous read after
        the run ended (prev): a ledger still filling changes between reads;
      - when the run SUCCEEDED and the dataset's record count is known and
        positive: a per-record event (_ITEM_EVENTS) has reached it, or, for an
        actor with no known per-record event, something other than the start fee
        is charged. Only for SUCCEEDED: a run Apify stopped at its spend cap
        (ABORTED) keeps records it was never charged for, so its final ledger
        is below its record count (a real Google ads run: 83 records, 54 charged, settled). The
        rule used to apply to every status, which flagged every capped run as a possibly
        low bill after waiting out the whole bound;
      - for an actor that passes platform usage through, usageTotalUsd has caught
        up with the event charges (it includes them once settled).
    Other pricing models: usageTotalUsd is the bill; final once it is non-zero
    and unchanged since the previous read.
    """
    if prev is None:
        return False
    if (prev.get("chargedEventCounts") or {}) != (run.get("chargedEventCounts") or {}):
        return False
    if float(prev.get("usageTotalUsd") or 0.0) != float(run.get("usageTotalUsd") or 0.0):
        return False
    if not _charge_settled(run):
        return False
    pi = run.get("pricingInfo") or {}
    if pi.get("pricingModel") != "PAY_PER_EVENT":
        return True
    prices, counts, events = _ledger(run)
    if item_count and run.get("status") == "SUCCEEDED":
        per_record = [k for k in prices if k in _ITEM_EVENTS]
        if per_record:
            if max(counts.get(k) or 0 for k in per_record) < item_count:
                return False
        elif not any(n for k, n in counts.items() if not _is_start_event(k)):
            return False
    if pi.get("isPPEPlatformUsagePaidByUser"):
        if float(run.get("usageTotalUsd") or 0.0) + 1e-9 < events:
            return False
    return True


def _charge_zero_final(prev, run, item_count):
    """True for a run that genuinely charged nothing: no records in its dataset,
    nothing in its ledger, and the same on two reads. Only asked once the wait
    has run out; such a run is not an incomplete bill (it is $0, just later)."""
    if prev is None or item_count != 0:
        return False
    if (prev.get("chargedEventCounts") or {}) != (run.get("chargedEventCounts") or {}):
        return False
    counts = run.get("chargedEventCounts") or {}
    charged = sum(n for n in counts.values() if isinstance(n, (int, float)))
    return charged == 0 and float(run.get("usageTotalUsd") or 0.0) == 0.0


def _dataset_item_count(dataset_id):
    """The run dataset's record count (GET /datasets/{id} -> itemCount), or None
    when it cannot be read. A free read; a failure only drops one check."""
    if not dataset_id:
        return None
    try:
        d = _req("GET", f"/datasets/{dataset_id}")
    except Exception:  # noqa: BLE001 -- best-effort; the other checks still apply
        return None
    data = d.get("data") if isinstance(d, dict) else None
    n = (data or {}).get("itemCount") if isinstance(data, dict) else None
    if isinstance(n, bool) or not isinstance(n, (int, float)) or n < 0:
        return None
    return int(n)


def _settle_charge(run_id, settle_poll_s, settle_max_s, actor=None):
    """Re-read a finished run until its charges are final (_charge_complete),
    for at most settle_max_s, and return the last read.

    The bound stays: a slow ledger must never hang the product. When it runs
    out first, the returned run carries "_charge_incomplete": True and the bill
    SAYS so (_note_incomplete_charge) instead of passing a possibly low figure
    off as final. A run that genuinely charged nothing (_charge_zero_final) is
    not flagged.
    """
    deadline = time.time() + settle_max_s
    prev = None
    while True:
        time.sleep(settle_poll_s)
        run = _run_info_retry(run_id)
        n = _dataset_item_count(run.get("defaultDatasetId"))
        if _charge_complete(prev, run, n):
            return run
        if time.time() >= deadline:
            break
        prev = run
    if _charge_zero_final(prev, run, n):
        return run
    run = dict(run)
    run["_charge_incomplete"] = True
    _note_incomplete_charge(run, actor, settle_max_s)
    return run


def _note_incomplete_charge(run, actor, waited_s):
    """Say, next to the step and in the run's closing bill, that this step's
    charge may be incomplete. Never raises: the notice must not stop a run."""
    rid = run.get("id", "?")
    try:
        charge = actual_charge_usd(run)
    except Exception:  # noqa: BLE001
        charge = 0.0
    print(f"   ⚠ Apify was still recording the charges of run {rid} after "
          f"{waited_s}s. The ${charge:.4f} counted for this step may be lower than "
          f"what Apify bills; the exact amount is on the Apify console.", flush=True)
    path = os.environ.get(CHARGE_NOTES_ENV)
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"run": rid, "actor": actor or run.get("actId"),
                                "charge_counted_usd": round(charge, 6)}) + "\n")
    except Exception:  # noqa: BLE001
        return


_ACTOR_MIN_CACHE = {}


def actor_min_charge(actor):
    """The actor's minimalMaxTotalChargeUsd -- the floor Apify enforces on the
    caller's maxTotalChargeUsd. Pass a cap below it and Apify REJECTS the run at
    start (nothing charged). The developer sets it per actor "to cover the price
    of starting the Actor and producing one result", and it is exposed on the
    public actor record, so we read it and floor our cap at it rather than guess
    or hardcode (actor pricing changes). Cached per process -- one GET per actor.
    Returns 0.0 when unset or unreadable: a lookup hiccup must never block a run
    (the cap then behaves as before this floor was handled -- no worse than old).
    """
    if actor in _ACTOR_MIN_CACHE:
        return _ACTOR_MIN_CACHE[actor]
    val = 0.0
    try:
        aid = actor.replace("/", "~")
        d = _req("GET", f"/acts/{aid}")
        data = d.get("data", d) if isinstance(d, dict) else {}
        pricing = data.get("pricingInfos") or []
        if pricing:
            m = pricing[-1].get("minimalMaxTotalChargeUsd")
            if isinstance(m, (int, float)) and m > 0:
                val = float(m)
    except Exception:  # noqa: BLE001 -- best-effort; fall back to no floor
        val = 0.0
    _ACTOR_MIN_CACHE[actor] = val
    return val


def run_actor(actor, actor_input, memory_mb=4096, timeout_s=1800, on_status=None,
              on_start=None, poll_s=3, max_wait_s=1800, settle_poll_s=3,
              settle_max_s=30, max_charge_usd=None):
    """Start an actor run via REST, poll until its status is terminal, return the
    settled run object. Non-blocking on Apify's side: the run proceeds regardless
    of this process; we only poll. usageTotalUsd is re-read once after the run
    reaches a terminal state, so it reflects the settled cost rather than the
    pre-settlement underreport.

    Run recovery: on_start(run_id, dataset_id), if given, is called the
    instant the run is launched -- BEFORE the poll loop. The caller persists that
    handle, so a transient client drop anywhere in the (possibly long) poll or
    the later fetch can resume this exact run by id via reattach() instead of
    paying to launch a second one. The poll's own reads also retry on transient
    errors, so a brief blip never kills a run that is fine on Apify's side.

    Spend safety when max_charge_usd is set -- two independent layers,
    because an actor can ignore its own input caps (a live Capterra run scraped
    3,058 reviews against a cap of 30):
      * L2 -- pass it to Apify as `maxTotalChargeUsd`, the platform BILLING
        hard-cap. Verified live: a run configured for up
        to 60 items stopped at $0.0596 under a $0.06 cap, halting at the exact
        last item that fit under it. A >=$0.01 floor stops a tiny forecast from
        rounding to "0.0000", which the platform could read as "allow no charge".
      * L3 -- our OWN poll-and-abort. L2's enforcement lives inside the actor's
        Actor.charge() calls; apify-sdk-js#572 documents pay-per-event runs that
        hang past the cap when the actor stops calling charge(). So we ALSO price
        the run mid-flight from its live chargedEventCounts (verified live: the
        ledger increments DURING the run, not only at terminal) and POST an abort
        ourselves the moment it breaches the ceiling. This stop is ours and does
        not depend on the actor being correctly implemented.
    """
    # No actor run starts without a spending limit.
    # Every actor run can charge (a start fee, or each record), and the limit
    # below was sent only when max_charge_usd was above 0: a forecast of $0 (a
    # count setting of 0) sent a paid call with no limit at all. A caller that
    # has no positive, finite limit for a run must not start it.
    _lim = max_charge_usd
    if isinstance(_lim, bool) or not isinstance(_lim, (int, float)) \
            or not (0 < float(_lim) < float("inf")):
        raise ApifyCliError(
            f"not starting {actor}: this paid run was given no spending limit "
            f"({max_charge_usd!r}), so nothing was started and nothing was spent")
    aid = actor.replace("/", "~")
    if on_status:
        on_status("CALLING")
    q = f"/acts/{aid}/runs?memory={memory_mb}&timeout={timeout_s}"
    # Spend cap, two DECOUPLED ceilings (so the platform floor never loosens our
    # own protection):
    #  * l3_ceiling -- our poll-and-abort threshold below, kept tight at the
    #    caller's forecast x factor. This is the real runaway protection.
    #  * the value sent to Apify as maxTotalChargeUsd -- floored at the actor's
    #    minimalMaxTotalChargeUsd, because some actors REJECT a run whose cap is
    #    below their floor (e.g. compass/crawler-google-places at $0.50). Flooring
    #    the CAP satisfies the platform WITHOUT touching the work quantity, so a
    #    cheap run still bills cheaply and our tight L3 still catches a runaway.
    l3_ceiling = None
    if max_charge_usd is not None and float(max_charge_usd) > 0:
        l3_ceiling = float(max_charge_usd)
        apify_cap = max(l3_ceiling, actor_min_charge(actor), 0.01)
        q += f"&maxTotalChargeUsd={apify_cap:.4f}"
    start = _req("POST", q, actor_input)
    run = start["data"]
    rid = run["id"]
    if on_start:
        on_start(rid, run.get("defaultDatasetId"))
    last = None
    we_aborted = False
    t0 = time.time()
    while run.get("status") not in TERMINAL:
        if time.time() - t0 > max_wait_s:
            raise ApifyCliError(f"run {rid} still {run.get('status')} after {max_wait_s}s")
        # L3: independent poll-and-abort. Price the run so far from its live
        # ledger and stop it ourselves if it breaches the ceiling -- a control
        # point DURING the run, which budget.enforce() (post-return) never had.
        if l3_ceiling is not None:
            if actual_charge_usd(run) > l3_ceiling:
                if on_status:
                    on_status("ABORTING-OVER-BUDGET")
                we_aborted = True
                try:
                    _req("POST", f"/actor-runs/{rid}/abort?gracefully=false")
                except ApifyCliError:
                    pass  # already terminal / abort raced the finish -- settle below
                run = _run_info_retry(rid)
                break
        if on_status and run.get("status") != last:
            on_status(run.get("status"))
            last = run.get("status")
        time.sleep(poll_s)
        run = _run_info_retry(rid)
    # Settle the billing ledger before returning. Apify fills a finished run's
    # ledger (chargedEventCounts, usageTotalUsd) a few seconds late and not all
    # at once, so the first read after terminal can hold only the start fee.
    # _settle_charge re-reads until the charges have stopped changing (see
    # _charge_complete), bounded by settle_max_s so the product never hangs; a
    # run that hits the bound is flagged and its bill says it may be
    # incomplete. This is a deterministic latency-bound wait, so a bound is
    # correct here (unlike non-deterministic steps, which get no timeout).
    run = _settle_charge(rid, settle_poll_s, settle_max_s, actor)
    # COST NOTE: price the run with actual_charge_usd(run), which reads the
    # run's own pricingInfo + chargedEventCounts (and usageTotalUsd for actors
    # that pass platform usage through); see its docstring for what
    # usageTotalUsd holds.
    if on_status:
        on_status(run.get("status", "DONE"))
    # The run carries what the RUN INCOMPLETE block said, so the
    # caller records the same judgment instead of making a second one.
    _inc = _warn_incomplete(run, actor, max_charge_usd, we_aborted)
    if _inc:
        run["_incomplete"] = _inc
    return run


def _incomplete_info(run, max_charge_usd, we_aborted):
    """The one judgment of whether a finished run did NOT finish its work.

    None for a run that SUCCEEDED (or whose status is unknown); otherwise a dict:
    status, run_id, charged_usd, ceiling_usd and cap_stop (it stopped at the
    spend cap, ours via L3 or Apify's maxTotalChargeUsd). _warn_incomplete prints
    the RUN INCOMPLETE block from this dict and run_actor attaches it to the run
    as run["_incomplete"]; the stage records and the scheduled run's
    notification read that, never a second judgment (the notification
    said "Run complete" under a printed RUN INCOMPLETE block)."""
    status = run.get("status")
    if status not in ("ABORTED", "FAILED", "TIMED-OUT", "TIMED_OUT"):
        return None
    try:
        charge = actual_charge_usd(run)
    except Exception:  # noqa: BLE001 — a pricing hiccup must never mask the warning
        charge = 0.0
    ceiling = float(max_charge_usd) if max_charge_usd else 0.0
    cap_stop = bool(we_aborted or (ceiling > 0 and charge >= 0.9 * ceiling))
    return {"status": status, "run_id": run.get("id", "?"),
            "charged_usd": round(float(charge or 0.0), 6),
            "ceiling_usd": round(ceiling, 6), "cap_stop": cap_stop}


def _warn_incomplete(run, actor, max_charge_usd, we_aborted):
    """Print a LOUD, unambiguous notice when a run ended without finishing.

    A run that ABORTED/FAILED/TIMED-OUT still returns whatever landed in its
    dataset so far -- a PARTIAL result. The caller prints only the terminal
    status token (e.g. `[ABORTED]`), which reads as benign ("it stopped") and
    has been misread downstream as "it finished" -- reports were then written on
    a truncated corpus as though it were complete. This states the
    incompleteness in words and names the one correct recovery: RESUME the same
    run (an Apify resurrect continues the SAME dataset from where it stopped),
    never a fresh run (which re-charges for data already pulled) and never a
    research/verify detour first.

    It distinguishes a spend-cap stop (ours via L3, or Apify's
    maxTotalChargeUsd) from an external/transient abort, because the recovery
    differs: a cap stop needs the budget raised before resuming; an external
    abort just needs a resume. Prints to stderr so it never contaminates a
    stdout data stream, mirroring actual_charge_usd's warnings.

    Returns the _incomplete_info dict it printed from, or None (nothing printed).
    """
    info = _incomplete_info(run, max_charge_usd, we_aborted)
    if info is None:
        return None
    status, rid = info["status"], info["run_id"]
    charge, ceiling, cap_stop = info["charged_usd"], info["ceiling_usd"], info["cap_stop"]
    bar = "=" * 68
    out = [
        "",
        bar,
        f"⚠  RUN INCOMPLETE: actor {actor} ended {status} (run {rid}).",
        "   It did NOT finish; whatever it returned is a PARTIAL result, and any",
        "   report built on it is partial. Do not treat this as complete.",
    ]
    if cap_stop:
        out += [
            f"   It stopped at the spend cap (~${charge:.2f} of ${ceiling:.2f}).",
            "   To finish: raise the budget cap, THEN resume this same run.",
        ]
    else:
        out += [
            f"   It stopped for an external reason (~${charge:.2f}, under the "
            f"${ceiling:.2f} cap).",
            "   Baseline recovery for an unexplained abort is to RESUME, not to",
            "   research or verify the failure first.",
        ]
    out += [
        f"   RESUME = resurrect run {rid} on Apify; it continues the SAME dataset",
        "   from where it stopped. Never start a fresh run to 'redo' it (that",
        "   re-charges for data you already have).",
        bar,
        "",
    ]
    print("\n".join(out), file=sys.stderr)
    return info


def _run_info_retry(run_id, tries=5, delay=3.0):
    """run_info() with a few retries on transient errors. GET is idempotent, so
    re-reading a run after a dropped poll is safe -- unlike a POST that starts a
    run. Lets a long poll survive a brief network blip instead of crashing the
    whole run. Re-raises the last error if every try fails."""
    last_err = None
    for _ in range(max(1, tries)):
        try:
            return run_info(run_id)
        except ApifyCliError as e:
            last_err = e
            time.sleep(delay)
    raise last_err


def reattach(run_id, on_status=None, poll_s=3, max_wait_s=1800,
             settle_poll_s=3, settle_max_s=30):
    """Recover a previously-launched run instead of paying to launch it again.

    A transient client drop (ECONNRESET) between launching a run and saving its
    output leaves the run alive on Apify -- runs and datasets are kept
    indefinitely. Re-launching would charge a SECOND time for a result that
    already exists or is still being produced (the double-charge risk). Given the
    run id we persisted at launch, this:
      * returns the settled run object if the run is RUNNING (waits it out) or
        already SUCCEEDED -- the caller then fetches its dataset, no new charge;
      * returns None when there is nothing to recover (id unknown/unreachable, or
        the run FAILED / ABORTED / TIMED-OUT) -- the caller re-launches, which is
        legitimate because no usable paid result exists.
    """
    try:
        run = _run_info_retry(run_id)
    except ApifyCliError:
        return None  # id unknown or unreachable -> nothing to recover
    if run.get("status") in ("FAILED", "ABORTED", "TIMED-OUT", "TIMED_OUT"):
        return None
    t0 = time.time()
    while run.get("status") not in TERMINAL:
        if time.time() - t0 > max_wait_s:
            raise ApifyCliError(f"reattached run {run_id} still {run.get('status')} "
                                f"after {max_wait_s}s")
        if on_status:
            on_status(f"RECOVER:{run.get('status')}")
        time.sleep(poll_s)
        run = _run_info_retry(run_id)
    if run.get("status") != "SUCCEEDED":
        return None  # ended not-successfully after reattach -> re-launch
    run = _settle_charge(run_id, settle_poll_s, settle_max_s)
    if on_status:
        on_status("RECOVERED")
    return run


def fetch_dataset(dataset_id, *_ignored):
    """Return all items from a dataset as a list."""
    if not dataset_id:
        return []
    items = _req("GET", f"/datasets/{dataset_id}/items?format=json")
    return items if isinstance(items, list) else []


def list_kvstore_keys(store_id, *_ignored):
    """List keys in a key-value store (screenshots land here for apify/screenshot-url)."""
    if not store_id:
        return []
    data = _req("GET", f"/key-value-stores/{store_id}/keys")
    items = data.get("data", {}).get("items", []) if isinstance(data, dict) else []
    return [k.get("key") if isinstance(k, dict) else k for k in items]


def download_kv_value(store_id, key, dest_path, *_ignored):
    """Download one key-value-store record (e.g. a screenshot PNG) to a file."""
    _req("GET", f"/key-value-stores/{store_id}/records/{key}",
         want_binary=True, dest_path=dest_path)
