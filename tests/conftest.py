"""Shared pytest fixtures and settings data for unit tests."""

import copy
from unittest.mock import MagicMock, patch
import logging

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
    """Patch signal, load_settings, and get_class for main() tests.

    The settings include a real ``hue`` block because main() now expands configured
    sources into work units before dispatching, and Hue expands to one unit per
    *configured bridge*. A ``sources:`` list naming a source with no settings block at
    all would correctly expand to nothing - previously that went unnoticed only because
    get_class is mocked, so the missing block was never read.
    """
    mock_handler = MagicMock(STREAMING=False, instance=None)
    mock_handler.get_data.return_value = {}
    mock_handler.source_settings = {"interval": 60}
    with (
        patch("sendtoinflux.signal.signal"),
        patch("sendtoinflux.toinflux.load_settings") as mock_load_settings,
        patch("sendtoinflux.toinflux.get_class", return_value=mock_handler) as mock_get_class,
    ):
        mock_load_settings.return_value = {
            "sources": ["hue"],
            "hue": {"db": "hue_db", "interval": 60, "host": "hue.example.com", "user": "test_hue_user"},
        }
        yield mock_handler, mock_get_class


@pytest.fixture(autouse=True)
def _restore_root_logger_state():
    """Restore the root logger's level and handlers around every test.

    Several tests call ``configure_logging()`` for real - deliberately, since patching it would
    hide which stream logging reaches - and it sets the root level and installs a handler. Tests
    that tidied up afterwards restored handlers but not the level, so the suite finished with the
    root logger at INFO instead of WARNING. Measured across the whole suite before this fixture:
    WARNING/0 handlers in, INFO/0 handlers out.

    Nothing was visibly broken by that, which is the problem: it makes any later log-capture
    assertion depend on what ran before it, and the failure would appear as an unrelated test
    being order-dependent. Restoring centrally means a new test cannot reintroduce it by
    forgetting.
    """
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    try:
        yield
    finally:
        for handler in [h for h in root.handlers if h not in handlers]:
            root.removeHandler(handler)
            handler.close()
        root.setLevel(level)
