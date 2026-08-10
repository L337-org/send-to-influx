"""Unit tests for the MCP device-write path: the per-collector write tools (Hue
light control, Speedtest trigger), the Hue capability handling, and the opt-in,
least-privilege write-tool registration."""

from unittest.mock import MagicMock, patch

import anyio
import pytest
import requests

from toinflux.exceptions import SourceConnectionError, ToolParamError
from toinflux.mcp_write import (
    register_write_tools,
    writable_enabled_sources,
    _hue_list_devices_result,
    _hue_set_light_result,
    _speedtest_run_result,
)


def make_hue(mcp_read_write=True, insecure=True):
    from toinflux.philipshue import Hue

    settings = {
        "hue": {
            "host": "hue.local",
            "user": "abc",
            "db": "hue_db",
            "interval": 300,
            "insecure": insecure,
            "timeout": 5,
            "mcp_read_write": mcp_read_write,
        }
    }
    with patch("toinflux.influx.load_settings", return_value=settings):
        handler = Hue("hue")
    handler.session = MagicMock()
    return handler


def make_speedtest(mcp_read_write=True):
    from toinflux.speedtest import Speedtest

    settings = {"speedtest": {"db": "speedtest_db", "mcp_read_write": mcp_read_write}}
    with patch("toinflux.influx.load_settings", return_value=settings):
        handler = Speedtest("speedtest")
    handler.session = MagicMock()
    return handler


def _bridge_lights():
    # One light per Hue capability tier: dimmable white, colour-temperature, full
    # colour, and an on/off plug - so capability-awareness can be exercised.
    return {
        "1": {"name": "Kitchen", "state": {"on": False, "bri": 10}},
        "2": {"name": "Lamp", "state": {"on": True, "bri": 200}},
        "3": {
            "name": "Hall",
            "state": {"on": True, "bri": 100, "ct": 300},
            "capabilities": {"control": {"ct": {"min": 153, "max": 454}}},
        },
        "4": {
            "name": "Lounge",
            "state": {"on": True, "bri": 100, "ct": 300, "xy": [0.3, 0.3], "hue": 0, "sat": 0},
            "capabilities": {
                "control": {"ct": {"min": 153, "max": 500}, "colorgamut": [[0.7, 0.3], [0.2, 0.7], [0.15, 0.05]]}
            },
        },
        "5": {"name": "Plug", "state": {"on": False}},
    }


def _wire_bridge(handler, put_result=None, lights=None):
    """Point the handler's mocked session at a fake bridge GET (device list) and
    PUT (state change), recording the PUT url/body on the returned closure."""
    put_result = put_result if put_result is not None else [{"success": {"/lights/1/state/on": True}}]
    lights = _bridge_lights() if lights is None else lights

    def fake_get(url, **kwargs):
        resp = MagicMock()
        resp.json.return_value = {"lights": lights}
        resp.raise_for_status.return_value = None
        return resp

    def fake_put(url, **kwargs):
        fake_put.url = url
        fake_put.body = kwargs.get("json")
        fake_put.verify = kwargs.get("verify")
        resp = MagicMock()
        resp.json.return_value = put_result
        resp.raise_for_status.return_value = None
        return resp

    handler.session.get.side_effect = fake_get
    handler.session.put.side_effect = fake_put
    return fake_put


def test_param_error_is_not_a_retryable_connection_error():
    # The taxonomy the write path relies on: a parameter mistake must not be a
    # SourceConnectionError (which the collector worker loop retries with backoff);
    # retrying a permanently-invalid input would loop forever.
    assert not issubclass(ToolParamError, SourceConnectionError)
    assert not issubclass(SourceConnectionError, ToolParamError)


