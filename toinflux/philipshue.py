"""Functions to get data from a Hue Bridge and format ready for InfluxDB"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT License"
__version__ = "1.0"

import logging
import warnings
import urllib3
import requests
from toinflux.influx import DataHandler
from toinflux.exceptions import SourceConnectionError


class Hue(DataHandler):
    """Child class of DataHandler to get data from a Hue Bridge"""

    # Hue is the one v1 source with a documented, buildable device-write path
    # (PUT /api/{user}/lights/{id}/state on the same session/auth the collector
    # already uses). The MCP write tool is still only registered when the
    # operator sets hue.mcp_read_write: true - see DataHandler.mcp_write_enabled.
    MCP_WRITABLE = True

    # Hue brightness ("bri") is 1-254; the MCP tool speaks 0-100 % and maps here.
    HUE_BRI_MIN = 1
    HUE_BRI_MAX = 254

    def get_data(self):
        """
        Get the data from the Hue Bridge

        :return: data
        :rtype: dict
        """
        self.influx_header = f"hue,host={self.settings['hue']['host']} "
        self.data = self.parse_hue_data()
        return self.data

    def get_data_from_hue_bridge(self):
        """
        Connect to the Hue bridge and get the sensor data

        :return: hue_data
        :rtype: dict
        """
        # Hue bridges are commonly reached over a self-signed local cert, so verification is
        # skipped by default; set hue.insecure: false in settings.yaml if yours has a valid cert.
        insecure = self.settings["hue"].get("insecure", True)
        try:
            with warnings.catch_warnings():
                if insecure:
                    warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
                response = self.session.get(
                    f"https://{self.settings['hue']['host']}/api/{self.settings['hue']['user']}",
                    timeout=self.settings["hue"].get("timeout", 5),
                    verify=not insecure,
                )
            hue_data = response.json()
        except requests.exceptions.RequestException as e:
            logging.error("Error connecting to Hue Bridge - %s", e)
            raise SourceConnectionError(str(e)) from e
        except ValueError as e:
            # response.json() raises on a non-JSON body (e.g. an HTML error page);
            # guarding here fixes both the collector read path and the MCP write
            # tools' device discovery, which both go through this method.
            logging.error("Hue Bridge returned an unparseable response - %s", e)
            raise SourceConnectionError(f"Hue Bridge returned an unparseable response: {e}") from e
        # A successful GET returns a dict (sensors/lights); a list only ever comes
        # back on error. Guard the indexing: an empty list, or a list whose first
        # item isn't the documented {"error": {...}} shape, is unexpected and must
        # fail cleanly rather than raise IndexError/KeyError unhandled.
        if isinstance(hue_data, list):
            first = hue_data[0] if hue_data else None
            if isinstance(first, dict) and "error" in first:
                description = first["error"].get("description", str(first["error"]))
            else:
                description = f"unexpected list response: {hue_data!r:.200}"
            logging.error("Error connecting to Hue Bridge - %s", description)
            raise SourceConnectionError(description)
        return hue_data

    def hue_device_name_to_name(self, device_name):
        """
        Converts the device name into a name to be used in InfluxDB

        If no name mapping exists in the settings file, the name in the Hue settings is used.
        Any spaces will be replaced with underscores.

        :param device_name: name of the device in the hue settings
        :type device_name: str
        :return: name
        :rtype: str
        """
        if "sensors" in self.settings["hue"]:
            name = self.settings["hue"]["sensors"].get(device_name, device_name)
        else:
            name = device_name
        return name.replace(" ", "_")

    def parse_hue_data(self):
        """
        Parse the data from the bridge to get the values we want

        :return: data
        :rtype: dict
        """
        data = {}
        hue_data = self.get_data_from_hue_bridge()

        # parse the sensor data
        for device in hue_data["sensors"].values():
            name = self.hue_device_name_to_name(device["name"])
            if device["type"] == "ZLLTemperature":
                # convert temperature to the desired units
                celsius = device["state"]["temperature"] / 100
                if self.settings["hue"].get("temperature_units") == "F":
                    data[name] = round((celsius * 1.8) + 32, 2)
                elif self.settings["hue"].get("temperature_units") == "K":
                    data[name] = round(celsius + 273.15, 2)
                else:
                    data[name] = round(celsius, 2)
            elif device["type"] == "ZLLLightLevel":
                # convert light level to lux
                data[name] = round(float(10 ** ((device["state"]["lightlevel"] - 1) / 10000)), 2)
            elif device["type"] == "ZLLPresence":
                # convert presence to boolean 0 or 1
                data[name] = int(1 if device["state"]["presence"] else 0)

        for device in hue_data["lights"].values():
            name = self.hue_device_name_to_name(device["name"])
            # convert brightness to percentage if the light is dimmable (has a "bri" attribute)
            # otherwise boolean 0 or 1 to cover smart plugs which are also listed as lights
            data[name] = int(device["state"].get("bri", 2.54) / 2.54) if device["state"]["on"] else 0

        return data

    def mcp_list_writable_devices(self):
        """Return ``{light_id: name}`` for every light/plug the bridge exposes.

        Used by the MCP write tool to resolve a friendly device name to its
        bridge id and to validate the target exists (the write allowlist, the
        same idea as the read tool's live field discovery). Reuses the collector's
        own authenticated bridge GET.

        :return: mapping of Hue light id (str) to the device's bridge name
        :rtype: dict
        :raises SourceConnectionError: if the bridge can't be reached
        """
        hue_data = self.get_data_from_hue_bridge()
        # Coerce both id and name to str: the docstring promises {id: name} as
        # strings, and a missing/blank/non-string bridge name falls back to the id.
        return {
            str(light_id): str(device.get("name") or light_id)
            for light_id, device in hue_data.get("lights", {}).items()
        }

    def _bri_from_percent(self, percent):
        """Map a 0-100 brightness percentage to Hue's 1-254 ``bri`` scale.

        0 % clamps to the minimum on-brightness rather than off - turning a light
        off is expressed with ``on=False``, not brightness 0, so the two controls
        stay independent and unambiguous.
        """
        scaled = round(percent / 100 * self.HUE_BRI_MAX)
        return max(self.HUE_BRI_MIN, min(scaled, self.HUE_BRI_MAX))

    def mcp_set_device_state(self, device, *, on=None, brightness_pct=None):
        """Set a Hue light/plug's state, the MCP write primitive for this source.

        Resolves ``device`` (a bridge light id or its exact name) against the live
        device list, builds the Hue state body from the friendly parameters, and
        PUTs it. Setting a brightness implies turning the light on unless ``on`` is
        explicitly given, since the bridge ignores ``bri`` on an off light.

        :param device: the target light id or its exact bridge name
        :type device: str
        :param on: turn the device on (True) or off (False); None leaves it
        :type on: bool or None
        :param brightness_pct: target brightness 0-100 %; None leaves it
        :type brightness_pct: int or float or None
        :return: a summary dict of what was applied
        :rtype: dict
        :raises SourceConnectionError: unknown device, invalid parameters, or a
            bridge/transport failure (mapped from the bridge's own error)
        """
        if on is None and brightness_pct is None:
            raise SourceConnectionError("nothing to set: provide 'on' and/or 'brightness_pct'")

        devices = self.mcp_list_writable_devices()
        light_id = self._resolve_device_id(device, devices)

        state = {}
        if brightness_pct is not None:
            if not isinstance(brightness_pct, (int, float)) or isinstance(brightness_pct, bool):
                raise SourceConnectionError(f"brightness_pct must be a number 0-100 (got {brightness_pct!r})")
            if not 0 <= brightness_pct <= 100:
                raise SourceConnectionError(f"brightness_pct must be between 0 and 100 (got {brightness_pct!r})")
            state["bri"] = self._bri_from_percent(brightness_pct)
            # Changing brightness only takes effect on a light that's on; default
            # it on unless the caller explicitly asked to turn it off.
            if on is None:
                on = True
        if on is not None:
            if not isinstance(on, bool):
                raise SourceConnectionError(f"on must be true or false (got {on!r})")
            state["on"] = on

        self._put_light_state(light_id, state)
        return {"source": self.source, "device": devices[light_id], "device_id": light_id, "applied": state}

    @staticmethod
    def _resolve_device_id(device, devices):
        """Resolve a device id or exact name to a bridge light id, or raise.

        An id match wins over a name match. A name that isn't unique is rejected
        rather than guessed at, since actuating the wrong light is not a
        recoverable mistake.
        """
        if not isinstance(device, str) or not device.strip():
            raise SourceConnectionError(f"device must be a non-empty light id or name (got {device!r})")
        if device in devices:
            return device
        matches = [light_id for light_id, name in devices.items() if name == device]
        if len(matches) == 1:
            return matches[0]
        available = ", ".join(f"{name!r} (id {light_id})" for light_id, name in sorted(devices.items())) or "(none)"
        if len(matches) > 1:
            raise SourceConnectionError(f"device name {device!r} is ambiguous; use the light id. Devices: {available}")
        raise SourceConnectionError(f"unknown device {device!r}; available devices: {available}")

    def _put_light_state(self, light_id, state):
        """PUT a state body to a light and surface any bridge-reported error.

        Uses the collector's own session/auth and the same TLS-verification
        policy as the reads (``hue.insecure``, default true for a local
        self-signed bridge cert).

        :raises SourceConnectionError: on a transport failure or a bridge error
            (the CLIP API returns 200 with a list of per-key success/error items)
        """
        insecure = self.settings["hue"].get("insecure", True)
        url = f"https://{self.settings['hue']['host']}/api/{self.settings['hue']['user']}/lights/{light_id}/state"
        try:
            with warnings.catch_warnings():
                if insecure:
                    warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
                response = self.session.put(
                    url,
                    json=state,
                    timeout=self.settings["hue"].get("timeout", 5),
                    verify=not insecure,
                )
            response.raise_for_status()
            result = response.json()
        except requests.exceptions.RequestException as e:
            logging.error("Error writing to Hue Bridge - %s", e)
            raise SourceConnectionError(str(e)) from e
        except ValueError as e:
            # response.json() raises on a non-JSON body (requests' JSONDecodeError
            # is a ValueError) - don't let it escape as an unhandled crash.
            logging.error("Hue Bridge returned an unparseable response to a write - %s", e)
            raise SourceConnectionError(f"Hue Bridge returned an unparseable response: {e}") from e
        # The CLIP API always answers a state PUT with a JSON *list* of per-key
        # success/error items. A non-list body is unexpected and must fail cleanly
        # rather than being read as success (an empty error list) by the scan below.
        if not isinstance(result, list):
            logging.error("Hue Bridge returned an unexpected response shape to a write - %.200r", result)
            raise SourceConnectionError(f"Hue Bridge returned an unexpected response: {result!r:.200}")
        errors = [
            item["error"].get("description", str(item["error"]))
            for item in result
            if isinstance(item, dict) and "error" in item
        ]
        if errors:
            logging.error("Hue Bridge rejected a write to light %s - %s", light_id, "; ".join(errors))
            raise SourceConnectionError(f"Hue Bridge rejected the write: {'; '.join(errors)}")
        return result
