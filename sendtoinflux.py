#!/usr/bin/env python3
"""Script to get data from a variety of sources and send it to InfluxDB"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT License"

import sys
import time
import json
import math
import signal
import logging
import argparse
import threading
import faulthandler
from importlib.metadata import version, PackageNotFoundError
import toinflux
from toinflux.influx import InfluxWriteError
from toinflux.exceptions import ConfigError, SourceConnectionError

try:
    __version__ = version("send-to-influx")
except PackageNotFoundError:
    # Running from a source checkout without the package installed (e.g. `python sendtoinflux.py`
    # in a dev venv) - pyproject.toml's [project] version is the single source of truth otherwise.
    __version__ = "0.0.0-dev"

DEFAULT_STAGGER_SECONDS = 10
BACKOFF_BASE_SECONDS = 5
BACKOFF_MAX_SECONDS = 300

# If a source's worker thread hasn't logged a successful cycle *or* a retried
# failure (both already visible) in this long, it isn't merely slow - it's stuck.
# A hung thread never reaches either branch, so this is the only signal a silent
# stall produces; see run_multi_source()'s supervisor loop. The actual threshold
# used is the larger of this and STALL_INTERVAL_MULTIPLIER times the source's own
# configured interval (see _stall_threshold_seconds) - a source legitimately
# sleeps for its full interval between cycles, so a flat threshold shorter than
# that would flag every long-interval source (e.g. speedtest's 6-hour default)
# as stalled on every single cycle.
STALL_WARNING_SECONDS = 900
STALL_INTERVAL_MULTIPLIER = 3

# Set by the signal handler to ask a streaming source's blocking loop to stop and
# disconnect (see stream_source_data). Timer-driven sources don't consult it - they exit
# via the SystemExit the handler raises - but a streaming source is blocked inside its
# network loop, so it needs an explicit stop signal. How cleanly it then disconnects
# differs between single- and multi-source mode; see signal_handler for the detail.
SHUTDOWN = threading.Event()


def print_source_data(source, data):
    """Print data from a source in a consistent JSON envelope."""
    blob = {
        "source": source,
        "time": time.strftime("%a, %d %b %Y, %H:%M:%S %Z", time.localtime()),
        "data": data,
    }
    print(json.dumps(blob, indent=4))


def get_backoff_delay(
    failure_count, backoff_base_seconds=BACKOFF_BASE_SECONDS, backoff_max_seconds=BACKOFF_MAX_SECONDS
):
    """Return the bounded exponential backoff delay in seconds."""
    exponent = max(0, failure_count - 1)
    if backoff_base_seconds <= 0:
        return 0
    ratio = max(1, backoff_max_seconds // backoff_base_seconds)
    max_exponent = ratio.bit_length()
    exponent = min(exponent, max_exponent)
    delay = backoff_base_seconds * (2**exponent)
    return min(delay, backoff_max_seconds)


def collect_source_data(source, args, data_handler):
    """Collect one data point for a source and either print or send it."""
    data = data_handler.get_data()
    if args.print:
        print_source_data(source, data)
    else:
        data_handler.send_data()
    return data_handler.source_settings["interval"]


def stream_source_data(source, args, data_handler, should_stop, on_activity=None):
    """
    Run a streaming (event-driven) source, blocking until ``should_stop`` is set.

    Streaming sources (MQTT - see ``MqttDataHandler``) hold their subscription open
    instead of polling on a timer, so a state change is written the instant it arrives
    rather than only being caught if a poll happens to land on it. Two write paths run
    concurrently, serialised by the transport:

    - **Immediate:** each arriving message is decoded (``decode_stream_message``) and
      its point written straight away. A write failure is buffered by ``send_data`` and
      swallowed here - the transport would otherwise log a full traceback per message
      during an InfluxDB outage, and the point is safely queued for the backlog flush.
    - **Periodic (the timer-based safety net + health probe):** once every ``interval``
      the source's normal full-state poll still runs, exactly as it did before streaming,
      so a missed message is caught up from retained state and the heartbeat keeps ticking
      on an idle source. That poll doubles as an active health probe (see ``periodic``): it
      hits the same broker as the live stream, so its failure correlates with the stream
      being down, and drives the heartbeat's ``ok``. A failing poll never tears down the
      stream (paho reconnects genuine drops); it's folded into the heartbeat instead.

    Only returns on a clean stop; a broker failure at startup raises
    ``SourceConnectionError`` and a config-shape problem raises ``ConfigError``, both left
    to the caller's existing retry/stop handling (identical to the polling path).

    :param source: source name
    :type source: str
    :param args: parsed CLI arguments (``args.print`` routes writes to stdout)
    :type args: argparse.Namespace
    :param data_handler: the source's streaming DataHandler instance
    :type data_handler: toinflux.mqtt.MqttDataHandler
    :param should_stop: set to end the stream and return
    :type should_stop: threading.Event
    :param on_activity: optional no-arg callback stamped on each write/tick, so the
        multi-source stall watchdog sees a live stream making progress
    :type on_activity: collections.abc.Callable or None
    :return: None
    """
    sink = _StreamSink(source, args, data_handler, on_activity)
    data_handler.stream_mqtt_messages(
        data_handler.STREAM_TOPIC_FILTER,
        sink.on_message,
        sink.periodic,
        data_handler.source_settings["interval"],
        should_stop,
    )


class _StreamSink:
    """
    Bridges a streaming source's transport callbacks to the collector's write, heartbeat
    and stall-activity behaviour.

    Holds the per-run context (source, args, handler, activity callback) so the transport's
    ``on_message``/``periodic`` callbacks are plain bound methods rather than closures. See
    ``stream_source_data`` for the two write paths and their failure handling.
    """

    def __init__(self, source, args, data_handler, on_activity):
        self.source = source
        self.args = args
        self.data_handler = data_handler
        self.on_activity = on_activity
        # Health state for the periodic heartbeat. Both are only touched from the single
        # queue-drain thread (on_message and periodic run there, not paho's network
        # thread), so no lock is needed. _message_since_tick lets a demonstrably-working
        # stream override a flaky one-off probe; _consecutive_probe_failures carries the
        # streak so alerting can tolerate transients.
        self._message_since_tick = False
        self._consecutive_probe_failures = 0

    def _write(self, data):
        if self.args.print:
            print_source_data(self.source, data)
        else:
            self.data_handler.send_data(data=data)

    def _stamp_activity(self):
        if self.on_activity is not None:
            self.on_activity()

    def on_message(self, topic, payload):
        """Write the point for one arriving message immediately (the interrupt path), and
        note that the stream showed life this interval so the next heartbeat counts it
        healthy even if the periodic probe happens to fail."""
        data = self.data_handler.decode_stream_message(topic, payload)
        if not data:
            return
        self._message_since_tick = True
        self._stamp_activity()
        try:
            self._write(data)
        except InfluxWriteError:
            # Already buffered by send_data (which logged the write error); a failed write
            # is not a stream failure, so don't let it bubble up and be logged per message
            # with a full traceback during an InfluxDB outage.
            pass

    def periodic(self):
        """Run the periodic tick once per interval: an active full-state probe of the
        source (its normal poll), the heartbeat, and a stall-activity stamp.

        The probe doubles as the streaming source's health signal. It hits the same broker
        as the live stream, so its failure correlates with the stream being down - exactly
        the silent outage (stream dead, no messages arriving) the heartbeat exists to
        surface. So ``ok`` reflects *any sign of life since the last tick*: the probe
        succeeded, or a message arrived. A demonstrably-working stream mustn't be marked
        down by a flaky one-off probe, and an idle-but-healthy source sends no messages for
        hours so the probe is what proves it alive - only when both go dark is ``ok=0``
        reported, with ``consecutive_failures`` carrying the streak so alerting tolerates
        transients (identical meaning to the polling heartbeat).

        A failing probe never tears down the stream (paho reconnects genuine drops); it's
        logged and folded into the heartbeat. ``last_activity`` is stamped every tick
        regardless - an unreachable source whose thread is still ticking is not *stuck*, the
        separate condition the stall watchdog detects.
        """
        probe_ok = True
        try:
            data = self.data_handler.get_data()
        except SourceConnectionError as exc:
            probe_ok = False
            logging.warning("Health probe for streaming source '%s' failed: %s", self.source, exc)
        else:
            try:
                self._write(data)
            except InfluxWriteError:
                # Buffered by send_data; the source itself was reachable, so the probe
                # succeeded - only the downstream InfluxDB write failed (surfaced by the
                # heartbeat's own write failing / the gap it leaves, and the backlog flush).
                pass
        ok = probe_ok or self._message_since_tick
        self._consecutive_probe_failures = 0 if ok else self._consecutive_probe_failures + 1
        maybe_send_heartbeat(
            self.args, self.data_handler, self.source, ok=ok, consecutive_failures=self._consecutive_probe_failures
        )
        self._message_since_tick = False
        self._stamp_activity()


def send_heartbeat(data_handler, source, ok, consecutive_failures):
    """
    Write a ``collector_status`` point via the source's own DataHandler, so a dead
    collector shows up as ``ok=0`` in Grafana instead of a silent gap.

    Reuses send_data() by temporarily swapping in a heartbeat measurement header -
    it doesn't care what measurement/fields it's sending. Passes an explicit
    ``timestamp`` of "now": some handlers (e.g. Octopus) set ``self.timestamp`` to
    something other than the current time for their own writes, which send_data()
    would otherwise fall back to, making the heartbeat reflect a stale time rather
    than when the collector was actually last checked. A heartbeat write failure
    is logged and swallowed rather than counted as a source failure. Passes
    ``use_buffer=False``: a heartbeat is a live signal with no replay value, so a
    failed one is dropped rather than buffered - otherwise every failed cycle
    during an InfluxDB outage would consume a buffer slot per heartbeat, evicting
    real measurement points, and recovery would backfill stale ok=0 status lines.

    :param data_handler: the source's DataHandler instance, or None if it hasn't
        been constructed yet (e.g. a config error) - in which case there's no
        handler to send a heartbeat through, so this is a no-op
    :type data_handler: DataHandler or None
    :param source: source name, used as the ``source`` tag
    :type source: str
    :param ok: whether the most recent collection cycle succeeded
    :type ok: bool
    :param consecutive_failures: current failure streak for this source
    :type consecutive_failures: int
    :return: None
    """
    if data_handler is None:
        return
    original_header = data_handler.influx_header
    data_handler.influx_header = f"collector_status,source={source} "
    try:
        data_handler.send_data(
            data={"ok": 1 if ok else 0, "consecutive_failures": consecutive_failures},
            timestamp=int(time.time()),
            use_buffer=False,
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.warning("Failed to write heartbeat for source '%s': %s", source, exc)
    finally:
        data_handler.influx_header = original_header


def maybe_send_heartbeat(args, data_handler, source, ok, consecutive_failures):
    """Send a heartbeat unless running in --print mode, which never touches InfluxDB."""
    if not args.print:
        send_heartbeat(data_handler, source, ok=ok, consecutive_failures=consecutive_failures)


def _stamp_activity(last_activity, source):
    """Stamp ``last_activity[source]`` with the current time for the multi-source stall
    watchdog, or do nothing when stall detection isn't in use (``last_activity`` is None)."""
    if last_activity is not None:
        last_activity[source] = time.time()


