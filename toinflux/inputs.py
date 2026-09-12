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

The floor's settings key is ``general.MINIMUM_INTERVAL_KEY``, declared there because
``--check-config`` validates it and this module imports ``influx``, which imports
``general``: owning the name here and importing it back would be a cycle.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT"

import fcntl
import logging
import math
import os
import random
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass

from toinflux.exceptions import ConfigError, SourceConnectionError, ToolParamError
from toinflux.general import MINIMUM_INTERVAL_KEY, get_class, resolve_state_dir, source_class
from toinflux.influx import InfluxWriteError, build_latest_query, resolve_db, run_query, single_series


def resolve_minimum_interval(source, settings):
    """Return the minimum seconds between live fetches of a source.

    Three answers in order: the operator's ``minimum_interval`` for that source, then the
    source class's own ``MINIMUM_INTERVAL``, then its collection ``interval``.

    The class's value is the real one, and it belongs there because it describes what the
    far end tolerates rather than how often this operator wants data. Someone collecting
    Open-Meteo every six hours has said nothing about how often its API may be asked, so
    reading the floor off ``interval`` would be hours wrong in either direction. The
    operator override exists because only they know their own estate: a Hue bridge on a
    congested network may want more room than the class assumes.

    The floor is per source rather than per control on purpose. Controls are separate
    processes, so a number written in one control's file cannot bind another's behaviour:
    two controls each honouring sixty seconds still reach the device at thirty combined.

    Args:
        source (str): the source name in any case, which must have a settings section
        settings (dict): the whole parsed settings document

    Returns:
        float: the floor in seconds, never negative

    Raises:
        ConfigError: where the source has no settings section, or neither key is usable
    """
    # A source name is case-insensitive across this project - get_class() says so and
    # lowercases before constructing - while settings sections are canonically lowercase,
    # because validate_settings() matches them against known_sources(). Normalising here
    # rather than at one call site keeps every caller on the same convention.
    source = source.lower()
    source_cfg = (settings or {}).get(source)
    if not isinstance(source_cfg, dict):
        raise ConfigError(
            f"cannot resolve the live-fetch floor for source {source!r}: it has no settings section. "
            f"Add a {source!r} section with an 'interval' in it, or set "
            f"{f'{source}.{MINIMUM_INTERVAL_KEY}'!r} there to give the floor directly"
        )
    if MINIMUM_INTERVAL_KEY in source_cfg:
        return _as_seconds(source_cfg[MINIMUM_INTERVAL_KEY], f"{source}.{MINIMUM_INTERVAL_KEY}")
    declared = source_class(source).MINIMUM_INTERVAL
    if declared is not None:
        return _as_seconds(declared, f"{source}'s built-in minimum interval")
    # Nothing declared, which no shipped source does. The collection interval is a safe
    # answer rather than a good one: it is the operator's cadence, not what the far end
    # tolerates, so it can be hours out in either direction.
    return _as_seconds(source_cfg.get("interval"), f"{source}.interval")


def _as_seconds(value, setting):
    """Return a settings value as a non-negative number of seconds.

    A bool is refused rather than accepted as 1 or 0. ``bool`` subclasses ``int``, so
    ``minimum_interval: true`` would otherwise validate and then behave as a one-second floor,
    which is not what anyone typing ``true`` meant.

    Args:
        value (object): the raw value from the settings document
        setting (str): what to call it in a message. Every duration this module reads comes
            through here - the floor, an input's max_age, the lock budget - so the caller
            names the one it is checking and the messages stay true for all three.

    Returns:
        float: the value in seconds

    Raises:
        ConfigError: where the value is missing, not a number, or negative
    """
    # Quoted because the setting name is built from a source name, and that arrives from a
    # control document an MCP client writes. general.py's validator interpolates the same
    # names bare, matching every message around it, where they come from the operator's own
    # sources: list instead - a different provenance rather than an inconsistency.
    if value is None:
        raise ConfigError(f"{setting!r} is required, and is missing")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{setting!r} must be a number of seconds (got {value!r})")
    # YAML .nan and .inf parse to floats and survive every check above. Neither fails
    # loudly later, which is the problem: a nan floor compares False against any age, so
    # the floor never holds and every cycle goes live; an inf floor compares True, so it
    # never does. A nan used as a wait budget is worse - see fetch_lock, where the deadline
    # comparison is False forever and the loop cannot exit.
    if not math.isfinite(value):
        raise ConfigError(f"{setting!r} must be a finite number of seconds (got {value!r})")
    if value < 0:
        raise ConfigError(f"{setting!r} must not be negative (got {value!r})")
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


