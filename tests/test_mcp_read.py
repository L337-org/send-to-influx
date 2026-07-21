"""Unit tests for toinflux.mcp_read (the MCP read-query layer: query building,
time parsing, field discovery, result annotation, and tool registration)."""

import datetime
from unittest.mock import MagicMock, patch

import anyio
import pytest

from toinflux.exceptions import SourceConnectionError
from toinflux.mcp_read import (
    DEFAULT_RESULT_POINTS,
    MAX_RESULT_POINTS,
    QueryParamError,
    ReadSchema,
    annotate_rows,
    build_query,
    build_schema,
    configured_sources,
    discover_fields,
    parse_time_bound,
    register_read_tools,
    resolve_schema,
    run_query,
    _influx_read_request,
)

NOW = datetime.datetime(2026, 7, 21, 12, 0, 0, tzinfo=datetime.timezone.utc)


def make_schema(**overrides):
    base = dict(
        source="zappi",
        measurement="myenergi",
        db="zappi_db",
        tag_filters={"device": "zappi"},
        allowed_fields={"gen", "grd"},
        field_metadata={"gen": {"unit": "W"}},
    )
    base.update(overrides)
    return ReadSchema(**base)


class TestParseTimeBound:
    def test_now(self):
        assert parse_time_bound("now", now=NOW) == NOW

    @pytest.mark.parametrize(
        "expr,delta",
        [
            ("-24h", datetime.timedelta(hours=-24)),
            ("-7d", datetime.timedelta(days=-7)),
            ("-90m", datetime.timedelta(minutes=-90)),
        ],
    )
    def test_relative(self, expr, delta):
        assert parse_time_bound(expr, now=NOW) == NOW + delta

    def test_iso_with_z(self):
        assert parse_time_bound("2026-07-01T00:00:00Z", now=NOW) == datetime.datetime(
            2026, 7, 1, tzinfo=datetime.timezone.utc
        )

    def test_naive_iso_assumed_utc(self):
        assert parse_time_bound("2026-07-01T06:00:00", now=NOW).tzinfo == datetime.timezone.utc

    def test_aware_iso_converted_to_utc(self):
        # 06:00 at +02:00 is 04:00 UTC
        assert parse_time_bound("2026-07-01T06:00:00+02:00", now=NOW).hour == 4

    @pytest.mark.parametrize("bad", ["", "   ", "'; DROP", "yesterday", "-3x", 5, None])
    def test_invalid(self, bad):
        with pytest.raises(QueryParamError):
            parse_time_bound(bad, now=NOW)


class TestBuildQuery:
    def test_raw_query_structure(self):
        q = build_query(make_schema(), field="gen", start="2026-07-01T00:00:00Z", end="2026-07-02T00:00:00Z")
        assert q.startswith('SELECT "gen" FROM "myenergi" WHERE')
        assert "time >= '2026-07-01T00:00:00Z'" in q
        assert "\"device\" = 'zappi'" in q
        assert q.endswith("ORDER BY time DESC LIMIT 500")

    def test_aggregated_query(self):
        q = build_query(make_schema(), field="gen", start="-1h", end="now", aggregation="mean", group_by="1h")
        assert 'MEAN("gen")' in q
        assert "GROUP BY time(1h) fill(none)" in q

    def test_unknown_field_rejected(self):
        with pytest.raises(QueryParamError, match="unknown field"):
            build_query(make_schema(), field="nope", start="-1h", end="now")

    @pytest.mark.parametrize("evil", ['gen"; DROP', "gen OR 1=1", "gen';--", "a b"])
    def test_injection_field_rejected_as_unknown(self, evil):
        # Not in allowed_fields, so rejected before any interpolation.
        with pytest.raises(QueryParamError):
            build_query(make_schema(), field=evil, start="-1h", end="now")

    def test_identifier_charset_guard_on_allowed_but_unsafe_field(self):
        # Defence in depth: even if an unsafe name slipped into allowed_fields,
        # the identifier validator rejects it before it reaches the query.
        schema = make_schema(allowed_fields={'evil"name'}, field_metadata={})
        with pytest.raises(QueryParamError, match="invalid field name"):
            build_query(schema, field='evil"name', start="-1h", end="now")

    def test_unknown_aggregation_rejected(self):
        with pytest.raises(QueryParamError, match="unknown aggregation"):
            build_query(make_schema(), field="gen", start="-1h", end="now", aggregation="bogus", group_by="1h")

    def test_aggregation_requires_group_by(self):
        with pytest.raises(QueryParamError, match="requires a group_by"):
            build_query(make_schema(), field="gen", start="-1h", end="now", aggregation="mean")

    @pytest.mark.parametrize("bad", ["1", "1x", "h", "-1h", "1 h", "'; DROP"])
    def test_invalid_group_by_rejected(self, bad):
        with pytest.raises(QueryParamError, match="invalid group_by"):
            build_query(make_schema(), field="gen", start="-1h", end="now", aggregation="mean", group_by=bad)

    def test_start_after_end_rejected(self):
        with pytest.raises(QueryParamError, match="must be before"):
            build_query(make_schema(), field="gen", start="now", end="-1h")

    def test_limit_clamped_to_max(self):
        q = build_query(make_schema(), field="gen", start="-1h", end="now", limit=999999)
        assert f"LIMIT {MAX_RESULT_POINTS}" in q

    def test_limit_floor_of_one(self):
        q = build_query(make_schema(), field="gen", start="-1h", end="now", limit=0)
        assert "LIMIT 1" in q

    def test_invalid_limit_rejected(self):
        with pytest.raises(QueryParamError, match="invalid limit"):
            build_query(make_schema(), field="gen", start="-1h", end="now", limit="lots")

    def test_no_tag_filter_omits_tag_clause(self):
        schema = make_schema(tag_filters={}, measurement="hue", source="hue", allowed_fields={"Kitchen"})
        q = build_query(schema, field="Kitchen", start="-1h", end="now")
        assert "device" not in q

    def test_default_limit_used_when_unspecified(self):
        q = build_query(make_schema(), field="gen", start="-1h", end="now")
        assert f"LIMIT {DEFAULT_RESULT_POINTS}" in q

    def test_two_relative_bounds_share_one_reference_time(self):
        # start='-2h', end='-1h' must be exactly one hour apart - both parsed
        # against a single 'now', not two datetime.now() calls.
        import re as _re

        q = build_query(make_schema(), field="gen", start="-2h", end="-1h")
        lo = _re.search(r"time >= '([^']+)'", q).group(1)
        hi = _re.search(r"time <= '([^']+)'", q).group(1)
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        assert datetime.datetime.strptime(hi, fmt) - datetime.datetime.strptime(lo, fmt) == datetime.timedelta(hours=1)


