"""Unit tests for toinflux.philipshue (Hue)."""

from unittest.mock import MagicMock, patch
import pytest
import requests

from toinflux.philipshue import Hue
from toinflux.exceptions import SourceConnectionError


class TestHue:
    """Tests for Hue class."""

    def test_get_data_sets_influx_header_and_returns_parsed_data(self, sample_settings):
        """get_data sets influx_header and returns parse_hue_data result."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            with patch.object(Hue, "parse_hue_data", return_value={"room1": 21.5}) as mock_parse:
                hue = Hue(source="hue")
                result = hue.get_data()
                mock_parse.assert_called_once()
                assert hue.influx_header == f"hue,host={sample_settings['hue']['host']} "
                assert result == {"room1": 21.5}
                assert hue.data == {"room1": 21.5}

    def test_get_data_from_hue_bridge_returns_json_on_success(self, sample_settings):
        """get_data_from_hue_bridge returns parsed JSON when request succeeds."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            hue = Hue(source="hue")
            mock_response = MagicMock()
            mock_response.json.return_value = {"sensors": {}, "lights": {}}
            with patch.object(hue.session, "get", return_value=mock_response):
                result = hue.get_data_from_hue_bridge()
                assert result == {"sensors": {}, "lights": {}}

    def test_get_data_from_hue_bridge_raises_on_request_exception(self, sample_settings):
        """get_data_from_hue_bridge raises SourceConnectionError on requests exception."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            hue = Hue(source="hue")
            with patch.object(hue.session, "get") as mock_get:
                mock_get.side_effect = requests.exceptions.RequestException("connection failed")
                with pytest.raises(SourceConnectionError):
                    hue.get_data_from_hue_bridge()

    def test_get_data_from_hue_bridge_skips_tls_verification_by_default(self, sample_settings):
        """get_data_from_hue_bridge defaults to verify=False (backward-compatible with self-signed bridge certs)."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            hue = Hue(source="hue")
            mock_response = MagicMock()
            mock_response.json.return_value = {"sensors": {}, "lights": {}}
            with patch.object(hue.session, "get", return_value=mock_response) as mock_get:
                hue.get_data_from_hue_bridge()
                assert mock_get.call_args[1]["verify"] is False

    def test_get_data_from_hue_bridge_verifies_tls_when_insecure_false(self, sample_settings):
        """get_data_from_hue_bridge passes verify=True when hue.insecure is explicitly false."""
        settings = {**sample_settings, "hue": {**sample_settings["hue"], "insecure": False}}
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            hue = Hue(source="hue")
            mock_response = MagicMock()
            mock_response.json.return_value = {"sensors": {}, "lights": {}}
            with patch.object(hue.session, "get", return_value=mock_response) as mock_get:
                hue.get_data_from_hue_bridge()
                assert mock_get.call_args[1]["verify"] is True

    def test_get_data_from_hue_bridge_suppresses_warning_only_when_insecure(self, sample_settings):
        """get_data_from_hue_bridge only suppresses InsecureRequestWarning when insecure is true."""
        settings = {**sample_settings, "hue": {**sample_settings["hue"], "insecure": False}}
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            hue = Hue(source="hue")
            mock_response = MagicMock()
            mock_response.json.return_value = {"sensors": {}, "lights": {}}
            with patch.object(hue.session, "get", return_value=mock_response):
                with patch("toinflux.philipshue.warnings.simplefilter") as mock_simplefilter:
                    hue.get_data_from_hue_bridge()
                    mock_simplefilter.assert_not_called()

    def test_get_data_from_hue_bridge_raises_on_api_error_list(self, sample_settings):
        """get_data_from_hue_bridge raises SourceConnectionError when API returns error list."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            hue = Hue(source="hue")
            mock_response = MagicMock()
            mock_response.json.return_value = [{"error": {"description": "unauthorized"}}]
            with patch.object(hue.session, "get", return_value=mock_response):
                with pytest.raises(SourceConnectionError):
                    hue.get_data_from_hue_bridge()

    def test_get_data_from_hue_bridge_raises_on_empty_list(self, sample_settings):
        """An empty JSON list must fail cleanly, not raise IndexError from hue_data[0]."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            hue = Hue(source="hue")
            mock_response = MagicMock()
            mock_response.json.return_value = []
            with patch.object(hue.session, "get", return_value=mock_response):
                with pytest.raises(SourceConnectionError, match="unexpected list response"):
                    hue.get_data_from_hue_bridge()

    def test_get_data_from_hue_bridge_raises_on_unparseable_body(self, sample_settings):
        """A non-JSON body raises SourceConnectionError, not an unhandled ValueError."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            hue = Hue(source="hue")
            mock_response = MagicMock()
            mock_response.json.side_effect = ValueError("no JSON")
            with patch.object(hue.session, "get", return_value=mock_response):
                with pytest.raises(SourceConnectionError, match="unparseable response"):
                    hue.get_data_from_hue_bridge()

    def test_hue_device_name_to_name_uses_mapping_when_present(self, sample_settings):
        """hue_device_name_to_name uses sensors mapping when in settings."""
        settings = {**sample_settings}
        settings["hue"] = {**settings["hue"], "sensors": {"Device A": "Mapped_Name"}}
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            hue = Hue(source="hue")
            assert hue.hue_device_name_to_name("Device A") == "Mapped_Name"
            assert hue.hue_device_name_to_name("Unknown Device") == "Unknown_Device"

    def test_hue_device_name_to_name_replaces_spaces_with_underscores(self, sample_settings):
        """hue_device_name_to_name replaces spaces with underscores."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            hue = Hue(source="hue")
            assert hue.hue_device_name_to_name("Room 1 Sensor") == "Room_1_Sensor"

    def test_hue_device_name_to_name_falls_back_to_device_name_without_sensors(self, sample_settings):
        """hue_device_name_to_name uses device name when no sensors key."""
        settings = {**sample_settings}
        s = settings["hue"].copy()
        s.pop("sensors", None)
        settings["hue"] = s
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            hue = Hue(source="hue")
            assert hue.hue_device_name_to_name("My Sensor") == "My_Sensor"

    def test_parse_hue_data_temperature_celsius(self, sample_settings):
        """parse_hue_data converts ZLLTemperature to Celsius."""
        settings = {**sample_settings, "hue": {**sample_settings["hue"], "temperature_units": "C"}}
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            hue = Hue(source="hue")
            hue_data = {
                "sensors": {
                    "1": {"name": "Temp", "type": "ZLLTemperature", "state": {"temperature": 2150}},
                },
                "lights": {},
            }
            with patch.object(Hue, "get_data_from_hue_bridge", return_value=hue_data):
                result = hue.parse_hue_data()
                assert result["Temp"] == 21.5

    def test_parse_hue_data_temperature_fahrenheit(self, sample_settings):
        """parse_hue_data converts ZLLTemperature to Fahrenheit when configured."""
        settings = {**sample_settings, "hue": {**sample_settings["hue"], "temperature_units": "F"}}
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            hue = Hue(source="hue")
            hue_data = {
                "sensors": {
                    "1": {"name": "Temp", "type": "ZLLTemperature", "state": {"temperature": 2500}},
                },
                "lights": {},
            }
            with patch.object(Hue, "get_data_from_hue_bridge", return_value=hue_data):
                result = hue.parse_hue_data()
                assert result["Temp"] == 77.0

    def test_parse_hue_data_temperature_kelvin(self, sample_settings):
        """parse_hue_data converts ZLLTemperature to Kelvin when configured."""
        settings = {**sample_settings, "hue": {**sample_settings["hue"], "temperature_units": "K"}}
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            hue = Hue(source="hue")
            # 0 centidegrees C = 0°C -> 273.15 K
            hue_data = {
                "sensors": {
                    "1": {"name": "Temp", "type": "ZLLTemperature", "state": {"temperature": 0}},
                },
                "lights": {},
            }
            with patch.object(Hue, "get_data_from_hue_bridge", return_value=hue_data):
                result = hue.parse_hue_data()
                assert result["Temp"] == 273.15

    def test_parse_hue_data_light_level(self, sample_settings):
        """parse_hue_data converts ZLLLightLevel to lux."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            hue = Hue(source="hue")
            hue_data = {
                "sensors": {
                    "1": {"name": "Light", "type": "ZLLLightLevel", "state": {"lightlevel": 1}},
                },
                "lights": {},
            }
            with patch.object(Hue, "get_data_from_hue_bridge", return_value=hue_data):
                result = hue.parse_hue_data()
                assert "Light" in result
                assert result["Light"] == round(float(10 ** ((1 - 1) / 10000)), 2)

    def test_parse_hue_data_presence(self, sample_settings):
        """parse_hue_data converts ZLLPresence to 0 or 1."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            hue = Hue(source="hue")
            hue_data = {
                "sensors": {
                    "1": {"name": "Motion", "type": "ZLLPresence", "state": {"presence": True}},
                    "2": {"name": "Motion2", "type": "ZLLPresence", "state": {"presence": False}},
                },
                "lights": {},
            }
            with patch.object(Hue, "get_data_from_hue_bridge", return_value=hue_data):
                result = hue.parse_hue_data()
                assert result["Motion"] == 1
                assert result["Motion2"] == 0

    def test_parse_hue_data_lights_on_dimmable(self, sample_settings):
        """parse_hue_data converts dimmable light bri to percentage."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            hue = Hue(source="hue")
            hue_data = {
                "sensors": {},
                "lights": {
                    "1": {"name": "Lamp", "state": {"on": True, "bri": 127}},
                },
            }
            with patch.object(Hue, "get_data_from_hue_bridge", return_value=hue_data):
                result = hue.parse_hue_data()
                assert result["Lamp"] == int(127 / 2.54)

    def test_parse_hue_data_lights_off(self, sample_settings):
        """parse_hue_data sets 0 when light is off."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            hue = Hue(source="hue")
            hue_data = {
                "sensors": {},
                "lights": {
                    "1": {"name": "Lamp", "state": {"on": False, "bri": 200}},
                },
            }
            with patch.object(Hue, "get_data_from_hue_bridge", return_value=hue_data):
                result = hue.parse_hue_data()
                assert result["Lamp"] == 0
