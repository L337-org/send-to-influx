"""Functions to get National Grid Carbon Intensity data ready to send to InfluxDB."""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT"

import time
from datetime import datetime, timezone
import requests
from toinflux.influx import DataHandler, forecast_field_name, forecast_slots
from toinflux.exceptions import SourceConnectionError
from toinflux.general import render_external, render_values

CARBON_INTENSITY_BASE_URL = "https://api.carbonintensity.org.uk"

#: The intensity fields ``fields`` may select, and the default: both, as every install collected
#: before the setting existed. The ``gen_<fuel>`` fields stay with ``include_generation``, because
#: their names come from the API's fuel list rather than from us.
INTENSITY_FIELDS = ("intensity_actual", "intensity_forecast")
ACCEPT_JSON = {"Accept": "application/json"}


class CarbonIntensity(DataHandler):
    """Child class of DataHandler to get National Grid carbon intensity data.

    Attributes:
        MINIMUM_INTERVAL (int): 900 - the grid publishes on half-hourly
            settlement periods, so this is already twice as often as the figure moves.
        DEFAULT_MAX_AGE (int): 3600 - half-hourly settlement periods, and the
            figure caps output rather than holding a setpoint.
        FORECAST_MAX_LEAD_HOURS (int): 47 - the forward forecast runs 48 hours.
        FORECAST_HEADER (str): forecasts go to ``carbonintensity_forecast``.
        FORECAST_UNITS (dict): the unit of the forecast base name ``intensity``.
        MCP_DESCRIPTION (str): what this source advertises to an MCP client.
        MCP_FIELD_METADATA (dict): unit and aggregation kind for the two intensity
            fields; the ``gen_<fuel>`` fields carry none, and UNITS.md documents them.
    """

    # The national grid publishes on half-hourly settlement periods, so fifteen minutes is
    # already twice as often as the underlying figure can move. Free public API, no key,
    # same courtesy as Open-Meteo. The shipped collection interval is 1800.
    MINIMUM_INTERVAL = 900
    # Half-hourly settlement periods, so an hour spans two of them. This figure caps output
    # rather than holding a setpoint - a control uses it to decide whether to run one heater
    # or two - so a little staleness costs efficiency rather than correctness.
    DEFAULT_MAX_AGE = 3600
    # The forward forecast runs 48 hours from the half-hour it is asked from. One hour short
    # of that, so the half-hour at or after the longest lead is always inside it.
    FORECAST_MAX_LEAD_HOURS = 47
    # Its own measurement, beside "carbonintensity": see DataHandler.FORECAST_HEADER.
    FORECAST_HEADER = "carbonintensity_forecast,source=national_grid "
    # The forecast is of "intensity", which is not a live field of its own.
    FORECAST_UNITS = {"intensity": "gCO2/kWh"}

    MCP_DESCRIPTION = "UK carbon intensity (actual/forecast gCO2/kWh) and the generation fuel mix."
    # Only the two intensity fields carry a unit here. The gen_<fuel>
    # generation-mix fields (gen_wind, gen_gas, ...) are all %, but they share a
    # prefix rather than the suffix ReadSchema.metadata_for matches on, so they
    # go un-annotated rather than special-casing prefix matching for one source;
    # UNITS.md documents them.
    MCP_FIELD_METADATA = {
        "intensity_actual": {"unit": "gCO2/kWh", "kind": "gauge"},
        # The one thing the name does not carry: this is the forecast for the same
        # half-hour the actual describes, so plotting the two together compares a
        # prediction with its outcome rather than showing two periods.
        "intensity_forecast": {
            "unit": "gCO2/kWh",
            "kind": "gauge",
            "description": "Forecast for the same settlement period as intensity_actual, not a future one.",
        },
    }

    def _get(self, path):
        """Make a GET request to the Carbon Intensity API.

        Args:
            path (str): API path relative to the base URL (without leading slash)

        Returns:
            dict: parsed JSON response

        Raises:
            SourceConnectionError: the API could not be reached, or answered with an error
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
            # Raised, not logged as well: every caller reports a failed read itself, and at
            # the level its own situation deserves. See the note in philipshue.py.
            raise SourceConnectionError(render_external(e)) from e
        return response.json()

    def get_data(self):
        """Get current national grid carbon intensity, and optionally fuel mix.

        Both come from the National Grid ESO Carbon Intensity API.

        Carbon intensity (gCO2/kWh) is collected from the ``/intensity`` endpoint.
        If ``include_generation`` is set in settings, generation fuel mix percentages
        are also collected from the ``/generation`` endpoint.

        Returns:
            dict: ``intensity_actual`` and ``intensity_forecast`` in gCO2/kWh, each present
                only when ``fields`` selects it (both by default) and the API reported it,
                plus a ``gen_<fuel>`` percentage per fuel when ``include_generation`` is set.

        Raises:
            SourceConnectionError: propagated from ``_get`` when the API could not be reached,
                or answered with an error
        """
        self.influx_header = "carbonintensity,source=national_grid "
        self.data = {}
        wanted = self.source_settings.get("fields", INTENSITY_FIELDS)

        # Get current carbon intensity
        result = self._get("intensity")
        if result.get("data"):
            intensity = result["data"][0].get("intensity", {})
            if "intensity_actual" in wanted and intensity.get("actual") is not None:
                self.data["intensity_actual"] = intensity["actual"]
            if "intensity_forecast" in wanted and intensity.get("forecast") is not None:
                self.data["intensity_forecast"] = intensity["forecast"]

        # Optionally collect the generation fuel mix
        if self.source_settings.get("include_generation", False):
            result = self._get("generation")
            if result.get("data"):
                for item in result["data"].get("generationmix", []):
                    self.data[f"gen_{item['fuel']}"] = item["perc"]

        return self.data

    @classmethod
    def settings_problems(cls, source, source_settings):
        """Return problems with ``fields``, the intensity fields to collect.

        A name outside :data:`INTENSITY_FIELDS` is refused rather than ignored: ignored, a
        mistyped ``intensity_actaul`` would quietly stop the actual being collected.

        Args:
            source (str): the source name, for the message
            source_settings (dict): the ``carbonintensity`` section

        Returns:
            list: error strings, empty when ``fields`` is absent or usable
        """
        if "fields" not in source_settings:
            return []
        fields = source_settings["fields"]
        where = f"{source}.fields"
        if not isinstance(fields, list) or not fields:
            return [f"{where} must be a non-empty list chosen from {render_values(INTENSITY_FIELDS)} (got {fields!r})"]
        unknown = [field for field in fields if field not in INTENSITY_FIELDS]
        if unknown:
            return [f"{where} may only name {render_values(INTENSITY_FIELDS)} (got {render_values(unknown)})"]
        if len(set(fields)) != len(fields):
            return [f"{where} must not repeat a field (got {fields!r})"]
        return []

    @classmethod
    def forecast_fields_for(cls, source_settings):
        """Return the one field forecast: the national intensity.

        Args:
            source_settings (dict): the ``carbonintensity`` settings section, unused

        Returns:
            list: ``["intensity"]``
        """
        return ["intensity"]

    def get_forecast(self):
        """Get the national forward forecast of carbon intensity, one value per lead time.

        From the ``/intensity/{from}/fw48h`` endpoint, which returns every half-hour from the
        one containing ``from`` to 48 hours ahead. The field is ``intensity_forecast_<hours>h``:
        a forecast of the intensity, made that far ahead. Not to be confused with
        ``intensity_forecast`` on the live measurement, which is the forecast for the
        half-hour ``intensity_actual`` describes, read after the fact.

        Returns:
            list: ``(timestamp, {field: value})`` pairs, one per half-hour a lead time lands
                on, stamped at the half-hour's start. Empty when ``forecast_hours`` is not
                set. A half-hour with no forecast value is left out.

        Raises:
            SourceConnectionError: the API could not be reached or refused the request, or
                answered with something that is not a forward forecast
        """
        leads = self.forecast_lead_hours()
        if not leads:
            return []
        now = time.time()
        start = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        try:
            result = self._get(f"intensity/{start}/fw48h")
        # ValueError too: _get() decodes the body outside its own try, so a 200 that is not JSON
        # - a captive portal, a proxy's error page - raises a decode error. As a source failure
        # collect_forecast() tolerates it; any other type would reach the worker and cost the
        # collector its interval for a forecast it does not depend on.
        except (SourceConnectionError, ValueError) as e:
            raise SourceConnectionError(
                f"Could not fetch the carbon-intensity forecast from {start}: {render_external(e)}"
            ) from e

        periods = _forecast_periods(result)
        slots = forecast_slots(sorted(periods), now, leads)
        points = {}
        for lead, slot in slots.items():
            for field in self.forecast_fields():
                points.setdefault(slot, {})[forecast_field_name(field, lead)] = periods[slot]
        return sorted(points.items())


def _forecast_periods(result):
    """Read the forward forecast's half-hours into ``{start: forecast}``.

    Args:
        result (object): the parsed response body

    Returns:
        dict: each half-hour's start, in epoch seconds, to its forecast intensity, leaving out
            a half-hour with no forecast value

    Raises:
        SourceConnectionError: the body is not a list of half-hours, or one cannot be read
    """
    entries = result.get("data") if isinstance(result, dict) else None
    if not isinstance(entries, list):
        raise SourceConnectionError(
            f"The carbon-intensity forecast came back without a list of half-hours (got {result!r:.200})"
        )
    periods = {}
    for entry in entries:
        try:
            begins = datetime.strptime(entry["from"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
            forecast = entry["intensity"]["forecast"]
        except (KeyError, TypeError, ValueError) as e:
            raise SourceConnectionError(
                f"The carbon-intensity forecast held a half-hour it could not be read from "
                f"(got {entry!r:.200}): {render_external(e)}"
            ) from e
        if forecast is not None:
            periods[int(begins.timestamp())] = forecast
    return periods
