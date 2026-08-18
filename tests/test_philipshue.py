"""Unit tests for toinflux.philipshue (Hue)."""

from unittest.mock import MagicMock, patch
import pytest
import requests

from toinflux.philipshue import Hue, _url_host, Bridge, bridge_field_names, enumerate_bridges
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
            # requests' JSONDecodeError is a ValueError *and* a RequestException; use
            # it so the test guards the except ordering (parse handler before transport).
            mock_response.json.side_effect = requests.exceptions.JSONDecodeError("Expecting value", "", 0)
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


class TestUrlHost:
    """Tests for _url_host - bracketing a bare IPv6 literal for use in a URL."""

    @pytest.mark.parametrize(
        "host, expected",
        [
            # A bare IPv6 literal must be bracketed, or everything from its first
            # colon parses as a port and every request fails.
            ("2001:db8::1", "[2001:db8::1]"),
            ("fe80::1", "[fe80::1]"),
            ("::1", "[::1]"),
            # Fully-expanded form is still IPv6, and is bracketed as written -
            # never rewritten to the compressed form.
            ("2001:0db8:0000:0000:0000:0000:0000:0001", "[2001:0db8:0000:0000:0000:0000:0000:0001]"),
            # Idempotent: a host the user already bracketed is left alone rather
            # than double-bracketed.
            ("[2001:db8::1]", "[2001:db8::1]"),
            # Unaffected: IPv4 literals and hostnames.
            ("192.168.1.2", "192.168.1.2"),
            ("hue.example.com", "hue.example.com"),
            ("hue", "hue"),
            # Not second-guessed: a hand-written host:port keeps working exactly
            # as it does today (it is not a parseable address, so it passes through).
            ("hue.example.com:8443", "hue.example.com:8443"),
        ],
    )
    def test_url_host(self, host, expected):
        """_url_host brackets a bare IPv6 literal and leaves everything else alone."""
        assert _url_host(host) == expected

    def test_url_host_strips_surrounding_whitespace(self):
        """_url_host tolerates stray whitespace around a configured value."""
        assert _url_host("  2001:db8::1  ") == "[2001:db8::1]"

    def test_url_host_passes_through_a_non_string(self):
        """A YAML-coerced non-string (e.g. `host: 10.0` is a float) must not raise."""
        assert _url_host(10.0) == "10.0"


class TestHueIpv6Host:
    """A bridge configured with a bare IPv6 address must be reachable."""

    @staticmethod
    def _hue_with_host(sample_settings, host):
        settings = {**sample_settings, "hue": {**sample_settings["hue"], "host": host}}
        with patch("toinflux.influx.load_settings", return_value=settings):
            return Hue(source="hue")

    def test_read_path_brackets_a_bare_ipv6_host(self, sample_settings):
        """get_data_from_hue_bridge requests a bracketed URL for a bare IPv6 host."""
        hue = self._hue_with_host(sample_settings, "2001:db8::1")
        mock_response = MagicMock()
        mock_response.json.return_value = {"sensors": {}, "lights": {}}
        with patch.object(hue.session, "get", return_value=mock_response) as mock_get:
            hue.get_data_from_hue_bridge()
            assert mock_get.call_args[0][0] == f"https://[2001:db8::1]/api/{sample_settings['hue']['user']}"

    def test_read_path_leaves_a_hostname_alone(self, sample_settings):
        """The URL for a hostname is unchanged - no brackets, no rewriting."""
        hue = self._hue_with_host(sample_settings, "hue.example.com")
        mock_response = MagicMock()
        mock_response.json.return_value = {"sensors": {}, "lights": {}}
        with patch.object(hue.session, "get", return_value=mock_response) as mock_get:
            hue.get_data_from_hue_bridge()
            assert mock_get.call_args[0][0] == f"https://hue.example.com/api/{sample_settings['hue']['user']}"

    def test_read_path_does_not_double_bracket(self, sample_settings):
        """A host the user already bracketed is requested as-is."""
        hue = self._hue_with_host(sample_settings, "[2001:db8::1]")
        mock_response = MagicMock()
        mock_response.json.return_value = {"sensors": {}, "lights": {}}
        with patch.object(hue.session, "get", return_value=mock_response) as mock_get:
            hue.get_data_from_hue_bridge()
            assert mock_get.call_args[0][0].startswith("https://[2001:db8::1]/api/")

    def test_influx_host_tag_is_the_configured_value_not_the_url_form(self, sample_settings):
        """The InfluxDB host tag keeps the configured value verbatim.

        Bracketing is a URL concern only: normalising the tag would change the
        series identity for anyone already running an IPv6 bridge.
        """
        hue = self._hue_with_host(sample_settings, "2001:db8::1")
        with patch.object(Hue, "parse_hue_data", return_value={}):
            hue.get_data()
        assert hue.influx_header == "hue,host=2001:db8::1 "


