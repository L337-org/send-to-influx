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

import hashlib
import json
import logging

import math

from simple_pid import PID

from toinflux.exceptions import ConfigError
from toinflux.general import render_values
from toinflux.controls import DEFAULT_CYCLE_SECONDS, parameter_devices, rule_names
from toinflux.rules import RuleEvaluationError, parse_rule
from toinflux.staging import build_ladder, cap_ladder, plan_window, reachable_ladder


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
        # The same default as `ControlProcess.cycle_seconds` and `stall_seconds`, because
        # `cycle_seconds` is optional and three readers must not disagree about what an
        # omitted one means. Left as None here, a document that passed --check-config raised
        # ConfigError on its first cycle - and a ConfigError is how a control says "no retry
        # will help", so it died permanently on a document it had just been told was fine.
        self.cycle_seconds = output.get("cycle_seconds", DEFAULT_CYCLE_SECONDS)
        self._default_transition = output.get("min_transition_seconds", 0)
        # Worked out once: which devices are set to a value rather than switched. The planner
        # needs it every cycle and the answer cannot change without a new document.
        self.driven = parameter_devices(self.devices)
        names = rule_names(document)
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

    def step(self, bindings, dt=None, frozen=frozenset(), states=None):
        """Run one cycle and return how the window should be spent.

        Args:
            bindings (dict): name -> value for every input and parameter the rules read
            dt (float or None): seconds since the previous cycle, for tests; None lets
                simple-pid measure it from the clock. Must be a positive finite number.
            frozen (frozenset): devices whose ``min_transition_seconds`` has not elapsed, so
                the window may only use rungs that leave them where they are
            states (dict or None): device name to the state it is currently in, which is what
                "leave them where they are" is measured against

        Returns:
            tuple: Dwell, filling one cycle window

        Raises:
            RuleEvaluationError: where a rule could not produce a usable value this cycle.
                The controller is left untouched, so the next cycle with good data works.
            ConfigError: where the cycle window or the cap is unusable
        """
        # dt first, because it reaches the PID too. A nan poisons it exactly as a nan reading
        # does - measured, and the next healthy cycle still returns nan - while zero or a
        # negative raises simple-pid's own ValueError, a bare built-in crossing this module's
        # boundary. In production dt is measured from the clock, so an unusable one means the
        # clock moved oddly: this cycle, not this control, is what has failed.
        if dt is not None:
            _finite(dt, "the interval since the last cycle")
            if dt <= 0:
                raise RuleEvaluationError(f"the interval since the last cycle came out as {dt!r} seconds")
        # Everything else is evaluated and checked before the PID is touched at all, because one
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
        # After the cap and deliberately *not* reflected in `limits` above. A cap is a
        # standing instruction and the integral should stop winding towards what it forbids;
        # a frozen device is a wait of a window or two, and narrowing the limits for it would
        # have the PID forget the demand it is part way through building and pay it back as
        # overshoot the moment the device came free.
        ladder = reachable_ladder(ladder, frozen, states or {})
        self.pid.setpoint = setpoint
        if limits is not None:
            # The limits follow the cap. Left at the full ladder's range, the integral would
            # keep accumulating towards a level the cap has just forbidden, and every cycle
            # spent capped would be paid back as overshoot the moment it lifted.
            self.pid.output_limits = limits
        demand = self.pid(process_variable, dt=dt)
        plan = plan_window(ladder, demand, self.cycle_seconds, self.min_transition_for, self.driven)
        # **What the loop decided, once per cycle, at DEBUG.** Nothing in this subsystem said
        # anything during a healthy cycle: a control holding the wrong temperature produced a
        # temperature curve and no record of what it was thinking, so tuning it meant guessing
        # at kp from the outside. The terms are the ones a tuning argument is actually had in -
        # what it read, what it was chasing, what it asked for, and what the ladder could give
        # it - and the rungs are named because a demand that cannot be reached looks identical
        # to one that was met until you can see which rung was chosen.
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            logging.debug(
                "input=%.3f setpoint=%.3f demand=%.1f (p=%.1f i=%.1f d=%.1f) plan=%s%s",
                process_variable,
                setpoint,
                demand,
                *self.pid.components,
                ", ".join(f"level {dwell.stage.level:g} for {dwell.seconds:.0f}s" for dwell in plan),
                f", held={render_values(sorted(frozen))}" if frozen else "",
            )
        return plan

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

    @property
    def fingerprint(self):
        """Return what this controller's memory is only meaningful against.

        The gains, the ladder and the window. An integral is in the output's units and is
        bounded by the ladder's range, so the same number means one thing under one tuning and
        something else under another - and the commonest restart a control sees is the one the
        supervisor performs because its document was edited.

        Deliberately not the whole document: a changed `enable_when`, safe state or input
        max_age does not make the accumulated error wrong, and discarding it for those would
        throw away the settling time this exists to save.

        Returns:
            str: a short digest, stable across processes and Python versions
        """
        material = json.dumps(
            {
                "kp": self.pid.Kp,
                "ki": self.pid.Ki,
                "kd": self.pid.Kd,
                "cycle": self.cycle_seconds,
                "ladder": [(stage.level, sorted(stage.states.items())) for stage in self.ladder],
            },
            sort_keys=True,
            default=str,
        )
        # sha256 rather than hash(): the built-in is salted per process, so a fingerprint
        # written by one control would never match the one that read it back.
        return hashlib.sha256(material.encode()).hexdigest()[:16]

    def capture(self):
        """Return the loop's own memory, as plain numbers a file can hold.

        The integral is the whole point: it is what takes a slow plant an hour to rebuild and
        the only part a restart genuinely loses. The last input and error come with it so a
        derivative term is continuous too, though every shipped example runs kd at zero.

        Returns:
            dict: the state, or empty where the PID has nothing worth keeping
        """
        integral = getattr(self.pid, "_integral", None)
        if not isinstance(integral, (int, float)) or not math.isfinite(integral):
            return {}
        state = {"integral": float(integral)}
        for name in ("_last_input", "_last_error"):
            value = getattr(self.pid, name, None)
            if isinstance(value, (int, float)) and math.isfinite(value):
                state[name.lstrip("_")] = float(value)
        return state

    def resume_from(self, state) -> None:
        """Put back a memory this loop saved before it was restarted.

        The integral is clamped to the output limits on the way in, because the ladder it was
        earned against is the ladder it is being restored into - the fingerprint says so - but
        a file is a file and a number that escaped the range would command past the top rung.

        ``_last_time`` is deliberately not restored. It belongs to this process's clock, and a
        timestamp from a previous one would make the first interval either enormous or
        negative depending on which way the clock had moved.

        Args:
            state (dict): what :meth:`capture` produced
        """
        integral = state.get("integral")
        if not isinstance(integral, (int, float)) or not math.isfinite(integral):
            return
        low, high = self.pid.output_limits
        if low is not None:
            integral = max(low, integral)
        if high is not None:
            integral = min(high, integral)
        self.pid._integral = float(integral)
        for name in ("last_input", "last_error"):
            value = state.get(name)
            if isinstance(value, (int, float)) and math.isfinite(value):
                setattr(self.pid, f"_{name}", float(value))

    def hold(self) -> None:
        """Stop the integral accumulating while the control is not actuating.

        A control outside its active period, or gated off by ``enable_when``, is not
        controlling anything - so an error term measured against a setpoint nobody is
        chasing is not information, and integrating it means resuming with a demand built
        from a period when the actuators were deliberately idle.

        **No ``last_output`` here, deliberately.** This used to take one and pass it to
        ``set_auto_mode(False, last_output=...)``, copying :meth:`resume`'s wording - but
        simple-pid reads that argument only in the branch that *enables* the controller
        (verified against 2.0.1's ``set_auto_mode``), so disabling with one silently
        discarded it. A caller that passed a demand expecting the next ``resume`` to pick up
        from it would have got an unannounced actuation level instead, with nothing logged.
        The value belongs to :meth:`resume`, which is where the library reads it.
        """
        self.pid.set_auto_mode(False)

    def resume(self, last_output=None) -> None:
        """Start controlling again.

        simple-pid's ``set_auto_mode(True, last_output=...)`` back-computes the integral so
        the first demand after resuming matches ``last_output``. **No caller passes one**, so
        in practice the integral restarts at zero and the loop rebuilds it over a few cycles,
        which is the same cost a restart already carries. The parameter is kept because the
        continuity it buys is a real option, not because anything takes it today - see the
        note on the design record before wiring it up, since it changes what a heater does at
        the start of every active period.

        Idempotent while already automatic: ``set_auto_mode`` only resets when the mode
        actually changes, which is what lets the cycle call this unconditionally.

        Args:
            last_output (float or None): the demand to resume from, or None to restart the
                integral at zero
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
