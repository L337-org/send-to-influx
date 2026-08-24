"""Tests for the dashboard-panel suggestion tool.

The point of this being a tool rather than a prompt is that its rules can be
asserted. "Never take the mean of a counter" is a hope when it is prose in a
prompt and a guarantee when it is a test here, so the aggregation rules get one
test each rather than being covered incidentally by a payload-shape assertion.

Every Grafana-side fact these tests pin - the target field names, the value-mapping
shape, the ``$tag_`` alias, the unit identifiers - was established by running a real
Grafana 13.2.0 against a real InfluxDB 1.8, not from recollection. What the tests
guard is that our side keeps emitting it.
"""

from unittest.mock import MagicMock, patch

import pytest

from toinflux.exceptions import ToolParamError
from toinflux.mcp_dashboards import (
    GRAFANA_UNITS,
    panel_spec,
    series_tags,
    suggest_panels_result,
    value_mappings,
)
from toinflux.mcp_read import MeasurementKeys, ReadSchema, build_panel_query


def _schema(**kwargs):
    """A ReadSchema with the fields these tests care about defaulted."""
    base = {
        "source": "nuki",
        "measurement": "nuki",
        "db": "home",
        "allowed_fields": {"stateValue", "batteryChargeState"},
        "field_types": {"stateValue": "float", "batteryChargeState": "float"},
        "tag_keys": {"device"},
        "field_metadata": {},
    }
    base.update(kwargs)
    return ReadSchema(**base)


class TestBuildPanelQuery:
    """A panel query is not a history query: the panel owns the time window, so the
    macros go in and the LIMIT stays out."""

    def test_uses_grafana_macros_not_concrete_bounds(self):
        query = build_panel_query(_schema(), "stateValue", "last", ["device"])
        assert query == (
            'SELECT LAST("stateValue") FROM "nuki" WHERE $timeFilter ' 'GROUP BY time($__interval), "device" fill(none)'
        )

    def test_carries_the_sources_disambiguating_tags(self):
        # Without this a zappi panel would chart every MyEnergi device at once, since
        # all three types share the measurement.
        schema = _schema(
            source="zappi",
            measurement="myenergi",
            tag_filters={"device": "zappi"},
            allowed_fields={"che"},
            field_types={"che": "float"},
        )
        query = build_panel_query(schema, "che", "last", [])
        assert "WHERE $timeFilter AND \"device\" = 'zappi'" in query

    def test_applies_no_limit(self):
        # A LIMIT would fight the panel's own maxDataPoints.
        assert "LIMIT" not in build_panel_query(_schema(), "stateValue", "last", [])

    def test_refuses_a_field_outside_the_live_allowlist(self):
        # The same allowlist that guards query_history: a field discovery never
        # returned must not reach a query string.
        with pytest.raises(ToolParamError, match="unknown field"):
            build_panel_query(_schema(), "nope", "last", [])

    def test_refuses_an_unknown_aggregation(self):
        with pytest.raises(ToolParamError, match="unknown aggregation"):
            build_panel_query(_schema(), "stateValue", "average", [])

    def test_quotes_and_escapes_identifiers(self):
        schema = _schema(allowed_fields={'a"b'}, field_types={'a"b': "float"})
        assert 'LAST("a\\"b")' in build_panel_query(schema, 'a"b', "last", [])

    def test_rejects_a_control_character_in_a_field_name(self):
        schema = _schema(allowed_fields={"bad\nname"}, field_types={})
        with pytest.raises(ToolParamError, match="invalid field name"):
            build_panel_query(schema, "bad\nname", "last", [])


