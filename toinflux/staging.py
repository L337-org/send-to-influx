"""Turning a control's demand into device states over one cycle window.

A control's actuators are switches, not dials: two heaters on smart plugs give a ladder of
discrete levels. The PID asks for a number somewhere between two rungs, and this decides
what the devices actually do about it.

**Stage selection plus time-proportioning between adjacent stages.** Find the two rungs
bracketing the demand and split the window between them, so a demand of 1237 against rungs
at 750 and 1500 spends 65% of the window at 1500 and 35% at 750.

Neither half works alone. Selecting a single rung quantises the demand onto 750, which
winds the integral up until it jumps to 1500, overshoots, and oscillates. Time-proportioning
one element between off and full switches far more often for the same average.

Nothing here reads a clock or touches a device. It answers "what should happen during the
next window", and the loop that owns the clock carries it out - which is what lets the
awkward cases (a two-hour hold, a daylight-saving boundary) be tested against an injected
time rather than waited for.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT"

import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from toinflux.exceptions import ConfigError
from toinflux.rules import RuleEvaluationError


@dataclass(frozen=True)
class Stage:
    """One rung of the ladder: a level, and what every device does to reach it.

    Attributes:
        level (float): the magnitude on the operator's chosen scale, not in watts.
        declared (int): the stage's position in the document, which breaks ties between
            equal levels - the operator writes the one that should do the steady-state work
            first.
        states (Mapping): device name -> the state to command, covering every device the
            control owns. A read-only view: ``frozen=True`` stops the attribute being
            rebound and does nothing about the dict behind it, so without this one cycle's
            plan could be edited by another's.
    """

    level: float
    declared: int
    states: Mapping


def build_ladder(stages):
    """Return a control's stages ordered as a ladder.

    Sorted by declared level, because "the next stage up" is a question about magnitude and
    the document's order is the operator's, not the controller's.

    Equal levels are kept in declaration order rather than merged or refused. Two rungs can
    genuinely reach the same level by different means - "far heater on" and "near heater on"
    are both 750 - and they are not interchangeable: one sits next to the temperature sensor.
    Declaring the far one first is how the operator says which should do the steady-state
    work, so that order is preserved and used.

    Args:
        stages (list): the ``output.stages`` list, already structurally validated by the
            control store: every entry has a numeric ``level`` and a ``set`` covering every
            device.

    Returns:
        tuple: Stage, ordered by level then declaration

    Raises:
        ConfigError: where the ladder is empty
    """
    if not stages:
        raise ConfigError("a control's output.stages is empty: there is no ladder to work with")
    rungs = [
        Stage(
            level=_finite_level(stage["level"], index),
            declared=index,
            states=MappingProxyType(dict(stage["set"])),
        )
        for index, stage in enumerate(stages)
    ]
    return tuple(sorted(rungs, key=lambda rung: (rung.level, rung.declared)))


def _finite_level(level, index):
    """Return a stage's level as a float, refusing one that cannot be ordered.

    The control store rejects a non-finite level too, so this is the second line rather than
    the first. It is here because the consequence is silent: nan compares False against
    everything, so a ladder containing one is not sorted and a demand does not bracket
    within it - the control simply picks the wrong rung, for ever, with nothing logged.

    Args:
        level (object): the level as the document gave it
        index (int): the stage's position, for the message

    Returns:
        float: the level

    Raises:
        ConfigError: where the level is not a finite number
    """
    if isinstance(level, bool) or not isinstance(level, (int, float)) or not math.isfinite(level):
        raise ConfigError(f"output.stages[{index}].level must be a finite number (got {level!r})")
    return float(level)


def bracket(ladder, demand):
    """Return the two rungs a demand sits between.

    Both are the same rung where the demand is at or beyond an end of the ladder, which is
    the honest answer: a demand of 2000 against a ladder topping out at 1500 cannot be met,
    and the caller spends the whole window at 1500 rather than pretending otherwise.

    Where several rungs share the level being landed on, the earliest declared wins, which is
    what ``build_ladder`` ordered them for.

    Args:
        ladder (tuple): Stage in ladder order
        demand (float): the level the controller is asking for

    Returns:
        tuple: ``(lower, upper)`` Stage, equal at either end of the ladder
    """
    if demand <= ladder[0].level:
        return ladder[0], ladder[0]
    if demand >= ladder[-1].level:
        return ladder[-1], ladder[-1]
    lower = ladder[0]
    for rung in ladder:
        # An exact landing collapses, like either end of the ladder does. Returning the next
        # rung up with a zero share happens to work, because a dwell of no length is dropped
        # downstream - but then this function is only correct while that filter exists, and
        # a caller reading "the rungs a demand sits between" gets two rungs for a demand
        # that sits on one.
        if rung.level == demand:
            return rung, rung
        if rung.level > demand:
            return lower, rung
        # Only advance past a level once it is genuinely below the demand, so the first of
        # several equal-level rungs is the one a demand lands on.
        if rung.level > lower.level:
            lower = rung
    return lower, ladder[-1]


def cap_ladder(ladder, max_level):
    """Return the ladder with every rung above a cap removed.

    The cap applies to the **ladder**, not to the demand, and the difference is the point of
    having it. Capping the demand at 1000 would still time-proportion between rungs at 750
    and 1500, so the actuator would spend part of every window at 1500 and average 1000 -
    fine if the cap expresses a preference, wrong if it expresses a limit. It expresses a
    limit: the rule behind it is house load or grid carbon, and neither is satisfied by
    breaching the figure briefly and often.

    Where no rung is at or below the cap, the lowest is kept. An empty ladder has nothing to
    command, and the lowest rung is the least the control can do rather than a breach.

    Args:
        ladder (tuple): Stage in ladder order
        max_level (float or None): the cap, or None for no cap

    Returns:
        tuple: Stage, never empty

    Raises:
        ConfigError: where the cap is not a finite number
    """
    if max_level is None:
        return ladder
    # A cap comes from evaluating a rule, and the rule language can produce a non-finite
    # number: `1e400` is inf and `1e400 - 1e400` is nan. A nan compares False against every
    # rung, so it would silently collapse the ladder to its lowest - fail-safe by accident,
    # and indistinguishable from a cap that genuinely forbids everything.
    if isinstance(max_level, bool) or not isinstance(max_level, (int, float)) or not math.isfinite(max_level):
        raise ConfigError(f"output.max_level evaluated to {max_level!r}, which is not a level to cap at")
    allowed = tuple(rung for rung in ladder if rung.level <= max_level)
    return allowed or ladder[:1]


@dataclass(frozen=True)
class Dwell:
    """One stretch of a cycle window spent at one rung.

    Attributes:
        stage (Stage): what the devices do for this stretch.
        seconds (float): how long it lasts.
    """

    stage: Stage
    seconds: float


def plan_window(ladder, demand, cycle_seconds, min_transition_for):
    """Return how a cycle window is split between rungs to average out at a demand.

    One dwell where the demand sits on a rung or beyond an end of the ladder; two where it
    falls between, in the proportion that makes the window average the demand.

    **A dwell shorter than a transitioning device can honour collapses the window onto one
    rung.** Commanding a heater on for forty seconds when it needs five minutes between
    changes is not a shorter burst of heat - it is a command the device ignores, or obeys at
    a cost the operator asked it not to pay. The nearer rung is chosen, so the error is the
    smaller one available.

    The minimum consulted is the longest among the devices that actually *change* between
    the two rungs. A device holding the same state across both is not transitioning, so its
    own minimum has nothing to say about this window - which is the same rule as "a stage
    change that leaves one heater untouched must not restart that heater's clock", applied
    a window earlier.

    Args:
        ladder (tuple): Stage in ladder order, already capped
        demand (float): the level the controller is asking for
        cycle_seconds (float): the window to fill
        min_transition_for (callable): device name -> its minimum transition in seconds

    Returns:
        tuple: Dwell, in the order they should be commanded, lower rung first

    Raises:
        ConfigError: where the cycle window is not a positive number of seconds
        RuleEvaluationError: where the demand is not a finite number, which is a cycle that
            cannot produce a plan rather than a control that must stop
    """
    if (
        not isinstance(cycle_seconds, (int, float))
        or isinstance(cycle_seconds, bool)
        # isfinite before the comparison: nan <= 0 is False, so a nan window would pass a
        # bare sign check and then divide a window that is not a length. Same trap the
        # durations in toinflux.inputs had.
        or not math.isfinite(cycle_seconds)
        or cycle_seconds <= 0
    ):
        raise ConfigError(f"output.cycle_seconds must be a positive number of seconds (got {cycle_seconds!r})")
    if isinstance(demand, bool) or not isinstance(demand, (int, float)) or not math.isfinite(demand):
        # Not a plan this cycle, so the caller falls safe rather than acting. A nan reaching
        # here did not do nothing: every comparison against it is False, so it fell through
        # bracket() to the top of the ladder and commanded a full window at maximum - the
        # heaters full on because of arithmetic nobody could see.
        raise RuleEvaluationError(f"a control's demand came out as {demand!r}, which is not a level to hold")
    lower, upper = bracket(ladder, demand)
    if lower is upper:
        return (Dwell(stage=lower, seconds=float(cycle_seconds)),)
    share = (demand - lower.level) / (upper.level - lower.level)
    upper_seconds = float(cycle_seconds) * share
    lower_seconds = float(cycle_seconds) - upper_seconds
    minimum = _minimum_transition(lower, upper, min_transition_for)
    if lower_seconds < minimum or upper_seconds < minimum:
        nearer = upper if share >= 0.5 else lower
        return (Dwell(stage=nearer, seconds=float(cycle_seconds)),)
    # Both stretches have length: bracket() collapses an exact landing, so the demand is
    # strictly between these two rungs and the share is strictly between 0 and 1. A zero
    # dwell would be a state change that immediately reverses.
    return (
        Dwell(stage=lower, seconds=lower_seconds),
        Dwell(stage=upper, seconds=upper_seconds),
    )


def _minimum_transition(lower, upper, min_transition_for):
    """Return the longest minimum transition among the devices changing between two rungs.

    Args:
        lower (Stage): the rung below
        upper (Stage): the rung above
        min_transition_for (callable): device name -> its minimum transition in seconds

    Returns:
        float: seconds, zero where no device changes
    """
    changing = [name for name, state in upper.states.items() if lower.states.get(name) != state]
    return max((float(min_transition_for(name)) for name in changing), default=0.0)
