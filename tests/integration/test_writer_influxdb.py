"""The writer against a real InfluxDB, through a proxy that can take it away.

What the unit tests reach only through a scripted post: real HTTP, a real server's answers,
and an outage that behaves like one - the proxy holds each connection for a moment and then
drops it, the way a server that has gone away without refusing does. The proxy also records
every write it sees, which is the only account of what the writer attempted that the writer
did not write itself.

Needs an InfluxDB 1.x at ``INFLUX_TEST_URL`` (default ``http://localhost:8086``), with
authentication off; a run without one is a skip rather than a failure. CI starts one beside
the MQTT broker. Excluded from the default run with the other integration tests
(``pytest -m integration``).
"""

import http.server
import os
import socketserver
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import pytest

from toinflux import writer as writer_module
from toinflux.writer import InfluxWriter

pytestmark = pytest.mark.integration

INFLUX_URL = os.environ.get("INFLUX_TEST_URL", "http://localhost:8086").rstrip("/")


def _influx_reachable():
    try:
        with urllib.request.urlopen(f"{INFLUX_URL}/ping", timeout=2):
            return True
    except OSError:
        return False


def _query(database, statement):
    """Return the rows an InfluxQL statement produces, as lists of values.

    Returns:
        list: rows, empty where the statement produced no series
    """
    url = f"{INFLUX_URL}/query?" + urllib.parse.urlencode({"db": database, "q": statement, "epoch": "s"})
    with urllib.request.urlopen(url, data=b"" if not statement.startswith("SELECT") else None, timeout=5) as reply:
        import json

        result = json.load(reply)["results"][0]
    return [row for series in result.get("series", []) for row in series["values"]]


class _Proxy:
    """A proxy in front of InfluxDB that records every write and can drop connections.

    Attributes:
        url (str): where to send writes instead of InfluxDB
        down (threading.Event): set to drop every request after a short hold
        writes (list): (was it down, the body) for every write request, in order
    """

    def __init__(self):
        self.down = threading.Event()
        self.writes = []
        proxy = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: D102 - the test's output is the assertions
                pass

            def do_POST(self):  # noqa: D102 - part of the proxy
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                proxy.writes.append((proxy.down.is_set(), body.decode()))
                if proxy.down.is_set():
                    time.sleep(0.2)
                    self.close_connection = True
                    return
                request = urllib.request.Request(INFLUX_URL + self.path, data=body, method="POST")
                try:
                    with urllib.request.urlopen(request, timeout=10) as reply:
                        status, data = reply.status, reply.read()
                except urllib.error.HTTPError as exc:
                    status, data = exc.code, exc.read()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        self._server = Server(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def database():
    if not _influx_reachable():
        pytest.skip(f"no InfluxDB at {INFLUX_URL}")
    name = f"writer_it_{uuid.uuid4().hex[:8]}"
    _query(name, f'CREATE DATABASE "{name}"')
    yield name
    _query(name, f'DROP DATABASE "{name}"')


@pytest.fixture
def proxy():
    running = _Proxy()
    yield running
    running.close()


def _wait_until_sent(writer, seconds=30):
    deadline = time.monotonic() + seconds
    while writer.pending() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not writer.pending(), f"the backlog did not drain within {seconds}s"


def test_an_outage_loses_nothing_and_replays_no_heartbeat(database, proxy, tmp_path, monkeypatch):
    monkeypatch.setattr(writer_module, "RETRY_FIRST_SECONDS", 0.2)
    monkeypatch.setattr(writer_module, "RETRY_MAX_SECONDS", 1.0)
    writer = InfluxWriter("integration", str(tmp_path / "spool"), {}, buffer_mb=1)
    destination = f"{proxy.url}/write?db={database}&precision=s"
    for source in ("hue", "octopus"):
        writer.set_destination(source, destination, {"timeout": 2})
    try:
        for n in range(10):
            for source in ("hue", "octopus"):
                writer.submit(source, None, f"{source} n={n}i {1700000000 + n}")
        _wait_until_sent(writer)

        proxy.down.set()
        for n in range(10, 60):
            for source in ("hue", "octopus"):
                writer.submit(source, None, f"{source} n={n}i {1700000000 + n}")
            if n % 10 == 0:
                writer.submit("hue", None, f"collector_status,source=hue ok=1 {1700001000 + n}", buffered=False)
            time.sleep(0.02)
        time.sleep(1.5)
        attempted_live = [body for down, body in proxy.writes if down and body.startswith("collector_status")]
        # At most the one that found the outage: after it, the timer is running and they are
        # dropped unposted, each of which would otherwise have cost a connection that hangs.
        assert len(attempted_live) <= 1, attempted_live

        proxy.down.clear()
        writes_before_recovery = len(proxy.writes)
        _wait_until_sent(writer)
    finally:
        writer.close(2)

    for source in ("hue", "octopus"):
        values = sorted(row[1] for row in _query(database, f'SELECT n FROM "{source}"'))
        assert values == list(range(60)), f"{source}: InfluxDB holds {len(values)} of 60 points"
    assert _query(database, 'SELECT ok FROM "collector_status"') == [], "a heartbeat from the outage was replayed"
    drained = [body for down, body in proxy.writes[writes_before_recovery:] if not down]
    points = sum(len(body.split("\n")) for body in drained)
    # Two databases' worth of interleaved points, so one post each per chunk rather than one
    # per point: the regression the stress run found.
    assert points / len(drained) >= 20, f"{points} points took {len(drained)} posts"
