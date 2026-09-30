"""The controls' PID history read back through the MCP read and dashboard tools, on a real InfluxDB.

The record's own tests show what a point carries, and the read layer's tests show how a query is
built. Nothing else showed the two meeting: that the history is listed as a source, that its fields
and their meanings are discovered from what the record actually wrote, that a read scopes to one
control, and that every panel query the dashboard tool suggests runs on the server and returns a
series per control. The tools are called through the MCP server as built, so their registration and
error translation are exercised too, and the dashboard queries are run with Grafana's two macros
substituted, which is all Grafana does to them.

Runs against each real InfluxDB ``conftest.py`` finds - 1.8 and, where one is set up, 2.7 - and a
server that is not there is a skip rather than a failure. Excluded from the default run with the
other integration tests (``pytest -m integration``).
"""

import json
import time

import anyio
import pytest

from tests.harness.installation import Installation
from toinflux.control_record import ACTIVE, FAIL_SAFE, ControlRecord
from toinflux.controller import StepTerms
from toinflux.general import load_settings
from toinflux.mcpserver import build_mcp_server

pytestmark = pytest.mark.integration

#: The fields a dashboard of one control's tuning is built from, and the ones this test reads.
TUNING_FIELDS = ["input", "setpoint", "demand", "p", "i", "d", "delivered", "state"]


@pytest.fixture
def recorded(tmp_path, influx_server, influx_database):
    """Yield an installation whose two controls have written a few cycles to a fresh database.

    Yields:
        tuple: (settings file, the server, database name, when the points were stamped)
    """
    database = influx_database
    installation = Installation(tmp_path, sources=["openmeteo", "carbonintensity"])
    installation.set_settings("influx", **influx_server.settings)
    installation.set_settings("controls", enabled=True, db=database)
    # The server refuses to build without an MCP block; nothing here listens on it.
    installation.set_settings(
        "mcp",
        disabled=False,
        bind_address="127.0.0.1:8420",
        public_url="https://mcp.example.org",
        user="it",
        password="it",
    )
    now = int(time.time())
    for name, base in (("conservatory", 16.0), ("lamp", 120.0)):
        record = ControlRecord(installation.settings_file, instance=name)
        try:
            for cycle in range(3):
                terms = StepTerms(
                    base + cycle, base + 2, 400.0 + cycle, 300.0, 100.0 + cycle, 0.0, 200.0, 0.5, 0.0, curve=()
                )
                record.write(ACTIVE, terms, delivered=375.0 + cycle, timestamp=now - 300 + cycle * 60)
            record.write(FAIL_SAFE, timestamp=now - 60)
        finally:
            record.session.close()
    yield installation.settings_file, influx_server, database, now


def _call(settings_file, tool, **arguments):
    """Call one tool through the MCP server as built, and return its result as data.

    Returns:
        dict: the tool's result
    """
    server = build_mcp_server(load_settings(settings_file), settings_file)
    result = anyio.run(server.call_tool, tool, arguments)
    return json.loads("".join(block.text for block in result.content))


def test_the_history_is_listed_as_a_source(recorded):
    settings_file, _server, _database, _now = recorded
    sources = {entry["source"]: entry for entry in _call(settings_file, "list_sources")["sources"]}
    assert "controls" in sources
    assert sources["controls"]["instance_tag"] == "control"


def test_every_tuning_field_is_discovered_with_its_meaning(recorded):
    settings_file, _server, _database, _now = recorded
    result = _call(settings_file, "list_fields", source="controls", detail=True)
    fields = {entry["field"]: entry for entry in result["fields"]}
    for name in TUNING_FIELDS + ["kp", "ki", "kd"]:
        assert name in fields, f"{name} was not discovered"
        assert fields[name].get("description"), f"{name} carries no meaning"
    # The state field's values mean nothing without the explanation, so both are asserted.
    assert "'active'" in fields["state"]["description"]
    assert "'fail_safe'" in fields["state"]["description"]


def test_a_read_scopes_to_one_control(recorded):
    settings_file, _server, _database, _now = recorded
    result = _call(
        settings_file, "query_history", source="controls", field="delivered", start="-1h", end="now", instance="lamp"
    )
    assert result["instance"] == "lamp"
    assert sorted(point["value"] for point in result["points"]) == [375.0, 376.0, 377.0]
    unscoped = _call(settings_file, "query_history", source="controls", field="delivered", start="-1h", end="now")
    assert set(unscoped["instances"]) == {"conservatory", "lamp"}


def test_the_latest_cycle_is_reported_per_control(recorded):
    settings_file, _server, _database, _now = recorded
    state = _call(settings_file, "get_current_state", source="controls")
    text = json.dumps(state)
    assert "conservatory" in text and "lamp" in text
    assert "fail_safe" in text, "the latest cycle of each control was the fail-safe one"


def test_every_suggested_panel_query_runs_and_splits_by_control(recorded):
    """Grafana substitutes ``$timeFilter`` and ``$__interval`` and runs the query as written;
    this does the same, and every panel must come back with one series per control."""
    settings_file, server, database, _now = recorded
    result = _call(settings_file, "suggest_dashboard_panels", source="controls", fields=TUNING_FIELDS)
    assert result["series_tags"] == ["control"]
    assert sorted(panel["field"] for panel in result["panels"]) == sorted(TUNING_FIELDS)
    for panel in result["panels"]:
        query = panel["query"].replace("$timeFilter", "time > now() - 1h").replace("$__interval", "1m")
        answer = server.query(database, query)
        assert "error" not in answer, f"{panel['field']}: {answer.get('error')}"
        controls = {series["tags"]["control"] for series in answer.get("series", [])}
        assert controls == {"conservatory", "lamp"}, f"{panel['field']}: series for {controls}"
