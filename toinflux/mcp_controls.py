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
import time
import threading


from toinflux.controls import (
    CONTROL_EXAMPLES,
    TUNING_NOTES,
    actuators_owned,
    enabled_owner_of,
    CONTROL_KEY_HELP,
    CONTROL_RULE_SLOTS,
    BUILT_IN_SAFE_STATES,
    REQUIRED_CONTROL_KEYS,
    control_dir,
    device_identity,
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
from toinflux.general import load_settings, render_external
from toinflux.mcp_common import configured_sources, register_tool

# One writer at a time across the control-write tools. Each of them is a read-modify-write -
# `set_control_enabled` most obviously, but `save_control` also reads the stored document to
# report what changed - and the MCP SDK runs them on anyio worker threads, so two calls can
# interleave. Without this, a `set_control_enabled` that loaded before a `save_control` stored
# would write its stale copy afterwards and silently undo the edit, breaking the narrow tool's
# one promise: that it changes `enabled` and nothing else.
#
# A plain lock rather than per-control, because the set is tiny, the operations are
# sub-millisecond file writes, and a lock per name is a map that has to be reaped. It does not
# reach a hand-edit made outside the process; `save_control`'s atomic rename is what keeps a
# reader from seeing a half-written file there.
_WRITE_LOCK = threading.Lock()


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


def _describe(name, settings_file, supervision, settings=None):
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
        settings (dict or None): the parsed settings, read once by the caller for the whole
            listing rather than once per control

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
    errors = validate_control(name, document, settings)
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
        title="Get Control Loop State",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def get_control_state(name: str) -> dict:  # noqa: DOC101,DOC103,DOC108,DOC201
        """Report what a running control has worked out: its PID's integral, and what each
        device was last commanded to and when.

        `get_control` gives the document - what it was told to do. This gives what it has
        since learned, which device output alone cannot show. Read it before concluding a
        demand is misbehaving: an output falling while the input is still far from target is
        usually the integral, which is invisible from outside.

        `matches_document` is false once the control has been edited, when that memory stops
        meaning anything. `held_by_minimum` names devices still inside their
        `min_transition_seconds`, so they are being commanded what they already have.

        Fails where there is no such control, naming it. Reads stored files and changes
        nothing. See `get_control` for the document and `list_controls` for what is running.
        """
        return await anyio.to_thread.run_sync(_control_state_result, name, settings_file, supervisor)

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
        you are not sure the name exists. This is what the control was told to do; for what it
        has since worked out - its integral, and what each device was last set to - use
        `get_control_state`. Reads a stored file and changes nothing.
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
    # INFO, not WARNING. This is the configuration the operator asked for, and a warning
    # about a deliberate setting is noise that teaches people to skim warnings. The
    # neighbouring grant says the same thing the same way: `mcp_write.py` logs "MCP
    # device-write tools enabled for: ..." at INFO, and that one lets a model switch a real
    # heater on this second. WARNING is kept for a configuration that will not do what was
    # asked - `mcp_write.py` uses it for a source that is write-enabled with no tools wired.
    logging.info(
        "MCP control-write tools enabled: a client may create, change and delete control " "loops (controls.mcp_write)"
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
        what silently rewrites a stage ladder. Replacing one returns a `changed` object:
        `sections` is everything that differs, `device_plan` the subset deciding which
        devices are commanded and when. An unexpected entry there means you changed more
        than you meant to.
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


def _control_state_result(name, settings_file=None, supervisor=None):
    """Assemble what a running control currently knows, off the event loop.

    **Read from the file the control writes rather than asked of the control itself.** The
    PID lives in a child process and the MCP server does not, so the alternative would be
    inventing a channel between them - and the child already writes this down every cycle so
    that a restart does not lose it. The state is therefore at most one cycle old, which is
    the same freshness anything else here reports.

    Args:
        name (str): the control to describe
        settings_file (str or None): the settings path the process was started with
        supervisor (Supervisor or None): the running supervisor, where there is one

    Returns:
        dict: the tool's result

    Raises:
        ToolParamError: there is no such control
    """
    from toinflux.controller import Controller
    from toinflux.transitions import TransitionLog

    try:
        document = load_control(name, settings_file)
    except ConfigError as exc:
        raise ToolParamError(f"control {name!r} cannot be read: {render_external(exc)}") from exc
    log = TransitionLog(name, settings_file)
    now = time.time()
    entry: dict = {"control": name, **_supervision_of(name, _supervision_by_name(supervisor))}

    fingerprint = None
    if not validate_control(name, document):
        fingerprint = Controller(document).fingerprint
    stored = log.loop
    if stored.get("integral") is not None:
        entry["loop"] = {
            "integral": stored["integral"],
            "recorded_at": stored.get("at"),
            "age_seconds": _age(stored.get("at"), now),
            # The question a reader actually has: would a restart keep this, or start over?
            "matches_document": fingerprint is not None and stored.get("fingerprint") == fingerprint,
        }
    else:
        entry["loop"] = None

    minimum_for = Controller(document).min_transition_for if fingerprint is not None else None
    # Coerced, not trusted: this tool reads a document that may have been hand-edited, and
    # it validates only to decide whether a fingerprint is meaningful. A `devices:` holding a
    # list would otherwise reach `parameter_devices` and come back as an AttributeError,
    # which is an internal error where the tool documents a ToolParamError.
    declared = document.get("devices")
    declared = declared if isinstance(declared, dict) else {}
    devices = tuple(declared)
    recorded = log.identities()
    entry["devices"] = {
        device: {
            "state": record.get("state"),
            # **The scale the state is on, because the number means nothing without it.** A
            # driven device's 40 is forty percent or forty kelvin depending on this, and a
            # client reading the state has no other way to tell.
            #
            # The scale it was *recorded* against, not the document's current one: after an
            # edit those differ, and it is the recorded value a client is trying to read.
            # Taken from the identity, which is the declaration as it stood at the time.
            "parameter": dict(record.get("for") or ()).get("parameter"),
            "changed_at": record.get("at"),
            "age_seconds": _age(record.get("at"), now),
            # A safe state overrides min_transition_seconds in both directions, so a device
            # marked this way is free to move whatever its clock says.
            "forced": bool(record.get("forced")),
        }
        for device, record in sorted(log.entries.items())
    }
    # **The same test the loop applies, not just the clock.** `_hold` refuses to pin a device
    # whose recorded parameter is not the one the document now drives it by, because the value
    # is on a scale that no longer means anything - so after such an edit the next cycle will
    # move that device whatever its timer says. Reporting it as held would describe a restraint
    # that is not going to happen.
    held = log.frozen(minimum_for, devices, now=now) if minimum_for is not None else set()
    # The same single comparison `_hold` makes, so this cannot report a restraint the next
    # cycle will not honour. It used to be two checks kept in step by hand, which is how they
    # came to disagree.
    entry["held_by_minimum"] = sorted(
        device for device in held if recorded.get(device) == device_identity(declared.get(device))
    )
    return entry


def _age(at, now):
    """Return how long ago something was recorded, or None where it was not.

    Args:
        at (float or None): epoch seconds it happened
        now (float): epoch seconds now

    Returns:
        float or None: seconds, never negative, or None where there is no moment
    """
    if not isinstance(at, (int, float)):
        return None
    return round(max(0.0, now - float(at)), 1)


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
        # Beside the examples, because the examples are what get copied: the numbers in them
        # are the one part of a control document that cannot be right in the abstract, and
        # every word explaining that used to live in comments this payload does not carry.
        "tuning": TUNING_NOTES,
        "sources": _usable_sources(settings),
        "examples": CONTROL_EXAMPLES,
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
    # **Two different answers, because the two paths are not the same.** A control written by
    # a tool is announced to the supervisor, so it starts without a restart. A control written
    # by hand is not: nothing watches the control directory, and a reload is queued only by
    # the write tools. Saying "the service picks it up" to somebody being told to save a file
    # themselves is the one place that wording does real harm, since they are the only reader
    # for whom it is false.
    if supervisor is None:
        pickup = (
            "this service is not currently supervising any control, so a newly stored one "
            "will not start until the service is restarted - which is the normal state when "
            "no control is stored yet"
        )
    else:
        pickup = "the service picks up a control written through these tools without a restart"
    by_hand = (
        "the service does not watch this directory, so restart it after saving, or have "
        "somebody enable controls.mcp_write and ask again"
    )
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
            f"here. Note that {by_hand}"
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


def _validated_document(name, document, settings_file=None):
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
        settings_file (str or None): the settings path the process was started with, so a
            source can be checked against this installation and not only against the build

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
    errors = validate_control(name, document, load_settings(settings_file))
    if errors:
        raise ToolParamError(
            f"control {name!r} is not valid and nothing has been written; "
            f"call get_control_schema for the format. Problems: " + "; ".join(errors)
        )
    return document


def _enabled_owner_elsewhere(name, document, settings_file):
    """Return the enabled control already commanding one of this document's actuators.

    **One enabled control per actuator.** Two loops sharing a heater each run their own PID
    against their own setpoint and each apply their own safe state, so whichever commanded
    last wins until the other's next cycle - and neither can detect it. Nothing inside a
    single document can see the clash, which is why this reads the others.

    A document that will not parse is skipped rather than guessed at: what it owns is exactly
    what cannot be determined, and refusing a new control over a broken old one would leave
    somebody unable to proceed without first fixing a file they may not have written.

    Args:
        name (str): the control being written, excluded from its own check
        document (dict): the document being written
        settings_file (str or None): the settings path the process was started with

    Returns:
        tuple or None: ``(owner name, actuator)``, or None where nothing else claims them
    """
    stored = {}
    for other in stored_control_names(settings_file):
        if other == name:
            continue
        try:
            stored[other] = load_control(other, settings_file)
        except ConfigError:
            continue
    return enabled_owner_of(actuators_owned(document), stored, excluding=name)


def _describe_actuator(identity):
    """Render an actuator identity for a message.

    Args:
        identity (tuple): ``(source, instance, device)``

    Returns:
        str: a phrase naming it
    """
    source, instance, device = identity
    return f"{device!r} on {source!r}" + (f" ({instance!r})" if instance else "")


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
    document = _validated_document(name, document, settings_file)
    with _WRITE_LOCK:
        return _save_validated(name, document, settings_file, supervisor)


def _save_validated(name, document, settings_file, supervisor):
    """Store a document already known to be valid, under the write lock.

    Args:
        name (str): the control's name
        document (dict): the document to store, already validated
        settings_file (str or None): the settings path the process was started with
        supervisor (Supervisor or None): the running supervisor, where there is one

    Returns:
        dict: the tool's result
    """
    # **Stored disabled rather than refused, where an enabled control already owns one of
    # these actuators.** Refusing throws away a document somebody has just composed and makes
    # them ask again with one key changed; storing it enabled puts two loops on one heater,
    # each running its own PID and each applying its own safe state, with neither able to
    # detect the other. Disabling keeps the work, keeps the invariant, and leaves one obvious
    # next step - which the result states plainly, because returning a document different
    # from the one it was given is only acceptable if it says so.
    withheld = None
    if document.get("enabled", True) is True:
        clash = _enabled_owner_elsewhere(name, document, settings_file)
        if clash is not None:
            owner, actuator = clash
            document = dict(document, enabled=False)
            withheld = {
                "enabled": False,
                "because": (
                    f"control {owner!r} is enabled and already commands "
                    f"{_describe_actuator(actuator)}; two enabled controls must not share one"
                ),
                "to_enable_this_one": f"disable {owner!r} with set_control_enabled, then enable {name!r}",
            }
            logging.warning(
                "Control %r was stored disabled: %r is enabled and already commands %s",
                name,
                owner,
                _describe_actuator(actuator),
            )
    replaced = name in set(stored_control_names(settings_file))
    changes = _changes_against_stored(name, document, settings_file) if replaced else None
    store_control(name, document, settings_file)
    logging.info("Control %r was %s over MCP", name, "replaced" if replaced else "created")
    if changes and changes["device_plan"]:
        # At WARNING because this is the line an operator wants to find after a heater did
        # something they did not ask for. Naming the sections rather than diffing them: the
        # document is on disk either way, and a rendered diff in the journal is unreadable.
        logging.warning(
            "Control %r was rewritten over MCP, changing which devices it commands and when: %s",
            name,
            ", ".join(changes["device_plan"]),
        )
    result = {
        "saved": name,
        "replaced_existing": replaced,
        # `get(..., True)`, matching `Gate` and `_describe`: an omitted key means enabled.
        # `is True` reported a document with no `enabled` key as disabled while the
        # supervisor started actuating it, which is the one direction this must not be
        # wrong in - a caller told a control is off does not go and turn it off.
        "enabled": document.get("enabled", True) is True,
        "reload": _reload_outcome(supervisor, name),
    }
    if changes is not None:
        result["changed"] = changes
    if withheld is not None:
        result["stored_disabled"] = withheld
    return result


# The sections that decide **which devices are commanded and when** - the ladder, the device
# list, the gating, the safe state. Deliberately not every section that changes behaviour:
# `parameters` and `pid` change what the loop *aims at*, which is what an operator adjusts on
# purpose, and flagging those would warn on every ordinary edit until nobody read the warning.
# The split is "did you change the machinery" against "did you change the target", because the
# first is the one somebody does by accident while meaning to do the second.
DEVICE_PLAN_SECTIONS = ("output", "devices", "safe_state", "active_period", "enable_when", "enabled")


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
        dict: ``sections`` (every top-level key that differs) and ``device_plan`` (those of
        them in :data:`DEVICE_PLAN_SECTIONS`, which decide which devices are commanded and
        when), both sorted. ``device_plan`` is a subset and not a "behaviour changed" flag:
        a ``parameters`` or ``pid`` edit changes what the loop aims at, and appears in
        ``sections`` only. ``sections`` is the complete answer.
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
        "device_plan": [key for key in sections if key in DEVICE_PLAN_SECTIONS],
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
    with _WRITE_LOCK:
        return _set_enabled_locked(name, enabled, settings_file, supervisor)


def _set_enabled_locked(name, enabled, settings_file, supervisor):
    """Flip one control's enabled flag, under the write lock.

    Separate so the whole read-modify-write is inside the lock rather than only the store:
    the load, the edit and the save have to be one step, or a `save_control` landing between
    them is silently undone by this stale copy.

    Args:
        name (str): the control's name
        enabled (bool): the state to put it in
        settings_file (str or None): the settings path the process was started with
        supervisor (Supervisor or None): the running supervisor, where there is one

    Returns:
        dict: the tool's result

    Raises:
        ToolParamError: the stored document is not valid
    """
    document = load_control(name, settings_file)
    # `get(..., True)` for the same reason as the save result: an omitted key is enabled,
    # so `is True` made enabling an already-enabled control look like a change and rewrite
    # the document for nothing.
    was = document.get("enabled", True) is True
    if was == enabled:
        return {
            "control": name,
            "enabled": enabled,
            "changed": False,
            "detail": f"control {name!r} was already {'enabled' if enabled else 'disabled'}; nothing was written",
        }
    if enabled:
        # Refused rather than stored, because unlike a save there is nothing to preserve:
        # the document already exists and the caller asked for exactly one thing, so the
        # honest answer is that it cannot have it and which control is in the way.
        clash = _enabled_owner_elsewhere(name, document, settings_file)
        if clash is not None:
            owner, actuator = clash
            raise ToolParamError(
                f"control {name!r} was not enabled: {owner!r} is enabled and already commands "
                f"{_describe_actuator(actuator)}, and two enabled controls must not share one. "
                f"Disable {owner!r} first, then enable {name!r}"
            )
    document["enabled"] = enabled
    _validated_document(name, document, settings_file)
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
    with _WRITE_LOCK:
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
    # Read once for the whole listing rather than once per control.
    settings = load_settings(settings_file)
    result = {
        "controls": [_describe(name, settings_file, supervision, settings) for name in names],
        "supervisor_running": supervision is not None,
    }
    if supervision is None:
        # **A bare `false` said nothing about what it meant**, and a client shown only
        # `supervisor_running: false` reported to its operator that the supervisor was not
        # running, which sounds like something crashed. It once meant "no control is stored,
        # so none is needed", which was the common case and not a fault at all; a supervisor
        # now runs whenever controls are enabled, so this branch is the genuinely odd one and
        # says so. Absence cannot explain itself either way.
        result["supervisor_state"] = (
            "nothing is supervising controls in this process, so a control stored now will "
            "not start until the service is restarted. An installation with controls enabled "
            "supervises them whether or not any are stored, so this is unusual: the service "
            "log says why it did not start"
        )
    return result


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
