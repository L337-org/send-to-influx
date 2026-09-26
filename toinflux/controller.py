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
import time

from simple_pid import PID

from toinflux.exceptions import ConfigError
from toinflux.general import render_values
from toinflux.controls import DEFAULT_CYCLE_SECONDS, parameter_devices, rule_names
from toinflux.rules import RuleEvaluationError, parse_rule
from toinflux.staging import build_ladder, cap_ladder, plan_window, reachable_ladder

#: How long a hold may last and still be resumed from. A fail-safe lasts one cycle and a
#: gated-off control lasts until its window reopens, which is the distinction that matters: a
#: blip must not cost the loop what it learned, and last night's integral must not be handed
#: to this evening. Generous against the first and far short of the second.
RESUMABLE_HOLD_SECONDS = 1800.0


#: What a control document says that does *not* change what the loop's memory means. The
#: fingerprint is everything else, so a key added to the format is covered without anybody
#: remembering to add it - which is the mistake this list exists to stop repeating.
#:
#: `name` and `enabled` are bookkeeping. `timezone` and `active_period` say when a control
#: acts, `enable_when` says whether, and `safe_state` says what happens when it does not -
#: none of which changes the units or the range of an error accumulated while it was acting.
_LOOP_IRRELEVANT_KEYS = frozenset({"name", "enabled", "timezone", "enable_when", "safe_state", "active_period"})

#: The same, for keys that appear inside sections. `max_age` decides how fresh a reading must
#: be rather than what it measures, and a transition minimum is a promise about wear rather
#: than about scale. Both are edited in ordinary tuning, and discarding a hard-won integral for
#: either would be the over-reaction this digest is deliberately narrow to avoid.
_LOOP_IRRELEVANT_SETTINGS = frozenset({"max_age", "min_transition_seconds"})


