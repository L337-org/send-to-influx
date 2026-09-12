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
import time

import requests

from toinflux.controller import Controller
from toinflux.controls import load_control, validate_control
from toinflux.exceptions import ConfigError, SourceConnectionError
from toinflux.gating import DeviceGuard, Gate, commands_for
from toinflux.general import load_settings
from toinflux.inputs import input_max_age, read_input, source_handler
from toinflux.rules import RuleEvaluationError
from toinflux.staging import build_ladder

#: How long a cycle waits when the document names nothing.
DEFAULT_CYCLE_SECONDS = 900.0


def gather(document, settings, session, settings_file=None, now=None):
    """Return name -> value for everything a control's rules may read.

    Parameters first, then inputs, so a parameter can never quietly shadow a reading: the
    store already refuses a name declared as both, and this ordering means a future
    relaxation of that fails loudly here rather than silently preferring the constant.

    Args:
        document (dict): the control document
        settings (dict): the whole parsed settings document
        session (requests.Session): the session to read through; the caller owns it
        settings_file (str or None): the settings path the process was started with
        now (float or None): the clock, for tests

    Returns:
        dict: name -> value, every value a finite number

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
        bindings[name] = reading.value
    return bindings


def command_devices(document, commands, settings_file=None) -> None:
    """Put a control's devices into the states given.

    Grouped by source and instance so one handler serves every device on the same bridge,
    and closed on the way out: a handler opens a session whether or not anything uses it,
    and a control commanding two heaters every cycle would otherwise leak two sockets a
    cycle.

    Args:
        document (dict): the control document
        commands (dict): the control's own device names, to the state each should take
        settings_file (str or None): the settings path the process was started with

    Raises:
        ConfigError: where a device names a source that cannot actuate anything
        SourceConnectionError: where the far end refused or could not be reached
    """
    declared = document.get("devices") or {}
    targets: dict = {}
    for name, state in commands.items():
        spec = declared.get(name)
        if not isinstance(spec, dict):
            raise ConfigError(f"control device {name!r} is not declared in this control's devices section")
        targets.setdefault((spec["source"], spec.get("instance")), []).append((spec["device"], state))
    for (source, instance), devices in sorted(targets.items(), key=lambda item: str(item[0])):
        with source_handler(source, settings_file=settings_file, instance=instance) as handler:
            if not getattr(handler, "MCP_WRITABLE", False):
                raise ConfigError(f"control device source {source!r} has no write path, so a control cannot actuate it")
            for device, state in devices:
                handler.mcp_set_device_state(device, on=bool(state))


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
        errors = validate_control(name, self.document)
        if errors:
            raise ConfigError(f"control {name!r} is not valid:\n  " + "\n  ".join(errors))
        self.settings = load_settings(settings_file)
        self.gate = Gate(self.document)
        self.controller = Controller(self.document)
        self.ladder = build_ladder(self.document["output"]["stages"])
        self._session = session or requests.Session()
        self._owns_session = session is None
        self._bindings = None
        self.guard = DeviceGuard(
            name,
            self.document.get("safe_state", "unenergised"),
            tuple(self.document.get("devices") or {}),
            self._apply,
        )

    @property
    def cycle_seconds(self):
        """Return how long one window lasts.

        Returns:
            float: seconds
        """
        return float((self.document.get("output") or {}).get("cycle_seconds", DEFAULT_CYCLE_SECONDS))

    def _apply(self, commands) -> None:
        """Command the devices, for the guard and the fail-safe alike.

        Args:
            commands (dict): device name to the state it should take
        """
        command_devices(self.document, commands, self.settings_file)

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
            Decision: what the gate decided, so a caller can see the edges

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
                    self._apply(commands_for(decision.apply, tuple(self.document.get("devices") or {})))
                logging.info("Control %r stopped acting: %s", self.name, decision.reason)
            elif decision.edge == "opened":
                self.controller.resume()
                logging.info("Control %r resumed", self.name)
            if decision.actuating:
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
        demand = self.controller.step(bindings, dt)
        for dwell in demand:
            self._apply(dict(dwell.stage.states))
            sleep(dwell.seconds)

    def _fail_safe(self, reason) -> None:
        """Put the devices somewhere safe after a cycle that could not be completed.

        Args:
            reason (Exception): what went wrong, for the log line
        """
        # Logged here, where it is handled, rather than where it was raised: one report per
        # failure, carrying the type as well as the message because a connection failure
        # worth retrying reads identically to a permanent one without it.
        logging.error("Control %r could not complete a cycle, going to its safe state: %r", self.name, reason)
        self.controller.hold()
        try:
            self._apply(commands_for(self.guard.safe_state, tuple(self.document.get("devices") or {})) or {})
        except (SourceConnectionError, ConfigError) as exc:
            # The one place a broad-ish catch is right: the cycle has already failed, and a
            # device that cannot be reached to be made safe is exactly what the supervisor's
            # own safe-state pass exists for.
            logging.error("Control %r could not reach its devices to make them safe: %r", self.name, exc)

    def close(self) -> None:
        """Release what this process opened."""
        if self._owns_session:
            self._session.close()
