"""Speedtest class to send data to InfluxDB"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT License"
__version__ = "1.0"

import sys
import logging
from socket import gethostname
import speedtest
from toinflux.influx import DataHandler
from toinflux.general import flatten_dict


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
            sys.exit(2)
        if not isinstance(st_data, dict):
            logging.error("Error running Speedtest - invalid results")
            sys.exit(2)

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
