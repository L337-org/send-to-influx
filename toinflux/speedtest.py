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

# speedtest-cli's get_best_server() penalises a failed latency probe with a hardcoded 3600-second
# penalty instead of raising, then averages penalties in with any real samples before converting to
# milliseconds - so even a single failed probe out of the 3 it tries per server skews the reported
# "ping" to ~600,000 ms, orders of magnitude above any real-world value. The 3600 ms cutoff below is
# an unrelated round number chosen for that same reason - no genuine ping comes remotely close to it.
MAX_PLAUSIBLE_PING_MS = 3600


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