class TestAggregationRules:
    """The rules that stop a chart being confidently wrong."""

    def test_a_counter_is_never_averaged_or_summed(self):
        schema = _schema(field_metadata={"che": {"unit": "kWh", "kind": "counter"}}, allowed_fields={"che"})
        spec = panel_spec(schema, "che", [])
        assert spec["aggregation"] == "last"
        assert "mean" in spec["avoid_aggregations"]
        assert "sum" in spec["avoid_aggregations"]
        assert "LAST(" in spec["query"]

    def test_a_gauge_is_averaged_and_never_summed(self):
        # Summing instantaneous readings adds up quantities that never existed. This
        # warning is only sound because interval quantities are their own kind: while
        # they were gauges, keeping `sum` here steered callers off the right aggregation
        # for Octopus consumption, and dropping it made the warning useless for a real
        # temperature. See test_an_interval_quantity_is_a_kind_of_its_own.
        schema = _schema(field_metadata={"tp1": {"unit": "°C", "kind": "gauge"}}, allowed_fields={"tp1"})
        spec = panel_spec(schema, "tp1", [])
        assert spec["aggregation"] == "mean"
        assert spec["avoid_aggregations"] == ["sum"]

    def test_an_interval_quantity_is_summed_and_nothing_is_ruled_out(self):
        # Summing is the whole point: it gives the total for whatever range is charted.
        # Nothing is ruled out, because a mean is legitimately the average interval.
        schema = _schema(
            source="octopus",
            measurement="octopus",
            field_metadata={"consumption_kwh": {"unit": "kWh", "kind": "interval"}},
            allowed_fields={"consumption_kwh"},
            field_types={"consumption_kwh": "float"},
        )
        spec = panel_spec(schema, "consumption_kwh", [])
        assert spec["kind"] == "interval"
        assert spec["aggregation"] == "sum"
        assert "SUM(" in spec["query"]
        assert "avoid_aggregations" not in spec

    def test_an_interval_quantity_is_a_kind_of_its_own(self):
        # The three fields whose own descriptions say they are per-interval. They were
        # declared gauges, which is what made gauge's `sum` warning unsound - one
        # vocabulary cannot rule a sum out for a temperature and endorse it for half an
        # hour of consumption. If one of these moves back, gauge's avoid list has to be
        # revisited rather than left quietly wrong.
        from toinflux.octopus import Octopus
        from toinflux.openmeteo import OpenMeteo

        assert Octopus.MCP_FIELD_METADATA["consumption_kwh"]["kind"] == "interval"
        assert Octopus.MCP_FIELD_METADATA["gas_consumption"]["kind"] == "interval"
        assert OpenMeteo.MCP_FIELD_METADATA["precipitation"]["kind"] == "interval"

    def test_no_declared_gauge_is_really_an_interval_quantity(self):
        # The other half of that guard, mechanised: a field whose description talks about
        # an interval or an accumulation while calling itself a gauge is the exact
        # contradiction this kind was added to remove, and would silently reinstate a
        # wrong `sum` warning.
        from toinflux.general import known_sources, source_class

        suspicious = []
        for source in known_sources():
            for field, meta in source_class(source).MCP_FIELD_METADATA.items():
                if meta.get("kind") != "gauge":
                    continue
                description = (meta.get("description") or "").lower()
                # "average of the preceding hour" is a gauge: an average over an interval
                # is still a reading, and summing averages means nothing.
                if "average" in description:
                    continue
                if any(word in description for word in ("during one", "accumulated", "preceding interval")):
                    suspicious.append(f"{source}.{field}")
        assert not suspicious, f"declared gauge(s) describing themselves as per-interval: {suspicious}"

    def test_a_state_gets_a_state_timeline_and_no_arithmetic(self):
        schema = _schema(
            field_metadata={"stateValue": {"kind": "state", "codes": {1: "locked"}}},
            allowed_fields={"stateValue"},
        )
        spec = panel_spec(schema, "stateValue", [])
        assert spec["panel_type"] == "state-timeline"
        assert spec["aggregation"] == "last"
        for banned in ("mean", "median", "sum", "min", "max"):
            assert banned in spec["avoid_aggregations"]

    def test_an_undeclared_numeric_field_is_not_treated_as_a_gauge(self):
        # The whole point of omitting `kind` rather than defaulting it: suggesting
        # `mean` here would say averaging is safe for a field that might be a counter.
        schema = _schema(field_metadata={}, allowed_fields={"mystery"}, field_types={"mystery": "float"})
        spec = panel_spec(schema, "mystery", [])
        assert "kind" not in spec
        assert spec["aggregation"] == "last"
        assert "avoid_aggregations" not in spec

    def test_a_text_field_is_a_state_without_being_declared(self):
        schema = _schema(field_metadata={}, allowed_fields={"ectt1"}, field_types={"ectt1": "string"})
        spec = panel_spec(schema, "ectt1", [])
        assert spec["kind"] == "state"
        assert spec["panel_type"] == "state-timeline"