@contextmanager
def source_handler(source, settings_file=None, instance=None):  # noqa: DOC403 - a generator, but unannotated
    """Yield a handler for a source and close the session it opened.

    ``DataHandler.__init__`` opens a ``requests.Session`` whether or not anything uses it,
    and nothing closes it. A collector builds one handler for the life of the process, so
    that never mattered; a control loop reads its inputs every cycle, and one unclosed
    session per read is a socket and a file descriptor per read.

    Whoever opens a handler closes it. The alternative - letting each function build its
    own and hoping - is how the leak got here in the first place.

    Args:
        source (str): the source to build a handler for
        settings_file (str or None): the settings path, so the handler reads the same document
        instance (str or None): which producer, for a source that has several

    Yields:
        DataHandler: the handler, valid for the duration of the block
    """
    # Keywords at every call site. This module's functions do not agree on the order of
    # these two - stored_reading takes instance first, this takes settings_file first - and
    # transposing them here would drop settings_file into instance, which is the bug already
    # fixed once in this module: the handler reading a different settings document from the
    # caller, invisible until someone runs with -s.
    handler = get_class(source, settings_file=settings_file, instance=instance)
    try:
        yield handler
    finally:
        opened = getattr(handler, "session", None)
        if opened is not None:
            opened.close()


def stored_reading(session, settings, source, field, instance=None, settings_file=None, now=None):
    """Return the newest point InfluxDB holds for one field, or None.

    Reads are ``epoch=s``, so the time column is already unix seconds and there is no
    timestamp format to parse or timezone to get wrong.

    Args:
        session (requests.Session): the session to query through; the caller owns its lifetime
        settings (dict): the whole parsed settings document
        source (str): the source that writes the field
        field (str): the field key to read
        instance (str or None): which producer, for a source that has several
        settings_file (str or None): the settings path the caller was started with, so the
            handler this builds reads the same document the ``settings`` argument came from
        now (float or None): the clock, for tests; defaults to time.time()

    Returns:
        InputReading or None: the newest point, or None where the measurement holds none

    Raises:
        SourceConnectionError: on a transport or parse failure
        ConfigError: where the source has no usable settings section
    """
    # Before the handler, not after. Building one loads the settings document, and a field
    # name this can never read is wrong whatever the settings say - checking second meant a
    # missing settings.yaml masked the real complaint, which is how CI found this.
    _refuse_reserved_field(field)
    with source_handler(source, settings_file=settings_file, instance=instance) as handler:
        return handler_reading(session, settings, handler, field, now)


def _default_max_age(source):
    """Return how long a source's readings stay worth acting on.

    The source class's ``DEFAULT_MAX_AGE``. Every shipped source declares one, and a hygiene
    test says so; a source under development that has not gets the conservative answer,
    since acting on data of unknown age is the failure this bound exists to prevent.

    Args:
        source (str): the source name, in any case

    Returns:
        float: seconds
    """
    declared = source_class(source).DEFAULT_MAX_AGE
    if declared is None:
        return 0.0
    return float(declared)


def _required(spec, key):
    """Return one key of an input declaration, or say which is missing.

    The control store validates a document at --check-config, so a spec reaching here is
    normally complete. This keeps the promise in the docstring true anyway: everything
    wrong with a declaration arrives as a ConfigError rather than as a KeyError traceback
    from somewhere further in.

    Args:
        spec (dict): one input declaration
        key (str): the key wanted

    Returns:
        object: the value

    Raises:
        ConfigError: where the key is absent
    """
    if key not in spec:
        raise ConfigError(f"control input declaration is missing {key!r}")
    return spec[key]


