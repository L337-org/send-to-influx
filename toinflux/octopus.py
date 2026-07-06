"""Functions to get Octopus Energy data ready to send to InfluxDB"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT License"
__version__ = "1.0"

import logging
import requests
from datetime import datetime, timezone
from toinflux.influx import DataHandler
from toinflux.exceptions import SourceConnectionError

OCTOPUS_BASE_URL = "https://api.octopus.energy/v1"


class Octopus(DataHandler):
    """Child class of DataHandler to get data from Octopus Energy"""

    def _get(self, path):
        """Make an authenticated GET request to the Octopus Energy API.

        :param path: API path relative to the base URL (without leading slash)
        :type path: str
        :return: parsed JSON response
        :rtype: dict
        """
        url = f"{OCTOPUS_BASE_URL}/{path}"
        try:
            response = self.session.get(
                url,
                auth=(self.source_settings["api_key"], ""),
                timeout=self.source_settings.get("timeout", 10),
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            logging.error("Error connecting to Octopus Energy API - %s", e)
            raise SourceConnectionError(str(e)) from e
        return response.json()

    @staticmethod
    def _parse_interval_start(interval_start):
        """Convert an Octopus API ISO-8601 ``interval_start`` string to unix epoch seconds.

        :param interval_start: ISO-8601 timestamp, e.g. "2026-07-06T10:00:00+01:00", or None
        :type interval_start: str or None
        :return: unix epoch seconds, or None if interval_start was not provided
        :rtype: int or None
        """
        if not interval_start:
            return None
        return int(datetime.fromisoformat(interval_start.replace("Z", "+00:00")).timestamp())

    def get_data(self):
        """
        Get the latest electricity/gas consumption and optionally current unit rate from Octopus Energy.

        Consumption is returned as the most recent half-hourly reading (smart meter data
        typically arrives with a delay of up to 24 hours). Gas consumption is only collected
        if ``gas_mprn`` and ``gas_meter_serial`` are configured in settings; its unit depends
        on the meter type (kWh for SMETS1 Secure meters, m3 for SMETS2 meters), so it is sent
        unconverted as ``gas_consumption``. If ``product_code`` and ``tariff_code`` are
        configured in settings, the current electricity unit rate for that tariff is also collected.

        :return: data
        :rtype: dict
        """
        self.influx_header = "octopus,source=octopus_energy "
        self.data = {}
        self.timestamp = None

        # Get most recent electricity consumption reading
        mpan = self.source_settings["mpan"]
        serial = self.source_settings["meter_serial"]
        result = self._get(
            f"electricity-meter-points/{mpan}/meters/{serial}/consumption/" "?page_size=1&order_by=-period"
        )
        if result.get("results"):
            reading = result["results"][0]
            self.data["consumption_kwh"] = reading["consumption"]
            # Write the point at the reading's own interval start rather than "now" - readings
            # arrive up to 24h late, and Octopus keeps re-serving the same latest reading for
            # ~30 min before the next one lands, so a fixed timestamp makes repeat writes of the
            # same reading idempotent (overwrite) instead of creating duplicate points.
            self.timestamp = self._parse_interval_start(reading.get("interval_start"))

        # Get most recent gas consumption reading, if a gas meter is configured
        gas_mprn = self.source_settings.get("gas_mprn")
        gas_meter_serial = self.source_settings.get("gas_meter_serial")
        if gas_mprn and gas_meter_serial:
            result = self._get(
                f"gas-meter-points/{gas_mprn}/meters/{gas_meter_serial}/consumption/" "?page_size=1&order_by=-period"
            )
            if result.get("results"):
                self.data["gas_consumption"] = result["results"][0]["consumption"]

        # Get current unit rate if tariff is configured (useful for time-of-use tariffs)
        product_code = self.source_settings.get("product_code")
        tariff_code = self.source_settings.get("tariff_code")
        if product_code and tariff_code:
            now = datetime.now(timezone.utc)
            slot_min = (now.minute // 30) * 30
            slot_start = now.replace(minute=slot_min, second=0, microsecond=0)
            period_from = slot_start.strftime("%Y-%m-%dT%H:%M:%SZ")
            result = self._get(
                f"products/{product_code}/electricity-tariffs/{tariff_code}/"
                f"standard-unit-rates/?period_from={period_from}&page_size=1"
            )
            if result.get("results"):
                self.data["unit_rate_p_per_kwh"] = result["results"][0]["value_inc_vat"]

        return self.data