class TestBuildSchema:
    def test_combines_class_metadata_with_discovered_fields(self):
        handler = MagicMock()
        handler.source = "openmeteo"
        handler.MCP_MEASUREMENT = "weather"
        handler.MCP_TAG_FILTERS = {}
        handler.MCP_FIELD_METADATA = {"temperature_2m": {"unit": "°C"}}
        handler.source_settings = {"db": "weather_db"}
        schema = build_schema(handler, {"temperature_2m", "precipitation"})
        assert schema.measurement == "weather"
        assert schema.db == "weather_db"
        assert schema.allowed_fields == {"temperature_2m", "precipitation"}

    def test_bucket_preferred_over_db(self):
        handler = MagicMock()
        handler.source = "hue"
        handler.MCP_MEASUREMENT = None
        handler.MCP_TAG_FILTERS = {}
        handler.MCP_FIELD_METADATA = {}
        handler.source_settings = {"db": "hue_db", "bucket": "hue_bucket"}
        schema = build_schema(handler, set())
        assert schema.measurement == "hue"  # None -> source name
        assert schema.db == "hue_bucket"


class TestReadSchemaMetadata:
    def test_exact_match(self):
        schema = make_schema(field_metadata={"gen": {"unit": "W"}})
        assert schema.metadata_for("gen") == {"unit": "W"}

    def test_suffix_match_for_prefixed_field(self):
        schema = make_schema(field_metadata={"stateValue": {"codes": {1: "locked"}}})
        assert schema.metadata_for("Front_Door_stateValue") == {"codes": {1: "locked"}}

    def test_no_match_returns_empty(self):
        assert make_schema(field_metadata={"gen": {"unit": "W"}}).metadata_for("grd") == {}

    def test_longest_suffix_wins(self):
        # "Front_Door_stateValue" ends with both "_value" and "_stateValue"; the
        # longer, more specific key must win regardless of dict insertion order.
        schema = make_schema(field_metadata={"value": {"unit": "generic"}, "stateValue": {"codes": {1: "locked"}}})
        assert schema.metadata_for("Front_Door_stateValue") == {"codes": {1: "locked"}}
        # And with the keys inserted the other way round.
        schema2 = make_schema(field_metadata={"stateValue": {"codes": {1: "locked"}}, "value": {"unit": "generic"}})
        assert schema2.metadata_for("Front_Door_stateValue") == {"codes": {1: "locked"}}