class TestHueListDevices:
    def test_lists_devices_with_capabilities(self):
        handler = make_hue()
        _wire_bridge(handler)
        by_id = {d["id"]: d for d in handler.mcp_list_writable_devices()}
        assert by_id["1"]["name"] == "Kitchen"
        assert by_id["1"]["controls"] == ["on_off", "brightness"]
        assert by_id["3"]["controls"] == ["on_off", "brightness", "color_temp"]
        # 454 mirek -> 2203 K (warm end), 153 mirek -> 6536 K (cool end)
        assert by_id["3"]["color_temp_range_k"] == [2203, 6536]
        assert by_id["4"]["controls"] == ["on_off", "brightness", "color_temp", "color"]
        assert by_id["5"]["controls"] == ["on_off"]
        assert "color_temp_range_k" not in by_id["5"]

    def test_missing_or_blank_names_fall_back_to_id_as_strings(self):
        handler = make_hue()
        _wire_bridge(handler, lights={"1": {}, "2": {"name": ""}, "3": {"name": "Lamp"}})
        names = {d["id"]: d["name"] for d in handler.mcp_list_writable_devices()}
        assert names == {"1": "1", "2": "2", "3": "Lamp"}
        assert all(isinstance(n, str) for n in names.values())

    def test_unparseable_response_surfaces(self):
        handler = make_hue()

        def fake_get(url, **kwargs):
            resp = MagicMock()
            resp.raise_for_status.return_value = None
            # requests raises its own JSONDecodeError (a ValueError *and* a
            # RequestException) on a non-JSON body - use it, not a plain ValueError,
            # so this guards the except-clause ordering (parse before transport).
            resp.json.side_effect = requests.exceptions.JSONDecodeError("Expecting value", "", 0)
            return resp

        handler.session.get.side_effect = fake_get
        with pytest.raises(SourceConnectionError, match="unparseable response"):
            handler.mcp_list_writable_devices()

    def test_non_dict_response_surfaces_cleanly(self):
        # A valid-JSON but non-dict/non-list body (a scalar, e.g. from a
        # misconfigured proxy) must fail as SourceConnectionError, not crash a
        # caller with TypeError/AttributeError on dict access.
        handler = make_hue()

        def fake_get(url, **kwargs):
            resp = MagicMock()
            resp.raise_for_status.return_value = None
            resp.json.return_value = "totally unexpected"
            return resp

        handler.session.get.side_effect = fake_get
        with pytest.raises(SourceConnectionError, match="unexpected response type"):
            handler.mcp_list_writable_devices()


