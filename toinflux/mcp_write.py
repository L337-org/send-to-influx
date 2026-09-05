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
__license__ = "MIT"

import logging

from mcp.types import ToolAnnotations

from toinflux.exceptions import SourceConnectionError, ToolParamError

# Shared per-call handler lifecycle (construct from current settings, close the
# session afterwards) - writes use the same plumbing as reads, from one place.
from toinflux.mcp_common import (
    close_session,
    configured_sources,
    register_tool,
    resolve_handler,
    resolve_handlers,
)


def writable_enabled_sources(settings, settings_file=None):
    """Return the configured sources that are both writable and opted in.

    Opting in means ``<source>.mcp_read_write: true``. Each handler is constructed to
    check, then its session closed - this runs once at server-build time to decide
    whether to register write tools at all.

    Args:
        settings: parsed settings dict
        settings_file: settings path, for constructing handlers

    Returns:
        list of source names enabled for writes
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

    Raises:
        ToolParamError: unknown source, no usable target, or not opted in for writes
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
    """Construct a handler for a source and confirm it is enabled for writes.

    Raises ToolParamError otherwise. The caller owns the returned handler's session and
    must close it.

    Raises:
        ToolParamError: unknown source, or a source not opted in for writes
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


def _hue_matches_across_bridges(handlers, device, bridge):
    """Search every given bridge for ``device``, returning ``(matches, unreachable)``.

    Split out of :func:`_resolve_hue_target` to keep that function's decision logic readable
    - this half is the I/O, that half is the arbitration.

    A bridge that cannot be answered is collected rather than fatal *only* when it was being
    consulted to arbitrate. If the caller named a bridge, or it is the only one configured,
    the failure is against the target itself and is propagated as the transport error it is.

    Searches ``mcp_list_writable_devices()`` - the source's public write allowlist - rather
    than the bridge response directly, so what ``hue_set_light`` will act on is by
    construction what ``hue_list_devices`` advertises, instead of two paths that happen to
    derive the same answer.

    Args:
        handlers: ``[(instance, handler), ...]`` to search
        device: light id or exact name to match
        bridge: the bridge the caller named, or None

    Returns:
        ``([(instance, handler, light_id, name), ...], [(instance, exc), ...])``

    Raises:
        SourceConnectionError: the named or only bridge could not be reached
    """
    matches, unreachable = [], []
    for instance, handler in handlers:
        try:
            names = {entry["id"]: entry["name"] for entry in handler.mcp_list_writable_devices()}
        except SourceConnectionError as exc:
            if bridge is not None or len(handlers) == 1:
                raise
            logging.warning("Could not list lights on Hue bridge %s while resolving %r: %s", instance, device, exc)
            unreachable.append((instance, exc))
            continue
        for light_id, name in names.items():
            if device == light_id or device == name:
                matches.append((instance, handler, light_id, name))
    return matches, unreachable


