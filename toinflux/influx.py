"""Parent class for data handlers to send data to InfluxDB"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT License"
__version__ = "1.0"

import sys
import logging
import warnings
import urllib3
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
            logging.error("Source %s not found in settings", self.source)
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
            logging.warning("No data to send to InfluxDB")
            return

        # format the data to send
        data_to_send = self.influx_header + ",".join([f"{key}={value}" for key, value in data.items()])

        # send to InfluxDB
        influx_settings = self.settings["influx"]
        timeout = influx_settings.get("timeout", 5)
        if "token" in influx_settings:
            url = (
                f'{influx_settings["url"]}/api/v2/write'
                f'?org={influx_settings["org"]}'
                f'&bucket={self.source_settings.get("bucket", self.source_settings.get("db"))}'
                f"&precision=s"
            )
            headers = {"Authorization": f'Token {influx_settings["token"]}'}
            kwargs = {"headers": headers}
        else:
            url = f'{influx_settings["url"]}/write?db={self.source_settings["db"]}&precision=s'
            kwargs = {"auth": (influx_settings["user"], influx_settings["password"])}

        insecure = influx_settings.get("insecure", False)
        kwargs["verify"] = not insecure

        try:
            with warnings.catch_warnings():
                if insecure:
                    warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
                response = requests.post(url, data=data_to_send, timeout=timeout, **kwargs)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            logging.error("Error sending data to InfluxDB - %s", e)
