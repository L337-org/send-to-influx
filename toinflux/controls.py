"""Storage and structural validation for control configurations.

A control is a closed loop that holds something at a target by actuating a device: a
conservatory at a temperature, a room at a brightness. Each one is a YAML document, one
file per control, under the installation's state directory rather than ``/etc``, because
these are created and edited by the running service (and by an MCP client on its behalf)
rather than by an admin with a text editor.

This module owns where those documents live, how they are read and written, and whether
their *shape* is right. It deliberately does not understand rule expressions: the strings
in ``setpoint``, ``enable_when`` and the rest are checked to be strings here and parsed
elsewhere, so that a syntax error in a rule and a missing key are reported by the code
that actually knows about each.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import logging
import math
import os
import re
import stat
import tempfile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import yaml
from toinflux.exceptions import ConfigError
from toinflux.general import resolve_state_dir

# Where control documents live inside the state directory.
CONTROL_DIR_NAME = "controls"

# The cycle window a control uses when its document does not say. Homed here, with the
# document format, because three readers need it - the loop, the process and the
# supervisor's stall threshold - and they disagreed: two defaulted to 900 and the
# controller left it None, so a document that passed --check-config raised ConfigError on
# its first cycle. A default with three spellings is a default with none.
DEFAULT_CYCLE_SECONDS = 900.0

CONTROL_SUFFIX = ".yaml"

# A control's name is also its filename, and an MCP client can choose it. Anything
# outside this set is refused rather than sanitised: silently rewriting a name would
# make the control the caller asked for and the control that exists two different
# things, and quietly mapping two requested names onto one file is worse than an error.
# Lowercase because the store must behave the same on a case-insensitive filesystem.
CONTROL_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,62}$"

# The two settings a control may leave its devices in when it stops actuating.
# `unenergised` commands every device in the control's own device list off directly,
# rather than meaning "the lowest stage" - that way it does not depend on the operator
# having declared a zero stage correctly. `leave_unchanged` is the absence of a safe
# state rather than a safe state, and is opt-in precisely so it is never reached by
# omission.
# What an active period's boundary looks like. Shared with toinflux.schedule, which parses
# the same strings: strptime("%H:%M") accepts "9:00" and "9:0", so a parser left to itself
# would be laxer than the validator and a document could pass one and not the other.
CLOCK_TIME_PATTERN = r"([01]\d|2[0-3]):[0-5]\d"

SAFE_STATE_UNENERGISED = "unenergised"
SAFE_STATE_LEAVE_UNCHANGED = "leave_unchanged"
BUILT_IN_SAFE_STATES = (SAFE_STATE_UNENERGISED, SAFE_STATE_LEAVE_UNCHANGED)

# Keys a control document may carry at the top level. Checked as a closed set: a
# mistyped key that was merely ignored would leave the operator looking at a setting
# they believe is in force and is not.
# What each nested section may contain. Unknown keys are refused rather than ignored, for
# the same reason the top-level set below refuses them: a misspelt key is a setting the
# operator meant to make, so falling back to the default silently gives them a control that
# does something other than what the document in front of them says. `output.cycle_secconds`
# validated cleanly and then ran a 900-second window nobody asked for.
#
# `parameters` is deliberately not here - its keys are the operator's own names - and
# neither is a stage's `set`, whose keys are checked against the device list instead.
INPUT_KEYS = frozenset({"source", "field", "instance", "max_age"})
DEVICE_KEYS = frozenset({"source", "device", "instance", "min_transition_seconds"})
PID_KEYS = frozenset({"input", "setpoint", "kp", "ki", "kd"})
OUTPUT_KEYS = frozenset({"cycle_seconds", "min_transition_seconds", "max_level", "stages"})
STAGE_KEYS = frozenset({"level", "set"})
ACTIVE_PERIOD_KEYS = frozenset({"from", "to", "end_state"})

CONTROL_KEYS = frozenset(
    {
        "name",
        "enabled",
        "timezone",
        "parameters",
        "inputs",
        "pid",
        "output",
        "devices",
        "enable_when",
        "safe_state",
        "active_period",
    }
)

REQUIRED_CONTROL_KEYS = ("inputs", "pid", "output", "devices")

#: One line per permitted key, for a client that has to write a control document rather
#: than read one. Beside CONTROL_KEYS rather than in prose somewhere, and checked against it
#: by tests/test_repo_hygiene.py: a key added to the closed set above and not described here
#: is a key nobody can use, because the only way to learn the format is to be told it.
CONTROL_KEY_HELP = {
    "name": "the control's own name, which must match the file it is stored as",
    "enabled": "true or false; false keeps the document without running the loop",
    "timezone": "an IANA zone name for the active period; absent means this machine's local time",
    "parameters": "constants the rules may read, such as a target temperature, adjustable at runtime",
    "inputs": "name -> {source, field, instance, max_age}: the readings the rules may use",
    "pid": "the loop itself: input and setpoint rules, and the kp, ki and kd gains",
    "output": "cycle_seconds, min_transition_seconds, an optional max_level rule, and the stage ladder",
    "devices": "name -> {source, device, instance, min_transition_seconds}: what the control switches",
    "enable_when": "a rule gating actuation; the control acts only while it evaluates non-zero",
    "safe_state": "what the devices do at startup, on failure and at shutdown",
    "active_period": "{from, to, end_state}: a daily wall-clock window in the control's own timezone",
}

#: Every slot in a control document holding a rule expression: where it lives, the name to
#: call it in a message, and whether it may be absent. One table, because the runtime parses
#: these in two other modules and a validator with its own private list would be a second
#: copy that drifts - a rule slot the runtime honours and the validator has never heard of
#: passes `--check-config` and kills the control at startup, which is the failure this table
#: exists to make impossible. ``tests/test_repo_hygiene.py`` fails the build where the
#: runtime parses a number of slots this table does not describe.
CONTROL_RULE_SLOTS = (
    (("pid", "setpoint"), "pid.setpoint", False),
    (("pid", "input"), "pid.input", False),
    (("output", "max_level"), "output.max_level", True),
    (("enable_when",), "enable_when", True),
)


#: One control document that is known to be valid, because CI validates it. Handed out by
#: the schema tool so a client writing its first control has something that works to start
#: from, and used as the fixture every test builds on, so there is one example rather than
#: a shipped one and a tested one that drift.
#:
#: It is the design note's conservatory example. That example was wrong for as long as it
#: existed - it read `outside` in `enable_when` and declared no such input - and nothing
#: could see it until rules were validated. Being the thing tests are built on is what
#: keeps this one honest.
CONTROL_EXAMPLE = {
    "name": "conservatory",
    "enabled": True,
    "timezone": "Europe/London",
    "parameters": {"target": 18.0},
    "inputs": {
        "inside": {"source": "hue", "field": "temperature_conservatory", "instance": "bridge1", "max_age": 900},
        "dew": {"source": "openmeteo", "field": "dew_point_2m", "max_age": 1800},
        # `outside` and `grid_co2` are declared because `enable_when` and `max_level`
        # read them. They were missing until the rule check existed, so this fixture -
        # and the design-note example it mirrors - described a control that passed
        # every structural check and would have died at startup naming them.
        "outside": {"source": "openmeteo", "field": "temperature_2m", "max_age": 1800},
        "grid_co2": {"source": "carbonintensity", "field": "intensity_actual", "max_age": 3600},
    },
    "pid": {"input": "inside", "setpoint": "max(target, dew + 5)", "kp": 12.0, "ki": 0.02, "kd": 0.0},
    "output": {
        "cycle_seconds": 300,
        "min_transition_seconds": 60,
        "max_level": "if(grid_co2 > 300, 750, 2250)",
        "stages": [
            {"level": 0, "set": {"heater_far": False, "heater_near": False}},
            {"level": 750, "set": {"heater_far": True, "heater_near": False}},
            {"level": 1500, "set": {"heater_far": True, "heater_near": True}},
        ],
    },
    "devices": {
        "heater_far": {"source": "hue", "device": "Conservatory heater far"},
        "heater_near": {"source": "hue", "device": "Conservatory heater near"},
    },
    "enable_when": "outside < 15",
    "safe_state": "unenergised",
    "active_period": {"from": "23:35", "to": "05:25", "end_state": "unenergised"},
}


#: Three complete documents, one per situation, handed out together by the schema tool.
#:
#: **Separate documents rather than one annotated with alternatives.** An example is copied,
#: not read: an agent writing a control took this example's `min_transition_seconds: 300` at
#: the output level and `900` on a device override and wrote 600, which is the average of two
#: numbers and belonged to neither. Anything present to demonstrate a mechanism rather than
#: to be run is a hazard, and two values for one setting within reach at once is the specific
#: shape of it. So each of these is internally coherent and says when it applies, and a
#: per-device override appears only where the situation justifies keeping it.
#:
#: Every one is validated by CI, because an example nothing exercises is a document that
#: stops working the first time the format moves.
CONTROL_EXAMPLES = {
    "normal": {
        "use_when": (
            "the usual case: devices that can be switched as often as the loop likes, and one "
            "transition minimum covering all of them"
        ),
        "document": CONTROL_EXAMPLE,
    },
    "slow_response": {
        "use_when": (
            "one device's effect takes longer to show up at the sensor than another's - a heater "
            "across the room from it, or a larger load - so it should be left alone while the "
            "nearer one trims. Give that device its own longer minimum: it binds only at the rungs "
            "where that device actually changes, so the nearer one still moves at its own rate"
        ),
        "document": {
            "name": "conservatory_staged",
            "enabled": True,
            "timezone": "Europe/London",
            "parameters": {"target": 18.0},
            "inputs": {
                "inside": {"source": "hue", "field": "temperature_conservatory", "max_age": 900},
                "outside": {"source": "openmeteo", "field": "temperature_2m", "max_age": 1800},
            },
            "pid": {"input": "inside", "setpoint": "target", "kp": 12.0, "ki": 0.02, "kd": 0.0},
            "output": {
                "cycle_seconds": 300,
                "min_transition_seconds": 60,
                "stages": [
                    {"level": 0, "set": {"heater_far": False, "heater_near": False}},
                    {"level": 750, "set": {"heater_far": True, "heater_near": False}},
                    {"level": 1500, "set": {"heater_far": True, "heater_near": True}},
                ],
            },
            "devices": {
                # The far heater carries the steady load and is slow to read, so it is held
                # for longer. This governs only the 0 <-> 750 rungs, which are the ones it
                # changes at; heater_near still trims at the 60 above.
                "heater_far": {
                    "source": "hue",
                    "device": "Conservatory heater far",
                    "min_transition_seconds": 120,
                },
                "heater_near": {"source": "hue", "device": "Conservatory heater near"},
            },
            "enable_when": "outside < 15",
            "safe_state": "unenergised",
        },
    },
    "fast_adjustment": {
        "use_when": (
            "a small thermal mass and a device with nothing to protect, where the loop should "
            "recompute often and the window should split finely"
        ),
        "document": {
            "name": "propagator",
            "enabled": True,
            "timezone": "Europe/London",
            "parameters": {"target": 21.0},
            "inputs": {"tray": {"source": "hue", "field": "temperature_propagator", "max_age": 300}},
            "pid": {"input": "tray", "setpoint": "target", "kp": 40.0, "ki": 0.1, "kd": 0.0},
            "output": {
                "cycle_seconds": 60,
                "min_transition_seconds": 10,
                "stages": [
                    {"level": 0, "set": {"mat": False}},
                    {"level": 1000, "set": {"mat": True}},
                ],
            },
            "devices": {"mat": {"source": "hue", "device": "Propagator mat"}},
            "safe_state": "unenergised",
        },
    },
}


def rule_names(document):
    """Return the names a control's rules may reference, in the order the runtime builds them.

    Inputs then parameters, which is the order both the controller and the gate use. The
    order is not arbitrary even though the parser only needs the set: the store refuses a
    name declared as both, and if that were ever relaxed this ordering is what decides
    which one wins - so the three places that build it must agree, which is why they share
    this one.

    Args:
        document (dict): the control document

    Returns:
        tuple: the declared input and parameter names, or empty where neither is a mapping
    """
    names: tuple = ()
    for key in ("inputs", "parameters"):
        section = document.get(key)
        if isinstance(section, dict):
            names += tuple(section)
    return names


def control_dir(settings_file=None):
    """Return the directory control documents are stored in.

    Args:
        settings_file (str or None): the settings path the process was started with,
            used to anchor the location when not running under systemd

    Returns:
        str: the directory holding one YAML document per control
    """
    return os.path.join(resolve_state_dir(settings_file), CONTROL_DIR_NAME)


def control_path(name, settings_file=None):
    """Return the path of one control's document.

    Args:
        name (str): the control's name, already validated
        settings_file (str or None): the settings path the process was started with

    Returns:
        str: the absolute path of that control's YAML document

    Raises:
        ConfigError: the name is not one this store will accept
    """
    require_valid_control_name(name)
    return os.path.join(control_dir(settings_file), f"{name}{CONTROL_SUFFIX}")


def require_valid_control_name(name) -> None:
    """Refuse a control name that cannot safely become a filename.

    The check is here rather than at each call site because a control name reaches this
    module from an MCP client, and a name is concatenated into a path. A traversal
    sequence, an absolute path or a separator would otherwise choose the file that gets
    written, so the pattern is an allow-list rather than a list of things to strip.

    Args:
        name (str): the candidate name

    Raises:
        ConfigError: the name is empty, not a string, or outside the accepted pattern
    """
    # fullmatch, not match: `$` matches before a trailing newline, so "conservatory\n" passed
    # this allow-list - and a control name becomes a filename an MCP client chooses, which is
    # the whole reason the allow-list exists.
    if not isinstance(name, str) or not re.fullmatch(CONTROL_NAME_PATTERN, name):
        raise ConfigError(
            f"invalid control name {name!r}: use 1-63 characters, lower-case letters, "
            f"digits, underscore or hyphen, starting with a letter or digit"
        )


def list_controls(settings_file=None):
    """Return the names of every stored control.

    A file whose name this store would refuse is skipped with a warning rather than
    raising: one unreadable document must not make every other control invisible, and
    the operator still needs telling that something in the directory is being ignored.

    Args:
        settings_file (str or None): the settings path the process was started with

    Returns:
        list: control names, sorted

    Raises:
        ConfigError: the control directory exists but could not be read
    """
    directory = control_dir(settings_file)
    try:
        entries = os.listdir(directory)
    except FileNotFoundError:
        # No controls have ever been created. Not a fault: the subsystem is optional.
        return []
    except OSError as exc:
        raise ConfigError(f"cannot read the control directory {directory!r}: {exc}") from exc

    names = []
    for entry in sorted(entries):
        if not entry.endswith(CONTROL_SUFFIX):
            continue
        name = entry[: -len(CONTROL_SUFFIX)]
        try:
            require_valid_control_name(name)
        except ConfigError as exc:
            logging.warning("Ignoring %r in the control directory: %r", entry, exc)
            continue
        names.append(name)
    return names


def load_control(name, settings_file=None):
    """Read one control's document.

    Args:
        name (str): the control's name
        settings_file (str or None): the settings path the process was started with

    Returns:
        dict: the parsed document

    Raises:
        ConfigError: the control does not exist, cannot be read, is not valid YAML, or
            does not parse to a mapping
    """
    path = control_path(name, settings_file)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"no control named {name!r} at {path!r}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read control {name!r} from {path!r}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"control {name!r} at {path!r} is not valid YAML: {exc}") from exc

    if document is None:
        # An empty file. Distinguished from a malformed one because the cause differs:
        # this is usually a half-finished edit rather than a syntax mistake.
        raise ConfigError(f"control {name!r} at {path!r} is empty")
    if not isinstance(document, dict):
        raise ConfigError(f"control {name!r} at {path!r} must be a mapping, got {type(document).__name__}")
    return document


def save_control(name, document, settings_file=None) -> None:
    """Write one control's document, replacing any existing one atomically.

    Written to a temporary file in the same directory and renamed into place, so a reader
    sees either the old document or the new one and never a half-written file. That
    matters more here than for most configuration: the writer is the running service, and
    a control process may be reading the same document at the moment it changes.

    Args:
        name (str): the control's name
        document (dict): the document to store
        settings_file (str or None): the settings path the process was started with

    Raises:
        ConfigError: the name is unacceptable, or the document could not be written
    """
    path = control_path(name, settings_file)
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, exist_ok=True)
        # 0700: the directory sits under the state directory, which systemd already
        # creates 0700, but a source checkout puts it beside settings.yaml where the
        # default would be whatever the umask allows.
        os.chmod(directory, stat.S_IRWXU)
    except OSError as exc:
        raise ConfigError(f"cannot create the control directory {directory!r}: {exc}") from exc

    handle = None
    temporary = None
    try:
        file_descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=f".{name}.", suffix=CONTROL_SUFFIX)
        handle = os.fdopen(file_descriptor, "w", encoding="utf-8")
        yaml.safe_dump(document, handle, default_flow_style=False, sort_keys=False)
        handle.flush()
        # The rename is only atomic with respect to what actually reached the disk, so
        # the flush and fsync are what make the guarantee hold across a power loss
        # rather than only across a concurrent read.
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, path)
        temporary = None
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot write control {name!r} to {path!r}: {exc}") from exc
    finally:
        if handle is not None:
            handle.close()
        if temporary is not None and os.path.exists(temporary):
            # A failed write must not leave a dot-file behind for every attempt.
            os.unlink(temporary)


def delete_control(name, settings_file=None) -> None:
    """Remove one control's document.

    Args:
        name (str): the control's name
        settings_file (str or None): the settings path the process was started with

    Raises:
        ConfigError: the control does not exist, or could not be removed
    """
    # Imported here because toinflux.transitions imports this module for the name rules, and
    # a module-level import would be circular. Here rather than at the two callers so a third
    # one cannot forget: a log left behind is read by the next control to take the name, which
    # would then hold devices frozen on the strength of what a different control did.
    from toinflux.transitions import forget_control

    path = control_path(name, settings_file)
    try:
        os.unlink(path)
    except FileNotFoundError as exc:
        raise ConfigError(f"no control named {name!r} at {path!r}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot delete control {name!r} at {path!r}: {exc}") from exc
    # After the document is gone: the log is bookkeeping, and failing to remove it must not
    # leave a control that is half-deleted.
    forget_control(name, settings_file)


def _render_names(values):
    """Render a set of document keys for a message, whatever they turn out to be.

    Two problems in one place. YAML keys are not necessarily strings - ``1:`` is a
    perfectly legal mapping key - so sorting them directly raises ``TypeError`` on mixed
    types and joining them raises on anything that is not a string, which would crash
    validation instead of reporting the document that caused it. And a key is external
    input: one containing a newline would forge a second line in the diagnostic, which
    reaches the journal and any connected MCP client.

    Sorting by ``repr`` gives a total order across mixed types, and rendering each as
    ``repr`` escapes a newline rather than obeying it.

    Args:
        values (collections.abc.Iterable): keys taken from a parsed document

    Returns:
        str: a comma-separated, quoted, safely-ordered list
    """
    return ", ".join(repr(value) for value in sorted(values, key=repr))


def _is_number(value):
    """Whether a value is a number rather than something YAML merely allows.

    ``bool`` is excluded on purpose: it is a subclass of ``int`` in Python, so a stage
    written as ``level: true`` would otherwise validate and then sort as 1.

    So are ``.nan`` and ``.inf``, which YAML represents and which are floats to
    ``isinstance``. A nan level compares False against everything, so it neither sorts nor
    brackets: a ladder containing one silently stops being ordered, and a demand can land
    anywhere in it.

    Args:
        value (object): the parsed value

    Returns:
        bool: True for a finite int or float that is not a bool
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _check_no_unknown_keys(where, mapping, allowed, errors) -> None:
    """Refuse keys a section does not define.

    Args:
        where (str): the section's position, for the message
        mapping (dict): the section as parsed
        allowed (frozenset): every key the section defines
        errors (list): appended to with any problems found
    """
    unknown = set(mapping) - allowed
    if unknown:
        errors.append(f"{where}: unknown key(s): {_render_names(unknown)}")


