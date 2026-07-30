"""Unit tests for toinflux.philipshue (Hue)."""

from unittest.mock import MagicMock, patch
import pytest
import requests

from toinflux.philipshue import Hue, _url_host
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
    """A bridge configured with a bare IPv6 address must be reachable (SI-17)."""

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
        """No token to hide must mean no rewriting - a naive ""-replace would
        splice the marker between every character of the message."""
        hue = self._hue(sample_settings, token=token)
        boom = requests.exceptions.ConnectionError("Max retries exceeded with url: /api/whatever")
        with patch.object(hue.session, "get", side_effect=boom):
            with pytest.raises(SourceConnectionError) as excinfo:
                hue.get_data_from_hue_bridge()
        assert str(excinfo.value) == "Max retries exceeded with url: /api/whatever"
