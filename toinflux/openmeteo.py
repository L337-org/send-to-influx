"""Functions to get Open-Meteo weather data ready to send to InfluxDB."""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT"

import logging
import requests
from toinflux.influx import DataHandler
from toinflux.exceptions import SourceConnectionError

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

DEFAULT_FIELDS = ["temperature_2m"]


class OpenMeteo(DataHandler):
    """Child class of DataHandler to get weather data from Open-Meteo.

    Attributes:
        MINIMUM_INTERVAL (int): 600 - a free public API used without a key, and about
            the resolution of the data, so asking faster returns the same numbers.
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

    MCP_DESCRIPTION = "Open-Meteo weather: temperature, humidity, precipitation, cloud, wind and radiation."
    # Writes to the "weather" measurement, not "openmeteo".
    MCP_MEASUREMENT = "weather"
    # Units for the example-settings fields (see UNITS.md); other Open-Meteo
    # variables use that API's own default unit, so only the common ones are
    # annotated here.
    MCP_FIELD_METADATA = {
        "temperature_2m": {"unit": "°C", "kind": "gauge"},
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
            logging.error("Error connecting to Open-Meteo - %s", e)
            raise SourceConnectionError(str(e)) from e

        current = response.json().get("current", {})
        self.data = {k: current[k] for k in fields if k in current}
        self.influx_header = "weather,source=open-meteo "
        return self.data