class TestHueTokenRedaction:
    """The bridge token must never reach the log or a raised error.

    The CLIP v1 API carries the token in the URL path and requests puts the URL
    into its exception messages, so without redaction one unreachable bridge
    writes the token to the journal/logfile, again via the worker's own "Source
    failed" line, and out to any connected MCP client (a SourceConnectionError
    from a read/write tool is returned to the caller).
    """

    TOKEN = "SUPERSECRETHUETOKEN123"

    def _hue(self, sample_settings, token=None):
        hue_cfg = {**sample_settings["hue"], "host": "hue.example.com"}
        if token is None:
            hue_cfg["user"] = self.TOKEN
        else:
            hue_cfg["user"] = token
        settings = {**sample_settings, "hue": hue_cfg}
        with patch("toinflux.influx.load_settings", return_value=settings):
            return Hue(source="hue")

    def test_connection_error_redacts_the_token_from_log_and_exception(self, sample_settings, caplog):
        """A connection failure must not put the token in the log or the error."""
        hue = self._hue(sample_settings)
        # The shape requests actually produces - the URL, and therefore the token,
        # is inside the message (verified against requests, not assumed).
        boom = requests.exceptions.ConnectionError(
            f"HTTPSConnectionPool(host='bridge-under-test', port=443): Max retries "
            f"exceeded with url: /api/{self.TOKEN} (Caused by ConnectTimeoutError())"
        )
        with patch.object(hue.session, "get", side_effect=boom):
            with caplog.at_level("ERROR"):
                with pytest.raises(SourceConnectionError) as excinfo:
                    hue.get_data_from_hue_bridge()

        assert self.TOKEN not in caplog.text
        assert "<redacted>" in caplog.text
        # Asserted by equality rather than substring: this pins both halves of the
        # contract at once - the token is gone, and every other byte (host, port,
        # underlying cause) survives, so the failure is still diagnosable.
        assert str(excinfo.value) == (
            "HTTPSConnectionPool(host='bridge-under-test', port=443): Max retries "
            "exceeded with url: /api/<redacted> (Caused by ConnectTimeoutError())"
        )

    def test_unparseable_response_redacts_the_token(self, sample_settings):
        """The JSON-decode path is redacted too, not just the transport one."""
        hue = self._hue(sample_settings)
        mock_response = MagicMock()
        mock_response.json.side_effect = ValueError(f"Expecting value for url /api/{self.TOKEN}")
        with patch.object(hue.session, "get", return_value=mock_response):
            with pytest.raises(SourceConnectionError) as excinfo:
                hue.get_data_from_hue_bridge()
        assert self.TOKEN not in str(excinfo.value)

    def test_every_occurrence_is_replaced(self, sample_settings):
        """A message repeating the token (URL plus 'Caused by' clause) is fully cleaned."""
        hue = self._hue(sample_settings)
        boom = requests.exceptions.ConnectionError(f"url: /api/{self.TOKEN} (Caused by /api/{self.TOKEN})")
        with patch.object(hue.session, "get", side_effect=boom):
            with pytest.raises(SourceConnectionError) as excinfo:
                hue.get_data_from_hue_bridge()
        assert self.TOKEN not in str(excinfo.value)
        assert str(excinfo.value).count("<redacted>") == 2

    @pytest.mark.parametrize("token", ["", None, 12345])
    def test_absent_or_non_string_token_leaves_the_message_intact(self, sample_settings, token):
        """No token to hide must mean no rewriting - a naive ""-replace would splice the
        marker between every character of the message.

        Exercised directly rather than through a request, because a bridge with no usable
        token no longer reaches the request path at all - bridge() raises ConfigError
        first (see test_host_without_a_token_is_a_config_error_not_a_retry).
        """
        hue = self._hue(sample_settings, token=token)
        assert hue._redact("Max retries exceeded with url: /api/whatever") == (
            "Max retries exceeded with url: /api/whatever"
        )

    def test_host_without_a_token_is_a_config_error_not_a_retry(self, sample_settings):
        """A bridge whose token is unusable must fail fast, not authenticate forever.

        ConfigError stops that worker permanently (the worker loop does not retry it) while
        leaving every other source running - which is the outcome acceptance criterion 6
        was after, without the fatal-at-load-time behaviour that would have taken the whole
        service down on a fresh install.
        """
        from toinflux.exceptions import ConfigError

        hue = self._hue(sample_settings, token="")
        with pytest.raises(ConfigError) as excinfo:
            hue.get_data_from_hue_bridge()
        # The message must name the real cause. "no Hue bridge is configured" would be
        # wrong here - the host is configured, it is the token that is missing.
        assert "hue.user is not set" in str(excinfo.value)
        assert sample_settings["hue"]["host"] in str(excinfo.value)

    def test_every_configured_bridges_token_is_redacted(self, sample_settings):
        """Redaction covers the whole configured set, not just this worker's own token.

        Enumeration cannot raise, so this stays safe to call from an exception handler, and
        a message can never carry a token merely because it came from a different slot than
        the one expected.
        """
        settings = {
            **sample_settings,
            "hue": {
                **sample_settings["hue"],
                "host": "a.example.com",
                "user": "TOKEN_A",
                "host2": "b.example.com",
                "user2": "TOKEN_B",
            },
        }
        with patch("toinflux.influx.load_settings", return_value=settings):
            hue = Hue(source="hue", instance="a.example.com")
        cleaned = hue._redact("url /api/TOKEN_A failed, and /api/TOKEN_B also failed")
        assert "TOKEN_A" not in cleaned and "TOKEN_B" not in cleaned
        assert cleaned.count("<redacted>") == 2


