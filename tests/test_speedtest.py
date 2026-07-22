"""Unit tests for toinflux.speedtest (Speedtest)."""

from socket import gethostname
from unittest.mock import MagicMock, patch
import pytest
from toinflux.speedtest import Speedtest
from toinflux.exceptions import ConfigError, SourceConnectionError


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

    def test_get_data_raises_source_connection_error_on_implausible_ping(self, sample_settings):
        """get_data rejects an implausible ping >= 5000ms - the kind of value speedtest-cli
        produces by averaging failed-probe penalties into the result - rather than writing it
        to InfluxDB as a real measurement."""
        settings = {**sample_settings, "speedtest": {"db": "speedtest_db", "interval": 300}}
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = Speedtest(source="speedtest")

            st_payload = {"download": 123.4, "upload": 56.7, "ping": 1800000}
            mock_st = MagicMock()
            mock_st.results.dict.return_value = st_payload

            with patch("toinflux.speedtest.speedtest.Speedtest", return_value=mock_st):
                with pytest.raises(SourceConnectionError):
                    handler.get_data()

    def test_get_data_accepts_ping_just_below_threshold(self, sample_settings):
        """A ping just under the implausibility threshold is treated as a real measurement."""
        settings = {**sample_settings, "speedtest": {"db": "speedtest_db", "interval": 300}}
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = Speedtest(source="speedtest")

            st_payload = {"download": 123.4, "upload": 56.7, "ping": 4999.999}
            mock_st = MagicMock()
            mock_st.results.dict.return_value = st_payload

            with patch("toinflux.speedtest.speedtest.Speedtest", return_value=mock_st):
                result = handler.get_data()
                assert result == st_payload

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


class TestSpeedtestTriggerAndLock:
    """The MCP trigger action and the one-run-at-a-time per-host lock."""

    def _handler(self, sample_settings, mcp_read_write=True):
        settings = {
            **sample_settings,
            "speedtest": {"db": "speedtest_db", "interval": 300, "mcp_read_write": mcp_read_write},
        }
        with patch("toinflux.influx.load_settings", return_value=settings):
            return Speedtest(source="speedtest")

    def _mock_run(self, payload):
        mock_st = MagicMock()
        mock_st.results.dict.return_value = payload
        return patch("toinflux.speedtest.speedtest.Speedtest", return_value=mock_st)

    def test_get_data_rejects_when_a_run_is_in_progress(self, sample_settings):
        # A second run while one holds the lock must not start (it would saturate
        # the link and skew both); it surfaces as a transient error.
        handler = self._handler(sample_settings)
        assert Speedtest._run_lock.acquire(blocking=False)
        try:
            with pytest.raises(SourceConnectionError, match="already in progress"):
                handler.get_data()
        finally:
            Speedtest._run_lock.release()

    def test_get_data_releases_lock_after_a_successful_run(self, sample_settings):
        handler = self._handler(sample_settings)
        with self._mock_run({"download": 1.0, "upload": 2.0, "ping": 3.0}):
            handler.get_data()
        assert Speedtest._run_lock.acquire(blocking=False)  # free again
        Speedtest._run_lock.release()

    def test_get_data_releases_lock_on_failure(self, sample_settings):
        import speedtest as st_mod

        handler = self._handler(sample_settings)
        with patch("toinflux.speedtest.speedtest.Speedtest", side_effect=st_mod.SpeedtestException("boom")):
            with pytest.raises(SourceConnectionError):
                handler.get_data()
        assert Speedtest._run_lock.acquire(blocking=False)  # released despite the failure
        Speedtest._run_lock.release()

    def test_is_writable_and_opt_in(self, sample_settings):
        assert Speedtest.MCP_WRITABLE is True
        assert self._handler(sample_settings, mcp_read_write=True).mcp_write_enabled() is True
        assert self._handler(sample_settings, mcp_read_write=False).mcp_write_enabled() is False

    def test_mcp_trigger_run_runs_records_and_annotates(self, sample_settings):
        handler = self._handler(sample_settings)
        with self._mock_run({"download": 111.0, "upload": 22.0, "ping": 9.0}):
            with patch.object(handler, "send_data") as send:
                result = handler.mcp_trigger_run()
        send.assert_called_once()
        assert result["source"] == "speedtest" and result["recorded"] is True
        assert result["result"]["download"] == {"value": 111.0, "unit": "bits/s"}
        assert result["result"]["ping"] == {"value": 9.0, "unit": "ms"}

    def test_mcp_trigger_run_recording_failure_is_best_effort(self, sample_settings):
        from toinflux.influx import InfluxWriteError

        handler = self._handler(sample_settings)
        with self._mock_run({"download": 1.0, "upload": 2.0, "ping": 3.0}):
            with patch.object(handler, "send_data", side_effect=InfluxWriteError("nope")):
                result = handler.mcp_trigger_run()
        assert result["recorded"] is False
        assert result["result"]["download"]["value"] == 1.0
