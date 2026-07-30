"""Device-write support for the MCP server (opt-in, per source).

The MCP server is read-only by default. A source becomes controllable only when it
both implements a write path (``DataHandler.MCP_WRITABLE``) *and* the operator opts
in with ``<source>.mcp_read_write: true``. When no source is enabled for writes,
nothing here is registered at all: a disabled capability is absent, not
present-and-refusing, so it never appears in the server's advertised surface (least
privilege).

Writes are heterogeneous - a Hue light takes on/brightness/colour temperature/
colour, a Speedtest run takes nothing, a future thermostat a setpoint - so each
writable source gets its own bespoke, well-described tool(s), wired by a per-source
registrar in ``_WRITE_TOOL_REGISTRARS`` and gated per source. The vendor logic
(name->id resolution, capability checks, the friendly-parameter->API mapping) lives
on the source class, exactly as the read tools' domain knowledge does; this module
only wires those methods up as MCPServer tools and owns the per-call handler
lifecycle (shared with the read side via ``mcp_common``).
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT License"

import logging

from toinflux.exceptions import SourceConnectionError, ToolParamError

# Shared per-call handler lifecycle (construct from current settings, close the
# session afterwards) - writes use the same plumbing as reads, from one place.
from toinflux.mcp_common import close_session, configured_sources, resolve_handler, resolve_handlers


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


def _resolve_writable_handlers(source, settings, settings_file):
    """Construct a handler per instance of a source and confirm it is enabled for writes.

    The opt-in is per *source* (``<source>.mcp_read_write``), not per instance - one
    setting covers every bridge, since they are one estate behind one settings block. The
    caller owns every session and must close them all.

    :raises ToolParamError: unknown source, no usable target, or not opted in for writes
    """
    handlers = resolve_handlers(source, settings, settings_file)
    if not handlers[0][1].mcp_write_enabled():
        for _, handler in handlers:
            close_session(handler.session)
        raise ToolParamError(
            f"source {source!r} is not enabled for device writes; set {source}.mcp_read_write: true to allow it"
        )
    return handlers


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


def _hue_list_devices_result(settings, settings_file):
    """Build the hue_list_devices payload (runs in a worker thread).

    Covers every configured bridge, each device carrying the bridge it lives on. Light ids
    are per-bridge, so the same id - and often the same name - exists on more than one
    bridge; ``bridge`` is what makes an entry unambiguous, and what ``hue_set_light``
    needs when a name or id is not unique across the estate.

    A bridge that cannot be reached does not suppress the others: its devices are absent
    and the failure is reported in ``unreachable``, so the model sees a partial list *and*
    knows it is partial rather than concluding those lights do not exist.
    """
    handlers = _resolve_writable_handlers("hue", settings, settings_file)
    try:
        devices, unreachable = [], []
        for instance, handler in handlers:
            try:
                for device in handler.mcp_list_writable_devices():
                    devices.append({**device, "bridge": instance})
            except SourceConnectionError as exc:
                logging.warning("Could not list devices on Hue bridge %s: %s", instance, exc)
                unreachable.append({"bridge": instance, "error": str(exc)})
        result = {"source": "hue", "devices": devices}
        if unreachable:
            result["unreachable"] = unreachable
        return result
    finally:
        for _, handler in handlers:
            close_session(handler.session)


def _resolve_hue_target(handlers, device, bridge):
    """Resolve ``device`` (and optional ``bridge``) to exactly one light on one bridge.

    Cross-bridge arbitration lives here rather than on the Hue class because it is about
    the *tool's* parameters: each handler already resolves a device within its own bridge,
    and this decides which handler should be asked.

    Refuses rather than guesses whenever the answer is not unique - actuating the wrong
    light is not recoverable, and with several bridges non-uniqueness is the normal case
    for ids (every bridge has a light ``1``) and common for names (``Kitchen`` on each
    floor). The error names the bridges involved and how to disambiguate.

    :param handlers: ``[(instance, handler), ...]`` from _resolve_writable_handlers
    :param device: light id or exact name, from the tool call
    :param bridge: bridge host to restrict to, or None to search every bridge
    :return: ``(instance, handler, light_id, name)`` for the single match
    :raises ToolParamError: unknown bridge, unknown device, or an ambiguous device
    :raises SourceConnectionError: a bridge needed for the decision is unreachable
    """
    if not isinstance(device, str) or not device.strip():
        raise ToolParamError(f"device must be a non-empty light id or name (got {device!r})")

    known = [instance for instance, _ in handlers]
    if bridge is not None:
        if bridge not in known:
            raise ToolParamError(f"unknown bridge {bridge!r}; configured bridges: {', '.join(known)}")
        handlers = [(instance, handler) for instance, handler in handlers if instance == bridge]

    matches = []
    for instance, handler in handlers:
        names = handler._names_by_id(handler._fetch_lights())
        for light_id, name in names.items():
            if device == light_id or device == name:
                matches.append((instance, handler, light_id, name))

    if len(matches) == 1:
        return matches[0]
    if matches:
        where = ", ".join(f"{name!r} (id {light_id}) on bridge {instance}" for instance, _, light_id, name in matches)
        raise ToolParamError(
            f"device {device!r} is not unique across your bridges - it matches {where}. "
            f"Pass 'bridge' to say which one, and use the light id if the name repeats on that bridge too"
        )
    scope = f" on bridge {bridge}" if bridge is not None else ""
    raise ToolParamError(
        f"unknown device {device!r}{scope}; call hue_list_devices for the ids, names and bridges available"
    )


def _hue_set_light_result(settings, settings_file, *, device, on, brightness_pct, color_temp_k, color, bridge=None):
    """Build the hue_set_light payload (runs in a worker thread)."""
    handlers = _resolve_writable_handlers("hue", settings, settings_file)
    try:
        instance, handler, light_id, _ = _resolve_hue_target(handlers, device, bridge)
        # Act by light id on the bridge that owns it: the name may repeat elsewhere, and
        # only this handler is bound to that bridge's host and token.
        result = handler.mcp_set_device_state(
            light_id, on=on, brightness_pct=brightness_pct, color_temp_k=color_temp_k, color=color
        )
        result["bridge"] = instance
        logging.info(
            "MCP write applied to hue device %r on bridge %s: %s",
            result.get("device"),
            instance,
            result.get("applied"),
        )
        return result
    finally:
        for _, handler in handlers:
            close_session(handler.session)


def _speedtest_run_result(settings, settings_file):
    """Build the speedtest_run payload (runs in a worker thread)."""
    handler = _resolve_writable_handler("speedtest", settings, settings_file)
    try:
        return handler.mcp_trigger_run()
    finally:
        close_session(handler.session)


def _register_hue_write_tools(server, settings, settings_file):
    """Register Hue's write tools (light/plug control)."""
    import anyio

    @server.tool()
    async def hue_list_devices() -> dict:
        """List the controllable Hue lights and plugs across every configured bridge, each
        with its id, name, the bridge it is on, and the controls it supports (on/off,
        brightness, colour temperature, colour), plus the kelvin range for
        colour-temperature lights.

        Call this before `hue_set_light` to get exact ids/names and see what a given light
        can do - an unknown or ambiguous device, or a control the light lacks, is rejected
        there. Light ids are per-bridge, so the same id (and often the same name) appears on
        more than one bridge: use the `bridge` value to tell them apart. If a bridge is
        unreachable its lights are absent and it is listed under `unreachable`, so a short
        list means "could not ask", not "no such light"."""
        return await anyio.to_thread.run_sync(_hue_list_devices_result, settings, settings_file)

    @server.tool()
    async def hue_set_light(
        device: str,
        on: "bool | None" = None,
        brightness_pct: "float | None" = None,
        color_temp_k: "float | None" = None,
        color: "str | None" = None,
        bridge: "str | None" = None,
    ) -> dict:
        """Set a Hue light or plug's state. This changes a real device; to read its
        state use `get_current_state`, or `query_history` for history.

        Get exact ids/names and each light's supported controls from
        `hue_list_devices` first. An unknown device, an ambiguous name (use the id
        instead), a value out of range, both `color_temp_k` and `color` at once, a
        control the light doesn't have, or setting nothing all return an error
        *before* any change. A transport failure mid-write is reported, not hidden;
        the bridge applies fields one at a time, so an error can mean part of the
        change already took effect - re-read to confirm.

        - device: the light id or its exact name (from `hue_list_devices`).
        - bridge: which bridge the light is on, when more than one is configured. Only
          needed if `device` is not unique across them - light ids repeat on every bridge,
          and names often do too, so an ambiguous device is refused with the bridges listed
          rather than guessed at.
        - on: turn on (true) / off (false); omit to leave unchanged.
        - brightness_pct: 0-100 for dimmable lights; 0 is the lowest on-brightness,
          not off (use on=false). Omit to leave unchanged.
        - color_temp_k: white colour temperature in kelvin (~2000 warm to ~6500
          cool), clamped to the light's range; colour-temp/colour lights only.
        - color: a colour as an '#rrggbb' hex or a name (red, warm white, ...);
          colour lights only.

        Setting brightness/temperature/colour turns the light on unless on=false.
        Returns the resolved device and the state actually applied."""
        return await anyio.to_thread.run_sync(
            lambda: _hue_set_light_result(
                settings,
                settings_file,
                device=device,
                on=on,
                brightness_pct=brightness_pct,
                color_temp_k=color_temp_k,
                color=color,
                bridge=bridge,
            )
        )

    return server