def _should_stream(data_handler):
    """
    Whether to run the event-driven stream loop for this handler rather than polling.

    True only when it's a ``STREAMING`` transport *and* a concrete source has actually
    given it a topic filter to subscribe to. ``MqttDataHandler`` sets ``STREAMING = True``
    for the whole transport, but its ``STREAM_TOPIC_FILTER`` defaults to ``None`` until a
    subclass wires up the per-message decode (the filter and ``decode_stream_message``
    land together, per source). Gating on the filter means enabling the transport before a
    source is wired can't strand that source in the stream path subscribing to ``None`` and
    retrying forever - it keeps polling, exactly as it did before streaming existed.

    :param data_handler: the source's DataHandler instance
    :type data_handler: toinflux.influx.DataHandler
    :return: True to take the streaming path, False to poll on the timer
    :rtype: bool
    """
    return bool(data_handler.STREAMING) and getattr(data_handler, "STREAM_TOPIC_FILTER", None) is not None


def create_source_worker(source, source_start_delay, args, stopped_sources, last_activity=None):
    """Create a worker function for continuous source collection with retries.

    :param stopped_sources: shared set that the worker adds ``source`` to when it
        gives up permanently (a ConfigError), so the multi-source supervisor loop
        knows not to restart it
    :type stopped_sources: set
    :param last_activity: shared dict the worker stamps with ``source: time.time()``
        on every successful or failed cycle, so the multi-source supervisor loop can
        tell a source that's merely retrying (already visible via its own WARNING
        lines) from one that's stopped making any progress at all - a thread stuck
        mid-instruction never reaches either branch, so this is the only signal a
        silent stall produces. None (the default) skips this bookkeeping - used by
        callers that don't need stall detection (e.g. tests exercising retry logic
        in isolation).
    :type last_activity: dict or None
    """

    def source_worker():
        failure_count = 0
        next_update = time.time() + source_start_delay
        data_handler = None
        if last_activity is not None:
            # Stamp with the scheduled first-run time, not now: a large
            # stagger_seconds (or many sources) can make source_start_delay
            # itself exceed the stall threshold, which would otherwise flag a
            # source as stalled while it's still in its intentional initial
            # delay, before it's ever had a chance to run.
            last_activity[source] = next_update
        while True:
            try:
                if data_handler is None:
                    data_handler = toinflux.get_class(source, args.settings)
                sleep_time = max(0, next_update - time.time())
                time.sleep(sleep_time)
                if _should_stream(data_handler):
                    # Blocks until shutdown, streaming points as they arrive and running
                    # the interval snapshot/heartbeat itself. It returns only on a clean
                    # stop; a broker failure at startup raises and is handled by the
                    # backoff branch below exactly like a failed poll.
                    stream_source_data(
                        source, args, data_handler, SHUTDOWN, on_activity=lambda: _stamp_activity(last_activity, source)
                    )
                    return
                interval = collect_source_data(source, args, data_handler)
                next_update += interval
                failure_count = 0
                maybe_send_heartbeat(args, data_handler, source, ok=True, consecutive_failures=0)
                _stamp_activity(last_activity, source)
            except ConfigError as exc:
                logging.critical("Source '%s' has a configuration problem and will not be retried: %s", source, exc)
                maybe_send_heartbeat(args, data_handler, source, ok=False, consecutive_failures=failure_count + 1)
                stopped_sources.add(source)
                return
            except Exception as exc:  # pylint: disable=broad-exception-caught
                failure_count += 1
                restart_delay = get_backoff_delay(failure_count)
                logging.warning(
                    "Source '%s' failed: %s. Restarting in %s seconds (attempt %s).",
                    source,
                    exc,
                    restart_delay,
                    failure_count,
                )
                maybe_send_heartbeat(args, data_handler, source, ok=False, consecutive_failures=failure_count)
                data_handler = None
                next_update = time.time() + restart_delay
                _stamp_activity(last_activity, source)

    return source_worker


