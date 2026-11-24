"""Unit tests for toinflux.carbonintensity (CarbonIntensity)."""

from unittest.mock import MagicMock, patch
import pytest
import requests
from toinflux.carbonintensity import CarbonIntensity
from toinflux.exceptions import SourceConnectionError


def _ci_settings(base, include_generation=False):
    """Build minimal settings dict for CarbonIntensity tests."""
    settings = {**base}
    settings["carbonintensity"] = {
        "db": "grid_db",
        "interval": 1800,
        "timeout": 10,
        "include_generation": include_generation,
    }
    return settings


def _mock_get(responses):
    """Return a side_effect function that yields successive mock responses."""
    responses_iter = iter(responses)

    def side_effect(*args, **kwargs):
        resp = MagicMock()
        resp.json.return_value = next(responses_iter)
        return resp

    return side_effect


_INTENSITY_RESPONSE = {"data": [{"intensity": {"forecast": 150, "actual": 143, "index": "moderate"}}]}

_GENERATION_RESPONSE = {
    "data": {
        "generationmix": [
            {"fuel": "gas", "perc": 30.1},
            {"fuel": "nuclear", "perc": 18.5},
            {"fuel": "wind", "perc": 35.2},
            {"fuel": "solar", "perc": 8.7},
            {"fuel": "coal", "perc": 0.0},
        ]
    }
}


class TestCarbonIntensity:
    """Tests for CarbonIntensity class."""

    def test_get_data_returns_intensity_and_sets_influx_header(self, sample_settings):
        """get_data returns actual and forecast intensity and sets correct influx_header."""
        settings = _ci_settings(sample_settings)
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = CarbonIntensity(source="carbonintensity")
            with patch.object(handler.session, "get", side_effect=_mock_get([_INTENSITY_RESPONSE])):
                result = handler.get_data()
                assert result["intensity_actual"] == 143
                assert result["intensity_forecast"] == 150
                assert handler.influx_header == "carbonintensity,source=national_grid "
                assert handler.data == result

    def test_get_data_includes_generation_when_configured(self, sample_settings):
        """get_data adds gen_* fields when include_generation is True."""
        settings = _ci_settings(sample_settings, include_generation=True)
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = CarbonIntensity(source="carbonintensity")
            with patch.object(
                handler.session,
                "get",
                side_effect=_mock_get([_INTENSITY_RESPONSE, _GENERATION_RESPONSE]),
            ):
                result = handler.get_data()
                assert result["gen_gas"] == 30.1
                assert result["gen_wind"] == 35.2
                assert result["gen_solar"] == 8.7
                assert result["gen_coal"] == 0.0

    def test_get_data_skips_generation_when_not_configured(self, sample_settings):
        """get_data omits gen_* fields when include_generation is False."""
        settings = _ci_settings(sample_settings, include_generation=False)
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = CarbonIntensity(source="carbonintensity")
            with patch.object(handler.session, "get", side_effect=_mock_get([_INTENSITY_RESPONSE])):
                result = handler.get_data()
                assert not any(k.startswith("gen_") for k in result)

    def test_get_data_handles_null_actual(self, sample_settings):
        """get_data omits intensity_actual when the API returns null for actual."""
        settings = _ci_settings(sample_settings)
        response = {"data": [{"intensity": {"forecast": 200, "actual": None, "index": "high"}}]}
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = CarbonIntensity(source="carbonintensity")
            with patch.object(handler.session, "get", side_effect=_mock_get([response])):
                result = handler.get_data()
                assert "intensity_actual" not in result
                assert result["intensity_forecast"] == 200

    def test_get_data_raises_source_connection_error_on_request_exception(self, sample_settings):
        """get_data raises SourceConnectionError when the HTTP request fails."""
        settings = _ci_settings(sample_settings)
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = CarbonIntensity(source="carbonintensity")
            with patch.object(
                handler.session,
                "get",
                side_effect=requests.exceptions.ConnectionError("timeout"),
            ):
                with pytest.raises(SourceConnectionError):
                    handler.get_data()

    def test_get_data_sends_accept_json_header(self, sample_settings):
        """get_data includes Accept: application/json header in all requests."""
        settings = _ci_settings(sample_settings)
        with patch("toinflux.influx.load_settings") as mock_load_settings:
            mock_load_settings.return_value = settings
            handler = CarbonIntensity(source="carbonintensity")
            with patch.object(handler.session, "get", side_effect=_mock_get([_INTENSITY_RESPONSE])) as mock_get:
                handler.get_data()
                headers_used = mock_get.call_args[1]["headers"]
                assert headers_used.get("Accept") == "application/json"