class TestAnnotateRows:
    def test_unit_added(self):
        result = annotate_rows(make_schema(), "gen", ["time", "gen"], [[100, 5], [200, 7]])
        assert result["unit"] == "W"
        assert result["points"] == [{"time": 100, "value": 5}, {"time": 200, "value": 7}]

    def test_codes_decode_to_labels(self):
        schema = make_schema(
            allowed_fields={"Front_Door_stateValue"},
            field_metadata={"stateValue": {"codes": {1: "locked", 3: "unlocked"}}},
        )
        result = annotate_rows(schema, "Front_Door_stateValue", ["time", "Front_Door_stateValue"], [[10, 1], [20, 3]])
        assert result["points"][0]["label"] == "locked"
        assert result["points"][1]["label"] == "unlocked"
        assert result["codes"] == {"1": "locked", "3": "unlocked"}

    def test_undocumented_code_gets_null_label(self):
        schema = make_schema(field_metadata={"stateValue": {"codes": {1: "locked"}}})
        result = annotate_rows(schema, "Lock_stateValue", ["time", "Lock_stateValue"], [[10, 99]])
        assert result["points"][0]["label"] is None

    def test_aggregated_column_name_handled(self):
        # Aggregated queries name the value column after the function (e.g. "mean").
        result = annotate_rows(make_schema(), "gen", ["time", "mean"], [[100, 120.0]])
        assert result["points"] == [{"time": 100, "value": 120.0}]
        assert result["unit"] == "W"

    def test_empty_values(self):
        result = annotate_rows(make_schema(), "gen", [], [])
        assert result["points"] == []


class TestInfluxReadRequest:
    def test_v1_uses_basic_auth(self):
        url, kwargs = _influx_read_request({"url": "http://influx", "user": "u", "password": "p"}, "db1", "SELECT 1")
        assert url == "http://influx/query"
        assert kwargs["auth"] == ("u", "p")
        assert kwargs["params"]["db"] == "db1"
        assert kwargs["params"]["epoch"] == "s"

    def test_v2_uses_token_header(self):
        url, kwargs = _influx_read_request({"url": "http://influx", "token": "tok", "org": "o"}, "bucket1", "SELECT 1")
        assert kwargs["headers"]["Authorization"] == "Token tok"
        assert kwargs["params"]["db"] == "bucket1"
        assert kwargs["params"]["org"] == "o"

    def test_v2_omits_org_when_absent(self):
        _url, kwargs = _influx_read_request({"url": "http://influx", "token": "tok"}, "b", "SELECT 1")
        assert "org" not in kwargs["params"]

    def test_insecure_toggles_verify(self):
        _url, kwargs = _influx_read_request(
            {"url": "http://influx", "token": "t", "org": "o", "insecure": True}, "b", "SELECT 1"
        )
        assert kwargs["verify"] is False


def _mock_session(json_payload=None, exc=None):
    session = MagicMock()
    response = MagicMock()
    if exc is not None:
        session.get.side_effect = exc
    else:
        response.json.return_value = json_payload
        response.raise_for_status.return_value = None
        session.get.return_value = response
    return session


class TestDiscoverFields:
    def test_parses_field_keys(self):
        payload = {
            "results": [
                {"series": [{"columns": ["fieldKey", "fieldType"], "values": [["gen", "float"], ["grd", "float"]]}]}
            ]
        }
        fields = discover_fields(
            _mock_session(payload), {"url": "http://x", "token": "t", "org": "o"}, "db", "myenergi"
        )
        assert fields == {"gen", "grd"}

    def test_empty_when_no_series(self):
        fields = discover_fields(
            _mock_session({"results": [{}]}), {"url": "http://x", "user": "u", "password": "p"}, "db", "hue"
        )
        assert fields == set()

    def test_transport_failure_raises_source_connection_error(self):
        import requests

        session = _mock_session(exc=requests.exceptions.ConnectionError("down"))
        with pytest.raises(SourceConnectionError):
            discover_fields(session, {"url": "http://x", "user": "u", "password": "p"}, "db", "hue")


class TestRunQuery:
    def test_returns_columns_and_values(self):
        payload = {"results": [{"series": [{"columns": ["time", "gen"], "values": [[1, 100]]}]}]}
        cols, vals = run_query(
            _mock_session(payload), {"url": "http://x", "user": "u", "password": "p"}, "db", "SELECT 1"
        )
        assert cols == ["time", "gen"]
        assert vals == [[1, 100]]

    def test_query_error_raises(self):
        payload = {"results": [{"error": "boom"}]}
        with pytest.raises(SourceConnectionError, match="rejected the query"):
            run_query(_mock_session(payload), {"url": "http://x", "token": "t", "org": "o"}, "db", "SELECT 1")

    def test_no_series_returns_empty(self):
        cols, vals = run_query(
            _mock_session({"results": [{}]}), {"url": "http://x", "user": "u", "password": "p"}, "db", "q"
        )
        assert (cols, vals) == ([], [])


