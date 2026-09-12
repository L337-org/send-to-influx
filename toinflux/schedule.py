"""When a control is allowed to act: the active period, and its daylight-saving edges.

An active period is a wall-clock window in the control's own time zone - "heat the
conservatory between 23:35 and 05:25" - because that is how somebody thinks about it. A
window that drifted an hour twice a year would be a window nobody asked for.

**Both daylight-saving behaviours are correct, and neither is an error.** A local time can
fail to happen (the clocks go forward over it) and can happen twice (they go back over it).
A wall-clock schedule should skip the first and fire twice on the second, which is what
comparing the current local time-of-day against the boundaries does by construction. Nothing
here computes a transition or special-cases a date.

That only works because the question asked is "what is the local time now", never "when does
this window next start". The second question has no single answer on a transition day, and
answering it is how a scheduler acquires a table of exceptions.

It also only works if the zone is still a zone when the question is asked. A control that
names no ``timezone`` keeps None rather than a captured one, because the obvious way to
capture the machine's zone yields a *fixed offset* that reports the same value in January
as in July - see :func:`control_zone`. A control process runs for months; a zone captured
in summer is an hour wrong all winter.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT"

import datetime
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from toinflux.controls import BUILT_IN_SAFE_STATES, CLOCK_TIME_PATTERN, SAFE_STATE_UNENERGISED
from toinflux.exceptions import ConfigError
from toinflux.general import render_values


@dataclass(frozen=True)
class ActivePeriod:
    """A wall-clock window in one control's time zone.

    Attributes:
        start (datetime.time): when the window opens, inclusive.
        end (datetime.time): when it closes, exclusive - so a window ending at 05:25 is not
            active at 05:25, and two windows meeting at that time cannot both claim it.
        end_state (str): what the devices do when the window closes, which is configured
            separately from ``safe_state`` because a control can want to be left alone on
            failure and switched off at dawn.
        zone (datetime.tzinfo or None): the control's time zone, and **None where it named
            no zone**, meaning the machine's local time resolved at each comparison rather
            than a zone captured once. See :func:`control_zone` for why that distinction
            is the difference between following daylight saving and drifting an hour
            behind it.
    """

    start: datetime.time
    end: datetime.time
    end_state: str
    zone: "datetime.tzinfo | None"


def parse_active_period(document):
    """Return a control's active period, or None where it has none.

    Args:
        document (dict): the control document, already structurally validated: the store has
            checked the times against a 24-hour pattern and the end state against the
            built-in names.

    Returns:
        ActivePeriod or None: the window, None for a control that runs whenever it is enabled

    Raises:
        ConfigError: where the times, zone or end state are unusable
    """
    period = document.get("active_period")
    if period is None:
        return None
    if not isinstance(period, dict):
        raise ConfigError(f"active_period must be a mapping, got {type(period).__name__}")
    start = _clock_time(period.get("from"), "active_period.from")
    end = _clock_time(period.get("to"), "active_period.to")
    if start == end:
        # Ambiguous rather than empty or eternal, and the two readings differ by 24 hours of
        # heating. Refused rather than guessed.
        raise ConfigError(
            f"active_period.from and active_period.to are both {period.get('from')!r}: a window "
            f"that starts when it ends is either always open or never open, so say which"
        )
    end_state = period.get("end_state", SAFE_STATE_UNENERGISED)
    if end_state not in BUILT_IN_SAFE_STATES:
        raise ConfigError(
            f"active_period.end_state must be one of {render_values(BUILT_IN_SAFE_STATES)}, got {end_state!r}"
        )
    return ActivePeriod(start=start, end=end, end_state=end_state, zone=control_zone(document))


def control_zone(document):
    """Return the time zone a control's wall-clock times are read in, or None for local.

    The control's own ``timezone`` where it names one. Where it names none, **None**: the
    machine's local time, resolved at each comparison rather than captured here.

    That is not a missing value, and the difference is an hour twice a year.
    ``datetime.now().astimezone().tzinfo`` returns a *fixed-offset* zone - measured on this
    machine under Europe/London it is ``timezone(timedelta(hours=1), 'BST')``, which reports
    the same offset in January as in July and carries no transition rules at all. A control
    process runs for months, so one captured in summer would read every wall-clock time an
    hour out for the whole winter: the drift this module says it does not have.

    ``astimezone(None)`` asks the system for the offset *of the moment being converted*, so
    the answer follows daylight saving with nothing here to maintain.

    Args:
        document (dict): the control document

    Returns:
        datetime.tzinfo or None: the named zone as a ``ZoneInfo``, or None for the
        machine's local time at the moment of comparison

    Raises:
        ConfigError: where the named zone is not one this machine knows
    """
    name = document.get("timezone")
    if name is None:
        return None
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"timezone {name!r} is not a time zone this machine knows: {exc}") from exc


def _clock_time(value, where):
    """Return an ``HH:MM`` string as a time.

    Args:
        value (object): the value from the document
        where (str): the field, for the message

    Returns:
        datetime.time: the parsed time

    Raises:
        ConfigError: where the value is not a 24-hour HH:MM string
    """
    # Against the store's own pattern rather than strptime, which accepts "9:00" and even
    # "9:0". A parser laxer than the validator means a document the store refuses can still
    # be parsed here, and the two disagree about what a control says.
    if not isinstance(value, str) or not re.fullmatch(CLOCK_TIME_PATTERN, value):
        raise ConfigError(f"{where} is required and must be a 24-hour HH:MM time, got {value!r}")
    hour, minute = value.split(":")
    return datetime.time(int(hour), int(minute))


def is_inside(period, moment):
    """Whether a moment falls within an active period.

    The moment is converted to the control's zone and compared by wall-clock time, which is
    what makes the daylight-saving behaviour fall out rather than needing handling: a local
    time the clocks skipped simply never arrives, and one they repeat arrives twice.

    A window whose end is before its start wraps midnight, which is the ordinary shape for
    overnight heating. 23:35 to 05:25 is active from 23:35 until midnight and from midnight
    until 05:25, and inactive through the day between.

    Args:
        period (ActivePeriod or None): the window; None means always active
        moment (datetime.datetime): an aware moment, converted into the period's zone, or
            into the machine's local time where the control named none

    Returns:
        bool: whether the control may act

    Raises:
        ConfigError: where the moment carries no time zone, so "what is the local time" has
            no answer
    """
    if period is None:
        return True
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ConfigError("an active period needs an aware moment: a naive one names no instant to convert")
    # A period with no zone of its own passes None, which is astimezone's own way of
    # saying "local time", evaluated for this moment rather than for whenever the control
    # happened to start.
    local = moment.astimezone(period.zone).time()
    if period.start < period.end:
        return period.start <= local < period.end
    # Wraps midnight: the window is the two ends of the day rather than the middle.
    return local >= period.start or local < period.end
