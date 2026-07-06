#!/usr/bin/env python3
"""Script to get data from a variety of sources and send it to InfluxDB"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT License"

import sys
import time
import json
import signal
import logging
import argparse
import threading
from importlib.metadata import version, PackageNotFoundError
import toinflux
from toinflux.exceptions import ConfigError

try:
    __version__ = version("send-to-influx")
except PackageNotFoundError:
    # Running from a source checkout without the package installed (e.g. `python sendtoinflux.py`
    # in a dev venv) - pyproject.toml's [project] version is the single source of truth otherwise.
    __version__ = "0.0.0-dev"

DEFAULT_STAGGER_SECONDS = 10
BACKOFF_BASE_SECONDS = 5
BACKOFF_MAX_SECONDS = 300


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


def send_heartbeat(data_handler, source, ok, consecutive_failures):
    """
    Write a ``collector_status`` point via the source's own DataHandler, so a dead
    collector shows up as ``ok=0`` in Grafana instead of a silent gap.

    Reuses send_data() by temporarily swapping in a heartbeat measurement header -
    it doesn't care what measurement/fields it's sending. A heartbeat write failure
    is logged and swallowed rather than counted as a source failure.

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
        data_handler.send_data(data={"ok": 1 if ok else 0, "consecutive_failures": consecutive_failures})
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.warning("Failed to write heartbeat for source '%s': %s", source, exc)
    finally:
        data_handler.influx_header = original_header


def maybe_send_heartbeat(args, data_handler, source, ok, consecutive_failures):
    """Send a heartbeat unless running in --print mode, which never touches InfluxDB."""
    if not args.print:
        send_heartbeat(data_handler, source, ok=ok, consecutive_failures=consecutive_failures)


def create_source_worker(source, source_start_delay, args, stopped_sources):
    """Create a worker function for continuous source collection with retries.

    :param stopped_sources: shared set that the worker adds ``source`` to when it
        gives up permanently (a ConfigError), so the multi-source supervisor loop
        knows not to restart it
    :type stopped_sources: set
    """

    def source_worker():
        failure_count = 0
        next_update = time.time() + source_start_delay
        data_handler = None
        while True:
            try:
                if data_handler is None:
                    data_handler = toinflux.get_class(source, args.settings)
                sleep_time = max(0, next_update - time.time())
                time.sleep(sleep_time)
                interval = collect_source_data(source, args, data_handler)
                next_update += interval
                failure_count = 0
                maybe_send_heartbeat(args, data_handler, source, ok=True, consecutive_failures=0)
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
    sys.exit(0)


def main():
    """
    The main function
    """
    # register the signal handler for ctrl-c and termination
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

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
        print("Configuration OK")
        sys.exit(0)

    loglevel = "DEBUG" if args.verbose else settings.get("loglevel", "INFO")
    toinflux.configure_logging(
        settings.get("logfile"),
        loglevel=loglevel,
        log_max_bytes=settings.get("log_max_bytes", toinflux.DEFAULT_LOG_MAX_BYTES),
        log_backup_count=settings.get("log_backup_count", toinflux.DEFAULT_LOG_BACKUP_COUNT),
    )
    default_source = settings.get("default_source", "hue")

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
    run_multi_source(sources, args, settings.get("stagger_seconds", DEFAULT_STAGGER_SECONDS))


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
        try:
            data_handler = toinflux.get_class(source, args.settings)
            data = data_handler.get_data()
        except ConfigError as exc:
            logging.critical("Source '%s' has a configuration problem: %s", source, exc)
            sys.exit(1)
        print(json.dumps(data, indent=4))
        sys.exit(0)

    failure_count = 0
    next_update = time.time()
    while True:
        try:
            if data_handler is None:
                data_handler = toinflux.get_class(source, args.settings)
            next_update += data_handler.source_settings["interval"]
            data = data_handler.get_data()

            if args.print:
                print_source_data(source, data)
            else:
                data_handler.send_data()

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


def run_multi_source(sources, args, stagger_seconds):
    """
    Run all configured sources concurrently, with staggered start offsets.

    :param sources: list of source names to run
    :type sources: list[str]
    :param args: parsed CLI arguments
    :type args: argparse.Namespace
    :param stagger_seconds: delay between source start offsets (coerced to int)
    :type stagger_seconds: int
    """

    try:
        stagger_value = int(stagger_seconds)
    except (TypeError, ValueError):
        logging.warning("Invalid 'stagger_seconds' value '%s' in configuration; defaulting to 0.", stagger_seconds)
        stagger_value = 0

    threads = []
    workers = []
    stopped_sources = set()
    stagger_step = max(0, stagger_value)
    for index, source in enumerate(sources):
        start_delay = stagger_step * index
        worker = create_source_worker(source, start_delay, args, stopped_sources)
        workers.append(worker)
        threads.append(spawn_source_thread(worker))

    while True:
        for idx, thread in enumerate(threads):
            if not thread.is_alive() and sources[idx] not in stopped_sources:
                logging.warning("Source '%s' worker stopped unexpectedly. Restarting worker thread.", sources[idx])
                threads[idx] = spawn_source_thread(workers[idx])
        time.sleep(1)


if __name__ == "__main__":
    main()