def _check_mapping_of(document, key, errors, required_fields, optional_numbers=(), allowed=frozenset()):
    """Check one mapping-of-mappings section: inputs, devices.

    Args:
        document (dict): the control document
        key (str): the section's key
        errors (list): appended to with any problems found
        required_fields (tuple): field names each entry must carry as a non-empty string
        optional_numbers (tuple): field names that must be positive numbers if present
        allowed (frozenset): every key an entry may carry; anything else is refused

    Returns:
        dict: the section, or an empty mapping when it was missing or the wrong shape
    """
    section = document.get(key)
    if section is None:
        return {}
    if not isinstance(section, dict):
        errors.append(f"{key}: must be a mapping, got {type(section).__name__}")
        return {}
    for entry_name, entry in section.items():
        if not isinstance(entry_name, str):
            # Reported rather than rendered: an operator who wrote `1:` needs telling
            # that the name is the problem, not shown a message about a key called 1.
            errors.append(f"{key}: entry names must be strings, got {entry_name!r}")
            continue
        where = f"{key}[{entry_name!r}]"
        if not isinstance(entry, dict):
            errors.append(f"{where}: must be a mapping, got {type(entry).__name__}")
            continue
        for field in required_fields:
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{where}: {field} is required and must be a non-empty string")
        for field in optional_numbers:
            if field in entry and not (_is_number(entry[field]) and entry[field] > 0):
                errors.append(f"{where}: {field} must be a positive number, got {entry[field]!r}")
        _check_no_unknown_keys(where, entry, allowed, errors)
    return section


