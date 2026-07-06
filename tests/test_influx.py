"""Unit tests for toinflux.influx (DataHandler)."""

from unittest.mock import MagicMock, patch
import requests
import pytest
from toinflux.influx import DataHandler, InfluxWriteError, _format_field_value


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
                assert "light=100i" not in body
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

    def test_send_data_falls_back_to_v1_when_token_is_empty(self, sample_settings):
        """send_data uses v1 user/password auth when token is present but empty."""
        sample_settings["influx"] = {
            "url": "https://influx.example.com:8086",
            "token": "",
            "user": "influx_user",
            "password": "influx_password",
            "timeout": 5,
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
                call_kw = mock_post.call_args[1]
                assert "/api/v2/write" not in url
                assert call_kw["auth"] == ("influx_user", "influx_password")
                assert "headers" not in call_kw

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

    def test_send_data_raises_influx_write_error_on_request_exception(self, sample_settings):
        """send_data raises InfluxWriteError when requests.post raises, so callers can retry/backoff."""

        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue "
            h.data = {"x": 1}
            with patch("toinflux.influx.requests.post") as mock_post:
                mock_post.side_effect = requests.exceptions.RequestException("network error")
                with pytest.raises(InfluxWriteError):
                    h.send_data()

    def test_send_data_raises_influx_write_error_on_bad_status(self, sample_settings):
        """send_data raises InfluxWriteError when InfluxDB returns an error status."""

        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue "
            h.data = {"x": 1}
            with patch("toinflux.influx.requests.post") as mock_post:
                mock_post.return_value.raise_for_status.side_effect = requests.exceptions.HTTPError("500 Server Error")
                with pytest.raises(InfluxWriteError):
                    h.send_data()

    def test_send_data_formats_mixed_field_types_as_line_protocol(self, sample_settings):
        """send_data formats strings/bools/ints/floats per InfluxDB line protocol rules."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue "
            h.data = {"name": 'a "quoted" val', "active": True, "count": 3, "ratio": 1.5}
            with patch("toinflux.influx.requests.post") as mock_post:
                mock_post.return_value.raise_for_status = MagicMock()
                h.send_data()
                body = mock_post.call_args[1]["data"]
                assert 'name="a \\"quoted\\" val"' in body
                assert "active=true" in body
                assert "count=3" in body
                assert "count=3i" not in body
                assert "ratio=1.5" in body


class TestFormatFieldValue:
    """Tests for the _format_field_value line protocol helper."""

    def test_bool_true(self):
        assert _format_field_value(True) == "true"

    def test_bool_false(self):
        assert _format_field_value(False) == "false"

    def test_int_is_unquoted_and_unsuffixed(self):
        """Ints are written as bare numbers (not i-suffixed) to match existing float-typed fields."""
        assert _format_field_value(42) == "42"

    def test_float_is_unquoted_and_unsuffixed(self):
        assert _format_field_value(3.14) == "3.14"

    def test_string_is_quoted(self):
        assert _format_field_value("Charging") == '"Charging"'

    def test_string_escapes_quotes_and_backslashes(self):
        assert _format_field_value('say "hi"\\bye') == '"say \\"hi\\"\\\\bye"'