class TestHueSetLight:
    def test_write_enabled_reflects_setting(self):
        assert make_hue(mcp_read_write=True).mcp_write_enabled() is True
        assert make_hue(mcp_read_write=False).mcp_write_enabled() is False

    def test_write_disabled_for_non_bool_truthy(self):
        # Strict `is True`: a stray string doesn't silently enable writes.
        assert make_hue(mcp_read_write="true").mcp_write_enabled() is False

    def test_input_validation_raises_tool_param_not_connection_error(self):
        handler = make_hue()
        _wire_bridge(handler)
        with pytest.raises(ToolParamError) as excinfo:
            handler.mcp_set_device_state("Kitchen", brightness_pct=999)
        assert not isinstance(excinfo.value, SourceConnectionError)

    def test_set_brightness_by_name_maps_and_auto_ons(self):
        handler = make_hue()
        put = _wire_bridge(handler)
        result = handler.mcp_set_device_state("Kitchen", brightness_pct=50)
        assert put.url == "https://hue.local/api/abc/lights/1/state"
        assert put.body == {"bri": 127, "on": True}  # 50% -> 127, auto-on
        assert result["device"] == "Kitchen" and result["device_id"] == "1"

    def test_set_on_off_by_id(self):
        handler = make_hue()
        put = _wire_bridge(handler)
        handler.mcp_set_device_state("2", on=False)
        assert put.url.endswith("/lights/2/state")
        assert put.body == {"on": False}

    def test_write_error_does_not_leak_the_bridge_token_to_the_caller(self):
        """A failed write must not return the bridge token to the MCP client.

        A SourceConnectionError from a write tool is handed back to whatever
        client is attached, so an unreachable bridge would otherwise send the Hue
        token off the machine - the most serious of this leak's paths.
        """
        handler = make_hue()
        handler.settings["hue"]["user"] = "SUPERSECRETHUETOKEN123"
        _wire_bridge(handler)
        handler.session.put.side_effect = requests.exceptions.HTTPError(
            "503 Server Error: Service Unavailable for url: "
            "https://hue.local/api/SUPERSECRETHUETOKEN123/lights/2/state"
        )
        with pytest.raises(SourceConnectionError) as excinfo:
            handler.mcp_set_device_state("2", on=False)
        # Equality, not substring: pins that the token is gone *and* that the rest
        # of the message (status text, host, path) survives for diagnosis.
        assert str(excinfo.value) == (
            "503 Server Error: Service Unavailable for url: https://hue.local/api/<redacted>/lights/2/state"
        )

    def test_write_path_brackets_a_bare_ipv6_host(self):
        """The write PUT brackets an IPv6 host exactly as the read GET does (SI-17).

        Both paths build their URL from the one shared _api_base(), so this is the
        guard against a future second copy of the construction reintroducing the
        bug on only one of them.
        """
        handler = make_hue()
        handler.settings["hue"]["host"] = "2001:db8::1"
        put = _wire_bridge(handler)
        handler.mcp_set_device_state("2", on=False)
        assert put.url == "https://[2001:db8::1]/api/abc/lights/2/state"

    def test_brightness_zero_maps_to_min_not_off(self):
        handler = make_hue()
        put = _wire_bridge(handler)
        handler.mcp_set_device_state("Kitchen", brightness_pct=0)
        assert put.body["bri"] == handler.HUE_BRI_MIN

    def test_brightness_hundred_maps_to_max(self):
        handler = make_hue()
        put = _wire_bridge(handler)
        handler.mcp_set_device_state("Kitchen", brightness_pct=100)
        assert put.body["bri"] == handler.HUE_BRI_MAX

    def test_explicit_off_with_brightness_is_respected(self):
        handler = make_hue()
        put = _wire_bridge(handler)
        handler.mcp_set_device_state("Kitchen", on=False, brightness_pct=80)
        assert put.body["on"] is False

    def test_set_color_temp_on_ct_light(self):
        handler = make_hue()
        put = _wire_bridge(handler)
        handler.mcp_set_device_state("Hall", color_temp_k=2700)
        # 2700 K -> 370 mirek, within Hall's [153, 454] range; auto-on.
        assert put.body == {"ct": 370, "on": True}

    @pytest.mark.parametrize("kelvin,expected_ct", [(1000, 454), (10000, 153)])
    def test_color_temp_clamped_to_light_range(self, kelvin, expected_ct):
        handler = make_hue()
        put = _wire_bridge(handler)
        handler.mcp_set_device_state("Hall", color_temp_k=kelvin)
        assert put.body["ct"] == expected_ct

    def test_set_color_on_color_light(self):
        handler = make_hue()
        put = _wire_bridge(handler)
        handler.mcp_set_device_state("Lounge", color="#ff0000")
        assert put.body["xy"] == [0.6401, 0.33]
        assert put.body["on"] is True

    def test_color_name_matches_hex(self):
        handler = make_hue()
        put = _wire_bridge(handler)
        handler.mcp_set_device_state("Lounge", color="red")
        assert put.body["xy"] == [0.6401, 0.33]

    def test_color_temp_on_white_only_light_rejected(self):
        handler = make_hue()
        _wire_bridge(handler)
        with pytest.raises(ToolParamError, match="does not support colour temperature"):
            handler.mcp_set_device_state("Kitchen", color_temp_k=2700)

    def test_color_on_ct_only_light_rejected(self):
        handler = make_hue()
        _wire_bridge(handler)
        with pytest.raises(ToolParamError, match="does not support colour"):
            handler.mcp_set_device_state("Hall", color="red")

    def test_brightness_on_plug_rejected(self):
        handler = make_hue()
        _wire_bridge(handler)
        with pytest.raises(ToolParamError, match="does not support brightness"):
            handler.mcp_set_device_state("Plug", brightness_pct=50)

    def test_color_and_color_temp_together_rejected(self):
        handler = make_hue()
        _wire_bridge(handler)
        with pytest.raises(ToolParamError, match="not both"):
            handler.mcp_set_device_state("Lounge", color_temp_k=2700, color="red")

    @pytest.mark.parametrize("bad", ["notacolour", "#12", "", 123])
    def test_invalid_color_rejected(self, bad):
        handler = make_hue()
        _wire_bridge(handler)
        with pytest.raises(ToolParamError, match="color must be"):
            handler.mcp_set_device_state("Lounge", color=bad)

    @pytest.mark.parametrize("bad", [0, -5, "hot", True])
    def test_invalid_color_temp_rejected(self, bad):
        handler = make_hue()
        _wire_bridge(handler)
        with pytest.raises(ToolParamError, match="positive number in kelvin"):
            handler.mcp_set_device_state("Hall", color_temp_k=bad)

    def test_nothing_to_set_rejected(self):
        handler = make_hue()
        _wire_bridge(handler)
        with pytest.raises(ToolParamError, match="nothing to set"):
            handler.mcp_set_device_state("Kitchen")

    def test_unknown_device_rejected(self):
        handler = make_hue()
        _wire_bridge(handler)
        with pytest.raises(ToolParamError, match="unknown device"):
            handler.mcp_set_device_state("Nonexistent", on=True)

    @pytest.mark.parametrize("bad", [-1, 101, "50", True])
    def test_invalid_brightness_rejected(self, bad):
        handler = make_hue()
        _wire_bridge(handler)
        with pytest.raises(ToolParamError):
            handler.mcp_set_device_state("Kitchen", brightness_pct=bad)

    def test_non_bool_on_rejected(self):
        handler = make_hue()
        _wire_bridge(handler)
        with pytest.raises(ToolParamError, match="on must be"):
            handler.mcp_set_device_state("Kitchen", on="yes")

    def test_ambiguous_name_rejected(self):
        handler = make_hue()
        _wire_bridge(
            handler, lights={"1": {"name": "Dup", "state": {"on": True}}, "2": {"name": "Dup", "state": {"on": True}}}
        )
        with pytest.raises(ToolParamError, match="ambiguous"):
            handler.mcp_set_device_state("Dup", on=True)

    def test_bridge_error_response_surfaces(self):
        handler = make_hue()
        _wire_bridge(handler, put_result=[{"error": {"description": "resource not available"}}])
        with pytest.raises(SourceConnectionError, match="resource not available"):
            handler.mcp_set_device_state("Kitchen", on=True)

    def test_bridge_error_non_dict_surfaces_cleanly(self):
        # A malformed error item whose "error" isn't a dict must still surface a
        # clean SourceConnectionError, not crash with AttributeError from .get().
        handler = make_hue()
        _wire_bridge(handler, put_result=[{"error": "resource not available"}])
        with pytest.raises(SourceConnectionError, match="resource not available"):
            handler.mcp_set_device_state("Kitchen", on=True)

    def test_transport_failure_surfaces(self):
        handler = make_hue()
        _wire_bridge(handler)
        handler.session.put.side_effect = requests.exceptions.ConnectionError("bridge down")
        with pytest.raises(SourceConnectionError):
            handler.mcp_set_device_state("Kitchen", on=True)

    def test_unparseable_response_surfaces(self):
        handler = make_hue()
        _wire_bridge(handler)

        def bad_json_put(url, **kwargs):
            resp = MagicMock()
            resp.raise_for_status.return_value = None
            # A real requests JSONDecodeError (ValueError + RequestException), so the
            # test verifies the parse handler wins over the transport handler.
            resp.json.side_effect = requests.exceptions.JSONDecodeError("Expecting value", "", 0)
            return resp

        handler.session.put.side_effect = bad_json_put
        with pytest.raises(SourceConnectionError, match="unparseable response"):
            handler.mcp_set_device_state("Kitchen", on=True)

    def test_non_list_response_not_treated_as_success(self):
        handler = make_hue()
        _wire_bridge(handler, put_result={"unexpected": "shape"})
        with pytest.raises(SourceConnectionError, match="unexpected response"):
            handler.mcp_set_device_state("Kitchen", on=True)

    def test_insecure_toggles_verify(self):
        handler = make_hue(insecure=False)
        put = _wire_bridge(handler)
        handler.mcp_set_device_state("Kitchen", on=True)
        assert put.verify is True  # insecure false -> verify true


