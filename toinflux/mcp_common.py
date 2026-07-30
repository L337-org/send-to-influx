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
from toinflux.general import expand_sources, get_class, resolve_default_source


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


def resolve_handlers(source, settings, settings_file):
    """Construct one handler per *instance* of a configured source.

    Most sources have a single target and yield one handler with ``instance=None`` -
    identical to ``resolve_handler()``. An instanced source (a Hue install with more than
    one bridge) yields one handler per bridge, so a tool can report on or act against all
    of them rather than silently seeing only the first.

    Uses ``expand_sources()``, the same function the collectors use to decide what runs, so
    the MCP surface and the collectors cannot disagree about which bridges exist.

    The caller owns every returned handler's session and must close them all (see
    :func:`close_session`) - typically in a ``finally``, since a partial failure part-way
    through the list still leaves earlier sessions open.

    :param source: source name from a tool argument
    :param settings: parsed settings dict
    :param settings_file: settings path, threaded to each handler's own load
    :return: list of ``(instance, handler)``, instance None for a single-target source
    :raises ToolParamError: source is missing/non-string, unknown, unusable, or - for an
        instanced source - has no usable target at all (a Hue install whose bridges have no
        tokens), which would otherwise return an empty result that looks like "no devices"
        rather than "not configured"
    """
    if not isinstance(source, str) or not source.strip():
        raise ToolParamError(f"source must be a non-empty string (got {source!r})")
    available = configured_sources(settings)
    if source.lower() not in available:
        raise ToolParamError(
            f"unknown source {source!r}; available sources: {', '.join(sorted(available)) or '(none)'}"
        )
    units = expand_sources([source.lower()], settings)
    if not units:
        raise ToolParamError(
            f"source {source!r} has no usable target configured - nothing to report on. "
            f"Run 'send-to-influx --check-config' for the details"
        )
    handlers = []
    try:
        for _, instance in units:
            handlers.append((instance, get_class(source, settings_file, instance=instance)))
    except ConfigError as exc:
        for _, handler in handlers:
            close_session(handler.session)
        raise ToolParamError(f"source {source!r} is not usable: {exc}") from exc
    return handlers


def resolve_handler(source, settings, settings_file, instance=None):
    """Construct the DataHandler for a configured source, or raise
    ``ToolParamError`` if the name isn't one the MCP tools expose. Case-insensitive,
    matching the collector factory. The caller owns the returned handler's session
    and must close it (see :func:`close_session`).

    :param source: source name from a tool argument
    :param settings: parsed settings dict
    :param settings_file: settings path, threaded to the handler's own load
    :param instance: which instance of the source to construct for - a Hue bridge host, or
        None for a single-target source (and, for Hue, the first configured bridge)
    :return: a constructed DataHandler subclass instance
    :raises ToolParamError: source is missing/non-string, unknown, unusable, or named an
        instance that is not configured
    """
    if not isinstance(source, str) or not source.strip():
        raise ToolParamError(f"source must be a non-empty string (got {source!r})")
    available = configured_sources(settings)
    if source.lower() not in available:
        raise ToolParamError(
            f"unknown source {source!r}; available sources: {', '.join(sorted(available)) or '(none)'}"
        )
    try:
        handler = get_class(source, settings_file, instance=instance)
    except ConfigError as exc:
        raise ToolParamError(f"source {source!r} is not usable: {exc}") from exc

    if instance is not None:
        # Force the instance to resolve now. Construction does not touch it, so an
        # unconfigured one would otherwise be accepted here and surface much later - as a
        # raw ConfigError from deep inside schema building, bypassing the ToolParamError
        # wrapping the MCP layer relies on to distinguish a caller mistake from a transport
        # failure, and leaking this handler's session because the caller never receives it.
        # mcp_tag_filters() is the resolution point (Hue looks its bridge up there), so
        # calling it is what turns "wrong bridge" into an immediate, clean refusal.
        try:
            handler.mcp_tag_filters()
        except ConfigError as exc:
            close_session(handler.session)
            raise ToolParamError(f"source {source!r} is not usable: {exc}") from exc
    return handler


def close_session(session):
    """Best-effort close of a handler's ``requests.Session``, swallowing any error -
    this runs in cleanup paths and must never mask the real result or exception.

    :param session: the handler's requests.Session
    """
    try:
        session.close()
    except Exception:  # pragma: no cover - close() shouldn't raise; never let cleanup break a tool
        logging.debug("Ignoring error closing an MCP handler session", exc_info=True)
