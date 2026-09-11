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
SAFE_STATE_UNENERGISED = "unenergised"
SAFE_STATE_LEAVE_UNCHANGED = "leave_unchanged"
BUILT_IN_SAFE_STATES = (SAFE_STATE_UNENERGISED, SAFE_STATE_LEAVE_UNCHANGED)

# Keys a control document may carry at the top level. Checked as a closed set: a
# mistyped key that was merely ignored would leave the operator looking at a setting
# they believe is in force and is not.
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
    if not isinstance(name, str) or not re.match(CONTROL_NAME_PATTERN, name):
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
            logging.warning("Ignoring %r in the control directory: %s", entry, exc)
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
    path = control_path(name, settings_file)
    try:
        os.unlink(path)
    except FileNotFoundError as exc:
        raise ConfigError(f"no control named {name!r} at {path!r}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot delete control {name!r} at {path!r}: {exc}") from exc


def _is_number(value):
    """Whether a value is a number rather than something YAML merely allows.

    ``bool`` is excluded on purpose: it is a subclass of ``int`` in Python, so a stage
    written as ``level: true`` would otherwise validate and then sort as 1.

    Args:
        value (object): the parsed value

    Returns:
        bool: True for an int or float that is not a bool
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_mapping_of(document, key, errors, required_fields, optional_numbers=()):
    """Check one mapping-of-mappings section: inputs, devices.

    Args:
        document (dict): the control document
        key (str): the section's key
        errors (list): appended to with any problems found
        required_fields (tuple): field names each entry must carry as a non-empty string
        optional_numbers (tuple): field names that must be positive numbers if present

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
        where = f"{key}.{entry_name}"
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
    if not _is_number(stage.get("level")):
        errors.append(f"{where}.level: is required and must be a number")
    assignments = stage.get("set")
    if not isinstance(assignments, dict):
        errors.append(f"{where}.set: is required and must be a mapping of device to state")
        return
    unknown = sorted(set(assignments) - device_names)
    if unknown:
        errors.append(f"{where}.set: names no such device: {', '.join(unknown)}")
    missing = sorted(device_names - set(assignments))
    if missing:
        errors.append(
            f"{where}.set: does not say what to do with {', '.join(missing)} - " f"every stage must assign every device"
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
    for field in ("from", "to"):
        value = period.get(field)
        if not isinstance(value, str) or not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", value):
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
            errors.append(f"parameters.{parameter}: must be a number, got {value!r}")


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


def validate_control(name, document):
    """Check one control document's shape, returning every problem found.

    Structure only. A rule expression is checked to be a string and nothing more: the
    parser reports its own syntax errors, so that a missing key and a malformed
    expression are each described by the code that understands them.

    Every problem is collected rather than raising on the first, because an operator
    writing a control by hand would otherwise fix one typo per run.

    Args:
        name (str): the control's name, which its ``name`` key must agree with
        document (dict): the parsed document

    Returns:
        list: human-readable problems, empty when the document is structurally sound
    """
    errors = []

    unknown = sorted(set(document) - CONTROL_KEYS)
    if unknown:
        errors.append(f"unknown key(s): {', '.join(unknown)}")
    for key in REQUIRED_CONTROL_KEYS:
        if key not in document:
            errors.append(f"{key}: is required")

    _check_scalars(name, document, errors)
    _check_mapping_of(document, "inputs", errors, ("source", "field"), ("max_age",))
    devices = _check_mapping_of(document, "devices", errors, ("source", "device"), ("min_transition_seconds",))
    _check_pid(document, errors)
    _check_stages(document, devices, errors)
    _check_active_period(document, errors)
    return errors


def validate_stored_controls(settings_file=None) -> None:
    """Check every stored control, reporting all of their problems at once.

    Args:
        settings_file (str or None): the settings path the process was started with

    Raises:
        ConfigError: one or more stored controls is unreadable or structurally wrong
    """
    problems = []
    for name in list_controls(settings_file):
        try:
            document = load_control(name, settings_file)
        except ConfigError as exc:
            problems.append(str(exc))
            continue
        problems.extend(f"control {name!r}: {error}" for error in validate_control(name, document))
    if problems:
        raise ConfigError("\n  ".join(["control configuration is invalid:"] + problems))
