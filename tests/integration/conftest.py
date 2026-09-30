"""The real InfluxDB servers the integration tests run against: 1.8, and 2.7 where one is set up.

Each test that takes ``influx_server`` runs once per server, because the two versions take
different paths through the code: 1.8 writes to ``/write`` with a user, 2.7 to ``/api/v2/write``
with a token, and 2.7 answers reads through its v1-compatible ``/query``, which finds a bucket by
its own name without a mapping being created (checked against 2.7). A server that is not there is
a skip for that run, not a failure, so a machine with only one of them still runs the other.

- 1.8: ``INFLUX_TEST_URL`` (default ``http://localhost:8086``), with authentication off.
- 2.7: ``INFLUX2_TEST_URL``, ``INFLUX2_TEST_TOKEN`` (an all-access token) and ``INFLUX2_TEST_ORG``.
  The token is generated when CI starts the server, never written into the repository.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field

import pytest


def pytest_collection_modifyitems(items) -> None:
    """Mark every test in this directory as an integration test, by where it lives.

    Each module also marks itself, but a new one that forgot would join the default run and fail
    wherever the servers are absent; marking by location means it cannot be forgotten.

    Args:
        items (list): the collected tests, of every directory
    """
    here = os.path.dirname(__file__)
    for item in items:
        if str(item.fspath).startswith(here + os.sep):
            item.add_marker(pytest.mark.integration)


@dataclass
class InfluxServer:
    """One real InfluxDB, and how the code under test is told to reach it.

    Attributes:
        name (str): which server, for test ids and messages
        url (str): its base URL
        token (str or None): an all-access token, for 2.x; None for 1.x
        org (str or None): the organisation, for 2.x
        settings (dict): the ``influx`` settings block that points at it
    """

    name: str
    url: str
    token: "str | None" = None
    org: "str | None" = None
    settings: dict = field(default_factory=dict)

    def _request(self, path, data=None, method=None, params=None, body=None):
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        headers = {"Authorization": f"Token {self.token}"} if self.token else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        request = urllib.request.Request(self.url + path + query, data=data, method=method, headers=headers)
        with urllib.request.urlopen(request, timeout=10) as reply:
            raw = reply.read()
        return json.loads(raw) if raw else {}

    def reachable(self):
        """Return whether the server answers its ping."""
        try:
            urllib.request.urlopen(f"{self.url}/ping", timeout=2).close()
            return True
        except OSError:
            return False

    def create_database(self, name) -> None:
        """Create a database (1.x) or a bucket of that name (2.x)."""
        if self.token:
            org_id = self._request("/api/v2/orgs", params={"org": self.org})["orgs"][0]["id"]
            self._request("/api/v2/buckets", method="POST", body={"orgID": org_id, "name": name})
        else:
            self._request("/query", data=b"", params={"q": f'CREATE DATABASE "{name}"'})

    def drop_database(self, name) -> None:
        """Remove what ``create_database`` made."""
        if self.token:
            for bucket in self._request("/api/v2/buckets", params={"name": name}).get("buckets", []):
                self._request(f"/api/v2/buckets/{bucket['id']}", method="DELETE")
        else:
            self._request("/query", data=b"", params={"q": f'DROP DATABASE "{name}"'})

    def query(self, database, statement):
        """Return an InfluxQL SELECT's first result, through ``/query`` on either version.

        Returns:
            dict: the result, with ``series`` where anything matched and ``error`` where the
            server refused the statement
        """
        params = {"db": database, "q": statement, "epoch": "s"}
        if self.org:
            params["org"] = self.org
        return self._request("/query", params=params)["results"][0]


def _servers():
    servers = [
        InfluxServer(
            "influxdb-1.8",
            os.environ.get("INFLUX_TEST_URL", "http://localhost:8086").rstrip("/"),
        )
    ]
    servers[0].settings = {"url": servers[0].url, "user": "harness", "password": "harness", "timeout": 5}
    v2_url = os.environ.get("INFLUX2_TEST_URL")
    if v2_url:
        v2 = InfluxServer(
            "influxdb-2.7",
            v2_url.rstrip("/"),
            token=os.environ.get("INFLUX2_TEST_TOKEN"),
            org=os.environ.get("INFLUX2_TEST_ORG"),
        )
        v2.settings = {"url": v2.url, "token": v2.token, "org": v2.org, "timeout": 5}
        servers.append(v2)
    return servers


@pytest.fixture(params=_servers(), ids=lambda server: server.name)
def influx_server(request):
    """Yield each configured InfluxDB in turn, skipping one that is not reachable.

    Yields:
        InfluxServer: the server
    """
    server = request.param
    if not server.reachable():
        pytest.skip(f"no {server.name} at {server.url}")
    yield server


@pytest.fixture
def influx_database(influx_server):
    """Yield the name of a fresh database or bucket on the server, removed afterwards.

    Yields:
        str: the name
    """
    name = f"it_{uuid.uuid4().hex[:10]}"
    influx_server.create_database(name)
    try:
        yield name
    finally:
        try:
            influx_server.drop_database(name)
        except (OSError, urllib.error.URLError) as exc:
            # Tolerated: a leftover database on a throwaway CI server costs nothing, and failing
            # the test for it would hide the result that was being asked for.
            print(f"could not remove {name!r} from {influx_server.name}: {exc!r}")