def _check_pid(document, errors) -> None:
    """Check the pid section's shape, not its rules.

    Args:
        document (dict): the control document
        errors (list): appended to with any problems found
    """
    pid = document.get("pid")
    if not isinstance(pid, dict):
        if pid is not None:
            errors.append(f"pid: must be a mapping, got {type(pid).__name__}")
        return
    for field in ("input", "setpoint"):
        if not isinstance(pid.get(field), str) or not pid[field].strip():
            errors.append(f"pid.{field}: is required and must be a rule expression")
    for field in ("kp", "ki", "kd"):
        if field in pid and not _is_number(pid[field]):
            errors.append(f"pid.{field}: must be a number, got {pid[field]!r}")
    _check_no_unknown_keys("pid", pid, PID_KEYS, errors)


def _check_stages(document, devices, errors) -> None:
    """Check the output section and its stage ladder.

    Every stage must assign every device the control owns. Omission is how a heater
    silently stays on: a stage that simply does not mention a device says nothing about
    what that device should be doing, and the operator reading the ladder would assume
    it was off.

    Args:
        document (dict): the control document
        devices (dict): the validated devices section, for cross-checking
        errors (list): appended to with any problems found
    """
    output = document.get("output")
    if not isinstance(output, dict):
        if output is not None:
            errors.append(f"output: must be a mapping, got {type(output).__name__}")
        return

    _check_no_unknown_keys("output", output, OUTPUT_KEYS, errors)
    for field in ("cycle_seconds", "min_transition_seconds"):
        if field in output and not (_is_number(output[field]) and output[field] > 0):
            errors.append(f"output.{field}: must be a positive number, got {output[field]!r}")
    if "max_level" in output and not isinstance(output["max_level"], str):
        errors.append("output.max_level: must be a rule expression")

    stages = output.get("stages")
    if not isinstance(stages, list) or not stages:
        errors.append("output.stages: must be a non-empty list")
        return

    device_names = set(devices)
    for index, stage in enumerate(stages):
        _check_one_stage(f"output.stages[{index}]", stage, device_names, errors)


