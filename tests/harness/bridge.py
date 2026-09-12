"""A stub Hue bridge: the subset of the CLIP v1 API this project actually speaks.

Enough of a bridge that ``toinflux.philipshue.Hue`` cannot tell, which is the point - the
process under test uses its own handler, its own authentication and its own error mapping.
It serves HTTPS with a generated self-signed certificate for the same reason a real bridge
does, so the ``hue.insecure`` path is the path exercised.

The record of what was commanded is kept here, on the far end, rather than anywhere the
process under test can write. A control that believes it switched a heater off and did not
is exactly the failure the invariants are looking for, and it cannot be caught by asking
the control.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import time
from dataclasses import dataclass

from tests.harness.endpoints import StubEndpoint

#: What the bridge calls a switchable socket. The collector reads the type rather than
#: inferring one from the state keys, so a stub that omitted it would exercise a different
#: branch from the real thing.
PLUG_TYPE = "On/Off plug-in unit"

#: A dimmable white bulb, for a control whose stages are brightness rather than on/off.
BULB_TYPE = "Dimmable light"


@dataclass
class Command:
    """One state change the bridge was asked to make.

    Attributes:
        at (float): ``time.monotonic()`` when it arrived.
        light (str): the light's id.
        name (str): the light's name, so an assertion reads like the control document.
        state (dict): the state fragment that was PUT.
    """

    at: float
    light: str
    name: str
    state: dict


def plug(name, on=False):
    """Return a bridge light object for an on/off plug.

    Args:
        name (str): the light's name
        on (bool): whether it starts energised

    Returns:
        dict: the light object as the bridge reports it
    """
    return {"name": name, "type": PLUG_TYPE, "state": {"on": bool(on), "reachable": True}}


def bulb(name, on=False, brightness=254):
    """Return a bridge light object for a dimmable white bulb.

    Args:
        name (str): the light's name
        on (bool): whether it starts energised
        brightness (int): the bridge's 1-254 brightness

    Returns:
        dict: the light object as the bridge reports it
    """
    return {
        "name": name,
        "type": BULB_TYPE,
        "state": {"on": bool(on), "bri": int(brightness), "reachable": True},
        "capabilities": {"control": {"mindimlevel": 1000, "maxlumen": 806}},
    }


class StubBridge(StubEndpoint):
    """A Hue bridge with a fixed set of lights and a record of every command.

    Attributes:
        user (str): the API user the bridge accepts; any other gets the CLIP unauthorised
            error, which is a 200 carrying a list rather than an HTTP status.
        lights (dict): light id to light object, mutated in place as commands arrive.
        commands (list): every state change, oldest first.
    """

    def __init__(self, lights=None, user="harness-user", certificate=None):
        """Start a bridge.

        Args:
            lights (dict or None): light id to light object; two plugs, "far" and "near",
                when None
            user (str): the API user to accept
            certificate (tuple or None): an existing ``(certificate_path, key_path)``
        """
        super().__init__(tls=True, certificate=certificate)
        self.user = user
        self.lights = lights if lights is not None else {"1": plug("far"), "2": plug("near")}
        self.commands = []

    def respond(self, request):
        """Answer a bridge request.

        Args:
            request (Request): the request to answer

        Returns:
            tuple: the HTTP status and the body
        """
        parts = [p for p in request.path.split("/") if p]
        if len(parts) < 2 or parts[0] != "api":
            return 404, [{"error": {"type": 3, "address": request.path, "description": "resource not available"}}]
        if parts[1] != self.user:
            # The CLIP API answers an unknown user with a 200 and an error list, which is
            # the branch the handler's list-response guard exists for.
            return 200, [{"error": {"type": 1, "address": "/", "description": "unauthorized user"}}]
        if request.method == "GET" and len(parts) == 2:
            with self.lock:
                return 200, {"lights": {lid: dict(light) for lid, light in self.lights.items()}}
        if request.method == "PUT" and len(parts) == 5 and parts[2] == "lights" and parts[4] == "state":
            return self._set_state(parts[3], request.body)
        return 404, [{"error": {"type": 3, "address": request.path, "description": "resource not available"}}]

    def _set_state(self, light_id, body):
        """Apply a state fragment to one light.

        Args:
            light_id (str): the light to change
            body (dict): the state fragment

        Returns:
            tuple: the HTTP status and the CLIP per-key success list
        """
        if light_id not in self.lights:
            return 200, [
                {
                    "error": {
                        "type": 3,
                        "address": f"/lights/{light_id}",
                        "description": "resource, /lights/" f"{light_id}, not available",
                    }
                }
            ]
        if not isinstance(body, dict):
            return 400, [
                {
                    "error": {
                        "type": 2,
                        "address": f"/lights/{light_id}/state",
                        "description": "body contains " "invalid json",
                    }
                }
            ]
        with self.lock:
            light = self.lights[light_id]
            light["state"].update(body)
            self.commands.append(Command(at=time.monotonic(), light=light_id, name=light["name"], state=dict(body)))
        # One item per key, which is the shape the handler scans for errors.
        return 200, [{"success": {f"/lights/{light_id}/state/{key}": value}} for key, value in body.items()]

    def energised(self):
        """Return which lights are on right now, by name.

        Reading the bridge rather than anything the control reports, because "the heater is
        still on" is a fact about the bridge.

        Returns:
            dict: light name to whether it is on
        """
        with self.lock:
            return {light["name"]: bool(light["state"].get("on")) for light in self.lights.values()}

    def commanded(self, name=None):
        """Return the commands sent to one light, or to all of them, in order.

        Args:
            name (str or None): a light's name, or None for every light

        Returns:
            list: the matching commands
        """
        with self.lock:
            return [c for c in self.commands if name is None or c.name == name]

    def id_of(self, name):
        """Return a light's id from its name.

        Args:
            name (str): the light's name

        Returns:
            str: the light id

        Raises:
            KeyError: no light has that name
        """
        for light_id, light in self.lights.items():
            if light["name"] == name:
                return light_id
        raise KeyError(f"the stub bridge has no light named {name!r}")
