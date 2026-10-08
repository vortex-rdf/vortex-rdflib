"""A SPARQL endpoint that answers nothing and records every query it receives.

The official test driver draws each query's parameters from seeded pools over
``td_data`` (TestDriver.java ~L497, ``parameterPool.getParametersForQuery``),
never from answers, so an endpoint answering everything empty receives exactly
the stream the driver would send a store. It sends ``GET ?query=<form-encoded>``
one at a time, accepts ``application/rdf+xml`` for DESCRIBE/CONSTRUCT
(counting bytes) and ``application/sparql-results+xml`` otherwise (counting
``<result>``); an empty document of each kind is an answer it accepts.
"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Self
from urllib.parse import parse_qs, urlsplit

EMPTY_RESULTS = (
    b'<?xml version="1.0"?><sparql xmlns="http://www.w3.org/2005/sparql-results#">'
    b"<head/><results/></sparql>"
)
EMPTY_GRAPH = (
    b'<?xml version="1.0"?><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"/>'
)


def answer_for(accept: str | None) -> tuple[str, bytes]:
    if accept and "rdf+xml" in accept:
        return "application/rdf+xml", EMPTY_GRAPH
    return "application/sparql-results+xml", EMPTY_RESULTS


class CaptureServer:
    """``with CaptureServer() as server:`` an endpoint at ``server.url`` (ephemeral
    local port, background thread); ``server.queries`` holds every URL-decoded
    ``query`` parameter in arrival order, ``server.missing`` counts requests
    without one."""

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.missing = 0
        lock = threading.Lock()
        capture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                values = parse_qs(urlsplit(self.path).query).get("query")
                with lock:
                    if values:
                        capture.queries.append(values[0])
                    else:
                        capture.missing += 1
                content_type, body = answer_for(self.headers.get("Accept"))
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                pass  # no access log

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}/sparql"

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()
