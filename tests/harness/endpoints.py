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
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field

from tests.harness.certificates import write_self_signed

#: What "the client went away" looks like, which is not one exception type. The TLS ones
#: are not ConnectionErrors, and which of them a dead peer produces depends on the Python
#: version: 3.10-3.12 raise SSLEOFError where 3.13 and later raise ConnectionResetError.
DISCONNECTED = (ConnectionError, ssl.SSLEOFError, ssl.SSLZeroReturnError)


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

    def _drop(self):
        """Record an attempt and close the connection without answering.

        Recorded before it is dropped: a control that keeps trying through an unreachable
        fault *is* still cycling, and an endpoint that recorded nothing would let
        ``kept_cycling`` report a stall that never happened - failing a correct run, which
        is how an invariant ends up switched off.
        """
        endpoint = self.server.endpoint
        with endpoint.lock:
            endpoint.attempts.append(time.monotonic())
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            # The client may have gone already. Nothing to do either way: the point is
            # that no response is sent.
            pass
        self.close_connection = True

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
        # Checked here, where the request has arrived, rather than anywhere earlier in the
        # connection's life. HTTP/1.1 keeps a connection open for many requests, and the
        # server thread sits *inside* its request loop blocked on reading the next request
        # line - so a check made before that read has already been passed by the time the
        # request exists. Every client in this project holds a requests.Session, so with
        # the check one level out a control that had already talked to the bridge sailed
        # straight through an unreachable fault: measured at four answered requests with
        # the fault switched on throughout, which would have proved a control resilient to
        # an outage that never happened.
        if endpoint.unreachable:
            self._drop()
            return
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
        # 204 carries no body, and InfluxDB's /write really does answer 204: a stub that
        # sent JSON there would let code under test come to depend on a parseable body that
        # the real server never sends.
        if code == 204:
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
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

    def handle_error(self, request, client_address):
        """Report a handler failure, unless it is the client having gone away.

        The ``hanging`` fault exists to make a client give up mid-request, which leaves the
        handler writing to a socket that is no longer there. http.server prints a full
        traceback for each one, and those say nothing: the disconnect is what the test asked
        for. Measured before this was added - one `ConnectionResetError` traceback per
        timed-out request, from a background thread, landing in whichever test happened to
        be running.

        Only the disconnect family is swallowed. Anything else is a fault in the harness
        itself and still gets printed, because a stub that hides its own errors is worse
        than a noisy one.

        ``SSLEOFError`` is in that family and is not a ``ConnectionError``: a peer that
        vanishes mid-TLS raises it on Python 3.10 to 3.12, while 3.13 and later map the
        same event onto ``ConnectionResetError``. Filtering on ``ConnectionError`` alone
        was therefore silent on the two versions this was written on and noisy on the three
        older ones - which is exactly what CI reported. Not ``SSLError`` wholesale, because
        a real handshake or certificate fault should still be heard.

        Args:
            request (socket.socket): the connection that failed
            client_address (tuple): where it came from
        """
        if isinstance(sys.exc_info()[1], DISCONNECTED):
            return
        super().handle_error(request, client_address)

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
        #: When a connection was accepted and dropped rather than answered, as monotonic
        #: readings. Kept apart from `requests` because those carry what was asked and
        #: these carry only that somebody asked: a dropped connection is read before any
        #: request line is.
        self.attempts = []
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
        """Forget every request and attempt recorded so far."""
        with self.lock:
            self.requests.clear()
            self.attempts.clear()

    def contacts(self):
        """Return when the endpoint was reached at all, answered or not.

        What "the loop is still running" is measured against: a control talking to an
        endpoint that is refusing to answer is still a control that is running.

        Returns:
            list: monotonic readings, oldest first
        """
        with self.lock:
            return sorted([r.at for r in self.requests] + list(self.attempts))

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