def spawn_source_thread(worker):
    """Create and start a daemon thread for a source worker."""
    source_thread = threading.Thread(target=worker, daemon=True)
    source_thread.start()
    return source_thread


def signal_handler(sig, _frame):
    """
    Signal handler to exit gracefully
    """
    logging.info("Exiting on signal %s", sig)
    # Ask any streaming source to break out of its network loop and disconnect. In
    # single-source mode the loop runs on this (the main) thread, so the SystemExit
    # raised below unwinds through stream_mqtt_messages' finally and disconnects
    # cleanly. In multi-source mode the streams run on daemon threads and this
    # sys.exit(0) exits the process straight away, so a worker may not observe SHUTDOWN
    # before it's killed - the disconnect is best-effort there, and we lean on the
    # broker's keepalive/last-will to reap the dropped session.
    SHUTDOWN.set()
    sys.exit(0)


def maybe_start_mcp_server(settings, args):
    """Start the embedded MCP server thread when enabled and in a collection mode.

    ``--print`` and ``--dump`` are interactive debugging modes that never touch
    InfluxDB, so they don't start a network server either. The import is lazy
    (and gated on ``mcp_enabled``): ``toinflux.mcpserver`` pulls in the ``mcp``
    SDK, which the disabled path must not require - same pattern as paho-mqtt
    only being imported by the MQTT transport.

    :param settings: loaded settings dict
    :type settings: dict
    :param args: parsed CLI arguments
    :type args: argparse.Namespace
    :return: the server thread, or None when not started
    :rtype: threading.Thread or None
    """
    if args.print or args.dump:
        return None
    if not toinflux.mcp_enabled(settings):
        return None
    from toinflux.mcpserver import start_mcp_server_thread

    return start_mcp_server_thread(settings, args.settings)


