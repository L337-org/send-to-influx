"""One control's closed loop: inputs and rules in, a window plan out.

Holds the PID between cycles, because that is where the integral lives. Everything else a
cycle needs - the current input values, the clock - is passed in, so a two-hour hold or a
daylight-saving boundary is a sequence of calls rather than a wait.

The loop itself is deliberately small. Reading inputs is :mod:`toinflux.inputs`, turning a
demand into device states is :mod:`toinflux.staging`, and deciding whether to actuate at all
is the supervisor's. What is here is the part that has to remember something.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT"

import math

from simple_pid import PID

from toinflux.exceptions import ConfigError
from toinflux.rules import RuleEvaluationError, parse_rule
from toinflux.staging import build_ladder, cap_ladder, plan_window


class Controller:
    """The PID and ladder for one control, stepped once per cycle window.

    Carries ``ladder`` (the stages in ladder order, uncapped) and ``pid`` (the controller
    itself, held across cycles because that is where the integral lives).
    """

    def __init__(self, document, time_fn=None):
        """Build a controller from a validated control document.

        Args:
            document (dict): the control document, already structurally validated by the
                store and with its rules known to parse
            time_fn (callable or None): the clock simple-pid measures intervals with,
                injectable so a two-hour hold is a test rather than a wait

        Raises:
            ConfigError: where a rule does not parse, or the document names no stages
        """
        output = document.get("output") or {}
        self.ladder = build_ladder(output.get("stages") or [])
        self.devices = document.get("devices") or {}
        self.cycle_seconds = output.get("cycle_seconds")
        self._default_transition = output.get("min_transition_seconds", 0)
        names = tuple(document.get("inputs") or ()) + tuple(document.get("parameters") or ())
        self._setpoint_rule = _rule(document.get("pid", {}).get("setpoint"), names, "pid.setpoint")
        self._input_rule = _rule(document.get("pid", {}).get("input"), names, "pid.input")
        self._max_level_rule = _rule(output.get("max_level"), names, "output.max_level", optional=True)
        tunings = document.get("pid") or {}
        self.pid = PID(
            Kp=float(tunings.get("kp", 0.0)),
            Ki=float(tunings.get("ki", 0.0)),
            Kd=float(tunings.get("kd", 0.0)),
            # Bounded by what the ladder can actually deliver, which is what makes this
            # anti-windup rather than decoration: simple-pid clamps the integral to
            # output_limits, so an unreachable setpoint cannot accumulate a demand the
            # actuators were never going to satisfy and then overshoot working it off.
            output_limits=(self.ladder[0].level, self.ladder[-1].level),
            # Every call is a cycle boundary and must be honoured. simple-pid's default
            # sample_time silently returns the previous output for a call that arrives too
            # soon, which for a loop that runs every fifteen minutes would mean discarding a
            # reading rather than acting on it.
            sample_time=None,
            time_fn=time_fn,
        )

    def step(self, bindings, dt=None):
        """Run one cycle and return how the window should be spent.

        Args:
            bindings (dict): name -> value for every input and parameter the rules read
            dt (float or None): seconds since the previous cycle, for tests; None lets
                simple-pid measure it from the clock

        Returns:
            tuple: Dwell, filling one cycle window

        Raises:
            RuleEvaluationError: where a rule could not produce a usable value this cycle.
                The controller is left untouched, so the next cycle with good data works.
            ConfigError: where the cycle window or the cap is unusable
        """
        # Everything is evaluated and checked before the PID is touched at all, because one
        # non-finite reading poisons it permanently: simple-pid folds the value into the
        # integral, the integral is nan from then on, and every later cycle returns nan
        # however good the data becomes. A control that fails safe for ever after one bad
        # rule evaluation is worse than one that skips a cycle.
        setpoint = _finite(self._setpoint_rule.evaluate(bindings), "pid.setpoint")
        process_variable = _finite(self._input_rule.evaluate(bindings), "pid.input")
        ladder = self.ladder
        limits = None
        if self._max_level_rule is not None:
            cap = self._max_level_rule.evaluate(bindings)
            ladder = cap_ladder(self.ladder, cap)
            limits = (ladder[0].level, ladder[-1].level)
        self.pid.setpoint = setpoint
        if limits is not None:
            # The limits follow the cap. Left at the full ladder's range, the integral would
            # keep accumulating towards a level the cap has just forbidden, and every cycle
            # spent capped would be paid back as overshoot the moment it lifted.
            self.pid.output_limits = limits
        demand = self.pid(process_variable, dt=dt)
        return plan_window(ladder, demand, self.cycle_seconds, self.min_transition_for)

    def min_transition_for(self, device):
        """Return a device's minimum transition time in seconds.

        The control's own ``output.min_transition_seconds`` unless that device overrides it,
        because the constraint belongs to the hardware: one heater on a contactor may need
        minutes where a smart plug beside it does not care.

        Args:
            device (str): the device name

        Returns:
            float: seconds
        """
        declared = (self.devices.get(device) or {}).get("min_transition_seconds")
        return float(self._default_transition if declared is None else declared)

    def hold(self, last_output=None) -> None:
        """Stop the integral accumulating while the control is not actuating.

        A control outside its active period, or gated off by ``enable_when``, is not
        controlling anything - so an error term measured against a setpoint nobody is
        chasing is not information, and integrating it means resuming with a demand built
        from a period when the actuators were deliberately idle.

        Args:
            last_output (float or None): the demand to resume from, or None to resume from
                where the loop left off
        """
        self.pid.set_auto_mode(False, last_output=last_output)

    def resume(self, last_output=None) -> None:
        """Start controlling again without a step change.

        simple-pid's ``set_auto_mode(True, last_output=...)`` back-computes the integral so
        the first demand after resuming matches ``last_output`` rather than jumping from
        zero, which is what stops a heater slamming on at the start of every active period.

        Args:
            last_output (float or None): the demand to resume from
        """
        self.pid.set_auto_mode(True, last_output=last_output)


def _finite(value, where):
    """Return a rule's value, refusing one the PID cannot survive.

    The rule language produces non-finite numbers from finite inputs - ``1e400`` is inf and
    ``1e400 - 1e400`` is nan - so this is reachable without anything upstream being wrong.

    Checked before the PID sees it because the damage is permanent rather than momentary:
    simple-pid folds the value into the integral, and a nan integral returns nan for every
    later cycle however good the data becomes. Measured - one nan reading, then three clean
    ones, all nan.

    Args:
        value (object): whatever the rule evaluated to
        where (str): the rule slot, for the message

    Returns:
        float: the value

    Raises:
        RuleEvaluationError: where the value is not a finite number
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RuleEvaluationError(f"{where} evaluated to {value!r}, which the loop cannot use this cycle")
    return float(value)


