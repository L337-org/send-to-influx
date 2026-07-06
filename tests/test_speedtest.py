"""Unit tests for toinflux.speedtest (Speedtest)."""

from socket import gethostname
from unittest.mock import MagicMock, patch
import pytest
from toinflux.speedtest import Speedtest
from toinflux.exceptions import ConfigError


class TestSpeedtest:
    """Tests for Speedtest class."""

    def test_init_raises_config_error_when_speedtest_source_missing_in_settings(self, sample_settings):
        """Speedtest init raises ConfigError when speedtest source is not in settings."""
        settings = {k: v for k, v in sample_settings.items() if k != "speedtest"}
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            with patch("toinflux.speedtest.speedtest.Speedtest"):
                with pytest.raises(ConfigError):
                    Speedtest(source="speedtest")

    def test_get_data_runs_speedtest_and_returns_all_fields_without_filter(self, sample_settings):
        """get_data runs download/upload and returns full result when no fields are set."""
        settings = {**sample_settings, "speedtest": {"db": "speedtest_db", "interval": 300}}
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = Speedtest(source="speedtest")

            st_payload = {"download": 123.4, "upload": 56.7, "ping": 10.1}
            mock_st = MagicMock()
            mock_st.results.dict.return_value = st_payload

            with patch("toinflux.speedtest.speedtest.Speedtest", return_value=mock_st):
                result = handler.get_data()

                mock_st.download.assert_called_once()
                mock_st.upload.assert_called_once()
                mock_st.results.dict.assert_called_once()
                assert result == st_payload
                assert handler.data == st_payload
                assert handler.influx_header == f"speedtest,host={gethostname().split('.')[0]} "

    def test_get_data_filters_to_configured_fields(self, sample_settings):
        """get_data keeps only configured fields that exist in speedtest result."""
        settings = {
            **sample_settings,
            "speedtest": {"db": "speedtest_db", "interval": 300, "fields": ["download", "ping", "missing"]},
        }
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = Speedtest(source="speedtest")

            st_payload = {"download": 200.0, "upload": 100.0, "ping": 12.3}
            mock_st = MagicMock()
            mock_st.results.dict.return_value = st_payload

            with patch("toinflux.speedtest.speedtest.Speedtest", return_value=mock_st):
                result = handler.get_data()
                assert result == {"download": 200.0, "ping": 12.3}
                assert "missing" not in result
                assert "upload" not in result

    def test_get_data_returns_empty_dict_when_only_missing_fields_configured(self, sample_settings):
        """get_data returns empty dict when configured fields are absent."""
        settings = {
            **sample_settings,
            "speedtest": {"db": "speedtest_db", "interval": 300, "fields": ["foo", "bar"]},
        }
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = Speedtest(source="speedtest")

            st_payload = {"download": 200.0, "upload": 100.0}
            mock_st = MagicMock()
            mock_st.results.dict.return_value = st_payload

            with patch("toinflux.speedtest.speedtest.Speedtest", return_value=mock_st):
                result = handler.get_data()
                assert result == {}
                assert handler.data == {}