def _check_one_stage(where, stage, device_names, errors) -> None:
    """Check a single rung of the ladder.

    Args:
        where (str): the stage's position, for the message
        stage (object): the parsed stage
        device_names (set): the devices this control owns
        errors (list): appended to with any problems found
    """
    if not isinstance(stage, dict):
        errors.append(f"{where}: must be a mapping, got {type(stage).__name__}")
        return
    _check_no_unknown_keys(where, stage, STAGE_KEYS, errors)
    if not _is_number(stage.get("level")):
        errors.append(f"{where}.level: is required and must be a number")
    assignments = stage.get("set")
    if not isinstance(assignments, dict):
        errors.append(f"{where}.set: is required and must be a mapping of device to state")
        return
    unknown = set(assignments) - device_names
    if unknown:
        errors.append(f"{where}.set: names no such device: {_render_names(unknown)}")
    missing = device_names - set(assignments)
    if missing:
        errors.append(
            f"{where}.set: does not say what to do with {_render_names(missing)} - "
            f"every stage must assign every device"
        )
    # **The state must be a real boolean, and this is the dangerous direction.** The loop
    # commands `on=bool(state)`, and every non-empty string is truthy - so a quoted
    # `"false"` or `"no"`, which is what somebody writes when they are being careful with
    # YAML, energised the device. On the level 0 rung that turned the everything-off rung
    # into an everything-on rung, and it passed --check-config clean. Unquoted `off`/`no`/
    # `false` are YAML booleans and were always right; quoting them silently inverted the
    # meaning. `level` above has been guarded against the same class since it was written.
    wrong = {name for name, state in assignments.items() if name in device_names and not isinstance(state, bool)}
    if wrong:
        errors.append(
            f"{where}.set: must be true or false for {_render_names(wrong)} - "
            f"a quoted 'false' is a string, and every non-empty string switches the device on"
        )


