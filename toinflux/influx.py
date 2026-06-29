"""Parent class for data handlers to send data to InfluxDB"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT License"
__version__ = "1.0"

import sys
import requests
from toinflux.general import load_settings


class DataHandler:
    """Class to send data to InfluxDB"""

    def __init__(self, source=None):
        self.settings = load_settings()
        self.source = source
        self.influx_header = None
        self.data = None

        if self.source and self.source in self.settings:
            self.source_settings = self.settings[self.source]
        else:
            print(f"Source {self.source} not found in settings")
            sys.exit(1)

    def send_data(self, data=None):
        """
        Sends data to influxDB

        :param data: data to send to InfluxDB
        :type data: dict
        :return: None
        """
        # if the data is not provided, use the data from the class
        if data is None:
            data = self.data

        if not data or not isinstance(data, dict):
            print("No data to send to InfluxDB")
            return

        # format the data to send
        data_to_send = self.influx_header + ",".join([f"{key}={value}" for key, value in data.items()])

        # send to InfluxDB
        url = f'{self.settings["influx"]["url"]}/write?db={self.source_settings["db"]}&precision=s'
        try:
            response = requests.post(
                url,
                auth=(self.settings["influx"]["user"], self.settings["influx"]["password"]),
                data=data_to_send,
                timeout=self.settings["influx"].get("timeout", 5),
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"Error sending data to InfluxDB - {e}")
