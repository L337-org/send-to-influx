"""Stub HTTP endpoints a real control process can be pointed at.

Two of them, because a control process talks to exactly two kinds of far end: it reads its
inputs from InfluxDB and it commands its devices through a Hue bridge. Both are the real
protocol over a real socket, so the code under test uses its own handlers, its own session,
its own timeouts and its own error mapping. A stub that was a mock of a handler would prove
none of that.

Each endpoint carries the same three faults, because they are the three ways a far end
fails in a way the client has to survive:

``unreachable``
    accepts the connection and drops it. Not a refused connection: a refused port cannot be
    reopened reliably in a test, and from the client's side both arrive as the same
    exception, which is the thing being exercised.
``hang_seconds``
    answers, slowly. The one that finds a missing timeout, and the reason it is a number
    rather than a flag: it has to be settable either side of the client's own.
``status``
    answers with an HTTP error. The far end is up and refusing, which is a different code
    path from both of the above.

Every request is recorded with a monotonic timestamp, and that record is what the
invariants read. It is written by the endpoint rather than by the process under test, so a
control that believes it commanded a device and did not cannot hide it.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import http.server
import json
import os
import shutil
import socket
import ssl
import threading
import time
import urllib.parse
from dataclasses import dataclass, field

from tests.harness.certificates import write_self_signed


@dataclass
class Request:
    """One request an endpoint answered.

    Attributes:
        at (float): ``time.monotonic()`` when the request arrived.
        method (str): the HTTP method.
        path (str): the path, without the query string.
        query (dict): the parsed query string.
        body (object): the parsed JSON body, or None.
    """

    at: float
    method: str
    path: str
    query: dict = field(default_factory=dict)
    body: object = None


class _Handler(http.server.BaseHTTPRequestHandler):
    """Routes a request to the endpoint that owns the server, applying its faults first."""

    protocol_version = "HTTP/1.1"

    def handle(self):
        """Serve this connection, or drop it where the endpoint is playing unreachable."""
        if self.server.endpoint.unreachable:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                # The client may have gone already. Nothing to do either way: the point is
                # that no response is sent.
                pass
            self.close_connection = True
            return
        super().handle()

    def log_message(self, format, *args):  # noqa: A002 - the base class names it `format`
        """Discard the default stderr access log.

        Args:
            format (str): the base class's format string, unused
            *args: its arguments, unused
        """

    def do_GET(self):  # noqa: N802 - the base class's naming
        """Answer a GET."""
        self._answer("GET", None)

    def do_PUT(self):  # noqa: N802 - the base class's naming
        """Answer a PUT."""
        self._answer("PUT", self._read_body())

    def do_POST(self):  # noqa: N802 - the base class's naming
        """Answer a POST."""
        self._answer("POST", self._read_body())

    def _read_body(self):
        """Return the request body, parsed as JSON where it is JSON.

        Returns:
            object: the parsed body, the raw text where it is not JSON, or None
        """
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return raw

    def _answer(self, method, body):
        """Record the request, apply the remaining faults, and send the endpoint's reply.

        Args:
            method (str): the HTTP method
            body (object): the parsed request body
        """
        endpoint = self.server.endpoint
        parsed = urllib.parse.urlparse(self.path)
        request = Request(
            at=time.monotonic(),
            method=method,
            path=parsed.path,
            query={k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()},
            body=body,
        )
        with endpoint.lock:
            endpoint.requests.append(request)
            hang, status = endpoint.hang_seconds, endpoint.status
        if hang:
            time.sleep(hang)
        if status:
            self._send(status, {"error": f"the endpoint was told to answer {status}"})
            return
        code, payload = endpoint.respond(request)
        self._send(code, payload)

    def _send(self, code, payload):
        """Send a JSON response.

        Args:
            code (int): the HTTP status
            payload (object): the body, serialised as JSON
        """
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class _Server(http.server.ThreadingHTTPServer):
    """A threading server that knows which endpoint owns it."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, endpoint):
        """Bind to an ephemeral loopback port.

        Args:
            endpoint (StubEndpoint): the endpoint this serves
        """
        self.endpoint = endpoint
        super().__init__(("127.0.0.1", 0), _Handler)


class StubEndpoint:
    """A real HTTP server answering one protocol, with faults that can be switched on.

    Attributes:
        requests (list): every request answered, oldest first.
        unreachable (bool): drop connections instead of answering.
        hang_seconds (float): wait this long before answering.
        status (int or None): answer with this HTTP status instead of a real reply.
    """

    def __init__(self, tls=False, certificate=None):
        """Start the server on an ephemeral loopback port.

        Args:
            tls (bool): serve HTTPS with a self-signed certificate
            certificate (tuple or None): an existing ``(certificate_path, key_path)`` to
                reuse, generated when None and ``tls`` is set
        """
        self.requests = []
        self.unreachable = False
        self.hang_seconds = 0.0
        self.status = None
        self.lock = threading.Lock()
        self._server = _Server(self)
        self.scheme = "https" if tls else "http"
        # The directory to remove on the way out, and None where the caller supplied its own
        # certificate: a shared one outlives this endpoint, and deleting it would break the
        # next endpoint that was given it.
        self._own_certificate_dir = None
        if tls:
            if certificate is None:
                certificate = write_self_signed()
                self._own_certificate_dir = os.path.dirname(certificate[0])
            certificate_path, key_path = certificate
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate_path, key_path)
            self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def host(self):
        """Return ``127.0.0.1:<port>``, the form a settings file names a host in.

        Returns:
            str: the host and port
        """
        return f"127.0.0.1:{self._server.server_address[1]}"

    @property
    def url(self):
        """Return the endpoint's base URL.

        Returns:
            str: scheme, host and port
        """
        return f"{self.scheme}://{self.host}"

    def respond(self, request):
        """Return ``(status, payload)`` for one request.

        Args:
            request (Request): the request to answer

        Returns:
            tuple: the HTTP status and the object to serialise as the body

        Raises:
            NotImplementedError: this base class answers nothing
        """
        raise NotImplementedError

    def clear(self) -> None:
        """Forget every request recorded so far."""
        with self.lock:
            self.requests.clear()

    def paths(self, method=None):
        """Return the paths requested so far, in order.

        Args:
            method (str or None): only this method, or every method when None

        Returns:
            list: the paths
        """
        with self.lock:
            return [r.path for r in self.requests if method is None or r.method == method]

    def stop(self) -> None:
        """Shut the server down, wait for its thread, and remove what it generated.

        The certificate directory goes with it. One per endpoint per test adds up to a
        great many ``harness-tls-*`` directories under the system temp dir over a run, and a
        test process that litters outside its own tree is a process somebody has to clean up
        after by hand.

        Raises:
            AssertionError: the server thread outlived the shutdown, which leaves a live
                socket answering in the background of every test that follows
        """
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        # A timed join that nobody checks is the same silence this harness refuses
        # everywhere else: the thread either stopped or it did not, and "did not" means the
        # rest of the suite runs with a live endpoint answering behind it. That failure
        # surfaces as flakiness somewhere unrelated, which is the expensive kind.
        if self._thread.is_alive():
            raise AssertionError(f"the stub endpoint at {self.url} did not shut down within 5s")
        # After the aliveness check, deliberately: a server still running would still be
        # holding this certificate open.
        if self._own_certificate_dir:
            shutil.rmtree(self._own_certificate_dir, ignore_errors=True)
            self._own_certificate_dir = None

    def __enter__(self):
        """Return the running endpoint.

        Returns:
            StubEndpoint: this endpoint
        """
        return self

    def __exit__(self, *_exception) -> None:
        """Stop the server."""
        self.stop()