def _rule(text, names, where, optional=False):
    """Parse one of a control's rule slots.

    Args:
        text (str or None): the expression
        names (tuple): the input and parameter names a rule may reference
        where (str): the slot, for the message
        optional (bool): whether an absent slot is acceptable

    Returns:
        Rule or None: the parsed rule, None for an absent optional slot

    Raises:
        ConfigError: where a required slot is missing, or the expression does not parse
    """
    if text is None:
        if optional:
            return None
        raise ConfigError(f"a control document needs {where}")
    return parse_rule(str(text), allowed_names=names)


def simulate(controller, plant, cycles, bindings_for, dt):
    """Run a controller against a plant for a number of cycles, returning the trace.

    A plant is anything that accepts the level a window averaged and returns the process
    variable that resulted. That is enough to exercise the loop's actual behaviour -
    convergence, overshoot, what a cap does to it - without a room, a heater or two hours.

    Args:
        controller (Controller): the loop under test
        plant (callable): level applied -> the resulting process variable
        cycles (int): how many windows to run
        bindings_for (callable): process variable -> the bindings for that cycle
        dt (float): seconds per cycle, handed to the PID rather than measured

    Returns:
        list: one ``(process_variable, level)`` pair per cycle
    """
    trace = []
    process_variable = plant(None)
    for _ in range(cycles):
        plan = controller.step(bindings_for(process_variable), dt=dt)
        # What the window actually delivers is its time-weighted average level, which is the
        # point of time-proportioning: the plant sees 1237, not an hour of 750 followed by
        # an hour of 1500.
        total = sum(dwell.seconds for dwell in plan)
        level = sum(dwell.stage.level * dwell.seconds for dwell in plan) / total if total else 0.0
        trace.append((process_variable, level))
        process_variable = plant(level)
    return trace


def first_order_plant(start, gain, loss, ambient):
    """Return a plant whose value rises with applied level and falls towards ambient.

    Deliberately crude: a room is not first-order and this is not trying to be one. It is
    enough to show the loop converging, and to make overshoot and oscillation visible if a
    change introduces them.

    Args:
        start (float): the initial process variable
        gain (float): units of process variable per unit of level per cycle
        loss (float): the fraction of the gap to ambient lost each cycle
        ambient (float): what it decays towards

    Returns:
        callable: level applied -> the resulting process variable, None to read it
    """
    state = {"value": float(start)}

    def plant(level):
        if level is None:
            return state["value"]
        if not math.isfinite(level):
            raise ValueError(f"a plant cannot be driven with {level!r}")
        state["value"] += level * gain - (state["value"] - ambient) * loss
        return state["value"]

    return plant