def _resolve_hue_target(handlers, device, bridge):
    """Resolve ``device`` (and optional ``bridge``) to exactly one light on one bridge.

    Cross-bridge arbitration lives here rather than on the Hue class because it is about
    the *tool's* parameters: each handler already resolves a device within its own bridge,
    and this decides which handler should be asked.

    Refuses rather than guesses whenever the answer is not unique - actuating the wrong
    light is not recoverable, and with several bridges non-uniqueness is the normal case
    for ids (every bridge has a light ``1``) and common for names (``Kitchen`` on each
    floor). The error names the bridges involved and how to disambiguate.

    One bridge being unreachable does not make every write impossible. If ``bridge`` was
    given, that bridge is the only one consulted and a connection failure against it is
    propagated. If it was not, an unreachable bridge means the estate cannot be searched
    completely, so the result is a ``ToolParamError`` naming the missing bridge and pointing
    at ``bridge`` - a refusal the caller can act on, rather than a transport error inviting
    an identical retry. Acting on a lone match found elsewhere is deliberately *not* the
    behaviour: the silent bridge may carry the same name.

    Args:
        handlers: ``[(instance, handler), ...]`` from _resolve_writable_handlers
        device: light id or exact name, from the tool call
        bridge: bridge host to restrict to, or None to search every bridge

    Returns:
        ``(instance, handler, light_id, name)`` for the single match

    Raises:
        ToolParamError: unknown bridge, unknown device, an ambiguous device, or a bridge that could not be reached while
            arbitrating across several
        SourceConnectionError: the bridge named in ``bridge`` is unreachable, or the only configured bridge is

    Raises:
        ToolParamError: the named light is unknown, ambiguous, or on no configured bridge
    """
    if not isinstance(device, str) or not device.strip():
        raise ToolParamError(f"device must be a non-empty light id or name (got {device!r})")

    known = [instance for instance, _ in handlers]
    if bridge is not None:
        if bridge not in known:
            raise ToolParamError(f"unknown bridge {bridge!r}; configured bridges: {', '.join(known)}")
        handlers = [(instance, handler) for instance, handler in handlers if instance == bridge]

    matches, unreachable = _hue_matches_across_bridges(handlers, device, bridge)

    if unreachable:
        # Refuse, but *actionably*. Acting on the single match found would risk actuating the
        # wrong light: the bridge that did not answer may carry that name too, and this
        # function exists precisely because that mistake is not recoverable. Equally, one
        # bridge being down must not make every write impossible - so say which bridge went
        # missing and that 'bridge' proceeds without consulting it. Raised as a
        # ToolParamError, not the underlying SourceConnectionError: the caller has something
        # different to do, whereas a transient-transport error invites an identical retry.
        # instance!r, so a target whose configured name contains a newline cannot split this
        # message into two lines in the log or the client's error text - same reasoning as
        # Nuki.send_data()'s per-lock failures.
        missing = ", ".join(f"{instance!r} ({exc})" for instance, exc in unreachable)
        found = (
            ", ".join(f"{name!r} (id {light_id}) on bridge {instance}" for instance, _, light_id, name in matches)
            or "none"
        )
        raise ToolParamError(
            f"cannot determine which light {device!r} refers to: bridge {missing} could not be reached, so "
            f"the estate could not be searched completely. Matches on the bridges that answered: {found}. "
            f"Pass 'bridge' to act on one bridge without consulting the others"
        )

    if len(matches) == 1:
        return matches[0]
    if matches:
        where = ", ".join(f"{name!r} (id {light_id}) on bridge {instance}" for instance, _, light_id, name in matches)
        # The hint has to follow the actual cause. Hue allows two lights to share a name on
        # a *single* bridge, and there 'bridge' cannot disambiguate anything - only the id
        # can. Suggesting it would send the caller down a dead end, and calling the
        # ambiguity cross-bridge would be simply untrue.
        if len({instance for instance, _, _, _ in matches}) == 1:
            hint = "Use the light id instead of the name"
        else:
            hint = "Pass 'bridge' to say which one, and use the light id if the name repeats on that bridge too"
        raise ToolParamError(f"device {device!r} is ambiguous - it matches {where}. {hint}")
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


def _speedtest_run_result(settings, settings_file, host=None):
    """Build the speedtest_run payload (runs in a worker thread)."""
    handler = _resolve_writable_handler("speedtest", settings, settings_file)
    try:
        return handler.mcp_trigger_run(host=host)
    finally:
        close_session(handler.session)


