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
        from mcp.server.fastmcp import FastMCP

        return FastMCP(name="test")

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
        with patch("toinflux.mcp_write.resolve_handler", return_value=handler):
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
        with patch("toinflux.mcp_write.resolve_handler", return_value=handler):
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
        with patch("toinflux.mcp_write.resolve_handler", return_value=handler):
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
        with patch("toinflux.mcp_write.resolve_handler", return_value=handler):
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
