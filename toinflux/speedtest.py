"""Speedtest class to send data to InfluxDB"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT License"
__version__ = "1.0"

import logging
import threading
from socket import gethostname
import speedtest
from toinflux.influx import DataHandler, InfluxWriteError
from toinflux.general import flatten_dict
from toinflux.exceptions import SourceConnectionError

# speedtest-cli's get_best_server() times each of the 3 latency probes it makes per candidate
# server using SpeedtestHTTPConnection/SpeedtestHTTPSConnection, whose __init__ defaults to a
# hardcoded timeout=10 (seconds) - get_best_server() never overrides it, so this 10s cap applies
# regardless of the `timeout` passed to speedtest.Speedtest() (that one only reaches the config-
# fetch/download/upload opener). Any probe that doesn't complete within that 10s therefore raises
# socket.timeout - caught alongside every other connection failure - and gets penalised with a
# hardcoded 3600 (seconds) instead of a real sample. The 3 per-server samples (real or penalty) are
# summed, divided by a fixed 6 - not the sample count - and converted to milliseconds, so a real
# (non-penalised) probe can never contribute more than 10s to that sum: the true ceiling for a
# genuine "ping" is (3 * 10 / 6) * 1000 = 5000 ms. Anything at or above that is provably longer
# than get_best_server() could actually have measured, and must include at least one 3600s
# penalty - which alone already yields ~600,000 ms, so there's no ambiguous middle ground.
MAX_PLAUSIBLE_PING_MS = 5000


class Speedtest(DataHandler):
    """Child class of DataHandler to run a speed test and send the results to InfluxDB"""

    MCP_DESCRIPTION = "Internet speed test: download/upload throughput and latency."
    # get_data() runs a full download/upload test (minutes, saturates the link),
    # so current-state must never call it live - it reads the latest recorded run
    # from InfluxDB instead (see DataHandler.MCP_LIVE_STATE).
    MCP_LIVE_STATE = False
    # A run can be *triggered* on demand via the MCP write tool (mcp_trigger_run),
    # opt-in per install with speedtest.mcp_read_write: true - unlike Hue this
    # controls no external device, it just runs a measurement on the local host.
    MCP_WRITABLE = True
    MCP_FIELD_METADATA = {
        "download": {"unit": "bits/s"},
        "upload": {"unit": "bits/s"},
        "ping": {"unit": "ms"},
    }

    # One speed test at a time per host: a scheduled collection cycle and an
    # MCP-triggered run (or two triggered runs) would otherwise saturate the same
    # link simultaneously and skew each other's results. Class-level so the lock is
    # shared between the collector worker thread and the MCP server thread in the
    # one process; there is no cross-host coordination (separate hosts run separate
    # processes with no shared state, and can't trigger each other anyway).
    _run_lock = threading.Lock()

    def get_data(self):
        """Run a speed test on this host and return the results.

        Guarded by a per-host lock (see ``_run_lock``): if a run is already in
        progress - a scheduled cycle or an MCP-triggered run - this raises
        ``SourceConnectionError`` rather than starting a second, overlapping test
        that would skew both.

        :return: data
        :rtype: dict
        :raises SourceConnectionError: a run is already in progress, the test
            failed, or it returned an implausible result
        """
        if not Speedtest._run_lock.acquire(blocking=False):
            raise SourceConnectionError("a Speedtest run is already in progress on this host")
        try:
            return self._run_speedtest()
        finally:
            Speedtest._run_lock.release()

    def _run_speedtest(self):
        """Run the test and populate ``self.data``/``self.influx_header``; the
        caller (``get_data``) holds ``_run_lock``. Split out so ``get_data`` is
        just the lock guard around it.

        :return: data
        :rtype: dict
        """
        try:
            st = speedtest.Speedtest(timeout=self.settings["speedtest"].get("timeout", 120))

            # run the download test
            st.download()

            # run the upload test
            st.upload()

            # get the results
            st_data = st.results.dict()
        except speedtest.SpeedtestException as e:
            logging.error("Error running Speedtest - %s", e)
            raise SourceConnectionError(str(e)) from e
        if not isinstance(st_data, dict):
            logging.error("Error running Speedtest - invalid results")
            raise SourceConnectionError("invalid results")

        ping = st_data.get("ping")
        if isinstance(ping, (int, float)) and ping >= MAX_PLAUSIBLE_PING_MS:
            logging.error("Error running Speedtest - implausible ping %s ms (server probes likely failed)", ping)
            raise SourceConnectionError(f"implausible ping {ping} ms (server probes likely failed)")

        # flatten the speedtest payload so nested values can be filtered and sent
        flattened_data = flatten_dict(st_data)

        # just extract the specific fields we want here
        if "fields" in self.settings["speedtest"]:
            self.data = {k: flattened_data[k] for k in self.settings["speedtest"]["fields"] if k in flattened_data}
        else:
            self.data = flattened_data

        # use the local hostname as the host tag
        self.influx_header = f"speedtest,host={gethostname().split('.')[0]} "

        return self.data

    def mcp_trigger_run(self):
        """Run a speed test now (the MCP write action for this source) and return
        the result, recording it to InfluxDB like a scheduled run.

        ``get_data()`` enforces the one-run-at-a-time lock, so a run already in
        progress surfaces as ``SourceConnectionError`` rather than a second test.
        Recording is best-effort: a failed write is reported in the result's
        ``recorded`` flag, not raised, since the measurement itself succeeded.

        :return: ``{"source", "recorded", "result": {field: {"value"[, "unit"]}}}``
        :rtype: dict
        :raises SourceConnectionError: a run is already in progress, or the test
            failed
        """
        data = self.get_data()
        recorded = True
        try:
            self.send_data()
        except InfluxWriteError as exc:
            recorded = False
            logging.warning("Triggered Speedtest ran but recording it to InfluxDB failed: %s", exc)
        result = {}
        for name, value in sorted((data or {}).items()):
            entry = {"value": value}
            unit = self.MCP_FIELD_METADATA.get(name, {}).get("unit")
            if unit:
                entry["unit"] = unit
            result[name] = entry
        logging.info("MCP-triggered Speedtest run complete (recorded=%s)", recorded)
        return {"source": self.source, "recorded": recorded, "result": result}
