"""Functions to get MyEnergi data ready to send to InfluxDB"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT License"
__version__ = "1.0"

import logging
import datetime
from dataclasses import dataclass
import requests
from requests.auth import HTTPDigestAuth
from toinflux.influx import DataHandler, escape_key_or_tag_value
from toinflux.exceptions import ConfigError, SourceConnectionError

# The device types that share the `myenergi` measurement, each its own source and settings
# block. Used to check label uniqueness across all three, since a label is the `device` tag
# value and two blocks agreeing on one would merge their series.
DEVICE_SOURCES = ("zappi", "eddi", "harvi")


@dataclass(frozen=True)
class MyEnergiDevice:
    """One configured MyEnergi device: which serial identifies it, what to call it, and
    which fields to collect.

    ``label`` is the emitted ``device`` tag value, not a display name - see
    ``enumerate_devices``. ``fields`` is None when everything the API returns should be
    written.
    """

    serial: str
    label: str
    fields: "list | None"


def enumerate_devices(source, source_settings):
    """
    Return the devices configured for one MyEnergi source, plus any problems found.

    Two config shapes, and both may appear together. A ``serial`` at the top of the block is
    the legacy single-device form every existing install has; its ``label`` is optional and
    **defaults to the source name**, which is what keeps such an install writing
    ``device=zappi`` exactly as before and is why this feature needs no data migration. A
    ``devices:`` list adds further devices, and each entry must name its ``label``
    explicitly - there is no sensible default for the second device, and deriving one from
    the serial would produce exactly the unreadable tag values that tagging by label exists
    to avoid.

    ``fields`` resolves device-first, then block-level, then everything the API returns, so
    a shared list can be written once and overridden per device.

    Follows ``philipshue.enumerate_bridges``' shape - (devices, errors, warnings) - so the
    two instanced sources report configuration problems the same way, and validation can
    treat them alike.

    :param source: the source name, used as the legacy device's default label
    :type source: str
    :param source_settings: that source's settings block
    :type source_settings: dict or None
    :return: (devices, errors, warnings)
    :rtype: tuple
    """
    if not isinstance(source_settings, dict):
        return [], [f"{source} settings must be a mapping"], []
    devices, errors, warnings = [], [], []
    block_fields = source_settings.get("fields")

    if source_settings.get("serial") is not None:
        devices.append(
            MyEnergiDevice(
                serial=str(source_settings["serial"]),
                # An explicit top-level label is honoured; without one the source name keeps
                # the emitted tag identical to what this install already writes.
                label=str(source_settings.get("label") or source),
                fields=block_fields,
            )
        )

    raw = source_settings.get("devices")
    if raw is None:
        # A bare `devices:` key parses as None. Treated as absent rather than rejected, the
        # same way the shipped `sources:` key behaves when every entry is commented out. If
        # that leaves the block with no device at all, the source simply expands to no
        # worker and the existing nothing-to-collect path explains why.
        raw = []
    if not isinstance(raw, list):
        errors.append(f"{source}.devices must be a list (got {type(raw).__name__})")
        raw = []

    for index, entry in enumerate(raw):
        position = f"{source}.devices[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{position} must be a mapping (got {type(entry).__name__})")
            continue
        if entry.get("serial") is None:
            errors.append(f"{position} has no serial")
            continue
        label = entry.get("label")
        if not (isinstance(label, str) and label.strip()):
            errors.append(
                f"{position} has no label - every entry in a devices list must name one, "
                f"since the label is what identifies the device in InfluxDB and in answers"
            )
            continue
        devices.append(
            MyEnergiDevice(serial=str(entry["serial"]), label=label.strip(), fields=entry.get("fields", block_fields))
        )

    errors.extend(_duplicate_errors(source, devices))
    if not devices:
        warnings.append(f"no {source} device is configured, so {source} will not be collected")
    return devices, errors, warnings


def _duplicate_errors(source, devices):
    """
    Return errors for duplicate labels or serials within one source's devices.

    A repeated label means two devices writing to one series, silently interleaving two
    devices' readings; a repeated serial means two workers collecting the same device and
    overwriting each other at second precision. Both are self-contradictory config rather
    than something to warn about and continue past.

    :param source: source name, for the messages
    :type source: str
    :param devices: the devices enumerated so far
    :type devices: list
    :return: error strings, empty when there are none
    :rtype: list
    """
    errors = []
    for attribute, description in (("label", "label"), ("serial", "serial")):
        seen, repeated = set(), set()
        for device in devices:
            value = getattr(device, attribute)
            if value in seen:
                repeated.add(value)
            seen.add(value)
        for value in sorted(repeated):
            errors.append(
                f"{source} has more than one device with {description} {value!r}; " f"each device needs its own"
            )
    return errors


def duplicate_label_errors(settings):
    """
    Return errors for a label used by more than one MyEnergi source.

    Uniqueness has to span all three blocks, not just one: the types share the ``myenergi``
    measurement and the label is the ``device`` tag value, so a zappi and an eddi both
    labelled "Garage" would merge into a single series carrying both devices' fields.

    :param settings: parsed settings dictionary
    :type settings: dict
    :return: error strings, empty when there are none
    :rtype: list
    """
    owners = {}
    for source in DEVICE_SOURCES:
        devices, _, _ = enumerate_devices(source, settings.get(source))
        for device in devices:
            owners.setdefault(device.label, []).append(source)
    errors = []
    for label, sources in sorted(owners.items()):
        if len(sources) > 1:
            errors.append(
                f"MyEnergi label {label!r} is used by more than one source ({', '.join(sorted(sources))}); "
                f"labels are the shared `device` tag, so they must be unique across zappi, eddi and harvi"
            )
    return errors


class MyEnergi(DataHandler):
    """Child class of DataHandler to get data from MyEnergi"""

    # All three types share the `myenergi` measurement, and `device` is the tag that tells
    # them apart - now carrying the operator's label rather than the type name. Naming it as
    # the instance axis is what lets a read scope to one device and report per device.
    # MCP_TAG_FILTERS is deliberately empty now: the filter is per instance, so it comes from
    # mcp_tag_filters() below rather than a class constant that cannot vary.
    MCP_INSTANCE_TAG = "device"
    MCP_TAG_FILTERS: dict = {}

    def device(self):
        """
        Return the configured device this handler collects from.

        ``self.instance`` is a device label; ``None`` means the first configured device, which
        is what keeps a single-device install - and every caller that builds a handler without
        an instance - behaving exactly as it did before this existed.

        :return: the device
        :rtype: MyEnergiDevice
        :raises ConfigError: nothing is configured, or the named label is not configured.
            Fatal rather than transient, so a worker whose device has been removed stops
            instead of retrying a doomed lookup forever
        """
        devices, errors, _ = enumerate_devices(self.source, self.source_settings)
        if errors:
            raise ConfigError("; ".join(errors))
        if not devices:
            raise ConfigError(f"no {self.source} device is configured")
        if self.instance is None:
            return devices[0]
        for device in devices:
            if device.label == self.instance:
                return device
        known = ", ".join(sorted(device.label for device in devices))
        raise ConfigError(f"no {self.source} device is labelled {self.instance!r}; configured labels: {known}")

    def mcp_tag_filters(self):
        """
        Scope reads to this handler's own device.

        A method rather than the class attribute because the answer depends on which device
        this handler serves. It also carries the type discrimination that
        ``MCP_TAG_FILTERS = {"device": "zappi"}`` used to: the three types share one
        measurement, so without a device filter a read of "the myenergi measurement" would
        return all three types' devices.

        :return: tag filters for this handler's reads
        :rtype: dict
        """
        return {"device": self.device().label}

    def heartbeat_tags(self):
        """
        Tag the heartbeat with this device, matching the tag its own data carries.

        The base implementation would tag ``host=<instance>``, which for MyEnergi is a device
        label and not a host at all - so the health series would disagree with the
        measurement it reports on, and could not be joined to it. Several devices of one type
        would otherwise also share ``collector_status,source=zappi`` and overwrite each other
        at second precision, the same defect Speedtest had across hosts.

        This adds a ``device`` tag to a legacy install's heartbeat, where previously there was
        none. A deliberate emitted-data change on a liveness signal, noted in UNITS.md: old
        heartbeat points sit in an untagged series.

        :return: ``{"device": <this device's label>}``
        :rtype: dict
        """
        return {"device": self.device().label}

    def auth_serial(self):
        """
        Return the serial used as the MyEnergi API's digest username.

        The credential is account-scoped rather than per device: the real zappi serial
        authenticates against the zappi, eddi *and* harvi endpoints alike, verified against
        the live account. So this defaults to the device's own serial, which is what every
        existing install already sends.

        ``myenergi.auth_serial`` overrides it, and exists because that account-scoping is not
        *proven* for a second device of one type - the test account has one zappi. If a
        second device's own serial turns out not to authenticate, this is already here and no
        one needs a config change to work around it.

        :return: the serial to authenticate with
        :rtype: str
        """
        override = self.settings.get("myenergi", {}).get("auth_serial")
        if override:
            return str(override)
        return self.device().serial

    def get_data_from_myenergi(self, url):
        """
        Get the data from the myenergi API

        :param url: full API endpoint URL
        :type url: str
        :return: parsed JSON response
        :rtype: dict
        """
        # Get the data for the given serial from the MyEnergi API
        auth = HTTPDigestAuth(self.auth_serial(), self.settings["myenergi"]["apikey"])
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
        device = self.device()
        myenergi_data = self.get_data_from_myenergi(self.settings["myenergi"][url_key])
        device_data = self._select_device(myenergi_data, device_key, device)

        # Fields resolve device-first, then block-level, then everything the API returned -
        # see enumerate_devices, which does that resolution once so this cannot disagree.
        if device.fields is not None:
            return {k: device_data[k] for k in device.fields if k in device_data}
        return device_data

    def _select_device(self, myenergi_data, device_key, device=None):
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
        serial = (device or self.device()).serial
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
        # Get the Day/Hour data from the MyEnergi API - this handler's own device, so a
        # second zappi's day totals are its own rather than the first one's.
        serial = self.device().serial
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
        # The label, escaped: send_data() takes the header verbatim, so a label
        # containing a comma, space or equals would end the tag set early and
        # silently corrupt the point. A legacy install's label defaults to the
        # source name, so this stays byte-identical to what it already writes.
        self.influx_header = f"myenergi,device={escape_key_or_tag_value(self.device().label)} "
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
        # The label, escaped: send_data() takes the header verbatim, so a label
        # containing a comma, space or equals would end the tag set early and
        # silently corrupt the point. A legacy install's label defaults to the
        # source name, so this stays byte-identical to what it already writes.
        self.influx_header = f"myenergi,device={escape_key_or_tag_value(self.device().label)} "
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
        # The label, escaped: send_data() takes the header verbatim, so a label
        # containing a comma, space or equals would end the tag set early and
        # silently corrupt the point. A legacy install's label defaults to the
        # source name, so this stays byte-identical to what it already writes.
        self.influx_header = f"myenergi,device={escape_key_or_tag_value(self.device().label)} "
        self.data = self.parse_harvi_data()
        return self.data

    def parse_harvi_data(self):
        """
        Parse the data from the MyEnergi API for the Harvi device

        :return: data
        :rtype: dict
        """
        return self._parse_device_data("harvi", "harvi_url")
