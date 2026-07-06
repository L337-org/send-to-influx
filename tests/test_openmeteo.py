"""Unit tests for toinflux.openmeteo (OpenMeteo)."""

from unittest.mock import MagicMock, patch
import pytest
import requests
from toinflux.openmeteo import OpenMeteo
from toinflux.exceptions import SourceConnectionError


def _openmeteo_settings(base):
    """Build minimal settings dict for OpenMeteo tests."""
    settings = {**base}
    settings["openmeteo"] = {
        "db": "weather_db",
        "interval": 900,
        "latitude": 51.5,
        "longitude": -0.1,
        "fields": ["temperature_2m", "relative_humidity_2m"],
    }
    return settings


class TestOpenMeteo:
    """Tests for OpenMeteo class."""

    def test_get_data_returns_configured_fields_and_sets_influx_header(self, sample_settings):
        """get_data returns only configured fields and sets the correct influx_header."""
        settings = _openmeteo_settings(sample_settings)
        api_response = {
            "current": {
                "temperature_2m": 18.5,
                "relative_humidity_2m": 72,
                "precipitation": 0.0,
            }
        }
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = OpenMeteo(source="openmeteo")
            mock_resp = MagicMock()
            mock_resp.json.return_value = api_response
            with patch.object(handler.session, "get", return_value=mock_resp):
                result = handler.get_data()
                assert result["temperature_2m"] == 18.5
                assert result["relative_humidity_2m"] == 72
                assert "precipitation" not in result
                assert handler.influx_header == "weather,source=open-meteo "
                assert handler.data == result

    def test_get_data_skips_fields_absent_from_api_response(self, sample_settings):
        """get_data silently drops fields that are absent from the API response."""
        settings = _openmeteo_settings(sample_settings)
        settings["openmeteo"]["fields"] = ["temperature_2m", "nonexistent_field"]
        api_response = {"current": {"temperature_2m": 15.0}}
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = OpenMeteo(source="openmeteo")
            mock_resp = MagicMock()
            mock_resp.json.return_value = api_response
            with patch.object(handler.session, "get", return_value=mock_resp):
                result = handler.get_data()
                assert result == {"temperature_2m": 15.0}
                assert "nonexistent_field" not in result

    def test_get_data_passes_lat_lon_and_fields_as_params(self, sample_settings):
        """get_data sends latitude, longitude, and joined fields to the API."""
        settings = _openmeteo_settings(sample_settings)
        api_response = {"current": {"temperature_2m": 10.0, "relative_humidity_2m": 80}}
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = OpenMeteo(source="openmeteo")
            mock_resp = MagicMock()
            mock_resp.json.return_value = api_response
            with patch.object(handler.session, "get", return_value=mock_resp) as mock_get:
                handler.get_data()
                call_kwargs = mock_get.call_args[1]
                params = call_kwargs["params"]
                assert params["latitude"] == 51.5
                assert params["longitude"] == -0.1
                assert params["current"] == "temperature_2m,relative_humidity_2m"
                assert params["timezone"] == "auto"

    def test_get_data_raises_source_connection_error_on_request_exception(self, sample_settings):
        """get_data raises SourceConnectionError when the HTTP request fails."""
        settings = _openmeteo_settings(sample_settings)
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = OpenMeteo(source="openmeteo")
            with patch.object(
                handler.session,
                "get",
                side_effect=requests.exceptions.ConnectionError("timeout"),
            ):
                with pytest.raises(SourceConnectionError):
                    handler.get_data()