def _configure_logging_or_exit(settings, args):
    """
    Configure logging from settings/args, exiting 1 with a clean message on failure.

    :param settings: loaded settings dict
    :type settings: dict
    :param args: parsed CLI arguments
    :type args: argparse.Namespace
    """
    loglevel = "DEBUG" if args.verbose else settings.get("loglevel", "INFO")
    try:
        toinflux.configure_logging(
            settings.get("logfile"),
            loglevel=loglevel,
            log_max_bytes=settings.get("log_max_bytes", toinflux.DEFAULT_LOG_MAX_BYTES),
            log_backup_count=settings.get("log_backup_count", toinflux.DEFAULT_LOG_BACKUP_COUNT),
        )
    except ConfigError as exc:
        # configure_logging() already attached the stdout handler before hitting this
        # error (see its implementation), so this still lands as a normal formatted
        # log line - not a raw traceback - and reaches the systemd journal.
        logging.critical("%s", exc)
        sys.exit(1)


def register_thread_dump_handler():
    """Register a SIGUSR1 handler that dumps every thread's live stack trace to
    stderr (captured by the journal under systemd).

    A hang produces no exception and therefore no log line of its own, so this
    is the only way to see what every thread is actually blocked on without
    attaching a debugger. SIGUSR1 doesn't exist on every platform (e.g.
    Windows) - skip registration there rather than letting an AttributeError
    take down startup, including plain --version/--help runs. register() also
    needs a real file descriptor for stderr, which isn't guaranteed in every
    embedding context (e.g. a captured/wrapped stream, as under pytest) -
    degrade to a warning rather than taking the whole process down over an
    optional diagnostic.
    """
    if not hasattr(signal, "SIGUSR1"):
        logging.debug("SIGUSR1 is not available on this platform; skipping thread-dump handler registration")
        return
    try:
        faulthandler.register(signal.SIGUSR1, all_threads=True)
    except (ValueError, OSError) as exc:
        logging.warning("Could not register SIGUSR1 thread-dump handler: %s", exc)


