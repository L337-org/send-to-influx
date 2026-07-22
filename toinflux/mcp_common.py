"""Shared plumbing for the MCP server's tool modules.

The read tools (:mod:`toinflux.mcp_read`), write tools
(:mod:`toinflux.mcp_write`), and the resource/prompt modules all follow the same
per-call handler lifecycle: construct a :class:`~toinflux.influx.DataHandler` for
a source from the *current* settings, use it, then close its ``requests.Session``.
That lifecycle - source resolution, handler construction, best-effort session
close - lives here so the tool modules import it from one shared place rather than
from each other.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT License"

import logging

from toinflux.exceptions import ConfigError, ToolParamError
from toinflux.general import get_class, resolve_default_source


def configured_sources(settings):
    """Return the lowercased source names the MCP tools expose - the same
    ``sources:`` list the collectors run, so the two can't drift. Falls back to
    the single default source when no list is configured.

    :param settings: parsed settings dict
    :return: list of lowercased source names
    """
    raw = settings.get("sources")
    if isinstance(raw, list) and raw:
        return [src.lower() for src in raw if isinstance(src, str)]
    # Normalise the default-source fallback the same way: lowercase it, and drop
    # a non-string value (YAML coerces `default_source: no` to False) so callers
    # always get a list[str] - a mixed-case default would otherwise never match
    # source.lower(), and a non-string would crash the error-message join.
    default = resolve_default_source(settings)
    return [default.lower()] if isinstance(default, str) else []


def resolve_handler(source, settings, settings_file):
    """Construct the DataHandler for a configured source, or raise
    ``ToolParamError`` if the name isn't one the MCP tools expose. Case-insensitive,
    matching the collector factory. The caller owns the returned handler's session
    and must close it (see :func:`close_session`).

    :param source: source name from a tool argument
    :param settings: parsed settings dict
    :param settings_file: settings path, threaded to the handler's own load
    :return: a constructed DataHandler subclass instance
    :raises ToolParamError: source is missing/non-string, unknown, or unusable
    """
    if not isinstance(source, str) or not source.strip():
        raise ToolParamError(f"source must be a non-empty string (got {source!r})")
    available = configured_sources(settings)
    if source.lower() not in available:
        raise ToolParamError(
            f"unknown source {source!r}; available sources: {', '.join(sorted(available)) or '(none)'}"
        )
    try:
        return get_class(source, settings_file)
    except ConfigError as exc:
        raise ToolParamError(f"source {source!r} is not usable: {exc}") from exc


def close_session(session):
    """Best-effort close of a handler's ``requests.Session``, swallowing any error -
    this runs in cleanup paths and must never mask the real result or exception.

    :param session: the handler's requests.Session
    """
    try:
        session.close()
    except Exception:  # pragma: no cover - close() shouldn't raise; never let cleanup break a tool
        logging.debug("Ignoring error closing an MCP handler session", exc_info=True)
