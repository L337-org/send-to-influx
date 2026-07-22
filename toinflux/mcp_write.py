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
only wires those methods up as FastMCP tools and owns the per-call handler
lifecycle (shared with the read side via ``mcp_common``).
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


def _hue_list_devices_result(settings, settings_file):
    """Build the hue_list_devices payload (runs in a worker thread)."""
    handler = _resolve_writable_handler("hue", settings, settings_file)
    try:
        return {"source": "hue", "devices": handler.mcp_list_writable_devices()}
    finally:
        close_session(handler.session)


def _hue_set_light_result(settings, settings_file, *, device, on, brightness_pct, color_temp_k, color):
    """Build the hue_set_light payload (runs in a worker thread)."""
    handler = _resolve_writable_handler("hue", settings, settings_file)
    try:
        result = handler.mcp_set_device_state(
            device, on=on, brightness_pct=brightness_pct, color_temp_k=color_temp_k, color=color
        )
        logging.info("MCP write applied to hue device %r: %s", result.get("device"), result.get("applied"))
        return result
    finally:
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
        """List the controllable Hue lights and plugs, each with its id, name and
        the controls it supports (on/off, brightness, colour temperature, colour),
        plus the kelvin range for colour-temperature lights.

        Call this before `hue_set_light` to get exact ids/names and see what a
        given light can do - an unknown/ambiguous name, or a control the light
        lacks, is rejected there."""
        return await anyio.to_thread.run_sync(_hue_list_devices_result, settings, settings_file)

    @server.tool()
    async def hue_set_light(
        device: str,
        on: "bool | None" = None,
        brightness_pct: "float | None" = None,
        color_temp_k: "float | None" = None,
        color: "str | None" = None,
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
    """Register each write-enabled source's own write tool(s) on a FastMCP server.
    When no source is enabled for writes, nothing is registered - the write
    capability is entirely absent from the server.

    :param server: the FastMCP instance
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
