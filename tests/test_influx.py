"""Unit tests for toinflux.influx (DataHandler)."""

from unittest.mock import MagicMock, patch
import requests
import pytest
from toinflux import writer as toinflux_writer
from toinflux.influx import (
    DataHandler,
    InfluxWriteError,
    escape_key_or_tag_value,
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
            with patch.object(toinflux_writer.current()._session, "post") as mock_post:
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
            with patch.object(toinflux_writer.current()._session, "post") as mock_post:
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
            with patch.object(toinflux_writer.current()._session, "post") as mock_post:
                mock_post.return_value.raise_for_status = MagicMock()
                h.send_data()
                url = mock_post.call_args[0][0]
                call_kw = mock_post.call_args[1]
                # The whole URL, not just the host somewhere in it - this test's name claims
                # it checks the URL is built correctly, and a substring match would pass on
                # a wrong endpoint, a wrong database or a wrong scheme.
                assert url == "https://influx.example.com:8086/write?db=hue_db&precision=s"
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
            with patch.object(toinflux_writer.current()._session, "post") as mock_post:
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
            with patch.object(toinflux_writer.current()._session, "post") as mock_post:
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
            with patch.object(toinflux_writer.current()._session, "post") as mock_post:
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
            with patch.object(toinflux_writer.current()._session, "post") as mock_post:
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
            with patch.object(toinflux_writer.current()._session, "post") as mock_post:
                mock_post.return_value.raise_for_status = MagicMock()
                h.send_data()
                assert mock_post.call_args[1]["verify"] is False

    def test_an_unreachable_influxdb_is_not_the_caller_s_problem(self, sample_settings):
        """send_data returns rather than raising when InfluxDB cannot be reached: the point is
        committed to the spool, and the collector keeps its interval rather than backing off
        as though its device had failed."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue "
            h.data = {"x": 1}
            with patch.object(toinflux_writer.current()._session, "post") as mock_post:
                mock_post.side_effect = requests.exceptions.RequestException("network error")
                h.send_data()
            assert toinflux_writer.current().pending()

    def test_an_influxdb_error_status_is_not_the_caller_s_problem_either(self, sample_settings):
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue "
            h.data = {"x": 1}
            with patch.object(toinflux_writer.current()._session, "post") as mock_post:
                mock_post.return_value.raise_for_status.side_effect = requests.exceptions.HTTPError("500 Server Error")
                h.send_data()
            assert toinflux_writer.current().pending()

    def test_a_point_that_cannot_be_built_is_the_caller_s_problem(self, sample_settings):
        """A newline cannot be escaped and would split the point in two: that is the caller's
        bug, not an outage, so it is raised where the caller can see it."""
        with patch("toinflux.influx.load_settings", return_value=sample_settings):
            h = DataHandler(source="hue")
        h.influx_header = "hue "
        with pytest.raises(InfluxWriteError, match="newline"):
            h.send_data(data={"bad\nkey": 1})

    def test_send_data_formats_mixed_field_types_as_line_protocol(self, sample_settings):
        """send_data formats strings/bools/ints/floats per InfluxDB line protocol rules."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            h = DataHandler(source="hue")
            h.influx_header = "hue "
            h.data = {"name": 'a "quoted" val', "active": True, "count": 3, "ratio": 1.5}
            with patch.object(toinflux_writer.current()._session, "post") as mock_post:
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
            with patch.object(toinflux_writer.current()._session, "post") as mock_post:
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
            with patch.object(toinflux_writer.current()._session, "post") as mock_post:
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
                patch.object(toinflux_writer.current()._session, "post") as mock_post,
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
            with patch.object(toinflux_writer.current()._session, "post") as mock_post:
                mock_post.return_value.raise_for_status = MagicMock()
                h.send_data(timestamp=1700000000)
                body = mock_post.call_args[1]["data"]
                assert r"Living\ Room\,\ Main\=Sensor=1" in body


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
    """Tests for the escape_key_or_tag_value line protocol helper."""

    def test_escapes_comma(self):
        assert escape_key_or_tag_value("a,b") == "a\\,b"

    def test_escapes_equals(self):
        assert escape_key_or_tag_value("a=b") == "a\\=b"

    def test_escapes_space(self):
        assert escape_key_or_tag_value("a b") == "a\\ b"

    def test_escapes_backslash(self):
        assert escape_key_or_tag_value("a\\b") == "a\\\\b"

    def test_leaves_clean_value_untouched(self):
        assert escape_key_or_tag_value("clean_key") == "clean_key"


class TestWorkerIdentity:
    """A source with several instances runs one worker per instance, and they must be
    distinguishable everywhere that is per-worker rather than per-source.

    Only Hue has instances today (one per bridge). Every other source has
    ``instance=None``, which is what keeps its buffering, logging and heartbeat
    byte-identical to before instances existed.
    """

    def test_single_target_source_has_no_instance(self, sample_settings):
        """The default is None, so nothing changes for the eight single-target sources."""
        with patch("toinflux.influx.load_settings", return_value=sample_settings):
            handler = DataHandler(source="hue")
        assert handler.instance is None
        assert handler.worker_key == ("hue", None)
        assert handler.worker_label == "hue"

    def test_instance_appears_in_key_and_label(self, sample_settings):
        """An instance is carried in the key and shown in the label."""
        with patch("toinflux.influx.load_settings", return_value=sample_settings):
            handler = DataHandler(source="hue", instance="192.168.1.5")
        assert handler.worker_key == ("hue", "192.168.1.5")
        assert handler.worker_label == "hue@192.168.1.5"

    def test_blank_instance_is_not_treated_as_absent(self, sample_settings):
        """Only None means "no instance".

        A blank-but-present instance is a misconfiguration. Rendering it as a bare
        source name would hide that *and* disagree with worker_key, which keeps the
        value verbatim - so the label has to show it rather than swallow it. (The
        enumeration slice rejects a blank bridge host at source; this is the
        belt-and-braces so the two views can never disagree.)
        """
        with patch("toinflux.influx.load_settings", return_value=sample_settings):
            handler = DataHandler(source="hue", instance="")
        assert handler.worker_key == ("hue", "")
        assert handler.worker_label == "hue@"
        assert handler.worker_label != "hue"

    def test_source_is_not_redefined_by_an_instance(self, sample_settings):
        """self.source keeps every one of its other meanings - settings-block lookup,
        get_class name, heartbeat tag, MCP measurement fallback - so an instance must
        not leak into it."""
        with patch("toinflux.influx.load_settings", return_value=sample_settings):
            handler = DataHandler(source="hue", instance="192.168.1.5")
        assert handler.source == "hue"
        assert handler.source_settings == sample_settings["hue"]

    def test_ipv6_instance_survives_in_the_key(self, sample_settings):
        """The key is a tuple precisely so an IPv6 instance can't be mis-split on ':'.

        The label is display-only, so it may contain colons; the key must keep the two
        parts separately addressable, since callers need the block name back out (e.g.
        the stall watchdog reads settings[source]["interval"]).
        """
        with patch("toinflux.influx.load_settings", return_value=sample_settings):
            handler = DataHandler(source="hue", instance="2001:db8::1")
        assert handler.worker_key == ("hue", "2001:db8::1")
        source, instance = handler.worker_key
        assert source == "hue" and instance == "2001:db8::1"

    def test_two_instances_of_one_source_are_spooled_apart(self, sample_settings, influx_posts):
        """Each point carries its own worker's identity into the spool, so a message about
        it - and the memory fallback's per-worker bound - names the right bridge."""
        writer = toinflux_writer.current()
        with patch("toinflux.influx.load_settings", return_value=sample_settings):
            first = DataHandler(source="hue", instance="bridge-a")
            second = DataHandler(source="hue", instance="bridge-b")
        influx_posts.side_effect = requests.exceptions.ConnectionError("down")
        for handler, value in ((first, 1), (second, 2)):
            handler.influx_header = "hue "
            handler.send_data(data={"x": value}, timestamp=1700000000)
        with writer._lock:
            spooled = writer._read_spool()
        assert [(entry.instance, entry.line) for entry in spooled] == [
            ("bridge-a", "hue x=1 1700000000"),
            ("bridge-b", "hue x=2 1700000000"),
        ]


def test_no_source_narrows_the_base_send_data_signature():
    """Every override must accept everything ``DataHandler.send_data()`` accepts.

    Written after the second time this went wrong: a parameter was added to the base for one
    source's benefit, and that source's own override was left on the old signature - so a call
    valid for all nine sources raised TypeError on one. The failure is invisible until a
    generic caller iterates handlers, which is exactly the kind of thing that surfaces in
    production rather than in a unit test of either class.

    Compared as accepted keyword names rather than by calling, so it stays true for parameters
    added later without anyone remembering this test exists.
    """
    import inspect

    from toinflux.general import known_sources, source_class
    from toinflux.influx import DataHandler

    base = inspect.signature(DataHandler.send_data)
    expected = {
        name
        for name, param in base.parameters.items()
        if param.kind in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY)
    }
    narrowed = []
    for source in known_sources():
        override = source_class(source).send_data
        params = inspect.signature(override).parameters
        if any(p.kind is p.VAR_KEYWORD for p in params.values()):
            continue  # **kwargs accepts anything the base does
        missing = expected - set(params)
        if missing:
            narrowed.append(f"{source}: missing {sorted(missing)}")
    assert not narrowed, (
        "these overrides do not accept everything DataHandler.send_data() does, so a generic "
        "caller breaks on them alone:\n  " + "\n  ".join(narrowed)
    )