def main():
    """
    The main function
    """
    # register the signal handler for ctrl-c and termination
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    register_thread_dump_handler()

    # parse the command line arguments first so --version/--help/--check-config work without a
    # settings.yaml present
    arg_parse = argparse.ArgumentParser(description="Send Hue Data to InfluxDB")
    arg_parse.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    arg_parse.add_argument(
        "--settings",
        dest="settings",
        type=str,
        default=None,
        help="path to the settings file (default: settings.yaml in the project root)",
    )
    arg_parse.add_argument(
        "--check-config",
        required=False,
        action="store_true",
        help="validate settings.yaml and exit (0 if valid, 1 if invalid)",
    )
    arg_parse.add_argument(
        "-v",
        "--verbose",
        required=False,
        action="store_true",
        help="enable DEBUG-level logging (overrides the 'loglevel' settings.yaml key)",
    )
    arg_parse.add_argument(
        "-d",
        "--dump",
        required=False,
        action="store_true",
        help=("dump the data to the console one time and exit. This requires a source to be specified"),
    )
    arg_parse.add_argument(
        "-p",
        "--print",
        required=False,
        action="store_true",
        help="print the raw data rather than sending it to InfluxDB",
    )
    arg_parse.add_argument(
        "-s",
        "--source",
        required=False,
        dest="source",
        type=str,
        help=(
            "the source of the data to send to InfluxDB (hue, zappi, etc.). "
            "If this parameter is omitted, all sources in the settings file 'sources' list are started. "
            "If no sources are specified in the settings file, the 'default_source' settings key is used."
        ),
    )
    args = arg_parse.parse_args()

    # load settings once for defaults and configured source list
    try:
        settings = toinflux.load_settings(args.settings)
    except ConfigError as exc:
        if args.check_config:
            print(f"Configuration error: {exc}")
        sys.exit(1)

    if args.check_config:
        # load_settings() already validated the configured sources/default_source above;
        # also validate args.source specifically, since a user checking config for a
        # particular --source shouldn't get a false "OK" if that source isn't part of
        # the sources/default_source list load_settings() already checked.
        try:
            toinflux.validate_settings(settings, source=args.source, settings_path=args.settings or "settings.yaml")
        except ConfigError as exc:
            print(f"Configuration error: {exc}")
            sys.exit(1)
        print("Configuration OK")
        sys.exit(0)

    _configure_logging_or_exit(settings, args)
    maybe_start_mcp_server(settings, args)
    default_source = toinflux.resolve_default_source(settings)

    if args.source:
        logging.info("Starting send-to-influx v%s (source=%s)", __version__, args.source)
        run_single_source(args.source, args)
        return

    sources = settings.get("sources")
    if not isinstance(sources, list) or not sources:
        logging.info("Starting send-to-influx v%s (source=%s, from default_source)", __version__, default_source)
        run_single_source(default_source, args)
        return

    if args.dump:
        logging.error("The --dump option requires --source when running in multi-source mode.")
        sys.exit(1)

    logging.info("Starting send-to-influx v%s (sources=%s)", __version__, ", ".join(map(str, sources)))
    run_multi_source(sources, args, settings.get("stagger_seconds", DEFAULT_STAGGER_SECONDS), settings)


