"""Device-write support for the MCP server (opt-in, per source).

The MCP server is read-only by default. When a collector both implements a write
path (``DataHandler.MCP_WRITABLE``) *and* the operator opts in with
``<source>.mcp_read_write: true``, this module registers two write tools -
``list_writable_devices`` and ``set_device_state`` - onto the server. When no
source is enabled for writes, nothing is registered at all: a disabled capability
is absent, not present-and-refusing, so the tool never even appears in the
server's advertised surface (least privilege).

``set_device_state`` is a generic primitive (source + device + desired state)
that dispatches to the source class's own ``mcp_set_device_state()``. Hue is the
only writable source today; the primitive is deliberately source-agnostic so a
later controller (e.g. SI-7's PID actuation) can drive the same tool without new
MCP wiring.

The per-source device knowledge (how a name resolves to a bridge id, how the
friendly parameters map to the vendor API, how to read back the device list for
the write allowlist) lives on the source class, exactly as the read tools' domain
knowledge does - no parallel adapter hierarchy.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT License"

import logging

from toinflux.exceptions import ToolParamError

# Shared per-call handler lifecycle (construct from current settings, close the
# session afterwards) - writes use the same plumbing as reads, from one place.
from toinflux.mcp_common import close_session, configured_sources, resolve_handler


def writable_enabled_sources(settings, settings_file=None):
    """Return the configured sources that are both writable and opted in via
    ``<source>.mcp_read_write: true``. Each handler is constructed to check, then
    its session closed - this runs once at server-build time to decide whether to
    register write tools at all.

    :param settings: parsed settings dict
    :param settings_file: settings path, for constructing handlers
    :return: list of source names enabled for writes
    """
    enabled = []
    for source in configured_sources(settings):
        try:
            handler = resolve_handler(source, settings, settings_file)
        except ToolParamError:
            continue
        try:
            if handler.mcp_write_enabled():
                enabled.append(source)
        finally:
            close_session(handler.session)
    return enabled


def _resolve_writable_handler(source, settings, settings_file):
    """Construct a handler for a source and confirm it's enabled for writes, or
    raise ToolParamError. The caller owns the returned handler's session and must
    close it.

    :raises ToolParamError: unknown source, or a source not opted in for writes
    """
    handler = resolve_handler(source, settings, settings_file)
    if not handler.mcp_write_enabled():
        close_session(handler.session)
        raise ToolParamError(
            f"source {source!r} is not enabled for device writes; set {source}.mcp_read_write: true to allow it"
        )
    return handler


def _list_writable_devices_result(source, settings, settings_file):
    """Build the list_writable_devices payload (runs in a worker thread)."""
    handler = _resolve_writable_handler(source, settings, settings_file)
    try:
        devices = handler.mcp_list_writable_devices()
        return {
            "source": source,
            "devices": [{"id": light_id, "name": name} for light_id, name in sorted(devices.items())],
        }
    finally:
        close_session(handler.session)


def _set_device_state_result(settings, settings_file, *, source, device, on, brightness_pct):
    """Build the set_device_state payload (runs in a worker thread)."""
    handler = _resolve_writable_handler(source, settings, settings_file)
    try:
        result = handler.mcp_set_device_state(device, on=on, brightness_pct=brightness_pct)
        logging.info("MCP write applied to %s device %r: %s", source, result.get("device"), result.get("applied"))
        return result
    finally:
        close_session(handler.session)


def register_write_tools(server, settings, settings_file=None, enabled_sources=None):
    """Register the device-write tools on a FastMCP server, but only if at least
    one configured source is enabled for writes. When none is, nothing is
    registered - the write capability is entirely absent from the server.

    :param server: the FastMCP instance
    :param settings: parsed settings dict
    :param settings_file: settings path, for re-resolving handlers per call
    :param enabled_sources: the pre-computed write-enabled source list, if the
        caller already has it (build_mcp_server shares one computation with
        register_prompts); ``None`` computes it here (constructing a handler per
        source), so the function still stands alone.
    :return: the server
    """
    import anyio

    enabled = writable_enabled_sources(settings, settings_file) if enabled_sources is None else enabled_sources
    if not enabled:
        return server
    logging.info("MCP device-write tools enabled for: %s", ", ".join(enabled))

    @server.tool()
    async def list_writable_devices(source: str) -> dict:
        """List the controllable devices for a write-enabled source, each with the
        id and name to pass to `set_device_state`.

        The read-side counterpart is `list_fields` (queryable fields); this lists
        *controllable* targets. Call it before `set_device_state` to get exact
        ids/names - an unknown or ambiguous name there is rejected. `source` must
        be a source with writes enabled (`<source>.mcp_read_write: true`); another
        source returns an error."""
        return await anyio.to_thread.run_sync(_list_writable_devices_result, source, settings, settings_file)

    @server.tool()
    async def set_device_state(
        source: str,
        device: str,
        on: "bool | None" = None,
        brightness_pct: "float | None" = None,
    ) -> dict:
        """Set a device's state on a write-enabled source (e.g. a Hue light/plug).
        This changes a real device; to read history instead, use `query_history`.

        Get exact ids/names from `list_writable_devices` first: an unknown device,
        an ambiguous name (resolve it with the id instead), out-of-range
        brightness, or giving neither `on` nor `brightness_pct` all return an
        error *before* any device change. A bridge/transport failure during the
        write is reported rather than silently dropped; note the bridge applies a
        multi-field change per-field, so an error can mean part of the change
        (e.g. `on` but not `bri`) already took effect - re-read to confirm.

        - device: the device id or its exact name (from `list_writable_devices`).
        - on: turn the device on (true) or off (false); omit to leave unchanged.
        - brightness_pct: target brightness 0-100 for dimmable lights; omit to
          leave unchanged. Setting a brightness turns the light on unless `on` is
          explicitly false; 0 is the lowest on-brightness, not off (use on=false).

        At least one of `on`/`brightness_pct` must be given. Returns the source,
        resolved device id/name, and the state actually applied. Only sources with
        `<source>.mcp_read_write: true` expose this tool at all."""
        return await anyio.to_thread.run_sync(
            lambda: _set_device_state_result(
                settings, settings_file, source=source, device=device, on=on, brightness_pct=brightness_pct
            )
        )

    return server