class TestGrafanaMapping:
    def test_value_mappings_match_the_shape_grafana_stores(self):
        # Verified against a real Grafana 13.2, which stored this unchanged.
        assert value_mappings({3: "unlocked", 1: "locked"}) == [
            {
                "type": "value",
                "options": {"1": {"text": "locked", "index": 0}, "3": {"text": "unlocked", "index": 1}},
            }
        ]

    def test_no_codes_means_no_mappings(self):
        assert value_mappings(None) == []
        assert value_mappings({}) == []

    def test_a_known_unit_maps_to_grafanas_identifier(self):
        schema = _schema(field_metadata={"che": {"unit": "kWh", "kind": "counter"}}, allowed_fields={"che"})
        assert panel_spec(schema, "che", [])["unit"] == "kwatth"

    def test_an_unmappable_unit_is_omitted_rather_than_passed_through(self):
        # W/m2 has no Grafana identifier. Emitting it raw would put a string Grafana
        # does not understand into the panel, which renders as no unit at all but looks
        # configured; omitting it is the same answer, honestly labelled.
        schema = _schema(
            field_metadata={"direct_radiation": {"unit": "W/m²", "kind": "gauge"}},
            allowed_fields={"direct_radiation"},
        )
        assert "unit" not in panel_spec(schema, "direct_radiation", [])

    def test_every_mapped_identifier_is_one_grafana_actually_defines(self):
        # Read out of a running Grafana 13.2's bundled frontend. Grafana accepts any
        # string server-side, so a typo here would only show up as an unformatted axis.
        verified = {
            "watt",
            "watth",
            "kwatth",
            "celsius",
            "percent",
            "bps",
            "ms",
            "hertz",
            "lengthmm",
            "velocitykmh",
            "humidity",
            "lux",
            "volt",
            "amp",
            "joule",
        }
        assert set(GRAFANA_UNITS.values()) <= verified


class TestSeriesTags:
    def test_a_pinned_tag_is_not_grouped_by(self):
        # Grouping by a tag the WHERE clause has narrowed to one value adds a series
        # dimension with a single member.
        schema = _schema(measurement="myenergi", tag_keys={"device"}, tag_filters={"device": "zappi"})
        assert series_tags(schema) == []

    def test_a_free_tag_is_grouped_by_and_aliased(self):
        schema = _schema(
            tag_keys={"device"},
            field_metadata={"stateValue": {"kind": "state"}},
            allowed_fields={"stateValue"},
        )
        tags = series_tags(schema)
        assert tags == ["device"]
        spec = panel_spec(schema, "stateValue", tags)
        # Without an alias the series is named after the query ("nuki.last"), which is
        # what a legend then shows - verified on a real Grafana.
        assert spec["alias"] == "$tag_device"

    def test_a_single_series_panel_is_aliased_to_the_field_name(self):
        # Never left unaliased: on a real Grafana a panel with no alias is named after
        # its query, so a zappi energy panel showed up as "myenergi.last" in the legend.
        schema = _schema(tag_keys=set(), allowed_fields={"stateValue"})
        assert panel_spec(schema, "stateValue", [])["alias"] == "stateValue"


