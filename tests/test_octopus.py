"""Unit tests for toinflux.octopus (Octopus)."""

from unittest.mock import MagicMock, patch
import pytest
import requests
from toinflux.octopus import Octopus


def _octopus_settings(base):
    """Build minimal settings dict for Octopus tests."""
    settings = {**base}
    settings["octopus"] = {
        "db": "octopus_db",
        "interval": 1800,
        "api_key": "test_api_key",
        "mpan": "1234567890123",
        "meter_serial": "E1A1234567",
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


class TestOctopus:
    """Tests for Octopus class."""

    def test_get_data_returns_consumption_and_sets_influx_header(self, sample_settings):
        """get_data returns consumption_kwh from latest reading and sets influx_header."""
        settings = _octopus_settings(sample_settings)
        consumption_response = {"results": [{"consumption": 0.123}]}
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = Octopus(source="octopus")
            with patch("toinflux.octopus.requests.get", side_effect=_mock_get([consumption_response])):
                result = handler.get_data()
                assert result["consumption_kwh"] == 0.123
                assert handler.influx_header == "octopus,source=octopus_energy "
                assert handler.data == result

    def test_get_data_includes_gas_consumption_when_gas_meter_configured(self, sample_settings):
        """get_data adds gas_consumption when gas_mprn and gas_meter_serial are set."""
        settings = _octopus_settings(sample_settings)
        settings["octopus"]["gas_mprn"] = "9876543210"
        settings["octopus"]["gas_meter_serial"] = "G4F1234567"
        elec_response = {"results": [{"consumption": 0.123}]}
        gas_response = {"results": [{"consumption": 1.5}]}
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = Octopus(source="octopus")
            with patch(
                "toinflux.octopus.requests.get", side_effect=_mock_get([elec_response, gas_response])
            ) as mock_get:
                result = handler.get_data()
                assert result["consumption_kwh"] == 0.123
                assert result["gas_consumption"] == 1.5
                gas_url = mock_get.call_args_list[1][0][0]
                assert "gas-meter-points/9876543210/meters/G4F1234567/consumption/" in gas_url

    def test_get_data_skips_gas_consumption_when_not_configured(self, sample_settings):
        """get_data omits gas_consumption when gas_mprn/gas_meter_serial are absent."""
        settings = _octopus_settings(sample_settings)
        consumption_response = {"results": [{"consumption": 0.123}]}
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = Octopus(source="octopus")
            with patch("toinflux.octopus.requests.get", side_effect=_mock_get([consumption_response])) as mock_get:
                result = handler.get_data()
                assert "gas_consumption" not in result
                assert mock_get.call_count == 1

    def test_get_data_handles_empty_gas_consumption_results(self, sample_settings):
        """get_data omits gas_consumption when the API returns no results for the gas meter."""
        settings = _octopus_settings(sample_settings)
        settings["octopus"]["gas_mprn"] = "9876543210"
        settings["octopus"]["gas_meter_serial"] = "G4F1234567"
        elec_response = {"results": [{"consumption": 0.123}]}
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = Octopus(source="octopus")
            with patch("toinflux.octopus.requests.get", side_effect=_mock_get([elec_response, {"results": []}])):
                result = handler.get_data()
                assert "gas_consumption" not in result

    def test_get_data_includes_unit_rate_when_tariff_configured(self, sample_settings):
        """get_data adds unit_rate_p_per_kwh when product_code and tariff_code are set."""
        settings = _octopus_settings(sample_settings)
        settings["octopus"]["product_code"] = "AGILE-FLEX-22-11-25"
        settings["octopus"]["tariff_code"] = "E-1R-AGILE-FLEX-22-11-25-C"
        consumption_response = {"results": [{"consumption": 0.25}]}
        rate_response = {"results": [{"value_inc_vat": 18.5}]}
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = Octopus(source="octopus")
            with patch("toinflux.octopus.requests.get", side_effect=_mock_get([consumption_response, rate_response])):
                result = handler.get_data()
                assert result["consumption_kwh"] == 0.25
                assert result["unit_rate_p_per_kwh"] == 18.5

    def test_get_data_skips_unit_rate_when_no_tariff_configured(self, sample_settings):
        """get_data omits unit_rate_p_per_kwh when product_code or tariff_code is absent."""
        settings = _octopus_settings(sample_settings)
        consumption_response = {"results": [{"consumption": 0.1}]}
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = Octopus(source="octopus")
            with patch("toinflux.octopus.requests.get", side_effect=_mock_get([consumption_response])):
                result = handler.get_data()
                assert "unit_rate_p_per_kwh" not in result

    def test_get_data_handles_empty_consumption_results(self, sample_settings):
        """get_data returns empty dict when API returns no consumption results."""
        settings = _octopus_settings(sample_settings)
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = Octopus(source="octopus")
            with patch("toinflux.octopus.requests.get", side_effect=_mock_get([{"results": []}])):
                result = handler.get_data()
                assert result == {}

    def test_get_data_exits_on_request_exception(self, sample_settings):
        """get_data exits with code 2 when the HTTP request fails."""
        settings = _octopus_settings(sample_settings)
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = Octopus(source="octopus")
            with patch(
                "toinflux.octopus.requests.get",
                side_effect=requests.exceptions.ConnectionError("refused"),
            ):
                with pytest.raises(SystemExit) as exc_info:
                    handler.get_data()
                assert exc_info.value.code == 2

    def test_get_data_uses_api_key_for_auth(self, sample_settings):
        """get_data authenticates with api_key as basic auth username."""
        settings = _octopus_settings(sample_settings)
        consumption_response = {"results": [{"consumption": 0.5}]}
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = Octopus(source="octopus")
            with patch("toinflux.octopus.requests.get", side_effect=_mock_get([consumption_response])) as mock_get:
                handler.get_data()
                auth_used = mock_get.call_args[1]["auth"]
                assert auth_used == ("test_api_key", "")