def _refuse_reserved_field(field):
    """Refuse a field name that cannot be told apart from the timestamp column.

    A result carries its timestamp in a column called ``time``, so a field of the same name
    gives two columns with one name and nothing in the response to say which is which.
    Reading the wrong one produces an age rather than an error, and a wrong age is the one
    thing a control must not be handed quietly.

    Whether InfluxDB will store such a field is not a question this checkout can answer,
    and it does not change the answer: unreachable if it cannot, unreadable here if it can.

    Args:
        field (str): the field key a control asked for

    Raises:
        ConfigError: where the name is the timestamp column's
    """
    if field == TIME_COLUMN:
        raise ConfigError(
            f"control input cannot read a field named {TIME_COLUMN!r}: a result's timestamp "
            f"column has that name too, so the two cannot be told apart"
        )


def handler_reading(session, settings, handler, field, now=None):
    """Return the newest stored point for a field, using a handler the caller owns.

    Split from :func:`stored_reading` so a caller reading the same source twice - which
    ``read_input`` does either side of the fetch lock - builds one handler rather than one
    per read.

    Args:
        session (requests.Session): the session to query through
        settings (dict): the whole parsed settings document
        handler (DataHandler): the source's handler, already scoped to the instance
        field (str): the field key to read
        now (float or None): the clock, for tests; defaults to time.time()

    Returns:
        InputReading or None: the newest point, or None where the measurement holds none

    Raises:
        SourceConnectionError: on a transport or parse failure
        ConfigError: where the field name cannot go into a query, or collides with the
            timestamp column
    """
    _refuse_reserved_field(field)
    measurement = handler.MCP_MEASUREMENT or handler.source
    try:
        query = build_latest_query(measurement, handler.mcp_tag_filters(), {field})
    except ToolParamError as exc:
        # Quoted, like every other exception this module reports: the message carries the
        # field name straight from a control document, so a newline in it would otherwise
        # write its own line into the journal. influx.py does the same with !r.
        #
        # The identifier check refuses a field name carrying a control character, and says so
        # as ToolParamError because its other caller is an MCP tool taking a model's argument.
        # Here the name came from a control document, so it is a configuration fault: the
        # control must not start, and no retry helps. Re-typed rather than documented as-is,
        # because a caller catching ConfigError to mean "stop" would otherwise miss it.
        raise ConfigError(f"control input names an unusable field: {exc!r}") from exc
    db = resolve_db(handler.source_settings, settings["influx"])
    columns, values = single_series(run_query(session, settings["influx"], db, query))
    if not values:
        return None
    row = values[0]
    index = {name: position for position, name in enumerate(columns)}
    stamp, value = _cell(row, index, TIME_COLUMN), _cell(row, index, field)
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


# Locks live in their own directory under the state directory, as control documents do.
# Off systemd the state directory is wherever settings.yaml is, which for a source checkout
# is the repository root, and loose fetch-<source>.lock files landing there are easy to
# commit by accident. One directory also makes one ignore rule enough.
LOCK_DIR_NAME = "locks"

# The column InfluxDB returns a point's timestamp in. Named because a field key equal to it
# is ambiguous rather than merely awkward, and handler_reading refuses one.
TIME_COLUMN = "time"