def _check_active_period(document, errors) -> None:
    """Check the active period's shape and its time format.

    Args:
        document (dict): the control document
        errors (list): appended to with any problems found
    """
    period = document.get("active_period")
    if period is None:
        return
    if not isinstance(period, dict):
        errors.append(f"active_period: must be a mapping, got {type(period).__name__}")
        return
    _check_no_unknown_keys("active_period", period, ACTIVE_PERIOD_KEYS, errors)
    for field in ("from", "to"):
        value = period.get(field)
        # fullmatch: `$` matches before a trailing newline, so "23:35\n" passed this and
        # became an active-period boundary carrying a line break.
        if not isinstance(value, str) or not re.fullmatch(CLOCK_TIME_PATTERN, value):
            errors.append(f"active_period.{field}: is required and must be a 24-hour HH:MM time, got {value!r}")
    end_state = period.get("end_state", SAFE_STATE_UNENERGISED)
    if end_state not in BUILT_IN_SAFE_STATES:
        errors.append(f"active_period.end_state: must be one of {', '.join(BUILT_IN_SAFE_STATES)}, got {end_state!r}")


def _check_timezone_and_parameters(document, errors) -> None:
    """Check the time zone name and the adjustable parameter block.

    The time zone is validated by asking the platform's database rather than matching a
    pattern, because a plausible-looking name that no database knows is exactly the case
    that would otherwise fail at the first daylight-saving boundary instead of at load.

    Args:
        document (dict): the parsed document
        errors (list): appended to with any problems found
    """
    timezone = document.get("timezone")
    if timezone is not None:
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            errors.append(f"timezone: {timezone!r} is not a known time zone name")

    parameters = document.get("parameters")
    if parameters is None:
        return
    if not isinstance(parameters, dict):
        errors.append(f"parameters: must be a mapping, got {type(parameters).__name__}")
        return
    for parameter, value in parameters.items():
        if not _is_number(value):
            errors.append(f"parameters[{parameter!r}]: must be a number, got {value!r}")


def _check_scalars(name, document, errors) -> None:
    """Check the document's own top-level values, as distinct from its sections.

    Args:
        name (str): the control's name, taken from the filename
        document (dict): the parsed document
        errors (list): appended to with any problems found
    """
    if "name" in document and document["name"] != name:
        # The filename is the identity; a name key that disagrees means one of the two is
        # about a different control, and guessing which would be worse than refusing.
        errors.append(f"name: is {document['name']!r} but the file is named {name!r}")
    if "enabled" in document and not isinstance(document["enabled"], bool):
        errors.append(f"enabled: must be true or false, got {document['enabled']!r}")
    if "enable_when" in document and not isinstance(document["enable_when"], str):
        errors.append("enable_when: must be a rule expression")

    _check_timezone_and_parameters(document, errors)

    safe_state = document.get("safe_state", SAFE_STATE_UNENERGISED)
    if safe_state not in BUILT_IN_SAFE_STATES:
        errors.append(f"safe_state: must be one of {', '.join(BUILT_IN_SAFE_STATES)}, got {safe_state!r}")


def validate_control(name, document, settings=None):
    """Check one control document completely: its shape, then its rules, then its sources.

    The one call a caller should make. Everything that reads a stored document goes
    through here - ``--check-config``, the supervisor, a control process starting, the
    test harness - so that "is this document usable" has a single answer rather than each
    caller assembling its own and getting a different one.

    Three halves, each answering a question the others cannot:
    :func:`validate_control_structure` for the shape the store requires,
    :func:`validate_control_rules` for what the rule parser will accept, and
    :func:`validate_control_sources` for whether the sources named exist and can do what is
    asked of them. A caller wanting only one of those can call it directly.

    Args:
        name (str): the control's name, which its ``name`` key must agree with
        document (dict): the parsed document
        settings (dict or None): the parsed settings, so a source can be checked against
            this installation and not only against the build. Omitted means structural and
            rule validity only; every caller that could actually run the control passes it.

    Returns:
        list: human-readable problems, empty when the document is sound
    """
    return (
        validate_control_structure(name, document)
        + validate_control_rules(document)
        + validate_control_sources(document, settings)
    )


