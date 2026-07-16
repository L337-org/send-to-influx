"""Unit tests for toinflux.influx (DataHandler)."""

from unittest.mock import MagicMock, patch
import requests
import pytest
from toinflux.influx import (
    MAX_POINT_REJECTIONS,
    DataHandler,
    InfluxWriteError,
    _escape_key_or_tag_value,
    _format_field_value,
)
from toinflux.exceptions import ConfigError


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

    def test_init_source_not_in_settings_raises_config_error(self, sample_settings):
        """DataHandler __init__ raises ConfigError when source not in settings."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            with pytest.raises(ConfigError):
                DataHandler(source="unknown_source")

    def test_init_passes_settings_file_through_to_load_settings(self, sample_settings):
        """DataHandler __init__ forwards an explicit settings_file to load_settings()."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            DataHandler(source="hue", settings_file="/etc/send-to-influx/settings.yaml")
            mock_load_settings.assert_called_once_with("/etc/send-to-influx/settings.yaml")

    def test_init_defaults_settings_file_to_none(self, sample_settings):
        """DataHandler __init__ calls load_settings(None) when settings_file is omitted."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            DataHandler(source="hue")
            mock_load_settings.assert_called_once_with(None)

    def test_send_data_uses_instance_data_when_data_is_none(self, sample_settings):
        """send_data uses self.data when data argument is None."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue,host=test "
            h.data = {"temp": 21.5, "light": 100}
            with patch.object(h.session, "post") as mock_post:
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
            with patch.object(h.session, "post") as mock_post:
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
            with patch.object(h.session, "post") as mock_post:
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
            with patch.object(h.session, "post") as mock_post:
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
            with patch.object(h.session, "post") as mock_post:
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
            with patch.object(h.session, "post") as mock_post:
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
            with patch.object(h.session, "post") as mock_post:
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
            with patch.object(h.session, "post") as mock_post:
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
            with patch.object(h.session, "post") as mock_post:
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
            with patch.object(h.session, "post") as mock_post:
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
            with patch.object(h.session, "post") as mock_post:
                mock_post.return_value.raise_for_status = MagicMock()
                h.send_data()
                body = mock_post.call_args[1]["data"]
                assert 'name="a \\"quoted\\" val"' in body
                assert "active=true" in body
                assert "count=3" in body
                assert "count=3i" not in body
                assert "ratio=1.5" in body

    def test_send_data_appends_explicit_timestamp(self, sample_settings):
        """send_data appends an explicitly-passed timestamp to the line protocol body."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue "
            h.data = {"x": 1}
            with patch.object(h.session, "post") as mock_post:
                mock_post.return_value.raise_for_status = MagicMock()
                h.send_data(timestamp=1700000000)
                body = mock_post.call_args[1]["data"]
                assert body == "hue x=1 1700000000"

    def test_send_data_uses_instance_timestamp_when_not_passed(self, sample_settings):
        """send_data falls back to self.timestamp (set by get_data()) when no timestamp arg is given."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue "
            h.data = {"x": 1}
            h.timestamp = 1600000000
            with patch.object(h.session, "post") as mock_post:
                mock_post.return_value.raise_for_status = MagicMock()
                h.send_data()
                body = mock_post.call_args[1]["data"]
                assert body == "hue x=1 1600000000"

    def test_send_data_defaults_timestamp_to_now(self, sample_settings):
        """send_data defaults to the current time when neither timestamp arg nor self.timestamp is set."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue "
            h.data = {"x": 1}
            assert h.timestamp is None
            with (
                patch.object(h.session, "post") as mock_post,
                patch("toinflux.influx.time.time", return_value=1234567890.5),
            ):
                mock_post.return_value.raise_for_status = MagicMock()
                h.send_data()
                body = mock_post.call_args[1]["data"]
                assert body == "hue x=1 1234567890"

    def test_send_data_escapes_field_keys(self, sample_settings):
        """send_data escapes commas, equals signs and spaces in field keys."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue "
            h.data = {"Living Room, Main=Sensor": 1}
            with patch.object(h.session, "post") as mock_post:
                mock_post.return_value.raise_for_status = MagicMock()
                h.send_data(timestamp=1700000000)
                body = mock_post.call_args[1]["data"]
                assert r"Living\ Room\,\ Main\=Sensor=1" in body


