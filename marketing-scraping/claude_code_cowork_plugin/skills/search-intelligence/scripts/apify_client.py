"""
Apify access via the REST API (v2).

Originally this shelled out to the `apify` CLI (`apify call`). That was a mistake:
`apify call` BLOCKS for the actor's entire runtime and, on the pinned/old CLI
build, kept the process open well after the run had already SUCCEEDED — so the
pipeline looked hung on a run that was actually finished, every foreground
command hit its wall, and `usageTotalUsd` got read before it settled (the $0.001
-> $0.0035 underreport in the run logs).

Now we drive the REST API directly: START a run (returns instantly with an id),
POLL the run object until its status is terminal, then re-read the settled usage.
The run executes on Apify regardless of this process — polling is the *only* thing
we do locally, so nothing "hangs" and status is always ground truth.

Auth: the token the CLI already stored at `apify login` (~/.apify/auth.json), or
`APIFY_TOKEN` / `APIFY_API_TOKEN` from the environment. No CLI binary required.
"""

import json
import os
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


def _persist_token_home(tok):
    """Best-effort save of the token to ~/.apify/auth.json -- the standard `apify
    login` location that _token() reads on later runs, so the token is entered at
    most once per environment. Non-destructive (keeps any other keys the CLI
    stored), idempotent (skips a rewrite when unchanged), mode 600. NEVER raises and
    NEVER logs the token: persistence is a convenience, never a reason to fail a run
    or leak a credential."""
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
        if data.get("token") == tok:
            return  # already saved; don't rewrite
        data["token"] = tok
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass  # best-effort on filesystems without POSIX modes
    except Exception:  # noqa: BLE001 — never break a run over persistence
        pass


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

    `usageTotalUsd` is the platform RESOURCE cost (compute + proxy GB), which is
    NOT the same as what the user is billed:

      * PAY_PER_COMPUTE actors: the platform cost IS the bill -> usageTotalUsd.
      * PAY_PER_EVENT, platform absorbed by developer: bill = event charges only;
        usageTotalUsd is irrelevant (the developer eats it).
      * PAY_PER_EVENT + `isPPEPlatformUsagePaidByUser`: bill = event charges PLUS
        the platform usage passed through to the user.

    Some pay-per-event actors (e.g. lexis-solutions/google-ads-scraper) use the
    third mode: event charges PLUS platform usage passed through. Charging bare
    usageTotalUsd there UNDER-reports by the whole per-ad portion — the budget
    would think it spent less than it did and could overrun the cap.

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
        return platform  # can't price the events -> fall back rather than under-bill
    events = sum(prices.get(k, 0.0) * n for k, n in counts.items())
    return events + platform if pi.get("isPPEPlatformUsagePaidByUser") else events


def _charge_settled(run):
    """True once the run's billing fields are populated enough for
    actual_charge_usd to price it WITHOUT falling back to an unsettled number.

    Apify populates the billing ledger a moment AFTER a run reaches a terminal
    state. This is not instant: crawlerbros/google-keywords-suggest-scraper-pro
    can have an empty chargedEventCounts at terminal+2s, so actual_charge_usd
    falls back to the (also-unsettled) usageTotalUsd and under-charges the budget
    by the whole per-event portion ($0.065 recorded vs $0.231 real, ~3.5x low).

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
        # thewolves/appstore-reviews-scraper (run pDIrLLoaxzVr98SLj: 0 at return,
        # 100 items / $0.01 a short time later). bool(counts) is True for that
        # zeros-dict, so the old check left the settle loop and actual_charge_usd
        # priced the run at $0.00 when it truly billed $0.01. A run that genuinely
        # charged nothing AND has no start fee sums to 0 and waits out
        # settle_max_s, then returns 0 -- the correct figure, just later.
        # Start-fee actors always carry start>=1, so they still settle instantly.
        total = sum(v for v in counts.values() if isinstance(v, (int, float)))
        return bool(prices) and total > 0
    if model:
        return float(run.get("usageTotalUsd") or 0.0) > 0.0
    return False


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
    # Settle the billing ledger before returning. The event counts + per-event
    # prices (and usageTotalUsd) are populated a beat AFTER terminal; reading
    # them too early makes actual_charge_usd fall back and UNDER-charge the
    # budget (see _charge_settled for details on the settling behavior). Poll
    # until the charge is priceable, bounded by settle_max_s so a genuinely
    # zero-event or slow run still returns. This is a deterministic
    # latency-bound wait, so a bound is correct here (unlike non-deterministic
    # steps, which get no timeout).
    settle_deadline = time.time() + settle_max_s
    while True:
        time.sleep(settle_poll_s)
        run = _run_info_retry(rid)
        if _charge_settled(run) or time.time() >= settle_deadline:
            break
    # COST NOTE: do NOT read `usageTotalUsd` as "the bill" — it is the platform
    # RESOURCE cost, which only equals the bill for pay-per-compute actors. For
    # pay-per-event actors the bill is the event charges (and, when the actor
    # passes platform usage through, the platform cost on top). Use
    # actual_charge_usd(run), which reads the run's own pricingInfo +
    # chargedEventCounts. The USD `eventUsage` field settles minutes after
    # SUCCEEDED, but chargedEventCounts is the live ledger and is reliable at
    # terminal, so actual_charge_usd does not need to block for settlement.
    if on_status:
        on_status(run.get("status", "DONE"))
    _warn_incomplete(run, actor, max_charge_usd, we_aborted)
    return run


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
    """
    status = run.get("status")
    if status in (None, "SUCCEEDED"):
        return
    if status not in ("ABORTED", "FAILED", "TIMED-OUT", "TIMED_OUT"):
        return
    rid = run.get("id", "?")
    try:
        charge = actual_charge_usd(run)
    except Exception:  # noqa: BLE001 — a pricing hiccup must never mask the warning
        charge = 0.0
    ceiling = float(max_charge_usd) if max_charge_usd else 0.0
    cap_stop = we_aborted or (ceiling > 0 and charge >= 0.9 * ceiling)
    bar = "=" * 68
    out = [
        "",
        bar,
        f"⚠  RUN INCOMPLETE — actor {actor} ended {status} (run {rid}).",
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
            "   Baseline recovery for an unexplained abort is to RESUME — not to",
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
    settle_deadline = time.time() + settle_max_s
    while True:
        time.sleep(settle_poll_s)
        run = _run_info_retry(run_id)
        if _charge_settled(run) or time.time() >= settle_deadline:
            break
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
