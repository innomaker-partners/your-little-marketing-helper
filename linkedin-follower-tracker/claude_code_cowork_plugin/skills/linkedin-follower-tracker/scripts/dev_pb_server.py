"""
The local HTTP PhantomBuster look-alike used by --backend http.

Runs a local web server that answers the exact PhantomBuster requests, backed by the
synthetic look-alike (`pb_fake.py`) seeded with synthetic reference data (`dev_seed.py`).
The tool points its base URL at http://localhost:PORT and talks to this exactly as it
will talk to the real service on a live run. This is the closest possible dress rehearsal:
the client's real HTTP stack, JSON serialisation, headers, and large-output URL
download are all exercised for real, against free local data.

Run standalone (blocking):
    python3 dev_pb_server.py --port 8899 --mode sample
    python3 dev_pb_server.py --port 8899 --mode scale --running-polls 3

Or start it in-process (tests / the runner):
    with DevPBServer(mode="sample") as srv:
        base_url = srv.base_url   # e.g. http://127.0.0.1:8899

Endpoints (mirror the real API):
    POST /api/v2/agents/launch
    GET  /api/v2/agents/fetch-output?id=
    GET  /api/v2/containers/fetch-result-object?id=
    GET  /dev-files/<container>.json        (the large-output download)

It ignores the API key on purpose: its whole job is to stand in for the paid service
so the pipeline can be rehearsed end to end for free. Never exposed beyond localhost.
"""

from __future__ import annotations

import argparse
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dev_seed import build_fake, load_scenario


class DevPBServer:
    """A local HTTP PhantomBuster look-alike. Usable as a context manager."""

    def __init__(self, mode: str = "sample", host: str = "127.0.0.1", port: int = 0,
                 running_polls: int = 1, collector_runtime: float = 0.0,
                 scraper_runtime: float = 0.0, **scenario_kwargs):
        self.host = host
        scenario = load_scenario(mode, **scenario_kwargs)
        # port 0 lets the OS pick a free port (good for tests); resolved after bind.
        self._server = ThreadingHTTPServer((host, port), _make_handler(self))
        self.port = self._server.server_address[1]
        self.base_url = f"http://{host}:{self.port}"
        self.fake = build_fake(
            scenario, running_polls=running_polls,
            collector_runtime=collector_runtime, scraper_runtime=scraper_runtime,
            file_url_base=f"{self.base_url}/dev-files/",
        )
        self._thread: threading.Thread | None = None

    def start(self):
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


def _make_handler(srv: "DevPBServer"):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # quiet by default

        def _send(self, obj, code=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw.decode("utf-8")) if raw else {}
            if self.path.startswith("/api/v2/agents/launch"):
                return self._send(srv.fake.launch(body["id"], body.get("argument", {})))
            if self.path.startswith("/api/v2/org-storage/leads/save-many"):
                return self._send(srv.fake.leads_save_many(body.get("leads", [])))
            if self.path.startswith("/api/v2/org-storage/leads/delete-many"):
                return self._send(srv.fake.leads_delete_many(body.get("ids", [])))
            if self.path.startswith("/api/v2/org-storage/lists/save"):
                return self._send(srv.fake.lists_save(body))
            if self.path.startswith("/api/v2/org-storage/lists/delete"):
                return self._send(srv.fake.lists_delete(body))
            if self.path.startswith("/api/v2/org-storage/leads/by-list/"):
                list_id = urllib.parse.urlparse(self.path).path.rsplit("/", 1)[-1]
                return self._send(srv.fake.leads_by_list(list_id, body))
            self._send({"error": "not found"}, 404)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            if parsed.path == "/api/v2/agents/fetch-output":
                return self._send(srv.fake.fetch_output(params.get("id")))
            if parsed.path == "/api/v2/agents/fetch":
                return self._send(srv.fake.agents_fetch(params.get("id")))
            if parsed.path == "/api/v2/containers/fetch-result-object":
                return self._send(srv.fake.fetch_result_object(params.get("id")))
            if parsed.path == "/api/v2/containers/fetch":
                return self._send(srv.fake.containers_fetch(
                    params.get("id"), with_output=bool(params.get("withOutput"))))
            if parsed.path == "/api/v2/org-storage/lists/fetch-all":
                return self._send(srv.fake.lists_fetch_all())
            if parsed.path.startswith("/dev-files/"):
                url = srv.base_url + parsed.path
                return self._send(srv.fake.fetch_file(url))
            self._send({"error": "not found"}, 404)

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Synthetic PhantomBuster (--backend http rehearsal)")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--mode", choices=["sample", "scale"], default="sample")
    ap.add_argument("--running-polls", type=int, default=1,
                    help="how many 'running' polls before 'finished' (raise to mimic slow runs)")
    ap.add_argument("--collector-runtime", type=float, default=0.0,
                    help="seconds the collector phantom takes (models ~1h, compressed)")
    ap.add_argument("--scraper-runtime", type=float, default=0.0,
                    help="seconds the scraper phantom takes (models ~40-45m, compressed)")
    args = ap.parse_args()
    srv = DevPBServer(mode=args.mode, host=args.host, port=args.port,
                      running_polls=args.running_polls,
                      collector_runtime=args.collector_runtime,
                      scraper_runtime=args.scraper_runtime)
    print(f"Synthetic PhantomBuster listening on {srv.base_url}  (mode={args.mode})")
    print("Collector agent id: DEV_COLLECTOR   Scraper agent id: DEV_SCRAPER")
    print("Ctrl-C to stop.")
    try:
        srv._server.serve_forever()
    except KeyboardInterrupt:
        srv.stop()


if __name__ == "__main__":
    main()
