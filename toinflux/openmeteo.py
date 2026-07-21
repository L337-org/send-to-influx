"""Functions to get Open-Meteo weather data ready to send to InfluxDB"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT License"
__version__ = "1.0"

import logging
import requests
from toinflux.influx import DataHandler
from toinflux.exceptions import SourceConnectionError

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

DEFAULT_FIELDS = ["temperature_2m"]


class OpenMeteo(DataHandler):
    """Child class of DataHandler to get weather data from Open-Meteo"""

    # Writes to the "weather" measurement, not "openmeteo".
    MCP_MEASUREMENT = "weather"
    # Units for the example-settings fields (see UNITS.md); other Open-Meteo
    # variables use that API's own default unit, so only the common ones are
    # annotated here.
    MCP_FIELD_METADATA = {
        "temperature_2m": {"unit": "°C"},
        "relative_humidity_2m": {"unit": "%"},
        "precipitation": {"unit": "mm"},
        "cloud_cover": {"unit": "%"},
        "wind_speed_10m": {"unit": "km/h"},
        "direct_radiation": {"unit": "W/m²"},
    }

    def get_data(self):
        """
        Get current weather observations from Open-Meteo

        :return: data
        :rtype: dict
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
