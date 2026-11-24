"""Forecasts written to a real InfluxDB, and read back through the MCP read tools.

The unit tests show which slot a forecast lands on and what line is spooled for it. Nothing else
showed what the server does with those lines: that a slot written twice holds one value, the later
(the documented "union of the old field set and the new field set, where any ties go to the new
field set"), on both versions; that a past slot's forecast is read beside the actual at the same
time; and that a future-stamped forecast never surfaces as the live source's newest point.

Runs against each real InfluxDB ``conftest.py`` finds - 1.8 and, where one is set up, 2.7 - and a
server that is not there is a skip rather than a failure. Excluded from the default run with the
other integration tests (``pytest -m integration``).
"""

import json
import time

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tests.harness.installation import Installation
from toinflux.general import load_settings
from toinflux.mcpserver import build_mcp_server
from toinflux.openmeteo import OpenMeteo

pytestmark = pytest.mark.integration

HOUR = 3600


@pytest.fixture
def forecasting(tmp_path, influx_server, influx_database):
    """Yield an installation with Open-Meteo forecasting into a fresh database.

    Yields:
        tuple: (settings file, the server, database name, a past hour, a future hour)
    """
    installation = Installation(tmp_path, sources=["openmeteo"])
    installation.set_settings("influx", **influx_server.settings)
    installation.set_settings("openmeteo", db=influx_database, forecast_lead_hours=[3])
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
    past = now - now % HOUR - HOUR
    future = past + 5 * HOUR
    yield installation.settings_file, influx_server, influx_database, past, future


def _handler(settings_file):
    return OpenMeteo("openmeteo", settings_file=settings_file)


def _call(settings_file, tool, **arguments):
    """Call one tool through the MCP server as built, and return its result as data.

    Returns:
        dict: the tool's result

    Raises:
        ToolError: the tool refused the call
    """
    server = build_mcp_server(load_settings(settings_file), settings_file)
    result = anyio.run(server.call_tool, tool, arguments)
    return json.loads("".join(block.text for block in result.content))


def test_a_slot_written_twice_holds_one_value_the_later(forecasting):
    settings_file, server, database, _, future = forecasting
    handler = _handler(settings_file)
    try:
        handler.send_forecast([(future, {"temperature_2m_forecast_3h": 8.0})])
        handler.send_forecast([(future, {"temperature_2m_forecast_3h": 9.0})])
    finally:
        handler.session.close()
    result = server.query(database, 'SELECT * FROM "weather_forecast"')
    (series,) = result["series"]
    rows = [dict(zip(series["columns"], row)) for row in series["values"]]
    assert rows == [{"time": future, "source": "open-meteo", "temperature_2m_forecast_3h": 9.0}]


def test_a_past_forecast_is_read_beside_the_actual_over_mcp(forecasting):
    settings_file, _, _, past, future = forecasting
    handler = _handler(settings_file)
    try:
        handler.influx_header = "weather,source=open-meteo "
        handler.send_data(data={"temperature_2m": 7.5}, timestamp=past)
        handler.send_forecast(
            [(past, {"temperature_2m_forecast_3h": 7.1}), (future, {"temperature_2m_forecast_3h": 4.0})]
        )
    finally:
        handler.session.close()

    sources = _call(settings_file, "list_sources")["sources"]
    assert "openmeteo_forecast" in [entry["source"] for entry in sources]

    forecast = _call(
        settings_file, "query_history", source="openmeteo_forecast", field="temperature_2m_forecast_3h", start="-3h"
    )
    assert [(point["time"], point["value"]) for point in forecast["points"]] == [(past, 7.1)]

    actual = _call(settings_file, "query_history", source="openmeteo", field="temperature_2m", start="-3h")
    assert [(point["time"], point["value"]) for point in actual["points"]] == [(past, 7.5)]

    fields = {
        entry["field"]: entry for entry in _call(settings_file, "list_fields", source="openmeteo_forecast")["fields"]
    }
    assert fields["temperature_2m_forecast_3h"]["unit"] == "°C"


def test_a_future_forecast_never_reads_as_the_present(forecasting):
    settings_file, _, _, past, future = forecasting
    handler = _handler(settings_file)
    try:
        handler.influx_header = "weather,source=open-meteo "
        handler.send_data(data={"temperature_2m": 7.5}, timestamp=past)
        handler.send_forecast([(future, {"temperature_2m_forecast_3h": 4.0})])
    finally:
        handler.session.close()

    assert _call(settings_file, "get_data_range", source="openmeteo")["latest"] == past

    with pytest.raises(ToolError, match="holds forecasts.*query_history"):
        _call(settings_file, "get_current_state", source="openmeteo_forecast")
