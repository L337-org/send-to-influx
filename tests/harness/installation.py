"""A throwaway installation: a settings file, a state directory and control documents.

What a control process needs in order to start, assembled in a temporary directory and
pointed at whichever stub endpoints the scenario built. The process finds its state
directory through ``STATE_DIRECTORY`` exactly as it does under systemd, so the harness
exercises the same resolution the service uses rather than a test-only path.

The settings file is written 0600. The service warns about a readable one at startup, and a
harness that produced that warning would train everyone reading the output to ignore it.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import os

import yaml

from toinflux.controls import CONTROL_DIR_NAME

#: The conservatory from the design note: two heaters on plugs, held at the greater of a
#: target and dew point plus five, actuated as a two-rung ladder.
CONSERVATORY = {
    "name": "conservatory",
    "enabled": True,
    "timezone": "Europe/London",
    "inputs": {
        "inside": {"source": "hue", "field": "conservatory_temperature"},
        "outside": {"source": "openmeteo", "field": "temperature_2m"},
    },
    "parameters": {"target": 18.0},
    "pid": {"input": "inside", "setpoint": "target", "kp": 200.0, "ki": 0.5, "kd": 0.0},
    "devices": {
        "far": {"source": "hue", "device": "far"},
        "near": {"source": "hue", "device": "near"},
    },
    "output": {
        "cycle_seconds": 60,
        "min_transition_seconds": 30,
        "stages": [
            {"level": 0, "set": {"far": False, "near": False}},
            {"level": 750, "set": {"far": True, "near": False}},
            {"level": 1500, "set": {"far": True, "near": True}},
        ],
    },
    "safe_state": "unenergised",
}


def conservatory(**overrides):
    """Return a copy of the example control, with the top-level keys overridden.

    Args:
        **overrides: top-level keys to replace

    Returns:
        dict: a control document
    """
    document = {key: (value.copy() if isinstance(value, dict) else value) for key, value in CONSERVATORY.items()}
    document.update(overrides)
    return document


class Installation:
    """A temporary installation a real control process can be started against.

    Attributes:
        root (str): the directory holding everything.
        settings_file (str): the settings path to pass to a child.
        state_dir (str): what the child sees as its ``STATE_DIRECTORY``.
        bridge (StubBridge or None): the Hue bridge the settings point at.
        influx (StubInflux or None): the InfluxDB the settings point at.
    """

    def __init__(self, root, bridge=None, influx=None, sources=None):
        """Write a settings file and a state directory under ``root``.

        Args:
            root (str): a directory to build the installation in
            bridge (StubBridge or None): the Hue bridge to point ``hue`` at
            influx (StubInflux or None): the InfluxDB to read and write through
            sources (list or None): the ``sources`` list; hue and openmeteo when None
        """
        self.root = str(root)
        self.bridge = bridge
        self.influx = influx
        self.state_dir = os.path.join(self.root, "state")
        os.makedirs(os.path.join(self.state_dir, CONTROL_DIR_NAME), exist_ok=True)
        self.settings_file = os.path.join(self.root, "settings.yaml")
        self._settings = {
            "sources": list(sources) if sources is not None else ["hue", "openmeteo"],
            "openmeteo": {
                "db": "weather",
                "interval": 900,
                "latitude": 51.5,
                "longitude": -0.1,
                "fields": ["temperature_2m", "dew_point_2m"],
            },
            "influx": {
                "url": influx.url if influx else "http://127.0.0.1:1",
                "user": "harness",
                "password": "harness",
                "timeout": 5,
            },
        }
        if bridge is not None:
            self._settings["hue"] = {
                "db": "hue_db",
                "host": bridge.host,
                "user": bridge.user,
                "timeout": 5,
                "interval": 300,
            }
        self._write_settings()

    def _write_settings(self) -> None:
        """Write the settings document, readable only by its owner."""
        with open(self.settings_file, "w", encoding="utf-8") as handle:
            yaml.safe_dump(self._settings, handle, sort_keys=False)
        os.chmod(self.settings_file, 0o600)

    @property
    def settings(self):
        """Return the settings document as written.

        Returns:
            dict: the parsed settings
        """
        return self._settings

    def set_settings(self, section, **values) -> None:
        """Change one settings section and rewrite the file.

        Args:
            section (str): the top-level key
            **values: the keys to set within it
        """
        self._settings.setdefault(section, {}).update(values)
        self._write_settings()

    def write_control(self, document, name=None):
        """Write one control document into the state directory.

        Validated before it is written, because a scenario that silently ran against an
        invalid control would report the control subsystem's complaint as its own result.

        Args:
            document (dict): the control document
            name (str or None): the control's name; the document's own ``name`` when None

        Returns:
            str: the path written

        Raises:
            AssertionError: the document is not structurally valid
        """
        from toinflux.controls import validate_control

        name = name or document["name"]
        errors = validate_control(name, document)
        assert not errors, f"the harness wrote an invalid control {name!r}: {errors}"
        path = os.path.join(self.state_dir, CONTROL_DIR_NAME, f"{name}.yaml")
        with open(path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(document, handle, sort_keys=False)
        return path

    def environment(self, **extra):
        """Return the environment to start a child process with.

        ``PYTHONPATH`` is cleared deliberately: a stray one on the developer's machine
        leaks a different site-packages into the child and the child then tests something
        other than this checkout.

        Args:
            **extra: further variables to set

        Returns:
            dict: the child's environment
        """
        return {**os.environ, "STATE_DIRECTORY": self.state_dir, "PYTHONPATH": "", **extra}