def validate_control_sources(document, settings=None):
    """Check that every source a control names exists and can do what is asked of it.

    The third half, and the one that asks a question the document cannot answer about
    itself: whether ``hue`` is a source this build knows, and whether it can switch a
    device, are facts about the code rather than about the file.

    **A devices entry needs more than a readable source.** ``MCP_ACTUATES_DEVICES`` is the
    narrow claim - this source can switch a named device on and off - and it is not implied
    by a source being writable at all. Without this check, a control naming a source that
    cannot actuate passed ``--check-config``, started, and failed on its first command, a
    cycle window after being told the configuration was fine.

    Structure is not re-reported here. A section that is not a mapping, an entry that is
    not a mapping, a missing or non-string ``source``: each is named precisely by the
    structural check, and saying it twice has an operator looking for two faults.

    Args:
        document (dict): the parsed document
        settings (dict or None): the parsed settings, for the configured-here check;
            omitted means the build-level checks only

    Returns:
        list: human-readable problems, empty where every source named can do its job
    """
    from toinflux.general import source_block_problem, source_class

    errors = []
    for key, must_actuate in (("inputs", False), ("devices", True)):
        entries = document.get(key)
        if not isinstance(entries, dict):
            continue
        for entry_name, entry in sorted(entries.items(), key=lambda item: str(item[0])):
            # An entry name that is not a string is the structural check's to report, and it
            # does. Reporting a source fault against it as well would name the same broken
            # key twice, in two different vocabularies.
            if not isinstance(entry_name, str) or not isinstance(entry, dict):
                continue
            source = entry.get("source")
            if not isinstance(source, str) or not source.strip():
                continue
            where = f"{key}[{entry_name!r}]"
            try:
                handler = source_class(source)
            except ConfigError:
                errors.append(f"{where}: source {source!r} is not one this build collects from")
                continue
            if must_actuate and not getattr(handler, "MCP_ACTUATES_DEVICES", False):
                errors.append(
                    f"{where}: source {source!r} cannot switch a device on and off, so a control " f"cannot actuate it"
                )
                continue
            # Knowing the class is not knowing the installation. Without this, a control
            # naming `hue` on a machine whose settings have no `hue` block passed
            # --check-config, started, and died on its first safe-state command - then
            # again on every restart the backoff allowed, each time reporting a fault that
            # was really a missing settings section. The same question settings validation
            # already asks, asked with the same function so the two cannot drift.
            unusable = source_block_problem(source.lower(), settings) if settings is not None else None
            if unusable:
                errors.append(f"{where}: {unusable}")
    return errors


def validate_control_rules(document):
    """Parse every rule a control declares, returning what would not parse.

    Separate from the structural check because the two are answered by different code: the
    store knows which keys must be present and what shape they take, and the parser knows
    what a rule may say. They were separate before this existed too - the difference is
    that the parser was only reached when a control process started, so a document with a
    malformed expression passed ``--check-config``, was written, and killed the control at
    startup instead.

    **Skipped entirely where a name source is unusable.** Names come from the ``inputs``
    and ``parameters`` sections, so without them every rule reports every name it uses as
    undeclared - a cascade of consequences from one fault the structural check already
    names precisely.

    Unusable means two different things for the two sections, because one is required and
    one is not. ``inputs`` must be a mapping, and a document without it has none of the
    names its rules will read; ``parameters`` is optional, so an absent one is ordinary and
    must not suppress anything.

    Takes no ``name``: a rule does not know which control it is in, and every message here
    names its slot instead, which is what an operator needs to find it.

    Args:
        document (dict): the parsed document

    Returns:
        list: human-readable problems, empty when every rule parses
    """
    from toinflux.rules import RuleSyntaxError, parse_rule

    for key in ("inputs", "parameters"):
        section = document.get(key)
        if not isinstance(section, dict) and (key in REQUIRED_CONTROL_KEYS or section is not None):
            return []
    names = rule_names(document)
    errors = []
    for path, where, _optional in CONTROL_RULE_SLOTS:
        text = document
        for key in path:
            text = text.get(key) if isinstance(text, dict) else None
        # A slot that is absent, or holds something that is not text, is the structural
        # check's to report - and it already does, naming the same slot. Reporting it twice
        # would have an operator looking for two faults.
        if not isinstance(text, str):
            continue
        try:
            parse_rule(text, allowed_names=names)
        except RuleSyntaxError as exc:
            errors.append(f"{where}: {exc}")
    return errors


def validate_control_structure(name, document):
    """Check one control document's shape, returning every problem found.

    Structure only. A rule expression is checked to be a string and nothing more: the
    parser reports its own syntax errors, so that a missing key and a malformed
    expression are each described by the code that understands them. See
    :func:`validate_control_rules` for the other half, and :func:`validate_control` for
    both together, which is what a caller normally wants.

    Every problem is collected rather than raising on the first, because an operator
    writing a control by hand would otherwise fix one typo per run.

    Args:
        name (str): the control's name, which its ``name`` key must agree with
        document (dict): the parsed document

    Returns:
        list: human-readable problems, empty when the document is structurally sound
    """
    errors = []

    unknown = set(document) - CONTROL_KEYS
    if unknown:
        errors.append(f"unknown key(s): {_render_names(unknown)}")
    for key in REQUIRED_CONTROL_KEYS:
        if key not in document:
            errors.append(f"{key}: is required")
        elif document[key] is None:
            # `inputs:` with nothing indented under it is a mapping key whose value is None,
            # so the presence check above is satisfied by a section that is not there. Every
            # shape check further down then treats None as "absent" and says nothing, and
            # the document passes with no inputs, no devices or no loop at all. Named as its
            # own case because the cause is almost always a block that was not indented,
            # which "is required" alone does not point at.
            errors.append(f"{key}: is required and has nothing under it")

    _check_scalars(name, document, errors)
    _check_mapping_of(document, "inputs", errors, ("source", "field"), ("max_age",), INPUT_KEYS)
    devices = _check_mapping_of(
        document, "devices", errors, ("source", "device"), ("min_transition_seconds",), DEVICE_KEYS
    )
    _check_instances_are_names(devices, errors)
    _check_one_key_per_actuator(document, devices, errors)
    _check_pid(document, errors)
    _check_stages(document, devices, errors)
    _check_active_period(document, errors)
    return errors


