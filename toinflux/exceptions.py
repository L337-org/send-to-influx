"""Custom exceptions shared across toinflux data handlers."""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT"


class ToInfluxError(Exception):
    """Base for every failure this project raises deliberately.

    The MCP layer translates this one class into the SDK error whose message a client is allowed to
    see, so a new project exception is covered the moment it inherits from here. That is the whole
    reason it exists: the translation used to name a tuple of types, and a tuple is a list that goes
    stale - ``ConfigError`` was never added to it, so an unconfigured device reported "Error
    executing tool list_fields" and nothing else, which is the failure the translation was written
    to prevent.

    A bare ``Exception``/``ValueError``/``RuntimeError`` still means this server is broken, and its
    text stays in the log where it belongs.
    """


class ConfigError(ToInfluxError):
    """A configuration problem (missing/invalid settings, unknown source).

    Retrying won't fix these, so callers should stop and surface the error
    rather than applying the usual connection-failure backoff.
    """


class SourceConnectionError(ToInfluxError):
    """A data source's API call failed (network, auth, bad response).

    Raised by each handler's get_data()/API-call code - not by InfluxDB writes, which raise
    the separate InfluxWriteError (toinflux.influx) instead. Both are plain Exception
    subclasses, so both are caught by the worker loop's generic exception handling and
    treated as transient and retried with backoff; they're kept as distinct types so a
    source's own API failures and its InfluxDB write failures aren't conflated should any
    caller ever need to react to them differently.
    """


class ToolParamError(ToInfluxError, ValueError):
    """An MCP tool was called with an invalid parameter - an unknown field or
    device, a time/range/aggregation that doesn't parse, an out-of-range value,
    or a missing required argument.

    A caller/model mistake, surfaced back to the model as a tool error and never
    retried. Deliberately distinct from SourceConnectionError, which is a
    *transient* API/transport failure the collector worker loop retries with
    backoff - raising SourceConnectionError for a permanently-invalid input would
    make such a caller retry something that can never succeed. Lives here rather
    than in an MCP module so both the read/write tool layers and the source
    handlers (which implement the write primitives) can raise it without importing
    the MCP layer. A ValueError subclass so it reads as the bad-argument error it
    is.
    """
