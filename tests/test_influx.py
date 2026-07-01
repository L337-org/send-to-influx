"""Unit tests for toinflux.influx (DataHandler)."""

from unittest.mock import MagicMock, patch
import requests
import pytest
from toinflux.influx import DataHandler


class TestDataHandler:
    """Tests for DataHandler class."""

    def test_init_sets_source_and_source_settings(self, sample_settings):
        """DataHandler __init__ sets source and source_settings from settings."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            assert h.settings == sample_settings
            assert h.source == "hue"
            assert h.source_settings == sample_settings["hue"]
            assert h.source_settings["db"] == "hue_db"
            assert h.source_settings["interval"] == 300
            assert h.influx_header is None
            assert h.data is None

    def test_init_source_not_in_settings_exits(self, sample_settings):
        """DataHandler __init__ exits when source not in settings."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            with pytest.raises(SystemExit):
                DataHandler(source="unknown_source")

    def test_send_data_uses_instance_data_when_data_is_none(self, sample_settings):
        """send_data uses self.data when data argument is None."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue,host=test "
            h.data = {"temp": 21.5, "light": 100}
            with patch("toinflux.influx.requests.post") as mock_post:
                mock_post.return_value.raise_for_status = MagicMock()
                h.send_data()
                body = mock_post.call_args[1]["data"]
                assert "temp=21.5" in body
                assert "light=100" in body
                assert body.startswith("hue,host=test ")

    def test_send_data_uses_provided_data(self, sample_settings):
        """send_data uses provided data when given."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue,host=test "
            h.data = {"old": 1}
            with patch("toinflux.influx.requests.post") as mock_post:
                mock_post.return_value.raise_for_status = MagicMock()
                h.send_data(data={"a": 1, "b": 2})
                body = mock_post.call_args[1]["data"]
                assert "a=1" in body
                assert "b=2" in body
                assert "old=1" not in body

    def test_send_data_builds_correct_url_and_auth(self, sample_settings):
        """send_data posts to correct Influx URL with auth."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue "
            h.data = {"x": 1}
            with patch("toinflux.influx.requests.post") as mock_post:
                mock_post.return_value.raise_for_status = MagicMock()
                h.send_data()
                url = mock_post.call_args[0][0]
                call_kw = mock_post.call_args[1]
                assert "influx.example.com" in url
                assert call_kw["auth"] == ("influx_user", "influx_password")

    def test_send_data_v2_uses_token_auth_and_bucket_url(self, sample_settings):
        """send_data uses v2 API endpoint and token header when token is in influx settings."""
        sample_settings["influx"] = {
            "url": "https://influx.example.com:8086",
            "token": "my-token",
            "org": "my-org",
            "timeout": 5,
        }
        sample_settings["hue"]["bucket"] = "hue_bucket"
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue "
            h.data = {"x": 1}
            with patch("toinflux.influx.requests.post") as mock_post:
                mock_post.return_value.raise_for_status = MagicMock()
                h.send_data()
                url = mock_post.call_args[0][0]
                call_kw = mock_post.call_args[1]
                assert "/api/v2/write" in url
                assert "org=my-org" in url
                assert "bucket=hue_bucket" in url
                assert call_kw["headers"] == {"Authorization": "Token my-token"}
                assert "auth" not in call_kw

    def test_send_data_v2_falls_back_to_db_when_no_bucket(self, sample_settings):
        """send_data uses db value as bucket when bucket is not set in source settings."""
        sample_settings["influx"] = {
            "url": "https://influx.example.com:8086",
            "token": "my-token",
            "org": "my-org",
        }
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue "
            h.data = {"x": 1}
            with patch("toinflux.influx.requests.post") as mock_post:
                mock_post.return_value.raise_for_status = MagicMock()
                h.send_data()
                url = mock_post.call_args[0][0]
                assert "bucket=hue_db" in url

    def test_send_data_verifies_tls_by_default(self, sample_settings):
        """send_data passes verify=True to requests.post when insecure is not set."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue "
            h.data = {"x": 1}
            with patch("toinflux.influx.requests.post") as mock_post:
                mock_post.return_value.raise_for_status = MagicMock()
                h.send_data()
                assert mock_post.call_args[1]["verify"] is True

    def test_send_data_skips_tls_verification_when_insecure(self, sample_settings):
        """send_data passes verify=False to requests.post when influx.insecure is true."""
        sample_settings["influx"]["insecure"] = True
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue "
            h.data = {"x": 1}
            with patch("toinflux.influx.requests.post") as mock_post:
                mock_post.return_value.raise_for_status = MagicMock()
                h.send_data()
                assert mock_post.call_args[1]["verify"] is False

    def test_send_data_handles_request_exception(self, sample_settings):
        """send_data does not raise when requests.post raises."""

        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue "
            h.data = {"x": 1}
            with patch("toinflux.influx.requests.post") as mock_post:
                mock_post.side_effect = requests.exceptions.RequestException("network error")
                # should not raise
                h.send_data()
