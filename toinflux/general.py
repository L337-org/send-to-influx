"""General functions for sending data to InfluxDB"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT License"
__version__ = "1.0"

# pylint: disable=import-outside-toplevel
import os
import sys
import yaml


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
        print(f"Source {class_name} not found")
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
            print(f"Invalid or empty configuration in {settings_path}. Please check {settings_path}.")
            sys.exit(1)

        return settings
    except FileNotFoundError:
        print(f"{settings_path} not found.")
        print(f"Make sure you copy example_settings.yaml to {settings_path} and edit it.")
        sys.exit(1)
    except yaml.YAMLError as e:
        print(f"Error in {settings_path} - {e}")
        sys.exit(1)
