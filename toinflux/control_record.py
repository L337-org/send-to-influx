"""The history of what each control's loop did, written to InfluxDB one point per cycle.

A control keeps its integral on disk so a restart can resume, and ``get_control_state``
reports the present moment, but neither keeps the past - and tuning a loop is a question
about the past: the input against the setpoint, the demand split into its terms, and what
the devices were told to do, on one time axis. A P term swinging from side to side is too
much ``kp``, an I term creeping for an hour is too little ``ki``, and an I term pinned at the
top of the ladder is a plant that cannot deliver what is asked, which no tuning fixes.

**One measurement, ``control``, with the control's name as its ``control`` tag.** The tag is
the measurement's instance axis, so the existing read tools list the controls, scope to one
and report per control without a tool of their own, and a Grafana variable selects between
them.

**Configured once, as ``controls.db`` or ``controls.bucket``**, and resolved exactly as every
source's database is. Neither set means no record, which the control subsystem says once as
it starts.

**Recording never affects control.** Points go through the ordinary buffered writer, so an
outage queues them rather than failing a cycle, and a failed write is said once for the
outage rather than once per cycle.

Not a collector, and deliberately not registered as one: there is nothing to poll, and a
``sources:`` entry naming it would pass validation and then fail at its first collection.
The MCP read layer is the only other thing that constructs it.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import logging

from toinflux.general import RepeatingProblem
from toinflux.influx import DataHandler, InfluxWriteError, escape_key_or_tag_value, resolve_db

#: The settings section the record is configured in, and the name the MCP read tools know
#: it by. The section already existed, holding the subsystem's own switches.
RECORD_SOURCE = "controls"

#: The measurement every control's points are written to.
RECORD_MEASUREMENT = "control"

#: The tag naming which control a point belongs to.
RECORD_TAG = "control"

#: ``state`` for a cycle that ran the loop.
ACTIVE = "active"

#: ``state`` for a cycle inside the active period that could not run the loop - an input
#: stale or unreadable, a rule that could not be evaluated - and fell to the safe state.
#: Not ``held``: that word already means a device inside its ``min_transition_seconds``, in
#: the ``-v`` line and in ``get_control_state``, and one word with two meanings in the same
#: tuning session is how a conclusion gets drawn from the wrong one.
FAIL_SAFE = "fail_safe"

#: The fields a cycle that ran the loop writes, in the order they are written. A fail-safe
#: point carries ``state`` alone, because none of the rest was computed.
TERM_FIELDS = ("input", "setpoint", "demand", "p", "i", "d", "kp", "ki", "kd")


def record_destination(settings):
    """Return the database or bucket the record goes to, or None where there is none.

    Args:
        settings (dict): the parsed settings document

    Returns:
        str or None: the db or bucket name, None where the record is not configured
    """
    block = settings.get(RECORD_SOURCE)
    if not isinstance(block, dict):
        return None
    return resolve_db(block, settings.get("influx") or {}) or None


def record_settings_errors(settings):
    """Return errors for the record's own settings, empty where they are usable or absent.

    Args:
        settings (dict): the parsed settings document

    Returns:
        list: error strings naming the setting at fault
    """
    block = settings.get(RECORD_SOURCE)
    if not isinstance(block, dict):
        # The block's own shape is reported by the settings validation that owns it.
        return []
    return [
        f"{RECORD_SOURCE}.{key} must be the name of an InfluxDB database or bucket (got {block[key]!r})"
        for key in ("db", "bucket")
        if key in block
        and (not isinstance(block[key], str) or not block[key].strip() or any(char in block[key] for char in "\r\n"))
    ]


class ControlRecord(DataHandler):
    """Writes one control's cycles to the ``control`` measurement, and describes them to readers.

    Built with the control's name as its instance by the control process, which writes
    through it; built with no instance by the MCP read layer, which only reads the class's
    description of the data. The instance also keys the write buffer, so each control has
    its own backlog - though each control runs in a process of its own, so they could not
    share one anyway.

    Attributes:
        MCP_MEASUREMENT (str): "control" - one measurement for every control.
        MCP_INSTANCE_TAG (str): "control" - the control's name, which is how the read tools
            list the controls and scope a read to one.
        MCP_LIVE_STATE (bool): False - the latest point is what the loop last did.
        MCP_DESCRIPTION (str): what this history advertises to an MCP client.
        MCP_FIELD_METADATA (dict): every field's aggregation kind and what it means for tuning.
    """

    MCP_MEASUREMENT = RECORD_MEASUREMENT
    MCP_INSTANCE_TAG = RECORD_TAG
    # Nothing to read live: the control's own process holds the loop, and what it last did is
    # exactly the latest point.
    MCP_LIVE_STATE = False
    MCP_DESCRIPTION = (
        "Each control loop's PID history, one point per cycle while it is acting: input against "
        "setpoint, the demand split into its P, I and D terms, the gains, and the level delivered. "
        "Use it to tune a control."
    )
    # Every field carries a description, because none of them is self-describing to somebody
    # who has not read CONTROLS.md, and the agent tuning a loop is exactly that somebody. No
    # units: the input and setpoint are in whatever the control's rules produce, and the
    # level scale is the operator's own.
    MCP_FIELD_METADATA = {
        "input": {
            "kind": "gauge",
            "description": "The control's input rule as evaluated this cycle: the value being controlled, "
            "in the units of whatever it reads.",
        },
        "setpoint": {
            "kind": "gauge",
            "description": "The setpoint rule as evaluated this cycle, in the same units as input. "
            "The loop works to bring input to it.",
        },
        "demand": {
            "kind": "gauge",
            "description": "What the PID asked for, on the control's own level scale (its stage levels). "
            "It is p + i + d, limited to the ladder's range and any max_level cap.",
        },
        "p": {
            "kind": "gauge",
            "description": "The proportional term's share of demand, kp x (setpoint - input). "
            "Swinging from side to side cycle after cycle means kp is too high.",
        },
        "i": {
            "kind": "gauge",
            "description": "The integral term's share of demand, on the level scale: ki x error accumulated "
            "over time, limited to the ladder's range. Creeping for a long time means ki is too low; pinned "
            "at the top of the ladder means the devices cannot deliver what is asked. Once input sits on "
            "setpoint it is the level needed to hold it there.",
        },
        "d": {
            "kind": "gauge",
            "description": "The derivative term's share of demand, from how fast input is changing. "
            "Zero while kd is zero.",
        },
        "kp": {"kind": "gauge", "description": "The proportional gain in effect this cycle. A step is a retune."},
        "ki": {
            "kind": "gauge",
            "description": "The integral gain in effect this cycle: level added per unit of error per second.",
        },
        "kd": {
            "kind": "gauge",
            "description": "The derivative gain in effect this cycle: level per unit of input change per second.",
        },
        "delivered": {
            "kind": "gauge",
            "description": "The level the devices were commanded to over the cycle's window, on demand's scale: "
            "switched devices time-weighted across the stages used, a dimmable device read off the ladder "
            "at the value it was set to. Below or above demand where a device was held by "
            "min_transition_seconds or the ladder could not reach demand. What was commanded, not "
            "confirmed; the device's own source shows whether it switched.",
        },
        "state": {
            "kind": "state",
            "description": "Whether the loop ran: 'active' where it did this cycle, 'fail_safe' where the "
            "cycle was inside the active period but could not run the loop (an input stale or unreadable, "
            "or a rule that could not be evaluated), so the devices went to their safe state and no other "
            "field was written. No point at all is written while a control is disabled, outside its active "
            "period or gated off by enable_when.",
        },
    }

    def __init__(self, settings_file=None, instance=None):
        """Build the record for one control, or for reading every control's.

        Args:
            settings_file (str or None): the settings path the process was started with
            instance (str or None): the control whose cycles this writes, or None to read

        Raises:
            ConfigError: where the settings have no ``controls`` section
        """
        super().__init__(RECORD_SOURCE, settings_file=settings_file, instance=instance)
        # For the life of the writer, which for a control is the life of its process: that is
        # the span over which an outage repeats.
        self._problems = RepeatingProblem()

    def write(self, state, terms=None, delivered=None, timestamp=None) -> None:
        """Write one cycle's point, never raising for a failure to write it.

        **Returns whether or not the point reached InfluxDB**, because nothing the caller
        could do with the answer would help: the point is buffered and sent with the next one
        that gets through, and the failure has been said. What a control must not do is stop
        controlling because its history could not be written.

        Args:
            state (str): :data:`ACTIVE` or :data:`FAIL_SAFE`
            terms (StepTerms or None): the step's terms, for an active point
            delivered (float or None): the level the window's plan delivers, for an active point
            timestamp (int or None): the cycle's own time, in epoch seconds
        """
        fields = {}
        if terms is not None:
            fields.update({name: float(getattr(terms, name)) for name in TERM_FIELDS})
            if delivered is not None:
                fields["delivered"] = float(delivered)
        fields["state"] = state
        self.influx_header = f"{RECORD_MEASUREMENT},{RECORD_TAG}={escape_key_or_tag_value(self.instance)} "
        try:
            self.send_data(fields, timestamp=timestamp)
        except InfluxWriteError:
            # Said already, once for the outage, by `_write_problem` below; and buffered by
            # `send_data`, which is the handling. Re-raising would carry a history problem into
            # the control's cycle, which is the one thing this must never do.
            return
        # "full" is left to repeat on its own schedule: a point already dropped stays dropped,
        # and recovering does not un-say that.
        self._problems.cleared("write", "Control %r is recording its cycles to InfluxDB again", self.instance)

    def _write_problem(self, kind, level, message, *args) -> None:
        """Say a write failure once for the outage rather than once per cycle.

        Args:
            kind (str): which failure this is
            level (int): the logging level
            message (str): a %-style format string
            *args: its arguments
        """
        # A failed post and a failed flush are one outage seen from two places - the flush is
        # what a post becomes once a backlog has built up - so they share a key, and the
        # second is a repeat of the first rather than news. A buffer that has started dropping
        # points *is* news during an outage already reported, so it has its own. The identity
        # is the key alone: the messages carry an exception and a count, which change every
        # cycle and would otherwise make every repeat look new.
        key = "full" if kind == "full" else "write"
        self._problems.report(
            key,
            level,
            "Control %r could not record its cycle: " + message,
            self.instance,
            *args,
            identity=key,
        )


def log_record_destination(settings) -> None:
    """Say once, as the control subsystem starts, where the controls' history goes.

    Args:
        settings (dict): the parsed settings document
    """
    destination = record_destination(settings)
    if destination is None:
        logging.info(
            "Controls are not recording their PID history: set %s.db (InfluxDB 1) or %s.bucket (InfluxDB 2) "
            "to keep it for tuning",
            RECORD_SOURCE,
            RECORD_SOURCE,
        )
    else:
        logging.info("Controls are recording their PID history to %r, measurement %r", destination, RECORD_MEASUREMENT)