class TestEnumerateBridges:
    """enumerate_bridges is the single source of truth for "which bridges are
    configured" - shared by validate_settings, the worker spawner and the CLI modes, so
    they cannot disagree about what runs.
    """

    @staticmethod
    def _hue(**fields):
        return {"db": "hue_db", "interval": 300, **fields}

    def test_legacy_single_bridge(self):
        """The unnumbered host/user pair is slot 1 - every existing install."""
        bridges, errors, warnings = enumerate_bridges(self._hue(host="hue.example.com", user="token1"))
        assert errors == [] and warnings == []
        assert bridges == [Bridge(slot=1, host="hue.example.com", user="token1")]

    def test_non_contiguous_slots(self):
        """Slot numbers carry no ordering and need not be contiguous - a vacated slot 2
        must not stop slot 3 being collected, and nothing renumbers."""
        settings = self._hue(host="a.example.com", user="t1", host2="", user2="", host3="c.example.com", user3="t3")
        bridges, errors, warnings = enumerate_bridges(settings)
        assert errors == [] and warnings == []
        assert [(b.slot, b.host) for b in bridges] == [(1, "a.example.com"), (3, "c.example.com")]

    def test_host_without_a_token_warns_and_is_not_collected(self):
        """A warning, not an error: example_settings.yaml's hue: block still ships the
        placeholder token, so enabling hue in sources: without also setting it is
        exactly this state, and raising would stop every other collector too."""
        bridges, errors, warnings = enumerate_bridges(self._hue(host="hue.example.com", user="your_hue_user"))
        assert errors == []
        assert bridges == []
        assert len(warnings) == 1
        assert "hue.user is not set for the bridge at hue.host (hue.example.com)" in warnings[0]

    @pytest.mark.parametrize("token", ["", "   ", None, 12345, "<stored in systemd-creds - run x>"])
    def test_unusable_tokens_warn_rather_than_collect(self, token):
        """Blank, absent, non-string and unsubstituted-sentinel tokens are all unusable.

        The sentinel case matters: settings.yaml says the value lives in the credstore but
        no credential was found, so it must not be handed to the bridge as a doomed login.
        """
        bridges, errors, warnings = enumerate_bridges(self._hue(host="hue.example.com", user=token))
        assert bridges == [] and errors == [] and len(warnings) == 1

    def test_token_without_a_host_is_not_an_error(self):
        """The resting state after `--remove`, which blanks the token and leaves clearing
        the host as a separate step - treating it as fatal would break the documented
        removal procedure. Cosmetic only."""
        settings = self._hue(host="a.example.com", user="t1", host2="", user2="leftover-token")
        bridges, errors, warnings = enumerate_bridges(settings)
        assert errors == [] and warnings == []
        assert [b.slot for b in bridges] == [1]

    def test_no_bridge_at_all_warns(self):
        """Nothing to collect, but still not fatal - other sources must keep running."""
        bridges, errors, warnings = enumerate_bridges(self._hue(host="", user=""))
        assert bridges == [] and errors == []
        assert len(warnings) == 1 and "no Hue bridge is configured" in warnings[0]

    @pytest.mark.parametrize(
        "pair",
        [
            ("2001:db8::1", "2001:0db8:0000:0000:0000:0000:0000:0001"),  # same address, two spellings
            ("[2001:db8::1]", "2001:db8::1"),  # bracketed vs bare
            ("HUE1.local", "hue1.local"),  # hostname case
            ("hue.example.com", "hue.example.com"),  # identical
        ],
    )
    def test_duplicate_hosts_are_an_error(self, pair):
        """Two slots addressing one bridge is self-contradictory, so it IS fatal: both
        workers would collect the same devices and write two series for one device set."""
        first, second = pair
        bridges, errors, _ = enumerate_bridges(self._hue(host=first, user="t1", host2=second, user2="t2"))
        assert len(errors) == 1
        assert "same bridge" in errors[0]
        assert len(bridges) == 2  # still enumerated, so every problem is reportable at once

    def test_distinct_hosts_are_not_flagged(self):
        """Different addresses in the same family must not trip the normaliser."""
        bridges, errors, warnings = enumerate_bridges(
            self._hue(host="2001:db8::1", user="t1", host2="2001:db8::2", user2="t2")
        )
        assert errors == [] and warnings == [] and len(bridges) == 2

    @pytest.mark.parametrize("slot", [10, 11, 19, 42, 99, 100])
    def test_two_digit_and_higher_slots_are_valid(self, slot):
        """There is no cap, so a high slot number must be accepted.

        Regression guard: the canonical-suffix pattern was first written `[2-9]\\d*`,
        which looks right but rejects 10-19 (and 100-199, ...) because they begin with a
        1 - so hue.host10 was refused as malformed. The original tests only covered
        host1/host0/host02 and so never caught it.
        """
        host_field, user_field = bridge_field_names(slot)
        settings = self._hue(host="a.example.com", user="t1", **{host_field: "b.example.com", user_field: "t2"})
        bridges, errors, warnings = enumerate_bridges(settings)
        assert errors == [] and warnings == []
        assert [b.slot for b in bridges] == [1, slot]

    def test_slots_are_processed_in_numeric_order(self):
        """Slot 10 must come after slot 2, not before it.

        The keys are scanned with sorted(), which is lexicographic - "host10" sorts before
        "host2". Slots are numeric identifiers, so out-of-order processing would make the
        bridge list, the startup log and the "same bridge as hue.hostN" message name an
        arbitrary slot as the earlier one.
        """
        settings = self._hue(
            host="a.example.com",
            user="t1",
            host10="j.example.com",
            user10="t10",
            host2="b.example.com",
            user2="t2",
        )
        bridges, errors, warnings = enumerate_bridges(settings)
        assert errors == [] and warnings == []
        assert [b.slot for b in bridges] == [1, 2, 10]

    def test_duplicate_message_names_the_lower_slot_as_the_original(self):
        """With slots 2 and 10 duplicating, slot 2 is the one already taken."""
        settings = self._hue(
            host="a.example.com", user="t1", host10="dup.example.com", user10="t10", host2="dup.example.com", user2="t2"
        )
        _, errors, _ = enumerate_bridges(settings)
        assert len(errors) == 1
        assert "hue.host10" in errors[0] and "same bridge as hue.host2" in errors[0]

    def test_duplicate_is_caught_even_when_one_slot_has_no_token(self):
        """Duplicate detection covers every slot with a host, not only usable ones.

        A tokenless slot spawns no worker, so nothing is double-collected *yet* - but
        reporting the duplicate only once the token is filled in would surface it long
        after the copy-paste that caused it, at the least expected moment.
        """
        settings = self._hue(host="dup.example.com", user="t1", host2="dup.example.com", user2="")
        _, errors, warnings = enumerate_bridges(settings)
        assert len(errors) == 1 and "same bridge" in errors[0]
        assert len(warnings) == 1  # the missing token is still reported separately

    @pytest.mark.parametrize("field", ["user1", "user0", "user02"])
    def test_non_canonical_user_fields_are_also_an_error(self, field):
        """Both halves of a slot are validated, not just the host.

        A mistyped `user02` would otherwise be silently ignored: slot 2 would report its
        token as unset while the token sat in a key nothing reads.
        """
        settings = self._hue(host="a.example.com", user="t1", **{field: "sometoken"})
        _, errors, _ = enumerate_bridges(settings)
        assert len(errors) == 1 and f"hue.{field}" in errors[0]

    def test_token_in_a_slot_whose_host_key_is_absent_is_still_seen(self):
        """A slot is discovered from either half, so a token left behind after the host
        line was deleted outright is still noticed (at DEBUG) rather than invisible."""
        bridges, errors, warnings = enumerate_bridges(self._hue(host="a.example.com", user="t1", user3="orphan"))
        assert errors == [] and warnings == []
        assert [b.slot for b in bridges] == [1]

    def test_mixed_type_yaml_keys_do_not_crash(self):
        """YAML permits non-string mapping keys (`1: x`, `true: y`); a mixed-type key set
        makes a bare sorted() raise TypeError, crashing out of validation rather than
        reporting a clean ConfigError."""
        bridges, errors, warnings = enumerate_bridges({1: "x", 2.5: "y", "host": "a.example.com", "user": "t1"})
        assert errors == [] and warnings == []
        assert [b.host for b in bridges] == ["a.example.com"]

    def test_warning_names_the_field_to_set(self):
        """The message must name the exact field, since that is what the reader has to edit.

        This previously also asserted that `set-credential` was *absent*, because at the time
        that command could not accept a numbered slot, so naming it would have sent the reader
        to a tool that would refuse them. It handles slots now, and the warning carries a
        credential-store caveat when run outside the service - so the original reason is gone.
        What still matters, and is asserted here, is that the field name is present.
        """
        _, _, warnings = enumerate_bridges(self._hue(host="a.example.com", user="t1", host2="b.example.com"))
        assert len(warnings) == 1
        assert "hue.user2" in warnings[0]

    def test_caveat_is_absent_under_the_service(self, monkeypatch):
        """Under systemd the value really was substituted, so an unset token is genuinely
        unset - the credential-store caveat would be misdirection."""
        monkeypatch.setenv("CREDENTIALS_DIRECTORY", "/run/credentials/send-to-influx.service")
        _, _, warnings = enumerate_bridges(self._hue(host="a.example.com", user="t1", host2="b.example.com"))
        assert len(warnings) == 1
        assert "set-credential" not in warnings[0]

    @pytest.mark.parametrize("field", ["host1", "host0", "host02"])
    def test_non_canonical_slot_fields_are_an_error(self, field):
        """host1 would be a second way to spell slot 1, and host02 a second way to spell
        slot 2 - both ambiguous, so they're rejected rather than silently folded in."""
        settings = self._hue(host="a.example.com", user="t1", **{field: "b.example.com"})
        _, errors, _ = enumerate_bridges(settings)
        assert len(errors) == 1
        assert f"hue.{field}" in errors[0]

    def test_non_string_host_is_an_error(self):
        """YAML coerces `host: 10.0` to a float; it cannot address a bridge and must not
        be str()-ed into a doomed URL."""
        _, errors, _ = enumerate_bridges(self._hue(host=10.0, user="t1"))
        assert len(errors) == 1 and "must be a string" in errors[0]

    def test_absent_section_is_left_to_the_source_block_check(self):
        """One cause, one message.

        validate_settings' own per-source check already reports "no configuration section
        found for source 'hue'". Enumerating a missing block on top of that would add
        "must be a mapping (got NoneType)" - true but misleading, since the problem is
        that it isn't there rather than that it's the wrong type.
        """
        from toinflux.general import validate_settings
        from toinflux.exceptions import ConfigError

        settings = {"influx": {"url": "http://x", "token": "t", "org": "o"}, "sources": ["hue"]}
        with pytest.raises(ConfigError) as excinfo:
            validate_settings(settings)
        assert "no configuration section found for source 'hue'" in str(excinfo.value)
        assert "must be a mapping" not in str(excinfo.value)

    def test_non_mapping_block_is_an_error(self):
        """A malformed `hue:` section must fail cleanly rather than raise from .get()."""
        bridges, errors, _ = enumerate_bridges("oops")
        assert bridges == [] and len(errors) == 1 and "must be a mapping" in errors[0]

    def test_unrelated_fields_are_ignored(self):
        """Only host/hostN name a slot - hostname-ish neighbours must not be mistaken for one."""
        settings = self._hue(host="a.example.com", user="t1", hostname="x", insecure=True, temperature_units="C")
        bridges, errors, warnings = enumerate_bridges(settings)
        assert errors == [] and warnings == [] and [b.slot for b in bridges] == [1]

    def test_field_names_are_derived_in_one_place(self):
        """Callers never build f"host{n}" themselves - slot 1 is the unnumbered pair."""
        assert bridge_field_names(1) == ("host", "user")
        assert bridge_field_names(2) == ("host2", "user2")
        assert bridge_field_names(99) == ("host99", "user99")


