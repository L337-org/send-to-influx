"""Unit tests for toinflux.myenergi (MyEnergi, Zappi, Eddi, Harvi)."""

import datetime
from unittest.mock import MagicMock, patch
import pytest
import requests
from toinflux.myenergi import MyEnergi, Zappi, Eddi, Harvi
from toinflux.exceptions import ConfigError, SourceConnectionError


def _eddi_settings(base):
    """Build minimal settings dict for Eddi tests."""
    settings = {**base}
    settings["myenergi"] = {
        **base["myenergi"],
        "eddi_url": "https://s18.myenergi.net/cgi-jstatus-E",
    }
    settings["eddi"] = {
        "db": "eddi_db",
        "interval": 300,
        "serial": "67890",
        "fields": ["frq", "div", "che"],
    }
    return settings


def _harvi_settings(base):
    """Build minimal settings dict for Harvi tests."""
    settings = {**base}
    settings["myenergi"] = {
        **base["myenergi"],
        "harvi_url": "https://s18.myenergi.net/cgi-jstatus-H",
    }
    settings["harvi"] = {
        "db": "harvi_db",
        "interval": 300,
        "serial": "99999",
        "fields": ["ectp1", "ectp2"],
    }
    return settings


class TestMyEnergi:
    """Tests for MyEnergi class."""

    def test_get_data_from_myenergi_returns_json_on_200(self, sample_settings):
        """get_data_from_myenergi returns response JSON when status is 200."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            handler = MyEnergi(source="zappi")
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"key": "value"}
            with patch.object(handler.session, "get", return_value=mock_resp):
                result = handler.get_data_from_myenergi("https://example.com/api")
                assert result == {"key": "value"}

    def test_get_data_from_myenergi_raises_on_401(self, sample_settings):
        """get_data_from_myenergi raises SourceConnectionError on 401."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            handler = MyEnergi(source="zappi")
            mock_resp = MagicMock()
            mock_resp.status_code = 401
            with patch.object(handler.session, "get", return_value=mock_resp):
                with pytest.raises(SourceConnectionError):
                    handler.get_data_from_myenergi("https://example.com")

    def test_get_data_from_myenergi_raises_on_other_error_code(self, sample_settings):
        """get_data_from_myenergi raises SourceConnectionError on non-200, non-401 status."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            handler = MyEnergi(source="zappi")
            mock_resp = MagicMock()
            mock_resp.status_code = 500
            with patch.object(handler.session, "get", return_value=mock_resp):
                with pytest.raises(SourceConnectionError):
                    handler.get_data_from_myenergi("https://example.com")

    def test_get_data_from_myenergi_raises_on_request_exception(self, sample_settings):
        """get_data_from_myenergi raises SourceConnectionError when the request itself fails."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            handler = MyEnergi(source="zappi")
            with patch.object(handler.session, "get") as mock_get:
                mock_get.side_effect = requests.exceptions.RequestException("connection failed")
                with pytest.raises(SourceConnectionError):
                    handler.get_data_from_myenergi("https://example.com")

    def test_get_data_from_myenergi_raises_on_invalid_json(self, sample_settings):
        """get_data_from_myenergi raises SourceConnectionError when the response body isn't valid JSON."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            handler = MyEnergi(source="zappi")
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.side_effect = requests.exceptions.JSONDecodeError("bad json", "", 0)
            with patch.object(handler.session, "get", return_value=mock_resp):
                with pytest.raises(SourceConnectionError):
                    handler.get_data_from_myenergi("https://example.com")

    def test_dayhour_results_aggregates_day(self, sample_settings):
        """dayhour_results sums all hours for the day when hour is None."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            handler = MyEnergi(source="zappi")
            serial = handler.source_settings["serial"]
            response_data = {
                f"U{serial}": [
                    {"hr": 0, "h1d": 3600, "imp": 1000, "exp": 0, "gep": 0},
                    {"hr": 1, "h1d": 3600, "imp": 2000, "exp": 0, "gep": 0},
                ],
            }
            with patch.object(handler, "get_data_from_myenergi", return_value=response_data):
                result = handler.dayhour_results("2025", "01", "15", hour=None)
                assert result["Charge"] == round((3600 + 3600) / 3600 / 1000, 4)
                assert result["Import"] == round((1000 + 2000) / 3600 / 1000, 4)
                assert result["Export"] == 0
                assert result["Genera"] == 0

    def test_dayhour_results_single_hour_when_hour_specified(self, sample_settings):
        """dayhour_results returns single hour when hour is specified."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            handler = MyEnergi(source="zappi")
            serial = handler.source_settings["serial"]
            response_data = {
                f"U{serial}": [
                    {"hr": 0, "h1d": 0, "imp": 0, "exp": 0, "gep": 0},
                    {"hr": 2, "h1d": 7200, "imp": 5000, "exp": 100, "gep": 200},
                ],
            }
            with patch.object(handler, "get_data_from_myenergi", return_value=response_data):
                result = handler.dayhour_results("2025", "01", "15", hour=2)
                assert result["Charge"] == round(7200 / 3600 / 1000, 4)
                assert result["Import"] == round(5000 / 3600 / 1000, 4)
                assert result["Export"] == round(100 / 3600 / 1000, 4)
                assert result["Genera"] == round(200 / 3600 / 1000, 4)

    def test_dayhour_results_hour_zero_returns_single_hour_not_whole_day(self, sample_settings):
        """dayhour_results treats hour=0 (midnight) as a specific hour, not 'whole day'."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            handler = MyEnergi(source="zappi")
            serial = handler.source_settings["serial"]
            response_data = {
                f"U{serial}": [
                    {"hr": 0, "h1d": 3600, "imp": 1000, "exp": 0, "gep": 0},
                    {"hr": 1, "h1d": 3600, "imp": 2000, "exp": 0, "gep": 0},
                ],
            }
            with patch.object(handler, "get_data_from_myenergi", return_value=response_data):
                result = handler.dayhour_results("2025", "01", "15", hour=0)
                assert result["Charge"] == round(3600 / 3600 / 1000, 4)
                assert result["Import"] == round(1000 / 3600 / 1000, 4)

    def test_dayhour_results_empty_when_no_serial_key(self, sample_settings):
        """dayhour_results returns zeroed data when response has no U+serial key."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            handler = MyEnergi(source="zappi")
            with patch.object(handler, "get_data_from_myenergi", return_value={}):
                result = handler.dayhour_results("2025", "01", "15")
                assert result["Charge"] == 0
                assert result["Import"] == 0
                assert result["Export"] == 0
                assert result["Genera"] == 0


class TestZappi:
    """Tests for Zappi class."""

    def test_get_data_sets_influx_header_and_returns_parsed_data(self, sample_settings):
        """get_data sets influx_header and returns parse_zappi_data result."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            with patch.object(Zappi, "parse_zappi_data", return_value={"frq": 50, "Charge": 1.5}) as mock_parse:
                zappi = Zappi(source="zappi")
                result = zappi.get_data()
                mock_parse.assert_called_once()
                assert zappi.influx_header == "myenergi,device=zappi "
                assert result == {"frq": 50, "Charge": 1.5}
                assert zappi.data == result

    def test_parse_zappi_data_merges_zappi_and_day_data(self, sample_settings):
        """parse_zappi_data merges API zappi fields with dayhour data."""
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            zappi = Zappi(source="zappi")
            myenergi_data = {"zappi": [{"sno": "12345", "frq": 50, "vol": 240, "gen": 100, "other": "ignored"}]}
            day_data = {"Charge": 1.0, "Import": 2.0, "Export": 0.0, "Genera": 0.5}
            with patch.object(Zappi, "get_data_from_myenergi", return_value=myenergi_data):
                with patch.object(Zappi, "dayhour_results", return_value=day_data):
                    result = zappi.parse_zappi_data()
                    assert result["frq"] == 50
                    assert result["vol"] == 240
                    assert result["gen"] == 100
                    assert result["Charge"] == 1.0
                    assert result["Import"] == 2.0
                    assert "other" not in result

    def test_parse_zappi_data_uses_all_zappi_fields_when_no_fields_setting(self, sample_settings):
        """parse_zappi_data uses full zappi[0] when zappi.fields not in settings."""
        settings = {**sample_settings}
        zappi_cfg = {k: v for k, v in settings["zappi"].items() if k != "fields"}
        settings["zappi"] = zappi_cfg
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            zappi = Zappi(source="zappi")
            myenergi_data = {"zappi": [{"sno": "12345", "frq": 50, "vol": 240, "custom": "yes"}]}
            day_data = {"Charge": 0, "Import": 0, "Export": 0, "Genera": 0}
            with patch.object(Zappi, "get_data_from_myenergi", return_value=myenergi_data):
                with patch.object(Zappi, "dayhour_results", return_value=day_data):
                    result = zappi.parse_zappi_data()
                    assert result["frq"] == 50
                    assert result["vol"] == 240
                    assert result["custom"] == "yes"

    def test_parse_zappi_data_uses_utc_for_dayhour_lookup(self, sample_settings):
        """parse_zappi_data computes the day/hour lookup in UTC, not local time.

        23:30 UTC on 2025-06-30 is 00:30 on 2025-07-01 in a UTC+1 (e.g. BST) local
        timezone - a different day and hour. Using local time here would look up the
        wrong day/hour bucket from the MyEnergi API, which is UTC-keyed.
        """
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = sample_settings
            zappi = Zappi(source="zappi")
            myenergi_data = {"zappi": [{"sno": "12345", "frq": 50}]}
            fixed_utc_now = datetime.datetime(2025, 6, 30, 23, 30, tzinfo=datetime.timezone.utc)
            day_data = {"Charge": 0, "Import": 0, "Export": 0, "Genera": 0}
            with patch.object(Zappi, "get_data_from_myenergi", return_value=myenergi_data):
                with patch("toinflux.myenergi.datetime") as mock_datetime_module:
                    mock_datetime_module.datetime.now.return_value = fixed_utc_now
                    mock_datetime_module.timezone.utc = datetime.timezone.utc
                    with patch.object(Zappi, "dayhour_results", return_value=day_data) as mock_dayhour:
                        zappi.parse_zappi_data()
                        mock_datetime_module.datetime.now.assert_called_once_with(datetime.timezone.utc)
                        mock_dayhour.assert_called_once_with("2025", "06", "30", 23)