def actuator_identity(spec):
    """Return what identifies one device entry at the far end, or None where it cannot.

    ``(source, instance, device)``, because that triple is what reaches the bridge: two
    entries differing only in an absent versus explicit instance are one actuator to it.

    **The source is lower-cased and the other two are not**, which follows what each one
    means rather than being a general tidy-up. ``source_class`` resolves a source name
    case-insensitively, so ``Hue`` and ``hue`` are one handler commanding one device - left
    verbatim they produced two identities and slipped past both the duplicate-device check
    and the one-enabled-owner rule. A device name is the bridge's own, and the bridge tells
    two lights apart by case. Folding it would invent a clash between two real devices, which
    is the opposite failure and the worse one, since it refuses a configuration that works.

    **An absent instance is not normalised here and is not comparable by equality.** ``None``
    means "the first configured target" (see ``Hue.bridge``), so an entry omitting it and one
    naming that bridge explicitly are the same actuator while comparing unequal. Resolving it
    would need the settings document, which validation does not have, so the comparison is
    done by :func:`actuators_may_be_one` rather than by matching tuples.

    Args:
        spec (object): a ``devices`` entry

    Returns:
        tuple or None: the identity, or None where the entry is unusable
    """
    if not isinstance(spec, dict):
        return None
    source, device = spec.get("source"), spec.get("device")
    if source is None or device is None:
        return None
    return (source.lower() if isinstance(source, str) else source, spec.get("instance"), device)


def actuators_may_be_one(first, second):
    """Whether two actuator identities may name the same physical device.

    Not equality, because an absent instance is ambiguous rather than distinct: ``None``
    means "the first configured target", so an entry that omits it and one that names that
    target explicitly are the same actuator and compare unequal. Resolving the default would
    need the settings document, which the validators do not have.

    So an absent instance is treated as possibly matching any instance of the same source and
    device. That errs toward refusing - two controls on different bridges, one of which omits
    its instance, are reported as a clash they may not have - and that is the right direction
    here: the alternative is two loops commanding one heater, and the refusal names the fix,
    which is to say which target each one means.

    Args:
        first (tuple): ``(source, instance, device)``
        second (tuple): the identity to compare it with

    Returns:
        bool: True where they may be one actuator
    """
    if first[0] != second[0] or first[2] != second[2]:
        return False
    return first[1] is None or second[1] is None or first[1] == second[1]


def control_is_enabled(document):
    """Whether a stored document will actuate.

    ``get(..., True)`` because an omitted key means enabled - the same reading
    :class:`toinflux.gating.Gate` uses, and the one that decides whether devices move.

    Args:
        document (object): a parsed control document

    Returns:
        bool: True where the control is enabled
    """
    return isinstance(document, dict) and document.get("enabled", True) is True


def actuators_owned(document):
    """Return the actuator identities one control commands.

    Args:
        document (object): a parsed control document

    Returns:
        set: the identities, empty where the document has no usable devices section
    """
    devices = document.get("devices") if isinstance(document, dict) else None
    if not isinstance(devices, dict):
        return set()
    return {identity for identity in (actuator_identity(spec) for spec in devices.values()) if identity is not None}


def enabled_owner_of(actuators, documents, excluding=None):
    """Return the first enabled control already commanding any of these actuators.

    **One enabled control per actuator, ever.** Two loops sharing a heater fight: each runs
    its own PID against its own setpoint, each applies its own safe state, and whichever
    commanded last wins until the other's next cycle. Nothing in either one can detect it.

    Names are considered in sorted order so the answer does not depend on directory listing
    order - the same pair must give the same answer on every machine and every start.

    Args:
        actuators (set): the identities being claimed
        documents (dict): name to parsed document, the stored controls to check against
        excluding (str or None): a control to ignore, being the one doing the claiming

    Returns:
        tuple or None: ``(owner name, actuator)`` for the first clash, or None where there
        is none
    """
    for name in sorted(documents):
        if name == excluding or not control_is_enabled(documents[name]):
            continue
        for theirs in sorted(actuators_owned(documents[name]), key=repr):
            for ours in sorted(actuators, key=repr):
                if actuators_may_be_one(ours, theirs):
                    return name, ours
    return None


def _check_instances_are_names(devices, errors) -> None:
    """Refuse a device ``instance`` that is not a usable name.

    It is optional and was never shape-checked, so a list or a mapping validated cleanly and
    then reached ``command_devices``, which groups devices by ``(source, instance)`` - and a
    tuple containing a list cannot be hashed, so the control died with a TypeError rather
    than the "cannot run" a configuration fault is supposed to produce. An empty string is
    refused too: it is not ``None``, so it does not mean "the first configured target", and
    it names nothing.

    Args:
        devices (dict or None): the devices section, where it was usable
        errors (list): appended to with any problems found
    """
    for key, spec in sorted((devices or {}).items(), key=lambda item: repr(item[0])):
        if not isinstance(spec, dict) or "instance" not in spec:
            continue
        instance = spec["instance"]
        if not isinstance(instance, str) or not instance.strip():
            errors.append(
                f"devices.{key}.instance: must be a non-empty name where it is given, got {instance!r} - "
                f"omit it entirely to mean the first configured target"
            )