class TestBridgeResolution:
    """Which bridge a handler collects from, and what it emits for it."""

    @staticmethod
    def _hue(instance=None, **hue_fields):
        settings = {
            "hue": {"db": "hue_db", "interval": 300, "host": "a.example.com", "user": "tok-a", **hue_fields},
            "influx": {"url": "http://influx", "token": "t", "org": "o"},
        }
        with patch("toinflux.influx.load_settings", return_value=settings):
            return Hue(source="hue", instance=instance)

    def test_no_instance_resolves_to_the_first_configured_bridge(self):
        """The single-bridge case, and every caller that doesn't care about instances
        (the MCP tools construct handlers without one) - must behave as it always has."""
        hue = self._hue()
        assert hue.bridge() == Bridge(slot=1, host="a.example.com", user="tok-a")

    def test_instance_selects_its_own_bridge(self):
        """Each worker collects from its own bridge, with that bridge's own token."""
        hue = self._hue(instance="b.example.com", host2="b.example.com", user2="tok-b")
        assert hue.bridge() == Bridge(slot=2, host="b.example.com", user="tok-b")

    def test_url_uses_the_instance_bridges_host_and_token(self):
        """The request URL is built from the resolved bridge, not from slot 1."""
        hue = self._hue(instance="b.example.com", host2="b.example.com", user2="tok-b")
        assert hue._api_base() == "https://b.example.com/api/tok-b"

    def test_header_tags_the_instance_bridges_host(self):
        """Each bridge's points carry its own host tag - which is what keeps field names
        identical to the single-bridge era instead of needing per-bridge prefixes."""
        hue = self._hue(instance="b.example.com", host2="b.example.com", user2="tok-b")
        with patch.object(Hue, "parse_hue_data", return_value={}):
            hue.get_data()
        assert hue.influx_header == "hue,host=b.example.com "

    def test_header_escapes_a_host_containing_line_protocol_specials(self):
        """send_data() escapes field keys but takes the header verbatim, so a host with a
        space/comma/equals would end the tag set early and silently corrupt the point."""
        hue = self._hue(instance="my bridge,x", host2="my bridge,x", user2="tok-b")
        with patch.object(Hue, "parse_hue_data", return_value={}):
            hue.get_data()
        assert hue.influx_header == "hue,host=my\\ bridge\\,x "

    def test_header_does_not_normalise_the_host(self):
        """Escaped, but never rewritten: normalising would change the series identity of
        an install that is already running an IPv6 bridge."""
        hue = self._hue(instance="2001:0db8::1", host2="2001:0db8::1", user2="tok-b")
        with patch.object(Hue, "parse_hue_data", return_value={}):
            hue.get_data()
        assert hue.influx_header == "hue,host=2001:0db8::1 "

    def test_unknown_instance_is_a_config_error(self):
        """A worker outliving a configuration change must stop, not loop against a bridge
        that is no longer configured."""
        from toinflux.exceptions import ConfigError

        hue = self._hue(instance="gone.example.com")
        with pytest.raises(ConfigError) as excinfo:
            hue.bridge()
        assert "no Hue bridge configured at 'gone.example.com'" in str(excinfo.value)
        assert "configured: a.example.com" in str(excinfo.value)

    def test_malformed_block_is_a_config_error(self):
        """Enumeration errors surface as a ConfigError rather than being collected from."""
        from toinflux.exceptions import ConfigError

        hue = self._hue(host2="a.example.com", user2="tok-b")  # duplicate of slot 1
        with pytest.raises(ConfigError) as excinfo:
            hue.bridge()
        assert "same bridge" in str(excinfo.value)


