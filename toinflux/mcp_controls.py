"""Reading the control loops over MCP: what this installation controls, and whether it is.

Two tools, both read-only. They are registered only where ``controls.enabled`` is true,
because a capability that is switched off should be absent from the advertised surface
rather than present and refusing - a tool a model can see is a tool it will try, and a
refusal costs a round trip to learn what the tool list could have said for free.

**Reading is not behind the write flag.** A control document holds no secrets: it says
what is held at what, and which devices move. Being able to ask "what is this install
controlling, and is it actually running" is most of the value, and gating it behind the
same switch that permits a model to *change* a heating loop would mean nobody could look
without also granting that.

**The supervisor is passed in rather than looked up.** It lives in a thread of this same
process, so this is an ordinary method call and not an RPC - but the MCP server can also
run with no supervisor at all (controls switched off, or every stored control unusable),
and that is a third answer rather than "not running". A caller told a control is stopped
when in truth nothing was asked would go looking for a crash that never happened.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import logging

from toinflux.controls import controls_enabled, load_control, validate_control
from toinflux.controls import list_controls as stored_control_names
from toinflux.exceptions import ConfigError
from toinflux.mcp_common import register_tool


def _supervision_by_name(supervisor):
    """Return name -> the supervisor's account of that control, or None where there is none.

    Args:
        supervisor (Supervisor or None): the running supervisor, where there is one

    Returns:
        dict or None: name to :class:`~toinflux.supervision.ControlStatus`, or None where
        nothing is supervising - which is a different answer from an empty mapping, and
        the reason this does not simply return ``{}``
    """
    if supervisor is None:
        return None
    return {status.name: status for status in supervisor.status()}


def _describe(name, settings_file, supervision):
    """Return one control's entry for the list, including why it cannot be used.

    A control that cannot be read, or that reads and is the wrong shape, is reported rather
    than omitted. Left out, it would read as "no such control" to a caller that had just
    been told the name by somebody else, and the next question would be about the wrong
    thing entirely.

    **The shaped fields are only read once the document has been validated.** A document
    that parses as YAML can still be any shape at all - ``output`` a string, ``devices`` a
    number - and the store guarantees only that it is a mapping. Reaching into one of those
    raises an AttributeError or a TypeError, which here would take out the whole listing
    for every *other* control as well: one bad document becoming everybody's outage, which
    is the failure this subsystem is organised against.

    **Supervision is reported whatever the document says.** A control whose file was edited
    into nonsense a minute ago is still running the document it started with, and "this is
    unusable" and "this is currently actuating your heaters" are both true and the second
    is the more urgent.

    Args:
        name (str): the control's name
        settings_file (str or None): the settings path the process was started with
        supervision (dict or None): name to status, or None where nothing is supervising

    Returns:
        dict: the control's entry
    """
    entry: dict = {"name": name}
    entry.update(_supervision_of(name, supervision))
    try:
        document = load_control(name, settings_file)
    except ConfigError as exc:
        entry["readable"] = False
        entry["error"] = repr(exc)
        return entry
    entry["readable"] = True
    errors = validate_control(name, document)
    entry["valid"] = not errors
    if errors:
        entry["errors"] = errors
        return entry
    entry["enabled"] = document.get("enabled", True)
    entry["devices"] = sorted(document.get("devices") or {})
    entry["cycle_seconds"] = (document.get("output") or {}).get("cycle_seconds")
    return entry


def _supervision_of(name, supervision):
    """Return what the supervisor has to say about one control, in its own right.

    Separated from reading the document because the two are independent: a control can be
    running from a document that has since been edited into nonsense, and the answer to
    "is it actuating" does not depend on the answer to "can this file be used".

    Args:
        name (str): the control's name
        supervision (dict or None): name to status, or None where nothing is supervising

    Returns:
        dict: the supervision fields for this control's entry
    """
    if supervision is None:
        # Not false: nothing is watching, so nothing knows. See the module docstring.
        return {"running": None}
    status = supervision.get(name)
    if status is None:
        # Stored but not supervised, which is what a control added since the collector
        # started looks like where no reload has reached the supervisor yet, and what an
        # unusable one looks like because the supervisor skipped it.
        return {"running": False, "supervised": False}
    fields = {
        "supervised": True,
        "running": status.running,
        "pid": status.pid,
        "failures": status.failures,
    }
    if status.silent_for is not None:
        fields["silent_for_seconds"] = round(status.silent_for, 1)
    if status.restart_in is not None:
        fields["restart_in_seconds"] = round(status.restart_in, 1)
    return fields


def register_control_tools(server, settings, settings_file=None, supervisor=None):
    """Register the read-only control tools, where the control subsystem is switched on.

    Args:
        server (MCPServer): the MCPServer instance
        settings (dict): the parsed settings document
        settings_file (str or None): the settings path the process was started with
        supervisor (Supervisor or None): the running supervisor, where there is one

    Returns:
        MCPServer: the same server, for chaining
    """
    from mcp.types import ToolAnnotations

    import anyio

    if not controls_enabled(settings):
        # Absent rather than refusing: see the module docstring.
        return server
    logging.info("MCP control tools enabled (read-only)")

    @register_tool(
        server,
        title="List Control Loops",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def list_controls() -> dict:  # noqa: DOC201
        """List this installation's control loops: each one's name, whether it is enabled,
        the devices it actuates, its cycle length, and whether its process is running right
        now.

        A control is a closed loop that holds something at a target by switching devices -
        a room at a temperature, say. This describes the loops themselves; to read what a
        device is doing at this moment use `get_current_state`, and for a control's full
        stored document including its rules and gains use `get_control`.

        `running` has three values, and the third is not a failure: `true` and `false` mean
        the supervisor knows, while `null` means nothing is supervising and so nothing can
        say - which happens when the subsystem is on but no control could be started.
        `supervised: false` on a control that is stored means the supervisor has not taken
        it on yet.

        A control that cannot be used is listed rather than omitted, so a name you were
        given does not simply vanish: `readable: false` with the reason where the file will
        not parse, or `valid: false` with `errors` where it parses and is the wrong shape.
        Either way its supervision fields are still reported, because a control whose file
        was edited into nonsense is still running the document it started with. Reads
        stored files and changes nothing.
        """
        return await anyio.to_thread.run_sync(_list_controls_result, settings_file, supervisor)

    @register_tool(
        server,
        title="Get Control Loop",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def get_control(name: str) -> dict:  # noqa: DOC101,DOC103,DOC108,DOC201
        """Return one control loop's stored document exactly as held on disk: its inputs,
        its PID gains, its setpoint and gating rules, its stage ladder and its devices.

        Get the name from `list_controls` first, which also says whether the control is
        running. This is the configuration rather than the live state; for what the devices
        are doing now use `get_current_state`.

        An unknown name, or one the store will not accept as a filename, is an error naming
        the control asked for. So is a document that is present but will not parse - which
        `list_controls` reports as `readable: false` rather than failing, so list first if
        you are not sure the name exists. Reads a stored file and changes nothing.
        """
        return await anyio.to_thread.run_sync(_get_control_result, name, settings_file)

    return server


def _list_controls_result(settings_file, supervisor):
    """Assemble the list off the event loop.

    Args:
        settings_file (str or None): the settings path the process was started with
        supervisor (Supervisor or None): the running supervisor, where there is one

    Returns:
        dict: the tool's result
    """
    supervision = _supervision_by_name(supervisor)
    names = stored_control_names(settings_file)
    return {
        "controls": [_describe(name, settings_file, supervision) for name in names],
        "supervisor_running": supervision is not None,
    }


def _get_control_result(name, settings_file):
    """Read one document off the event loop.

    Args:
        name (str): the control's name
        settings_file (str or None): the settings path the process was started with

    Returns:
        dict: the tool's result

    Raises:
        ConfigError: the name is unusable, the control does not exist, or its document
            cannot be parsed
    """
    return {"name": name, "document": load_control(name, settings_file)}