def _register_hue_write_tools(server, settings, settings_file):
    """Register Hue's write tools (light/plug control)."""
    import anyio

    # A read despite living in the write registrar: it only lists devices and
    # their capabilities, changing nothing - grouped here because it exists
    # purely to feed hue_set_light's device/bridge arguments.
    @register_tool(
        server,
        title="List Hue Devices",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def hue_list_devices() -> dict:
        """List the controllable Hue lights and plugs across every configured bridge,
        each with its id, name, the bridge it is on, and the controls it supports
        (on/off, brightness, colour temperature, colour), plus the kelvin range for
        colour-temperature lights.

        Hue-only, and about controllability: it exists to feed `hue_set_light`'s
        `device` and `bridge` arguments, where an unknown or ambiguous device, or a
        control the light lacks, is rejected. To read what any source reports right
        now - including which Hue lights are on - use the source-agnostic
        `get_current_state` instead.

        Reads the bridges and changes nothing. Light ids are per-bridge, so the same
        id (and often the same name) appears on more than one bridge: use the
        `bridge` value to tell them apart. An
        unreachable bridge's lights are absent and the bridge is named under
        `unreachable`, so a short list means "could not ask", not "no such light" -
        and that includes the case where no bridge answers at all, which returns an
        empty `devices` list rather than an error.
        """
        return await anyio.to_thread.run_sync(_hue_list_devices_result, settings, settings_file)

    # Additive/reversible (turns a light on/off, adjusts brightness/colour) and
    # idempotent (setting the same state twice ends in the same state), so
    # neither destructive_hint nor a false idempotent_hint would be accurate.
    @register_tool(
        server,
        title="Set Hue Light State",
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
        ),
    )
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
        *before* any change. A transport failure
        mid-write is reported, not hidden; the bridge applies fields one at a time, so
        an error can mean part of the change already took effect - re-read to confirm.

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
        Returns the resolved device and the state actually applied.
        """
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

    # Not idempotent - each call runs a fresh test and can return a different
    # result - and, unlike every other tool here, genuinely open-world: it
    # picks a best server from speedtest.net's public network rather than a
    # fixed set of configured devices.
    @register_tool(
        server,
        title="Run Speed Test",
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
        ),
    )
    async def speedtest_run(host: "str | None" = None) -> dict:
        """Run an internet speed test now, on the host this server runs on, and
        return the result (download/upload throughput and latency). Use this for an
        on-demand check; `get_current_state`/`query_history` report the last recorded
        run without starting a new one, and `get_data_range` how far the records go.

        The result names the machine that ran it in `host`, which matters when several
        hosts collect into one database: it can only ever measure *this* machine's
        connection, because each collecting host runs its own process with no listener
        for the others to be reached through. Passing `host` asserts which machine you
        expect; naming a different one is refused rather than measured here and returned
        as though it were that host's. To ask about another host, query its recorded
        history with `query_history` and `instance` instead.

        A run takes up to a couple of minutes and saturates the connection while it
        runs. Only one runs at a time per host: if a scheduled or triggered run is
        already in progress, that's reported rather than a second test started. The
        result is also recorded to InfluxDB like a scheduled run (best-effort; a
        failed recording is flagged, not fatal).
        """
        return await anyio.to_thread.run_sync(_speedtest_run_result, settings, settings_file, host)

    return server


# Per-source write-tool registrars, keyed by source name. A writable, opted-in
# source with no entry here is a wiring bug (writable but no tools) - logged in
# register_write_tools, not silently ignored.
_WRITE_TOOL_REGISTRARS = {
    "hue": _register_hue_write_tools,
    "speedtest": _register_speedtest_write_tools,
}


def register_write_tools(server, settings, settings_file=None, enabled_sources=None):
    """Register each write-enabled source's own write tools on a MCPServer server.

    When no source is enabled for writes, nothing is registered - the write capability is
    entirely absent from the server.

    Args:
        server: the MCPServer instance
        settings: parsed settings dict
        settings_file: settings path, for re-resolving handlers per call
        enabled_sources: the pre-computed write-enabled source list, if the caller already has it (build_mcp_server
            shares one computation with register_prompts); ``None`` computes it here (constructing a handler per
            source), so the function still stands alone.

    Returns:
        the server
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
