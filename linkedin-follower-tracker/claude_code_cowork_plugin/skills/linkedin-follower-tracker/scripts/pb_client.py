"""
PhantomBuster client for the linkedin-follower-tracker Claude port.

This is the ONLY code that talks to PhantomBuster, and it is written to run
UNCHANGED against two backends:
  - the real PhantomBuster REST API (production, tomorrow's run), and
  - the local synthetic look-alike in `pb_fake.py` (development, today).
The only difference between the two is which Transport is injected. That is the
whole point of the transport seam: everything we rehearse against the look-alike
exercises the exact same client logic that will run live.

Real API surface, confirmed from PhantomBuster's own API + MCP tool schemas
(2026-08-31), all under base https://api.phantombuster.com :
  POST /api/v2/agents/launch            body {id, argument}      -> {containerId}
  GET  /api/v2/agents/fetch-output      ?id={agentId}            -> {status, containerId, output}
  GET  /api/v2/containers/fetch-result-object ?id={containerId}  -> {resultObject | jsonUrl | csvURL}
  Auth header on every call: X-Phantombuster-Key: <api key>
  status is one of: starting | running | finished | unknown | launch error | never launched

The money-path rule (covenant): a launch spends money and its result may already
exist or still be arriving, so on ANY failure (a failure status or a transport
error) the client raises PBStop and does NOT retry the launch. Rets are the caller's
explicit decision, never automatic.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Protocol

from pipeline import lead_freshness


# Terminal / failure statuses, exactly as the real API reports them.
STATUS_FINISHED = "finished"
STATUS_RUNNING = "running"
STATUS_STARTING = "starting"
FAILURE_STATUSES = {"unknown", "launch error", "never launched"}


# Capture names — the keys under which the client hands paid handles and results to
# the injected sink (the runner writes each to captures/<name>.json). Named as
# constants so the runner and the tests agree on them without string drift. The
# *_SALVAGED variants carry a best-effort read taken ON the failure path, distinct
# from the clean-run result so an operator can tell "this is what we rescued" apart
# from "this is the finished output".
CAP_COLLECTOR_CONTAINER_ID = "collector_container_id"
CAP_COLLECTOR_RESULT = "collector_result"
CAP_COLLECTOR_RESULT_SALVAGED = "collector_result_salvaged"
CAP_ENRICH_LIST_ID = "enrich_list_id"
CAP_SCRAPER_CONTAINER_ID = "scraper_container_id"
CAP_SCRAPER_LEADS = "scraper_leads"
CAP_SCRAPER_LEADS_SALVAGED = "scraper_leads_salvaged"


class PBStop(Exception):
    """Raised on any failure that must halt the run without retrying (money path)."""


# A saved-but-unenriched lead carries ONLY identity fields: the URL/URN we saved, the
# slug PhantomBuster derives from it, and PB's own bookkeeping (id, editionsHistory).
# Verified against the live prepared list (2026-09-01): a bare record's non-empty keys
# are exactly {editionsHistory, id, linkedinProfileSlug, linkedinProfileUrn}; an enriched
# record adds 26-40 profile/company fields (companyName, linkedinHeadline, location, …).
# So ANY non-empty key outside this set is an enrichment signal. Identifying "bare" by the
# absence of enrichment — rather than by a field-count threshold or a subset test — is
# robust to enrichment fields we have not catalogued, and needs no calibration.
_BARE_LEAD_KEYS = {"id", "_id", "editionsHistory", "linkedinProfileUrn",
                   "linkedinProfileSlug", "linkedinProfileUrl"}


def _is_bare_lead(record: dict) -> bool:
    """True if a lead record carries no enrichment — every non-empty field is an identity
    field. A bare record is a safe-to-drop DUPLICATE only when another record for the same
    person carries the enrichment (see _dedupe_list_leads); on its own it is a
    not-yet-scraped new follower and must be kept."""
    if not isinstance(record, dict):
        return False
    return all(k in _BARE_LEAD_KEYS for k, v in record.items()
               if v not in (None, "", [], {}))


# ---------------------------------------------------------------------------
# Transport seam. The client speaks only through this; the real one uses HTTP,
# the fake one answers in-process. `get_url` fetches an absolute result-file URL
# (the large-output download), which for the real API is an S3 link and for the
# fake is served by the local dev server.
# ---------------------------------------------------------------------------

class Transport(Protocol):
    def post(self, path: str, body: dict) -> dict: ...
    def get(self, path: str, params: dict) -> dict: ...
    def get_url(self, url: str) -> Any: ...


# Characters left untouched when encoding a download URL: every printable ASCII char
# EXCEPT space (0x20) and DEL (0x7F). quote() encodes whatever is OUTSIDE `safe`, so only
# spaces and control chars are percent-encoded while %, /, ?, &, =, : etc. pass through —
# which means a URL that is already valid (including existing %xx escapes) is returned
# unchanged rather than double-encoded.
_URL_SAFE = "".join(map(chr, range(0x21, 0x7F)))


def _encode_download_url(url: str) -> str:
    """Percent-encode a PhantomBuster large-output URL so urllib will accept it.

    PB names the large-output file after the phantom's csvName, which can contain spaces
    (e.g. 'sample followers - 2026-09-03.json'), so the download URL contains spaces —
    and Python's urllib rejects a raw URL with a space as an InvalidURL 'control character',
    which aborted the collector's read AFTER a successful ~52-min paid run (2026-09-03).
    n8n's HTTP node encodes such URLs; our stdlib downloader must too. Encodes only
    space/control chars, so an already-valid URL is unchanged."""
    return urllib.parse.quote(url, safe=_URL_SAFE)


class HttpTransport:
    """Real PhantomBuster transport over HTTPS (stdlib urllib, zero deps)."""

    def __init__(self, api_key: str, base_url: str = "https://api.phantombuster.com",
                 timeout: float | None = 60.0):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        # A timeout is legitimate here: these are deterministic HTTP round-trips,
        # not the (untimed) wait for a job to finish. The waiting happens in the
        # client's poll loop, which sleeps between quick, individually-timed calls.
        self.timeout = timeout

    def _headers(self) -> dict:
        return {"X-Phantombuster-Key": self.api_key, "Content-Type": "application/json"}

    def post(self, path: str, body: dict) -> dict:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base_url + path, data=data,
                                     headers=self._headers(), method="POST")
        return self._send(req)

    def get(self, path: str, params: dict) -> dict:
        qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{self.base_url}{path}?{qs}" if qs else self.base_url + path
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        return self._send(req)

    def get_url(self, url: str) -> Any:
        try:
            req = urllib.request.Request(_encode_download_url(url), method="GET")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            # A large-output download failure is a money-path STOP, not a raw traceback:
            # mirror _send so collect/scrape can salvage and never auto-retry. (The
            # InvalidURL that broke the first live run is prevented upstream by
            # _encode_download_url; this covers a genuine network failure on the download.)
            raise PBStop(f"large-output download failed: {e}") from e

    def _send(self, req) -> dict:
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.URLError as e:
            # Any transport failure is a STOP: we do not know whether the launch
            # took effect, so retrying could double-spend.
            raise PBStop(f"PhantomBuster transport error: {e}") from e
        return json.loads(raw) if raw else {}


# ---------------------------------------------------------------------------
# The client. Pure orchestration over the transport; no PhantomBuster-specific
# HTTP lives here, so it is identical for real and fake.
# ---------------------------------------------------------------------------

class PhantomBusterClient:
    def __init__(self, transport: Transport, *, poll_interval: float = 5.0,
                 max_polls: int = 5000, sleep: Callable[[float], None] = time.sleep,
                 on_poll: Callable[[dict], None] | None = None,
                 on_capture: Callable[[str, Any], None] | None = None,
                 user_agent: str | None = None, adds_per_launch: int = 500,
                 identity_id: str | None = None, csv_name: str | None = None,
                 enrich_list_name: str | None = None):
        """
        poll_interval: seconds between status checks (the between-call wait, not a
            per-call timeout — waiting for a ~1-hour job is normal, not a hang).
        max_polls: a safety ceiling so a stuck job cannot poll forever. The default
            5000 * poll_interval is deliberately generous (hours) so a genuinely long
            phantom run — the collector grows past an hour as the following grows — is
            never cut off; the ceiling only guards against an endless loop.
        sleep: injectable so tests advance the loop without real time passing.
        on_poll: optional heartbeat callback, invoked each poll with a dict
            {agent_id, poll, status, elapsed}. This is how the process signals it is
            still alive and still waiting, without changing the terminal-state logic.
        on_capture: optional capture sink, invoked (name, value) the instant a paid
            handle or result arrives — the container id BEFORE the wait, the result
            AFTER the fetch, and a best-effort salvage read on any post-launch failure.
            This is the capture-once rule: a crash after a launch must still leave the
            paid handle and whatever data was fetched on disk, so no paid result is
            lost to an exception. The runner injects a disk writer; tests a collector.
        """
        self.t = transport
        self.poll_interval = poll_interval
        self.max_polls = max_polls
        self.sleep = sleep
        self.on_poll = on_poll
        self.on_capture = on_capture
        # Production launch settings. user_agent should match the session cookie's
        # browser (reduces LinkedIn detection); adds_per_launch must be >= the number
        # of profiles handed to the scraper or PhantomBuster processes only a subset.
        self.user_agent = user_agent
        self.adds_per_launch = adds_per_launch
        # Optional cosmetic name for the collector's output CSV in PhantomBuster storage,
        # mirroring the n8n collector's `csvName`. Purely a label in the PB console; it
        # does not affect the container result the pipeline reads. Omitted when None.
        self.csv_name = csv_name
        # Optional name for the org-storage enrichment list (a label in the PB console).
        # The list is KEPT after the run — dated, for backtracking — never deleted.
        # None -> the default below.
        self.enrich_list_name = enrich_list_name
        # PhantomBuster identity id for the scraper's session. When set, the scraper is
        # launched with identities:[{identityId,...}] and PhantomBuster uses its own
        # managed (auto-refreshed) session for that identity — the robust choice for a
        # live run, since the li_at cookie in any static export rotates and goes stale.
        self.identity_id = identity_id

    # ----- capture-once salvage plumbing -----

    def _capture(self, name: str, value: Any) -> None:
        """Hand a paid handle/result to the sink the instant it arrives. Capture must
        NEVER itself break the run: the sink is a safety net, so a failure to persist is
        warned about loudly and swallowed — turning a good run into a crash because the
        net tore would defeat the purpose."""
        if not self.on_capture:
            return
        try:
            self.on_capture(name, value)
        except Exception as e:  # noqa: BLE001 - the net must not sink the swimmer
            print(f"WARNING: capture of {name!r} failed (run continues): {e}",
                  file=sys.stderr, flush=True)

    def _salvage_container_result(self, container_id: str, name: str) -> None:
        """On a post-launch collector failure, try ONCE to read whatever the paid
        container holds (possibly a partial result) and capture it. Never raises — we
        are already failing, and the container id is already captured as the durable
        handle, so even if this read fails the run is still recoverable from the console."""
        try:
            records = self.fetch_result(container_id)
        except Exception as e:  # noqa: BLE001 - best effort; already on the failure path
            print(f"WARNING: salvage read of container {container_id} failed: {e}",
                  file=sys.stderr, flush=True)
            return
        if records:
            self._capture(name, records)

    def _salvage_list_leads(self, list_id: str, name: str) -> None:
        """On a post-launch scraper failure, try ONCE to read back whatever enrichment
        landed on the list's leads and capture it. Never raises; the list is kept (not
        deleted) so it also remains a live handle to the same leads."""
        try:
            leads = self.fetch_list_leads(list_id)
        except Exception as e:  # noqa: BLE001 - best effort; already on the failure path
            print(f"WARNING: salvage read of list {list_id} failed: {e}",
                  file=sys.stderr, flush=True)
            return
        if leads:
            self._capture(name, leads)

    # ----- low-level steps, one per real endpoint -----

    def launch(self, agent_id: str, argument: dict) -> str:
        """POST /agents/launch -> container id. Argument goes as an object."""
        resp = self.t.post("/api/v2/agents/launch", {"id": agent_id, "argument": argument})
        container_id = resp.get("containerId") or resp.get("container_id")
        if not container_id:
            # No container id means the launch did not take; STOP (do not re-launch).
            raise PBStop(f"launch returned no containerId: {resp!r}")
        return str(container_id)

    def wait_for_finish(self, agent_id: str) -> None:
        """
        Poll agents/fetch-output until status == finished. STOP on any failure
        status. This is where the 'smarter waiting' strategy lives: a fixed-interval
        poll for now, but the loop is structured so
        a backoff/heartbeat can replace the constant interval without touching the
        terminal-state logic.
        """
        prev_status = None
        for i in range(self.max_polls):
            resp = self.t.get("/api/v2/agents/fetch-output",
                              {"id": agent_id, "prevStatus": prev_status})
            status = resp.get("status")
            if self.on_poll:
                self.on_poll({"agent_id": agent_id, "poll": i + 1, "status": status,
                              "elapsed": resp.get("elapsed")})
            if status == STATUS_FINISHED:
                return
            if status in FAILURE_STATUSES:
                raise PBStop(f"agent {agent_id} reported failure status: {status!r}")
            prev_status = status
            self.sleep(self.poll_interval)
        raise PBStop(f"agent {agent_id} did not finish within {self.max_polls} polls")

    def _assert_container_succeeded(self, container_id: str) -> None:
        """After a phantom reports status='finished', confirm the CONTAINER actually
        exited cleanly. These are two different signals: fetch-output's `status` tracks
        the phantom's lifecycle (it reaches 'finished' whenever the process ends, cleanly
        or not), while the container's `exitCode` is the ground truth for whether the WORK
        succeeded. Verified live 2026-09-01: a scraper launched against a LinkedIn identity
        whose session had expired reported status='finished' AND exitCode=87 ("No valid
        credentials found"); a bad list-URL protocol reported status='finished' AND
        exitCode=1. The old code read only `status`, so both errors were reported to the
        user as success while stale leads were read back — the exact failure this guards.
        Money is already spent by the time we get here (the launch happened), so STOP is
        the safe direction: it declines to declare success and lets the caller keep the
        list as a salvage handle rather than emit garbage as real. exitCode 0 is the only
        pass; anything else — including an unreported/None code — is a loud STOP."""
        info = self.t.get("/api/v2/containers/fetch", {"id": container_id})
        exit_code = info.get("exitCode")
        if exit_code in (0, "0"):
            return
        detail = self._container_failure_reason(container_id)
        raise PBStop(
            f"container {container_id} finished but exited {exit_code!r} "
            f"(only 0 is success): {detail}".rstrip())

    def _container_failure_reason(self, container_id: str) -> str:
        """The human-readable reason a container failed, for the money-path STOP message.
        containers/fetch carries NO message field (verified live 2026-09-02: the real
        response is {id, status, exitCode, endType} — there is no lastEndMessage/
        lastEndStatus, so the old code that read those always produced an EMPTY detail).
        The reason is printed in the container's console `output`: a dead lowercased-URN
        slug logs '⚠️ No Linkedin profile found for <urn>', and a stale session logs the
        exit-87 'No valid credentials found'. Fetch the output ONLY on the failure path
        (it can be large on a full run) and return its signal lines. Best-effort: never
        raises — we are already failing, and the exitCode is already in the STOP even when
        the reason comes back empty."""
        try:
            info = self.t.get("/api/v2/containers/fetch",
                              {"id": container_id, "withOutput": "1"})
        except Exception:  # noqa: BLE001 - already failing; reason is a bonus, not a gate
            return ""
        out = info.get("output")
        if not isinstance(out, str) or not out.strip():
            return ""
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        signal = [ln for ln in lines
                  if "[error_]" in ln or "[warn_]" in ln
                  or "error" in ln.lower() or "no valid credentials" in ln.lower()]
        picked = signal[-4:] if signal else lines[-3:]
        return " | ".join(picked)[:600]

    def fetch_result(self, container_id: str) -> list[dict]:
        """
        GET containers/fetch-result-object, then the n8n 'Is Large Output?' branch:
        if the payload is a large-output POINTER to a result file, download the file;
        otherwise the payload IS the inline records. Always returns a list of records.

        The order matters and was the live-run bug. n8n's 'Is Large Output?' node
        checks for the download URL FIRST and only falls back to the inline result. The
        original port checked resultObject first — and for a large output the REAL
        containers/fetch-result-object response is `{"resultObject": "<JSON string of
        {csvURL, jsonUrl}>"}` (no top-level url; verified live 2026-09-01 against
        a real large-output container). So checking resultObject first returned the S3
        pointer object as if it were ONE follower, and 4392 real followers became "1".
        The fix restores n8n's priority: resolve a download URL first, inline only after.
        """
        resp = self.t.get("/api/v2/containers/fetch-result-object", {"id": container_id})

        # Large output: a pointer to a result file. Download it — those ARE the records.
        url = self._extract_download_url(resp)
        if url:
            return _as_list(self.t.get_url(url))

        # Inline output: resultObject carries the records directly (a JSON array string).
        result_object = resp.get("resultObject")
        if result_object not in (None, "", "null"):
            return _as_list(result_object)

        # Neither a pointer nor inline: nothing to read. Empty, not a crash.
        return []

    @staticmethod
    def _extract_download_url(resp: dict) -> str | None:
        """
        The large-output result-file URL if the payload is a POINTER rather than inline
        records, else None. This is the faithful port of n8n's 'Is Large Output?'
        condition (`{{ $json.jsonUrl }}` notEmpty -> download), adapted to where the URL
        actually lives in the containers/fetch-result-object endpoint.

        Two shapes are handled, in n8n's own priority order:
          1. A top-level `jsonUrl`/`csvURL` — n8n's literal field path (kept in case the
             endpoint ever surfaces it there; it did not in the observed live response).
          2. A `resultObject` that is the pointer object itself: a JSON string parsing to
             `{csvURL, jsonUrl}` (the observed live shape), or defensively a 1-element
             list wrapping that dict. This is our endpoint's actual large-output form.

        The discriminator is safe: a follower record never carries a `jsonUrl`/`csvURL`
        field, so 'the payload points to a jsonUrl/csvURL' cannot be confused with a
        single inline follower. jsonUrl is preferred over csvURL (the pipeline parses
        JSON, and n8n downloads the jsonUrl).
        """
        if not isinstance(resp, dict):
            return None
        top = resp.get("jsonUrl") or resp.get("csvURL")
        if top:
            return top
        ro = resp.get("resultObject")
        if isinstance(ro, str):
            try:
                ro = json.loads(ro)
            except (ValueError, TypeError):
                return None
        pointer: dict | None = None
        if isinstance(ro, dict):
            pointer = ro
        elif isinstance(ro, list) and len(ro) == 1 and isinstance(ro[0], dict):
            pointer = ro[0]
        if isinstance(pointer, dict):
            return pointer.get("jsonUrl") or pointer.get("csvURL") or None
        return None

    # ----- high-level operations, one per pipeline stage -----

    def fetch_agent_session(self, agent_id: str) -> dict:
        """
        Read the agent's CURRENT saved session (cookie, userAgent, identityId) from
        GET /agents/fetch — the freshest cookie PhantomBuster holds for that identity,
        exactly what n8n's "Get an agent" node feeds its launch. Read-only, no cost.

        Handles both saved forms: a top-level sessionCookie (the collector) and an
        identities:[{...}] array (the scraper). Returns a dict with whatever is
        present; missing pieces are None.
        """
        resp = self.t.get("/api/v2/agents/fetch", {"id": agent_id})
        arg = resp.get("argument") if isinstance(resp, dict) else None
        if isinstance(arg, str):
            try:
                arg = json.loads(arg)
            except (ValueError, TypeError):
                arg = {}
        arg = arg or {}
        if arg.get("sessionCookie"):
            return {"sessionCookie": arg.get("sessionCookie"),
                    "userAgent": arg.get("userAgent"),
                    "identityId": arg.get("identityId")}
        for ident in (arg.get("identities") or []):
            if ident.get("sessionCookie") or ident.get("identityId"):
                return {"sessionCookie": ident.get("sessionCookie"),
                        "userAgent": ident.get("userAgent"),
                        "identityId": ident.get("identityId")}
        return {"sessionCookie": None, "userAgent": None, "identityId": None}

    def _resolve_session(self, agent_id: str, override_cookie: str | None) -> dict:
        """The session to launch with. Default: fetch it live from the agent (fresh,
        never stale). An explicit override_cookie short-circuits that (used by tests
        and as an escape hatch), pairing with the configured userAgent/identityId."""
        if override_cookie is not None:
            return {"sessionCookie": override_cookie, "userAgent": self.user_agent,
                    "identityId": self.identity_id}
        return self.fetch_agent_session(agent_id)

    def collect_followers(self, agent_id: str,
                          session_cookie: str | None = None) -> list[dict]:
        """Launch the follower collector and return its follower list. The session is
        fetched live from the agent unless session_cookie overrides it."""
        sess = self._resolve_session(agent_id, session_cookie)
        if not sess.get("sessionCookie"):
            raise PBStop(f"collector agent {agent_id} has no session cookie to launch "
                         f"with (the agent's saved session may need reconnecting)")
        # Faithful to the n8n collector launch (verified against the live workflow,
        # 2026-09-01): the session, plus `enrichLeadsWithAdditionalInformation: true`
        # (n8n always sends it; it makes PB also write the followers into its Leads DB,
        # and does not change the container result we parse), plus the cosmetic `csvName`
        # label when one is configured. Omitting these was the porting-fidelity gap.
        argument: dict = {"sessionCookie": sess["sessionCookie"],
                          "enrichLeadsWithAdditionalInformation": True}
        if sess.get("userAgent"):
            argument["userAgent"] = sess["userAgent"]
        if self.csv_name:
            argument["csvName"] = self.csv_name
        container_id = self.launch(agent_id, argument)
        # Capture the paid handle BEFORE the long wait: if the process dies mid-wait,
        # the container id is on disk and the follower pull can be fetched from the
        # console. This is the whole capture-once point — the collector's output lives
        # ONLY on this container, so losing the id loses the pull.
        self._capture(CAP_COLLECTOR_CONTAINER_ID, container_id)
        try:
            self.wait_for_finish(agent_id)
            # Same guard as the scraper: a collector can report status='finished' while
            # its container exited non-zero (e.g. its own session went stale). Confirm
            # the exitCode before trusting the follower pull.
            self._assert_container_succeeded(container_id)
            records = self.fetch_result(container_id)
        except PBStop:
            # Money spent; try to rescue whatever the container holds, then re-raise
            # (the money-path rule still forbids an automatic re-launch).
            self._salvage_container_result(container_id, CAP_COLLECTOR_RESULT_SALVAGED)
            raise
        self._capture(CAP_COLLECTOR_RESULT, records)
        return records

    def enrich_profiles(self, agent_id: str, lead_inputs: list[dict],
                        session_cookie: str | None = None, *,
                        list_name: str | None = None) -> list[dict]:
        """
        Enrich the new followers and return their enriched org-storage lead records.

        The real "LinkedIn Profile Scraper" does NOT accept a raw profileUrls array
        (verified against its live argument schema, 2026-08-31: it requires a
        `spreadsheetUrl`). So the input is a PhantomBuster-native lead list — the
        platform's documented `acceptsLeadListAsInput` mode. The flow, all on the same
        REST API + token:

          1-3. (prepare_enrichment_list) create a list selecting exactly the new followers
               by URN, save as leads only the ones not already in the CRM, dedupe, and
               verify the list resolves to the full set — NO money spent yet.
          4-6. (scrape_enrichment_list) launch the scraper over
               org-storage://leads/by-list/<listId> (THIS spends money) and read the
               enriched leads back. The list is KEPT (dated) for backtracking.

        This method runs both halves in one call (the all-at-once path). The halves are
        also callable separately so a run can PAUSE at the natural seam — after the list
        is created and proven complete (step 3), before the paid launch (step 4) — which
        is what "pause before the paid step" means. See the two methods below.

        lead_inputs: the output of pipeline.build_lead_inputs — dicts with
        `linkedinProfileUrl` and (for URN-form URLs) `linkedinProfileUrn`.
        """
        if not lead_inputs:
            return []
        list_id = self.prepare_enrichment_list(lead_inputs, list_name=list_name)
        # prepare already captured the id and proved the list complete. scrape spends and
        # reads back; the list is KEPT either way (dated, for backtracking).
        return self.scrape_enrichment_list(agent_id, list_id,
                                           session_cookie=session_cookie)

    def prepare_enrichment_list(self, lead_inputs: list[dict], *,
                                list_name: str | None = None) -> str:
        """
        Steps 1-3: everything BEFORE the paid scrape. Gate the URNs, create the
        URN-selection list, save as leads ONLY the followers not already in the CRM (save
        appends, so saving already-present ones duplicates), dedupe any duplicates, capture
        the list id, and verify the list resolves to the FULL set. Spends NO phantom budget
        — it only writes org-storage leads + a list. Returns the verified list_id, ready
        for `scrape_enrichment_list`.

        This is the natural pause point: on return the list is ACTIVE on PhantomBuster
        and provably complete, and the very next call (scrape) is the one that spends. A
        completeness shortfall STOPs before leaving anything unusable: a URN-less lead
        stops before any write; a list that resolves short is KEPT (dated, for inspection)
        and stops. (Completeness gate #1 lives here: every lead must carry a URN, because the
        list filters ON the URN — a URN-less lead would be saved yet never selected, and
        silently skipped from a paid, client-facing enrichment.)
        """
        if not lead_inputs:
            raise PBStop("no lead inputs to prepare an enrichment list for")

        urns = [l["linkedinProfileUrn"] for l in lead_inputs if l.get("linkedinProfileUrn")]
        if not urns:
            raise PBStop("no URN-form followers to enrich; cannot build the "
                         "org-storage selection list")
        if len(urns) < len(lead_inputs):
            without = [l.get("linkedinProfileUrl") for l in lead_inputs
                       if not l.get("linkedinProfileUrn")]
            raise PBStop(
                f"{len(lead_inputs) - len(urns)} of {len(lead_inputs)} new followers have "
                f"no URN and could not be selected into the enrichment list (e.g. "
                f"{without[:3]}). STOP before a paid scrape that would silently skip them.")

        # Create the URN-selection list FIRST, then save only the people who are not
        # already leads. A PhantomBuster list is a filter over the CRM, so it can be
        # created before its leads exist — it simply resolves to whatever matches. And
        # save-many does NOT upsert by URN: it APPENDS a record. So blindly saving a
        # follower who is already a lead (e.g. one an outreach connection-request phantom
        # already stored) creates a bare DUPLICATE of them. Saving only the MISSING ones
        # is the root fix — an already-present follower is selected by the filter without
        # a second record. (Saving all is what left 89 bare duplicates on the 2026-09-01
        # run.)
        list_id = self.create_list(list_name or self.enrich_list_name
                                   or "linkedin-follower-tracker enrichment", urns)
        # Capture the list id at once (the handle to exactly these leads) so a later
        # crash — or a deliberate pause here — can still find and re-read them.
        self._capture(CAP_ENRICH_LIST_ID, list_id)

        already = {r.get("linkedinProfileUrn") for r in self.fetch_list_leads(list_id)}
        already.discard(None)
        missing = [l for l in lead_inputs if l.get("linkedinProfileUrn") not in already]
        if missing:
            self.save_leads(missing)

        resolved = self._wait_list_resolves(list_id, len(urns))
        # save-many appends, so a re-run (or a transient double-save) can still leave
        # duplicate records; collapse them to one-per-person before the completeness gate
        # and the scrape, so we never pay to scrape the same profile twice. This deletes
        # only bare duplicates, never enriched data — it is the mechanism managing the
        # duplication the platform's append-only save would otherwise accumulate.
        resolved = self._dedupe_list_leads(list_id, resolved)

        distinct = {r.get("linkedinProfileUrn") for r in resolved}
        distinct.discard(None)
        if len(distinct) < len(urns):
            missing_urns = [u for u in urns if u not in distinct]
            # Keep the incomplete list (dated) as a diagnostic handle — no money was spent,
            # and retaining it lets us backtrack WHY it resolved short. Its id is captured.
            raise PBStop(
                f"enrichment list {list_id} resolved {len(distinct)}/{len(urns)} people; "
                f"missing {len(missing_urns)} (e.g. {missing_urns[:3]}). STOP before a "
                f"paid partial scrape — a launch now would enrich an incomplete set. "
                f"The list is kept for inspection.")
        # Honest summary of what this actually did — saves vary (save-the-missing), so the
        # caller must not assume every follower was newly saved.
        print(f"prepared list {list_id}: {len(already)} already leads, saved "
              f"{len(missing)} new, {len(distinct)} people verified in the list.",
              file=sys.stderr, flush=True)
        return list_id

    def scrape_enrichment_list(self, agent_id: str, list_id: str, *,
                               session_cookie: str | None = None,
                               expected: int | None = None) -> list[dict]:
        """
        Steps 4-6: the PAID step. Launch the scraper over
        `org-storage://leads/by-list/<listId>`, wait, and read the now-enriched leads back
        from the scraper's container output. The list is KEPT (dated) for backtracking —
        never deleted.

        `expected`: when resuming a list prepared in an EARLIER invocation, pass the
        expected lead count so the list is RE-verified to still resolve in full before
        any money is spent (a prepared list could have drifted in between). When called
        straight after prepare_enrichment_list in the same run, prepare already proved it,
        so expected is omitted. On a post-launch failure the list is KEPT (a live salvage
        handle) and a best-effort read is captured; the money-path rule still forbids an
        automatic re-launch.
        """
        sess = self._resolve_session(agent_id, session_cookie)
        # Size the paid launch to the list's ACTUAL contents (so the scraper covers every
        # lead, never a subset), and — when resuming a list prepared in an earlier run —
        # RE-verify it still resolves to the expected count before spending. Reading the
        # list is free.
        present = (self._wait_list_resolves(list_id, expected)
                   if expected is not None else self.fetch_list_leads(list_id))
        if expected is not None and len(present) < expected:
            raise PBStop(
                f"prepared enrichment list {list_id} now resolves "
                f"{len(present)}/{expected} leads; STOP before a paid partial scrape.")
        list_count = len(present)

        launched = False
        try:
            argument = {
                # The org-storage list reference the scraper accepts is the by-list
                # RESOURCE PATH, not a bare id: `org-storage://leads/by-list/<listId>`.
                # Verified live 2026-09-01: the bare `org-storage://<listId>` form is
                # rejected at load with exit 1 "Unsupported protocol org-storage:", while
                # this form loads the list and proceeds to LinkedIn. The input field's
                # own regex accepts both, so the regex is NOT the source of truth — the
                # runtime is. This is the exact string the phantom UI writes when a real
                # list is selected.
                "spreadsheetUrl": f"org-storage://leads/by-list/{list_id}",
                "enrichWithCompanyData": True,
                "numberOfAddsPerLaunch": max(self.adds_per_launch, list_count),
                # Faithful to the n8n scraper launch (verified against the live workflow,
                # 2026-09-01). pushResultToCRM is the load-bearing one: in PhantomBuster
                # the "CRM" IS the org-storage Leads DB, so this flag is what makes the
                # scraper WRITE its enrichment back onto the leads — which is exactly what
                # our by-list read-back below reads. n8n sends true; without it the scrape
                # could enrich yet persist nothing and fetch_list_leads would come back
                # bare. updateMonitoringMetadata / crmOutputFieldsMapping are sent exactly
                # as n8n sends them. `columnName` is deliberately NOT sent: the scraper
                # hides it for org-storage input and reads the lead's structured URL
                # instead, so it is a no-op here, not a gap.
                "updateMonitoringMetadata": False,
                "pushResultToCRM": True,
                "crmOutputFieldsMapping": [],
            }
            argument.update(self._scraper_session_args(sess))
            container_id = self.launch(agent_id, argument)   # this spends money
            self._capture(CAP_SCRAPER_CONTAINER_ID, container_id)
            launched = True
            self.wait_for_finish(agent_id)
            # status='finished' is NOT proof the scrape worked: check the container's
            # exitCode before trusting the leads. A stale LinkedIn session finishes with
            # exit 87 yet leaves the leads bare, so reading them back here would report
            # pre-existing/stale data as fresh enrichment. STOP first.
            self._assert_container_succeeded(container_id)
            # Read the enrichment from the SCRAPER'S OWN container output (the resultObject),
            # NOT from the org-storage leads. This restores n8n fidelity: the workflow's
            # "Prepare Enrichment Data" reads "Get the output of an agent1" — the scraper
            # container output — which carries the FULL scrape, including the deep company-
            # page fields (linkedinCompanyName / Description / FollowerCount / EmployeesCount
            # / Size / Headquarter / Specialities). The org-storage by-list read-back was a
            # Google-Sheets -> org-storage porting artifact that persisted only a SUBSET:
            # verified live 2026-09-02 that a real scraper container carried the deep fields
            # 8/10 while the same leads read back via by-list carried them 0/10. pushResultToCRM
            # still writes the leads (so prepare's re-verify and the failure-salvage read work);
            # the success read simply takes the richer source. fetch_result handles the large-
            # output URL branch, exactly as the collector's read does.
            enriched = self.fetch_result(container_id)
            self._capture(CAP_SCRAPER_LEADS, enriched)
            # The list is KEPT — never deleted — so every run's enriched lead set stays
            # reachable on PhantomBuster, uniquely dated, for backtracking. This holds on
            # success and on failure alike; the only cleanup the
            # run does is of bare DUPLICATE lead records inside the list (see _dedupe_list_leads).
            return enriched
        except PBStop:
            # If money was spent, the leads may already carry partial enrichment: rescue
            # a best-effort read now. The list is kept regardless (a live handle to them),
            # so no extra retention step is needed here. Then re-raise — no automatic re-launch.
            if launched:
                self._salvage_list_leads(list_id, CAP_SCRAPER_LEADS_SALVAGED)
            raise

    def _scraper_session_args(self, sess: dict) -> dict:
        """Session block for the scraper launch, from a resolved session dict.

        When the scraper is bound to a PhantomBuster IDENTITY, launch with
        identities:[{identityId}] ALONE — no sessionCookie, no userAgent — so
        PhantomBuster resolves the identity's OWN managed session (the cookie it
        auto-refreshes and keeps in the identity's activeCredentials) and the
        userAgent that session was created with.

        This is deliberate and evidence-based (2026-09-01). The session we read from
        the AGENT's saved argument via agents/fetch is a stale SNAPSHOT: after the
        LinkedIn identity's cookie was reconnected, agents/fetch still returned the
        OLD cookie (different tail) paired with an OLD userAgent (Chrome/144 while the
        refreshed identity was Chrome/152). Passing that snapshot makes the phantom
        try a DEAD cookie first ("New session cookie detected. Trying provided session
        cookie first...") and pins it to a mismatched UA — the failure mode behind the
        exit-87 "No valid credentials found" run. Referencing the identity WITHOUT a
        cookie hands PhantomBuster the session it actually keeps fresh. (The earlier
        code re-sent the fetched cookie on the theory it was fresh; the snapshot
        measurement disproved that theory.)

        Only a scraper with NO identity (a cookie-only agent) sends the top-level
        sessionCookie/userAgent we resolved — there is no managed session to defer to.
        """
        if sess.get("identityId"):
            return {"identities": [{"identityId": sess["identityId"]}]}
        if sess.get("sessionCookie"):
            args = {"sessionCookie": sess["sessionCookie"]}
            if sess.get("userAgent"):
                args["userAgent"] = sess["userAgent"]
            return args
        raise PBStop("scraper has no session (no identityId or cookie) to launch with")

    # ----- org-storage operations (lead list as scraper input) -----

    def save_leads(self, leads: list[dict], batch_size: int = 20) -> int:
        """Save leads via /org-storage/leads/save-many (max 20 per call). Returns the
        count sent. IMPORTANT: save-many does NOT upsert by URN — it APPENDS a new record
        even when a lead for that profile already exists (verified live 2026-09-01: saving
        194 followers, 89 of whom were already leads, produced 89 duplicate records). So
        callers must save only followers who are not already leads; prepare_enrichment_list
        does exactly that (save-the-missing) and _dedupe_list_leads cleans any that slip
        through. Passing an already-present follower here duplicates them."""
        sent = 0
        for i in range(0, len(leads), batch_size):
            batch = leads[i:i + batch_size]
            self.t.post("/api/v2/org-storage/leads/save-many", {"leads": batch})
            sent += len(batch)
        return sent

    def delete_leads(self, lead_ids: list[str], batch_size: int = 20) -> int:
        """Delete lead RECORDS by id via /org-storage/leads/delete-many (batched like
        save-many). Returns the count requested. Used only to remove bare DUPLICATE
        records the append-only save creates — never enriched data (see
        _dedupe_list_leads, which is the only caller and only ever passes bare-record ids)."""
        ids = [str(i) for i in lead_ids if i]
        for i in range(0, len(ids), batch_size):
            self.t.post("/api/v2/org-storage/leads/delete-many",
                        {"ids": ids[i:i + batch_size]})
        return len(ids)

    def create_list(self, name: str, urns: list[str]) -> str:
        """Create a dynamic list selecting exactly the given leads by URN, and return
        its id. A PB list is filter-defined, so 'these specific followers' is expressed
        as `linkedin_profile_urn in [...]`."""
        filt = {"and": [{"filter": {"linkedin_profile_urn": {
            "entity": "lead", "operator": "in", "valueToCompare": list(urns)}}}]}
        # No tag. Lists are kept (dated) for backtracking, so they must be visible in the
        # PhantomBuster console to be findable -- the "hidden" tag we used before hid them.
        # PB's lists/save validates tags against a CLOSED enum
        # (["hidden","workflow","pbai","opinion-leaders"]); an arbitrary descriptive tag is
        # rejected with HTTP 400 "Could not validate data". So we drop the tag entirely (empty
        # list) rather than invent one -- the dated name already provides backtracking.
        resp = self.t.post("/api/v2/org-storage/lists/save",
                           {"name": name, "filter": filt, "tags": []})
        list_id = resp.get("id") if isinstance(resp, dict) else None
        if not list_id:
            raise PBStop(f"lists/save returned no id: {resp!r}")
        return str(list_id)

    def delete_list(self, list_id: str) -> None:
        """Delete a list. NO LONGER called on any automatic path — lists are kept (dated)
        for backtracking. Retained as a thin endpoint wrapper
        for deliberate MANUAL cleanup only. Best-effort: a failure here must not fail the
        run, so a transport error is swallowed."""
        try:
            self.t.post("/api/v2/org-storage/lists/delete", {"id": list_id})
        except PBStop:
            pass

    def fetch_list_leads(self, list_id: str, page_size: int = 100) -> list[dict]:
        """Return all leads in a list, paginating /org-storage/leads/by-list/{listId}."""
        out: list[dict] = []
        offset = 0
        while True:
            resp = self.t.post(
                f"/api/v2/org-storage/leads/by-list/{list_id}",
                {"paginationOptions": {"paginationSize": page_size,
                                       "paginationOffset": offset,
                                       "includeTotalCount": True}})
            if isinstance(resp, list):
                leads = resp
                total = None
            else:
                leads = resp.get("leads") or []
                total = resp.get("totalCount")
            out.extend(leads)
            offset += len(leads)
            if not leads or len(leads) < page_size or (total is not None and offset >= total):
                break
        return out

    def _wait_list_resolves(self, list_id: str, expected: int,
                            max_tries: int = 6) -> list[dict]:
        """Fetch the list's leads, retrying briefly if fewer than expected resolve —
        newly-saved leads can take a moment to become filterable. Returns the best read;
        the caller decides whether a shortfall is a STOP."""
        leads: list[dict] = []
        for i in range(max_tries):
            leads = self.fetch_list_leads(list_id)
            if len(leads) >= expected:
                return leads
            if i < max_tries - 1:
                self.sleep(self.poll_interval)
        return leads

    def _dedupe_list_leads(self, list_id: str, leads: list[dict]) -> list[dict]:
        """
        Collapse duplicate lead records so each person (URN) has ONE record in the list,
        and return the kept records. Because save-many APPENDS rather than upserts, a
        follower already in the CRM gets a second, BARE record every time they are saved.
        This removes those bare duplicates and keeps the enriched record. It is provably
        non-destructive: within a URN group it deletes ONLY bare records (no enrichment
        fields — see _is_bare_lead), and only when a surviving record remains for that
        person, so no profile data can be lost.

        Grounded in the live 2026-09-01 data: 89 of 194 people had exactly {1 enriched,
        1 bare} — the bare records this stage's own save created. Genuinely-new people
        (no enrichment yet) are single bare records with no sibling and are kept untouched.
        """
        from collections import defaultdict
        groups: dict[str, list[dict]] = defaultdict(list)
        no_urn: list[dict] = []
        for r in leads:
            urn = r.get("linkedinProfileUrn")
            if urn:
                groups[urn].append(r)
            else:
                no_urn.append(r)

        kept: list[dict] = list(no_urn)   # URN-less records (shouldn't occur) are untouched
        delete_ids: list[str] = []
        for urn, recs in groups.items():
            if len(recs) == 1:
                kept.append(recs[0])
                continue
            enriched = [r for r in recs if not _is_bare_lead(r)]
            # Keep every enriched record (the freshest wins at read-back). If none is
            # enriched (all bare dupes of the same new person), keep just the freshest.
            keepers = enriched if enriched else [max(recs, key=lead_freshness)]
            keep_marks = {id(r) for r in keepers}
            kept.extend(keepers)
            for r in recs:
                if id(r) in keep_marks:
                    continue
                rid = r.get("id") or r.get("_id")
                if rid and _is_bare_lead(r):
                    delete_ids.append(str(rid))
                else:
                    # Never auto-delete a non-bare record — keep it rather than risk
                    # destroying enrichment we cannot see. (Does not occur in the observed
                    # data; a guard against an unexpected two-enriched-records case.)
                    kept.append(r)
        if delete_ids:
            deleted = self.delete_leads(delete_ids)
            print(f"deduped enrichment list {list_id}: deleted {deleted} bare duplicate "
                  f"lead record(s), keeping one record per person.",
                  file=sys.stderr, flush=True)
        return kept


def _as_list(value: Any) -> list[dict]:
    """Coerce a result payload (JSON string or already-parsed) into a list."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        # Some phantoms wrap the array; unwrap the first list-valued field.
        for v in value.values():
            if isinstance(v, list):
                return v
        return [value]
    return []