# Recorded from the live API on 2026-11-24 at 15:49 UTC: GET /intensity/2026-11-24T15:49Z/fw48h
# answered 96 half-hours starting with the one containing `from`, each shaped
# {"from": "2026-11-24T15:30Z", "to": "2026-11-24T16:00Z",
#  "intensity": {"forecast": 164, "actual": null, "index": "moderate"}}.

_NOW = 1795535567  # 2026-11-24T15:52:47Z
_FIRST = 1795534200  # 2026-11-24T15:30Z


def _forward(count=96, forecast=lambda n: 160 + n):
    """A forward-forecast body of half-hours from 15:30 UTC."""
    from datetime import datetime, timezone

    entries = []
    for n in range(count):
        start = datetime.fromtimestamp(_FIRST + n * 1800, tz=timezone.utc)
        end = datetime.fromtimestamp(_FIRST + (n + 1) * 1800, tz=timezone.utc)
        entries.append(
            {
                "from": start.strftime("%Y-%m-%dT%H:%MZ"),
                "to": end.strftime("%Y-%m-%dT%H:%MZ"),
                "intensity": {"forecast": forecast(n), "actual": None, "index": "moderate"},
            }
        )
    return {"data": entries}


def _forecasting(sample_settings, leads=(1, 47)):
    settings = _ci_settings(sample_settings)
    settings["carbonintensity"]["forecast_hours"] = list(leads)
    with patch("toinflux.influx.load_settings", return_value=settings):
        return CarbonIntensity(source="carbonintensity")


class TestCarbonIntensityForecast:
    """The forward forecast, one field per lead time, stamped at the half-hour it is for."""

    def test_switched_off_it_asks_for_nothing(self, sample_settings):
        settings = _ci_settings(sample_settings)
        with patch("toinflux.influx.load_settings", return_value=settings):
            handler = CarbonIntensity(source="carbonintensity")
        with patch.object(handler.session, "get") as get:
            assert handler.get_forecast() == []
        get.assert_not_called()

    def test_it_asks_for_48_hours_forward_from_now(self, sample_settings):
        handler = _forecasting(sample_settings)
        with patch.object(handler.session, "get", side_effect=_mock_get([_forward()])) as get:
            with patch("toinflux.carbonintensity.time.time", return_value=_NOW):
                handler.get_forecast()
        assert get.call_args[0][0] == "https://api.carbonintensity.org.uk/intensity/2026-11-24T15:52Z/fw48h"

    def test_each_lead_is_stamped_at_the_first_half_hour_at_or_after_it(self, sample_settings):
        """15:52 plus one hour is 16:52, so the one-hour forecast is the 17:00 half-hour's."""
        handler = _forecasting(sample_settings)
        with patch.object(handler.session, "get", side_effect=_mock_get([_forward()])):
            with patch("toinflux.carbonintensity.time.time", return_value=_NOW):
                points = handler.get_forecast()
        seventeen = _FIRST + 3 * 1800
        assert points[0] == (seventeen, {"intensity_forecast_1h": 163})
        assert list(points[1][1]) == ["intensity_forecast_47h"]
        assert points[1][0] >= _NOW + 47 * 3600

    def test_a_half_hour_with_no_forecast_is_skipped(self, sample_settings):
        handler = _forecasting(sample_settings, leads=(1,))
        body = _forward(forecast=lambda n: None if n == 3 else 160 + n)
        with patch.object(handler.session, "get", side_effect=_mock_get([body])):
            with patch("toinflux.carbonintensity.time.time", return_value=_NOW):
                ((timestamp, fields),) = handler.get_forecast()
        assert timestamp == _FIRST + 4 * 1800 and fields == {"intensity_forecast_1h": 164}

    def test_an_unreachable_api_says_what_was_being_fetched(self, sample_settings):
        handler = _forecasting(sample_settings)
        with patch.object(handler.session, "get", side_effect=requests.exceptions.ConnectionError("no route")):
            with patch("toinflux.carbonintensity.time.time", return_value=_NOW):
                with pytest.raises(SourceConnectionError) as raised:
                    handler.get_forecast()
        assert "Could not fetch the carbon-intensity forecast from 2026-11-24T15:52Z" in str(raised.value)
        assert "no route" in str(raised.value)

    def test_an_answer_that_is_not_a_list_of_half_hours_is_refused(self, sample_settings):
        handler = _forecasting(sample_settings)
        with patch.object(handler.session, "get", side_effect=_mock_get([{"data": {"oops": 1}}])):
            with pytest.raises(SourceConnectionError, match="without a list of half-hours"):
                handler.get_forecast()

    def test_a_half_hour_that_cannot_be_read_is_named(self, sample_settings):
        handler = _forecasting(sample_settings)
        with patch.object(handler.session, "get", side_effect=_mock_get([{"data": [{"from": "yesterday"}]}])):
            with pytest.raises(SourceConnectionError) as raised:
                handler.get_forecast()
        assert "could not be read from" in str(raised.value) and "yesterday" in str(raised.value)


