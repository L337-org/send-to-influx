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

import functools
import inspect
import logging

from mcp.server.mcpserver.exceptions import ToolError

from toinflux.exceptions import ConfigError, ToInfluxError, ToolParamError
from toinflux.general import INSTANCED_SOURCES, expand_sources, get_class

# Every failure this project raises deliberately inherits ToInfluxError, and that base is what the
# translation catches. It was a tuple of two types, and a tuple is a list that goes stale: ConfigError
# was never in it, so an unconfigured device answered "Error executing tool list_fields" and nothing
# else - exactly the failure the translation exists to prevent, left live by the enumeration.
# Anything that is not a ToInfluxError is a bug in this server, and its text stays in the log.

# Set on every wrapper translate_failures() builds, so the surface guard in
# tests/test_mcp_surface.py can ask the built server - rather than the source of the
# two modules that happen to register things today - whether every advertised tool and
# resource actually carries the translation.
TRANSLATES_FAILURES = "toinflux_translates_failures"


def register_tool(server, **kwargs):
    """Register a tool whose advertised description is its dedented docstring.

    The one registration entry point for every MCP tool, read or write, and the
    reason it exists is a version difference that would otherwise ship silently.
    CPython 3.13 strips a docstring's leading indentation at compile time; 3.10-3.12
    do not, and the SDK advertises ``fn.__doc__`` verbatim (``func_doc = description
    or fn.__doc__ or ""``). So on the older half of the supported range - which the
    packaged ``.deb`` explicitly allows, ``Depends: python3 (>= 3.10)`` - every
    continuation line of every tool description reached the model with eight leading
    spaces on it: measured at 1,272 bytes of pure whitespace across the surface,
    paid for on every session that loads it, and invisible to anyone developing on
    3.13+.

    Dedenting here rather than per tool means a new tool cannot forget it, and
    ``tests/test_mcp_surface.py`` asserts that nothing bypasses this function.

    :param server: the MCPServer instance
    :param kwargs: passed to ``server.tool()`` (title, annotations, ...); a
        ``description`` given explicitly wins, as it does in the SDK
    :return: a decorator registering the function as a tool
    """

    def decorator(fn):
        kwargs.setdefault("description", inspect.cleandoc(fn.__doc__ or ""))
        return server.tool(**kwargs)(translate_failures(fn, ToolError))

    return decorator


def translate_failures(fn, error_cls):
    """Wrap a tool or resource so an anticipated failure keeps its own message.

    mcp 2.1.0 stopped putting a non-``ToolError`` exception's text on the wire: the
    SDK now treats anything else a tool raises as a crash, so the model receives only
    ``Error executing tool <name>`` and the server logs a traceback at ERROR
    (``mcp/server/mcpserver/tools/base.py``, whose docstring puts it as "a crash does
    not, so nothing from an unexpected exception reaches the client"). Under 2.0.0
    every exception was re-raised as ``ToolError(f"Error executing tool {name}: {e}")``,
    so ``ToolParamError``'s "unknown field 'evil' for source 'zappi'; choose one of..."
    - the entire point of raising it - travelled with it. Without this wrapper the
    upgrade silently emptied every one of those messages, and logged 46 raise sites'
    worth of ordinary caller mistakes as server crashes.

    The resource half of the rule is the same shape with ``ResourceError``, and was
    never a regression but a gap: 2.0.0 flattened even a deliberately raised
    ``ResourceError`` to ``Error reading resource <uri>``, so a resource could not
    explain itself at all until 2.1.0 made it possible.

    Translating here rather than per tool is the same argument as the dedent above: a
    new tool cannot forget it, because nothing registers a tool any other way. One
    implementation for both halves so the marker, and the decision about which
    failures are anticipated, exist once.

    Only ``ToInfluxError`` is translated. Anything else *is* a crash - the bare
    ``AttributeError`` that ``build_query`` used to raise, say - and the SDK withholding its text
    from the client is right, so it is deliberately left alone. Catching the base rather than a list
    of subclasses is deliberate: a new project exception is covered by inheriting, where a list has
    to be remembered, and the one time it was not, a whole class of failures went silent.

    :param fn: the tool or resource function, sync or async
    :param error_cls: the SDK error to re-raise as - ``ToolError`` for a tool,
        ``ResourceError`` for a resource; these are the only two types the SDK reads
        as deliberate rather than as a crash
    :return: a wrapper of the same sync/async-ness and signature, which the SDK needs
        to build the tool's schema and to decide whether to await it, carrying
        ``TRANSLATES_FAILURES`` so the surface guard can see it
    """
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except ToInfluxError as exc:
                raise error_cls(str(exc)) from exc

        # After functools.wraps, which copies the wrapped function's __dict__ over the
        # wrapper's and would otherwise drop this.
        setattr(async_wrapper, TRANSLATES_FAILURES, True)
        return async_wrapper

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToInfluxError as exc:
            raise error_cls(str(exc)) from exc

    setattr(wrapper, TRANSLATES_FAILURES, True)
    return wrapper


def configured_sources(settings):
    """Return the lowercased source names the MCP tools expose - the same
    ``sources:`` list the collectors run, so the two can't drift. Empty when
    nothing is configured.

    :param settings: parsed settings dict
    :return: list of lowercased source names
    """
    raw = settings.get("sources")
    if isinstance(raw, list):
        return [src.lower() for src in raw if isinstance(src, str)]
    return []


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
    if instance is not None and source.lower() not in INSTANCED_SOURCES:
        # A single-target source ignores its instance entirely - mcp_tag_filters() does not
        # consult it - so without this the value is accepted, the read runs *unscoped*, and
        # the caller is left believing it was narrowed. Refusing here rather than in each
        # tool is what makes it safe by default: this is the one function every read and
        # write tool constructs through, so a future tool that grows an instance-ish
        # parameter inherits the check instead of having to remember it.
        raise ToolParamError(
            f"source {source!r} has a single target, so it cannot be scoped to {instance!r}. "
            f"Sources with separate targets: {', '.join(sorted(INSTANCED_SOURCES)) or '(none)'}"
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
