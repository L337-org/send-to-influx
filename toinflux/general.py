"""General functions for sending data to InfluxDB"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT License"
__version__ = "1.0"

# pylint: disable=import-outside-toplevel
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
import yaml
from toinflux.exceptions import ConfigError

DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 3


def configure_logging(
    logfile=None, loglevel="INFO", log_max_bytes=DEFAULT_LOG_MAX_BYTES, log_backup_count=DEFAULT_LOG_BACKUP_COUNT
):
    """Configure root logger with stdout and an optional rotating file handler.

    :param logfile: path to log file; if None, logs to stdout only
    :type logfile: str or None
    :param loglevel: logging level name (e.g. "INFO", "DEBUG"); falls back to INFO if invalid
    :type loglevel: str
    :param log_max_bytes: max size in bytes before the log file is rotated
    :type log_max_bytes: int
    :param log_backup_count: number of rotated log files to keep
    :type log_backup_count: int
    """
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()

    resolved_level = getattr(logging, str(loglevel).upper(), None)
    if not isinstance(resolved_level, int):
        logging.warning("Invalid loglevel '%s'; defaulting to INFO", loglevel)
        resolved_level = logging.INFO
    root.setLevel(resolved_level)

    # Remove any handlers added by a previous call to this function, so repeated
    # calls (e.g. in tests, or if main() is invoked more than once) don't duplicate log lines.
    for handler in list(root.handlers):
        if getattr(handler, "_send_to_influx_handler", False):
            root.removeHandler(handler)
            handler.close()

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(fmt)
    stdout_handler._send_to_influx_handler = True
    root.addHandler(stdout_handler)

    if logfile:
        file_handler = RotatingFileHandler(logfile, maxBytes=log_max_bytes, backupCount=log_backup_count)
        file_handler.setFormatter(fmt)
        file_handler._send_to_influx_handler = True
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


def get_class(source, settings_file=None):
    """
    Create and return a class object for the given data source name

    This function modifies the case of the source so that the user can
    input this in any case and it will still work.

    When adding a new data source, import its class inside this function
    and add it to the classes dictionary.

    :param source: data source name
    :type source: str
    :param settings_file: path to the settings file (default: settings.yaml in the project root)
    :type settings_file: str or None
    :return: class object
    :rtype: DataHandler
    """
    from toinflux.carbonintensity import CarbonIntensity
    from toinflux.myenergi import MyEnergi, Zappi, Eddi, Harvi
    from toinflux.octopus import Octopus
    from toinflux.openmeteo import OpenMeteo
    from toinflux.philipshue import Hue
    from toinflux.speedtest import Speedtest

    classes = {
        "CarbonIntensity": CarbonIntensity,
        "Eddi": Eddi,
        "Harvi": Harvi,
        "Hue": Hue,
        "MyEnergi": MyEnergi,
        "Octopus": Octopus,
        "OpenMeteo": OpenMeteo,
        "Speedtest": Speedtest,
        "Zappi": Zappi,
    }

    class_name = next((k for k in classes if k.lower() == source.lower()), source)
    source_name = source.lower()
    try:
        my_class = classes[class_name](source_name, settings_file=settings_file)
    except KeyError:
        raise ConfigError(f"Source {class_name} not found") from None
    return my_class


def _validate_influx_block(influx):
    """Return a list of error strings for the influx configuration block."""
    errors = []
    if not influx.get("url"):
        errors.append("influx.url is required")
    if influx.get("token"):
        if not influx.get("org"):
            errors.append("influx.org is required when using token authentication (v2)")
    elif not (influx.get("user") and influx.get("password")):
        errors.append("influx requires either token+org (v2) or user+password (v1)")
    return errors


def _validate_source_block(source, settings, is_v2):
    """Return a list of error strings for a single source configuration section.

    :param is_v2: whether the influx block is configured for v2 (token) auth - v2's
        send_data() accepts either db or bucket (falling back from bucket to db), but
        v1's send_data() reads source_settings["db"] directly with no fallback, so a
        v1 config needs db specifically, not just "db or bucket"
    :type is_v2: bool
    """
    if not source:
        return []
    if source not in settings:
        return [f"no configuration section found for source '{source}'"]
    errors = []
    source_cfg = settings[source]
    if "interval" not in source_cfg:
        errors.append(f"{source}.interval is required")
    if is_v2:
        if "db" not in source_cfg and "bucket" not in source_cfg:
            errors.append(f"{source}.db (or {source}.bucket for InfluxDB v2) is required")
    elif "db" not in source_cfg:
        errors.append(f"{source}.db is required when using InfluxDB v1 (user/password) authentication")
    return errors


def validate_settings(settings, source=None):
    """Validate required keys in a parsed settings dictionary.

    :param settings: parsed settings dictionary
    :type settings: dict
    :param source: an additional specific source to validate (e.g. the --source CLI
        argument), even if it isn't in the configured sources/default_source - without
        this, --check-config --source <x> could report success while <x>'s own block
        is broken, if <x> isn't part of the normal sources list
    :type source: str or None
    :raises ConfigError: if any required settings are missing or invalid
    """
    influx = settings.get("influx", {})
    errors = _validate_influx_block(influx)
    is_v2 = bool(influx.get("token"))
    sources = settings.get("sources") or [settings.get("default_source")]
    if source and source not in sources:
        sources = [*sources, source]
    for src in sources:
        errors.extend(_validate_source_block(src, settings, is_v2))
    if errors:
        for error in errors:
            logging.critical("settings.yaml: %s", error)
        raise ConfigError("; ".join(errors))


def _apply_env_overrides(settings):
    """Override secret-bearing influx settings from the environment, if set.

    Lets a packaged/systemd deployment keep tokens and passwords out of the
    settings file on disk (e.g. in an EnvironmentFile), instead of requiring
    them in plain YAML.

    :param settings: parsed settings dictionary, modified in place
    :type settings: dict
    """
    influx = settings.get("influx")
    if not isinstance(influx, dict):
        return
    if os.environ.get("INFLUX_TOKEN"):
        influx["token"] = os.environ["INFLUX_TOKEN"]
    if os.environ.get("INFLUX_PASSWORD"):
        influx["password"] = os.environ["INFLUX_PASSWORD"]


def load_settings(settings_file=None):
    """Load settings from a YAML file and return as a dictionary.

    When the resolved path does not exist and ends with ``.yaml``, the function
    falls back to the ``.yml`` equivalent for backwards compatibility.

    ``INFLUX_TOKEN`` and ``INFLUX_PASSWORD`` environment variables, if set,
    override the corresponding values in the ``influx`` settings block, so
    secrets need not be stored in the settings file itself.

    :param settings_file: path to the settings file (absolute, or relative to the project
        root); defaults to ``settings.yaml`` in the project root when omitted
    :type settings_file: str or None
    :return: parsed settings dictionary
    :rtype: dict
    """
    if not settings_file:
        settings_file = "settings.yaml"
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
            raise ConfigError(f"Invalid or empty configuration in {settings_path}")

        _apply_env_overrides(settings)
        validate_settings(settings)
        return settings
    except FileNotFoundError:
        logging.critical(
            "%s not found. Make sure you copy example_settings.yaml to %s and edit it.", settings_path, settings_path
        )
        raise ConfigError(f"{settings_path} not found") from None
    except yaml.YAMLError as e:
        logging.critical("Error in %s - %s", settings_path, e)
        raise ConfigError(f"Error in {settings_path} - {e}") from e
