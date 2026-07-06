"""Module for sending data to InfluxDB from various sources"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT License"
__version__ = "1.0"

from .carbonintensity import CarbonIntensity
from .general import (
    load_settings,
    get_class,
    configure_logging,
    validate_settings,
    DEFAULT_LOG_MAX_BYTES,
    DEFAULT_LOG_BACKUP_COUNT,
)
from .influx import DataHandler
from .myenergi import MyEnergi, Zappi, Eddi, Harvi
from .octopus import Octopus
from .openmeteo import OpenMeteo
from .philipshue import Hue
from .speedtest import Speedtest
