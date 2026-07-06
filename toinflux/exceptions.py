"""Custom exceptions shared across toinflux data handlers."""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT License"
__version__ = "1.0"


class ConfigError(Exception):
    """A configuration problem (missing/invalid settings, unknown source).

    Retrying won't fix these, so callers should stop and surface the error
    rather than applying the usual connection-failure backoff.
    """


class SourceConnectionError(Exception):
    """A data source or InfluxDB API call failed (network, auth, bad response).

    These are treated as transient and retried with backoff by the worker loop.
    """
