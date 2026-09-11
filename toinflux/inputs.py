"""Reading a control's inputs: InfluxDB first, a live fetch only when it must.

A control loop needs a recent value for each of its declared inputs. It reads them from
InfluxDB rather than from the device, because the collector is already writing them there
and because several controls sharing an input must not each go to the device for it.

A live fetch happens only when the newest stored point is older than that input's
``max_age``, and when it does, the value is written back. That write-back is what makes the
database the shared state: the next control to ask reads what this one just stored, so no
other coordination is needed for the ordinary case.

This module deliberately imports nothing from the MCP layer. A control runs with the MCP
server absent entirely, and importing ``toinflux.mcp_read`` would pull an HTTP server stack
into every control process to ask what the last temperature reading was.

The floor's settings key is ``general.POLL_FLOOR_KEY``, declared there because
``--check-config`` validates it and this module imports ``influx``, which imports
``general``: owning the name here and importing it back would be a cycle.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT"

from toinflux.exceptions import ConfigError
from toinflux.general import POLL_FLOOR_KEY


def resolve_poll_floor(source, settings):
    """Return the minimum seconds between live fetches of a source.

    The source's own ``poll_floor`` where it sets one, and its ``interval`` otherwise: a
    collector already asks the device every ``interval`` seconds, so that cadence is known
    to be acceptable and makes a defensible default. A source that tolerates being asked
    more often sets a lower floor explicitly.

    The floor is per source rather than per control on purpose. Controls are separate
    processes, so a number written in one control's file cannot bind another's behaviour:
    two controls each honouring sixty seconds still reach the device at thirty combined.

    Args:
        source (str): the source name, which must have a settings section
        settings (dict): the whole parsed settings document

    Returns:
        float: the floor in seconds, never negative

    Raises:
        ConfigError: where the source has no settings section, or neither key is usable
    """
    source_cfg = (settings or {}).get(source)
    if not isinstance(source_cfg, dict):
        raise ConfigError(
            f"cannot resolve the live-fetch floor for source {source!r}: it has no settings section. "
            f"Add a {source} section with an interval, or a {POLL_FLOOR_KEY} to set the floor directly"
        )
    if POLL_FLOOR_KEY in source_cfg:
        return _as_seconds(source_cfg[POLL_FLOOR_KEY], f"{source}.{POLL_FLOOR_KEY}")
    return _as_seconds(source_cfg.get("interval"), f"{source}.interval")


def _as_seconds(value, setting):
    """Return a settings value as a non-negative number of seconds.

    A bool is refused rather than accepted as 1 or 0. ``bool`` subclasses ``int``, so
    ``poll_floor: true`` would otherwise validate and then behave as a one-second floor,
    which is not what anyone typing ``true`` meant.

    Args:
        value (object): the raw value from the settings document
        setting (str): the dotted setting name, for the message

    Returns:
        float: the value in seconds

    Raises:
        ConfigError: where the value is missing, not a number, or negative
    """
    if value is None:
        raise ConfigError(f"{setting} is required to resolve the live-fetch floor, and is missing")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{setting} must be a number of seconds (got {value!r})")
    if value < 0:
        raise ConfigError(f"{setting} must not be negative (got {value!r})")
    return float(value)
