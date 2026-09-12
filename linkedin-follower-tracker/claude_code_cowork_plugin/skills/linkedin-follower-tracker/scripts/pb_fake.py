"""
The fake PhantomBuster backend used by --backend fake and --backend http.

It answers the exact three requests the client makes (launch, poll status, fetch
result) with the exact shapes and the exact status lifecycle the real API uses, so
the client in `pb_client.py` runs against it UNCHANGED. It is seeded with synthetic
reference data (via `dev_seed.py`). Two ways to use it:

  - In-process (FakeTransport): fastest, deterministic, used by the tests. No
    network, no sleeping.
  - Behind real HTTP (dev_pb_server.py): the tool points its base URL at
    http://localhost:PORT and talks to it over the wire, the closest possible
    rehearsal of tomorrow's live run.

It authenticates nothing (it ignores the API key), because its whole job is to stand
in for the paid service so the pipeline can be rehearsed end to end for free.

What it faithfully reproduces, and why each matters:
  - launch -> containerId, then poll-by-agent, then fetch-result-by-container (the
    real two-id dance; the client's plumbing depends on it).
  - status lifecycle starting -> running -> finished, with a configurable number of
    "running" polls so we can rehearse both fast and production-like (~18 min) timing.
  - inline result (resultObject) for small outputs vs a download URL (jsonUrl) for
    large ones -- the n8n "Is Large Output?" branch. The collector output in 667 was
    large (delivered by URL); the fake reproduces that.
  - failure statuses (launch error) and no-containerId launches, so the money-path
    STOP behaviour is exercised.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from pb_client import PBStop
from pipeline import extract_urn

# PhantomBuster's org-storage lists/save validates `tags` against this closed enum. Any
# other value returns HTTP 400 "Could not validate data" (verified live 2026-09-03). Kept
# here so the fake rejects invalid tags exactly as the real API does.
_LEAD_LIST_TAGS = frozenset({"hidden", "workflow", "pbai", "opinion-leaders"})


@dataclass
class FakeAgent:
    agent_id: str
    kind: str                      # "collector" | "scraper"
    records: list[dict]            # the data this agent "returns"
    large: bool = False            # True -> deliver by URL, not inline
    runtime_seconds: float = 0.0   # >0 -> finish this many real seconds after launch
                                   # (models the real ~1h collector / ~45m scraper,
                                   #  compressed). 0 -> use the running_polls timeline.
    # The agent's saved session, returned by agents/fetch — stands in for the fresh
    # cookie PhantomBuster holds. A scraper with an identity_id reports the
    # identities[] form; otherwise the top-level sessionCookie form.
    session_cookie: str = "FAKE_SESSION_COOKIE"
    user_agent: str | None = "FakeUA/1.0"
    identity_id: str | None = None
    # Container exit code reported by containers/fetch after finish. 0 = clean success;
    # non-zero models a phantom that reaches status='finished' yet FAILED its work
    # (verified live 2026-09-01: exit 87 = "No valid credentials found", exit 1 =
    # "Unsupported protocol"). The status lifecycle and this code are independent, exactly
    # as the real API decouples them — that decoupling is what the client must now catch.
    exit_code: int = 0
    exit_message: str = ""


@dataclass
class PhantomBusterFake:
    """
    In-process synthetic PhantomBuster. Register agents, then answer the client's
    calls. State is per-launch (container), like the real service.
    """
    running_polls: int = 1                 # how many "running" polls before "finished"
    file_url_base: str = "pbfake://files/"  # base for large-output download URLs
    agents: dict[str, FakeAgent] = field(default_factory=dict)
    fail_agents: set[str] = field(default_factory=set)   # -> "launch error"
    launch_returns_no_container: set[str] = field(default_factory=set)
    # URNs a list should NEVER resolve, to model a saved lead that did not become
    # filterable — exercises the pre-launch "list resolved short" money-path STOP.
    drop_urns: set[str] = field(default_factory=set)

    # runtime state
    _containers: dict[str, dict] = field(default_factory=dict)  # container_id -> state
    _latest_by_agent: dict[str, str] = field(default_factory=dict)
    _files: dict[str, list[dict]] = field(default_factory=dict)  # url -> records
    _seq: int = 0
    # org-storage state: the Leads DB and the dynamic lists that select from it. The Leads
    # DB is a LIST of records, not a urn->lead map, because the real leads/save-many APPENDS
    # (does not upsert): two saves of one URN make two records. Modeling it as a map was the
    # unfaithfulness that hid the duplicate bug — a dict cannot hold a duplicate.
    _leads: list[dict] = field(default_factory=list)       # every saved lead RECORD
    _lists: dict[str, dict] = field(default_factory=dict)   # list_id -> {urns, name, enriched}
    _list_seq: int = 0
    _lead_seq: int = 0

    def register(self, agent: FakeAgent) -> None:
        self.agents[agent.agent_id] = agent

    def seed_lead(self, record: dict) -> None:
        """Test/dev helper: place a PRE-EXISTING lead record directly in the Leads DB — as
        an outreach phantom (connection-request / prospect-list scraper) would have, before
        this tool ran. Bypasses save-many so it does not itself count as a save. Assigns an
        id if none is given."""
        rec = dict(record)
        urn = _urn_of(rec)
        if urn:
            rec["linkedinProfileUrn"] = urn
        if not rec.get("id"):
            self._lead_seq += 1
            rec["id"] = f"seed_{self._lead_seq}"
        self._leads.append(rec)

    # ----- endpoint handlers (mirror the three real endpoints) -----

    def launch(self, agent_id: str, argument: dict) -> dict:
        if agent_id in self.launch_returns_no_container:
            return {}  # a launch that silently did not take -> client STOPs
        self._seq += 1
        container_id = f"cont_{self._seq}"
        # The status timeline this container will report, call by call.
        timeline = ["starting"] + ["running"] * max(0, self.running_polls) + ["finished"]
        # The scraper does BOTH: pushResultToCRM enriches the leads in its target
        # org-storage list (read back via by-list — used by prepare's re-verify and by the
        # failure-salvage path), AND its CONTAINER OUTPUT carries the scraped rows with the
        # deep company fields. The client's success read now takes the container output (the
        # richer source, faithful to n8n's "Get the output of an agent1"), so model that
        # output here. (An earlier fake returned [] for the scraper container, encoding the
        # same wrong assumption the client had — that the scrape is read only via by-list —
        # which hid the lost deep fields. Verified live 2026-09-02 the container DOES carry
        # them.) The collector still returns its own rows unchanged.
        agent = self.agents.get(agent_id)
        if agent is not None and agent.kind == "scraper":
            self._enrich_list_from_scraper(argument, agent)
            records = self._scraper_output(argument, agent)
        else:
            records = self._records_for(agent_id, argument)
        exit_code = agent.exit_code if agent is not None else 0
        exit_message = agent.exit_message if agent is not None else ""
        self._containers[container_id] = {
            "agent_id": agent_id, "timeline": timeline, "pos": 0, "records": records,
            "launched_at": time.time(), "exit_code": exit_code,
            "exit_message": exit_message,
        }
        self._latest_by_agent[agent_id] = container_id
        return {"containerId": container_id}

    def agents_fetch(self, agent_id: str) -> dict:
        """Mirror GET /agents/fetch: return the agent's saved argument (JSON string),
        carrying the session the client reads for a fresh cookie."""
        a = self.agents.get(agent_id)
        if a is None:
            return {}
        if a.kind == "scraper" and a.identity_id:
            ident = {"identityId": a.identity_id, "sessionCookie": a.session_cookie}
            if a.user_agent:
                ident["userAgent"] = a.user_agent
            arg = {"identities": [ident]}
        else:
            arg = {"sessionCookie": a.session_cookie}
            if a.user_agent:
                arg["userAgent"] = a.user_agent
        return {"argument": json.dumps(arg)}

    def fetch_output(self, agent_id: str) -> dict:
        if agent_id in self.fail_agents:
            return {"status": "launch error", "containerId": self._latest_by_agent.get(agent_id)}
        container_id = self._latest_by_agent.get(agent_id)
        if not container_id:
            return {"status": "never launched"}
        c = self._containers[container_id]
        agent = self.agents[agent_id]
        if agent.runtime_seconds > 0:
            # Time-based: report running until the real (compressed) runtime elapses,
            # then finished. Models a long phantom run the client must wait through.
            elapsed = time.time() - c["launched_at"]
            status = "finished" if elapsed >= agent.runtime_seconds else "running"
            return {"status": status, "containerId": container_id, "output": "",
                    "elapsed": round(elapsed, 1)}
        # Poll-count timeline (fast path for tests).
        status = c["timeline"][c["pos"]]
        if c["pos"] < len(c["timeline"]) - 1:
            c["pos"] += 1   # advance toward finished; clamp at the end (stays finished)
        return {"status": status, "containerId": container_id, "output": ""}

    def containers_fetch(self, container_id: str, with_output: bool = False) -> dict:
        """Mirror GET /containers/fetch: advances through the container's status timeline
        so the ATTACH path can poll status via containers/fetch alone — it has a container_id
        from the durable capture but no agent_id to call agents/fetch-output.

        The advance mirrors fetch_output's motion exactly, which means:
        - ATTACH path: each call to containers/fetch steps through starting→running→finished,
          returning {id, status} until finished, then adding {exitCode, endType}.
        - ALL-IN-ONE path: wait_for_finish exhausts the timeline via fetch_output first;
          when _assert_container_succeeded then calls containers_fetch, pos is already at the
          'finished' end (clamped), so the extra call is idempotent. exitCode is returned.

        FAITHFUL to the REAL response (verified live 2026-09-02): the base response carries
        {id, status, exitCode, endType} and NO lastEndMessage/lastEndStatus. The failure
        REASON is printed in the console `output`, returned only when withOutput is set.
        The earlier fake invented lastEndMessage/lastEndStatus, which let the client read a
        reason that never exists live — hiding the empty-detail STOP. Now the fake omits
        them and serves the reason via output, exactly as production does."""
        c = self._containers.get(container_id)
        if not c:
            return {}
        # Advance the timeline exactly as fetch_output does, so the ATTACH-path callers
        # (container_status → containers_fetch) see a live lifecycle, not always "finished".
        status = c["timeline"][c["pos"]]
        if c["pos"] < len(c["timeline"]) - 1:
            c["pos"] += 1   # advance toward finished; clamp at the end (stays finished)
        code = c.get("exit_code", 0)
        resp = {"id": container_id, "status": status}
        if status == "finished":
            resp["exitCode"] = code
            resp["endType"] = "finished"
        if with_output:
            resp["output"] = c.get("exit_message", "") or (
                "Process finished successfully" if code == 0
                else f"Process finished with an error (exit code: {code})")
        return resp

    def fetch_result_object(self, container_id: str) -> dict:
        c = self._containers.get(container_id)
        if not c:
            return {"resultObject": None}
        agent = self.agents[c["agent_id"]]
        records = c["records"]
        if agent.large:
            url = f"{self.file_url_base}{container_id}.json"
            self._files[url] = records
            # The REAL large-output response (verified live 2026-09-01 against a real
            # large-output container): the ONLY top-level key is `resultObject`, a JSON STRING
            # of the S3 pointer OBJECT {csvURL, jsonUrl}. There is NO top-level jsonUrl.
            # The earlier fake put jsonUrl at top level with resultObject:None, which
            # matched the OLD (buggy) fetch_result by luck and hid the live-run bug where
            # the pointer-inside-resultObject was returned as a single "follower". Model
            # the real shape so the client is tested against what PhantomBuster truly sends.
            return {"resultObject": json.dumps(
                {"csvURL": url.replace(".json", ".csv"), "jsonUrl": url})}
        return {"resultObject": json.dumps(records)}

    def fetch_file(self, url: str) -> list[dict]:
        return self._files.get(url, [])

    # ----- helpers -----

    def _records_for(self, agent_id: str, argument: dict) -> list[dict]:
        agent = self.agents.get(agent_id)
        if agent is None:
            return []
        return list(agent.records)

    # ----- org-storage endpoint handlers (mirror the real REST surface) -----

    def leads_save_many(self, leads: list[dict]) -> dict:
        """Save leads into the Leads DB. Mirrors the REAL leads/save-many, which APPENDS a
        new record even when a lead for that URN already exists — it does NOT upsert
        (verified live 2026-09-01). Each record gets a unique `id` and an editionsHistory
        entry, exactly as PhantomBuster assigns. So a double-save of one URN yields two
        records — the duplication the client must avoid (save-the-missing) and, failing
        that, clean up (_dedupe_list_leads)."""
        saved = 0
        for lead in leads:
            urn = _urn_of(lead)
            if not urn:
                continue
            self._lead_seq += 1
            self._leads.append({**lead, "linkedinProfileUrn": urn,
                                "id": f"lead_{self._lead_seq}",
                                "editionsHistory": [{"mainAgentId": "save",
                                                     "type": "manual",
                                                     "timestamp": self._lead_seq}]})
            saved += 1
        return {"savedCount": saved}

    def leads_delete_many(self, ids: list[str]) -> dict:
        """Remove lead records by id (mirrors leads/delete-many)."""
        idset = {str(i) for i in ids}
        before = len(self._leads)
        self._leads = [r for r in self._leads if str(r.get("id")) not in idset]
        return {"deletedCount": before - len(self._leads)}

    def lists_save(self, body: dict) -> dict:
        """Create/update a dynamic list. We resolve its `linkedin_profile_urn in [...]`
        filter to a concrete URN set at creation time (enough to stand in for the real
        dynamic resolution for our single, well-defined filter shape).

        Tags are validated against PB's real closed enum: the live API rejects any other
        value with HTTP 400 "Could not validate data" (verified 2026-09-03). Modelling it
        here is what lets the suite catch an invalid-tag regression -- the omission of this
        check is exactly why one reached a live run."""
        for tag in body.get("tags") or []:
            if tag not in _LEAD_LIST_TAGS:
                raise PBStop(
                    f"lists/save rejected tag {tag!r}: not one of {sorted(_LEAD_LIST_TAGS)}")
        list_id = body.get("id")
        if not list_id:
            self._list_seq += 1
            list_id = f"list_{self._list_seq}"
        self._lists[list_id] = {"urns": _urns_from_filter(body.get("filter")),
                                "name": body.get("name"), "enriched": False}
        return {"id": list_id, "name": body.get("name")}

    def lists_delete(self, body: dict) -> dict:
        self._lists.pop(body.get("id"), None)
        return {"id": body.get("id")}

    def lists_fetch_all(self) -> list[dict]:
        return [{"id": lid, "name": l.get("name")} for lid, l in self._lists.items()]

    def leads_by_list(self, list_id: str, body: dict) -> dict:
        lst = self._lists.get(list_id)
        if not lst:
            return {"leads": [], "totalCount": 0}
        urnset = set(lst["urns"])
        # Return only leads that actually EXIST (were saved) and are still filterable — NOT
        # a stub per filter URN. Returning stubs for unsaved URNs was unfaithful: it made
        # the save-the-missing check believe everyone was already present. drop_urns models
        # a saved lead that never became filterable (the pre-launch 'resolved short' STOP).
        leads = [r for r in self._leads
                 if r.get("linkedinProfileUrn") in urnset
                 and r.get("linkedinProfileUrn") not in self.drop_urns]
        opts = (body or {}).get("paginationOptions") or {}
        size = opts.get("paginationSize", len(leads))
        off = opts.get("paginationOffset", 0)
        return {"leads": leads[off:off + size], "totalCount": len(leads)}

    def _scraper_output(self, argument: dict, scraper: FakeAgent) -> list[dict]:
        """The scraper's CONTAINER OUTPUT (resultObject): one row per profile it scraped —
        the seed's scraper records whose URN is in the target by-list, each stamped with
        linkedinProfileUrn exactly as the real scraper output carries it. Models n8n's
        "Get the output of an agent1", which is the enrichment source the client reads on
        success. Off the by-list resource path, returns the raw seed (nothing to filter)."""
        sp = argument.get("spreadsheetUrl", "")
        prefix = "org-storage://leads/by-list/"
        if not sp.startswith(prefix):
            return [dict(r) for r in scraper.records]
        lst = self._lists.get(sp[len(prefix):])
        urnset = set(lst["urns"]) if lst else set()
        out = []
        for r in scraper.records:
            urn = _urn_of(r)
            if urn and urn in urnset:
                out.append({**r, "linkedinProfileUrn": urn})
        return out

    def _enrich_list_from_scraper(self, argument: dict, scraper: FakeAgent) -> None:
        """Model the scraper populating company data onto the leads in its target list.
        Enrichment rows (the seed's scraper records) are matched to leads by URN."""
        # Enrich ONLY on the by-list resource path the real scraper accepts. The bare
        # `org-storage://<id>` form the client used to send is rejected live (exit 1), so
        # the fake must not silently enrich on it either, or it would mask that regression.
        sp = argument.get("spreadsheetUrl", "")
        prefix = "org-storage://leads/by-list/"
        if not sp.startswith(prefix):
            return
        list_id = sp[len(prefix):]
        lst = self._lists.get(list_id)
        if not lst:
            return
        urnset = set(lst["urns"])
        enrich_by_urn = {_urn_of(r): r for r in scraper.records if _urn_of(r)}
        # pushResultToCRM updates the EXISTING lead records in place (mirrors the real
        # scraper), so read-back by list returns them enriched. Every record for a URN in
        # the list is enriched — after dedupe there is one, but this stays correct if not.
        for rec in self._leads:
            urn = rec.get("linkedinProfileUrn")
            if urn in urnset and urn in enrich_by_urn:
                rec.update(enrich_by_urn[urn])
        lst["enriched"] = True


def _key(url: Any) -> str:
    import re
    return re.sub(r"/+$", "", (url or "").strip().lower())


def _urn_of(record: dict) -> str:
    """URN of a lead/enrichment record: an explicit URN field, else extracted from any
    URL-bearing field. Mirrors pipeline.match_key's URN half."""
    urn = (record.get("linkedinProfileUrn") or record.get("linkedin_profile_urn")
           or record.get("profileUrn"))
    if urn:
        return str(urn)
    return extract_urn(record.get("linkedinProfileUrl") or record.get("profileUrl")
                       or record.get("profileLink") or "")


def _urns_from_filter(filt: Any) -> list[str]:
    """Pull the URN list out of the one filter shape the client builds:
    {and:[{filter:{linkedin_profile_urn:{operator:'in', valueToCompare:[...]}}}]}."""
    if not isinstance(filt, dict):
        return []
    for clause in filt.get("and", []):
        f = clause.get("filter") if isinstance(clause, dict) else None
        if isinstance(f, dict):
            spec = f.get("linkedin_profile_urn")
            if isinstance(spec, dict) and isinstance(spec.get("valueToCompare"), list):
                return [str(u) for u in spec["valueToCompare"]]
    return []


# ---------------------------------------------------------------------------
# In-process transport: routes the client's calls straight to the fake. Used by
# tests and by any dev run that does not need real HTTP.
# ---------------------------------------------------------------------------

class FakeTransport:
    def __init__(self, fake: PhantomBusterFake, *, raise_on_next: bool = False):
        self.fake = fake
        self.raise_on_next = raise_on_next  # simulate a mid-run network drop

    def _maybe_raise(self):
        if self.raise_on_next:
            self.raise_on_next = False
            raise PBStop("simulated transport error")

    def post(self, path: str, body: dict) -> dict:
        self._maybe_raise()
        if path.endswith("/agents/launch"):
            return self.fake.launch(body["id"], body.get("argument", {}))
        if path.endswith("/org-storage/leads/save-many"):
            return self.fake.leads_save_many(body.get("leads", []))
        if path.endswith("/org-storage/leads/delete-many"):
            return self.fake.leads_delete_many(body.get("ids", []))
        if path.endswith("/org-storage/lists/save"):
            return self.fake.lists_save(body)
        if path.endswith("/org-storage/lists/delete"):
            return self.fake.lists_delete(body)
        if "/org-storage/leads/by-list/" in path:
            list_id = path.rsplit("/", 1)[-1]
            return self.fake.leads_by_list(list_id, body)
        raise ValueError(f"fake has no POST route for {path}")

    def get(self, path: str, params: dict) -> dict:
        self._maybe_raise()
        if path.endswith("/agents/fetch-output"):
            return self.fake.fetch_output(params["id"])
        if path.endswith("/agents/fetch"):
            return self.fake.agents_fetch(params["id"])
        if path.endswith("/containers/fetch-result-object"):
            return self.fake.fetch_result_object(params["id"])
        if path.endswith("/containers/fetch"):
            return self.fake.containers_fetch(
                params["id"], with_output=bool(params.get("withOutput")))
        if path.endswith("/org-storage/lists/fetch-all"):
            return self.fake.lists_fetch_all()
        raise ValueError(f"fake has no GET route for {path}")

    def get_url(self, url: str) -> Any:
        self._maybe_raise()
        return self.fake.fetch_file(url)
