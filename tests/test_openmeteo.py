"""Unit tests for toinflux.openmeteo (OpenMeteo)."""

from unittest.mock import MagicMock, patch
import pytest
import requests
from toinflux.openmeteo import OpenMeteo
from toinflux.exceptions import SourceConnectionError


def _openmeteo_settings(base):
    """Build minimal settings dict for OpenMeteo tests."""
    settings = {**base}
    settings["openmeteo"] = {
        "db": "weather_db",
        "interval": 900,
        "latitude": 51.5,
        "longitude": -0.1,
        "fields": ["temperature_2m", "relative_humidity_2m"],
    }
    return settings


class TestOpenMeteo:
    """Tests for OpenMeteo class."""

    def test_get_data_returns_configured_fields_and_sets_influx_header(self, sample_settings):
        """get_data returns only configured fields and sets the correct influx_header."""
        settings = _openmeteo_settings(sample_settings)
        api_response = {
            "current": {
                "temperature_2m": 18.5,
                "relative_humidity_2m": 72,
                "precipitation": 0.0,
            }
        }
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = OpenMeteo(source="openmeteo")
            mock_resp = MagicMock()
            mock_resp.json.return_value = api_response
            with patch.object(handler.session, "get", return_value=mock_resp):
                result = handler.get_data()
                assert result["temperature_2m"] == 18.5
                assert result["relative_humidity_2m"] == 72
                assert "precipitation" not in result
                assert handler.influx_header == "weather,source=open-meteo "
                assert handler.data == result

    def test_get_data_skips_fields_absent_from_api_response(self, sample_settings):
        """get_data silently drops fields that are absent from the API response."""
        settings = _openmeteo_settings(sample_settings)
        settings["openmeteo"]["fields"] = ["temperature_2m", "nonexistent_field"]
        api_response = {"current": {"temperature_2m": 15.0}}
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = OpenMeteo(source="openmeteo")
            mock_resp = MagicMock()
            mock_resp.json.return_value = api_response
            with patch.object(handler.session, "get", return_value=mock_resp):
                result = handler.get_data()
                assert result == {"temperature_2m": 15.0}
                assert "nonexistent_field" not in result

    def test_get_data_passes_lat_lon_and_fields_as_params(self, sample_settings):
        """get_data sends latitude, longitude, and joined fields to the API."""
        settings = _openmeteo_settings(sample_settings)
        api_response = {"current": {"temperature_2m": 10.0, "relative_humidity_2m": 80}}
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = OpenMeteo(source="openmeteo")
            mock_resp = MagicMock()
            mock_resp.json.return_value = api_response
            with patch.object(handler.session, "get", return_value=mock_resp) as mock_get:
                handler.get_data()
                call_kwargs = mock_get.call_args[1]
                params = call_kwargs["params"]
                assert params["latitude"] == 51.5
                assert params["longitude"] == -0.1
                assert params["current"] == "temperature_2m,relative_humidity_2m"
                assert params["timezone"] == "auto"

    def test_get_data_raises_source_connection_error_on_request_exception(self, sample_settings):
        """get_data raises SourceConnectionError when the HTTP request fails."""
        settings = _openmeteo_settings(sample_settings)
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = OpenMeteo(source="openmeteo")
            with patch.object(
                handler.session,
                "get",
                side_effect=requests.exceptions.ConnectionError("timeout"),
            ):
                with pytest.raises(SourceConnectionError):
                    handler.get_data()


# The forecast mocks below follow responses recorded from the live API on 2026-11-24:
# GET /v1/forecast?hourly=temperature_2m,dew_point_2m,direct_radiation&forecast_hours=5&timeformat=unixtime
# answered {"hourly": {"time": [1795532400, 1795536000, ...], "temperature_2m": [9.7, 9.3, ...], ...}}
# with the series starting at the current hour, and a field it has no hourly value for was
# refused with HTTP 400 and {"reason": "Cannot initialize ... from invalid String value bogus_x",
# "error": true}. Re-record them if the API moves.