class TestSuggestPanelsResult:
    SETTINGS = {"sources": ["nuki"], "influx": {"url": "http://x", "user": "u", "password": "p"}}

    def _handler(self):
        handler = MagicMock()
        handler.source = "nuki"
        handler.MCP_MEASUREMENT = None
        handler.MCP_INSTANCE_TAG = "device"
        handler.mcp_tag_filters.return_value = {}
        handler.MCP_FIELD_METADATA = {
            "stateValue": {"kind": "state", "codes": {1: "locked"}},
            "batteryChargeState": {"unit": "%", "kind": "gauge"},
        }
        handler.source_settings = {"db": "home"}
        handler.settings = {"influx": {"url": "http://x", "user": "u", "password": "p"}}
        handler.session = MagicMock()
        return handler

    def _run(self, fields=None):
        keys = MeasurementKeys(
            field_types={"stateValue": "float", "batteryChargeState": "float"},
            tag_keys=frozenset({"device"}),
        )
        with (
            patch("toinflux.mcp_common.get_class", return_value=self._handler()),
            patch("toinflux.mcp_read.discover_measurement_keys", return_value=keys),
            patch("toinflux.mcp_read.discover_tag_values", return_value={"Front_Door"}),
        ):
            return suggest_panels_result("nuki", self.SETTINGS, None, fields)

    def test_describes_every_field_by_default(self):
        result = self._run()
        assert result["database"] == "home"
        assert result["measurement"] == "nuki"
        assert result["datasource_type"] == "influxdb"
        assert result["series_tags"] == ["device"]
        assert [p["field"] for p in result["panels"]] == ["batteryChargeState", "stateValue"]

    def test_a_field_subset_is_honoured(self):
        result = self._run(["stateValue"])
        assert [p["field"] for p in result["panels"]] == ["stateValue"]

    def test_an_unrecorded_field_is_refused_naming_what_is_available(self):
        with pytest.raises(ToolParamError) as excinfo:
            self._run(["nope"])
        assert "nope" in str(excinfo.value)
        assert "batteryChargeState" in str(excinfo.value)

    def test_every_unknown_field_is_named_at_once_not_just_the_first(self):
        # This is the whole reason the check is here rather than left to
        # build_panel_query, which refuses the same fields one at a time: a caller that
        # mistyped two names should not have to retry twice to find that out.
        with pytest.raises(ToolParamError) as excinfo:
            self._run(["nope", "stateValue", "alsoNope"])
        message = str(excinfo.value)
        assert "nope" in message and "alsoNope" in message

    def test_the_session_is_closed_even_when_a_field_is_refused(self):
        handler = self._handler()
        keys = MeasurementKeys(field_types={"stateValue": "float"}, tag_keys=frozenset())
        with (
            patch("toinflux.mcp_common.get_class", return_value=handler),
            patch("toinflux.mcp_read.discover_measurement_keys", return_value=keys),
            patch("toinflux.mcp_read.discover_tag_values", return_value=set()),
        ):
            with pytest.raises(ToolParamError):
                suggest_panels_result("nuki", self.SETTINGS, None, ["nope"])
        handler.session.close.assert_called_once()


class TestRegisterDashboardTools:
    def _server(self):
        from mcp.server.mcpserver import MCPServer

        return MCPServer(name="test")

    def test_the_tool_registers_with_a_title_and_the_read_only_hint(self):
        # Checked mechanically, not by reading the description: a client's
        # auto-permission logic and a registry review both read these fields directly.
        import anyio

        from toinflux.mcp_dashboards import register_dashboard_tools

        server = self._server()
        register_dashboard_tools(server, TestSuggestPanelsResult.SETTINGS, None)
        tools = {t.name: t for t in anyio.run(server.list_tools)}
        assert set(tools) == {"suggest_dashboard_panels"}
        tool = tools["suggest_dashboard_panels"]
        assert tool.title and tool.title != tool.name
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True

    def test_end_to_end_through_the_tool_boundary(self):
        # The wrapper threads `fields` into run_sync; a mis-ordered argument there
        # would arrive as the settings path and be silently ignored.
        import anyio

        from toinflux.mcp_dashboards import register_dashboard_tools

        server = self._server()
        register_dashboard_tools(server, TestSuggestPanelsResult.SETTINGS, None)
        keys = MeasurementKeys(
            field_types={"stateValue": "float", "batteryChargeState": "float"},
            tag_keys=frozenset({"device"}),
        )
        with (
            patch("toinflux.mcp_common.get_class", return_value=TestSuggestPanelsResult()._handler()),
            patch("toinflux.mcp_read.discover_measurement_keys", return_value=keys),
            patch("toinflux.mcp_read.discover_tag_values", return_value={"Front_Door"}),
        ):
            result = anyio.run(
                server.call_tool, "suggest_dashboard_panels", {"source": "nuki", "fields": ["stateValue"]}
            )
        text = result.content[0].text
        assert '"field": "stateValue"' in text
        assert "batteryChargeState" not in text
        assert "state-timeline" in text
