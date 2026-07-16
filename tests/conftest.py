"""Shared pytest fixtures and settings data for unit tests."""

import copy
from unittest.mock import MagicMock, patch
import pytest
from toinflux.influx import DataHandler


@pytest.fixture(autouse=True)
def _reset_influx_write_buffers():
    """Clear DataHandler's class-level per-source write buffers before and after every test.

    The buffer is intentionally class-level (see toinflux/influx.py) so it survives the
    DataHandler instance being discarded/recreated on failure - but that also means it
    persists across tests unless reset, since every test in this session shares the same
    class object.
    """
    DataHandler._write_buffers.clear()
    yield
    DataHandler._write_buffers.clear()


_BASE_SAMPLE_SETTINGS = {
    "sources": ["hue", "zappi", "speedtest"],
    "stagger_seconds": 10,
    "default_source": "hue",
    "hue": {
        "db": "hue_db",
        "host": "hue.example.com",
        "user": "hue_user",
        "timeout": 5,
        "interval": 300,
        "temperature_units": "C",
    },
    "myenergi": {
        "zappi_url": "https://s18.myenergi.net/cgi-jstatus-Z",
        "dayhour_url": "https://s18.myenergi.net/cgi-jdayhour-Z",
        "apikey": "test_apikey",
        "timeout": 5,
    },
    "zappi": {
        "db": "zappi_db",
        "interval": 300,
        "serial": "12345",
        "fields": ["frq", "vol", "gen"],
    },
    "speedtest": {
        "db": "speedtest_db",
        "interval": 3600,
        "timeout": 60,
        "fields": ["download", "upload", "ping"],
    },
    "influx": {
        "url": "https://influx.example.com:8086",
        "user": "influx_user",
        "password": "influx_password",
        "timeout": 5,
    },
}


@pytest.fixture
def sample_settings():
    """Minimal valid toinflux settings for testing handlers."""
    return copy.deepcopy(_BASE_SAMPLE_SETTINGS)


@pytest.fixture
def mock_main_deps():
    """Patch signal, load_settings, and get_class for main() tests."""
    mock_handler = MagicMock()
    mock_handler.get_data.return_value = {}
    mock_handler.source_settings = {"interval": 60}
    with (
        patch("sendtoinflux.signal.signal"),
        patch("sendtoinflux.toinflux.load_settings") as mock_load_settings,
        patch("sendtoinflux.toinflux.get_class", return_value=mock_handler) as mock_get_class,
    ):
        mock_load_settings.return_value = {"default_source": "hue"}
        yield mock_handler, mock_get_class
