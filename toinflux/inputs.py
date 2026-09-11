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

import fcntl
import os
import random
import time
from contextlib import contextmanager
from dataclasses import dataclass

from toinflux.exceptions import ConfigError
from toinflux.general import POLL_FLOOR_KEY, get_class, resolve_state_dir
from toinflux.influx import build_latest_query, resolve_db, run_query, single_series


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


@dataclass(frozen=True)
class InputReading:
    """One input's current value, and how it was obtained.

    The age travels with the value because the caller decides what to do about it. A
    control tolerates its own ``max_age`` and falls to its safe state past that, so a
    reading this module could not refresh is still the most useful thing it can return:
    the alternative is raising, and then the loop cannot tell "too old to act on" from
    "InfluxDB is unreachable", which call for different responses.

    Attributes:
        value (float): the field's value.
        timestamp (float): unix seconds the point was written at, as InfluxDB reports it.
        age (float): seconds between that timestamp and the moment of the read.
        live (bool): whether a live fetch produced it, rather than the stored point.
    """

    value: float
    timestamp: float
    age: float
    live: bool


def stored_reading(session, settings, source, field, instance=None, now=None):
    """Return the newest point InfluxDB holds for one field, or None.

    Reads are ``epoch=s``, so the time column is already unix seconds and there is no
    timestamp format to parse or timezone to get wrong.

    Args:
        session (requests.Session): the session to query through; the caller owns its lifetime
        settings (dict): the whole parsed settings document
        source (str): the source that writes the field
        field (str): the field key to read
        instance (str or None): which producer, for a source that has several
        now (float or None): the clock, for tests; defaults to time.time()

    Returns:
        InputReading or None: the newest point, or None where the measurement holds none

    Raises:
        SourceConnectionError: on a transport or parse failure
        ConfigError: where the source has no usable settings section
    """
    handler = get_class(source, instance=instance)
    measurement = handler.MCP_MEASUREMENT or handler.source
    query = build_latest_query(measurement, handler.mcp_tag_filters(), {field})
    db = resolve_db(handler.source_settings, settings["influx"])
    columns, values = single_series(run_query(session, settings["influx"], db, query))
    if not values:
        return None
    row = values[0]
    index = {name: position for position, name in enumerate(columns)}
    stamp, value = _cell(row, index, "time"), _cell(row, index, field)
    if stamp is None or value is None:
        # A row that carries no time, or no value for the field asked for, is not a
        # reading. Returning it with a substituted age would make a point that does not
        # exist look like a fresh one.
        return None
    moment = time.time() if now is None else now
    return InputReading(value=value, timestamp=float(stamp), age=moment - float(stamp), live=False)


def _cell(row, index, name):
    """Return a named column's value from a result row, or None.

    Nothing guarantees a row is as long as its column list, and a bare index would raise
    IndexError out of a read whose caller is written to expect a missing reading.

    Args:
        row (list): one row from the result series
        index (dict): column name -> position
        name (str): the column wanted

    Returns:
        object or None: the value, or None when the column is absent or the row is short
    """
    position = index.get(name)
    if position is None or position >= len(row):
        return None
    return row[position]


# Bounds on how long a control blocked on a source's fetch lock waits before looking again.
# Randomised because the release is itself a synchronising event: a fixed interval would
# wake every waiter together and re-collide, which is the behaviour the lock exists to stop.
MIN_LOCK_BACKOFF = 0.05
MAX_LOCK_BACKOFF = 0.5


def fetch_lock_path(source, settings_file=None):
    """Return the lock file serialising live fetches of one source.

    One file per source, in the state directory. Per source because that is what the floor
    binds: two controls reading different sources have no reason to wait for each other.

    Args:
        source (str): the source name
        settings_file (str or None): the settings path, for resolving the state directory

    Returns:
        str: the lock file's path, which may not exist yet
    """
    return os.path.join(resolve_state_dir(settings_file), f"fetch-{source}.lock")


@contextmanager
def fetch_lock(  # noqa: DOC403 - a generator, but unannotated
    source, budget, settings_file=None, rng=None, sleep=None, monotonic=None
):
    """Hold a source's fetch lock, or give up after ``budget`` seconds.

    Yields True where the lock was taken and False where it was not, rather than raising,
    because failing to get it is an ordinary outcome with a defined response: use the
    stored value, and fall to the safe state only if that is too old. One wedged fetch must
    not stall every control sharing the source.

    ``flock`` rather than a lock file with a PID in it, because **the kernel releases it
    when the holder dies**. This subsystem is about processes being killed, and a lock
    needing manual cleanup after a SIGKILL would be worse than the problem it solves.

    The first attempt is non-blocking, so an uncontended control sees no delay at all.

    Args:
        source (str): the source whose lock to take
        budget (float): seconds to keep trying before giving up
        settings_file (str or None): the settings path, for resolving the state directory
        rng (random.Random or None): the randomness, injectable for tests
        sleep (callable or None): the sleep, injectable for tests
        monotonic (callable or None): the clock, injectable for tests

    Yields:
        bool: True where the lock is held for the duration of the block
    """
    rng = random.Random() if rng is None else rng
    sleep = time.sleep if sleep is None else sleep
    monotonic = time.monotonic if monotonic is None else monotonic
    path = fetch_lock_path(source, settings_file)
    deadline = monotonic() + budget
    # Opened once and kept open: the lock is on the descriptor, so reopening per attempt
    # would drop it. "a" rather than "w" so a waiter cannot truncate the holder's file.
    with open(path, "a", encoding="utf-8") as handle:
        held = False
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except OSError:
                # Held by another control. Nothing here distinguishes "busy" from a lock
                # this platform cannot take, and both mean the same thing to the caller.
                if monotonic() >= deadline:
                    break
                sleep(min(rng.uniform(MIN_LOCK_BACKOFF, MAX_LOCK_BACKOFF), max(deadline - monotonic(), 0)))
        try:
            yield held
        finally:
            if held:
                fcntl.flock(handle, fcntl.LOCK_UN)