def _real_response(body):
    """A real requests.Response with status 200 and this raw body, so decoding is requests' own."""
    response = requests.Response()
    response.status_code = 200
    response._content = body
    return response


class TestAForecastAnswerThatIsNotAForecast:
    """A 200 that is not a forward forecast must be a SourceConnectionError, the only failure
    collect_forecast() tolerates."""

    def test_a_body_that_is_not_json(self, sample_settings):
        handler = _forecasting(sample_settings)
        with patch.object(handler.session, "get", return_value=_real_response(b"<html>proxy</html>")):
            with patch("toinflux.carbonintensity.time.time", return_value=_NOW):
                with pytest.raises(SourceConnectionError) as raised:
                    handler.get_forecast()
        assert "Could not fetch the carbon-intensity forecast from 2026-11-24T15:52Z" in str(raised.value)

    def test_a_json_body_that_is_not_an_object(self, sample_settings):
        handler = _forecasting(sample_settings)
        with patch.object(handler.session, "get", return_value=_real_response(b"[]")):
            with pytest.raises(SourceConnectionError, match="without a list of half-hours"):
                handler.get_forecast()

    @pytest.mark.parametrize("body", [b"<html>proxy</html>", b"[]"])
    def test_neither_becomes_the_collector_s_failure(self, sample_settings, body):
        import sendtoinflux
        from types import SimpleNamespace

        handler = _forecasting(sample_settings)
        with patch.object(handler.session, "get", return_value=_real_response(body)):
            sendtoinflux.collect_forecast(SimpleNamespace(print=False), handler)


class TestChoosingTheIntensityFields:
    """fields chooses which intensity fields are collected, both by default, so an install that
    records forecasts can drop intensity_forecast, the half-hour's forecast read after the fact."""

    def test_both_by_default(self, sample_settings):
        with patch("toinflux.influx.load_settings", return_value=_ci_settings(sample_settings)):
            handler = CarbonIntensity(source="carbonintensity")
        with patch.object(handler.session, "get", side_effect=_mock_get([_INTENSITY_RESPONSE])):
            assert handler.get_data() == {"intensity_actual": 143, "intensity_forecast": 150}

    def test_the_actual_alone(self, sample_settings):
        settings = _ci_settings(sample_settings)
        settings["carbonintensity"]["fields"] = ["intensity_actual"]
        with patch("toinflux.influx.load_settings", return_value=settings):
            handler = CarbonIntensity(source="carbonintensity")
        with patch.object(handler.session, "get", side_effect=_mock_get([_INTENSITY_RESPONSE])):
            assert handler.get_data() == {"intensity_actual": 143}

    def test_the_fuel_mix_is_not_affected(self, sample_settings):
        settings = _ci_settings(sample_settings, include_generation=True)
        settings["carbonintensity"]["fields"] = ["intensity_forecast"]
        with patch("toinflux.influx.load_settings", return_value=settings):
            handler = CarbonIntensity(source="carbonintensity")
        with patch.object(handler.session, "get", side_effect=_mock_get([_INTENSITY_RESPONSE, _GENERATION_RESPONSE])):
            data = handler.get_data()
        assert "intensity_actual" not in data and data["intensity_forecast"] == 150
        assert data["gen_gas"] == 30.1

    @pytest.mark.parametrize(
        "fields,expected",
        [
            (None, None),
            (["intensity_actual"], None),
            ("intensity_actual", "must be a non-empty list"),
            ([], "must be a non-empty list"),
            (["intensity_actaul"], "may only name 'intensity_actual', 'intensity_forecast' (got 'intensity_actaul')"),
            (["gen_gas"], "may only name"),
            (["intensity_actual", "intensity_actual"], "must not repeat a field"),
        ],
    )
    def test_settings_problems(self, fields, expected):
        section = {} if fields is None else {"fields": fields}
        problems = CarbonIntensity.settings_problems("carbonintensity", section)
        if expected is None:
            assert problems == []
        else:
            (problem,) = problems
            assert problem.startswith("carbonintensity.fields ") and expected in problem