def fetch_lock_path(source, settings_file=None):
    """Return the lock file serialising live fetches of one source.

    One file per source. Per source because that is what the floor binds: two controls
    reading different sources have no reason to wait for each other.

    Args:
        source (str): the source name, in any case
        settings_file (str or None): the settings path the caller was started with. Used for
            the state directory *and* passed to the handler, so it reads the same document
            the ``settings`` argument came from. Omitting it where the caller is not on the
            default settings.yaml leaves the handler on a different one from the queries.

    Returns:
        str: the lock file's path, which may not exist yet
    """
    # Lowercased for the same reason resolve_minimum_interval() does it, and here it is the
    # difference between serialising and not: two controls naming the same source in
    # different cases would otherwise take two different locks and both go live.
    return os.path.join(resolve_state_dir(settings_file), LOCK_DIR_NAME, f"fetch-{source.lower()}.lock")


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
        settings_file (str or None): the settings path the caller was started with. Used for
            the state directory *and* passed to the handler, so it reads the same document
            the ``settings`` argument came from. Omitting it where the caller is not on the
            default settings.yaml leaves the handler on a different one from the queries.
        rng (random.Random or None): the randomness, injectable for tests
        sleep (callable or None): the sleep, injectable for tests
        monotonic (callable or None): the clock, injectable for tests

    Yields:
        bool: True where the lock is held for the duration of the block

    Raises:
        ConfigError: where the budget is not a finite number of seconds, or the lock
            directory cannot be created
    """
    # Validated here as well as by read_input, because this is a public helper and the
    # failure it prevents is a hang: a non-finite budget makes the deadline non-finite, and
    # monotonic() >= nan is False for ever, so the wait loop can never exit.
    budget = _as_seconds(budget, "fetch lock budget")
    rng = random.Random() if rng is None else rng
    sleep = time.sleep if sleep is None else sleep
    monotonic = time.monotonic if monotonic is None else monotonic
    path = fetch_lock_path(source, settings_file)
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, exist_ok=True)
        # 0700 for the same reason the control directory sets it: systemd already creates
        # the state directory that way, but a source checkout puts this beside settings.yaml
        # where the default is whatever the umask allows.
        os.chmod(directory, stat.S_IRWXU)
    except OSError as exc:
        raise ConfigError(f"cannot create the fetch lock directory {directory!r}: {exc!r}") from exc
    deadline = monotonic() + budget
    # Opened once and kept open: the lock is on the descriptor, so reopening per attempt
    # would drop it. "a" rather than "w" so a waiter cannot truncate the holder's file.
    try:
        # Wrapped for the same reason the makedirs above is: an OSError crossing this
        # boundary breaks the documented contract and reaches an operator as a traceback
        # rather than as a message naming the file it could not open.
        handle = open(path, "a", encoding="utf-8")  # noqa: SIM115 - closed by the with below
    except OSError as exc:
        raise ConfigError(f"cannot open the fetch lock {path!r}: {exc!r}") from exc
    with handle:
        held = _acquire(handle, path, source, deadline, rng, sleep, monotonic)
        try:
            yield held
        finally:
            if held:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _acquire(handle, path, source, deadline, rng, sleep, monotonic):
    """Try for the lock until the deadline, and say whether it was taken.

    Split out of :func:`fetch_lock` only to keep that function within the project's
    cyclomatic complexity limit, as ``_send_buffered`` is split out of ``send_data``.

    Args:
        handle (io.TextIOWrapper): the open lock file; the lock is on this descriptor
        path (str): the lock file's path, for the message
        source (str): the source being locked, for the message
        deadline (float): the monotonic time to give up at
        rng (random.Random): the randomness for the backoff
        sleep (callable): the sleep
        monotonic (callable): the clock

    Returns:
        bool: True where the lock is now held

    Raises:
        ConfigError: where locking cannot work here at all
    """
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            # Contention, and only contention. LOCK_NB reports a lock someone else holds as
            # EAGAIN, which Python raises as BlockingIOError - measured, not assumed.
            if monotonic() >= deadline:
                return False
            sleep(min(rng.uniform(MIN_LOCK_BACKOFF, MAX_LOCK_BACKOFF), max(deadline - monotonic(), 0)))
        except OSError as exc:
            # Anything else is the lock not working rather than being held: a filesystem
            # that does not support flock, a bad descriptor. Retried as contention it would
            # look identical to a busy lock, so every control would wait its whole budget,
            # report "busy", and carry on with serialisation silently switched off - a lock
            # that appears to work and guarantees nothing.
            raise ConfigError(f"cannot lock {path!r} to serialise live fetches of {source!r}: {exc!r}") from exc


def read_input(session, settings, spec, settings_file=None, now=None):
    """Return the current value of one declared control input.

    InfluxDB first. A live fetch happens only when the stored point is older than this
    input can use, and it writes its result back, which is what lets the next control read
    it rather than going to the device itself.

    **The poll floor takes precedence over the input's own max_age.** The trigger is
    whichever is larger, and that is the same thing as "do not fetch if the newest point is
    younger than the floor" precisely because a live fetch writes back: a control asking
    sooner reads the value the previous one stored. Wanting fresher data than the floor
    allows is not an error, it just does not get it - and if the value is then too stale to
    act on, that is the control's fail-safe rather than this function's problem.

    A source that declares ``MCP_LIVE_STATE = False`` is never fetched live. Octopus is a
    day behind and Speedtest is expensive, so going to the device would cost something and
    return nothing fresher.

    Args:
        session (requests.Session): the session to read through; the caller owns its lifetime
        settings (dict): the whole parsed settings document
        spec (dict): one input declaration. ``source`` and ``field`` are required;
            ``max_age`` and ``instance`` are optional, matching what the control store
            validates. An absent ``max_age`` leaves the source's floor as the trigger.
        settings_file (str or None): the settings path the caller was started with. Used for
            the state directory *and* passed to the handler, so it reads the same document
            the ``settings`` argument came from. Omitting it where the caller is not on the
            default settings.yaml leaves the handler on a different one from the queries.
        now (float or None): the clock, for tests; defaults to time.time()

    Returns:
        InputReading: the newest value available, whose ``age`` the caller must judge

    Raises:
        SourceConnectionError: where no value could be obtained at all
        ConfigError: where the source has no usable settings section
    """
    source, field = _required(spec, "source"), _required(spec, "field")
    # max_age through the same door as the floor and the timeout. It is the third duration
    # in this expression and the one I left bare: an .inf max_age makes the trigger infinite,
    # so the input reads as perpetually fresh and is never refreshed however old it gets.
    minimum = resolve_minimum_interval(source, settings)
    # max_age is optional in a control document and stays that way: it arrived late enough
    # that requiring it would break documents people already have. An absent one takes the
    # source's own DEFAULT_MAX_AGE, which is how long that source's readings stay worth
    # acting on.
    #
    # Not a multiple of the minimum interval, which was the first attempt and is wrong in
    # both directions. Nuki's minimum interval is 0, so any multiple of it is 0 and every
    # reading would be instantly too old; Octopus data is a day behind by nature, so any
    # multiple of its rate limit would put a healthy feed permanently in the fail-safe. How
    # often a source may be asked and how long its answer stays true are unrelated.
    max_age = _as_seconds(spec.get("max_age", _default_max_age(source)), f"max_age for input {field}")
    trigger = max(max_age, minimum)
    # One handler for the whole call, closed on the way out. Every path here needs one -
    # even the stored read, for the measurement, tags and database - and each build opens a
    # session nothing closes.
    with source_handler(source, settings_file=settings_file, instance=spec.get("instance")) as handler:
        stored = handler_reading(session, settings, handler, field, now)
        if stored is not None and stored.age <= trigger:
            return stored
        if not handler.MCP_LIVE_STATE:
            # Nothing to gain: this source's live read is no fresher than what is stored.
            return _require(stored, source, field, "it has no live read and InfluxDB holds no point for it")
        # Through the same door as the floor, so one piece of code decides what a duration
        # is. A non-finite budget stops fetch_lock's wait loop ever reaching its deadline.
        budget = _as_seconds(handler.source_settings.get("timeout", 5), f"{source}.timeout")
        with fetch_lock(source, budget, settings_file) as held:
            if not held:
                logging.warning(
                    "Gave up waiting %.1fs for the %r fetch lock reading %r; falling back to "
                    "the stored value if there is one",
                    budget,
                    source,
                    field,
                )
                return _require(stored, source, field, "the fetch lock was busy and InfluxDB holds no point for it")
            # The holder we were waiting behind has almost certainly just written the value,
            # so look again before touching the device. The lock is held across the
            # write-back for this to be true.
            fresh = handler_reading(session, settings, handler, field, now)
            if fresh is not None and fresh.age <= trigger:
                return fresh
            return _live_reading(handler, source, field, stored, now)


def _live_reading(handler, source, field, stored, now):
    """Fetch from the source, write it back, and return it.

    A failed fetch degrades to the stored value rather than propagating. The device being
    unreachable is what the control's fail-safe is for, and raising here would make a
    reachable-but-stale input and an unreachable one behave differently when the control
    responds to both the same way.

    Args:
        handler (DataHandler): the source's handler, already built and scoped to the instance
        source (str): the source being read, for messages
        field (str): the field wanted
        stored (InputReading or None): what InfluxDB held, to fall back to
        now (float or None): the clock, for tests

    Returns:
        InputReading: the live value, or the stored one where the fetch failed

    Raises:
        SourceConnectionError: where the fetch failed and nothing was stored either
        ConfigError: where the source is misconfigured, which no amount of retrying fixes

    Note:
        A failed write-back is logged rather than raised: the reading is still returned.
    """
    moment = time.time() if now is None else now
    try:
        data = handler.get_data()
    except SourceConnectionError as exc:
        # SourceConnectionError only, not ToInfluxError. ConfigError is a ToInfluxError too
        # - Hue's bridge() and MyEnergi's device() raise it - and it means stop rather than
        # retry: degrading it here would hide a misconfigured bridge as a device that keeps
        # being unreachable, for as long as nobody looked. Same for ToolParamError.
        #
        # Logged rather than swallowed: this is the difference between a control acting on
        # old data and one that cannot see its input at all, and only the log says which.
        logging.warning(
            "Live read of %r for %r failed (%r); falling back to the stored value if there is one",
            source,
            field,
            exc,
        )
        return _require(stored, source, field, f"the live read failed ({exc!r}) and InfluxDB holds no point for it")
    # Write back every field the fetch returned, not just the one asked for: the round trip
    # has already been paid for, and another control reading a different field of this
    # source is the case the floor exists to serve.
    try:
        handler.send_data(data)
    except InfluxWriteError as exc:
        # The value in hand is good; only the coordination failed. Raising here would throw
        # away a fresh reading because a best-effort write missed, and the caller's response
        # to a failed read is the safe state - so an InfluxDB hiccup would switch the heating
        # off while the temperature it was holding was perfectly well known.
        #
        # What is lost is that other controls will not see this value and will each fetch
        # their own, so the floor stops binding until a write succeeds. send_data buffers the
        # point on failure, so it may still land on a later cycle.
        logging.warning("Could not write back the live read of %r for %r: %r", source, field, exc)
    if field not in data:
        return _require(stored, source, field, "the live read returned no such field")
    # The point's own time, not the time we asked for it. get_data() sets handler.timestamp
    # where the reading is older than the request - Nuki does, Octopus does - and send_data
    # writes the point at that same value, so reporting age 0 here would disagree with what
    # InfluxDB now holds and would tell a control an hour-old reading was brand new.
    stamp = moment if handler.timestamp is None else float(handler.timestamp)
    return InputReading(value=data[field], timestamp=stamp, age=moment - stamp, live=True)


def _require(reading, source, field, why):
    """Return a reading, or raise saying why there is none.

    Args:
        reading (InputReading or None): the candidate
        source (str): the source, for the message
        field (str): the field, for the message
        why (str): what was tried, phrased to follow the field name

    Returns:
        InputReading: the reading, when there is one

    Raises:
        SourceConnectionError: when there is not
    """
    if reading is None:
        raise SourceConnectionError(f"no value available for {field!r} from source {source!r}: {why}")
    return reading
