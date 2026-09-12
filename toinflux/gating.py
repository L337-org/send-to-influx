"""Whether a control acts this cycle, and what its devices do when it stops.

Three conditions, one answer. A control actuates when it is ``enabled``, **and** inside its
active period, **and** its ``enable_when`` rule holds. They are separate settings because
they answer different questions - keep a ruleset without running it, run it only at night,
run it only when it is cold - and one combined condition because a device does not care
which of them closed.

**Every falling edge applies a state and holds.** That is the whole point of unifying them:
one edge handler rather than three, so "what happens when this stops" has a single answer
and a single place to be wrong.

The safe state is separate from the end state, and the distinction is not fussiness. A
control that wants to be left alone when something breaks may well want to be switched off
at dawn: one is about failure, the other about a schedule, and a device wired to a
contactor can reasonably want opposite things from them.

**The same safe state also governs the two ends of the process**, through
:class:`DeviceGuard`: asserted before the first cycle and applied again on the way out.
The startup half is the one that has to exist, because the exit half cannot run after a
SIGKILL, an OOM kill or a power cut.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT"

import atexit
import logging
import math
from dataclasses import dataclass

from toinflux.controls import BUILT_IN_SAFE_STATES, SAFE_STATE_LEAVE_UNCHANGED, SAFE_STATE_UNENERGISED
from toinflux.exceptions import ConfigError
from toinflux.rules import RuleEvaluationError, parse_rule
from toinflux.schedule import is_inside, parse_active_period

#: What a falling edge or a failure does to the devices, when the answer is not "nothing".
UNENERGISED = SAFE_STATE_UNENERGISED
LEAVE_UNCHANGED = SAFE_STATE_LEAVE_UNCHANGED


@dataclass(frozen=True)
class Decision:
    """What a control should do this cycle.

    Attributes:
        actuating (bool): whether the loop should run and command a stage.
        reason (str or None): why not, when not - named so an operator reading the journal
            knows which of the three conditions closed rather than only that one did.
        edge (str or None): ``"opened"`` or ``"closed"`` where this cycle changed the
            answer, None where it did not. A control spends almost every cycle unchanged,
            so acting on the level rather than the edge would re-command the devices
            continuously.
        apply (str or None): the state to put the devices in, set only on a closing edge.
    """

    actuating: bool
    reason: "str | None" = None
    edge: "str | None" = None
    apply: "str | None" = None


class Gate:
    """Tracks whether a control is acting, and reports the moments that change.

    Holds one bit of state between cycles - whether it was acting last time - because an
    edge cannot be computed from the present alone.
    """

    def __init__(self, document):
        """Build a gate from a validated control document.

        Args:
            document (dict): the control document

        Raises:
            ConfigError: where the period, the safe states or the ``enable_when`` rule are
                unusable
        """
        self.enabled = document.get("enabled", True)
        if not isinstance(self.enabled, bool):
            raise ConfigError(f"enabled must be true or false, got {self.enabled!r}")
        self.period = parse_active_period(document)
        self.safe_state = document.get("safe_state", UNENERGISED)
        if self.safe_state not in BUILT_IN_SAFE_STATES:
            raise ConfigError(f"safe_state must be one of {BUILT_IN_SAFE_STATES}, got {self.safe_state!r}")
        names = tuple(document.get("inputs") or ()) + tuple(document.get("parameters") or ())
        text = document.get("enable_when")
        self._enable_when = None if text is None else parse_rule(str(text), allowed_names=names)
        # None rather than False, so the first cycle is not read as an edge. A control
        # starting up has not *become* inactive, and commanding the safe state because of a
        # transition that did not happen would be indistinguishable from one that did.
        self._acting = None

    def decide(self, bindings, moment):
        """Return what this cycle should do, and whether that is a change.

        Args:
            bindings (dict): name -> value for the rules
            moment (datetime.datetime): an aware moment, for the active period

        Returns:
            Decision: the answer for this cycle

        Raises:
            RuleEvaluationError: where ``enable_when`` could not be evaluated this cycle, or
                produced something that is not an answer
            ConfigError: where the moment is naive
        """
        acting, reason = self._assess(bindings, moment)
        was = self._acting
        self._acting = acting
        if was is None or was == acting:
            return Decision(actuating=acting, reason=reason)
        if acting:
            return Decision(actuating=True, edge="opened")
        return Decision(actuating=False, reason=reason, edge="closed", apply=self.closing_state())

    def _assess(self, bindings, moment):
        """Return whether the three conditions all hold, and which one did not.

        Checked in the order an operator would: a control switched off is not asked about
        the clock, and a control outside its window is not asked about the weather, so the
        reason names the outermost reason rather than an inner one that happens to be true
        as well.

        Args:
            bindings (dict): name -> value for the rules
            moment (datetime.datetime): an aware moment

        Returns:
            tuple: ``(acting, reason)``, the reason None when acting
        """
        if not self.enabled:
            return False, "the control is disabled"
        if not is_inside(self.period, moment):
            return False, "outside the active period"
        if self._enable_when is not None and not _holds(self._enable_when, bindings):
            return False, f"enable_when is false: {self._enable_when.source}"
        return True, None

    def closing_state(self):
        """Return the state the devices take when the control stops acting.

        The active period's ``end_state`` where there is a period, and ``safe_state``
        otherwise. A control with no period has no "end of the window", so a falling edge
        from ``enable_when`` is the nearest thing it has to failing.

        Returns:
            str: one of the built-in safe states
        """
        return self.period.end_state if self.period is not None else self.safe_state


def _holds(rule, bindings):
    """Whether a gate rule is true this cycle.

    The value has to be checked before it is read as a truth, and the direction of the
    failure is why. `not nan` is False, so a gate evaluating to nan would read as **true**
    and let the control actuate - a safety gate answering "yes" because its answer could not
    be computed. The rule language produces nan from finite input, so this is reachable.

    Raised rather than treated as false, because "the gate says no" and "the gate could not
    be evaluated" want different responses from the caller: one is a normal closing edge,
    the other is a cycle that failed.

    Args:
        rule (Rule): the parsed ``enable_when``
        bindings (dict): name -> value

    Returns:
        bool: whether the gate is open

    Raises:
        RuleEvaluationError: where the rule produced something that is not a finite number
    """
    value = rule.evaluate(bindings)
    if isinstance(value, bool):
        return value
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RuleEvaluationError(f"enable_when evaluated to {value!r}, which is not an answer to act on")
    return bool(value)


def commands_for(state, devices):
    """Return what to command each device to reach a safe or end state.

    ``unenergised`` names every device explicitly rather than meaning "stage 0". A control
    whose lowest stage was mis-declared - or which has no zero stage at all - would
    otherwise energise something while trying to make itself safe, and the failure would be
    invisible until the day it mattered.

    ``leave_unchanged`` returns None rather than an empty mapping. Those are different
    instructions, and a caller that treated an empty mapping as "nothing to do" would be
    right by accident: this says "do not touch these devices", which is also what the
    startup assertion must not override.

    Args:
        state (str): one of the built-in safe states
        devices (iterable): the device names the control owns

    Returns:
        dict or None: device -> the state to command, or None to touch nothing

    Raises:
        ConfigError: where the state is not one of the built-in names
    """
    if state == LEAVE_UNCHANGED:
        return None
    if state != UNENERGISED:
        raise ConfigError(f"{state!r} is not a safe state this knows: expected one of {BUILT_IN_SAFE_STATES}")
    return {name: False for name in devices}


class DeviceGuard:
    """Puts a control's devices in a known state when its process starts and when it ends.

    Two halves of one promise, and the startup half is the one that has to exist. The
    exit half is best effort: an atexit handler does not run on SIGKILL, an OOM kill or a
    power cut, so a control that only tidied up on the way out would leave heaters running
    after any of those until somebody noticed. Asserting the safe state on the way *in*
    closes all three at once, because a process that died without cleaning up is a process
    that is about to be restarted.

    ``leave_unchanged`` opts out of both halves, which is the point of it being opt-in: it
    means "this device's state is not mine to reset", and a control that said so at 03:00
    did not mean something different at startup.
    """

    def __init__(self, name, safe_state, devices, command):
        """Prepare a guard and register its exit handler.

        Registered here rather than after a successful start, so that a failure between
        construction and the first cycle still reaches the safe state on the way out.

        Args:
            name (str): the control's name, for the log lines
            safe_state (str): one of the built-in safe states
            devices (iterable): the device names the control owns
            command (callable): applied to a device -> state mapping; whatever it raises
                is what the caller sees

        Raises:
            ConfigError: where the safe state is not one of the built-in names
        """
        self.name = name
        self.safe_state = safe_state
        self.devices = tuple(devices)
        self._command = command
        # Computed now rather than at exit: an unknown state should stop the control
        # starting, not surface as a failure on the one path that cannot do anything
        # about it.
        self._commands = commands_for(safe_state, self.devices)
        self._stopped = False
        atexit.register(self._at_exit)

    def assert_safe_state(self) -> None:
        """Command the devices into the safe state before the loop runs.

        Raises:
            Exception: whatever ``command`` raises, unwrapped - a device that is missing
                and a bridge that is unreachable want different responses, and the caller
                is the one that can tell them apart.
        """
        if self._commands is None:
            logging.warning(
                "Control %r starts with safe_state %r, so its devices keep whatever state they "
                "were left in, including after a crash or a power cut",
                self.name,
                self.safe_state,
            )
            return
        logging.info("Control %r asserting %r on %s", self.name, self.safe_state, ", ".join(self.devices) or "nothing")
        self._command(self._commands)

    def stop(self, reason) -> None:
        """Put the devices in the safe state on the way out, once.

        Once, because a second command after the normal shutdown path has already run
        would fail against a torn-down connection and log as though the safe state had not
        been applied, which is the opposite of what happened.

        Args:
            reason (str): why the control is stopping, for the log line
        """
        if self._stopped:
            return
        self._stopped = True
        # Nothing left for the exit handler to do, and an entry that has already run is an
        # entry that outlives its guard: a control reloaded in a long-lived process would
        # otherwise leave one registered per document it has ever had.
        atexit.unregister(self._at_exit)
        if self._commands is None:
            return
        logging.info("Control %r applying %r on %s: %s", self.name, self.safe_state, ", ".join(self.devices), reason)
        try:
            self._command(self._commands)
        except Exception as exc:  # noqa: BLE001 - nothing above this can handle it
            # Caught rather than raised because this is the last thing the process does:
            # an exception here becomes a traceback printed by atexit and nothing else.
            # Logged with the underlying error because a device left energised after a
            # shutdown is exactly the failure somebody will be looking for.
            logging.error("Control %r could not apply %r on exit: %s", self.name, self.safe_state, exc)

    def close(self) -> None:
        """Drop the exit handler without applying anything.

        For a guard that has been replaced rather than stopped: the devices are now some
        other guard's business, and an abandoned one would still command them at exit.
        """
        self._stopped = True
        atexit.unregister(self._at_exit)

    def _at_exit(self) -> None:
        """Apply the safe state if nothing else already has."""
        self.stop("the process is exiting")