class TestEddi:
    """Tests for Eddi class."""

    def test_get_data_sets_influx_header_and_returns_parsed_data(self, sample_settings):
        """get_data sets influx_header and returns parse_eddi_data result."""
        settings = _eddi_settings(sample_settings)
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            with patch.object(Eddi, "parse_eddi_data", return_value={"frq": 50, "div": 100}) as mock_parse:
                eddi = Eddi(source="eddi")
                result = eddi.get_data()
                mock_parse.assert_called_once()
                assert eddi.influx_header == "myenergi,device=eddi "
                assert result == {"frq": 50, "div": 100}
                assert eddi.data == result

    def test_parse_eddi_data_filters_to_configured_fields(self, sample_settings):
        """parse_eddi_data returns only configured fields that exist in API response."""
        settings = _eddi_settings(sample_settings)
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            eddi = Eddi(source="eddi")
            myenergi_data = {
                "eddi": [{"sno": "67890", "frq": 50, "div": 100, "che": 0.5, "sta": 1, "other": "ignored"}]
            }
            with patch.object(Eddi, "get_data_from_myenergi", return_value=myenergi_data):
                result = eddi.parse_eddi_data()
                assert result == {"frq": 50, "div": 100, "che": 0.5}
                assert "sta" not in result
                assert "other" not in result

    def test_parse_eddi_data_uses_all_fields_when_no_fields_setting(self, sample_settings):
        """parse_eddi_data returns full eddi[0] when eddi.fields not configured."""
        settings = _eddi_settings(sample_settings)
        settings["eddi"] = {k: v for k, v in settings["eddi"].items() if k != "fields"}
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            eddi = Eddi(source="eddi")
            myenergi_data = {"eddi": [{"sno": "67890", "frq": 50, "div": 100, "custom": "yes"}]}
            with patch.object(Eddi, "get_data_from_myenergi", return_value=myenergi_data):
                result = eddi.parse_eddi_data()
                assert result == {"sno": "67890", "frq": 50, "div": 100, "custom": "yes"}


