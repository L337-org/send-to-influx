"""One control, as a process: gather, decide, command, wait.

The loop a control runs, assembled from the pieces the earlier slices built. It reads its
declared inputs, asks the gate whether it should be acting at all, runs the PID, splits the
cycle window between two rungs of the stage ladder, and commands the devices.

**The order is the argument.** Nothing touches the PID until every value it would be fed
has been read and found finite, because one non-finite reading poisons the integral
permanently. Nothing reads a sensor until the gate has said the control is running at all,
because a control switched off or outside its window should not pay for a read to be told
so. And nothing is commanded that the ladder does not declare.

**A failed cycle is not a failed control.** A reading that could not be obtained, one too
old to act on, or a rule that could not be evaluated fails *this* cycle: the control applies
its safe state, says why, and tries again next time. Only a configuration fault stops the
process, because no retry fixes one.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import datetime
import logging
import math
import os
import time

import requests

from toinflux.controller import Controller
from toinflux.controls import DEFAULT_CYCLE_SECONDS, load_control, validate_control
from toinflux.exceptions import ConfigError, SourceConnectionError, ToolParamError
from toinflux.gating import DeviceGuard, Gate, commands_for
from toinflux.general import RepeatingProblem, load_settings, render_values, source_class
from toinflux.inputs import input_max_age, read_input, source_handler
from toinflux.rules import RuleEvaluationError
from toinflux.staging import build_ladder
from toinflux.transitions import TransitionLog

#: How long a cycle waits when the document names nothing.


def gather(document, settings, session, settings_file=None, now=None):
    """Return name -> value for everything a control's rules may read.

    Parameters first, then inputs, so a parameter can never quietly shadow a reading: the
    store refuses a name declared as both, and this ordering means a future relaxation of
    that fails loudly here rather than silently preferring the constant.

    Args:
        document (dict): the control document
        settings (dict): the whole parsed settings document
        session (requests.Session): the session to read through; the caller owns it
        settings_file (str or None): the settings path the process was started with
        now (float or None): the clock, for tests

    Returns:
        dict: name -> value, every reading finite. The controller checks the finished rule
        too, but that is not enough on its own and was relied on as though it were: a nan
        reaching `max(target, dew + 5)` comes out of it as a plausible number, because every
        comparison against nan is False, so the check at the far end sees nothing wrong.
        Refused here as well, where the input has a name to report it by

    Raises:
        RuleEvaluationError: where a reading is too old to act on. This cycle has failed,
            not this control: the collector may be a minute behind, and the next cycle with
            fresh data works.
        SourceConnectionError: where a reading could not be obtained at all
        ConfigError: where an input declaration or a source section is unusable
    """
    bindings = dict(document.get("parameters") or {})
    for name, spec in (document.get("inputs") or {}).items():
        reading = read_input(session, settings, spec, settings_file=settings_file, now=now)
        limit = input_max_age(spec, settings)
        if reading.age > limit:
            # Raised rather than returned, and as a cycle failure rather than a config
            # fault: acting on a value that stopped being true an hour ago is the failure
            # this bound exists to prevent, and the source may well be back next cycle.
            raise RuleEvaluationError(
                f"input {name!r} is {reading.age:.0f}s old, past the {limit:.0f}s it may be "
                f"acted on: reading {spec.get('field')!r} from {spec.get('source')!r}"
            )
        if not math.isfinite(reading.value):
            # Same shape as the staleness check above, and the same answer: this cycle has
            # failed, not this control. A nan or an infinity in the stored series is a bad
            # point rather than a bad document, and the next one written may be fine.
            #
            # Here rather than only at the controller because a nan does not survive as a nan:
            # `max` and `clamp` compare, every comparison against nan is False, and the
            # worked example's own `max(target, dew + 5)` hands back the target as though
            # nothing had happened. By the time the controller looks, there is nothing to see.
            raise RuleEvaluationError(
                f"input {name!r} read {reading.value!r} from {spec.get('field')!r} of "
                f"{spec.get('source')!r}, which is not a value a control can act on"
            )
        bindings[name] = reading.value
    return bindings


def command_devices(name, document, commands, settings_file=None, transitions=None, forced=False) -> None:
    """Put a control's devices into the states given, and note which of them moved.

    Grouped by source and instance so one handler serves every device on the same bridge,
    and closed on the way out: a handler opens a session whether or not anything uses it,
    and a control commanding two heaters every cycle would otherwise leak two sockets a
    cycle.

    **The transition log is written here and nowhere else.** Both the control process and
    the supervisor command devices, and ``min_transition_seconds`` is only kept if every one
    of those is recorded - so the record is taken at the single point they share rather than
    at each of them, where a path added later would simply not have it. That is also why the
    control's name is the first argument and not optional: there is no such thing as
    commanding a control's devices without knowing whose they are.

    Recorded after the command, so a device the far end refused is not written down as having
    moved. A partial failure across two bridges therefore records the bridge that answered
    and not the one that did not, which is the truth about what happened.

    Args:
        name (str): the control whose devices these are
        document (dict): the control document
        commands (dict): the control's own device names, to the state each should take
        settings_file (str or None): the settings path the process was started with
        transitions (TransitionLog or None): the log to write to; one is opened for this
            control when None, which is what a caller with no loop of its own wants
        forced (bool): True where this is a safe state rather than a decision the ladder
            made. A safe state overrides ``min_transition_seconds`` going in, and must not
            then hold the control off going out: a heater forced off by a transient fault
            would otherwise sit there for a whole minimum after the fault had cleared

    Raises:
        ConfigError: where a device names a source that cannot actuate anything
        SourceConnectionError: where the far end refused or could not be reached
    """
    declared = document.get("devices") or {}
    commanded: dict = {}
    targets: dict = {}
    # Opened before a single device moves, for two reasons. It is what lets the record below
    # run in a `finally` without the risk of raising there and masking the failure that got
    # us there. And an unusable name or state directory is then found before the heaters are
    # touched rather than after, which is the right order for a fault that stops us writing
    # down what we did.
    log = TransitionLog(name, settings_file) if transitions is None else transitions
    # `device_key` rather than `name`, which is the control's: this loop used to call its
    # variable `name` and shadowed the parameter added above it, so the transition log was
    # written under the last device's key instead of the control's. Caught by a test rather
    # than by review, and only because the test asked for the log by the control's name.
    for device_key, state in commands.items():
        spec = declared.get(device_key)
        if not isinstance(spec, dict):
            raise ConfigError(f"control device {device_key!r} is not declared in this control's devices section")
        missing = [field for field in ("source", "device") if not spec.get(field)]
        if missing:
            # ConfigError rather than the KeyError a bare lookup gives: the supervisor calls
            # this to make a dead control's devices safe and handles the project's own
            # types, so a KeyError from one corrupt document would escape that handler and
            # stop every other control being supervised.
            raise ConfigError(f"control device {device_key!r} declares no {render_values(missing)}")
        # The control's own key travels with the bridge-side name, because it is the one an
        # operator can act on: a fault with this declaration is fixed by editing the entry
        # they wrote, not the device name the far end knows it by.
        # The parameter travels with the target: the commanding runs in its own function so a
        # partial failure can still be recorded, and that function has no view of the document.
        targets.setdefault((spec["source"], spec.get("instance")), []).append(
            (device_key, spec["device"], state, spec.get("parameter"))
        )
    try:
        _command_each(targets, commanded, settings_file)
    finally:
        # **In a finally, because a partial failure is the dangerous case.** Two heaters on
        # two bridges, the first commanded and the second unreachable: the exception used to
        # carry past the record, so the first had moved and nothing knew when. The next cycle
        # or the next restart would then switch it again inside its minimum - the one thing
        # this log exists to prevent, arriving exactly when the far end is already misbehaving.
        if commanded:
            log.record(commanded, forced=forced)


def _command_each(targets, commanded, settings_file) -> None:
    """Command every device, noting in `commanded` each one that the far end accepted.

    Split out so the caller can record what succeeded whether this returns or raises. The
    mapping is filled in place rather than returned for the same reason: a return value is
    lost when an exception is on its way out, and what was already commanded is exactly what
    must not be.

    Args:
        targets (dict): (source, instance) -> [(control's key, far-end name, state)]
        commanded (dict): filled in with the control's key -> state, for each device the far
            end accepted
        settings_file (str or None): the settings path the process was started with

    Raises:
        ConfigError: where a device names a source that cannot actuate anything, or the far
            end cannot resolve the device the document names
        SourceConnectionError: where the far end refused or could not be reached
    """
    for (source, instance), devices in sorted(targets.items(), key=lambda item: str(item[0])):
        # Asked of the class, before a handler is built. Building one loads settings and
        # opens a session, so refusing afterwards costs a socket for a source that is about
        # to be rejected - and, worse, reports the wrong fault first: a source that cannot
        # actuate and is also not configured answers "not found in settings", which sends an
        # operator off to configure it before they find out it could never have worked.
        if not getattr(source_class(source), "MCP_ACTUATES_DEVICES", False):
            # Not MCP_WRITABLE, which says only that *some* write path exists and is
            # satisfied by a source whose write path triggers a speed test. That check
            # passed and the call below then raised AttributeError - which is not one of
            # the types the supervisor's safe-state pass handles, so one control's
            # document could end the thread supervising all of them.
            #
            # Named by the control's own device keys rather than by the instance. The
            # instance is what the grouping is keyed on, but it cannot be the fault
            # here: actuating is a property of the source, so every instance of it
            # answers the same way, and naming one would point at the wrong thing.
            raise ConfigError(
                f"control device {render_values(sorted(key for key, _device, _state, _parameter in devices))} "
                f"names source {source!r}, which cannot switch a device on and off. Name a "
                f"source that can, or remove the device from this control"
            )
        with source_handler(source, settings_file=settings_file, instance=instance) as handler:
            for key, device, state, parameter in devices:
                # A device that names a parameter is *set* rather than switched, so the value
                # travels on that keyword. The handler turns a light on implicitly when it is
                # given a brightness, and a zero is an explicit "off" rather than a dimmest
                # setting, which is what makes `unenergised` mean the same thing for both
                # kinds of device.
                if parameter and state:
                    setting = {parameter: state}
                elif parameter:
                    setting = {"on": False}
                else:
                    setting = {"on": bool(state)}
                try:
                    handler.mcp_set_device_state(device, **setting)
                except ToolParamError as exc:
                    # **A stored document is not a caller mistake.** `mcp_set_device_state`
                    # raises this for a device it cannot resolve, which is right when a model
                    # asked - the model can pick another. Here the name came from a control
                    # document, so no retry fixes it and the loop must stop: ToolParamError is
                    # not a ConfigError, so it escaped the child's own handler and arrived as
                    # a traceback, and the supervisor restarted the control with backoff for
                    # ever against a name that will never resolve.
                    #
                    # The control's own key is added because the document is what has to be
                    # edited, and the handler only knows the bridge's name for the device.
                    raise ConfigError(
                        f"control device {key!r} cannot be commanded: {exc}",
                    ) from exc
                # As commanded, not coerced: a dimmer's 40 must be recorded as 40, or the
                # log cannot tell it from the same lamp at 5 and its minimum means nothing.
                commanded[key] = state


class ControlProcess:
    """One control's loop, and everything it owns for the life of the process.

    Built once and stepped repeatedly. The PID, the gate and the device guard all carry
    state between cycles - an integral, whether it was acting last time, whether the safe
    state has been applied - so rebuilding any of them per cycle would quietly reset it.

    Carries ``document`` (the validated control), ``gate``, ``controller`` and ``ladder``,
    and ``guard`` (the startup assertion and the exit handler).
    """

    def __init__(self, name, settings_file=None, session=None):
        """Build a control from its stored document.

        Args:
            name (str): the control to run
            settings_file (str or None): the settings path the process was started with
            session (requests.Session or None): the session to read through; one is made
                and owned here when None

        Raises:
            ConfigError: where the control is missing, structurally invalid, or names
                something unusable
        """
        self.name = name
        self.settings_file = settings_file
        self.document = load_control(name, settings_file)
        self.settings = load_settings(settings_file)
        errors = validate_control(name, self.document, self.settings)
        if errors:
            raise ConfigError(f"control {name!r} is not valid:\n  " + "\n  ".join(errors))
        # Opened once and carried, not read per command: it is this process's own record of
        # what it has done, and re-reading it every cycle would cost a file read to learn
        # what it already knows.
        self.transitions = TransitionLog(name, settings_file)
        # Carried for the life of the process, because that is the span over which a fault
        # repeats: rebuilt per cycle it would report every cycle, which is the thing it is for.
        self._problems = RepeatingProblem()
        self.gate = Gate(self.document)
        self.controller = Controller(self.document)
        self.ladder = build_ladder(self.document["output"]["stages"])
        self._session = session or requests.Session()
        self._owns_session = session is None
        self._bindings = None
        self.guard = DeviceGuard(
            name,
            self.document.get("safe_state", "unenergised"),
            self.document.get("devices") or {},
            self._apply_safe,
        )

    @property
    def cycle_seconds(self):
        """Return how long one window lasts.

        Returns:
            float: seconds
        """
        return float((self.document.get("output") or {}).get("cycle_seconds", DEFAULT_CYCLE_SECONDS))

    def _apply(self, commands, forced=False) -> None:
        """Command the devices, for the guard and the fail-safe alike.

        None is an instruction rather than an omission: it is what ``commands_for`` returns
        for ``leave_unchanged``, and it means "do not touch these devices". Handled here
        because every path that can produce it comes through here - the closing edge passed
        it straight to the commander before, so a control configured to be left alone
        crashed at the exact moment its window closed.

        Args:
            commands (dict or None): device name to the state it should take, or None to
                touch nothing at all
            forced (bool): True where this is a safe state rather than a decision the ladder
                made, so ``min_transition_seconds`` governs neither this move nor the next
        """
        if commands is None:
            return
        command_devices(self.name, self.document, commands, self.settings_file, self.transitions, forced=forced)

    def _apply_safe(self, commands) -> None:
        """Command the devices into a safe state.

        Separate from :meth:`_apply` because it is what the device guard is handed, and the
        guard has no opinion about transition minimums to pass along - every command that
        reaches it is a safe state by construction. Keeping the distinction in the method
        rather than in an argument the guard would have to carry means a future caller picks
        it by choosing which one to call.

        Args:
            commands (dict or None): device name to the state it should take, or None
        """
        self._apply(commands, forced=True)

    def _gather(self):
        """Return this cycle's bindings, reading them at most once.

        The gate may ask for them and the loop needs the same values afterwards; reading
        twice would cost two round trips and could return two different answers.

        Returns:
            dict: name -> value
        """
        if self._bindings is None:
            self._bindings = gather(self.document, self.settings, self._session, settings_file=self.settings_file)
        return self._bindings

    def cycle(self, dt=None, moment=None, sleep=time.sleep):
        """Run one cycle window to its end, and return what was decided.

        The window is *spent* here rather than planned and handed back, because
        time-proportioning is the loop: a demand between two rungs means commanding the
        lower one, waiting, commanding the upper one, and waiting again. A caller that
        received a plan would have to reimplement that, and the two would drift.

        Args:
            dt (float or None): seconds since the previous cycle, for tests; None lets the
                PID measure it from the clock
            moment (datetime.datetime or None): the moment to judge the active period at;
                now, in UTC, when None
            sleep (callable): how to wait out a dwell, injectable so a fifteen-minute
                window is a test rather than a wait

        Returns:
            Decision or None: what the gate decided, so a caller can see the edges - and
            **None where the cycle failed and the control fell to its safe state**, which
            is not an edge and not a decision the gate ever made

        Raises:
            ConfigError: where the control cannot run at all - no retry fixes one
        """
        self._bindings = None
        moment = moment or datetime.datetime.now(datetime.timezone.utc)
        try:
            decision = self.gate.decide(self._gather, moment)
            if decision.edge == "closed":
                # Held, not merely stopped: an error measured against a setpoint nobody is
                # chasing is not information, and integrating it means resuming with a
                # demand built from a window the actuators were deliberately idle through.
                self.controller.hold()
                if decision.apply:
                    self._apply_safe(commands_for(decision.apply, self.document.get("devices") or {}))
                # Only now is the edge spent. If the command above raised, this is not
                # reached, the gate still believes it is acting, and the next cycle delivers
                # the same closing edge again - which is the retry.
                self.gate.closed()
                logging.info("Control %r stopped acting: %s", self.name, decision.reason)
            elif decision.edge == "opened":
                self.controller.resume()
                logging.info("Control %r resumed", self.name)
            # Said only where something was being reported, so an ordinary cycle is silent.
            self._problems.cleared("cycle", "Control %r completed a cycle again", self.name)
            if decision.actuating:
                # **Let go of any hold before stepping.** `_fail_safe` holds the controller so
                # a failed cycle does not integrate an error the loop never acted on, and
                # nothing else ever released it: the gate had not closed, so no `opened` edge
                # followed, so `resume` above never ran. One transient failure therefore left
                # the PID in manual mode returning its last demand for ever - measured at 642
                # whether the room was 5 degrees or 25, which is a heater stuck on.
                # `set_auto_mode(True)` is a no-op while already automatic, so this costs
                # nothing on the ordinary path.
                self.controller.resume()
                self._spend_window(dt, sleep)
            else:
                sleep(self.cycle_seconds)
            return decision
        except (RuleEvaluationError, SourceConnectionError) as exc:
            # This cycle, not this control. The far end may be back next time, the
            # collector may be a minute behind, and a control that stopped for ever after
            # one bad cycle is worse than one that skips it - so the devices go safe, the
            # reason is logged once here where it is handled, and the loop carries on.
            self._fail_safe(exc)
            sleep(self.cycle_seconds)
            return None

    def _spend_window(self, dt, sleep) -> None:
        """Command each rung of this window's plan in turn, waiting out its dwell.

        Args:
            dt (float or None): seconds since the previous cycle
            sleep (callable): how to wait

        Raises:
            ConfigError: where the window or the cap is unusable
        """
        bindings = self._gather()
        # Asked before the step, so the plan is built from what this window may actually do
        # rather than built and then contradicted. A device still inside its minimum keeps
        # the state it is in, and the demand is met as closely as the rungs that remain allow.
        frozen = self.transitions.frozen(self.controller.min_transition_for, tuple(self.document.get("devices") or {}))
        # The two kinds of device are held still by different means, because "do not change"
        # means different things to them. A switched one is kept where it is by planning the
        # window only from the rungs that leave it there. A driven one has no rung to be kept
        # on - it holds a number - so its minimum is honoured by commanding the value it
        # already has, which is what `min_transition_seconds` means for a device that is
        # adjusted rather than switched: how often the adjustment is made.
        driven = self.controller.driven
        demand = self.controller.step(
            bindings, dt, frozen=frozenset(frozen - set(driven)), states=self.transitions.states()
        )
        demand = self._hold(demand, frozen & set(driven))
        for dwell in demand:
            # Per rung rather than per cycle, and the states in full: "level 750" does not
            # say which heater that turned on, and the question being asked of this log is
            # always about a particular device.
            logging.debug(
                "Control %r commanding level %g for %.0fs: %s",
                self.name,
                dwell.stage.level,
                dwell.seconds,
                # !r on the key: a device name comes from the control document, an MCP client
                # can write one, and nothing constrains its characters - so an unquoted one
                # containing a newline writes its own line into the journal. The same reason
                # `_render_names` exists two modules away.
                # A driven device holds a number, and rendering it as on/off threw the value
                # away entirely: a lamp at 56% and the same lamp at 5% both logged as "on",
                # which is the one thing this line exists to tell you. The rung's own level
                # can also read low for a driven control - the window collapses onto the
                # lower rung and the value is carried in the states - so the states are the
                # answer and the controller's own line above carries the demand.
                ", ".join(
                    f"{device!r}={('on' if state else 'off') if isinstance(state, bool) else state}"
                    for device, state in sorted(dwell.stage.states.items())
                ),
            )
            self._apply(dict(dwell.stage.states))
            sleep(dwell.seconds)

    def _hold(self, plan, held):
        """Return the plan with each held device pinned to the value it already has.

        Args:
            plan (tuple): Dwell, as planned
            held (set): devices whose minimum has not elapsed, and which are driven

        Returns:
            tuple: Dwell, with those devices unchanged from their last command
        """
        if not held:
            return plan
        from types import MappingProxyType

        from toinflux.staging import Dwell, Stage

        known = self.transitions.states()
        pinned = {device: known[device] for device in held if device in known}
        if not pinned:
            return plan
        return tuple(
            Dwell(
                stage=Stage(
                    level=dwell.stage.level,
                    declared=dwell.stage.declared,
                    states=MappingProxyType({**dwell.stage.states, **pinned}),
                ),
                seconds=dwell.seconds,
            )
            for dwell in plan
        )

    def _fail_safe(self, reason) -> None:
        """Put the devices somewhere safe after a cycle that could not be completed.

        Args:
            reason (Exception): what went wrong, for the log line
        """
        # Logged here, where it is handled, rather than where it was raised: one report per
        # failure, carrying the type as well as the message because a connection failure
        # worth retrying reads identically to a permanent one without it.
        # Through the reporter rather than straight to logging: a source that stays
        # unreachable fails every cycle, and the identical ERROR every `cycle_seconds` for
        # ever says nothing after the first and buries everything else. The same file's
        # heartbeat writer already says its own failure once for exactly this reason.
        self._problems.report(
            "cycle",
            logging.ERROR,
            "Control %r could not complete a cycle, going to its safe state: %r",
            self.name,
            reason,
        )
        self.controller.hold()
        try:
            self._apply_safe(commands_for(self.guard.safe_state, self.document.get("devices") or {}))
        except (SourceConnectionError, ConfigError) as exc:
            # The one place a broad-ish catch is right: the cycle has already failed, and a
            # device that cannot be reached to be made safe is exactly what the supervisor's
            # own safe-state pass exists for.
            logging.error("Control %r could not reach its devices to make them safe: %r", self.name, exc)

    def close(self) -> None:
        """Release what this process opened."""
        if self._owns_session:
            self._session.close()


def run_control(name, settings_file=None, heartbeat=None, cycles=None, sleep=time.sleep) -> None:
    """Run one control until it is stopped, or for a fixed number of cycles.

    The process entry point. It asserts the safe state before the first cycle, beats once
    per cycle so the supervisor can tell a slow control from a dead one, and leaves the
    devices safe on the way out.

    The order at startup is deliberate: the guard is built and the safe state asserted
    *before* anything is read. A control that has just been restarted after a kill is a
    control whose devices are in an unknown state, and finding out what the temperature is
    can wait until they are not.

    Args:
        name (str): the control to run
        settings_file (str or None): the settings path the process was started with
        heartbeat (callable or None): called with no arguments after each cycle
        cycles (int or None): stop after this many cycles; None runs until signalled
        sleep (callable): how to wait out a dwell, injectable for tests

    Raises:
        ConfigError: where the control cannot run at all
    """
    control = ControlProcess(name, settings_file=settings_file)
    try:
        # The gate picks it, because only the gate knows whether this control is inside its
        # active period - and a control starting outside its window belongs in its end state
        # rather than its safe state. Clock only: nothing is read from a sensor before the
        # devices are in a known condition.
        control.guard.assert_starting_state(control.gate.starting_state(datetime.datetime.now(datetime.timezone.utc)))
        logging.info("Control %r started, cycling every %.0fs", name, control.cycle_seconds)
        completed = 0
        while cycles is None or completed < cycles:
            control.cycle(sleep=sleep)
            completed += 1
            if heartbeat is not None:
                heartbeat()
    finally:
        # The guard's own exit handler covers a signal; this covers the ordinary return and
        # anything that escaped the loop, and is once-only either way.
        control.guard.stop("the control is stopping")
        control.close()


def heartbeat_writer(descriptor):
    """Return a callable that beats once down an inherited pipe.

    A line per cycle, flushed, carrying the time it was written. The parent needs only that
    something arrived, but a timestamp makes a journal of them readable afterwards, and a
    line rather than a byte means a partial write cannot be mistaken for a beat.

    Death detection comes free from the same pipe: the write end closes when the process
    dies however it dies, and the parent's read reaches EOF. That is why this is a pipe the
    parent passed rather than stdout - **stdout is inherited by every grandchild**, so a
    child that had spawned anything would hold the pipe open after its own death and the
    parent would wait for an EOF that never came.

    Args:
        descriptor (int): the write end of the pipe, already inherited

    Returns:
        callable: beats once per call, and a no-op once the pipe has gone - which it says
        once rather than on every cycle for the life of the process
    """
    stream = os.fdopen(descriptor, "w", buffering=1)
    # A list rather than a nonlocal, so the closure can say it has given up without the
    # ceremony. Once is once: a control with a fifteen-minute cycle would otherwise log the
    # same warning four times an hour for as long as it runs.
    stopped = []

    def beat() -> None:
        """Write one beat, unless the pipe has already gone."""
        if stopped:
            return
        try:
            stream.write(f"{time.time():.3f}\n")
            stream.flush()
        except (ValueError, OSError) as exc:
            # The parent has gone or closed its end. Not fatal to the control: a control
            # with no supervisor is still holding a room at temperature, and exiting here
            # would turn "the watchdog went away" into "the heating stopped". Said once,
            # then never again - a pipe that has gone does not come back.
            stopped.append(True)
            logging.warning("Control heartbeat could not be written, carrying on without it: %r", exc)

    return beat
