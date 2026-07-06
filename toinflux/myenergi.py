"""Functions to get MyEnergi data ready to send to InfluxDB"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT License"
__version__ = "1.0"

import sys
import logging
import datetime
from requests.auth import HTTPDigestAuth
from toinflux.influx import DataHandler


class MyEnergi(DataHandler):
    """Child class of DataHandler to get data from MyEnergi"""

    def get_data_from_myenergi(self, url):
        """
        Get the data from the myenergi API

        :param url: full API endpoint URL
        :type url: str
        :return: parsed JSON response
        :rtype: dict
        """
        # Get the data for the given serial from the MyEnergi API
        serial = self.source_settings["serial"]
        auth = HTTPDigestAuth(serial, self.settings["myenergi"]["apikey"])
        response = self.session.get(url, auth=auth, timeout=self.settings["myenergi"].get("timeout", 5))
        if response.status_code == 200:
            pass
        elif response.status_code == 401:
            logging.error("Login unsuccessful. Please check username, password or URL.")
            sys.exit(2)
        else:
            logging.error("Login unsuccessful. Return code: %s", response.status_code)
            sys.exit(2)

        return response.json()

    def dayhour_results(self, year, month, day, hour=None):
        """
        Get the data for a specific day

        :param year: four-digit year, e.g. "2026"
        :type year: str
        :param month: zero-padded month, e.g. "06"
        :type month: str
        :param day: zero-padded day, e.g. "29"
        :type day: str
        :param hour: hour of the day (0-23); if None, results for the whole day are returned
        :type hour: int
        :return: Charge, Import, Export and Genera values in kWh
        :rtype: dict
        """
        # Get the Day/Hour data from the MyEnergi API
        serial = self.source_settings["serial"]
        dayhour_url = self.settings["myenergi"]["dayhour_url"] + serial
        response_data = self.get_data_from_myenergi(dayhour_url + "-" + str(year) + "-" + str(month) + "-" + str(day))
        charge_amount = 0
        import_amount = 0
        export_amount = 0
        genera_amount = 0

        # Tot up the data for the day/hour
        if response_data.get("U" + serial, False):
            for item in response_data["U" + serial]:
                if hour is not None and item.get("hr", -1) == hour:
                    charge_amount = item.get("h1d", 0)
                    import_amount = item.get("imp", 0)
                    export_amount = item.get("exp", 0)
                    genera_amount = item.get("gep", 0)
                    break
                charge_amount += item.get("h1d", 0)
                import_amount += item.get("imp", 0)
                export_amount += item.get("exp", 0)
                genera_amount += item.get("gep", 0)

        # Convert and round the data to 4 decimal places
        data = {
            "Charge": round((charge_amount / 3600 / 1000), 4),
            "Import": round((import_amount / 3600 / 1000), 4),
            "Export": round((export_amount / 3600 / 1000), 4),
            "Genera": round((genera_amount / 3600 / 1000), 4),
        }

        return data


class Zappi(MyEnergi):
    """Child class of MyEnergi (which is in turn a child of DataHandler) to get data from a Zappi"""

    def get_data(self):
        """
        Get the data from the Zappi

        :return: data
        :rtype: dict
        """
        self.influx_header = "myenergi,device=zappi "
        self.data = self.parse_zappi_data()
        return self.data

    def parse_zappi_data(self):
        """
        Parse the data from the myenergi to get the values we want

        :return: data
        :rtype: dict
        """
        # Get the data for the Zappi from the MyEnergi API
        myenergi_data = self.get_data_from_myenergi(self.settings["myenergi"]["zappi_url"])

        # Get the day/hour data for the Zappi
        now = datetime.datetime.now()
        day_data = self.dayhour_results(
            now.strftime("%Y"),
            now.strftime("%m"),
            now.strftime("%d"),
            now.hour,
        )

        # just extract the specific fields we want here
        if "fields" in self.settings["zappi"]:
            zappi_data = dict(
                (k, myenergi_data["zappi"][0][k])
                for k in self.settings["zappi"]["fields"]
                if k in myenergi_data["zappi"][0]
            )
        else:
            zappi_data = myenergi_data["zappi"][0]

        return zappi_data | day_data


class Eddi(MyEnergi):
    """Child class of MyEnergi to get data from an Eddi hot water diverter"""

    def get_data(self):
        """
        Get the data from the Eddi

        :return: data
        :rtype: dict
        """
        self.influx_header = "myenergi,device=eddi "
        self.data = self.parse_eddi_data()
        return self.data

    def parse_eddi_data(self):
        """
        Parse the data from the MyEnergi API for the Eddi device

        :return: data
        :rtype: dict
        """
        myenergi_data = self.get_data_from_myenergi(self.settings["myenergi"]["eddi_url"])

        if "fields" in self.settings["eddi"]:
            return {
                k: myenergi_data["eddi"][0][k] for k in self.settings["eddi"]["fields"] if k in myenergi_data["eddi"][0]
            }
        return myenergi_data["eddi"][0]


class Harvi(MyEnergi):
    """Child class of MyEnergi to get data from a Harvi CT clamp energy monitor"""

    def get_data(self):
        """
        Get the data from the Harvi

        :return: data
        :rtype: dict
        """
        self.influx_header = "myenergi,device=harvi "
        self.data = self.parse_harvi_data()
        return self.data

    def parse_harvi_data(self):
        """
        Parse the data from the MyEnergi API for the Harvi device

        :return: data
        :rtype: dict
        """
        myenergi_data = self.get_data_from_myenergi(self.settings["myenergi"]["harvi_url"])

        if "fields" in self.settings["harvi"]:
            return {
                k: myenergi_data["harvi"][0][k]
                for k in self.settings["harvi"]["fields"]
                if k in myenergi_data["harvi"][0]
            }
        return myenergi_data["harvi"][0]