class TestWriteToolRegistration:
    def _server(self):
        from mcp.server.mcpserver import MCPServer

        return MCPServer(name="test")

    def _hue_settings(self):
        return {"sources": ["hue"], "influx": {"url": "http://x", "user": "u", "password": "p"}, "hue": {}}

    def _speedtest_settings(self):
        return {"sources": ["speedtest"], "influx": {"url": "http://x", "user": "u", "password": "p"}, "speedtest": {}}

    def test_no_write_tools_when_nothing_enabled(self):
        handler = make_hue(mcp_read_write=False)
        with patch("toinflux.mcp_write.resolve_handler", return_value=handler):
            server = self._server()
            register_write_tools(server, self._hue_settings(), None)
        assert not anyio.run(server.list_tools)

    def test_hue_write_tools_registered_when_enabled(self):
        handler = make_hue(mcp_read_write=True)
        with patch("toinflux.mcp_write.resolve_handler", return_value=handler):
            server = self._server()
            register_write_tools(server, self._hue_settings(), None)
        names = {t.name for t in anyio.run(server.list_tools)}
        assert names == {"hue_list_devices", "hue_set_light"}

    def test_every_write_tool_has_a_title_and_the_applicable_hint(self):
        # Structured annotations are checked mechanically, not by reading the
        # description - a client's auto-permission logic and a registry review
        # both read title/annotations directly. read_only_hint must be explicit
        # on every tool; a non-read-only tool must also carry an explicit
        # destructive_hint, since that's the one an auto-permission gate acts on.
        hue_handler = make_hue(mcp_read_write=True)
        with patch("toinflux.mcp_write.resolve_handler", return_value=hue_handler):
            server = self._server()
            register_write_tools(server, self._hue_settings(), None)
            register_write_tools(server, self._speedtest_settings(), None)
        tools = anyio.run(server.list_tools)
        assert len(tools) == 3
        for tool in tools:
            assert tool.title, f"{tool.name} has no title"
            assert tool.title != tool.name, f"{tool.name}'s title must be distinct from its name"
            assert tool.annotations is not None, f"{tool.name} has no annotations"
            assert tool.annotations.read_only_hint is not None, f"{tool.name} has no read_only_hint"
            if tool.annotations.read_only_hint is False:
                assert tool.annotations.destructive_hint is not None, f"{tool.name} has no destructive_hint"

    def test_speedtest_write_tool_registered_when_enabled(self):
        with patch("toinflux.mcp_write.resolve_handler", return_value=make_speedtest(True)):
            server = self._server()
            register_write_tools(server, self._speedtest_settings(), None)
        names = {t.name for t in anyio.run(server.list_tools)}
        assert names == {"speedtest_run"}

    def test_writable_enabled_sources(self):
        with patch("toinflux.mcp_write.resolve_handler", return_value=make_hue(mcp_read_write=True)):
            assert writable_enabled_sources({"sources": ["hue"]}, None) == ["hue"]
        with patch("toinflux.mcp_write.resolve_handler", return_value=make_hue(mcp_read_write=False)):
            assert writable_enabled_sources({"sources": ["hue"]}, None) == []

    def test_precomputed_enabled_sources_skips_recompute(self):
        server = self._server()
        with patch("toinflux.mcp_write.writable_enabled_sources", side_effect=AssertionError("recomputed")):
            register_write_tools(server, self._hue_settings(), None, enabled_sources=["hue"])
        names = {t.name for t in anyio.run(server.list_tools)}
        assert names == {"hue_list_devices", "hue_set_light"}

    def test_precomputed_empty_registers_nothing(self):
        server = self._server()
        with patch("toinflux.mcp_write.writable_enabled_sources", side_effect=AssertionError("recomputed")):
            register_write_tools(server, self._hue_settings(), None, enabled_sources=[])
        assert not anyio.run(server.list_tools)

    def test_enabled_but_unwired_source_is_skipped_not_fatal(self):
        # A source that's write-enabled but has no registrar is logged and skipped,
        # not a crash - defends the _WRITE_TOOL_REGISTRARS invariant.
        server = self._server()
        with patch("toinflux.mcp_write.writable_enabled_sources", side_effect=AssertionError("recomputed")):
            register_write_tools(server, self._hue_settings(), None, enabled_sources=["nosuch"])
        assert not anyio.run(server.list_tools)

    def test_hue_set_light_on_disabled_source_is_rejected(self):
        handler = make_hue(mcp_read_write=False)
        with patch("toinflux.mcp_write.resolve_handlers", return_value=[(None, handler)]):
            with pytest.raises(ToolParamError, match="not enabled for device writes"):
                _hue_set_light_result(
                    self._hue_settings(),
                    None,
                    device="Kitchen",
                    on=True,
                    brightness_pct=None,
                    color_temp_k=None,
                    color=None,
                )

    def test_hue_set_light_dispatches_and_closes_session(self):
        handler = make_hue(mcp_read_write=True)
        _wire_bridge(handler)
        with patch("toinflux.mcp_write.resolve_handlers", return_value=[("hue.local", handler)]):
            result = _hue_set_light_result(
                self._hue_settings(),
                None,
                device="Kitchen",
                on=True,
                brightness_pct=None,
                color_temp_k=None,
                color=None,
            )
        assert result["device"] == "Kitchen"
        handler.session.close.assert_called_once()

    def test_hue_set_light_closes_session_on_error(self):
        handler = make_hue(mcp_read_write=True)
        _wire_bridge(handler)
        handler.session.put.side_effect = requests.exceptions.ConnectionError("down")
        with patch("toinflux.mcp_write.resolve_handlers", return_value=[("hue.local", handler)]):
            with pytest.raises(SourceConnectionError):
                _hue_set_light_result(
                    self._hue_settings(),
                    None,
                    device="Kitchen",
                    on=True,
                    brightness_pct=None,
                    color_temp_k=None,
                    color=None,
                )
        handler.session.close.assert_called_once()

    def test_hue_list_devices_dispatches_and_closes_session(self):
        handler = make_hue(mcp_read_write=True)
        _wire_bridge(handler)
        with patch("toinflux.mcp_write.resolve_handlers", return_value=[("hue.local", handler)]):
            result = _hue_list_devices_result(self._hue_settings(), None)
        assert result["source"] == "hue" and any(d["name"] == "Kitchen" for d in result["devices"])
        handler.session.close.assert_called_once()

    def test_speedtest_run_result_dispatches_and_closes_session(self):
        handler = make_speedtest(True)
        handler.mcp_trigger_run = MagicMock(return_value={"source": "speedtest", "recorded": True, "result": {}})
        with patch("toinflux.mcp_write.resolve_handler", return_value=handler):
            result = _speedtest_run_result(self._speedtest_settings(), None)
        assert result["source"] == "speedtest"
        handler.session.close.assert_called_once()


