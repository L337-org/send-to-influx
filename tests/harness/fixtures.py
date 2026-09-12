"""The harness's pytest fixtures, shared by every test that drives a real control.

Registered as a plugin from ``tests/conftest.py`` rather than copied into each test
module: a stub bridge defined twice is two bridges that drift apart, and the second copy is
always the one nobody updates.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import socket

import pytest

from tests.harness.bridge import StubBridge
from tests.harness.influxdb import StubInflux
from tests.harness.installation import Installation


@pytest.fixture
def bridge():
    """Yield a running stub Hue bridge.

    Yields:
        StubBridge: the bridge, stopped afterwards
    """
    with StubBridge() as running:
        yield running


@pytest.fixture
def influx():
    """Yield a running stub InfluxDB holding one reading per control input.

    Yields:
        StubInflux: the database, stopped afterwards
    """
    with StubInflux({"temperature_2m": 8.5, "conservatory_temperature": 16.0}) as running:
        yield running


@pytest.fixture
def installation(tmp_path, bridge, influx):
    """Yield a throwaway installation wired to both stubs.

    Yields:
        Installation: the installation
    """
    yield Installation(tmp_path, bridge=bridge, influx=influx)


@pytest.fixture(autouse=True)
def no_outbound_network(monkeypatch):
    """Refuse any connection that is not to this machine, loudly.

    A control reads its inputs from InfluxDB and falls back to fetching them live from the
    device - which for Open-Meteo is a hard-coded ``https://api.open-meteo.com`` URL. A test
    that ages a reading past its ``max_age`` therefore asks the real internet for the
    weather, and passes for the wrong reason: measured, one such test ran green while
    fetching live conditions over the network.

    A suite that can reach the internet is a suite whose results depend on someone else's
    uptime, and the failure it produces in CI is a timeout somewhere unrelated. This makes
    the attempt itself the failure, at the point it happens, naming the address.

    Autouse, so it covers tests that never heard of the harness. Loopback is allowed
    because that is where the stub endpoints are.
    """
    real_connect = socket.socket.connect

    def guarded(self, address, *args, **kwargs):
        """Connect, unless the address leaves this machine.

        Args:
            address (tuple or str): where the socket is being pointed
            *args: passed through
            **kwargs: passed through

        Returns:
            object: whatever the real connect returns

        Raises:
            AssertionError: the address is not on this machine
        """
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(
                f"a test tried to connect to {host!r}, which is not this machine - point it at a "
                f"stub endpoint, or the suite depends on somebody else's uptime"
            )
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture
def state_directory(installation, monkeypatch):
    """Point this process's state directory at the installation's, as systemd would.

    Yields:
        Installation: the installation, with STATE_DIRECTORY set for the duration
    """
    monkeypatch.setenv("STATE_DIRECTORY", installation.state_dir)
    yield installation
