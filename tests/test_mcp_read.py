"""Unit tests for toinflux.mcp_read (the MCP read-query layer: query building,
time parsing, field discovery, result annotation, and tool registration)."""

import datetime
from unittest.mock import MagicMock, patch

import anyio
import pytest
import requests

from toinflux.exceptions import SourceConnectionError, ToolParamError
from toinflux.general import get_class
from toinflux.philipshue import Hue
from toinflux.mcp_read import (
    DEFAULT_RESULT_POINTS,
    MAX_RESULT_POINTS,
    ReadSchema,
    annotate_rows,
    build_documentation,
    build_latest_query,
    build_query,
    build_schema,
    current_state_result,
    discover_fields,
    discover_tag_values,
    list_fields_result,
    metadata_for,
    parse_time_bound,
    register_read_tools,
    resolve_db,
    resolve_schema,
    QuerySeries,
    run_query,
    single_series,
    configured_instances,
    _validate_instance,
    _annotate_state_field,
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
        with pytest.raises(ToolParamError):
            parse_time_bound(bad, now=NOW)

    @pytest.mark.parametrize("future", ["24h", "+24h", "1d"])
    def test_bare_or_positive_offset_rejected(self, future):
        # Relative offsets must be past-only (leading '-'); a future range has no
        # data (collectors only write at present time). ISO timestamps cover any
        # genuine future need.
        with pytest.raises(ToolParamError):
            parse_time_bound(future, now=NOW)


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
        with pytest.raises(ToolParamError, match="unknown field"):
            build_query(make_schema(), field="nope", start="-1h", end="now")

    @pytest.mark.parametrize("evil", ['gen"; DROP', "gen OR 1=1", "gen';--", "a b"])
    def test_injection_field_rejected_as_unknown(self, evil):
        # Not in allowed_fields, so rejected before any interpolation.
        with pytest.raises(ToolParamError):
            build_query(make_schema(), field=evil, start="-1h", end="now")

    def test_control_char_in_allowlisted_field_rejected(self):
        # Defence in depth: a control character (which could corrupt the query or
        # a log line) is rejected even if it somehow reached allowed_fields.
        schema = make_schema(allowed_fields={"evil\nname"}, field_metadata={})
        with pytest.raises(ToolParamError, match="invalid field name"):
            build_query(schema, field="evil\nname", start="-1h", end="now")

    def test_punctuated_field_name_is_queryable_and_escaped(self):
        # A legitimate field with punctuation - e.g. a Hue light "Kitchen (main)"
        # stored as "Kitchen_(main)" - must be queryable, not rejected. A field
        # with a double quote is double-quote-escaped rather than refused.
        schema = make_schema(source="hue", measurement="hue", tag_filters={}, allowed_fields={"Kitchen_(main)", 'a"b'})
        q = build_query(schema, field="Kitchen_(main)", start="-1h", end="now")
        assert 'SELECT "Kitchen_(main)" FROM "hue"' in q
        q2 = build_query(schema, field='a"b', start="-1h", end="now")
        assert 'SELECT "a\\"b"' in q2

    def test_unknown_aggregation_rejected(self):
        with pytest.raises(ToolParamError, match="unknown aggregation"):
            build_query(make_schema(), field="gen", start="-1h", end="now", aggregation="bogus", group_by="1h")

    def test_aggregation_requires_group_by(self):
        with pytest.raises(ToolParamError, match="requires a group_by"):
            build_query(make_schema(), field="gen", start="-1h", end="now", aggregation="mean")

    @pytest.mark.parametrize("bad", ["1", "1x", "h", "-1h", "1 h", "'; DROP"])
    def test_invalid_group_by_rejected(self, bad):
        with pytest.raises(ToolParamError, match="invalid group_by"):
            build_query(make_schema(), field="gen", start="-1h", end="now", aggregation="mean", group_by=bad)

    def test_start_after_end_rejected(self):
        with pytest.raises(ToolParamError, match="must be before"):
            build_query(make_schema(), field="gen", start="now", end="-1h")

    def test_limit_clamped_to_max(self):
        q = build_query(make_schema(), field="gen", start="-1h", end="now", limit=999999)
        assert f"LIMIT {MAX_RESULT_POINTS}" in q

    def test_limit_floor_of_one(self):
        q = build_query(make_schema(), field="gen", start="-1h", end="now", limit=0)
        assert "LIMIT 1" in q

    def test_invalid_limit_rejected(self):
        with pytest.raises(ToolParamError, match="invalid limit"):
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


class TestResolveDb:
    def test_v1_uses_db_only_ignoring_stale_bucket(self):
        # v1 (no token): db only, even if a stale bucket remains from a v2->v1
        # switch - reads must hit the same db the collectors write to.
        db = resolve_db({"db": "hue_db", "bucket": "hue_bucket"}, {"user": "u", "password": "p"})
        assert db == "hue_db"

    def test_v2_prefers_bucket_then_db(self):
        assert resolve_db({"db": "hue_db", "bucket": "hue_bucket"}, {"token": "t", "org": "o"}) == "hue_bucket"
        assert resolve_db({"db": "hue_db"}, {"token": "t", "org": "o"}) == "hue_db"


class TestBuildSchema:
    def test_combines_class_metadata_with_discovered_fields(self):
        handler = MagicMock()
        handler.source = "openmeteo"
        handler.MCP_MEASUREMENT = "weather"
        handler.MCP_INSTANCE_TAG = None
        handler.MCP_TAG_FILTERS = {}
        handler.MCP_FIELD_METADATA = {"temperature_2m": {"unit": "°C"}}
        schema = build_schema(handler, {"temperature_2m", "precipitation"}, "weather_db")
        assert schema.measurement == "weather"
        assert schema.db == "weather_db"
        assert schema.allowed_fields == {"temperature_2m", "precipitation"}

    def test_measurement_falls_back_to_source_name(self):
        handler = MagicMock()
        handler.source = "hue"
        handler.MCP_MEASUREMENT = None
        handler.MCP_INSTANCE_TAG = None
        handler.MCP_TAG_FILTERS = {}
        handler.MCP_FIELD_METADATA = {}
        schema = build_schema(handler, set(), "hue_db")
        assert schema.measurement == "hue"
        assert schema.db == "hue_db"


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

    def test_integer_valued_float_decodes(self):
        # The collector writes every numeric field as a float, so a lock state
        # arrives as 1.0 - it must still decode.
        schema = make_schema(field_metadata={"stateValue": {"codes": {1: "locked"}}})
        result = annotate_rows(schema, "Lock_stateValue", ["time", "Lock_stateValue"], [[10, 1.0]])
        assert result["points"][0]["label"] == "locked"

    def test_non_integer_float_is_not_truncated_to_a_code(self):
        # 1.5 must not become code 1 ("locked"); it gets a null label.
        schema = make_schema(field_metadata={"stateValue": {"codes": {1: "locked"}}})
        result = annotate_rows(schema, "Lock_stateValue", ["time", "Lock_stateValue"], [[10, 1.5]])
        assert result["points"][0]["label"] is None
        assert result["points"][0]["value"] == 1.5

    def test_bool_value_is_not_decoded_as_a_code(self):
        schema = make_schema(field_metadata={"stateValue": {"codes": {1: "locked"}}})
        result = annotate_rows(schema, "Lock_stateValue", ["time", "Lock_stateValue"], [[10, True]])
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

    def test_non_json_body_surfaces_as_unparseable(self):
        # requests' JSONDecodeError is a ValueError AND a RequestException; the parse
        # handler must be caught before the transport handler, or a bad body would be
        # misreported as a read/transport failure.
        import requests

        session = MagicMock()
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.side_effect = requests.exceptions.JSONDecodeError("Expecting value", "", 0)
        session.get.return_value = response
        with pytest.raises(SourceConnectionError, match="unparseable response"):
            discover_fields(session, {"url": "http://x", "user": "u", "password": "p"}, "db", "hue")

    def test_result_error_surfaces_not_empty_set(self):
        # An InfluxDB error in a 200 payload (wrong db/auth) must raise, not come
        # back as an empty field set that later reads as every field "unknown".
        payload = {"results": [{"error": "database not found: hue_db"}]}
        with pytest.raises(SourceConnectionError, match="rejected the field discovery"):
            discover_fields(_mock_session(payload), {"url": "http://x", "token": "t", "org": "o"}, "db", "hue")


class TestRunQuery:
    def test_returns_columns_and_values(self):
        payload = {"results": [{"series": [{"columns": ["time", "gen"], "values": [[1, 100]]}]}]}
        series = run_query(_mock_session(payload), {"url": "http://x", "user": "u", "password": "p"}, "db", "SELECT 1")
        assert len(series) == 1
        assert series[0].columns == ["time", "gen"]
        assert series[0].values == [[1, 100]]

    def test_query_error_raises(self):
        payload = {"results": [{"error": "boom"}]}
        with pytest.raises(SourceConnectionError, match="rejected the query"):
            run_query(_mock_session(payload), {"url": "http://x", "token": "t", "org": "o"}, "db", "SELECT 1")

    def test_no_series_returns_empty(self):
        assert (
            run_query(_mock_session({"results": [{}]}), {"url": "http://x", "user": "u", "password": "p"}, "db", "q")
            == []
        )

    def test_every_series_is_returned_with_its_tags(self):
        # A GROUP BY on a tag returns one series per tag value. Returning only the
        # first silently discards every producer but one - the exact failure that
        # makes a two-host speedtest install unanswerable. Payload captured from a
        # real InfluxDB 1.8 and confirmed byte-identical on 2.7's v1-compat endpoint.
        payload = {
            "results": [
                {
                    "statement_id": 0,
                    "series": [
                        {
                            "name": "speedtest",
                            "tags": {"host": "hostA"},
                            "columns": ["time", "ping"],
                            "values": [[1700000000, 12.3], [1700000600, 13.1]],
                        },
                        {
                            "name": "speedtest",
                            "tags": {"host": "hostB"},
                            "columns": ["time", "ping"],
                            "values": [[1700000000, 45.6], [1700000600, 46]],
                        },
                    ],
                }
            ]
        }
        series = run_query(_mock_session(payload), {"url": "http://x", "user": "u", "password": "p"}, "db", "SELECT 1")
        assert [s.tags for s in series] == [{"host": "hostA"}, {"host": "hostB"}]
        assert [s.values[0][1] for s in series] == [12.3, 45.6]

    def test_untagged_series_has_empty_tags(self):
        payload = {"results": [{"series": [{"columns": ["time", "gen"], "values": [[1, 100]]}]}]}
        series = run_query(_mock_session(payload), {"url": "http://x", "user": "u", "password": "p"}, "db", "SELECT 1")
        assert series[0].tags == {}

    def test_single_series_helper_returns_columns_and_values(self):
        payload = {"results": [{"series": [{"columns": ["time", "gen"], "values": [[1, 100]]}]}]}
        cols, vals = single_series(
            run_query(_mock_session(payload), {"url": "http://x", "user": "u", "password": "p"}, "db", "q")
        )
        assert (cols, vals) == (["time", "gen"], [[1, 100]])

    def test_single_series_helper_on_empty_result(self):
        assert single_series([]) == ([], [])

    def test_single_series_refuses_to_truncate(self):
        # Truncating here would re-introduce, behind a helper, exactly the silent
        # series loss this module was fixed for: a later edit adding a tag GROUP BY
        # without updating its consumer would go back to losing data invisibly.
        # Unreachable from any current caller (verified against a real InfluxDB 1.8:
        # every one of their queries returns a single series, including the
        # aggregation path's GROUP BY time(), which splits rows not series), so this
        # only ever fires on a programming error.
        grouped = [
            QuerySeries({"host": "hostA"}, ["time", "ping"], [[1, 12.3]]),
            QuerySeries({"host": "hostB"}, ["time", "ping"], [[1, 45.6]]),
        ]
        with pytest.raises(ValueError, match="expected at most one"):
            single_series(grouped)


class TestResolveSchema:
    def test_unknown_source_rejected(self):
        with pytest.raises(ToolParamError, match="unknown source"):
            resolve_schema("nosuch", {"sources": ["hue"]}, None)

    @pytest.mark.parametrize("bad", [None, "", "   ", 5, ["hue"]])
    def test_non_string_or_empty_source_is_query_param_error(self, bad):
        # A clean tool error, not an AttributeError from .lower() on a non-string.
        with pytest.raises(ToolParamError, match="non-empty string"):
            resolve_schema(bad, {"sources": ["hue"]}, None)

    def test_builds_schema_from_handler_and_discovery(self):
        handler = MagicMock()
        handler.source = "zappi"
        handler.MCP_MEASUREMENT = "myenergi"
        handler.MCP_INSTANCE_TAG = None
        handler.MCP_TAG_FILTERS = {"device": "zappi"}
        # Tag filters now come from a method, so the mock must return the real value -
        # a MagicMock method call otherwise yields another mock, and any assertion on the
        # schema's filters would compare against that instead.
        handler.mcp_tag_filters.return_value = {"device": "zappi"}
        handler.MCP_FIELD_METADATA = {"gen": {"unit": "W"}}
        handler.source_settings = {"db": "zappi_db"}
        handler.session = MagicMock()
        settings = {"sources": ["zappi"], "influx": {"url": "http://x", "user": "u", "password": "p"}}
        with (
            patch("toinflux.mcp_common.get_class", return_value=handler),
            patch("toinflux.mcp_read.discover_fields", return_value={"gen", "grd"}),
        ):
            _handler, schema = resolve_schema("zappi", settings, None)
        assert schema.measurement == "myenergi"
        assert schema.db == "zappi_db"
        assert schema.allowed_fields == {"gen", "grd"}
        assert schema.tag_filters == {"device": "zappi"}

    def test_discovery_uses_handlers_own_influx_block(self):
        # A live settings edit changes the handler's influx block; discovery must
        # use that, not the server's startup snapshot.
        handler = MagicMock()
        handler.source = "zappi"
        handler.MCP_MEASUREMENT = "myenergi"
        handler.MCP_INSTANCE_TAG = None
        handler.MCP_TAG_FILTERS = {}
        handler.MCP_FIELD_METADATA = {}
        handler.source_settings = {"db": "zappi_db"}
        handler.session = MagicMock()
        handler.settings = {"influx": {"url": "http://FRESH", "user": "u", "password": "p"}}
        stale = {"sources": ["zappi"], "influx": {"url": "http://STALE", "user": "u", "password": "p"}}
        with (
            patch("toinflux.mcp_common.get_class", return_value=handler),
            patch("toinflux.mcp_read.discover_fields", return_value=set()) as discover,
        ):
            resolve_schema("zappi", stale, None)
        assert discover.call_args.args[1]["url"] == "http://FRESH"


def _tool_text(result):
    """Pull the single text block out of a ``call_tool`` result.

    mcp 2.x returns a ``CallToolResult`` model; 1.x returned a bare sequence of
    content blocks (or a (blocks, structured) tuple). One helper so the shape is
    asserted in exactly one place.

    :param result: whatever ``MCPServer.call_tool`` returned
    :return: the text of the first content block
    :rtype: str
    """
    return result.content[0].text


class TestRegisterReadTools:
    """Register the tools on a real MCPServer and drive them with mocked InfluxDB."""

    def _server(self):
        from mcp.server.mcpserver import MCPServer

        return MCPServer(name="test")

    def _settings(self):
        return {
            "sources": ["zappi"],
            "influx": {"url": "http://x", "user": "u", "password": "p"},
            # A serial, because zappi is instanced: one worker per configured
            # device, so a block with no device expands to nothing.
            "zappi": {"db": "zappi_db", "serial": "12345"},
        }

    def _handler(self):
        handler = MagicMock()
        handler.source = "zappi"
        handler.MCP_MEASUREMENT = "myenergi"
        handler.MCP_INSTANCE_TAG = None
        handler.MCP_TAG_FILTERS = {"device": "zappi"}
        # Tag filters now come from a method, so the mock must return the real value -
        # a MagicMock method call otherwise yields another mock, and any assertion on the
        # schema's filters would compare against that instead.
        handler.mcp_tag_filters.return_value = {"device": "zappi"}
        handler.MCP_FIELD_METADATA = {"gen": {"unit": "W"}}
        handler.source_settings = {"db": "zappi_db"}
        handler.settings = {"influx": {"url": "http://x", "user": "u", "password": "p"}}
        handler.session = MagicMock()
        return handler

    def test_read_tools_registered(self):
        server = self._server()
        register_read_tools(server, self._settings(), None)
        names = {t.name for t in anyio.run(server.list_tools)}
        assert names == {
            "list_sources",
            "list_fields",
            "query_history",
            "get_current_state",
            "get_data_range",
            "get_documentation",
        }

    def test_every_read_tool_has_a_title_and_the_read_only_hint(self):
        # Structured annotations are checked mechanically, not by reading the
        # description - a client's auto-permission logic and a registry review
        # both read title/annotations directly. Every read tool is genuinely
        # read-only, so this is the one hint all six must carry.
        server = self._server()
        register_read_tools(server, self._settings(), None)
        tools = anyio.run(server.list_tools)
        assert tools
        for tool in tools:
            assert tool.title, f"{tool.name} has no title"
            assert tool.title != tool.name, f"{tool.name}'s title must be distinct from its name"
            assert tool.annotations is not None, f"{tool.name} has no annotations"
            assert tool.annotations.read_only_hint is True, f"{tool.name} should be marked read_only_hint=True"

    def test_list_sources(self):
        server = self._server()
        register_read_tools(server, self._settings(), None)
        with patch("toinflux.mcp_common.get_class", return_value=self._handler()):
            result = anyio.run(server.call_tool, "list_sources", {})
        text = _tool_text(result)
        assert "myenergi" in text and "zappi" in text

    def test_query_history_end_to_end(self):
        server = self._server()
        register_read_tools(server, self._settings(), None)
        payload = {"results": [{"series": [{"columns": ["time", "gen"], "values": [[100, 42]]}]}]}
        with (
            patch("toinflux.mcp_common.get_class", return_value=self._handler()),
            patch("toinflux.mcp_read.discover_fields", return_value={"gen"}),
            patch("toinflux.mcp_read.run_query", return_value=[QuerySeries({}, ["time", "gen"], [[100, 42]])]),
        ):
            result = anyio.run(
                server.call_tool,
                "query_history",
                {"source": "zappi", "field": "gen", "start": "-1h", "end": "now"},
            )
        _ = payload
        text = _tool_text(result)
        assert '"value": 42' in text
        assert '"unit": "W"' in text
        # The effective limit and truncation flag are surfaced (1 point < 500).
        assert '"limit": 500' in text
        assert '"truncated": false' in text

    def test_query_history_reports_truncation_at_limit(self):
        server = self._server()
        register_read_tools(server, self._settings(), None)
        rows = [[i, i] for i in range(3)]
        with (
            patch("toinflux.mcp_common.get_class", return_value=self._handler()),
            patch("toinflux.mcp_read.discover_fields", return_value={"gen"}),
            patch("toinflux.mcp_read.run_query", return_value=[QuerySeries({}, ["time", "gen"], rows)]),
        ):
            result = anyio.run(
                server.call_tool,
                "query_history",
                {"source": "zappi", "field": "gen", "start": "-1h", "end": "now", "limit": 3},
            )
        text = _tool_text(result)
        # 3 points returned at limit 3 -> truncated true (more may exist).
        assert '"limit": 3' in text
        assert '"truncated": true' in text

    def test_query_history_closes_the_session(self):
        server = self._server()
        register_read_tools(server, self._settings(), None)
        handler = self._handler()
        with (
            patch("toinflux.mcp_common.get_class", return_value=handler),
            patch("toinflux.mcp_read.discover_fields", return_value={"gen"}),
            patch("toinflux.mcp_read.run_query", return_value=[QuerySeries({}, ["time", "gen"], [[1, 2]])]),
        ):
            anyio.run(
                server.call_tool,
                "query_history",
                {"source": "zappi", "field": "gen", "start": "-1h", "end": "now"},
            )
        handler.session.close.assert_called_once()

    def test_query_history_closes_the_session_on_error(self):
        server = self._server()
        register_read_tools(server, self._settings(), None)
        handler = self._handler()
        with (
            patch("toinflux.mcp_common.get_class", return_value=handler),
            patch("toinflux.mcp_read.discover_fields", side_effect=SourceConnectionError("boom")),
        ):
            with pytest.raises(Exception, match="boom"):
                anyio.run(
                    server.call_tool,
                    "query_history",
                    {"source": "zappi", "field": "gen", "start": "-1h", "end": "now"},
                )
        # discover_fields failed inside resolve_schema -> its except path closed it.
        handler.session.close.assert_called_once()

    def test_list_sources_closes_each_session(self):
        server = self._server()
        register_read_tools(server, self._settings(), None)
        handler = self._handler()
        with patch("toinflux.mcp_common.get_class", return_value=handler):
            anyio.run(server.call_tool, "list_sources", {})
        handler.session.close.assert_called_once()

    def test_query_history_bad_field_is_tool_error(self):
        server = self._server()
        register_read_tools(server, self._settings(), None)
        with (
            patch("toinflux.mcp_common.get_class", return_value=self._handler()),
            patch("toinflux.mcp_read.discover_fields", return_value={"gen"}),
        ):
            with pytest.raises(Exception) as excinfo:
                anyio.run(
                    server.call_tool,
                    "query_history",
                    {"source": "zappi", "field": "evil", "start": "-1h", "end": "now"},
                )
        assert "unknown field" in str(excinfo.value)

    def test_get_current_state_tool(self):
        server = self._server()
        register_read_tools(server, self._settings(), None)
        handler = self._handler()
        handler.MCP_LIVE_STATE = True
        handler.MCP_DESCRIPTION = "Zappi desc"
        handler.get_data.return_value = {"sta": 3}
        handler.MCP_FIELD_METADATA = {"sta": {"codes": {3: "charging"}}}
        with patch("toinflux.mcp_common.get_class", return_value=handler):
            result = anyio.run(server.call_tool, "get_current_state", {"source": "zappi"})
        text = _tool_text(result)
        assert "charging" in text and "live" in text

    def test_get_documentation_tool(self):
        server = self._server()
        register_read_tools(server, self._settings(), None)
        handler = self._handler()
        handler.MCP_DESCRIPTION = "Zappi desc"
        handler.MCP_FIELD_METADATA = {"gen": {"unit": "W"}}
        with patch("toinflux.mcp_common.get_class", return_value=handler):
            result = anyio.run(server.call_tool, "get_documentation", {})
        text = _tool_text(result)
        assert "data reference" in text and "zappi" in text


class TestMetadataFor:
    def test_exact_match_wins(self):
        assert metadata_for({"gen": {"unit": "W"}}, "gen") == {"unit": "W"}

    def test_longest_suffix_match(self):
        meta = {"value": {"unit": "x"}, "stateValue": {"codes": {1: "locked"}}}
        assert metadata_for(meta, "Front_Door_stateValue") == {"codes": {1: "locked"}}

    def test_no_match_is_empty(self):
        assert metadata_for({"gen": {"unit": "W"}}, "unrelated") == {}


class TestAnnotateStateField:
    def test_unit_only(self):
        assert _annotate_state_field({"gen": {"unit": "W"}}, "gen", 1234) == {"value": 1234, "unit": "W"}

    def test_coded_value_decoded(self):
        meta = {"stateValue": {"codes": {1: "locked"}}}
        assert _annotate_state_field(meta, "Front_Door_stateValue", 1) == {"value": 1, "label": "locked"}

    def test_undocumented_code_gets_null_label(self):
        meta = {"stateValue": {"codes": {1: "locked"}}}
        assert _annotate_state_field(meta, "Front_Door_stateValue", 99) == {"value": 99, "label": None}

    def test_no_metadata_is_value_only(self):
        assert _annotate_state_field({}, "whatever", 5) == {"value": 5}


class TestBuildLatestQuery:
    def test_selects_named_fields_with_tag_filter(self):
        q = build_latest_query("myenergi", {"device": "zappi"}, {"grd", "gen"})
        assert q == 'SELECT "gen", "grd" FROM "myenergi" WHERE "device" = \'zappi\' ORDER BY time DESC LIMIT 1'

    def test_no_tags(self):
        q = build_latest_query("speedtest", {}, {"ping"})
        assert q == 'SELECT "ping" FROM "speedtest" ORDER BY time DESC LIMIT 1'

    def test_rejects_control_char_field(self):
        with pytest.raises(ToolParamError, match="invalid field"):
            build_latest_query("speedtest", {}, {"ping\n"})


class TestCurrentStateResult:
    """current_state_result: the live path reads get_data(); the non-live path
    reads InfluxDB and must never call get_data()."""

    def _live_handler(self):
        handler = MagicMock()
        handler.source = "zappi"
        handler.MCP_LIVE_STATE = True
        handler.MCP_DESCRIPTION = "Zappi desc"
        handler.MCP_FIELD_METADATA = {"gen": {"unit": "W"}, "sta": {"codes": {3: "charging"}}}
        handler.get_data.return_value = {"gen": 1234, "sta": 3}
        handler.session = MagicMock()
        return handler

    def test_live_annotates_and_reports_state(self):
        """MyEnergi is instanced - one worker per configured device - so the
        payload is keyed by device label rather than flat, even for the single legacy device.
        Same rule as Hue's per-bridge map: the shape must not depend on how many devices
        happen to be configured."""
        handler = self._live_handler()
        settings = {"sources": ["zappi"], "zappi": {"db": "z", "interval": 300, "serial": "12345"}}
        with patch("toinflux.mcp_common.get_class", return_value=handler):
            result = current_state_result("zappi", settings, None)
        assert result["source"] == "zappi"
        assert result["state"] == "live"
        assert result["description"] == "Zappi desc"
        fields = result["instances"]["zappi"]["fields"]
        assert fields["gen"] == {"value": 1234, "unit": "W"}
        assert fields["sta"] == {"value": 3, "label": "charging"}
        handler.session.close.assert_called_once()

    def test_non_live_reads_latest_from_influx_without_get_data(self):
        handler = MagicMock()
        handler.source = "speedtest"
        handler.MCP_LIVE_STATE = False
        handler.MCP_MEASUREMENT = None
        handler.MCP_INSTANCE_TAG = None
        handler.MCP_TAG_FILTERS = {}
        handler.MCP_DESCRIPTION = "speed"
        handler.MCP_FIELD_METADATA = {"ping": {"unit": "ms"}}
        handler.source_settings = {"db": "sdb"}
        handler.settings = {"influx": {"url": "http://x", "user": "u", "password": "p"}}
        handler.session = MagicMock()
        with (
            patch("toinflux.mcp_common.get_class", return_value=handler),
            patch("toinflux.mcp_read.discover_fields", return_value={"ping", "download"}),
            patch(
                "toinflux.mcp_read.run_query",
                return_value=[QuerySeries({}, ["time", "download", "ping"], [[1700, None, 12.5]])],
            ),
        ):
            result = current_state_result("speedtest", {"sources": ["speedtest"]}, None)
        # Critical: a non-live source's get_data() (a full speed test) is never run.
        handler.get_data.assert_not_called()
        assert result["state"] == "last_recorded"
        assert result["as_of"] == 1700
        assert result["fields"]["ping"] == {"value": 12.5, "unit": "ms"}
        # A field that came back NULL in the latest point is omitted, not reported None.
        assert "download" not in result["fields"]
        handler.session.close.assert_called_once()


class TestInstancedReadsWithARealHandler:
    """The per-producer read paths, driven by the real Speedtest class.

    These exist because the rest of this file builds handlers with MagicMock, and a mock is
    exactly what hid these two paths: the non-live current-state test sets
    ``MCP_INSTANCE_TAG = None``, so it exercises Speedtest as it behaved *before* this axis
    existed, and nothing reached ``list_fields_result`` at all. Both paths were correct when
    probed by hand - the gap was in the tests, not the code - but an untested path is one a
    later change can break silently, and between them they answer "does get_current_state
    report both hosts separately" and "does list_fields report the values an instance may
    take".

    Using the real class also means ``MCP_INSTANCE_TAG``, ``MCP_LIVE_STATE`` and the field
    metadata come from the source rather than from whatever the test asserts they are.
    """

    SETTINGS = {
        "influx": {"url": "http://influx.example.com:8086", "user": "u", "password": "p"},
        "speedtest": {"db": "sdb", "interval": 3600},
        "sources": ["speedtest"],
    }

    def _handler(self):
        from toinflux.speedtest import Speedtest

        with patch("toinflux.influx.load_settings", return_value=self.SETTINGS):
            handler = Speedtest("speedtest")
        handler.session = MagicMock()
        return handler

    def test_current_state_reports_each_host_separately(self):
        """Speedtest is not live (its get_data runs a full test), so current state comes from
        the latest recorded point per host - which is the whole point of the story: one
        merged answer was wrong rather than incomplete."""
        handler = self._handler()
        series = [
            QuerySeries({"host": "alpha"}, ["time", "download", "ping"], [[1700, 5.0, 12.5]]),
            QuerySeries({"host": "beta"}, ["time", "download", "ping"], [[1701, 6.0, 40.0]]),
        ]
        with (
            patch("toinflux.mcp_common.get_class", return_value=handler),
            patch("toinflux.mcp_read.discover_fields", return_value={"ping", "download"}),
            patch("toinflux.mcp_read.discover_tag_values", return_value={"alpha", "beta"}),
            patch("toinflux.mcp_read.run_query", return_value=series),
        ):
            result = current_state_result("speedtest", self.SETTINGS, None)

        assert result["state"] == "last_recorded"
        assert result["instance_tag"] == "host"
        assert set(result["instances"]) == {"alpha", "beta"}
        assert result["instances"]["alpha"]["fields"]["ping"] == {"value": 12.5, "unit": "ms"}
        assert result["instances"]["beta"]["fields"]["ping"] == {"value": 40.0, "unit": "ms"}
        # Each host's reading carries its own timestamp, not one shared "now".
        assert result["instances"]["alpha"]["as_of"] == 1700
        assert result["instances"]["beta"]["as_of"] == 1701
        # Never both shapes: a caller must not have to guess which to read.
        assert "fields" not in result
        handler.session.close.assert_called_once()

    def test_current_state_never_runs_a_speed_test(self):
        """The guard that matters most on this path: get_data() would saturate the link."""
        handler = self._handler()
        with (
            patch("toinflux.mcp_common.get_class", return_value=handler),
            patch.object(type(handler), "get_data", side_effect=AssertionError("get_data was called")),
            patch("toinflux.mcp_read.discover_fields", return_value={"ping"}),
            patch("toinflux.mcp_read.discover_tag_values", return_value={"alpha"}),
            patch(
                "toinflux.mcp_read.run_query",
                return_value=[QuerySeries({"host": "alpha"}, ["time", "ping"], [[1700, 12.5]])],
            ),
        ):
            result = current_state_result("speedtest", self.SETTINGS, None)
        assert set(result["instances"]) == {"alpha"}

    def test_an_untagged_series_is_skipped_rather_than_keyed_as_none(self):
        """A grouped query should not return an untagged series, but if one arrives it must
        not become a producer called ``None`` in the payload."""
        handler = self._handler()
        series = [
            QuerySeries({}, ["time", "ping"], [[1699, 1.0]]),
            QuerySeries({"host": "alpha"}, ["time", "ping"], [[1700, 12.5]]),
        ]
        with (
            patch("toinflux.mcp_common.get_class", return_value=handler),
            patch("toinflux.mcp_read.discover_fields", return_value={"ping"}),
            patch("toinflux.mcp_read.discover_tag_values", return_value={"alpha"}),
            patch("toinflux.mcp_read.run_query", return_value=series),
        ):
            result = current_state_result("speedtest", self.SETTINGS, None)
        assert set(result["instances"]) == {"alpha"}

    def test_list_fields_reports_the_axis_and_the_values_it_accepts(self):
        """list_fields is where a caller learns which values `instance` may take - the tool
        description points them here, and nothing asserted it did so."""
        handler = self._handler()
        with (
            patch("toinflux.mcp_common.get_class", return_value=handler),
            patch("toinflux.mcp_read.discover_fields", return_value={"ping", "download"}),
            patch("toinflux.mcp_read.discover_tag_values", return_value={"beta", "alpha"}),
        ):
            result = list_fields_result("speedtest", self.SETTINGS, None)

        assert result["source"] == "speedtest"
        assert result["measurement"] == "speedtest"
        assert result["instance_tag"] == "host"
        assert result["instances"] == ["alpha", "beta"], "instances must be sorted for a stable payload"
        # Field metadata still comes through, from the source class rather than the test.
        by_name = {entry["field"]: entry for entry in result["fields"]}
        assert by_name["ping"]["unit"] == "ms"
        assert [entry["field"] for entry in result["fields"]] == ["download", "ping"]
        handler.session.close.assert_called_once()

    def test_list_fields_omits_the_axis_for_a_source_without_one(self):
        """A single-producer source keeps the historical shape, so nothing reading the payload
        has to special-case its absence."""
        from toinflux.carbonintensity import CarbonIntensity

        settings = {
            "influx": {"url": "http://influx.example.com:8086", "user": "u", "password": "p"},
            "carbonintensity": {"db": "cdb", "interval": 1800},
            "sources": ["carbonintensity"],
        }
        with patch("toinflux.influx.load_settings", return_value=settings):
            handler = CarbonIntensity("carbonintensity")
        handler.session = MagicMock()
        with (
            patch("toinflux.mcp_common.get_class", return_value=handler),
            patch("toinflux.mcp_read.discover_fields", return_value={"intensity"}),
        ):
            result = list_fields_result("carbonintensity", settings, None)
        assert "instance_tag" not in result
        assert "instances" not in result


class TestBuildDocumentation:
    def test_includes_descriptions_units_and_codes(self):
        handler = MagicMock()
        handler.source = "zappi"
        handler.MCP_DESCRIPTION = "Zappi desc"
        handler.MCP_FIELD_METADATA = {"gen": {"unit": "W"}, "sta": {"codes": {1: "paused", 3: "charging"}}}
        handler.session = MagicMock()
        with patch("toinflux.mcp_common.get_class", return_value=handler):
            doc = build_documentation({"sources": ["zappi"]}, None)
        assert "# send-to-influx data reference" in doc
        assert "## zappi" in doc
        assert "Zappi desc" in doc
        assert "`gen` - unit W" in doc
        assert "1=paused, 3=charging" in doc
        handler.session.close.assert_called_once()


class TestSourceMcpMetadata:
    def test_speedtest_and_octopus_are_not_live(self):
        from toinflux.octopus import Octopus
        from toinflux.speedtest import Speedtest

        assert Speedtest.MCP_LIVE_STATE is False
        assert Octopus.MCP_LIVE_STATE is False

    def test_device_sources_are_live_by_default(self):
        from toinflux.nuki import Nuki
        from toinflux.philipshue import Hue

        assert Hue.MCP_LIVE_STATE is True
        assert Nuki.MCP_LIVE_STATE is True

    def test_every_source_has_a_description(self):
        from toinflux.carbonintensity import CarbonIntensity
        from toinflux.myenergi import Eddi, Harvi, Zappi
        from toinflux.nuki import Nuki
        from toinflux.octopus import Octopus
        from toinflux.openmeteo import OpenMeteo
        from toinflux.philipshue import Hue
        from toinflux.speedtest import Speedtest

        for cls in (Hue, Nuki, Octopus, OpenMeteo, CarbonIntensity, Speedtest, Zappi, Eddi, Harvi):
            assert cls.MCP_DESCRIPTION, f"{cls.__name__} has no MCP_DESCRIPTION"


class TestMultiBridgeReads:
    """The read surface must cover every bridge and say which is which."""

    @staticmethod
    def _handler(instance, fields=None, fail=None):
        handler = MagicMock(MCP_LIVE_STATE=True, MCP_DESCRIPTION="Philips Hue", MCP_FIELD_METADATA={})
        handler.source = "hue"
        handler.instance = instance
        handler.worker_label = f"hue@{instance}" if instance else "hue"
        handler.mcp_tag_filters.return_value = {"host": instance} if instance else {}
        if fail:
            handler.get_data.side_effect = SourceConnectionError(fail)
        else:
            handler.get_data.return_value = fields or {}
        handler.session = MagicMock()
        return handler

    def _settings(self):
        return {"sources": ["hue"], "influx": {"url": "http://x", "user": "u", "password": "p"}, "hue": {}}

    def test_each_bridge_is_reported_separately(self):
        """Two bridges can carry the same field name - a "Kitchen" per floor - so a single
        flat map would silently lose one of them."""
        handlers = [
            ("down.example.com", self._handler("down.example.com", {"Kitchen": 40})),
            ("up.example.com", self._handler("up.example.com", {"Kitchen": 80})),
        ]
        with patch("toinflux.mcp_read.resolve_handlers", return_value=handlers):
            result = current_state_result("hue", self._settings(), None)
        assert set(result["instances"]) == {"down.example.com", "up.example.com"}
        assert result["instances"]["down.example.com"]["fields"]["Kitchen"]["value"] == 40
        assert result["instances"]["up.example.com"]["fields"]["Kitchen"]["value"] == 80
        assert "fields" not in result  # never a flat map for an instanced source

    def test_a_single_bridge_is_still_grouped(self):
        """Keyed whenever the source is instanced, even with one bridge, so nothing reading
        the payload depends on how many are configured."""
        handlers = [("only.example.com", self._handler("only.example.com", {"Lamp": 1}))]
        with patch("toinflux.mcp_read.resolve_handlers", return_value=handlers):
            result = current_state_result("hue", self._settings(), None)
        assert list(result["instances"]) == ["only.example.com"]

    def test_a_single_target_source_keeps_the_flat_shape(self):
        """Every non-instanced source is untouched - instance None means no grouping.

        Exercised through a genuinely single-target source, not through Hue with a None
        instance. That combination cannot occur in production - for Hue, resolve_handlers
        always yields host instances or raises - so asserting the flat shape that way would
        prove it using a scenario nothing produces, and would not notice a regression in the
        real single-target path.

        Uses openmeteo: Nuki was the example here precisely because it was
        single-target, and it now has a device axis covering every lock.
        """
        handler = MagicMock(
            MCP_LIVE_STATE=True,
            MCP_INSTANCE_TAG=None,
            MCP_LIVE_STATE_COVERS_ALL_INSTANCES=False,
            MCP_DESCRIPTION="Weather",
            MCP_FIELD_METADATA={},
        )
        handler.source = "openmeteo"
        handler.instance = None
        handler.worker_label = "openmeteo"
        handler.get_data.return_value = {"temperature_2m": 18.5}
        handler.session = MagicMock()
        settings = {
            "sources": ["openmeteo"],
            "influx": {"url": "http://x", "user": "u", "password": "p"},
            "openmeteo": {"db": "weather_db"},
        }
        with patch("toinflux.mcp_read.resolve_handlers", return_value=[(None, handler)]):
            result = current_state_result("openmeteo", settings, None)
        assert result["source"] == "openmeteo"
        assert result["fields"]["temperature_2m"]["value"] == 18.5
        assert "as_of" in result
        assert "instances" not in result

    def test_one_failing_bridge_does_not_suppress_the_others(self):
        """A partial answer WITH its failure status, rather than all-or-nothing."""
        handlers = [
            ("down.example.com", self._handler("down.example.com", {"Kitchen": 40})),
            ("up.example.com", self._handler("up.example.com", fail="bridge unreachable")),
        ]
        with patch("toinflux.mcp_read.resolve_handlers", return_value=handlers):
            result = current_state_result("hue", self._settings(), None)
        assert result["instances"]["down.example.com"]["fields"]["Kitchen"]["value"] == 40
        assert result["instances"]["up.example.com"]["error"] == "bridge unreachable"

    def test_every_bridge_failing_raises(self):
        """Nothing useful to return, so this is a transport failure the caller should retry
        - not a success payload full of errors."""
        handlers = [
            ("down.example.com", self._handler("down.example.com", fail="down")),
            ("up.example.com", self._handler("up.example.com", fail="also down")),
        ]
        with patch("toinflux.mcp_read.resolve_handlers", return_value=handlers):
            with pytest.raises(SourceConnectionError) as excinfo:
                current_state_result("hue", self._settings(), None)
        message = str(excinfo.value)
        # Each bridge paired with its own error, not merely both hostnames present somewhere.
        assert "'down.example.com': down" in message and "'up.example.com': also down" in message

    def test_hue_declares_the_host_axis_so_scoping_uses_the_shared_path(self):
        """The core of per-bridge scoping: without the axis on the class, Hue falls back to the old
        merged behaviour and `instance` is refused as not applying. Driven through
        resolve_schema and the real Hue class rather than asserting the attribute, so it
        fails if the plumbing stops reading it as well as if the value changes."""
        settings = {
            "sources": ["hue"],
            "influx": {"url": "http://x", "user": "u", "password": "p"},
            "hue": {"db": "h", "interval": 300, "host": "a.example.com", "user": "tok"},
        }
        with patch("toinflux.influx.load_settings", return_value=settings):
            handler = Hue("hue")
        handler.session = MagicMock()
        with (
            patch("toinflux.mcp_common.get_class", return_value=handler),
            patch("toinflux.mcp_read.discover_fields", return_value={"Kitchen"}),
            patch("toinflux.mcp_read.discover_tag_values", return_value={"a.example.com"}),
        ):
            _, schema = resolve_schema("hue", settings, None)
        assert schema.instance_tag == "host"
        query = build_query(schema, field="Kitchen", start="-1h", end="now", instance="a.example.com")
        assert "\"host\" = 'a.example.com'" in query

    def test_allowlist_unions_configured_targets_with_recorded_ones(self):
        """Acceptance question 1 turns on this. Discovered values alone would refuse a
        bridge that is configured but has not collected yet - which `bridge` accepted - and
        would leave query_history disagreeing with get_current_state, which reads live from
        whatever is configured. A decommissioned bridge still has history worth querying."""
        settings = {
            "sources": ["hue"],
            "influx": {"url": "http://x", "user": "u", "password": "p"},
            "hue": {
                "db": "h",
                "interval": 300,
                "host": "configured-and-recording.example.com",
                "user": "tok",
                "host2": "configured-no-data-yet.example.com",
                "user2": "tok2",
            },
        }
        with patch("toinflux.influx.load_settings", return_value=settings):
            handler = Hue("hue")
        handler.session = MagicMock()
        with (
            patch("toinflux.mcp_common.get_class", return_value=handler),
            patch("toinflux.mcp_read.discover_fields", return_value={"Kitchen"}),
            patch(
                "toinflux.mcp_read.discover_tag_values",
                return_value={"configured-and-recording.example.com", "decommissioned.example.com"},
            ),
        ):
            _, schema = resolve_schema("hue", settings, None)
        assert schema.instance_values == {
            "configured-and-recording.example.com",
            "configured-no-data-yet.example.com",
            "decommissioned.example.com",
        }
        # Both edge cases must be accepted, not merely present in the set.
        assert _validate_instance(schema, "configured-no-data-yet.example.com") is None
        assert _validate_instance(schema, "decommissioned.example.com") is None

    def test_configured_instances_is_empty_for_a_single_target_source(self):
        """A source with no separate targets expands to one None instance, which is not a
        value - leaking it into the allowlist would make None an acceptable argument."""
        assert configured_instances("speedtest", {"sources": ["speedtest"]}) == []

    def test_bridge_is_rejected_for_a_source_with_one_producer(self):
        """Silently ignoring it would be worse than refusing: the source would run an
        unscoped query and the result would echo the value back, telling the caller the
        answer was narrowed when it was not.

        Still refused after `bridge` became an alias for `instance` - just through the one
        shared guard rather than a Hue-specific branch, which is the point of the change.
        Uses octopus because speedtest now *has* an axis and is no longer an example of a
        single-producer source.
        """
        plain = ReadSchema(source="octopus", measurement="octopus", db="o")
        with pytest.raises(ToolParamError, match="single producer"):
            _validate_instance(plain, "made-up")

    @pytest.mark.parametrize("bad_source", [5, None, ["hue"], "", "   "])
    def test_a_bad_source_is_reported_as_a_bad_source_not_an_instance_problem(self, bad_source):
        """Scoping must not be the thing that judges an unusable source name.

        Originally a review finding against the `bridge` guard: it called ``source.lower()``
        before anything had checked the type, so a non-string raised AttributeError -
        escaping the ToolParamError/SourceConnectionError split the MCP layer relies on to
        tell a caller mistake from a transport failure. A blank string was worse in a
        quieter way: it *is* a string, so it reached the guard and came back blaming the
        scoping parameter for what was wrong with ``source``.

        Kept after `bridge` was removed, because the concern outlives the parameter name -
        ``instance`` is now the argument that must not be blamed for a bad source.
        """
        from toinflux.mcp_read import _query_history_result

        with pytest.raises(ToolParamError) as excinfo:
            _query_history_result(
                {"sources": ["hue"]},
                None,
                source=bad_source,
                field="x",
                start=None,
                end=None,
                aggregation=None,
                group_by=None,
                limit=None,
                instance="anything",
            )
        assert "source must be a non-empty string" in str(excinfo.value)
        assert "does not apply" not in str(excinfo.value)

    def test_history_scoped_to_one_bridge_filters_by_its_host_tag(self):
        """Hue writes every bridge to one measurement, so scoping a query means filtering on
        the host tag its own writes carry."""
        schema = make_schema(
            source="hue",
            measurement="hue",
            tag_filters={"host": "up.example.com"},
            allowed_fields={"Kitchen"},
            field_metadata={},
        )
        query = build_query(schema, field="Kitchen", start="-1h", end="now")
        assert "\"host\" = 'up.example.com'" in query

    def test_unscoped_history_spans_every_bridge(self):
        """Deliberate: an unqualified question about the estate gets an answer about the
        estate. Documented in the tool description rather than left implicit."""
        schema = make_schema(
            source="hue", measurement="hue", tag_filters={}, allowed_fields={"Kitchen"}, field_metadata={}
        )
        query = build_query(schema, field="Kitchen", start="-1h", end="now")
        assert "host" not in query


class TestInstanceAxis:
    """The instance axis: scoping a query to one producer, and never merging producers.

    Every query shape asserted here was executed against a real InfluxDB 1.8 while this
    was written, so the SQL is known to parse and to return the series counts assumed -
    including the composed `GROUP BY time(1h), "host"`, which is the one most likely to
    be wrong by inspection.
    """

    @staticmethod
    def _schema(values=("hostA", "hostB"), tag="host"):
        return ReadSchema(
            source="speedtest",
            measurement="speedtest",
            db="sdb",
            allowed_fields={"ping"},
            field_metadata={"ping": {"unit": "ms"}},
            instance_tag=tag,
            instance_values=set(values),
        )

    def test_scoped_query_filters_on_the_instance_tag(self):
        q = build_query(self._schema(), field="ping", start="-1h", end="now", instance="hostA")
        assert "\"host\" = 'hostA'" in q
        # Scoped means one series already, so grouping would be noise.
        assert "GROUP BY" not in q

    def test_unscoped_query_groups_by_the_instance_tag(self):
        q = build_query(self._schema(), field="ping", start="-1h", end="now")
        assert 'GROUP BY "host"' in q
        assert "'hostA'" not in q

    def test_unscoped_aggregation_groups_by_time_and_tag(self):
        q = build_query(self._schema(), field="ping", start="-1h", end="now", aggregation="mean", group_by="1h")
        assert 'GROUP BY time(1h), "host" fill(none)' in q

    def test_limit_is_divided_across_instances_when_grouping(self):
        # InfluxDB applies LIMIT per series once grouped, so an undivided limit would
        # let N producers multiply the result cap - the bound would stop bounding.
        q = build_query(self._schema(values=("a", "b", "c", "d")), field="ping", start="-1h", end="now", limit=100)
        assert q.endswith("LIMIT 25")

    def test_scoped_limit_is_not_divided(self):
        q = build_query(self._schema(), field="ping", start="-1h", end="now", limit=100, instance="hostA")
        assert q.endswith("LIMIT 100")

    def test_source_without_an_axis_is_unchanged(self):
        plain = ReadSchema(source="octopus", measurement="octopus", db="o", allowed_fields={"cost"})
        q = build_query(plain, field="cost", start="-1h", end="now", limit=100)
        assert "GROUP BY" not in q
        assert q.endswith("LIMIT 100")

    def test_unknown_instance_value_is_refused_with_the_accepted_ones(self):
        with pytest.raises(ToolParamError, match="accepted values: hostA, hostB"):
            _validate_instance(self._schema(), "typo")

    def test_refusal_does_not_call_the_allowlist_recorded(self):
        """The allowlist is the union of present-in-data and configured, so a configured
        target that has not collected yet appears in it. Calling the list "recorded" would
        state something untrue about the very value being offered as an alternative."""
        with pytest.raises(ToolParamError) as excinfo:
            _validate_instance(self._schema(), "typo")
        assert "recorded values" not in str(excinfo.value)

    def test_instance_refused_for_a_single_producer_source(self):
        plain = ReadSchema(source="octopus", measurement="octopus", db="o")
        with pytest.raises(ToolParamError, match="single producer"):
            _validate_instance(plain, "anything")

    def test_no_instance_is_always_allowed(self):
        assert _validate_instance(self._schema(), None) is None

    def test_build_query_refuses_an_instance_on_a_source_with_no_axis(self):
        """build_query is public and reachable without _validate_instance. Unguarded it
        reached _quote_identifier(None) and raised a bare AttributeError - neither
        ToolParamError nor SourceConnectionError, so the MCP layer could not tell a caller
        mistake from a transport failure."""
        plain = ReadSchema(source="octopus", measurement="octopus", db="o", allowed_fields={"cost"})
        with pytest.raises(ToolParamError, match="single producer"):
            build_query(plain, field="cost", start="-1h", end="now", instance="whatever")


class TestPerInstanceHistoryShape:
    """Acceptance question 2: an unscoped result must say which producer each point
    came from, and must not merge them."""

    @staticmethod
    def _handler():
        handler = MagicMock()
        handler.settings = {"influx": {"url": "http://x", "user": "u", "password": "p"}}
        handler.session = MagicMock()
        return handler

    def _run(self, series, instance=None, limit=DEFAULT_RESULT_POINTS):
        from toinflux.mcp_read import _run_query_history

        schema = TestInstanceAxis._schema()
        with patch("toinflux.mcp_read.run_query", return_value=series):
            return _run_query_history(
                self._handler(), schema, "ping", "-1h", "now", "raw", None, limit, instance=instance
            )

    def test_unscoped_reports_each_producer_separately(self):
        result = self._run(
            [
                QuerySeries({"host": "hostA"}, ["time", "ping"], [[2, 13.1], [1, 12.3]]),
                QuerySeries({"host": "hostB"}, ["time", "ping"], [[2, 46.0], [1, 45.6]]),
            ]
        )
        assert set(result["instances"]) == {"hostA", "hostB"}
        assert [p["value"] for p in result["instances"]["hostA"]["points"]] == [13.1, 12.3]
        assert [p["value"] for p in result["instances"]["hostB"]["points"]] == [46.0, 45.6]
        assert result["instance_tag"] == "host"
        # Field-level metadata stays at the top rather than repeating per producer.
        assert result["unit"] == "ms"
        assert "points" not in result

    def test_keyed_even_with_one_producer(self):
        # So nothing reading the payload depends on how many producers exist - the same
        # reasoning as Hue's per-bridge map.
        # The tag value has to be one the schema knows about, since a producer outside the
        # allowlist is deliberately not reported - see the shared-measurement filter.
        result = self._run([QuerySeries({"host": "hostA"}, ["time", "ping"], [[1, 5.0]])])
        assert set(result["instances"]) == {"hostA"}

    def test_scoped_returns_the_flat_shape(self):
        result = self._run([QuerySeries({}, ["time", "ping"], [[1, 12.3]])], instance="hostA")
        assert [p["value"] for p in result["points"]] == [12.3]
        assert "instances" not in result

    def test_limit_reported_is_the_one_actually_applied_per_instance(self):
        # Reporting the caller's figure would make `truncated` a comparison against a
        # limit InfluxDB never used.
        result = self._run(
            [
                QuerySeries({"host": "hostA"}, ["time", "ping"], [[1, 1.0]]),
                QuerySeries({"host": "hostB"}, ["time", "ping"], [[1, 2.0]]),
            ],
            limit=10,
        )
        assert result["limit_per_instance"] == 5
        assert "limit" not in result

    def test_truncation_is_per_instance_and_summarised(self):
        result = self._run(
            [
                QuerySeries({"host": "hostA"}, ["time", "ping"], [[1, 1.0], [2, 2.0]]),
                QuerySeries({"host": "hostB"}, ["time", "ping"], [[1, 3.0]]),
            ],
            limit=4,
        )
        assert result["instances"]["hostA"]["truncated"] is True
        assert result["instances"]["hostB"]["truncated"] is False
        assert result["truncated"] is True

    def test_untagged_series_is_reported_not_silently_dropped(self, caplog):
        with caplog.at_level("WARNING"):
            result = self._run(
                [
                    QuerySeries({"host": "hostA"}, ["time", "ping"], [[1, 1.0]]),
                    QuerySeries({}, ["time", "ping"], [[1, 9.0]]),
                ]
            )
        assert set(result["instances"]) == {"hostA"}
        assert "no host tag" in caplog.text


class TestSharedMeasurementInstances:
    """The three MyEnergi types share the `myenergi`
    measurement and are told apart by the same `device` tag that now carries the operator's
    label - so a discovered value cannot be attributed to a type, and the config is the
    authority. The config does distinguish them: separate blocks, separate sources."""

    SETTINGS = {
        "sources": ["zappi", "eddi"],
        "influx": {"url": "http://x", "user": "u", "password": "p"},
        "zappi": {
            "db": "m",
            "interval": 300,
            "serial": "1",
            "devices": [{"serial": "2", "label": "Driveway"}],
        },
        "eddi": {"db": "m", "interval": 300, "serial": "3", "label": "Hot Water"},
        "myenergi": {"apikey": "k", "zappi_url": "u", "eddi_url": "u", "dayhour_url": "u"},
    }

    def _schema(self, source="zappi"):
        with patch("toinflux.influx.load_settings", return_value=self.SETTINGS):
            handler = get_class(source, None)
        handler.session = MagicMock()
        with (
            patch("toinflux.mcp_common.get_class", return_value=handler),
            patch("toinflux.mcp_read.discover_fields", return_value={"frq"}),
            # Every device in the measurement, whichever type wrote it - which is exactly
            # what SHOW TAG VALUES returns and why it cannot be trusted here.
            patch(
                "toinflux.mcp_read.discover_tag_values",
                return_value={"zappi", "Driveway", "Hot Water", "gone-device"},
            ) as discover,
        ):
            _, schema = resolve_schema(source, self.SETTINGS, None)
        return schema, discover

    def test_the_allowlist_is_the_configured_devices_of_that_source_only(self):
        """Acceptance question 6: a query for zappi covers the named device and the
        legacy-labelled one, and nothing belonging to another type."""
        schema, _ = self._schema("zappi")
        assert schema.instance_values == {"zappi", "Driveway"}
        assert "Hot Water" not in schema.instance_values

    def test_discovery_is_not_consulted_for_a_shared_measurement(self):
        """Not merely filtered afterwards - the round trip is skipped, since its answer
        could not be attributed to a type anyway."""
        _, discover = self._schema("zappi")
        discover.assert_not_called()

    def test_an_eddi_label_is_refused_for_a_zappi_query(self):
        schema, _ = self._schema("zappi")
        with pytest.raises(ToolParamError, match="accepted values: Driveway, zappi"):
            _validate_instance(schema, "Hot Water")

    def test_a_grouped_query_reports_only_this_sources_devices(self):
        """The measurement holds every type's devices, so an unfiltered grouped query would
        answer a zappi question with the eddi's readings."""
        from toinflux.mcp_read import _run_query_history

        schema, _ = self._schema("zappi")
        handler = MagicMock()
        handler.settings = {"influx": {"url": "http://x", "user": "u", "password": "p"}}
        series = [
            QuerySeries({"device": "zappi"}, ["time", "frq"], [[1, 50.0]]),
            QuerySeries({"device": "Driveway"}, ["time", "frq"], [[1, 49.0]]),
            QuerySeries({"device": "Hot Water"}, ["time", "frq"], [[1, 48.0]]),
            QuerySeries({"device": "gone-device"}, ["time", "frq"], [[1, 47.0]]),
        ]
        with patch("toinflux.mcp_read.run_query", return_value=series):
            result = _run_query_history(handler, schema, "frq", "-1h", "now", "raw", None, 100)
        assert set(result["instances"]) == {"zappi", "Driveway"}

    def test_an_empty_allowlist_reports_nothing_not_everything(self):
        """Review finding, reproduced first. The filter was guarded on the allowlist being
        non-empty, so a source that owns nothing in the measurement skipped filtering
        entirely and reported *every* producer - answering a zappi question with the eddi and
        harvi devices. Empty means nothing is ours, so nothing is the honest answer."""
        from toinflux.mcp_read import _run_query_history

        schema = ReadSchema(
            source="zappi",
            measurement="myenergi",
            db="m",
            allowed_fields={"frq"},
            instance_tag="device",
            instance_values=set(),
        )
        handler = MagicMock()
        handler.settings = {"influx": {"url": "http://x", "user": "u", "password": "p"}}
        series = [
            QuerySeries({"device": "Hot Water"}, ["time", "frq"], [[1, 48.0]]),
            QuerySeries({"device": "harvi"}, ["time", "frq"], [[1, 47.0]]),
        ]
        with patch("toinflux.mcp_read.run_query", return_value=series):
            result = _run_query_history(handler, schema, "frq", "-1h", "now", "raw", None, 100)
        assert result["instances"] == {}

    def test_a_source_owning_its_measurement_still_uses_discovery(self):
        """The rule must not have quietly changed Speedtest or Hue: they own their
        measurements, so a discovered value is unambiguous and the union still applies."""
        settings = {
            "sources": ["speedtest"],
            "influx": {"url": "http://x", "user": "u", "password": "p"},
            "speedtest": {"db": "s", "interval": 600},
        }
        with patch("toinflux.influx.load_settings", return_value=settings):
            handler = get_class("speedtest", None)
        handler.session = MagicMock()
        with (
            patch("toinflux.mcp_common.get_class", return_value=handler),
            patch("toinflux.mcp_read.discover_fields", return_value={"ping"}),
            patch("toinflux.mcp_read.discover_tag_values", return_value={"pi4", "nas"}) as discover,
        ):
            _, schema = resolve_schema("speedtest", settings, None)
        discover.assert_called_once()
        assert schema.instance_values == {"pi4", "nas"}


class TestDiscoverTagValues:
    def test_parses_values(self):
        payload = {
            "results": [{"series": [{"columns": ["key", "value"], "values": [["host", "hostA"], ["host", "hostB"]]}]}]
        }
        values = discover_tag_values(
            _mock_session(payload), {"url": "http://x", "user": "u", "password": "p"}, "db", "speedtest", "host"
        )
        assert values == {"hostA", "hostB"}

    def test_empty_when_nothing_recorded(self):
        values = discover_tag_values(
            _mock_session({"results": [{}]}), {"url": "http://x", "user": "u", "password": "p"}, "db", "m", "host"
        )
        assert values == set()

    def test_series_without_a_value_column_is_skipped_not_misread(self, caplog):
        """A -1 fallback read each row's last cell, which is right for today's
        ["key", "value"] shape and would silently invent tag values if that changed. A
        wrong allowlist refuses real producers and accepts ones that do not exist."""
        payload = {"results": [{"series": [{"columns": ["key"], "values": [["host"]]}]}]}
        with caplog.at_level("WARNING"):
            values = discover_tag_values(
                _mock_session(payload), {"url": "http://x", "user": "u", "password": "p"}, "db", "m", "host"
            )
        assert values == set()
        assert "no 'value' column" in caplog.text

    def test_result_error_surfaces_rather_than_looking_like_no_instances(self):
        payload = {"results": [{"error": "database not found: sdb"}]}
        with pytest.raises(SourceConnectionError, match="rejected the tag-value discovery"):
            discover_tag_values(
                _mock_session(payload), {"url": "http://x", "token": "t", "org": "o"}, "db", "m", "host"
            )


class TestDataRangeResult:
    """get_data_range: how far back data goes, and how long InfluxDB keeps it.

    The two halves are read differently and fail differently, which is what these tests are
    about. The range comes from the shared query path on both InfluxDB versions; retention
    comes from `SHOW RETENTION POLICIES` on v1 and the management API on v2, and a retention
    failure must degrade rather than take the whole call down.
    """

    V1 = {
        "sources": ["speedtest"],
        "influx": {"url": "http://influx.example.com:8086", "user": "u", "password": "p"},
        "speedtest": {"db": "speedtest_db"},
    }
    V2 = {
        "sources": ["speedtest"],
        "influx": {"url": "http://influx.example.com:8086", "token": "tok", "org": "si-org"},
        "speedtest": {"bucket": "speedtest_bucket"},
    }

    @staticmethod
    def _handler(settings):
        from toinflux.speedtest import Speedtest

        with patch("toinflux.influx.load_settings", return_value=settings):
            handler = Speedtest("speedtest")
        handler.session = MagicMock()
        return handler

    def _run(self, settings, responses):
        """Drive data_range_result with a canned response per GET, in order."""
        from toinflux.mcp_read import data_range_result

        handler = self._handler(settings)
        calls = []

        def fake_get(url, **kwargs):
            calls.append((url, kwargs.get("params", {})))
            resp = MagicMock()
            resp.raise_for_status.return_value = None
            body = responses[min(len(calls) - 1, len(responses) - 1)]
            if isinstance(body, Exception):
                raise body
            resp.json.return_value = body
            return resp

        handler.session.get.side_effect = fake_get
        with patch("toinflux.mcp_read.resolve_handler", return_value=handler):
            return data_range_result("speedtest", settings, None), calls

    @staticmethod
    def _fields(*names):
        return {"results": [{"series": [{"columns": ["fieldKey"], "values": [[n] for n in names]}]}]}

    @staticmethod
    def _point(ts):
        return {"results": [{"series": [{"columns": ["time", "ping"], "values": [[ts, 12.0]]}]}]}

    @staticmethod
    def _grouped_point(**per_host):
        """Speedtest has an instance axis, so the range is also read per producer -
        two extra grouped round trips (oldest, newest) before the overall pair."""
        return {
            "results": [
                {
                    "series": [
                        {"tags": {"host": h}, "columns": ["time", "ping"], "values": [[ts, 12.0]]}
                        for h, ts in per_host.items()
                    ]
                }
            ]
        }

    @staticmethod
    def _tag_values(*values):
        """Speedtest declares an instance tag, so resolve_schema enumerates it - one
        extra round trip between field discovery and the edge-time queries."""
        return {"results": [{"series": [{"columns": ["key", "value"], "values": [["host", v] for v in values]}]}]}

    def test_v1_reports_range_and_retention(self):
        """Acceptance question 2: v1 reports the configured duration and shard duration.

        The values are the ones a real InfluxDB 1.8 returned for a database created with
        30-day retention and a 1h shard group, so the parsing is checked against reality
        rather than an invented shape.
        """
        retention = {
            "results": [
                {
                    "series": [
                        {
                            "columns": ["name", "duration", "shardGroupDuration", "replicaN", "default"],
                            "values": [["autogen", "720h0m0s", "1h0m0s", 1, True]],
                        }
                    ]
                }
            ]
        }
        result, _ = self._run(
            self.V1,
            [
                self._fields("ping"),
                self._tag_values("hostA"),
                self._grouped_point(hostA=1000),
                self._grouped_point(hostA=5000),
                self._point(1000),
                self._point(5000),
                retention,
            ],
        )
        assert (result["earliest"], result["latest"], result["span_seconds"]) == (1000, 5000, 4000)
        assert result["points_present"] is True
        assert result["retention"]["known"] is True
        assert result["retention"]["policy"] == "autogen"
        assert result["retention"]["duration"] == "720h0m0s"
        assert result["retention"]["duration_seconds"] == 2592000
        assert result["retention"]["shard_group_duration_seconds"] == 3600
        assert result["retention"]["read_from"] == "v1 SHOW RETENTION POLICIES"
        # Per producer as well as overall: a host added last week and one collecting for a
        # year share a merged span that is true of the measurement and false of both.
        assert result["instance_tag"] == "host"
        assert result["instances"]["hostA"] == {"earliest": 1000, "latest": 5000, "span_seconds": 4000}

    def test_v1_prefers_the_default_policy(self):
        """Writes with no explicit policy land in the default one, so that is the policy
        whose duration actually bounds this project's data."""
        retention = {
            "results": [
                {
                    "series": [
                        {
                            "columns": ["name", "duration", "shardGroupDuration", "replicaN", "default"],
                            "values": [
                                ["short", "24h0m0s", "1h0m0s", 1, False],
                                ["keep", "8760h0m0s", "24h0m0s", 1, True],
                            ],
                        }
                    ]
                }
            ]
        }
        result, _ = self._run(
            self.V1,
            [
                self._fields("ping"),
                self._tag_values("hostA"),
                self._grouped_point(hostA=1000),
                self._grouped_point(hostA=5000),
                self._point(1),
                self._point(2),
                retention,
            ],
        )
        assert result["retention"]["policy"] == "keep"

    def test_v2_reads_retention_from_the_management_api_not_the_query_path(self):
        """Acceptance question 3, and the finding that shaped this tool.

        v2's v1-compatibility /query *does* answer SHOW RETENTION POLICIES with the same
        credential - but it reports the DBRP mapping's policy, not the bucket's. Verified
        against InfluxDB 2.7: a bucket with 720h retention and a 24h shard group came back
        as duration=0s, shardGroupDuration=168h0m0s. 0s means "keep forever", so trusting
        it would report unlimited retention for data that expires in 30 days. Hence the
        management API - and hence this test asserting which URL was actually used.
        """
        buckets = {
            "buckets": [
                {
                    "name": "speedtest_bucket",
                    "retentionRules": [{"type": "expire", "everySeconds": 2592000, "shardGroupDurationSeconds": 86400}],
                }
            ]
        }
        result, calls = self._run(
            self.V2,
            [
                self._fields("ping"),
                self._tag_values("hostA"),
                self._grouped_point(hostA=1000),
                self._grouped_point(hostA=5000),
                self._point(10),
                self._point(20),
                buckets,
            ],
        )
        assert result["retention"]["known"] is True
        assert result["retention"]["duration_seconds"] == 2592000
        # Rendered in v1's own style, so an answer is comparable across versions.
        assert result["retention"]["duration"] == "720h0m0s"
        assert result["retention"]["shard_group_duration"] == "24h0m0s"
        assert result["retention"]["read_from"] == "v2 /api/v2/buckets"
        # The retention read went to the management API, not /query.
        assert calls[-1][0].endswith("/api/v2/buckets")
        assert calls[-1][1]["name"] == "speedtest_bucket"
        assert calls[-1][1]["org"] == "si-org"

    def test_v2_bucket_with_no_rules_is_infinite_and_known(self):
        """A bucket that never expires data is a real answer, not a failed lookup - so it
        must stay distinguishable from 'could not find out'."""
        result, _ = self._run(
            self.V2,
            [self._fields("ping"), self._point(1), self._point(2), {"buckets": [{"name": "b", "retentionRules": []}]}],
        )
        assert result["retention"]["known"] is True
        assert result["retention"]["duration"] == "infinite"
        assert result["retention"]["duration_seconds"] == 0

    def test_v2_retention_failure_degrades_and_keeps_the_range(self):
        """Acceptance question 3's degraded half: the call must not fail wholesale.

        Reported rather than omitted, because a missing retention key reads as 'nothing
        expires' - the same misleading direction as v2's 0s.
        """
        result, _ = self._run(
            self.V2,
            [
                self._fields("ping"),
                self._tag_values("hostA"),
                self._grouped_point(hostA=1000),
                self._grouped_point(hostA=5000),
                self._point(100),
                self._point(200),
                requests.exceptions.HTTPError("403 Forbidden"),
            ],
        )
        assert (result["earliest"], result["latest"]) == (100, 200)
        assert result["retention"]["known"] is False
        assert "reason" in result["retention"]
        assert "403" in result["retention"]["reason"]

    def test_v2_missing_bucket_degrades_rather_than_reporting_infinite(self):
        """v2 answers 200 with an empty list for a name matching nothing, so this is not
        caught by raise_for_status - and must not be read as 'no retention rules'."""
        result, _ = self._run(
            self.V2,
            [
                self._fields("ping"),
                self._tag_values("hostA"),
                self._grouped_point(hostA=1000),
                self._grouped_point(hostA=5000),
                self._point(1),
                self._point(2),
                {"buckets": []},
            ],
        )
        assert result["retention"]["known"] is False
        assert "no bucket named" in result["retention"]["reason"]

    def test_no_data_yet_is_reported_not_failed(self):
        """A source configured today has no points; that is an answer, and retention is
        still worth reporting since it is configured independently of collection."""
        retention = {
            "results": [
                {
                    "series": [
                        {
                            "columns": ["name", "duration", "shardGroupDuration", "replicaN", "default"],
                            "values": [["autogen", "0s", "168h0m0s", 1, True]],
                        }
                    ]
                }
            ]
        }
        result, _ = self._run(self.V1, [{"results": [{}]}, retention])
        assert result["points_present"] is False
        assert result["earliest"] is None and result["latest"] is None and result["span_seconds"] is None
        assert result["retention"]["known"] is True
        assert result["retention"]["duration_seconds"] == 0
        # v1 reports keep-forever as the literal "0s"; it must read the same as v2's, or the
        # answer means different things depending on which InfluxDB is behind it. Asserting
        # only duration_seconds here is what let that inconsistency through review once.
        assert result["retention"]["duration"] == "infinite"

    def test_range_failure_is_not_swallowed(self):
        """The range is the tool's primary answer, so unlike retention its failure is a
        transport error the caller sees - not a null quietly reported as 'no data'."""
        from toinflux.mcp_read import data_range_result

        handler = self._handler(self.V1)
        handler.session.get.side_effect = requests.exceptions.ConnectionError("influx down")
        with patch("toinflux.mcp_read.resolve_handler", return_value=handler):
            with pytest.raises(SourceConnectionError):
                data_range_result("speedtest", self.V1, None)

    def test_v1_and_v2_render_the_same_retention_identically(self):
        """The cross-version guarantee, asserted directly rather than inferred.

        A caller must not have to know which InfluxDB version answered to interpret
        `duration`. v1 reports strings, v2 reports seconds; both go through one renderer, so
        the same underlying retention has to come back byte-identical either way.
        """
        v1_retention = {
            "results": [
                {
                    "series": [
                        {
                            "columns": ["name", "duration", "shardGroupDuration", "replicaN", "default"],
                            "values": [["autogen", "720h0m0s", "24h0m0s", 1, True]],
                        }
                    ]
                }
            ]
        }
        v2_buckets = {
            "buckets": [
                {
                    "name": "speedtest_bucket",
                    "retentionRules": [{"everySeconds": 2592000, "shardGroupDurationSeconds": 86400}],
                }
            ]
        }
        v1, _ = self._run(
            self.V1,
            [
                self._fields("ping"),
                self._tag_values("hostA"),
                self._grouped_point(hostA=1000),
                self._grouped_point(hostA=5000),
                self._point(1),
                self._point(2),
                v1_retention,
            ],
        )
        v2, _ = self._run(
            self.V2,
            [
                self._fields("ping"),
                self._tag_values("hostA"),
                self._grouped_point(hostA=1000),
                self._grouped_point(hostA=5000),
                self._point(1),
                self._point(2),
                v2_buckets,
            ],
        )
        for key in ("duration", "duration_seconds", "shard_group_duration", "shard_group_duration_seconds"):
            assert v1["retention"][key] == v2["retention"][key], key

    def test_a_short_result_row_reads_as_no_timestamp_not_a_crash(self):
        """Nothing guarantees a row is as long as its column list.

        A bare positional index would raise IndexError from inside the read rather than the
        "could not read that" the caller is written for. Applies to every row access in the
        module, not just this one, which is why they share one reader.
        """
        from toinflux.mcp_read import _cell, data_range_result

        assert _cell([], {"time": 0}, "time") is None
        assert _cell([1], {"time": 5}, "time") is None
        assert _cell([7], {"time": 0}, "time") == 7

        truncated = {"results": [{"series": [{"columns": ["time", "ping"], "values": [[]]}]}]}
        handler = self._handler(self.V1)
        responses = [self._fields("ping"), truncated, truncated, {"results": [{"series": []}]}]
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            resp = MagicMock()
            resp.raise_for_status.return_value = None
            resp.json.return_value = responses[min(len(calls) - 1, len(responses) - 1)]
            return resp

        handler.session.get.side_effect = fake_get
        with patch("toinflux.mcp_read.resolve_handler", return_value=handler):
            result = data_range_result("speedtest", self.V1, None)
        assert result["earliest"] is None and result["latest"] is None
        assert result["span_seconds"] is None

    @pytest.mark.parametrize(
        "duration,expected",
        [
            ("720h0m0s", 2592000),
            ("0s", 0),
            ("168h0m0s", 604800),
            (" 24h0m0s ", 86400),
            # Everything below must be None, not a confident number. findall alone accepted a
            # *prefix*, so "720h junk" parsed as 2592000 - a malformed value from some future
            # InfluxDB would then have been reported as a real retention rather than unknown.
            ("720h junk", None),
            ("junk720h", None),
            ("720h0m0sEXTRA", None),
            ("12x", None),
            ("720", None),
            ("INF", None),
            ("", None),
            (None, None),
        ],
    )
    def test_only_a_whole_valid_duration_parses(self, duration, expected):
        """A partly-parseable duration is not a duration.

        This feeds `duration_seconds`, which is reported as fact to the caller, so guessing
        from a prefix would be worse than admitting the value is unreadable.
        """
        from toinflux.mcp_read import _influx_duration_seconds

        assert _influx_duration_seconds(duration) == expected

    def test_the_oldest_point_query_orders_ascending(self):
        """The whole mechanism: ORDER BY time ASC is what makes it the *oldest* point."""
        from toinflux.mcp_read import build_edge_time_query, build_latest_query

        earliest = build_edge_time_query("hue", {}, "ASC")
        assert earliest.endswith("ORDER BY time ASC LIMIT 1")
        assert build_edge_time_query("hue", {}, "DESC").endswith("ORDER BY time DESC LIMIT 1")
        assert build_latest_query("hue", {}, {"lamp"}).endswith("ORDER BY time DESC LIMIT 1")
        # Same measurement/tag validation and quoting, since all of them share one builder.
        assert '"hue"' in earliest

    def test_the_edge_time_query_does_not_enumerate_fields(self):
        """It travels in a GET parameter, and only the `time` column is ever read.

        Measured against a real InfluxDB with a 120-field measurement, enumerating fields
        produced a 3.4 KB query string; a measurement grows with device count (Nuki prefixes
        fields per lock), so a wide enough estate would exceed a reverse proxy's request-line
        limit for a read that has no need of the width. `build_latest_query` still enumerates,
        because it reads values and must exclude tag columns.
        """
        from toinflux.mcp_read import build_edge_time_query, build_latest_query

        wide = {f"Front_Door_{i}_stateValue" for i in range(120)}
        edge = build_edge_time_query("nuki", {}, "ASC")
        assert "SELECT * FROM" in edge
        assert len(edge) < 100
        assert "Front_Door_0_stateValue" not in edge
        # The value-reading builder is deliberately unchanged and still enumerates.
        assert "Front_Door_0_stateValue" in build_latest_query("nuki", {}, wide)

    def test_the_edge_time_query_still_applies_tag_filters(self):
        """Selecting * must not lose the static tag scoping - the myenergi trio share one
        measurement and are told apart by a device tag."""
        from toinflux.mcp_read import build_edge_time_query

        query = build_edge_time_query("myenergi", {"device": "zappi"}, "ASC")
        assert "\"device\" = 'zappi'" in query
