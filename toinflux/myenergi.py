"""Functions to get MyEnergi data ready to send to InfluxDB"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT License"
__version__ = "1.0"

import logging
import datetime
import requests
from requests.auth import HTTPDigestAuth
from toinflux.influx import DataHandler
from toinflux.exceptions import ConfigError, SourceConnectionError


class MyEnergi(DataHandler):
    """Child class of DataHandler to get data from MyEnergi"""

    def get_data_from_myenergi(self, url):
        """
        Get the data from the myenergi API

        :param url: full API endpoint URL
        :type url: str
        :return: parsed JSON response
        :rtype: dict
        """
        # Get the data for the given serial from the MyEnergi API
        serial = self.source_settings["serial"]
        auth = HTTPDigestAuth(serial, self.settings["myenergi"]["apikey"])
        try:
            response = self.session.get(url, auth=auth, timeout=self.settings["myenergi"].get("timeout", 5))
        except requests.exceptions.RequestException as e:
            logging.error("Error connecting to MyEnergi API - %s", e)
            raise SourceConnectionError(str(e)) from e

        if response.status_code == 200:
            pass
        elif response.status_code == 401:
            logging.error("Login unsuccessful. Please check username, password or URL.")
            raise SourceConnectionError("Login unsuccessful. Please check username, password or URL.")
        else:
            logging.error("Login unsuccessful. Return code: %s", response.status_code)
            raise SourceConnectionError(f"Login unsuccessful. Return code: {response.status_code}")

        try:
            return response.json()
        except requests.exceptions.JSONDecodeError as e:
            logging.error("Error parsing MyEnergi API response - %s", e)
            raise SourceConnectionError(str(e)) from e

    def _parse_device_data(self, device_key, url_key):
        """
        Fetch data for a MyEnergi device and filter it to configured fields if set.

        The endpoint is per device *type* and returns every device of that type on the
        account, so the configured serial is what picks one out of the list. Taking index 0
        instead meant a second device of the same type was silently never collected, and an
        account owning none of that type raised ``IndexError`` - which the worker loop's
        broad handler caught, logged as "list index out of range", and retried forever
        without ever naming the cause.

        :param device_key: settings/response key for the device, e.g. "eddi", "harvi", "zappi"
        :type device_key: str
        :param url_key: settings key (under "myenergi") for the device's API URL, e.g. "eddi_url"
        :type url_key: str
        :return: device data, filtered to the configured "fields" list if present
        :rtype: dict
        :raises SourceConnectionError: the account has no device of this type. Transient
            rather than fatal because a device can legitimately be mid-provisioning, and
            because an absent key is not distinguishable here from a temporary API oddity
        :raises ConfigError: devices of this type came back but none has the configured
            serial. The account is reachable and the type exists, so the serial is simply
            wrong, and no amount of waiting fixes that - this stops the worker instead of
            backing off forever
        """
        myenergi_data = self.get_data_from_myenergi(self.settings["myenergi"][url_key])
        device_data = self._select_device(myenergi_data, device_key)

        device_settings = self.settings[device_key]
        if "fields" in device_settings:
            return {k: device_data[k] for k in device_settings["fields"] if k in device_data}
        return device_data

    def _select_device(self, myenergi_data, device_key):
        """
        Pick the device matching this source's configured serial out of the API response.

        ``sno`` is the serial field - confirmed against the live MyEnergi API, where it is
        the only key in a device object whose value equals the configured serial. Compared
        as strings on both sides: an all-digit serial in ``settings.yaml`` is an ``int``
        unless quoted, so a raw comparison would never match and would look exactly like a
        wrong serial rather than a type mismatch.

        :param myenergi_data: the parsed API response
        :type myenergi_data: dict
        :param device_key: the response key for this device type, e.g. "zappi"
        :type device_key: str
        :return: the matching device's data
        :rtype: dict
        :raises SourceConnectionError: no device of this type in the response
        :raises ConfigError: devices present, but none with the configured serial
        """
        # A missing key is treated as an empty list rather than allowed to raise KeyError:
        # the response shape is the vendor's to change, and a KeyError would escape the
        # SourceConnectionError/ConfigError split the worker loop relies on exactly as the
        # IndexError did.
        devices = myenergi_data.get(device_key) or []
        serial = str(self.source_settings["serial"])
        for device in devices:
            if str(device.get("sno")) == serial:
                return device
        if not devices:
            logging.error("MyEnergi returned no %s devices for this account", device_key)
            raise SourceConnectionError(
                f"MyEnergi returned no {device_key} devices for this account - check that a "
                f"{device_key} is provisioned, or remove {device_key} from the configured sources"
            )
        # Name what the account does have: the difference between a message the operator
        # can act on and one that only says no.
        found = ", ".join(sorted(str(device.get("sno")) for device in devices)) or "(none reported a serial)"
        logging.critical(
            "No %s on this MyEnergi account has serial %s; the account reports: %s",
            device_key,
            serial,
            found,
        )
        raise ConfigError(
            f"no {device_key} on this MyEnergi account has serial {serial!r}; "
            f"the account reports these {device_key} serials: {found}"
        )

    def dayhour_results(self, year, month, day, hour=None):
        """
        Get the data for a specific day

        :param year: four-digit year, e.g. "2026"
        :type year: str
        :param month: zero-padded month, e.g. "06"
        :type month: str
        :param day: zero-padded day, e.g. "29"
        :type day: str
        :param hour: hour of the day (0-23); if None, results for the whole day are returned
        :type hour: int
        :return: Charge, Import, Export and Genera values in kWh
        :rtype: dict
        """
        # Get the Day/Hour data from the MyEnergi API
        serial = self.source_settings["serial"]
        dayhour_url = self.settings["myenergi"]["dayhour_url"] + serial
        response_data = self.get_data_from_myenergi(dayhour_url + "-" + str(year) + "-" + str(month) + "-" + str(day))
        charge_amount = 0
        import_amount = 0
        export_amount = 0
        genera_amount = 0

        # Tot up the data for the day/hour
        if response_data.get("U" + serial, False):
            for item in response_data["U" + serial]:
                if hour is not None and item.get("hr", -1) == hour:
                    charge_amount = item.get("h1d", 0)
                    import_amount = item.get("imp", 0)
                    export_amount = item.get("exp", 0)
                    genera_amount = item.get("gep", 0)
                    break
                charge_amount += item.get("h1d", 0)
                import_amount += item.get("imp", 0)
                export_amount += item.get("exp", 0)
                genera_amount += item.get("gep", 0)

        # Convert and round the data to 4 decimal places
        data = {
            "Charge": round((charge_amount / 3600 / 1000), 4),
            "Import": round((import_amount / 3600 / 1000), 4),
            "Export": round((export_amount / 3600 / 1000), 4),
            "Genera": round((genera_amount / 3600 / 1000), 4),
        }

        return data


class Zappi(MyEnergi):
    """Child class of MyEnergi (which is in turn a child of DataHandler) to get data from a Zappi"""

    MCP_DESCRIPTION = "MyEnergi Zappi EV charger: charge and session energy, grid/generation power, and status."

    # All three MyEnergi devices share the "myenergi" measurement, distinguished
    # by the device tag - so the read schema needs both the measurement override
    # and the tag filter, or a query for one device would return all three.
    MCP_MEASUREMENT = "myenergi"
    MCP_TAG_FILTERS = {"device": "zappi"}
    MCP_FIELD_METADATA = {
        "frq": {"unit": "Hz"},
        "gen": {"unit": "W"},
        "grd": {"unit": "W"},
        "che": {"unit": "kWh"},
        "Charge": {"unit": "kWh"},
        "Import": {"unit": "kWh"},
        "Export": {"unit": "kWh"},
        "Genera": {"unit": "kWh"},
    }

    def get_data(self):
        """
        Get the data from the Zappi

        :return: data
        :rtype: dict
        """
        self.influx_header = "myenergi,device=zappi "
        self.data = self.parse_zappi_data()
        return self.data

    def parse_zappi_data(self):
        """
        Parse the data from the myenergi to get the values we want

        :return: data
        :rtype: dict
        """
        # Get the data for the Zappi from the MyEnergi API
        zappi_data = self._parse_device_data("zappi", "zappi_url")

        # Get the day/hour data for the Zappi. The MyEnergi day/hour API is keyed by UTC,
        # so the day/hour must be computed in UTC too - using local time would pick the
        # wrong hour (or the wrong day, around midnight) whenever local time isn't UTC.
        now = datetime.datetime.now(datetime.timezone.utc)
        day_data = self.dayhour_results(
            now.strftime("%Y"),
            now.strftime("%m"),
            now.strftime("%d"),
            now.hour,
        )

        return zappi_data | day_data


class Eddi(MyEnergi):
    """Child class of MyEnergi to get data from an Eddi hot water diverter"""

    MCP_DESCRIPTION = "MyEnergi Eddi hot-water diverter: diversion power, tank temperatures, and status."
    MCP_MEASUREMENT = "myenergi"
    MCP_TAG_FILTERS = {"device": "eddi"}
    MCP_FIELD_METADATA = {
        "frq": {"unit": "Hz"},
        "div": {"unit": "W"},
        "che": {"unit": "kWh"},
        "tp1": {"unit": "°C"},
        "tp2": {"unit": "°C"},
    }

    def get_data(self):
        """
        Get the data from the Eddi

        :return: data
        :rtype: dict
        """
        self.influx_header = "myenergi,device=eddi "
        self.data = self.parse_eddi_data()
        return self.data

    def parse_eddi_data(self):
        """
        Parse the data from the MyEnergi API for the Eddi device

        :return: data
        :rtype: dict
        """
        return self._parse_device_data("eddi", "eddi_url")


class Harvi(MyEnergi):
    """Child class of MyEnergi to get data from a Harvi CT clamp energy monitor"""

    MCP_DESCRIPTION = "MyEnergi Harvi energy monitor: CT-clamp power readings per channel."
    MCP_MEASUREMENT = "myenergi"
    MCP_TAG_FILTERS = {"device": "harvi"}
    MCP_FIELD_METADATA = {
        "ectp1": {"unit": "W"},
        "ectp2": {"unit": "W"},
        "ectp3": {"unit": "W"},
    }

    def get_data(self):
        """
        Get the data from the Harvi

        :return: data
        :rtype: dict
        """
        self.influx_header = "myenergi,device=harvi "
        self.data = self.parse_harvi_data()
        return self.data

    def parse_harvi_data(self):
        """
        Parse the data from the MyEnergi API for the Harvi device

        :return: data
        :rtype: dict
        """
        return self._parse_device_data("harvi", "harvi_url")
