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
from toinflux.exceptions import SourceConnectionError, ToolParamError


class Hue(DataHandler):
    """Child class of DataHandler to get data from a Hue Bridge"""

    MCP_DESCRIPTION = "Philips Hue: lights and smart plugs (on/off, brightness) and motion/temperature/light sensors."

    # Hue is the one v1 source with a documented, buildable device-write path
    # (PUT /api/{user}/lights/{id}/state on the same session/auth the collector
    # already uses). The MCP write tool is still only registered when the
    # operator sets hue.mcp_read_write: true - see DataHandler.mcp_write_enabled.
    MCP_WRITABLE = True

    # Hue brightness ("bri") is 1-254; the MCP tool speaks 0-100 % and maps here.
    HUE_BRI_MIN = 1
    HUE_BRI_MAX = 254

    # Hue colour temperature ("ct") is in mireds/mirek (= 1e6 / kelvin). The
    # standard range is 153 mirek (6535 K, coolest) to 500 mirek (2000 K,
    # warmest); a light reporting its own capabilities.control.ct range overrides
    # this. The MCP tool speaks kelvin and converts here.
    HUE_CT_MIN = 153
    HUE_CT_MAX = 500

    # Friendly colour names the write tool accepts alongside an "#rrggbb" hex.
    _HUE_COLOR_NAMES = {
        "red": "ff0000",
        "orange": "ff8800",
        "yellow": "ffff00",
        "green": "00ff00",
        "cyan": "00ffff",
        "blue": "0000ff",
        "purple": "8000ff",
        "magenta": "ff00ff",
        "pink": "ff69b4",
        "white": "ffffff",
        "warm white": "ffd6aa",
        "cool white": "f0f8ff",
    }

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
        except ValueError as e:
            # response.json() raises on a non-JSON body (e.g. an HTML error page).
            # requests' own JSONDecodeError is BOTH a ValueError and a
            # RequestException, so this must be caught before the RequestException
            # handler below - otherwise a parse failure would be misreported as a
            # transport "connection" error. (Guards both the collector read path
            # and the MCP write tools' device discovery, which share this method.)
            logging.error("Hue Bridge returned an unparseable response - %s", e)
            raise SourceConnectionError(f"Hue Bridge returned an unparseable response: {e}") from e
        except requests.exceptions.RequestException as e:
            logging.error("Error connecting to Hue Bridge - %s", e)
            raise SourceConnectionError(str(e)) from e
        # A successful GET returns a dict (sensors/lights); a list only ever comes
        # back on error. Guard the indexing: an empty list, or a list whose first
        # item isn't the documented {"error": {...}} shape, is unexpected and must
        # fail cleanly rather than raise IndexError/KeyError unhandled.
        if isinstance(hue_data, list):
            first = hue_data[0] if hue_data else None
            error = first.get("error") if isinstance(first, dict) else None
            # The "error" value should itself be a dict with a "description"; guard
            # it too, so a malformed error shape still fails cleanly rather than
            # raising AttributeError from .get() on a non-dict.
            if isinstance(error, dict):
                description = error.get("description", str(error))
            else:
                description = f"unexpected list response: {hue_data!r:.200}"
            logging.error("Error connecting to Hue Bridge - %s", description)
            raise SourceConnectionError(description)
        # A successful GET is a dict (sensors/lights). A non-dict, non-list body - a
        # JSON scalar/null, e.g. from a misconfigured proxy - is unexpected; fail
        # cleanly here rather than returning it for a caller (parse_hue_data /
        # _fetch_lights) to crash on with a TypeError/AttributeError.
        if not isinstance(hue_data, dict):
            logging.error("Hue Bridge returned an unexpected response type - %.200r", hue_data)
            raise SourceConnectionError(f"Hue Bridge returned an unexpected response type: {hue_data!r:.200}")
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

    def _fetch_lights(self):
        """Return ``{light_id(str): light_object(dict)}`` for every light/plug the
        bridge exposes, via the collector's own authenticated GET. The light
        objects carry the ``state``/``type``/``capabilities`` used to resolve a
        target and check what it can do.

        :raises SourceConnectionError: if the bridge can't be reached
        """
        hue_data = self.get_data_from_hue_bridge()
        return {str(lid): light for lid, light in hue_data.get("lights", {}).items() if isinstance(light, dict)}

    @staticmethod
    def _names_by_id(lights):
        """Return ``{id: name}`` from a ``{id: light_object}`` map (a missing or
        blank name falls back to the id), for name/id resolution and error text."""
        return {light_id: str(light.get("name") or light_id) for light_id, light in lights.items()}

    @classmethod
    def _light_capabilities(cls, light):
        """Derive what a light can do from its bridge object.

        A Hue install spans (at least) three tiers - on/off-or-dimmable white,
        colour-temperature, and full colour - so brightness, colour temperature
        and colour are three *independent* capabilities, checked separately. They
        are inferred from the light's ``state`` keys (``bri``/``ct``/``xy`` etc.),
        cross-checked against ``capabilities.control`` where the bridge reports it;
        the ``ct`` mired range comes from ``capabilities.control.ct`` when present,
        else the standard range.

        :return: ``{"brightness","color_temp","color": bool, "ct_range": (min,max)|None}``
        :rtype: dict
        """
        state = light.get("state") if isinstance(light.get("state"), dict) else {}
        state = state or {}
        caps = light.get("capabilities")
        control = caps["control"] if isinstance(caps, dict) and isinstance(caps.get("control"), dict) else {}
        supports_brightness = "bri" in state
        supports_color_temp = "ct" in state or "ct" in control
        supports_color = any(k in state for k in ("xy", "hue", "sat")) or bool(control.get("colorgamut"))
        ct_range = None
        if supports_color_temp:
            ctrl_ct = control.get("ct") if isinstance(control.get("ct"), dict) else None
            if ctrl_ct and isinstance(ctrl_ct.get("min"), int) and isinstance(ctrl_ct.get("max"), int):
                ct_range = (ctrl_ct["min"], ctrl_ct["max"])
            else:
                ct_range = (cls.HUE_CT_MIN, cls.HUE_CT_MAX)
        return {
            "brightness": supports_brightness,
            "color_temp": supports_color_temp,
            "color": supports_color,
            "ct_range": ct_range,
        }

    @staticmethod
    def _kelvin_to_mirek(kelvin):
        """Convert a colour temperature in kelvin to Hue's mired/mirek scale."""
        return round(1_000_000 / kelvin)

    @staticmethod
    def _mirek_to_kelvin(mirek):
        """Convert Hue's mired/mirek scale to kelvin (for the capability listing)."""
        return round(1_000_000 / mirek)

    @classmethod
    def _color_to_xy(cls, color):
        """Convert a colour (an ``#rrggbb``/``rrggbb`` hex or a known name) to a
        Hue CIE ``xy`` pair.

        :raises ToolParamError: the value isn't a hex colour or a known name
        """
        if isinstance(color, str) and color.strip():
            hex_str = cls._HUE_COLOR_NAMES.get(color.strip().lower(), color.strip()).lstrip("#").lower()
            if len(hex_str) == 6 and all(c in "0123456789abcdef" for c in hex_str):
                r, g, b = (int(hex_str[i : i + 2], 16) for i in (0, 2, 4))
                return cls._rgb_to_xy(r, g, b)
        raise ToolParamError(
            f"color must be an RGB hex like '#ff8800' or a known colour name (got {color!r}); "
            f"names: {', '.join(sorted(cls._HUE_COLOR_NAMES))}"
        )

    @staticmethod
    def _rgb_to_xy(r, g, b):
        """Convert 0-255 sRGB to a Hue CIE ``[x, y]`` pair (gamma-corrected sRGB ->
        XYZ -> xy chromaticity; the bridge clamps to the light's own gamut)."""

        def _linear(channel):
            c = channel / 255
            return ((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92

        lr, lg, lb = _linear(r), _linear(g), _linear(b)
        x = lr * 0.4124 + lg * 0.3576 + lb * 0.1805
        y = lr * 0.2126 + lg * 0.7152 + lb * 0.0722
        z = lr * 0.0193 + lg * 0.1192 + lb * 0.9505
        total = x + y + z
        if total == 0:
            return [0.0, 0.0]
        return [round(x / total, 4), round(y / total, 4)]

    def mcp_list_writable_devices(self):
        """Return the controllable Hue lights/plugs, each with its id, name and the
        controls it supports - the write allowlist and the model's discovery of
        what each device can actually do (so it doesn't ask a white bulb for a
        colour). Reuses the collector's own authenticated bridge GET.

        :return: list of ``{"id", "name", "controls": [...]}`` (plus
            ``"color_temp_range_k": [min, max]`` for colour-temperature lights),
            sorted by id
        :rtype: list
        :raises SourceConnectionError: if the bridge can't be reached
        """
        lights = self._fetch_lights()
        out = []
        for light_id, light in sorted(lights.items()):
            caps = self._light_capabilities(light)
            controls = ["on_off"]
            for control in ("brightness", "color_temp", "color"):
                if caps[control]:
                    controls.append(control)
            entry = {"id": light_id, "name": str(light.get("name") or light_id), "controls": controls}
            if caps["color_temp"] and caps["ct_range"]:
                lo, hi = caps["ct_range"]
                entry["color_temp_range_k"] = [self._mirek_to_kelvin(hi), self._mirek_to_kelvin(lo)]
            out.append(entry)
        return out

    def _bri_from_percent(self, percent):
        """Map a 0-100 brightness percentage to Hue's 1-254 ``bri`` scale.

        0 % clamps to the minimum on-brightness rather than off - turning a light
        off is expressed with ``on=False``, not brightness 0, so the two controls
        stay independent and unambiguous.
        """
        scaled = round(percent / 100 * self.HUE_BRI_MAX)
        return max(self.HUE_BRI_MIN, min(scaled, self.HUE_BRI_MAX))

    def mcp_set_device_state(self, device, *, on=None, brightness_pct=None, color_temp_k=None, color=None):
        """Set a Hue light/plug's state, the MCP write action for this source.

        Resolves ``device`` (a bridge light id or its exact name) against the live
        device list, then builds and PUTs the Hue state body from the friendly
        parameters. It is *capability-aware per capability*: brightness, colour
        temperature and colour are independent, and asking for one the target light
        doesn't have is rejected (naming the device) rather than silently ignored.
        Setting brightness, colour temperature or colour implies turning the light
        on unless ``on`` is given explicitly, since the bridge ignores those on an
        off light.

        :param device: the target light id or its exact bridge name
        :type device: str
        :param on: turn the device on (True) or off (False); None leaves it
        :type on: bool or None
        :param brightness_pct: target brightness 0-100 %; None leaves it
        :type brightness_pct: int or float or None
        :param color_temp_k: white colour temperature in kelvin (e.g. 2700 warm,
            6500 cool), clamped to the light's supported range; None leaves it
        :type color_temp_k: int or float or None
        :param color: a colour as an ``#rrggbb`` hex or a known name; None leaves it
        :type color: str or None
        :return: a summary dict of what was applied
        :rtype: dict
        :raises ToolParamError: a caller/model input mistake - nothing to set, both
            colour and colour temperature at once, an invalid value, a capability
            the device lacks, or an unknown/ambiguous device (not retryable)
        :raises SourceConnectionError: a bridge/transport failure reaching the
            device list or PUTting the change (retryable)
        """
        if on is None and brightness_pct is None and color_temp_k is None and color is None:
            raise ToolParamError(
                "nothing to set: provide at least one of 'on', 'brightness_pct', 'color_temp_k', 'color'"
            )
        # ct and xy are mutually exclusive on the bridge (a light is in one mode);
        # asking for both is a caller mistake, not something to silently pick from.
        if color_temp_k is not None and color is not None:
            raise ToolParamError("set either 'color_temp_k' or 'color', not both (a light is one or the other)")

        lights = self._fetch_lights()
        names = self._names_by_id(lights)
        light_id = self._resolve_device_id(device, names)
        name = names[light_id]
        caps = self._light_capabilities(lights[light_id])

        state = {}
        if brightness_pct is not None:
            state.update(self._brightness_state(name, caps, brightness_pct))
        if color_temp_k is not None:
            state.update(self._color_temp_state(name, caps, color_temp_k))
        if color is not None:
            state.update(self._color_state(name, caps, color))
        if on is not None:
            if not isinstance(on, bool):
                raise ToolParamError(f"on must be true or false (got {on!r})")
            state["on"] = on
        elif state:
            # Brightness/ct/xy only take effect on a light that's on; default it on
            # unless the caller explicitly asked to turn it off.
            state["on"] = True

        self._put_light_state(light_id, state)
        return {"source": self.source, "device": name, "device_id": light_id, "applied": state}

    def _brightness_state(self, name, caps, brightness_pct):
        """Validate a brightness request against the light and return ``{"bri": ...}``.

        :raises ToolParamError: the light isn't dimmable, or the value is invalid
        """
        if not caps["brightness"]:
            raise ToolParamError(f"device {name!r} does not support brightness (it is on/off only)")
        if not isinstance(brightness_pct, (int, float)) or isinstance(brightness_pct, bool):
            raise ToolParamError(f"brightness_pct must be a number 0-100 (got {brightness_pct!r})")
        if not 0 <= brightness_pct <= 100:
            raise ToolParamError(f"brightness_pct must be between 0 and 100 (got {brightness_pct!r})")
        return {"bri": self._bri_from_percent(brightness_pct)}

    def _color_temp_state(self, name, caps, color_temp_k):
        """Validate a colour-temperature request and return ``{"ct": ...}`` (kelvin
        converted to mirek and clamped to the light's supported range).

        :raises ToolParamError: the light lacks colour temperature, or the value is invalid
        """
        if not caps["color_temp"]:
            raise ToolParamError(f"device {name!r} does not support colour temperature")
        if not isinstance(color_temp_k, (int, float)) or isinstance(color_temp_k, bool) or color_temp_k <= 0:
            raise ToolParamError(f"color_temp_k must be a positive number in kelvin (got {color_temp_k!r})")
        lo, hi = caps["ct_range"]
        return {"ct": max(lo, min(self._kelvin_to_mirek(color_temp_k), hi))}

    def _color_state(self, name, caps, color):
        """Validate a colour request and return ``{"xy": [...]}``.

        :raises ToolParamError: the light lacks colour, or the colour is invalid
        """
        if not caps["color"]:
            raise ToolParamError(f"device {name!r} does not support colour")
        return {"xy": self._color_to_xy(color)}

    @staticmethod
    def _resolve_device_id(device, devices):
        """Resolve a device id or exact name to a bridge light id, or raise.

        An id match wins over a name match. A name that isn't unique is rejected
        rather than guessed at, since actuating the wrong light is not a
        recoverable mistake.

        :raises ToolParamError: the device is empty, unknown, or an ambiguous name
        """
        if not isinstance(device, str) or not device.strip():
            raise ToolParamError(f"device must be a non-empty light id or name (got {device!r})")
        if device in devices:
            return device
        matches = [light_id for light_id, name in devices.items() if name == device]
        if len(matches) == 1:
            return matches[0]
        available = ", ".join(f"{name!r} (id {light_id})" for light_id, name in sorted(devices.items())) or "(none)"
        if len(matches) > 1:
            raise ToolParamError(f"device name {device!r} is ambiguous; use the light id. Devices: {available}")
        raise ToolParamError(f"unknown device {device!r}; available devices: {available}")

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
        except ValueError as e:
            # response.json() on a non-JSON body raises requests' JSONDecodeError,
            # which is BOTH a ValueError and a RequestException - catch it before
            # the RequestException handler so a parse failure isn't misreported as
            # a transport error. (raise_for_status()'s HTTPError is a
            # RequestException but not a ValueError, so it still falls through.)
            logging.error("Hue Bridge returned an unparseable response to a write - %s", e)
            raise SourceConnectionError(f"Hue Bridge returned an unparseable response: {e}") from e
        except requests.exceptions.RequestException as e:
            logging.error("Error writing to Hue Bridge - %s", e)
            raise SourceConnectionError(str(e)) from e
        # The CLIP API always answers a state PUT with a JSON *list* of per-key
        # success/error items. A non-list body is unexpected and must fail cleanly
        # rather than being read as success (an empty error list) by the scan below.
        if not isinstance(result, list):
            logging.error("Hue Bridge returned an unexpected response shape to a write - %.200r", result)
            raise SourceConnectionError(f"Hue Bridge returned an unexpected response: {result!r:.200}")
        # Guard item["error"] being a non-dict (a malformed bridge/proxy response):
        # fall back to its string form rather than crashing on .get(), mirroring the
        # read path's defensive handling in get_data_from_hue_bridge().
        errors = [
            (
                item["error"].get("description", str(item["error"]))
                if isinstance(item["error"], dict)
                else str(item["error"])
            )
            for item in result
            if isinstance(item, dict) and "error" in item
        ]
        if errors:
            logging.error("Hue Bridge rejected a write to light %s - %s", light_id, "; ".join(errors))
            raise SourceConnectionError(f"Hue Bridge rejected the write: {'; '.join(errors)}")
        return result