def _dump_source_and_exit(source, args):
    """
    Collect one reading, print it as JSON, and exit - the one-shot ``--dump`` mode.

    A one-shot manual/debugging run has no worker loop to retry it with backoff, so a
    connection failure exits with a distinct code (2) rather than an unhandled traceback,
    and a config problem exits 1.

    :param source: source name from settings/get_class mapping
    :type source: str
    :param args: parsed CLI arguments
    :type args: argparse.Namespace
    :return: never returns - always exits the process
    """
    try:
        data_handler = toinflux.get_class(source, args.settings)
        data = data_handler.get_data()
    except ConfigError as exc:
        logging.critical("Source '%s' has a configuration problem: %s", source, exc)
        sys.exit(1)
    except SourceConnectionError as exc:
        logging.error("Source '%s' failed: %s", source, exc)
        sys.exit(2)
    print(json.dumps(data, indent=4))
    sys.exit(0)


def run_single_source(source, args):
    """
    Run a single data source in either dump, print, or send mode.

    :param source: source name from settings/get_class mapping
    :type source: str
    :param args: parsed CLI arguments
    :type args: argparse.Namespace
    """
    data_handler = None

    # dump the data if required and exit
    if args.dump:
        _dump_source_and_exit(source, args)

    failure_count = 0
    next_update = time.time()
    while True:
        try:
            if data_handler is None:
                data_handler = toinflux.get_class(source, args.settings)
            if _should_stream(data_handler):
                # Blocks until shutdown, streaming points as they arrive. On a signal the
                # handler sets SHUTDOWN and raises SystemExit on this thread, which unwinds
                # through stream_mqtt_messages' finally to disconnect cleanly; a broker
                # failure at startup raises and is handled by the backoff branch below.
                stream_source_data(source, args, data_handler, SHUTDOWN)
                return
            interval = collect_source_data(source, args, data_handler)
            next_update += interval

            failure_count = 0
            maybe_send_heartbeat(args, data_handler, source, ok=True, consecutive_failures=0)
        except ConfigError as exc:
            logging.critical("Source '%s' has a configuration problem and will not be retried: %s", source, exc)
            maybe_send_heartbeat(args, data_handler, source, ok=False, consecutive_failures=failure_count + 1)
            sys.exit(1)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            failure_count += 1
            restart_delay = get_backoff_delay(failure_count)
            logging.warning(
                "Source '%s' failed: %s. Restarting in %s seconds (attempt %s).",
                source,
                exc,
                restart_delay,
                failure_count,
            )
            maybe_send_heartbeat(args, data_handler, source, ok=False, consecutive_failures=failure_count)
            data_handler = None
            next_update = time.time() + restart_delay

        sleep_time = max(0, next_update - time.time())
        time.sleep(sleep_time)