_HOUR = 3600
_NOW = 1795535567  # 15:52:47 UTC, inside the hour that starts at 1795532400
_HOURLY = {
    "time": [1795532400 + n * _HOUR for n in range(28)],
    "temperature_2m": [9.7 - n * 0.1 for n in range(28)],
    "relative_humidity_2m": [80 + n for n in range(28)],
}


def _forecasting_handler(sample_settings, leads=(3, 24)):
    """Build an OpenMeteo handler with forecasting switched on."""
    settings = _openmeteo_settings(sample_settings)
    settings["openmeteo"]["forecast_hours"] = list(leads)
    with patch("toinflux.influx.load_settings", return_value=settings):
        return OpenMeteo(source="openmeteo")


def _response(body, status=200):
    """A mock response with a JSON body, raising for status like requests does."""
    resp = MagicMock()
    resp.json.return_value = body
    if status >= 400:
        error = requests.exceptions.HTTPError(f"{status} Client Error: Bad Request for url: https://api.open-meteo.com")
        error.response = resp
        resp.raise_for_status.side_effect = error
    return resp


class TestOpenMeteoForecast:
    """The forecast is a request of its own, stamped at the slot each lead time lands on."""

    def test_switched_off_it_asks_for_nothing(self, sample_settings):
        """No forecast_hours, no request: an install that predates forecasts is unchanged."""
        settings = _openmeteo_settings(sample_settings)
        with patch("toinflux.influx.load_settings", return_value=settings):
            handler = OpenMeteo(source="openmeteo")
        with patch.object(handler.session, "get") as get:
            assert handler.get_forecast() == []
        get.assert_not_called()

    def test_it_asks_for_the_configured_fields_hourly_in_epoch_seconds(self, sample_settings):
        handler = _forecasting_handler(sample_settings)
        with patch.object(handler.session, "get", return_value=_response({"hourly": _HOURLY})) as get:
            with patch("toinflux.openmeteo.time.time", return_value=_NOW):
                handler.get_forecast()
        params = get.call_args[1]["params"]
        assert params["hourly"] == "temperature_2m,relative_humidity_2m"
        assert params["timeformat"] == "unixtime"
        # Counted from the start of the current hour, so the slot after the 24-hour lead is in it.
        assert params["forecast_hours"] == 26
        assert (params["latitude"], params["longitude"]) == (51.5, -0.1)
        assert "current" not in params

    def test_each_lead_is_stamped_at_the_first_slot_at_or_after_it(self, sample_settings):
        """15:52 plus 3 hours is 18:52, so the 3-hour forecast is stamped at 19:00 - the last
        value a fetch made at least three hours ahead of 19:00 will leave there."""
        handler = _forecasting_handler(sample_settings)
        with patch.object(handler.session, "get", return_value=_response({"hourly": _HOURLY})):
            with patch("toinflux.openmeteo.time.time", return_value=_NOW):
                points = handler.get_forecast()
        nineteen, sixteen_tomorrow = 1795532400 + 4 * _HOUR, 1795532400 + 25 * _HOUR
        assert [timestamp for timestamp, _ in points] == [nineteen, sixteen_tomorrow]
        assert points[0][1] == {
            "temperature_2m_forecast_3h": pytest.approx(9.3),
            "relative_humidity_2m_forecast_3h": 84,
        }
        assert set(points[1][1]) == {"temperature_2m_forecast_24h", "relative_humidity_2m_forecast_24h"}

    def test_a_null_value_is_left_out_rather_than_written_as_none(self, sample_settings):
        hourly = {**_HOURLY, "relative_humidity_2m": [None] * 28}
        handler = _forecasting_handler(sample_settings, leads=(3,))
        with patch.object(handler.session, "get", return_value=_response({"hourly": hourly})):
            with patch("toinflux.openmeteo.time.time", return_value=_NOW):
                ((_, fields),) = handler.get_forecast()
        assert fields == {"temperature_2m_forecast_3h": pytest.approx(9.3)}

    def test_a_lead_the_series_does_not_reach_is_left_out(self, sample_settings):
        hourly = {key: values[:5] for key, values in _HOURLY.items()}
        handler = _forecasting_handler(sample_settings, leads=(3, 24))
        with patch.object(handler.session, "get", return_value=_response({"hourly": hourly})):
            with patch("toinflux.openmeteo.time.time", return_value=_NOW):
                points = handler.get_forecast()
        assert [set(fields) for _, fields in points] == [
            {"temperature_2m_forecast_3h", "relative_humidity_2m_forecast_3h"}
        ]

    def test_a_refusal_carries_the_reason_open_meteo_gave(self, sample_settings):
        """The status line alone says only 400; the body names the field at fault."""
        reason = "Cannot initialize SurfacePressureAndHeightVariable from invalid String value bogus_x"
        handler = _forecasting_handler(sample_settings)
        with patch.object(handler.session, "get", return_value=_response({"reason": reason, "error": True}, 400)):
            with pytest.raises(SourceConnectionError) as raised:
                handler.get_forecast()
        message = str(raised.value)
        assert "refused the forecast request" in message
        assert "['temperature_2m', 'relative_humidity_2m']" in message
        assert reason in message

    def test_an_unreachable_api_says_what_was_being_fetched(self, sample_settings):
        handler = _forecasting_handler(sample_settings)
        with patch.object(handler.session, "get", side_effect=requests.exceptions.ConnectionError("no route")):
            with pytest.raises(SourceConnectionError, match="Could not fetch the Open-Meteo forecast: no route"):
                handler.get_forecast()

    @pytest.mark.parametrize("body", [{}, {"hourly": {}}, {"hourly": {"time": "soon"}}, {"hourly": [1, 2]}])
    def test_an_answer_without_an_hourly_series_is_refused(self, sample_settings, body):
        """A parse that failed raises; an empty list would read as 'nothing to forecast'."""
        handler = _forecasting_handler(sample_settings)
        with patch.object(handler.session, "get", return_value=_response(body)):
            with pytest.raises(SourceConnectionError, match="without an hourly time series"):
                handler.get_forecast()

    def test_current_conditions_do_not_ask_for_the_forecast(self, sample_settings):
        """get_data() is also the live read for controls and MCP; it must stay one request."""
        handler = _forecasting_handler(sample_settings)
        with patch.object(handler.session, "get", return_value=_response({"current": {"temperature_2m": 1}})) as get:
            handler.get_data()
        assert get.call_count == 1
        assert "hourly" not in get.call_args[1]["params"]