class TestSendDataBuffering:
    """Tests for send_data()'s in-memory buffering of failed InfluxDB writes."""

    def test_buffers_point_on_failure_instead_of_dropping_it(self, sample_settings):
        """A failed write is appended to the source's buffer rather than being lost."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue "
            h.data = {"x": 1}
            with patch.object(h.session, "post") as mock_post:
                mock_post.side_effect = requests.exceptions.RequestException("down")
                with pytest.raises(InfluxWriteError):
                    h.send_data(timestamp=1700000000)
            buffer = DataHandler._write_buffers["hue"]
            assert len(buffer) == 1
            assert buffer[0][0] == "hue x=1 1700000000"

    def test_flushes_buffered_point_before_sending_new_one(self, sample_settings):
        """A previously-buffered point is sent (oldest first) once InfluxDB is reachable
        again, before the new point, and the buffer ends up empty."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue "
            h.data = {"x": 1}
            with patch.object(h.session, "post") as mock_post:
                mock_post.side_effect = requests.exceptions.RequestException("down")
                with pytest.raises(InfluxWriteError):
                    h.send_data(timestamp=1700000000)
            assert len(DataHandler._write_buffers["hue"]) == 1

            h.data = {"x": 2}
            with patch.object(h.session, "post") as mock_post:
                mock_post.return_value.raise_for_status = MagicMock()
                h.send_data(timestamp=1700000100)
                assert mock_post.call_count == 2
                first_body = mock_post.call_args_list[0][1]["data"]
                second_body = mock_post.call_args_list[1][1]["data"]
                assert first_body == "hue x=1 1700000000"
                assert second_body == "hue x=2 1700000100"
            assert len(DataHandler._write_buffers["hue"]) == 0

    def test_backlog_flushes_as_newline_batched_body(self, sample_settings):
        """A multi-point backlog is flushed as one newline-joined POST body, not one
        request per point."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue "
            with patch.object(h.session, "post") as mock_post:
                mock_post.side_effect = requests.exceptions.RequestException("down")
                for value in (1, 2, 3):
                    h.data = {"x": value}
                    with pytest.raises(InfluxWriteError):
                        h.send_data(timestamp=1700000000 + value)
            assert len(DataHandler._write_buffers["hue"]) == 3

            h.data = {"x": 4}
            with patch.object(h.session, "post") as mock_post:
                mock_post.return_value.raise_for_status = MagicMock()
                h.send_data(timestamp=1700000004)
                assert mock_post.call_count == 2  # one batched flush + the new point
                flush_body = mock_post.call_args_list[0][1]["data"]
                assert flush_body == "hue x=1 1700000001\nhue x=2 1700000002\nhue x=3 1700000003"
            assert len(DataHandler._write_buffers["hue"]) == 0

    def test_keeps_buffered_points_when_flush_still_fails(self, sample_settings):
        """If InfluxDB is still unreachable, a failed flush leaves already-buffered points
        in place (not dropped) and also buffers the new point, oldest first."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue "
            with patch.object(h.session, "post") as mock_post:
                mock_post.side_effect = requests.exceptions.RequestException("down")
                h.data = {"x": 1}
                with pytest.raises(InfluxWriteError):
                    h.send_data(timestamp=1700000000)
                h.data = {"x": 2}
                with pytest.raises(InfluxWriteError):
                    h.send_data(timestamp=1700000100)
                assert mock_post.call_count == 2

            buffer = DataHandler._write_buffers["hue"]
            assert len(buffer) == 2
            assert buffer[0][0] == "hue x=1 1700000000"
            assert buffer[1][0] == "hue x=2 1700000100"

    def test_buffer_evicts_oldest_point_once_full(self, sample_settings):
        """A source's write buffer drops its oldest point once MAX_BUFFERED_POINTS is exceeded."""
        with (
            patch("toinflux.influx.load_settings") as mock_load_settings,
            patch("toinflux.influx.MAX_BUFFERED_POINTS", 2),
        ):
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue "
            with patch.object(h.session, "post") as mock_post:
                mock_post.side_effect = requests.exceptions.RequestException("down")
                for value in (1, 2, 3):
                    h.data = {"x": value}
                    with pytest.raises(InfluxWriteError):
                        h.send_data(timestamp=1700000000 + value)

            buffer = DataHandler._write_buffers["hue"]
            assert len(buffer) == 2
            assert "x=2" in buffer[0][0]
            assert "x=3" in buffer[1][0]

    def test_different_sources_have_independent_buffers(self, sample_settings):
        """Buffered points for one source don't leak into another source's buffer/flush."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            hue = DataHandler(source="hue")
            hue.influx_header = "hue "
            zappi = DataHandler(source="zappi")
            zappi.influx_header = "zappi "

            with patch.object(hue.session, "post") as mock_post:
                mock_post.side_effect = requests.exceptions.RequestException("down")
                hue.data = {"x": 1}
                with pytest.raises(InfluxWriteError):
                    hue.send_data(timestamp=1700000000)

            with patch.object(zappi.session, "post") as mock_post:
                mock_post.return_value.raise_for_status = MagicMock()
                zappi.data = {"y": 1}
                zappi.send_data(timestamp=1700000000)
                assert mock_post.call_count == 1  # only zappi's own point, no hue backlog

            assert len(DataHandler._write_buffers["hue"]) == 1
            assert "zappi" not in DataHandler._write_buffers or len(DataHandler._write_buffers["zappi"]) == 0

    @staticmethod
    def _http_error(status_code):
        """Build an HTTPError carrying a mock response with the given status code, matching
        what response.raise_for_status() actually attaches in requests."""
        error = requests.exceptions.HTTPError(f"{status_code} error")
        error.response = MagicMock(status_code=status_code)
        return error

    def test_4xx_rejected_new_point_is_still_buffered(self, sample_settings):
        """A 4xx on the new point buffers it like any other failure - a single 4xx might
        be a middlebox answering for a down InfluxDB, so the point isn't trusted-dropped;
        it's given MAX_POINT_REJECTIONS flush attempts before being given up on."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue "
            h.data = {"x": 1}
            with patch.object(h.session, "post") as mock_post:
                mock_post.return_value.raise_for_status.side_effect = self._http_error(400)
                with pytest.raises(InfluxWriteError):
                    h.send_data(timestamp=1700000000)
            assert len(DataHandler._write_buffers["hue"]) == 1

    def test_rejected_point_dropped_after_max_rejections(self, sample_settings):
        """A buffered point the server keeps rejecting (4xx) is dropped once its
        rejection count reaches MAX_POINT_REJECTIONS, unjamming the queue behind it."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue "
            h.data = {"x": 1}
            with patch.object(h.session, "post") as mock_post:
                mock_post.return_value.raise_for_status.side_effect = self._http_error(422)
                with pytest.raises(InfluxWriteError):
                    h.send_data(timestamp=1700000000)
                # Each retry cycle's flush attempt earns the head point one more rejection.
                for _ in range(MAX_POINT_REJECTIONS - 1):
                    h.data = None  # empty-data cycles still flush the backlog
                    with pytest.raises(InfluxWriteError):
                        h.send_data()
                # The cap-reaching rejection drops the point, so this flush completes
                # cleanly and the cycle does not raise.
                h.data = None
                h.send_data()
            # Rejection cap reached: the poison point is gone and a healthy send works.
            assert len(DataHandler._write_buffers["hue"]) == 0
            h.data = {"x": 2}
            with patch.object(h.session, "post") as mock_post:
                mock_post.return_value.raise_for_status = MagicMock()
                h.send_data(timestamp=1700000100)
                assert mock_post.call_count == 1

    def test_connection_failures_never_age_points_out(self, sample_settings):
        """Connection-level failures (no response) don't count towards a point's
        rejection cap - an arbitrarily long outage can't age points out of the buffer."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue "
            h.data = {"x": 1}
            with patch.object(h.session, "post") as mock_post:
                mock_post.side_effect = requests.exceptions.RequestException("down")
                with pytest.raises(InfluxWriteError):
                    h.send_data(timestamp=1700000000)
                for _ in range(20):  # far more cycles than MAX_POINT_REJECTIONS
                    h.data = None
                    with pytest.raises(InfluxWriteError):
                        h.send_data()
            buffer = DataHandler._write_buffers["hue"]
            assert len(buffer) == 1
            assert buffer[0][1] == 0  # rejection count untouched by connection failures

    def test_429_rate_limits_never_age_points_out(self, sample_settings):
        """429 (and 408) are transient conditions, not verdicts on the point - they must
        not count towards the rejection cap, or a rate-limit burst would drop valid data."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue "
            h.data = {"x": 1}
            with patch.object(h.session, "post") as mock_post:
                mock_post.return_value.raise_for_status.side_effect = self._http_error(429)
                with pytest.raises(InfluxWriteError):
                    h.send_data(timestamp=1700000000)
                for _ in range(20):  # far more cycles than MAX_POINT_REJECTIONS
                    h.data = None
                    with pytest.raises(InfluxWriteError):
                        h.send_data()
            buffer = DataHandler._write_buffers["hue"]
            assert len(buffer) == 1
            assert buffer[0][1] == 0  # rejection count untouched by rate limiting

    def test_500_responses_also_never_age_points_out(self, sample_settings):
        """5xx responses are the server's problem, not the point's - they don't count
        towards the rejection cap either."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue "
            h.data = {"x": 1}
            with patch.object(h.session, "post") as mock_post:
                mock_post.return_value.raise_for_status.side_effect = self._http_error(500)
                with pytest.raises(InfluxWriteError):
                    h.send_data(timestamp=1700000000)
                for _ in range(20):
                    h.data = None
                    with pytest.raises(InfluxWriteError):
                        h.send_data()
            buffer = DataHandler._write_buffers["hue"]
            assert len(buffer) == 1
            assert buffer[0][1] == 0

    def test_rejected_chunk_falls_back_to_per_point_isolation(self, sample_settings):
        """When a batched flush is rejected with a 4xx, the offending point is isolated
        per-point (and eventually dropped) while the healthy points still get delivered."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue "
            # Buffer two points behind a connection failure.
            with patch.object(h.session, "post") as mock_post:
                mock_post.side_effect = requests.exceptions.RequestException("down")
                for value in (1, 2):
                    h.data = {"x": value}
                    with pytest.raises(InfluxWriteError):
                        h.send_data(timestamp=1700000000 + value)
            # Pre-poison the first point to one rejection short of the cap, so the next
            # 4xx on it drops it.
            DataHandler._write_buffers["hue"][0][1] = MAX_POINT_REJECTIONS - 1

            # Next send: batch POST 400s; per-point pass 400s the first point (dropping
            # it at the cap) and delivers the second point and the new one.
            with patch.object(h.session, "post") as mock_post:
                bodies = []

                def post_side_effect(*_args, **kwargs):
                    bodies.append(kwargs["data"])
                    response = MagicMock()
                    if "x=1" in kwargs["data"]:
                        response.raise_for_status.side_effect = self._http_error(400)
                    else:
                        response.raise_for_status = MagicMock()
                    return response

                mock_post.side_effect = post_side_effect
                h.data = {"x": 3}
                h.send_data(timestamp=1700000003)

            assert len(DataHandler._write_buffers["hue"]) == 0
            # The second buffered point and the new point were both delivered.
            assert any(body == "hue x=2 1700000002" for body in bodies)
            assert any(body == "hue x=3 1700000003" for body in bodies)

    def test_influx_write_error_status_code_defaults_to_none(self):
        """InfluxWriteError.status_code defaults to None (connection failure, no response)."""
        assert InfluxWriteError("boom").status_code is None

    def test_use_buffer_false_neither_flushes_nor_buffers(self, sample_settings):
        """use_buffer=False (the heartbeat path) posts only its own line: no backlog
        flush first, and no buffering of its line on failure."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue "
            # Seed a backlog.
            h.data = {"x": 1}
            with patch.object(h.session, "post") as mock_post:
                mock_post.side_effect = requests.exceptions.RequestException("down")
                with pytest.raises(InfluxWriteError):
                    h.send_data(timestamp=1700000000)
            assert len(DataHandler._write_buffers["hue"]) == 1

            # A failing use_buffer=False write: exactly one POST (no flush attempt),
            # and the buffer is unchanged (heartbeat line not appended).
            with patch.object(h.session, "post") as mock_post:
                mock_post.side_effect = requests.exceptions.RequestException("still down")
                with pytest.raises(InfluxWriteError):
                    h.send_data(data={"ok": 0}, timestamp=1700000100, use_buffer=False)
                assert mock_post.call_count == 1
            buffer = DataHandler._write_buffers["hue"]
            assert len(buffer) == 1
            assert buffer[0][0] == "hue x=1 1700000000"

    def test_empty_data_still_flushes_backlog(self, sample_settings):
        """A cycle with no data of its own still flushes the backlog, so recovery isn't
        gated on the source's next non-empty reading (or on the heartbeat path)."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue "
            h.data = {"x": 1}
            with patch.object(h.session, "post") as mock_post:
                mock_post.side_effect = requests.exceptions.RequestException("down")
                with pytest.raises(InfluxWriteError):
                    h.send_data(timestamp=1700000000)
            assert len(DataHandler._write_buffers["hue"]) == 1

            h.data = {}
            with patch.object(h.session, "post") as mock_post:
                mock_post.return_value.raise_for_status = MagicMock()
                h.send_data()
                assert mock_post.call_count == 1
                assert mock_post.call_args[1]["data"] == "hue x=1 1700000000"
            assert len(DataHandler._write_buffers["hue"]) == 0

    def test_non_dict_data_warns_explicitly_but_still_flushes_backlog(self, sample_settings, caplog):
        """A truthy non-dict from a handler is a bug worth its own warning - and it must
        not block the backlog flush that an empty-reading cycle would perform."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue "
            h.data = {"x": 1}
            with patch.object(h.session, "post") as mock_post:
                mock_post.side_effect = requests.exceptions.RequestException("down")
                with pytest.raises(InfluxWriteError):
                    h.send_data(timestamp=1700000000)

            h.data = ["not", "a", "dict"]
            with patch.object(h.session, "post") as mock_post:
                mock_post.return_value.raise_for_status = MagicMock()
                with caplog.at_level("WARNING"):
                    h.send_data()
                assert "non-dict data (list)" in caplog.text
                assert mock_post.call_count == 1  # the backlog flush still happened
            assert len(DataHandler._write_buffers["hue"]) == 0

    def test_empty_data_with_empty_buffer_makes_no_request(self, sample_settings):
        """The original early-return behaviour is preserved when there's no backlog."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue "
            h.data = {}
            with patch.object(h.session, "post") as mock_post:
                h.send_data()
                mock_post.assert_not_called()

    def test_identical_line_is_not_buffered_twice(self, sample_settings):
        """Re-collecting the same reading (same fields, same timestamp - e.g. Octopus)
        during an outage doesn't stack duplicate copies in the buffer."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue "
            with patch.object(h.session, "post") as mock_post:
                mock_post.side_effect = requests.exceptions.RequestException("down")
                for _ in range(3):
                    h.data = {"x": 1}
                    with pytest.raises(InfluxWriteError):
                        h.send_data(timestamp=1700000000)
            assert len(DataHandler._write_buffers["hue"]) == 1


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


class TestEscapeKeyOrTagValue:
    """Tests for the _escape_key_or_tag_value line protocol helper."""

    def test_escapes_comma(self):
        assert _escape_key_or_tag_value("a,b") == "a\\,b"

    def test_escapes_equals(self):
        assert _escape_key_or_tag_value("a=b") == "a\\=b"

    def test_escapes_space(self):
        assert _escape_key_or_tag_value("a b") == "a\\ b"

    def test_escapes_backslash(self):
        assert _escape_key_or_tag_value("a\\b") == "a\\\\b"

    def test_leaves_clean_value_untouched(self):
        assert _escape_key_or_tag_value("clean_key") == "clean_key"