class TestHostNewlineRejected:
    """Swept for the pattern after review raised it against MyEnergi labels rather than
    waiting to be told: the Hue host is written verbatim as the `host` tag, so it had the
    same exposure. A newline cannot appear in a line protocol tag value - it is what
    separates points - so one here would end the point early and turn the remainder into a
    second point nobody configured."""

    def test_a_newline_in_a_bridge_host_is_refused(self):
        from toinflux.philipshue import enumerate_bridges

        evil = "a.example.com" + chr(10) + "hue,host=Injected f=1"
        bridges, errors, _ = enumerate_bridges({"host": evil, "user": "tok"})
        assert bridges == []
        assert "must not contain a newline" in errors[0]

    def test_a_carriage_return_is_refused_too(self):
        from toinflux.philipshue import enumerate_bridges

        bridges, errors, _ = enumerate_bridges({"host": "a" + chr(13) + "b", "user": "tok"})
        assert bridges == []
        assert "must not contain a newline" in errors[0]

    def test_a_normal_host_is_unaffected(self):
        from toinflux.philipshue import enumerate_bridges

        bridges, errors, _ = enumerate_bridges({"host": "a.example.com", "user": "tok"})
        assert errors == []
        assert [bridge.host for bridge in bridges] == ["a.example.com"]