def _real_response(body):
    """A real requests.Response with status 200 and this raw body, so decoding is requests' own."""
    response = requests.Response()
    response.status_code = 200
    response._content = body
    return response


class TestAForecastAnswerThatIsNotAForecast:
    """A 200 that is not an hourly forecast must be a SourceConnectionError: that is the only
    failure collect_forecast() tolerates, and anything else reaches the worker and costs the
    collector its interval for a forecast it does not depend on."""

    def test_a_body_that_is_not_json(self, sample_settings):
        handler = _forecasting_handler(sample_settings)
        with patch.object(handler.session, "get", return_value=_real_response(b"<html>proxy</html>")):
            with pytest.raises(SourceConnectionError) as raised:
                handler.get_forecast()
        assert "something that is not JSON" in str(raised.value)
        assert "<html>proxy</html>" in str(raised.value)

    def test_a_json_body_that_is_not_an_object(self, sample_settings):
        handler = _forecasting_handler(sample_settings)
        with patch.object(handler.session, "get", return_value=_real_response(b"[]")):
            with pytest.raises(SourceConnectionError, match=r"without an hourly time series \(got \[\]\)"):
                handler.get_forecast()

    @pytest.mark.parametrize("body", [b"<html>proxy</html>", b"[]"])
    def test_neither_becomes_the_collector_s_failure(self, sample_settings, body):
        import sendtoinflux
        from types import SimpleNamespace

        handler = _forecasting_handler(sample_settings)
        with patch.object(handler.session, "get", return_value=_real_response(body)):
            sendtoinflux.collect_forecast(SimpleNamespace(print=False), handler)
