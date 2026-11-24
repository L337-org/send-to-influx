"""Functions to get Open-Meteo weather data ready to send to InfluxDB."""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT"

import time
import requests
from toinflux.influx import DataHandler, forecast_field_name, forecast_slots
from toinflux.exceptions import SourceConnectionError
from toinflux.general import render_external

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

DEFAULT_FIELDS = ["temperature_2m"]


class OpenMeteo(DataHandler):
    """Child class of DataHandler to get weather data from Open-Meteo.

    Attributes:
        MINIMUM_INTERVAL (int): 600 - a free public API used without a key, and about
            the resolution of the data, so asking faster returns the same numbers.
        DEFAULT_MAX_AGE (int): 3600 - conditions update about every fifteen minutes and
            outdoor dew point moves slowly, so an hour-old figure is still sound.
        FORECAST_MAX_LEAD_HOURS (int): 382 - sixteen days, less the slack the slot search needs.
        FORECAST_HEADER (str): forecasts go to ``weather_forecast``, beside ``weather``.
        MCP_DESCRIPTION (str): what this source advertises to an MCP client.
        MCP_MEASUREMENT (str): "weather" - this source's measurement is not its own name.
        MCP_FIELD_METADATA (dict): units and aggregation kinds for the example-settings
            fields; any other Open-Meteo variable a user configures carries none.
    """

    # A free public API used without a key, so the courtesy owed is real. Ten minutes is
    # also about the resolution of the data: the current-conditions block does not change
    # faster than that, so a shorter interval spends someone else's capacity to receive the
    # same numbers back. The shipped collection interval is 900.
    MINIMUM_INTERVAL = 600
    # Current conditions update roughly every fifteen minutes, and outdoor temperature and
    # dew point move slowly. An hour-old dew point is still a sound basis for a condensation
    # floor, which is the motivating use of this source.
    DEFAULT_MAX_AGE = 3600
    # Open-Meteo forecasts up to 16 days ahead. Two hours short of that, because the slot a
    # lead time is stamped at is the first hourly slot at or after it, so the request must
    # reach one hour past the longest lead, counted from the start of the current hour.
    FORECAST_MAX_LEAD_HOURS = 16 * 24 - 2
    # Its own measurement, beside "weather" rather than in it: see DataHandler.FORECAST_HEADER.
    FORECAST_HEADER = "weather_forecast,source=open-meteo "

    MCP_DESCRIPTION = "Open-Meteo weather: temperature, dew point, humidity, precipitation, cloud, wind and radiation."
    # Writes to the "weather" measurement, not "openmeteo".
    MCP_MEASUREMENT = "weather"
    # Units for the example-settings fields (see UNITS.md); other Open-Meteo
    # variables use that API's own default unit, so only the common ones are
    # annotated here.
    MCP_FIELD_METADATA = {
        "temperature_2m": {"unit": "°C", "kind": "gauge"},
        "dew_point_2m": {
            "unit": "°C",
            "kind": "gauge",
            "description": "The temperature the air must fall to for water to condense out of it.",
        },
        "relative_humidity_2m": {"unit": "%", "kind": "gauge"},
        # An accumulation over the preceding interval, not a reading at an instant,
        # which "mm" alone does not say. The interval is whatever the underlying
        # model uses (the API reports it as `interval` alongside the value - 900 s
        # observed, the hourly series documents an hour), so the description says
        # "interval" rather than naming a duration this collector never records.
        "precipitation": {
            "unit": "mm",
            "kind": "interval",
            "description": "Rain, showers and snow accumulated over the preceding interval, not a rate.",
        },
        "cloud_cover": {"unit": "%", "kind": "gauge"},
        "wind_speed_10m": {"unit": "km/h", "kind": "gauge"},
        # Horizontal plane, per Open-Meteo's own parameter definition - not the
        # normal plane, which is its separate direct_normal_irradiance variable.
        # Worth saying, because a solar-yield comparison wants one or the other and
        # the names alone do not distinguish them.
        "direct_radiation": {
            "unit": "W/m²",
            "kind": "gauge",
            "description": "Direct beam solar radiation on the horizontal plane, excluding diffuse light.",
        },
    }

    def get_data(self):
        """Get current weather observations from Open-Meteo.

        Returns:
            dict: one entry per configured field, taken from the API's ``current`` block.
                A field the API did not report is absent rather than None.

        Raises:
            SourceConnectionError: the API could not be reached, or answered with an error
        """
        fields = self.source_settings.get("fields", DEFAULT_FIELDS)
        params = {
            "latitude": self.source_settings["latitude"],
            "longitude": self.source_settings["longitude"],
            "current": ",".join(fields),
            "timezone": "auto",
        }
        try:
            response = self.session.get(
                OPEN_METEO_URL,
                params=params,
                timeout=self.source_settings.get("timeout", 10),
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            # Raised, not logged as well: every caller reports a failed read itself, and at
            # the level its own situation deserves. See the note in philipshue.py.
            raise SourceConnectionError(render_external(e)) from e

        current = response.json().get("current", {})
        self.data = {k: current[k] for k in fields if k in current}
        self.influx_header = "weather,source=open-meteo "
        return self.data

    @classmethod
    def forecast_fields_for(cls, source_settings):
        """Return the fields forecast: the same selection as current conditions.

        Args:
            source_settings (dict): the ``openmeteo`` settings section

        Returns:
            list: the configured ``fields``, or the default selection
        """
        return list(source_settings.get("fields", DEFAULT_FIELDS))

    def get_forecast(self):
        """Get Open-Meteo's hourly forecast of the configured fields, one value per lead time.

        A request of its own rather than a second block in the current-conditions request, so
        that a field Open-Meteo forecasts no hourly value for, which it answers with a 400 for
        the whole request, cannot cost the collector its current reading.

        Returns:
            list: ``(timestamp, {field: value})`` pairs, one per hourly slot a lead time lands
                on, each field named ``<field>_forecast_<hours>h`` and stamped at the slot's
                start. Empty when ``forecast_hours`` is not set. A value the API left null
                is absent rather than None.

        Raises:
            SourceConnectionError: the API could not be reached, refused the request, or
                answered with something that is not an hourly forecast
        """
        leads = self.forecast_lead_hours()
        if not leads:
            return []
        fields = self.forecast_fields()
        params = {
            "latitude": self.source_settings["latitude"],
            "longitude": self.source_settings["longitude"],
            "hourly": ",".join(fields),
            # Open-Meteo's own parameter, not our setting of the same name: how many hourly
            # slots to return, counted from the start of the current hour, so two more than the
            # longest lead reaches the slot at or after it.
            "forecast_hours": max(leads) + 2,
            # Epoch seconds in UTC, rather than local wall-clock strings that would need the
            # response's offset applied to mean anything.
            "timeformat": "unixtime",
        }
        try:
            response = self.session.get(
                OPEN_METEO_URL,
                params=params,
                timeout=self.source_settings.get("timeout", 10),
            )
            response.raise_for_status()
        except requests.exceptions.HTTPError as e:
            # Open-Meteo says why it refused in the body - "Cannot initialize ... from invalid
            # String value bogus_x" for a field it has no hourly forecast of - which the status
            # line alone does not. That is the one thing an operator needs to fix it.
            raise SourceConnectionError(
                f"Open-Meteo refused the forecast request for fields {fields!r}: "
                f"{render_external(e)}; reason: {render_external(_refusal_reason(e.response))}"
            ) from e
        except requests.exceptions.RequestException as e:
            raise SourceConnectionError(f"Could not fetch the Open-Meteo forecast: {render_external(e)}") from e

        hourly, times = _hourly_series(response)
        slots = forecast_slots(times, time.time(), leads)
        points = {}
        for lead, slot in slots.items():
            index = times.index(slot)
            for field in fields:
                series = hourly.get(field)
                if isinstance(series, list) and index < len(series) and series[index] is not None:
                    points.setdefault(int(slot), {})[forecast_field_name(field, lead)] = series[index]
        return sorted(points.items())


def _hourly_series(response):
    """Read an Open-Meteo forecast response's hourly block and its slot times.

    Args:
        response (requests.Response): the forecast response, already checked for an error status

    Returns:
        tuple: (the ``hourly`` mapping, its ``time`` list in epoch seconds)

    Raises:
        SourceConnectionError: the body is not JSON, or holds no hourly time series. Raised as a
            source failure because that is the one ``collect_forecast()`` tolerates; any other
            type would reach the worker and cost the collector its interval for a forecast it
            does not depend on. A 200 that is not JSON is a captive portal or a proxy's page.
    """
    try:
        body = response.json()
    except ValueError as e:
        raise SourceConnectionError(
            f"Open-Meteo answered the forecast request with something that is not JSON "
            f"(got {response.text[:200]!r}): {render_external(e)}"
        ) from e
    hourly = body.get("hourly") if isinstance(body, dict) else None
    times = hourly.get("time") if isinstance(hourly, dict) else None
    if not isinstance(times, list) or not all(isinstance(t, (int, float)) for t in times):
        raise SourceConnectionError(
            f"Open-Meteo answered the forecast request without an hourly time series (got {body!r:.200})"
        )
    return hourly, times


def _refusal_reason(response):
    """Return the reason Open-Meteo gave for refusing a request, or a note that it gave none.

    Args:
        response (requests.Response or None): the refused response

    Returns:
        str: the ``reason`` from the error body, or what was there instead
    """
    if response is None:
        return "no response"
    try:
        body = response.json()
    except ValueError:
        return f"no JSON body (got {response.text[:200]!r})"
    if isinstance(body, dict) and body.get("reason"):
        return str(body["reason"])
    return f"none given (got {body!r:.200})"
