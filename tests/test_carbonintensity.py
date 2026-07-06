"""Unit tests for toinflux.carbonintensity (CarbonIntensity)."""

from unittest.mock import MagicMock, patch
import pytest
import requests
from toinflux.carbonintensity import CarbonIntensity
from toinflux.exceptions import SourceConnectionError


def _ci_settings(base, include_generation=False):
    """Build minimal settings dict for CarbonIntensity tests."""
    settings = {**base}
    settings["carbonintensity"] = {
        "db": "grid_db",
        "interval": 1800,
        "timeout": 10,
        "include_generation": include_generation,
    }
    return settings


def _mock_get(responses):
    """Return a side_effect function that yields successive mock responses."""
    responses_iter = iter(responses)

    def side_effect(*args, **kwargs):
        resp = MagicMock()
        resp.json.return_value = next(responses_iter)
        return resp

    return side_effect


_INTENSITY_RESPONSE = {"data": [{"intensity": {"forecast": 150, "actual": 143, "index": "moderate"}}]}

_GENERATION_RESPONSE = {
    "data": {
        "generationmix": [
            {"fuel": "gas", "perc": 30.1},
            {"fuel": "nuclear", "perc": 18.5},
            {"fuel": "wind", "perc": 35.2},
            {"fuel": "solar", "perc": 8.7},
            {"fuel": "coal", "perc": 0.0},
        ]
    }
}


class TestCarbonIntensity:
    """Tests for CarbonIntensity class."""

    def test_get_data_returns_intensity_and_sets_influx_header(self, sample_settings):
        """get_data returns actual and forecast intensity and sets correct influx_header."""
        settings = _ci_settings(sample_settings)
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = CarbonIntensity(source="carbonintensity")
            with patch("toinflux.carbonintensity.requests.get", side_effect=_mock_get([_INTENSITY_RESPONSE])):
                result = handler.get_data()
                assert result["intensity_actual"] == 143
                assert result["intensity_forecast"] == 150
                assert handler.influx_header == "carbonintensity,source=national_grid "
                assert handler.data == result

    def test_get_data_includes_generation_when_configured(self, sample_settings):
        """get_data adds gen_* fields when include_generation is True."""
        settings = _ci_settings(sample_settings, include_generation=True)
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = CarbonIntensity(source="carbonintensity")
            with patch(
                "toinflux.carbonintensity.requests.get",
                side_effect=_mock_get([_INTENSITY_RESPONSE, _GENERATION_RESPONSE]),
            ):
                result = handler.get_data()
                assert result["gen_gas"] == 30.1
                assert result["gen_wind"] == 35.2
                assert result["gen_solar"] == 8.7
                assert result["gen_coal"] == 0.0

    def test_get_data_skips_generation_when_not_configured(self, sample_settings):
        """get_data omits gen_* fields when include_generation is False."""
        settings = _ci_settings(sample_settings, include_generation=False)
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = CarbonIntensity(source="carbonintensity")
            with patch("toinflux.carbonintensity.requests.get", side_effect=_mock_get([_INTENSITY_RESPONSE])):
                result = handler.get_data()
                assert not any(k.startswith("gen_") for k in result)

    def test_get_data_handles_null_actual(self, sample_settings):
        """get_data omits intensity_actual when the API returns null for actual."""
        settings = _ci_settings(sample_settings)
        response = {"data": [{"intensity": {"forecast": 200, "actual": None, "index": "high"}}]}
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = CarbonIntensity(source="carbonintensity")
            with patch("toinflux.carbonintensity.requests.get", side_effect=_mock_get([response])):
                result = handler.get_data()
                assert "intensity_actual" not in result
                assert result["intensity_forecast"] == 200

    def test_get_data_raises_source_connection_error_on_request_exception(self, sample_settings):
        """get_data raises SourceConnectionError when the HTTP request fails."""
        settings = _ci_settings(sample_settings)
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = CarbonIntensity(source="carbonintensity")
            with patch(
                "toinflux.carbonintensity.requests.get",
                side_effect=requests.exceptions.ConnectionError("timeout"),
            ):
                with pytest.raises(SourceConnectionError):
                    handler.get_data()

    def test_get_data_sends_accept_json_header(self, sample_settings):
        """get_data includes Accept: application/json header in all requests."""
        settings = _ci_settings(sample_settings)
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = CarbonIntensity(source="carbonintensity")
            with patch(
                "toinflux.carbonintensity.requests.get", side_effect=_mock_get([_INTENSITY_RESPONSE])
            ) as mock_get:
                handler.get_data()
                headers_used = mock_get.call_args[1]["headers"]
                assert headers_used.get("Accept") == "application/json"