def _check_one_key_per_actuator(document, devices, errors) -> None:
    """Refuse two device keys that name the same physical actuator.

    Two keys pointing at one light are not two devices, and the ladder treats them as if
    they were: a stage may set one true and the other false, and both commands are sent in
    whatever order the mapping yields. The rung's effect is then decided by dict ordering,
    which is not a thing a control should depend on and not a thing an operator reading the
    ladder would expect. It validated cleanly before this.

    Identity is ``(source, instance, device)`` because that is what reaches the far end -
    two keys differing only in an absent versus explicit instance are the same actuator to
    the bridge, so the instance is normalised rather than compared as written.

    Args:
        document (dict): the parsed control document
        devices (dict or None): the devices section, where it was usable
        errors (list): appended to with any problems found
    """
    if not devices:
        return
    # Through `actuator_identity` rather than rebuilding the triple here. It was built in
    # both places, so canonicalising the source fixed the cross-document rule and left this
    # one still treating `Hue` and `hue` as two actuators - one concept with two
    # implementations diverges the first time either is corrected.
    # Pairwise rather than keyed on the identity, because an absent instance is ambiguous
    # rather than distinct - see `actuators_may_be_one`. A dict keyed on the tuple put
    # `(hue, None, far)` and `(hue, bridge1, far)` in different buckets, which is the same
    # actuator in two entries and exactly what this refuses.
    entries = []
    for key, spec in sorted(devices.items(), key=lambda item: repr(item[0])):
        identity = actuator_identity(spec)
        if identity is None:
            # A missing source or device is already reported by the shape check above, and
            # guessing an identity from half of one would invent a second complaint.
            continue
        entries.append((key, identity))
    for index, (key, identity) in enumerate(entries):
        for other_key, other in entries[index + 1 :]:
            if actuators_may_be_one(identity, other):
                errors.append(
                    f"devices: {_render_names([key, other_key])} name the same actuator "
                    f"({identity[2]!r} on {identity[0]!r}) - a stage could set them to opposite "
                    f"states and which one wins would depend on ordering"
                )


def validate_stored_controls(settings_file=None, settings=None) -> None:
    """Check every stored control, reporting all of their problems at once.

    Args:
        settings_file (str or None): the settings path the process was started with
        settings (dict or None): the parsed settings, so each control's sources can be
            checked against this installation as well as against the build

    Raises:
        ConfigError: one or more stored controls is unreadable or structurally wrong
    """
    problems = []
    documents = {}
    for name in list_controls(settings_file):
        try:
            document = load_control(name, settings_file)
        except ConfigError as exc:
            problems.append(str(exc))
            continue
        errors = validate_control(name, document, settings)
        problems.extend(f"control {name!r}: {error}" for error in errors)
        if not errors:
            # Only documents that are usable on their own. A broken one has already said so,
            # and reading actuators out of it would add a second complaint about the same
            # fault - or invent one, since its devices section may be the thing that is wrong.
            documents[name] = document
    problems.extend(shared_actuator_problems(documents))
    if problems:
        raise ConfigError("\n  ".join(["control configuration is invalid:"] + problems))


def shared_actuator_problems(documents):
    """Return one problem per pair of enabled controls commanding the same actuator.

    Checked across documents because nothing inside one can see it: each control validates
    perfectly, starts its own process, runs its own PID against its own setpoint and applies
    its own safe state. Whichever commanded last wins until the other's next cycle, and
    neither can tell. A heater that will not hold a temperature and a journal showing two
    healthy controls is a long evening.

    Reported for enabled controls only. A duplicate stored *disabled* is how somebody
    prepares a replacement before switching over, which is a workflow rather than a fault.

    Args:
        documents (dict): name to parsed document, already individually valid

    Returns:
        list: a problem per clash, naming both controls and the actuator
    """
    problems = []
    claimed = []
    for name in sorted(documents):
        if not control_is_enabled(documents[name]):
            continue
        for identity in sorted(actuators_owned(documents[name]), key=repr):
            owner = next((who for who, held in claimed if actuators_may_be_one(identity, held)), None)
            if owner is not None:
                source, instance, device = identity
                where = f"{device!r} on {source!r}" + (f" ({instance!r})" if instance else "")
                problems.append(
                    f"controls {owner!r} and {name!r} are both enabled and both "
                    f"command {where} - two loops commanding one device fight, so disable one"
                )
            else:
                claimed.append((name, identity))
    return problems


def controls_enabled(settings):
    """Whether this installation runs the control subsystem at all.

    Off unless it is switched on. Controls actuate devices unattended, so an installation
    that has not said it wants that does not get it - and this is deliberately not the
    collector's ``mcp_read_write`` flag, because wanting a heating loop is not the same as
    granting a model device-write access.

    Exactly ``true`` and nothing else. ``enabled: "true"`` is a string, and a string is
    truthy, so a loose check would start unattended actuation for somebody who quoted a
    YAML boolean.

    Args:
        settings (dict): the parsed settings document

    Returns:
        bool: True where ``controls.enabled`` is exactly true
    """
    block = settings.get("controls")
    return isinstance(block, dict) and block.get("enabled") is True


def control_writes_enabled(settings):
    """Whether an MCP client may create, change or remove controls here.

    A second opt-in on top of :func:`controls_enabled`, because the two grant different
    things. ``controls.enabled`` runs the loops the operator wrote; this hands a model
    authorship of them, and an authored control actuates devices repeatedly and unattended
    for as long as it exists. Granting the first is not granting the second.

    **Both, checked here rather than left to the caller.** ``mcp_write`` alone describes an
    installation that has not asked for controls at all, and writing documents nothing will
    ever run is not a capability worth granting. The one caller does check
    ``controls_enabled`` first, so this conjunction changes nothing today - it is here so
    that the answer cannot become wrong by being asked somewhere new, which is the failure
    a predicate that half-answers its own question invites.

    It is also not the collector's ``mcp_read_write``: that permits an action now (turn
    this light on), where this permits a standing rule that keeps acting.

    Exactly ``true`` and nothing else, for the same reason as ``controls.enabled`` - a
    quoted ``"true"`` is a truthy string, and a loose check would hand out write access to
    somebody who quoted a YAML boolean.

    Off does not mean the model is stuck: the read tools still describe the format, so it
    can compose a document for the operator to save by hand. :func:`toinflux.mcp_controls`
    reports that state in ``get_control_schema`` so it can say so rather than guess.

    Args:
        settings (dict): the parsed settings document

    Returns:
        bool: True where ``controls.enabled`` and ``controls.mcp_write`` are both exactly true
    """
    block = settings.get("controls")
    return controls_enabled(settings) and isinstance(block, dict) and block.get("mcp_write") is True