def run_multi_source(sources, args, stagger_seconds, settings=None):
    """
    Run all configured sources concurrently, with staggered start offsets.

    :param sources: list of source names to run
    :type sources: list[str]
    :param args: parsed CLI arguments
    :type args: argparse.Namespace
    :param stagger_seconds: delay between source start offsets (coerced to int)
    :type stagger_seconds: int
    :param settings: parsed settings dict, used to read each source's own
        ``interval`` for the stall watchdog's threshold; None (the default)
        falls back to STALL_WARNING_SECONDS for every source
    :type settings: dict or None
    """

    try:
        stagger_value = int(stagger_seconds)
    except (TypeError, ValueError):
        logging.warning("Invalid 'stagger_seconds' value '%s' in configuration; defaulting to 0.", stagger_seconds)
        stagger_value = 0

    threads = []
    workers = []
    stopped_sources = set()
    last_activity = {}
    stalled_sources = set()
    stagger_step = max(0, stagger_value)
    for index, source in enumerate(sources):
        start_delay = stagger_step * index
        worker = create_source_worker(source, start_delay, args, stopped_sources, last_activity)
        workers.append(worker)
        threads.append(spawn_source_thread(worker))

    while True:
        for idx, thread in enumerate(threads):
            if not thread.is_alive() and sources[idx] not in stopped_sources:
                logging.warning("Source '%s' worker stopped unexpectedly. Restarting worker thread.", sources[idx])
                threads[idx] = spawn_source_thread(workers[idx])
        check_for_stalled_sources(sources, stopped_sources, last_activity, stalled_sources, settings)
        time.sleep(1)


def _stall_threshold_seconds(source, settings):
    """Return how long a source may go without activity before it's flagged as
    stalled: STALL_WARNING_SECONDS, or STALL_INTERVAL_MULTIPLIER times the
    source's own configured ``interval`` if that's larger - a source legitimately
    sleeps for its full interval between cycles, so a flat threshold shorter than
    that would flag every long-interval source (e.g. speedtest's 6-hour default)
    as stalled on every single cycle. Falls back to the flat threshold if
    ``settings``/the source's ``interval`` isn't available or isn't a finite positive
    number. ``.inf`` is valid YAML and passes a plain ``> 0`` check, so without the
    explicit finiteness check it would produce an infinite threshold - which raises
    when the CRITICAL log message formats it with ``%d``. (``.nan`` was already
    harmless here: NaN comparisons are always False, so ``interval > 0`` alone
    already rejected it - this check is kept for both anyway, since relying on
    that comparison quirk for NaN specifically would be a fragile thing to depend on.)

    :param source: source name
    :type source: str
    :param settings: parsed settings dict, or None
    :type settings: dict or None
    :rtype: int or float
    """
    interval = ((settings or {}).get(source) or {}).get("interval")
    if (
        isinstance(interval, (int, float))
        and not isinstance(interval, bool)
        and math.isfinite(interval)
        and interval > 0
    ):
        return max(STALL_WARNING_SECONDS, interval * STALL_INTERVAL_MULTIPLIER)
    return STALL_WARNING_SECONDS


def check_for_stalled_sources(sources, stopped_sources, last_activity, stalled_sources, settings=None):
    """Warn once per stall about a source whose worker thread is alive but has
    made no progress (success or failure) in over its stall threshold (see
    ``_stall_threshold_seconds``) - the thread-is_alive() check above can't
    catch this, since a thread stuck mid-instruction (e.g. the GIL-starvation-
    shaped hang this was added to diagnose) never dies, it just stops making
    progress silently. Logs once per stall (tracked via ``stalled_sources``)
    rather than every supervisor tick, and clears the flag once activity
    resumes so a later recurrence warns again.

    :param sources: configured source names
    :type sources: list[str]
    :param stopped_sources: sources that gave up permanently (ConfigError) - excluded
    :type stopped_sources: set
    :param last_activity: shared dict from create_source_worker(), source -> last
        successful-or-failed cycle's time.time()
    :type last_activity: dict
    :param stalled_sources: sources already warned about the current stall - mutated
        in place so the caller's loop sees updates
    :type stalled_sources: set
    :param settings: parsed settings dict, for per-source interval-aware
        thresholds; None falls back to STALL_WARNING_SECONDS for every source
    :type settings: dict or None
    :return: None
    """
    now = time.time()
    for source in sources:
        if source in stopped_sources:
            continue
        last = last_activity.get(source)
        if last is None:
            continue
        threshold = _stall_threshold_seconds(source, settings)
        if now - last > threshold:
            if source not in stalled_sources:
                logging.critical(
                    "Source '%s' has not completed a cycle (success or failure) in over %d "
                    "seconds - its worker thread is likely stuck rather than merely slow. Send "
                    "SIGUSR1 to this process (e.g. 'systemctl kill -s SIGUSR1 send-to-influx') to "
                    "dump every thread's stack trace to the log, and raise this as an issue.",
                    source,
                    threshold,
                )
                stalled_sources.add(source)
        else:
            stalled_sources.discard(source)


if __name__ == "__main__":
    main()