class TestHueMultiBridgeWrites:
    """The write tools must see every bridge, and must never guess which one."""

    @staticmethod
    def _two_bridges(same_name=False, second_fails=False):
        """Two handlers, each with its own lights. Both bridges have a light id "1" -
        ids are per-bridge, so that collision is the normal case, not a contrivance."""
        downstairs = make_hue(mcp_read_write=True)
        upstairs = make_hue(mcp_read_write=True)
        _wire_bridge(downstairs, lights={"1": {"name": "Kitchen", "state": {"on": False, "bri": 10}}})
        upstairs_name = "Kitchen" if same_name else "Landing"
        _wire_bridge(upstairs, lights={"1": {"name": upstairs_name, "state": {"on": False, "bri": 10}}})
        if second_fails:
            upstairs.session.get.side_effect = requests.exceptions.ConnectionError("upstairs down")
        return [("downstairs.example.com", downstairs), ("upstairs.example.com", upstairs)]

    def _settings(self):
        return {"sources": ["hue"], "influx": {"url": "http://x", "user": "u", "password": "p"}, "hue": {}}

    def test_list_covers_every_bridge_and_labels_each_device(self):
        """Without the bridge on each entry, two lights sharing an id are indistinguishable."""
        handlers = self._two_bridges()
        with patch("toinflux.mcp_write.resolve_handlers", return_value=handlers):
            result = _hue_list_devices_result(self._settings(), None)
        assert [(d["bridge"], d["id"], d["name"]) for d in result["devices"]] == [
            ("downstairs.example.com", "1", "Kitchen"),
            ("upstairs.example.com", "1", "Landing"),
        ]
        assert "unreachable" not in result

    def test_list_reports_an_unreachable_bridge_rather_than_omitting_it_silently(self):
        """A short list must not read as "no such light" when it means "could not ask"."""
        handlers = self._two_bridges(second_fails=True)
        with patch("toinflux.mcp_write.resolve_handlers", return_value=handlers):
            result = _hue_list_devices_result(self._settings(), None)
        assert [d["bridge"] for d in result["devices"]] == ["downstairs.example.com"]
        assert result["unreachable"] == [{"bridge": "upstairs.example.com", "error": "upstairs down"}]

    def test_an_unreachable_other_bridge_is_an_actionable_refusal_not_a_transport_error(self):
        """One bridge being down must not make every write impossible.

        Reported by review and reproduced: the arbitration loop had no error handling, so a
        light uniquely resolvable on a *healthy* bridge could not be written to while some
        other bridge was unreachable - the raw SourceConnectionError propagated. That reads
        as transient, so a caller retries and fails identically.

        Acting on the lone match anyway is deliberately not the fix: the silent bridge may
        carry that name too, and actuating the wrong light is not recoverable. So it still
        refuses - but as a ToolParamError that names the missing bridge and says 'bridge'
        proceeds without it, which is something the caller can actually do.
        """
        handlers = self._two_bridges(second_fails=True)
        with patch("toinflux.mcp_write.resolve_handlers", return_value=handlers):
            with pytest.raises(ToolParamError) as excinfo:
                _hue_set_light_result(
                    self._settings(),
                    None,
                    device="Kitchen",
                    on=True,
                    brightness_pct=None,
                    color_temp_k=None,
                    color=None,
                )
        message = str(excinfo.value)
        assert "bridge upstairs.example.com" in message and "could not be reached" in message
        assert "Pass 'bridge'" in message
        # The match that *was* found is reported, so the caller knows what naming a bridge
        # would get them rather than having to guess.
        assert "'Kitchen' (id 1) on bridge downstairs.example.com" in message

    def test_naming_a_healthy_bridge_writes_while_another_is_down(self):
        """The escape hatch the refusal above points at has to actually work."""
        handlers = self._two_bridges(second_fails=True)
        with patch("toinflux.mcp_write.resolve_handlers", return_value=handlers):
            result = _hue_set_light_result(
                self._settings(),
                None,
                device="Kitchen",
                on=True,
                brightness_pct=None,
                color_temp_k=None,
                color=None,
                bridge="downstairs.example.com",
            )
        assert result["bridge"] == "downstairs.example.com"
        assert result["device"] == "Kitchen"

    def test_naming_the_unreachable_bridge_stays_a_transport_error(self):
        """Then the failure is against the target itself, and a retry is the right response -
        so it must not be flattened into a caller-mistake error."""
        handlers = self._two_bridges(second_fails=True)
        with patch("toinflux.mcp_write.resolve_handlers", return_value=handlers):
            with pytest.raises(SourceConnectionError):
                _hue_set_light_result(
                    self._settings(),
                    None,
                    device="Landing",
                    on=True,
                    brightness_pct=None,
                    color_temp_k=None,
                    color=None,
                    bridge="upstairs.example.com",
                )

    def test_a_single_unreachable_bridge_stays_a_transport_error(self):
        """With one bridge configured there is nothing to arbitrate, so an unreachable bridge
        is simply down - not an ambiguity the caller can resolve by naming it."""
        handlers = self._two_bridges(second_fails=True)[1:]
        with patch("toinflux.mcp_write.resolve_handlers", return_value=handlers):
            with pytest.raises(SourceConnectionError):
                _hue_set_light_result(
                    self._settings(),
                    None,
                    device="Landing",
                    on=True,
                    brightness_pct=None,
                    color_temp_k=None,
                    color=None,
                )

    def test_a_unique_name_needs_no_bridge(self):
        """The common case stays simple: one match across the estate, act on it."""
        handlers = self._two_bridges()
        with patch("toinflux.mcp_write.resolve_handlers", return_value=handlers):
            result = _hue_set_light_result(
                self._settings(),
                None,
                device="Landing",
                on=True,
                brightness_pct=None,
                color_temp_k=None,
                color=None,
            )
        assert result["bridge"] == "upstairs.example.com"
        assert result["device"] == "Landing"

    def test_a_name_on_both_bridges_is_refused_not_guessed(self):
        """Acceptance criterion 15. Actuating the wrong light is not recoverable, so an
        ambiguous name must be refused - with the bridges named, so it can be resolved."""
        handlers = self._two_bridges(same_name=True)
        with patch("toinflux.mcp_write.resolve_handlers", return_value=handlers):
            with pytest.raises(ToolParamError) as excinfo:
                _hue_set_light_result(
                    self._settings(),
                    None,
                    device="Kitchen",
                    on=True,
                    brightness_pct=None,
                    color_temp_k=None,
                    color=None,
                )
        message = str(excinfo.value)
        assert "ambiguous" in message
        assert "on bridge downstairs.example.com" in message and "on bridge upstairs.example.com" in message
        # Cross-bridge ambiguity IS resolvable with 'bridge', so that is the hint here -
        # unlike two lights sharing a name on one bridge (see the test above).
        assert "Pass 'bridge'" in message
        # Nothing was actuated on either bridge.
        for _, handler in handlers:
            handler.session.put.assert_not_called()

    def test_duplicate_names_on_one_bridge_advise_the_id_not_the_bridge(self):
        """Hue allows two lights to share a name on a single bridge, and there 'bridge'
        cannot disambiguate anything - only the id can.

        A test gap the review found: every earlier case had the two matches on different
        bridges, so the cross-bridge wording and the 'pass bridge' hint were never checked
        against an ambiguity that is not cross-bridge.
        """
        handler = make_hue(mcp_read_write=True)
        _wire_bridge(
            handler,
            lights={
                "1": {"name": "Kitchen", "state": {"on": False, "bri": 10}},
                "2": {"name": "Kitchen", "state": {"on": False, "bri": 10}},
            },
        )
        with patch("toinflux.mcp_write.resolve_handlers", return_value=[("only.example.com", handler)]):
            with pytest.raises(ToolParamError) as excinfo:
                _hue_set_light_result(
                    self._settings(),
                    None,
                    device="Kitchen",
                    on=True,
                    brightness_pct=None,
                    color_temp_k=None,
                    color=None,
                )
        message = str(excinfo.value)
        assert "ambiguous" in message
        assert "Use the light id" in message
        # Must not send the caller down a dead end, or misdescribe the cause.
        assert "bridge" not in message.replace("on bridge only.example.com", "")
        handler.session.put.assert_not_called()

    def test_an_id_alone_is_ambiguous_across_bridges(self):
        """Every bridge has a light "1", so a bare id is ambiguous by nature."""
        handlers = self._two_bridges()
        with patch("toinflux.mcp_write.resolve_handlers", return_value=handlers):
            with pytest.raises(ToolParamError, match="ambiguous"):
                _hue_set_light_result(
                    self._settings(),
                    None,
                    device="1",
                    on=True,
                    brightness_pct=None,
                    color_temp_k=None,
                    color=None,
                )

    def test_bridge_disambiguates_a_repeated_name(self):
        """Passing bridge resolves what would otherwise be refused."""
        handlers = self._two_bridges(same_name=True)
        with patch("toinflux.mcp_write.resolve_handlers", return_value=handlers):
            result = _hue_set_light_result(
                self._settings(),
                None,
                device="Kitchen",
                on=True,
                brightness_pct=None,
                color_temp_k=None,
                color=None,
                bridge="upstairs.example.com",
            )
        assert result["bridge"] == "upstairs.example.com"
        handlers[0][1].session.put.assert_not_called()  # downstairs untouched
        handlers[1][1].session.put.assert_called_once()

    def test_unknown_bridge_is_refused_with_the_configured_ones_listed(self):
        handlers = self._two_bridges()
        with patch("toinflux.mcp_write.resolve_handlers", return_value=handlers):
            with pytest.raises(ToolParamError) as excinfo:
                _hue_set_light_result(
                    self._settings(),
                    None,
                    device="Kitchen",
                    on=True,
                    brightness_pct=None,
                    color_temp_k=None,
                    color=None,
                    bridge="nosuch.example.com",
                )
        assert "unknown bridge" in str(excinfo.value)
        assert "configured bridges: downstairs.example.com" in str(excinfo.value)

    def test_unknown_device_names_the_discovery_tool(self):
        handlers = self._two_bridges()
        with patch("toinflux.mcp_write.resolve_handlers", return_value=handlers):
            with pytest.raises(ToolParamError, match="hue_list_devices"):
                _hue_set_light_result(
                    self._settings(),
                    None,
                    device="Nowhere",
                    on=True,
                    brightness_pct=None,
                    color_temp_k=None,
                    color=None,
                )

    def test_every_session_is_closed(self):
        """A partial failure part-way through must not leak earlier bridges' sessions."""
        handlers = self._two_bridges()
        with patch("toinflux.mcp_write.resolve_handlers", return_value=handlers):
            with pytest.raises(ToolParamError):
                _hue_set_light_result(
                    self._settings(),
                    None,
                    device="1",
                    on=True,
                    brightness_pct=None,
                    color_temp_k=None,
                    color=None,
                )
        for _, handler in handlers:
            handler.session.close.assert_called_once()
