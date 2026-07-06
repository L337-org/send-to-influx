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
    """A data source's API call failed (network, auth, bad response).

    Raised by each handler's get_data()/API-call code - not by InfluxDB writes, which raise
    the separate InfluxWriteError (toinflux.influx) instead. Both are plain Exception
    subclasses, so both are caught by the worker loop's generic exception handling and
    treated as transient and retried with backoff; they're kept as distinct types so a
    source's own API failures and its InfluxDB write failures aren't conflated should any
    caller ever need to react to them differently.
    """
