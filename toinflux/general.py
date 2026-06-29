"""General functions for sending data to InfluxDB"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT License"
__version__ = "1.0"

# pylint: disable=import-outside-toplevel
import logging
import os
import sys
import yaml


def configure_logging(logfile=None):
    """Configure root logger with stdout and an optional file handler.

    :param logfile: path to log file; if None, logs to stdout only
    :type logfile: str or None
    """
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(fmt)
    root.addHandler(stdout_handler)

    if logfile:
        file_handler = logging.FileHandler(logfile)
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)


def flatten_dict(data, parent_key="", sep="_"):
    """Flatten a nested dictionary into a single-level dictionary.

    Nested keys are joined with ``sep``. Non-dictionary values are copied
    directly to the flattened output.

    :param data: dictionary to flatten
    :type data: dict
    :param parent_key: prefix used during recursion
    :type parent_key: str
    :param sep: separator for nested keys
    :type sep: str
    :return: flattened dictionary
    :rtype: dict
    """
    flattened = {}

    for key, value in data.items():
        new_key = f"{parent_key}{sep}{key}" if parent_key else str(key)
        if isinstance(value, dict):
            flattened.update(flatten_dict(value, parent_key=new_key, sep=sep))
        else:
            flattened[new_key] = value

    return flattened


def get_class(source):
    """
    Create and return a class object for the given data source name

    This function modifies the case of the source so that the user can
    input this in any case and it will still work.

    When adding a new data source, import its class inside this function
    and add it to the classes dictionary.

    :param source: data source name
    :type name: str
    :return: class object
    :rtype: DataHandler
    """
    from toinflux.influx import DataHandler
    from toinflux.myenergi import MyEnergi, Zappi
    from toinflux.philipshue import Hue
    from toinflux.speedtest import Speedtest

    classes = {"DataHandler": DataHandler, "Hue": Hue, "MyEnergi": MyEnergi, "Zappi": Zappi, "Speedtest": Speedtest}

    class_name = next((k for k in classes if k.lower() == source.lower()), source)
    source_name = source.lower()
    try:
        my_class = classes[class_name](source_name)
    except KeyError:
        logging.error("Source %s not found", class_name)
        sys.exit(1)
    return my_class


def load_settings(settings_file="settings.yaml"):
    """Load settings from a YAML file and return as a dictionary.

    When the resolved path does not exist and ends with ``.yaml``, the function
    falls back to the ``.yml`` equivalent for backwards compatibility.

    :param settings_file: path to the settings file (absolute, or relative to the project root)
    :type settings_file: str
    :return: parsed settings dictionary
    :rtype: dict
    """
    base_dir = os.path.abspath(os.path.dirname(__file__) + "/..")
    settings_path = os.path.join(base_dir, settings_file)

    if not os.path.exists(settings_path) and settings_path.endswith(".yaml"):
        fallback_path = settings_path[:-5] + ".yml"
        if os.path.exists(fallback_path):
            settings_path = fallback_path

    try:
        with open(settings_path, encoding="utf8") as f:
            settings = yaml.safe_load(f)

        if not isinstance(settings, dict) or not settings:
            logging.critical("Invalid or empty configuration in %s. Please check %s.", settings_path, settings_path)
            sys.exit(1)

        return settings
    except FileNotFoundError:
        logging.critical("%s not found. Make sure you copy example_settings.yaml to %s and edit it.",
                         settings_path, settings_path)
        sys.exit(1)
    except yaml.YAMLError as e:
        logging.critical("Error in %s - %s", settings_path, e)
        sys.exit(1)
