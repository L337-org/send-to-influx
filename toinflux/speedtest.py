"""Speedtest class to send data to InfluxDB"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT License"
__version__ = "1.0"

import logging
from socket import gethostname
import speedtest
from toinflux.influx import DataHandler
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
    """Speedtest class to send data to InfluxDB"""

    def get_data(self):
        """Run and get the data from Speedtest

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