class TestConfiguredSources:
    def test_uses_sources_list(self):
        assert configured_sources({"sources": ["Hue", "Zappi"]}) == ["hue", "zappi"]

    def test_falls_back_to_default_source(self):
        assert configured_sources({"default_source": "octopus"}) == ["octopus"]


class TestResolveSchema:
    def test_unknown_source_rejected(self):
        with pytest.raises(QueryParamError, match="unknown source"):
            resolve_schema("nosuch", {"sources": ["hue"]}, None)

    @pytest.mark.parametrize("bad", [None, "", "   ", 5, ["hue"]])
    def test_non_string_or_empty_source_is_query_param_error(self, bad):
        # A clean tool error, not an AttributeError from .lower() on a non-string.
        with pytest.raises(QueryParamError, match="non-empty string"):
            resolve_schema(bad, {"sources": ["hue"]}, None)

    def test_builds_schema_from_handler_and_discovery(self):
        handler = MagicMock()
        handler.source = "zappi"
        handler.MCP_MEASUREMENT = "myenergi"
        handler.MCP_TAG_FILTERS = {"device": "zappi"}
        handler.MCP_FIELD_METADATA = {"gen": {"unit": "W"}}
        handler.source_settings = {"db": "zappi_db"}
        handler.session = MagicMock()
        settings = {"sources": ["zappi"], "influx": {"url": "http://x", "user": "u", "password": "p"}}
        with (
            patch("toinflux.mcp_read.get_class", return_value=handler),
            patch("toinflux.mcp_read.discover_fields", return_value={"gen", "grd"}),
        ):
            _handler, schema = resolve_schema("zappi", settings, None)
        assert schema.measurement == "myenergi"
        assert schema.db == "zappi_db"
        assert schema.allowed_fields == {"gen", "grd"}
        assert schema.tag_filters == {"device": "zappi"}


class TestRegisterReadTools:
    """Register the tools on a real FastMCP and drive them with mocked InfluxDB."""

    def _server(self):
        from mcp.server.fastmcp import FastMCP

        return FastMCP(name="test")

    def _settings(self):
        return {
            "sources": ["zappi"],
            "influx": {"url": "http://x", "user": "u", "password": "p"},
            "zappi": {"db": "zappi_db"},
        }

    def _handler(self):
        handler = MagicMock()
        handler.source = "zappi"
        handler.MCP_MEASUREMENT = "myenergi"
        handler.MCP_TAG_FILTERS = {"device": "zappi"}
        handler.MCP_FIELD_METADATA = {"gen": {"unit": "W"}}
        handler.source_settings = {"db": "zappi_db"}
        handler.session = MagicMock()
        return handler

    def test_all_three_tools_registered(self):
        server = self._server()
        register_read_tools(server, self._settings(), None)
        names = {t.name for t in anyio.run(server.list_tools)}
        assert names == {"list_sources", "list_fields", "query_history"}

    def test_list_sources(self):
        server = self._server()
        register_read_tools(server, self._settings(), None)
        with patch("toinflux.mcp_read.get_class", return_value=self._handler()):
            result = anyio.run(server.call_tool, "list_sources", {})
        text = result[0][0].text if isinstance(result, tuple) else result[0].text
        assert "myenergi" in text and "zappi" in text

    def test_query_history_end_to_end(self):
        server = self._server()
        register_read_tools(server, self._settings(), None)
        payload = {"results": [{"series": [{"columns": ["time", "gen"], "values": [[100, 42]]}]}]}
        with (
            patch("toinflux.mcp_read.get_class", return_value=self._handler()),
            patch("toinflux.mcp_read.discover_fields", return_value={"gen"}),
            patch("toinflux.mcp_read.run_query", return_value=(["time", "gen"], [[100, 42]])),
        ):
            result = anyio.run(
                server.call_tool,
                "query_history",
                {"source": "zappi", "field": "gen", "start": "-1h", "end": "now"},
            )
        _ = payload
        text = result[0][0].text if isinstance(result, tuple) else result[0].text
        assert '"value": 42' in text
        assert '"unit": "W"' in text

    def test_query_history_bad_field_is_tool_error(self):
        server = self._server()
        register_read_tools(server, self._settings(), None)
        with (
            patch("toinflux.mcp_read.get_class", return_value=self._handler()),
            patch("toinflux.mcp_read.discover_fields", return_value={"gen"}),
        ):
            with pytest.raises(Exception) as excinfo:
                anyio.run(
                    server.call_tool,
                    "query_history",
                    {"source": "zappi", "field": "evil", "start": "-1h", "end": "now"},
                )
        assert "unknown field" in str(excinfo.value)