def _register_speedtest_write_tools(server, settings, settings_file):
    """Register Speedtest's write tool (trigger a run)."""
    import anyio

    @server.tool()
    async def speedtest_run() -> dict:
        """Run an internet speed test now, on the host this server runs on, and
        return the result (download/upload throughput and latency). Use this for an
        on-demand check; `get_current_state`/`query_history` report the last
        recorded run without starting a new one.

        A run takes up to a couple of minutes and saturates the connection while it
        runs. Only one runs at a time per host: if a scheduled or triggered run is
        already in progress, that's reported rather than a second test started. The
        result is also recorded to InfluxDB like a scheduled run (best-effort; a
        failed recording is flagged, not fatal). Takes no arguments."""
        return await anyio.to_thread.run_sync(_speedtest_run_result, settings, settings_file)

    return server


# Per-source write-tool registrars, keyed by source name. A writable, opted-in
# source with no entry here is a wiring bug (writable but no tools) - logged in
# register_write_tools, not silently ignored.
_WRITE_TOOL_REGISTRARS = {
    "hue": _register_hue_write_tools,
    "speedtest": _register_speedtest_write_tools,
}


def register_write_tools(server, settings, settings_file=None, enabled_sources=None):
    """Register each write-enabled source's own write tool(s) on a MCPServer server.
    When no source is enabled for writes, nothing is registered - the write
    capability is entirely absent from the server.

    :param server: the MCPServer instance
    :param settings: parsed settings dict
    :param settings_file: settings path, for re-resolving handlers per call
    :param enabled_sources: the pre-computed write-enabled source list, if the
        caller already has it (build_mcp_server shares one computation with
        register_prompts); ``None`` computes it here (constructing a handler per
        source), so the function still stands alone.
    :return: the server
    """
    enabled = writable_enabled_sources(settings, settings_file) if enabled_sources is None else enabled_sources
    if not enabled:
        return server
    logging.info("MCP device-write tools enabled for: %s", ", ".join(enabled))
    for source in enabled:
        registrar = _WRITE_TOOL_REGISTRARS.get(source)
        if registrar is None:
            logging.warning("Source %r is write-enabled but has no MCP write tools wired - skipping", source)
            continue
        registrar(server, settings, settings_file)
    return server
