"""Functions to get data from a Hue Bridge and format ready for InfluxDB"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT License"
__version__ = "1.0"

import sys
import logging
import warnings
import urllib3
import requests
from toinflux.influx import DataHandler


class Hue(DataHandler):
    """Child class of DataHandler to get data from a Hue Bridge"""

    def get_data(self):
        """
        Get the data from the Hue Bridge

        :return: data
        :rtype: dict
        """
        self.influx_header = f"hue,host={self.settings['hue']['host']} "
        self.data = self.parse_hue_data()
        return self.data

    def get_data_from_hue_bridge(self):
        """
        Connect to the Hue bridge and get the sensor data

        :return: hue_data
        :rtype: dict
        """
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
                response = self.session.get(
                    f"https://{self.settings['hue']['host']}/api/{self.settings['hue']['user']}",
                    timeout=self.settings["hue"].get("timeout", 5),
                    verify=False,
                )
            hue_data = response.json()
        except requests.exceptions.RequestException as e:
            logging.error("Error connecting to Hue Bridge - %s", e)
            sys.exit(2)
        if isinstance(hue_data, list) and "error" in hue_data[0]:
            logging.error("Error connecting to Hue Bridge - %s", hue_data[0]["error"]["description"])
            sys.exit(2)
        return hue_data

    def hue_device_name_to_name(self, device_name):
        """
        Converts the device name into a name to be used in InfluxDB

        If no name mapping exists in the settings file, the name in the Hue settings is used.
        Any spaces will be replaced with underscores.

        :param device_name: name of the device in the hue settings
        :type device_name: str
        :return: name
        :rtype: str
        """
        if "sensors" in self.settings["hue"]:
            name = self.settings["hue"]["sensors"].get(device_name, device_name)
        else:
            name = device_name
        return name.replace(" ", "_")

    def parse_hue_data(self):
        """
        Parse the data from the bridge to get the values we want

        :return: data
        :rtype: dict
        """
        data = {}
        hue_data = self.get_data_from_hue_bridge()

        # parse the sensor data
        for device in hue_data["sensors"].values():
            name = self.hue_device_name_to_name(device["name"])
            if device["type"] == "ZLLTemperature":
                # convert temperature to the desired units
                celsius = device["state"]["temperature"] / 100
                if self.settings["hue"].get("temperature_units") == "F":
                    data[name] = round((celsius * 1.8) + 32, 2)
                elif self.settings["hue"].get("temperature_units") == "K":
                    data[name] = round(celsius + 273.15, 2)
                else:
                    data[name] = round(celsius, 2)
            elif device["type"] == "ZLLLightLevel":
                # convert light level to lux
                data[name] = round(float(10 ** ((device["state"]["lightlevel"] - 1) / 10000)), 2)
            elif device["type"] == "ZLLPresence":
                # convert presence to boolean 0 or 1
                data[name] = int(1 if device["state"]["presence"] else 0)

        for device in hue_data["lights"].values():
            name = self.hue_device_name_to_name(device["name"])
            # convert brightness to percentage if the light is dimmable (has a "bri" attribute)
            # otherwise boolean 0 or 1 to cover smart plugs which are also listed as lights
            data[name] = int(device["state"].get("bri", 2.54) / 2.54) if device["state"]["on"] else 0

        return data
