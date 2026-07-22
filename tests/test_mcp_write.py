"""Unit tests for the MCP device-write path: the Hue write primitive and the
opt-in, least-privilege write tool registration."""

from unittest.mock import MagicMock, patch

import anyio
import pytest
import requests

from toinflux.exceptions import SourceConnectionError, ToolParamError
from toinflux.mcp_write import (
    register_write_tools,
    writable_enabled_sources,
    _set_device_state_result,
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


def _bridge_lights():
    return {
        "1": {"name": "Kitchen", "state": {"on": False, "bri": 10}},
        "2": {"name": "Lamp", "state": {"on": True, "bri": 200}},
    }


def _wire_bridge(handler, put_result=None):
    """Point the handler's mocked session at a fake bridge GET (device list) and
    PUT (state change), recording the PUT url/body on the returned closure."""
    put_result = put_result if put_result is not None else [{"success": {"/lights/1/state/on": True}}]

    def fake_get(url, **kwargs):
        resp = MagicMock()
        resp.json.return_value = {"lights": _bridge_lights()}
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


class TestHueWritePrimitive:
    def test_write_enabled_reflects_setting(self):
        assert make_hue(mcp_read_write=True).mcp_write_enabled() is True
        assert make_hue(mcp_read_write=False).mcp_write_enabled() is False

    def test_input_validation_raises_tool_param_not_connection_error(self):
        # A bad input raises ToolParamError, and specifically NOT
        # SourceConnectionError, so it isn't misclassified as retryable.
        handler = make_hue()
        _wire_bridge(handler)
        with pytest.raises(ToolParamError) as excinfo:
            handler.mcp_set_device_state("Kitchen", brightness_pct=999)
        assert not isinstance(excinfo.value, SourceConnectionError)

    def test_write_disabled_for_non_bool_truthy(self):
        # Strict `is True`: a stray string doesn't silently enable writes.
        handler = make_hue(mcp_read_write="true")
        assert handler.mcp_write_enabled() is False

    def test_list_writable_devices(self):
        handler = make_hue()
        _wire_bridge(handler)
        assert handler.mcp_list_writable_devices() == {"1": "Kitchen", "2": "Lamp"}

    def test_list_writable_devices_names_are_strings(self):
        # A missing or non-string bridge name falls back to the id, and everything
        # comes back as a string (the docstring promises {id: name}).
        handler = make_hue()

        def fake_get(url, **kwargs):
            resp = MagicMock()
            resp.json.return_value = {"lights": {"1": {}, "2": {"name": ""}, "3": {"name": "Lamp"}}}
            resp.raise_for_status.return_value = None
            return resp

        handler.session.get.side_effect = fake_get
        devices = handler.mcp_list_writable_devices()
        assert devices == {"1": "1", "2": "2", "3": "Lamp"}
        assert all(isinstance(v, str) for v in devices.values())

    def test_list_writable_devices_unparseable_response_surfaces(self):
        handler = make_hue()

        def fake_get(url, **kwargs):
            resp = MagicMock()
            resp.raise_for_status.return_value = None
            resp.json.side_effect = ValueError("not JSON")
            return resp

        handler.session.get.side_effect = fake_get
        with pytest.raises(SourceConnectionError, match="unparseable response"):
            handler.mcp_list_writable_devices()

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

        def fake_get(url, **kwargs):
            resp = MagicMock()
            resp.json.return_value = {"lights": {"1": {"name": "Dup"}, "2": {"name": "Dup"}}}
            resp.raise_for_status.return_value = None
            return resp

        handler.session.get.side_effect = fake_get
        with pytest.raises(ToolParamError, match="ambiguous"):
            handler.mcp_set_device_state("Dup", on=True)

    def test_bridge_error_response_surfaces(self):
        handler = make_hue()
        _wire_bridge(handler, put_result=[{"error": {"description": "resource not available"}}])
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
            resp.json.side_effect = ValueError("no JSON")
            return resp

        handler.session.put.side_effect = bad_json_put
        with pytest.raises(SourceConnectionError, match="unparseable response"):
            handler.mcp_set_device_state("Kitchen", on=True)

    def test_non_list_response_not_treated_as_success(self):
        # A dict body (not the CLIP list shape) must fail, not read as an empty
        # error list (i.e. silent success).
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

    def _settings(self):
        return {"sources": ["hue"], "influx": {"url": "http://x", "user": "u", "password": "p"}, "hue": {}}

    def test_no_write_tools_when_nothing_enabled(self):
        handler = make_hue(mcp_read_write=False)
        with patch("toinflux.mcp_write.resolve_handler", return_value=handler):
            server = self._server()
            register_write_tools(server, self._settings(), None)
        names = {t.name for t in anyio.run(server.list_tools)}
        assert "set_device_state" not in names and "list_writable_devices" not in names

    def test_write_tools_registered_when_enabled(self):
        handler = make_hue(mcp_read_write=True)
        with patch("toinflux.mcp_write.resolve_handler", return_value=handler):
            server = self._server()
            register_write_tools(server, self._settings(), None)
        names = {t.name for t in anyio.run(server.list_tools)}
        assert names == {"list_writable_devices", "set_device_state"}

    def test_writable_enabled_sources(self):
        with patch("toinflux.mcp_write.resolve_handler", return_value=make_hue(mcp_read_write=True)):
            assert writable_enabled_sources({"sources": ["hue"]}, None) == ["hue"]
        with patch("toinflux.mcp_write.resolve_handler", return_value=make_hue(mcp_read_write=False)):
            assert writable_enabled_sources({"sources": ["hue"]}, None) == []

    def test_set_device_state_on_disabled_source_is_rejected(self):
        handler = make_hue(mcp_read_write=False)
        with patch("toinflux.mcp_write.resolve_handler", return_value=handler):
            with pytest.raises(ToolParamError, match="not enabled for device writes"):
                _set_device_state_result(
                    self._settings(), None, source="hue", device="Kitchen", on=True, brightness_pct=None
                )

    def test_set_device_state_dispatches_and_closes_session(self):
        handler = make_hue(mcp_read_write=True)
        _wire_bridge(handler)
        with patch("toinflux.mcp_write.resolve_handler", return_value=handler):
            result = _set_device_state_result(
                self._settings(), None, source="hue", device="Kitchen", on=True, brightness_pct=None
            )
        assert result["device"] == "Kitchen"
        handler.session.close.assert_called_once()

    def test_set_device_state_closes_session_on_error(self):
        handler = make_hue(mcp_read_write=True)
        _wire_bridge(handler)
        handler.session.put.side_effect = requests.exceptions.ConnectionError("down")
        with patch("toinflux.mcp_write.resolve_handler", return_value=handler):
            with pytest.raises(SourceConnectionError):
                _set_device_state_result(
                    self._settings(), None, source="hue", device="Kitchen", on=True, brightness_pct=None
                )
        handler.session.close.assert_called_once()