def _loop_material(document):
    """Return the document reduced to what the loop's memory is meaningful against.

    Args:
        document (dict): the control document

    Returns:
        dict: the same document without the parts that do not bear on the memory
    """

    def pruned(value):
        if isinstance(value, dict):
            return {k: pruned(v) for k, v in value.items() if k not in _LOOP_IRRELEVANT_SETTINGS}
        if isinstance(value, list):
            return [pruned(item) for item in value]
        return value

    return {key: pruned(value) for key, value in document.items() if key not in _LOOP_IRRELEVANT_KEYS}


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
            time_fn (callable or None): the clock simple-pid measures intervals with, and
                which this measures a hold against, injectable so a two-hour hold is a test
                rather than a wait

        Raises:
            ConfigError: where a rule does not parse, or the document names no stages
        """
        output = document.get("output") or {}
        self.ladder = build_ladder(output.get("stages") or [])
        self.devices = document.get("devices") or {}
        # Kept whole for the fingerprint, which is the document less a named few rather than a
        # list of whatever somebody remembered mattered. See `fingerprint`.
        self._document = document
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
        # What the loop knew when it was last held, so a momentary fail-safe does not cost it.
        # Monotonic, and the same clock simple-pid is given where a test supplies one: this
        # measures a gap inside one process, where a wall clock could step and a restart is
        # somebody else's problem - `TransitionLog` covers that, in epoch seconds, on disk.
        self._clock = time_fn or time.monotonic
        self._held: "dict | None" = None
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
        # Kept before the freeze narrows it: `plan_window` picks rungs from the reachable
        # ladder and reads a driven device's value off this one, because a frozen device
        # constrains what may be switched rather than what another device should hold.
        curve = ladder
        ladder = reachable_ladder(ladder, frozen, states or {})
        self.pid.setpoint = setpoint
        if limits is not None:
            # The limits follow the cap. Left at the full ladder's range, the integral would
            # keep accumulating towards a level the cap has just forbidden, and every cycle
            # spent capped would be paid back as overshoot the moment it lifted.
            self.pid.output_limits = limits
        demand = self.pid(process_variable, dt=dt)
        if demand is None:
            # Held, and stepped anyway. simple-pid answers with its last output while manual,
            # which is None where it has never produced one - and that reaches `plan_window`
            # as a demand it cannot place on a ladder, so the complaint arrived from staging
            # and named the rungs. It is a caller fault rather than a data one: `resume_from`
            # restores into a hold and the cycle releases it before stepping, so anything
            # reaching here has skipped that.
            raise ConfigError(
                "the controller was stepped while it was held, so it has no demand to give - "
                "call resume() before step() after a hold or a resume_from()"
            )
        plan = plan_window(ladder, demand, self.cycle_seconds, self.min_transition_for, self.driven, curve=curve)
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

        **Everything the document says, less a named few.** This used to list what mattered,
        and that list was wrong eight times: the ladder's scale, then the cap, then the input
        and setpoint rules, then what those inputs read, then the adjustable parameters, then
        which actuator each device is. Each was found by somebody noticing one more thing an
        integral is measured against, which is not a process that ends.

        Inverted, the failure mode inverts with it. Forgetting to exclude something costs a
        control its settling time after a harmless edit, which is visible and recoverable.
        Forgetting to include something commands hardware from a memory that is no longer
        about it, which is neither. `_LOOP_IRRELEVANT` is the whole of what is left out, and
        each entry says why.

        The gains, the ladder, the window, what each device is driven by, and the cap. An
        integral is in the output's units and is bounded by the ladder's range, so the same
        number means one thing under one tuning and something else under another - and the
        commonest restart a control sees is the one the supervisor performs because its
        document was edited.

        **The adjustable parameters belong here too**, and are the easiest to overlook: a
        setpoint rule reading `target` is unchanged text when `target` moves from 1000 to 800,
        while the integral it earned is the accumulated error against the old number - and
        editing a target is the commonest change anybody makes to a control.

        **The device bindings and the cap belong here for the same reason the ladder does**,
        and were missing. Two lamps take the same `brightness_pct` ladder and are different
        plants, so what a device *points at* matters as much as what it is driven by - the
        same argument as for the inputs, which is where it was first applied and then not
        carried across. A device moved from `brightness_pct` to `color_temp_k` keeps its rung
        numbers while every one of them comes to mean something else, and a changed
        `max_level` changes the range the integral is clamped into - both left this digest
        identical, so a loop earned against one scale was handed back for another.

        Deliberately not the whole document: a changed `enable_when`, safe state or input
        max_age does not make the accumulated error wrong, and discarding it for those would
        throw away the settling time this exists to save.

        Returns:
            str: a short digest, stable across processes and Python versions
        """
        material = json.dumps(_loop_material(self._document), sort_keys=True, default=str)
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

    def resume_from(self, state, age=0.0) -> None:
        """Put back a memory this loop saved before it was restarted.

        The integral is clamped to the output limits on the way in, because the ladder it was
        earned against is the ladder it is being restored into - the fingerprint says so - but
        a file is a file and a number that escaped the range would command past the top rung.

        ``_last_time`` is deliberately not restored. It belongs to this process's clock, and a
        timestamp from a previous one would make the first interval either enormous or
        negative depending on which way the clock had moved.

        **Restored into a hold, not into a running loop**, and dated from when the state was
        written rather than from now. Two things follow from that, both of them faults this
        had:

        A restart that lands while the control is not acting - outside its window, or with
        `enable_when` false - is never held by anything, and simple-pid only resets on a real
        manual-to-automatic change, so `resume` at the next opening did nothing and this
        integral was used hours later with no age check at all.

        And the age it is then judged against has already been running. `loop_state` will hand
        back state up to `RESUMABLE_CYCLES` cycles old, which is longer than
        `RESUMABLE_HOLD_SECONDS` once the cycle passes six minutes - so a restart could carry
        an integral that a process running the whole time would have dropped. Backdating the
        hold by the age closes that: a restart buys no more leniency than staying up would.

        The age is passed in rather than read from the state, because the two clocks are not
        the same one and must not be compared. The log is written with epoch time, so that it
        survives a restart at all; a hold is measured with a monotonic clock, so that it is
        not confused by one. Only the elapsed seconds carry across.

        Args:
            state (dict): what :meth:`capture` produced
            age (float): how long ago it was written, in seconds
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
        if not isinstance(age, (int, float)) or not math.isfinite(age) or age < 0:
            # An unusable age means an unusable lease, and the safe reading of that is that it
            # has all been spent: a clock that went backwards must not buy extra time.
            age = RESUMABLE_HOLD_SECONDS + 1
        self._held = {"integral": float(integral), "at": self._clock() - age}
        self.pid.set_auto_mode(False)

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

        **What the loop knew is remembered as it goes.** `resume` decides whether to give it
        back, and cannot do so if nothing kept it: `set_auto_mode(False)` leaves the integral
        in place but `set_auto_mode(True)` resets it, so by the time anybody wants it, it has
        gone.
        """
        # **Only the first hold of a run records anything.** `_fail_safe` calls this on every
        # failing cycle, so recording each time made the age in `_held_integral` measure the
        # gap since the *last* failure rather than how long the loop has been held - always
        # about one cycle, however long the outage. A six-hour outage would have handed back
        # a six-hour-old integral, which is exactly what `RESUMABLE_HOLD_SECONDS` exists to
        # refuse. `resume` clears this, so the next hold after one starts the clock again.
        if self._held is None:
            self._held = {"integral": getattr(self.pid, "_integral", 0.0), "at": self._clock()}
        # Outside the guard, and idempotent while already manual: the mode is the thing that
        # must be true after every call, whether or not this one was the first.
        self.pid.set_auto_mode(False)

    def resume(self, last_output=None) -> None:
        """Start controlling again, keeping what was learned if the pause was brief.

        simple-pid's ``set_auto_mode(True)`` resets the controller, so resuming used to start
        the integral at zero however short the gap. That is right for an active period, where
        eighteen hours have passed and what the room was doing last night says nothing about
        this evening. It is wrong for the case that actually happens: one cycle that could not
        read its input calls ``hold`` through the fail-safe, and the next healthy cycle
        undid everything the loop had learned. A control holding a lamp at full output dropped
        to 64% on a single flaky read, with the error unchanged.

        So the gap decides, the same way it decides whether a restart may resume - see
        `RESUMABLE_HOLD_SECONDS`. Brief means carry on; long means the world has moved and the
        loop should look at it rather than at what it remembered.

        Idempotent while already automatic: ``set_auto_mode`` only resets when the mode
        actually changes, which is what lets the cycle call this unconditionally.

        Args:
            last_output (float or None): the demand to resume from, overriding what was held.
                None means use what was held, or zero where the hold was too long ago
        """
        if last_output is None:
            last_output = self._held_integral()
        self.pid.set_auto_mode(True, last_output=last_output)
        self._held = None

    def _held_integral(self):
        """Return the integral worth carrying across a brief hold, or None.

        Returns:
            float or None: what to resume the I-term from, or None to start it at zero
        """
        held = self._held
        if not held:
            return None
        elapsed = self._clock() - held["at"]
        if not 0 <= elapsed <= RESUMABLE_HOLD_SECONDS:
            return None
        integral = held["integral"]
        return float(integral) if isinstance(integral, (int, float)) and math.isfinite(integral) else None


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
