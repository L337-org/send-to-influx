"""The control loops over MCP: what this installation controls, whether it is, and editing it.

Two tiers behind two switches. ``controls.enabled`` registers the read tools -
``list_controls`` for what is stored and what is running, ``get_control`` for one document as
held on disk, and ``get_control_schema`` for the format itself. ``controls.mcp_write`` then
adds ``save_control``, ``set_control_enabled`` and ``delete_control``. Neither switch implies
the other: running the loops an operator wrote is not the same act as handing a model
authorship of them, and an authored control actuates devices unattended for as long as it
exists.

Both tiers are registered only where their switch is on, because a capability that is
switched off should be absent from the advertised surface rather than present and refusing -
a tool a model can see is a tool it will try, and a refusal costs a round trip to learn what
the tool list could have said for free.

**Absence cannot explain itself, so something else has to.** With the write tools
unregistered, a model has no way to tell "this installation has not enabled writing" from
"this build cannot write controls", and the two call for completely different answers to the
user. ``get_control_schema`` therefore reports the write state and names the setting - see
:func:`_writing_availability`. It is the tool a model must call before composing a control
anyway, so the fact arrives exactly when it is needed and costs nothing when it is not.

**The schema is here rather than behind the write flag**, even though its reason for
existing is to serve writing. An installation where nothing may write controls can still be
asked for a document to paste in by hand, or to explain one already written - and the format
is not a secret in any case.

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

from toinflux.controls import (
    CONTROL_EXAMPLE,
    CONTROL_KEY_HELP,
    CONTROL_RULE_SLOTS,
    BUILT_IN_SAFE_STATES,
    REQUIRED_CONTROL_KEYS,
    control_dir,
    control_writes_enabled,
    controls_enabled,
    load_control,
    validate_control,
)

# Aliased on import: each of these shares a name with the tool that wraps it, and the tool
# has to keep that name because it is the advertised surface.
from toinflux.controls import delete_control as remove_stored_control
from toinflux.controls import list_controls as stored_control_names
from toinflux.controls import save_control as store_control
from toinflux.exceptions import ConfigError, ToolParamError
from toinflux.mcp_common import configured_sources, register_tool


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
    # Deliberately not "(read-only)": the write tools may be registered a few lines below,
    # and an operator reading the journal should not be told read-only and then told
    # otherwise. What is read-only is this half, and the next line says what the other is.
    logging.info("MCP control read tools enabled")

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

    @register_tool(
        server,
        title="Get Control Loop Format",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def get_control_schema() -> dict:  # noqa: DOC201
        """Return the format of a control loop document: every permitted key with what it
        means, which keys are required, every slot that holds a rule, the language a slot may
        use, the safe states, which of this installation's sources can be read as inputs and
        which can switch devices, and a complete worked example that is known to be valid.

        Read this before composing a control. The rule language is small and deliberate -
        arithmetic over numbers, a fixed table of functions, no strings and no attribute
        access - and a rule naming an input the document does not declare is refused rather
        than ignored, so guessing the format costs a round trip per mistake.

        The example is the one the project tests itself against, so it is valid by
        construction rather than by having been checked once. Use `list_controls` for what
        this installation already has and `get_control` for one of those documents.

        Reads settings and constants, contacts no device, and changes nothing. It cannot
        fail on a control, because it describes the format rather than any stored document.
        """
        return await anyio.to_thread.run_sync(_control_schema_result, settings, settings_file, supervisor)

    if not control_writes_enabled(settings):
        # Absent rather than refusing, as above - and get_control_schema says so, because
        # absence alone cannot tell a model which of the two reasons it is looking at.
        return server
    logging.warning(
        "MCP control writing enabled: a connected client may create, change and delete "
        "control loops, which actuate devices unattended (controls.mcp_write)"
    )

    @register_tool(
        server,
        title="Save Control Loop",
        # Not idempotent, though the file would end up identical: a second save requests
        # another reload, and a reload stops a running control, makes its devices safe and
        # starts it again. Repeating the call moves heaters. `set_control_enabled` below is
        # idempotent for the opposite reason - it returns early when nothing would change.
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
        ),
    )
    async def save_control(name: str, document: dict) -> dict:  # noqa: DOC101,DOC103,DOC108,DOC201
        """Store a control loop document under `name`, replacing any control of that name,
        and put it into effect at once.

        Call `get_control_schema` first: the document must be valid in full, and it is
        checked before anything is written. An invalid one is refused and changes nothing -
        the control that was there keeps running the document it already had - and every
        problem comes back together rather than one per attempt.

        An enabled control actuates devices from its next cycle, without a restart. To
        write one without running it yet, save it with `enabled: false` and turn it on
        with `set_control_enabled` once you are happy with it.

        Replacing a running control stops it, makes its devices safe, and starts the new
        document - so an edit is a restart rather than a change applied mid-cycle.

        To change one thing about a control that exists, read it with `get_control`, alter
        that key and send the whole document back. Composing a replacement from memory is
        what silently rewrites a stage ladder. Replacing an existing control returns which
        sections changed and which of those change what the devices do, so read that back:
        it is how you catch having done it anyway.
        """
        return await anyio.to_thread.run_sync(_save_control_result, name, document, settings_file, supervisor)

    @register_tool(
        server,
        title="Enable Or Disable Control Loop",
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
        ),
    )
    async def set_control_enabled(name: str, enabled: bool) -> dict:  # noqa: DOC101,DOC103,DOC108,DOC201
        """Turn one stored control loop on or off, leaving the rest of its document alone.

        Enabling starts the control on its next cycle; disabling stops it and makes its
        devices safe rather than leaving them wherever the last cycle put them. Both take
        effect without restarting the service. An unknown control is an error.

        The separate tool exists because this is the common edit and the safe one: unlike
        `save_control` it cannot change what a control does, only whether it does it. Use it to park a loop
        you want to keep, or to commission one saved with `enabled: false`.

        Already in the requested state is success, not an error, and says so. Use
        `list_controls` for the names and which are on.
        """
        return await anyio.to_thread.run_sync(_set_enabled_result, name, enabled, settings_file, supervisor)

    @register_tool(
        server,
        title="Delete Control Loop",
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
        ),
    )
    async def delete_control(name: str) -> dict:  # noqa: DOC101,DOC103,DOC108,DOC201
        """Delete one stored control loop by name, permanently, and stop it if it is running.

        The document is removed from disk and cannot be recovered from here. Its process is
        stopped and it makes its devices safe rather than leaving them wherever the last
        cycle put them.

        `name` is required and there is no default: this deletes exactly the control named
        and never "the current one". Deleting a control that does not exist is an error
        rather than a silent success, so a mistyped name is reported rather than appearing
        to have worked.

        To stop a control without losing it, use `set_control_enabled` instead. Read it with
        `get_control` first if you may want to recreate it.
        """
        return await anyio.to_thread.run_sync(_delete_control_result, name, settings_file, supervisor)

    return server


def _usable_sources(settings):
    """Return which configured sources a control may read from, and which it may switch.

    Read from the handler classes without constructing one: building a handler loads and
    validates settings and opens a session, and this answers a question about the class.

    **What this installation collects, not what this build knows how to collect.** An input
    is read from stored data, so a source nothing collects has nothing to read - offering it
    would be describing a different installation. ``configured_sources`` is the same list the
    collectors run and the other MCP tools expose, reused rather than reimplemented: it
    lowercases, drops a non-string entry, and answers "nothing" for an absent ``sources:``,
    and a second reading of that setting here would eventually disagree with it.

    A configured source this build does not know is skipped rather than reported - it cannot
    be used either way, and ``--check-config`` is where an operator is told about it.

    Args:
        settings (dict): the parsed settings document

    Returns:
        dict: the source names readable as inputs, and those able to actuate a device
    """
    from toinflux.exceptions import ConfigError as _ConfigError
    from toinflux.general import source_class

    readable, actuating = [], []
    for name in sorted(configured_sources(settings)):
        try:
            handler = source_class(name)
        except _ConfigError:
            continue
        readable.append(name)
        if getattr(handler, "MCP_ACTUATES_DEVICES", False):
            actuating.append(name)
    return {"readable_as_inputs": readable, "can_switch_devices": actuating}


def _control_schema_result(settings, settings_file=None, supervisor=None):
    """Assemble the control document format off the event loop.

    Every part is read from the constant that governs it rather than written out again
    here. A description of a format that is maintained separately from the format is one
    that is wrong the first time somebody changes the format and does not think to look -
    and this one is handed to a client that will then write a document from it.

    Args:
        settings (dict): the parsed settings document
        settings_file (str or None): the settings path the process was started with
        supervisor (Supervisor or None): the running supervisor, where there is one

    Returns:
        dict: the tool's result
    """
    from toinflux.rules import FUNCTION_ARITY, KEYWORDS, MAX_NESTING_DEPTH, MAX_RULE_LENGTH, OPERATORS

    return {
        "document": {
            "required_keys": list(REQUIRED_CONTROL_KEYS),
            "keys": dict(sorted(CONTROL_KEY_HELP.items())),
        },
        "rules": {
            "slots": [{"where": where, "required": not optional} for _path, where, optional in CONTROL_RULE_SLOTS],
            "names": (
                "a rule may read the keys of `inputs` and `parameters` and nothing else; "
                "an undeclared name is refused when the document is validated"
            ),
            "functions": {name: _arity(low, high) for name, (low, high) in sorted(FUNCTION_ARITY.items())},
            "operators": list(OPERATORS),
            "keywords": sorted(KEYWORDS),
            "max_length": MAX_RULE_LENGTH,
            "max_nesting_depth": MAX_NESTING_DEPTH,
            "notes": [
                "everything is a number: a comparison is 1 or 0, and a gate acts while its rule is non-zero",
                "`and`, `or` and `if` short-circuit, so a rule can guard its own arithmetic",
                "comparisons do not chain: write `a < b and b < c` rather than `a < b < c`",
                "precedence is Python's: or, and, not, comparison, + -, * /, unary minus",
                "a number may not run straight into a name, so write `1 and 2` rather than `1and 2`",
            ],
        },
        "safe_states": list(BUILT_IN_SAFE_STATES),
        "sources": _usable_sources(settings),
        "example": CONTROL_EXAMPLE,
        "writing": _writing_availability(settings, settings_file, supervisor),
    }


def _writing_availability(settings, settings_file, supervisor=None):
    """Describe whether this installation lets a model change controls, and what to do if not.

    **This is the whole nudge, and it is here because it cannot be anywhere better.** A
    disabled tool is not registered, so there is no ``save_control`` to answer the question
    by refusing - the model simply does not see one and has no way to tell "this install
    will not let me" from "this build cannot". Both produce the same empty tool list and the
    likely guess is the wrong one. So the fact is published by the tool a model must call
    before composing a control anyway, which is registered whenever controls are on.

    Off is a working state rather than an error, and the wording says so: the model can
    still compose a document and hand it over, which is a use the operator may well prefer.
    What it must not do is present that as the only possibility, leaving somebody to wonder
    why the assistant keeps producing YAML instead of saving it.

    Naming the directory matters as much as naming the setting. "Ask your operator to enable
    writes" is a dead end if neither party knows where the file would go.

    **Whether a new document is picked up depends on the supervisor existing**, which is why
    one is taken. It does not exist when nothing was supervisable at startup, and the
    commonest way to be in that state is to have no controls stored at all - which is
    precisely the person being told here how to write their first one by hand. Promising
    them a pickup that cannot happen is the worst place to be wrong, so the advice is
    conditional on what is actually running. :func:`_reload_outcome` makes the same
    distinction for a write that goes through the tools.

    Args:
        settings (dict): the parsed settings document
        settings_file (str or None): the settings path the process was started with
        supervisor (Supervisor or None): the running supervisor, where there is one

    Returns:
        dict: what the client may do about controls, and how to change it
    """
    # One sentence, two states, used by both branches below: a control only starts by itself
    # where something is watching for it.
    if supervisor is None:
        pickup = (
            "this service is not currently supervising any control, so a newly stored one "
            "will not start until the service is restarted - which is the normal state when "
            "no control is stored yet"
        )
    else:
        pickup = "the service picks up a stored control without a restart"
    if control_writes_enabled(settings):
        return {
            "available": True,
            "tools": ["save_control", "set_control_enabled", "delete_control"],
            "takes_effect": (
                "a saved or deleted control is reconciled with the supervisor rather than "
                "waiting for a restart, so an enabled control starts actuating on its next "
                f"cycle - but note that {pickup}"
            ),
        }
    return {
        "available": False,
        "why": (
            "this installation has not opted in to letting an MCP client create, change or "
            "delete controls, so the tools that would do it are not registered"
        ),
        "setting": "controls.mcp_write",
        "set_it_to": True,
        "in_file": settings_file or "the settings file this service was started with",
        "note": (
            "controls.enabled switches the subsystem on; controls.mcp_write is a separate "
            "opt-in and neither implies the other"
        ),
        "meanwhile": (
            "compose the document and give it to the operator to save as "
            f"{control_dir(settings_file)}/<name>.yaml - it is the same format described "
            f"here. Note that {pickup}"
        ),
    }


def _reload_outcome(supervisor, name):
    """Ask the supervisor to reconcile one control, and say whether anyone was listening.

    Every write goes through here rather than calling ``request_reload`` directly, so that
    "the file changed" and "something will act on it" stay separate answers. They come
    apart in a real installation: the MCP server runs with no supervisor when the subsystem
    is on but no control could be started, and a client told its save took effect would then
    wait for a heater that nothing is going to start.

    Args:
        supervisor (Supervisor or None): the running supervisor, where there is one
        name (str): the control whose document changed

    Returns:
        dict: what happens next, in terms the caller can act on
    """
    if supervisor is None:
        return {
            "in_effect": False,
            "detail": (
                "the document is stored, but nothing is supervising controls in this "
                "process, so it will not start until the service is restarted"
            ),
        }
    supervisor.request_reload(name)
    return {
        "in_effect": True,
        "detail": "the supervisor has been asked to reconcile it; the change applies on its next pass",
    }


def _validated_document(name, document):
    """Return a document fit to store, or raise with everything wrong with it.

    **Validation is here and not in** :func:`toinflux.controls.save_control`, which writes
    whatever it is given. That split is deliberate: the supervisor's own reload path writes
    no documents, and a control process reads one that was already checked, so the store
    stays a store. This is the door an unchecked document actually arrives at.

    Every problem is returned at once. A model fixing one fault per round trip against a
    document with four of them is four exchanges the operator pays for, and the errors are
    independent, so there is nothing to be gained by stopping at the first.

    Args:
        name (str): the control's name, which some checks depend on
        document (dict): the document as the client sent it

    Returns:
        dict: the same document, once it is known to be valid

    Raises:
        ToolParamError: the document is not a mapping, or is not a valid control
    """
    if not isinstance(document, dict):
        raise ToolParamError(
            f"document must be a control document (a mapping of keys), got {type(document).__name__} - "
            "call get_control_schema for the format"
        )
    errors = validate_control(name, document)
    if errors:
        raise ToolParamError(
            f"control {name!r} is not valid and nothing has been written; "
            f"call get_control_schema for the format. Problems: " + "; ".join(errors)
        )
    return document


def _save_control_result(name, document, settings_file, supervisor):
    """Validate, store and apply one control document.

    Nothing is written until the document is known to be good, so a rejected save leaves
    whatever was there running untouched - which matters because the thing it was running
    may be holding a room at temperature.

    Args:
        name (str): the control's name
        document (dict): the document to store
        settings_file (str or None): the settings path the process was started with
        supervisor (Supervisor or None): the running supervisor, where there is one

    Returns:
        dict: the tool's result

    Raises:
        ToolParamError: the document is not valid
    """
    document = _validated_document(name, document)
    replaced = name in set(stored_control_names(settings_file))
    changes = _changes_against_stored(name, document, settings_file) if replaced else None
    store_control(name, document, settings_file)
    logging.info("Control %r was %s over MCP", name, "replaced" if replaced else "created")
    if changes and changes["actuation"]:
        # At WARNING because this is the line an operator wants to find after a heater did
        # something they did not ask for. Naming the sections rather than diffing them: the
        # document is on disk either way, and a rendered diff in the journal is unreadable.
        logging.warning(
            "Control %r was rewritten over MCP and this changed what its devices do: %s",
            name,
            ", ".join(changes["actuation"]),
        )
    result = {
        "saved": name,
        "replaced_existing": replaced,
        "enabled": document.get("enabled") is True,
        "reload": _reload_outcome(supervisor, name),
    }
    if changes is not None:
        result["changed"] = changes
    return result


# The sections that decide what a device physically does. A change to one of these is the
# difference between editing a control and rebuilding it, which is the thing worth saying
# out loud when a whole document has been replaced to alter one number.
ACTUATING_SECTIONS = ("output", "devices", "safe_state", "active_period", "enable_when", "enabled")


def _changes_against_stored(name, document, settings_file):
    """Say which sections this save alters, against the document already stored.

    **Why a whole-document tool reports this.** ``save_control`` replaces the document, so a
    client that re-composes one from memory rather than reading it, changing a key and
    writing it back can produce something that validates and is not what was there - a stage
    ladder with different rungs is legal, so nothing else would catch it. Naming what moved
    turns that from silent into visible, in the result the client sees and in the journal the
    operator reads, without a second narrow tool to keep in step.

    An unreadable stored document is reported as every section changing rather than as no
    change, because "it was broken and now it is not" is a change and the opposite reading
    would be reassuring and wrong.

    Args:
        name (str): the control's name
        document (dict): the document about to be written
        settings_file (str or None): the settings path the process was started with

    Returns:
        dict: ``sections`` (every top-level key that differs) and ``actuation`` (those of
        them that change what the devices do), both sorted
    """
    try:
        previous = load_control(name, settings_file)
    except ConfigError:
        previous = None
    if not isinstance(previous, dict):
        sections = sorted(document)
    else:
        sections = sorted(key for key in set(previous) | set(document) if previous.get(key) != document.get(key))
    return {
        "sections": sections,
        "actuation": [key for key in sections if key in ACTUATING_SECTIONS],
    }


def _set_enabled_result(name, enabled, settings_file, supervisor):
    """Turn one stored control on or off without touching the rest of its document.

    Read-modify-write rather than a targeted edit, because the store holds whole documents
    and a control's file is small. The document is re-validated on the way out: a file that
    was hand-edited into an invalid state between being written and being switched on would
    otherwise be started by this, and fail on its first cycle instead of here.

    Args:
        name (str): the control's name
        enabled (bool): the state to put it in
        settings_file (str or None): the settings path the process was started with
        supervisor (Supervisor or None): the running supervisor, where there is one

    Returns:
        dict: the tool's result

    Raises:
        ToolParamError: enabled is not a boolean, or the stored document is not valid
    """
    if not isinstance(enabled, bool):
        raise ToolParamError(f"enabled must be true or false (got {enabled!r})")
    document = load_control(name, settings_file)
    was = document.get("enabled") is True
    if was == enabled:
        return {
            "control": name,
            "enabled": enabled,
            "changed": False,
            "detail": f"control {name!r} was already {'enabled' if enabled else 'disabled'}; nothing was written",
        }
    document["enabled"] = enabled
    _validated_document(name, document)
    store_control(name, document, settings_file)
    logging.info("Control %r was %s over MCP", name, "enabled" if enabled else "disabled")
    return {
        "control": name,
        "enabled": enabled,
        "changed": True,
        "reload": _reload_outcome(supervisor, name),
    }


def _delete_control_result(name, settings_file, supervisor):
    """Remove one control document and stop what it was running.

    The reload request is made *after* the file is gone, because that is what tells the
    supervisor this is a deletion: it decides from the document on disk, and a request
    raised while the file still existed would be read as a restart.

    Args:
        name (str): the control's name
        settings_file (str or None): the settings path the process was started with
        supervisor (Supervisor or None): the running supervisor, where there is one

    Returns:
        dict: the tool's result
    """
    remove_stored_control(name, settings_file)
    logging.info("Control %r was deleted over MCP", name)
    return {
        "deleted": name,
        "reload": _reload_outcome(supervisor, name),
    }


def _arity(low, high):
    """Describe how many arguments one rule function takes.

    Args:
        low (int): the fewest it accepts
        high (int or None): the most, or None where it is unbounded

    Returns:
        str: a phrase naming the count
    """
    if high is None:
        return f"{low} or more arguments"
    if high == low:
        return f"{low} argument" if low == 1 else f"{low} arguments"
    return f"{low} to {high} arguments"


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