class TestHarvi:
    """Tests for Harvi class."""

    def test_get_data_sets_influx_header_and_returns_parsed_data(self, sample_settings):
        """get_data sets influx_header and returns parse_harvi_data result."""
        settings = _harvi_settings(sample_settings)
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            with patch.object(Harvi, "parse_harvi_data", return_value={"ectp1": 500, "ectp2": 0}) as mock_parse:
                harvi = Harvi(source="harvi")
                result = harvi.get_data()
                mock_parse.assert_called_once()
                assert harvi.influx_header == "myenergi,device=harvi "
                assert result == {"ectp1": 500, "ectp2": 0}
                assert harvi.data == result

    def test_parse_harvi_data_filters_to_configured_fields(self, sample_settings):
        """parse_harvi_data returns only configured fields that exist in API response."""
        settings = _harvi_settings(sample_settings)
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            harvi = Harvi(source="harvi")
            myenergi_data = {"harvi": [{"sno": "99999", "ectp1": 500, "ectp2": 0, "ectp3": 200, "ectt1": "Grid"}]}
            with patch.object(Harvi, "get_data_from_myenergi", return_value=myenergi_data):
                result = harvi.parse_harvi_data()
                assert result == {"ectp1": 500, "ectp2": 0}
                assert "ectp3" not in result
                assert "ectt1" not in result

    def test_parse_harvi_data_uses_all_fields_when_no_fields_setting(self, sample_settings):
        """parse_harvi_data returns full harvi[0] when harvi.fields not configured."""
        settings = _harvi_settings(sample_settings)
        settings["harvi"] = {k: v for k, v in settings["harvi"].items() if k != "fields"}
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            harvi = Harvi(source="harvi")
            myenergi_data = {"harvi": [{"sno": "99999", "ectp1": 500, "ectp2": 100, "ectt1": "Grid"}]}
            with patch.object(Harvi, "get_data_from_myenergi", return_value=myenergi_data):
                result = harvi.parse_harvi_data()
                assert result == {"sno": "99999", "ectp1": 500, "ectp2": 100, "ectt1": "Grid"}


