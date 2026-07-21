"""Functions to get National Grid Carbon Intensity data ready to send to InfluxDB"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT License"
__version__ = "1.0"

import logging
import requests
from toinflux.influx import DataHandler
from toinflux.exceptions import SourceConnectionError

CARBON_INTENSITY_BASE_URL = "https://api.carbonintensity.org.uk"
ACCEPT_JSON = {"Accept": "application/json"}


class CarbonIntensity(DataHandler):
    """Child class of DataHandler to get National Grid carbon intensity data"""

    # Only the two intensity fields carry a unit here. The gen_<fuel>
    # generation-mix fields (gen_wind, gen_gas, ...) are all %, but they share a
    # prefix rather than the suffix ReadSchema.metadata_for matches on, so they
    # go un-annotated rather than special-casing prefix matching for one source;
    # UNITS.md documents them.
    MCP_FIELD_METADATA = {
        "intensity_actual": {"unit": "gCO2/kWh"},
        "intensity_forecast": {"unit": "gCO2/kWh"},
    }

    def _get(self, path):
        """Make a GET request to the Carbon Intensity API.

        :param path: API path relative to the base URL (without leading slash)
        :type path: str
        :return: parsed JSON response
        :rtype: dict
        """
        url = f"{CARBON_INTENSITY_BASE_URL}/{path}"
        try:
            response = self.session.get(
                url,
                headers=ACCEPT_JSON,
                timeout=self.source_settings.get("timeout", 10),
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            logging.error("Error connecting to Carbon Intensity API - %s", e)
            raise SourceConnectionError(str(e)) from e
        return response.json()

    def get_data(self):
        """
        Get current national grid carbon intensity and optionally fuel mix from
        the National Grid ESO Carbon Intensity API.

        Carbon intensity (gCO2/kWh) is collected from the ``/intensity`` endpoint.
        If ``include_generation`` is set in settings, generation fuel mix percentages
        are also collected from the ``/generation`` endpoint.

        :return: data
        :rtype: dict
        """
        self.influx_header = "carbonintensity,source=national_grid "
        self.data = {}

        # Get current carbon intensity
        result = self._get("intensity")
        if result.get("data"):
            intensity = result["data"][0].get("intensity", {})
            if intensity.get("actual") is not None:
                self.data["intensity_actual"] = intensity["actual"]
            if intensity.get("forecast") is not None:
                self.data["intensity_forecast"] = intensity["forecast"]

        # Optionally collect the generation fuel mix
        if self.source_settings.get("include_generation", False):
            result = self._get("generation")
            if result.get("data"):
                for item in result["data"].get("generationmix", []):
                    self.data[f"gen_{item['fuel']}"] = item["perc"]

        return self.data
