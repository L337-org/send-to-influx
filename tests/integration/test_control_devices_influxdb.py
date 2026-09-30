"""The per-device summary against a real InfluxDB: written by the record, read by the tool.

The unit tests in ``tests/test_mcp_controls.py`` answer the tool's queries from a list, so they
show the shaping and none of the InfluxQL. Here the points go through the record and the writer
to a real server, and the summary is whatever that server makes of the tool's own queries -
which is where an aggregate InfluxQL refuses, a boolean that cannot be filtered on, or a tag
that comes back differently would show.

Needs an InfluxDB 1.x at ``INFLUX_TEST_URL`` (default ``http://localhost:8086``), with
authentication off; a run without one is a skip rather than a failure. Excluded from the
default run with the other integration tests (``pytest -m integration``).
"""

import json
import os
import urllib.parse
import urllib.request
import uuid

import pytest

from tests.harness.installation import Installation
from toinflux.control_record import ControlRecord
from toinflux.mcp_controls import _control_devices_result
from toinflux.staging import DeviceWindow

pytestmark = pytest.mark.integration

INFLUX_URL = os.environ.get("INFLUX_TEST_URL", "http://localhost:8086").rstrip("/")

#: 2023-11-14T22:13:20Z, and the two cycles after it.
FIRST = 1700000000


def _statement(statement):
    url = f"{INFLUX_URL}/query?" + urllib.parse.urlencode({"q": statement})
    with urllib.request.urlopen(url, data=b"", timeout=5) as reply:
        return json.load(reply)


@pytest.fixture
def installation(tmp_path):
    """Yield an installation recording to a fresh database on the real InfluxDB.

    Yields:
        Installation: with ``controls.db`` set, the database dropped afterwards
    """
    try:
        urllib.request.urlopen(f"{INFLUX_URL}/ping", timeout=2).close()
    except OSError:
        pytest.skip(f"no InfluxDB at {INFLUX_URL}")
    database = f"devices_it_{uuid.uuid4().hex[:8]}"
    _statement(f'CREATE DATABASE "{database}"')
    built = Installation(tmp_path, sources=["openmeteo", "carbonintensity"])
    built.set_settings("influx", url=INFLUX_URL)
    built.set_settings("controls", enabled=True, db=database)
    try:
        yield built
    finally:
        _statement(f'DROP DATABASE "{database}"')


def test_three_cycles_summarise_to_what_was_written(installation):
    record = ControlRecord(installation.settings_file, instance="conservatory")
    driven = {"lamp": "brightness_pct"}
    cycles = [
        ({"near": DeviceWindow(300.0, 120.0, None, 1), "lamp": DeviceWindow(300.0, None, 20.0, 1)}, set()),
        ({"near": DeviceWindow(300.0, 300.0, None, 0), "lamp": DeviceWindow(300.0, None, 20.0, 0)}, {"near", "lamp"}),
        ({"near": DeviceWindow(300.0, 0.0, None, 1), "lamp": DeviceWindow(300.0, None, 80.0, 1)}, set()),
    ]
    try:
        for index, (windows, held) in enumerate(cycles):
            record.write_devices(windows, held, driven, timestamp=FIRST + index * 300)
    finally:
        record.session.close()

    result = _control_devices_result(
        "conservatory", "2023-11-14T00:00:00Z", "2023-11-15T00:00:00Z", installation.settings_file
    )

    lamp, near = result["devices"]
    assert near == {
        "device": "near",
        "cycles": 3,
        "active_seconds": 900,
        "kind": "switched",
        "on_share": 0.4667,
        "changes": 2,
        "changes_per_active_hour": 8.0,
        "held_share": 0.3333,
    }
    assert lamp["parameter"] == "brightness_pct"
    assert lamp["value"] == {"mean": 40, "min": 20, "max": 80}
    assert lamp["held_share"] == 0.3333


def test_another_control_and_another_period_are_not_counted(installation):
    record = ControlRecord(installation.settings_file, instance="office")
    try:
        record.write_devices({"lamp": DeviceWindow(60.0, 60.0, None, 1)}, set(), {}, timestamp=FIRST)
    finally:
        record.session.close()
    settings = installation.settings_file
    assert (
        _control_devices_result("conservatory", "2023-11-14T00:00:00Z", "2023-11-15T00:00:00Z", settings)["devices"]
        == []
    )
    assert _control_devices_result("office", "2023-11-15T00:00:00Z", "2023-11-16T00:00:00Z", settings)["devices"] == []
    assert (
        len(_control_devices_result("office", "2023-11-14T00:00:00Z", "2023-11-15T00:00:00Z", settings)["devices"]) == 1
    )