class TestDeviceSelection:
    """SI-36. The device was picked out of the API response by a hardcoded index, so a
    second device of the same type was silently ignored and an account with none of that
    type raised IndexError - caught by the worker's broad handler and retried forever
    logging only "list index out of range"."""

    @staticmethod
    def _zappi(settings, serial="12345"):
        settings = {**settings}
        settings["zappi"] = {**settings["zappi"], "serial": serial}
        settings["zappi"].pop("fields", None)
        with patch("toinflux.influx.load_settings", return_value=settings):
            handler = Zappi("zappi")
        handler.session = MagicMock()
        return handler

    def test_selects_the_device_matching_the_configured_serial(self, sample_settings):
        """Acceptance question 1. The serial field is `sno`, verified against the live
        MyEnergi API - it is the only key whose value equals the configured serial."""
        zappi = self._zappi(sample_settings, serial="22222")
        response = {"zappi": [{"sno": "11111", "frq": 49.0}, {"sno": "22222", "frq": 50.0}]}
        with patch.object(Zappi, "get_data_from_myenergi", return_value=response):
            result = zappi._parse_device_data("zappi", "zappi_url")
        assert result["frq"] == 50.0, "picked the wrong device - index 0 rather than the serial"

    def test_a_second_device_of_the_same_type_is_no_longer_ignored(self, sample_settings):
        """The defect in its original form: with two devices, only the first was ever
        collected, whichever serial was configured."""
        response = {"zappi": [{"sno": "first", "frq": 1.0}, {"sno": "second", "frq": 2.0}]}
        for serial, expected in (("first", 1.0), ("second", 2.0)):
            zappi = self._zappi(sample_settings, serial=serial)
            with patch.object(Zappi, "get_data_from_myenergi", return_value=response):
                assert zappi._parse_device_data("zappi", "zappi_url")["frq"] == expected

    def test_serial_is_matched_as_a_string_whatever_yaml_produced(self, sample_settings):
        """An all-digit serial in settings.yaml is an int unless quoted, while the API
        returns whatever it returns - comparing them raw would silently never match and
        look exactly like a wrong serial."""
        zappi = self._zappi(sample_settings, serial=22222)
        response = {"zappi": [{"sno": 11111, "frq": 49.0}, {"sno": "22222", "frq": 50.0}]}
        with patch.object(Zappi, "get_data_from_myenergi", return_value=response):
            assert zappi._parse_device_data("zappi", "zappi_url")["frq"] == 50.0

    def test_no_device_of_that_type_names_the_cause(self, sample_settings):
        """Acceptance question 2. Verified against the live account: an endpoint for a
        device type you do not own answers 200 with an empty list. A transient failure
        rather than fatal, because a device can legitimately be mid-provisioning."""
        zappi = self._zappi(sample_settings)
        with patch.object(Zappi, "get_data_from_myenergi", return_value={"zappi": []}):
            with pytest.raises(SourceConnectionError) as excinfo:
                zappi._parse_device_data("zappi", "zappi_url")
        message = str(excinfo.value)
        assert "list index out of range" not in message
        assert "zappi" in message
        assert "no zappi" in message.lower() or "returned no" in message.lower()

    def test_a_missing_key_is_treated_the_same_as_an_empty_list(self, sample_settings):
        """The account response is not contractually guaranteed to include the key at all,
        and a KeyError would escape the SourceConnectionError/ConfigError split the worker
        loop relies on just as the IndexError did."""
        zappi = self._zappi(sample_settings)
        with patch.object(Zappi, "get_data_from_myenergi", return_value={}):
            with pytest.raises(SourceConnectionError):
                zappi._parse_device_data("zappi", "zappi_url")

    def test_a_serial_matching_nothing_is_fatal_not_retried(self, sample_settings):
        """Acceptance question 3. Devices came back, so the account is reachable and the
        type exists - the configured serial is simply wrong, which no amount of waiting
        fixes. ConfigError is what stops the worker instead of backing off forever."""
        zappi = self._zappi(sample_settings, serial="not-my-serial")
        response = {"zappi": [{"sno": "11111"}, {"sno": "22222"}]}
        with patch.object(Zappi, "get_data_from_myenergi", return_value=response):
            with pytest.raises(ConfigError) as excinfo:
                zappi._parse_device_data("zappi", "zappi_url")
        message = str(excinfo.value)
        assert "not-my-serial" in message
        # Naming what the account does have is the difference between a message you can act
        # on and one that just says no.
        assert "11111" in message and "22222" in message

    def test_the_two_failures_are_different_exception_types(self, sample_settings):
        """The split is the whole point: one is worth retrying and one never is, and the
        worker loop treats ConfigError and SourceConnectionError differently."""
        assert not issubclass(ConfigError, SourceConnectionError)
        assert not issubclass(SourceConnectionError, ConfigError)

    def test_eddi_and_harvi_get_the_same_treatment(self, sample_settings):
        """One shared code path, so a fix that only covered Zappi would be a trap for the
        next reader."""
        for name, factory in (("eddi", _eddi_settings), ("harvi", _harvi_settings)):
            settings = factory(sample_settings)
            cls = Eddi if name == "eddi" else Harvi
            with patch("toinflux.influx.load_settings", return_value=settings):
                handler = cls(name)
            handler.session = MagicMock()
            with patch.object(cls, "get_data_from_myenergi", return_value={name: []}):
                with pytest.raises(SourceConnectionError):
                    handler._parse_device_data(name, f"{name}_url")
