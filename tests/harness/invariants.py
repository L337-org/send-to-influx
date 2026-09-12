"""The invariants, as functions over what was observed from outside.

Each one takes observations - the bridge's own record of what it was commanded, a census
of what the operating system had running - and returns the violations it found, as strings
somebody can read without the code in front of them. None of them asks the thing under test
how it thinks it behaved.

They are the same assertions in every scenario. Static and chaos differ only in the driver
that produces the faults, so an invariant that is expensive to satisfy in one is expensive
in both, and there is exactly one place to change what "correct" means.

A check that could not be made is warned about rather than passed over. A census with no
descriptor count on a platform that does not offer one reads identically to a run that
leaked none, and the difference matters.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import time
import warnings
from dataclasses import dataclass, field


@dataclass
class Report:
    """What one invariant found.

    Attributes:
        name (str): the invariant, for the failure message.
        violations (list): what was wrong, empty when nothing was.
        skipped (list): checks that could not be made, and why.
    """

    name: str
    violations: list = field(default_factory=list)
    skipped: list = field(default_factory=list)


def devices_unenergised(bridge, devices):
    """Nothing the control owns is still switched on.

    The invariant behind the startup assertion and the exit handler both, and the only one
    whose failure is measured in kilowatt-hours.

    Args:
        bridge (StubBridge): the bridge to read
        devices (iterable): the device names the control owns

    Returns:
        Report: any device left energised
    """
    energised = bridge.energised()
    return Report(
        name="no device is left energised",
        violations=[f"{name!r} is still on" for name in devices if energised.get(name)],
    )


def untouched_since(bridge, devices, since):
    """Nobody else's devices moved.

    The invariant that says a control is isolated: killing one must not perturb another's
    actuation, and the only evidence that carries any weight is the far end's record.

    Args:
        bridge (StubBridge): the bridge to read
        devices (iterable): the bystander's device names
        since (float): a ``time.monotonic()`` reading to count from

    Returns:
        Report: any command to those devices after that moment
    """
    theirs = set(devices)
    disturbed = [
        f"{command.name!r} was commanded {command.state} at +{command.at - since:.2f}s"
        for command in bridge.commanded()
        if command.name in theirs and command.at >= since
    ]
    return Report(name="a bystander control is not perturbed", violations=disturbed)


def declared_states(control):
    """Return a control's device names and every combination it may legitimately command.

    Args:
        control (dict): the control document

    Returns:
        tuple: ``(bridge_names, allowed)`` - the control's device keys mapped to the names
        the bridge knows them by, and the list of allowed state mappings
    """
    bridge_names = {key: spec["device"] for key, spec in control["devices"].items()}
    allowed = [
        {bridge_names[key]: bool(value) for key, value in stage["set"].items()} for stage in control["output"]["stages"]
    ]
    safe = {name: False for name in bridge_names.values()}
    if safe not in allowed:
        allowed.append(safe)
    return bridge_names, allowed


def states_were_declared(bridge, control, settle=2.0):
    """Every state the devices were left in is one the control declared, or the safe state.

    Commands arrive one device at a time, so a two-device transition passes through a
    combination nobody asked for. Commands within ``settle`` of each other are therefore
    read as one transition, and this is a check on what the devices *settle* into rather
    than on what they pass through. That is a real limit: a control that genuinely
    commanded a wrong combination and corrected it within the settle window would not be
    caught here. It is the price of not failing every legitimate transition, and the window
    should be shorter than any stage the ladder can hold.

    The window is measured from the **first** command of a transition rather than between
    consecutive ones, so a slow drip of commands cannot extend one transition indefinitely
    and hide a state that was held. A transition is a burst, and a burst is bounded from
    where it started.

    Args:
        bridge (StubBridge): the bridge to read
        control (dict): the control document
        settle (float): how long after its first command a transition may still be arriving

    Returns:
        Report: every settled combination that no stage declares
    """
    bridge_names, allowed = declared_states(control)
    wanted = set(bridge_names.values())
    state, violations, group_at = {}, [], None
    # This control's own commands, before anything is decided. Reading "has it settled" off
    # the next command on the *bridge* meant another control could answer for this one: a
    # foreign command inside the settle window, with nothing of ours after it, left the last
    # transition never settled and so never checked - in exactly the two-control scenario
    # the harness exists for, and confirmed by a test before this was changed.
    commands = [c for c in sorted(bridge.commanded(), key=lambda c: c.at) if c.name in wanted]
    # Offsets are reported from the first command seen. A raw time.monotonic() reading is
    # an arbitrary number of seconds since an arbitrary moment, and printing one after a
    # "+" says nothing to somebody reading the failure.
    origin = commands[0].at if commands else 0.0
    for index, command in enumerate(commands):
        if group_at is None:
            group_at = command.at
        if "on" in command.state:
            state[command.name] = bool(command.state["on"])
        settled = index + 1 == len(commands) or commands[index + 1].at - group_at > settle
        if not settled:
            continue
        group_at = None
        if set(state) == wanted and state not in allowed:
            violations.append(f"settled at {state} at +{command.at - origin:.2f}s, which no stage declares")
    return Report(name="every settled state is one the control declared", violations=violations)


def backoff_grew(starts, minimum):
    """A repeatedly-failing control backs off rather than becoming a respawn loop.

    Args:
        starts (list): the moments the control started, in order
        minimum (float): the smallest acceptable gap between two starts

    Returns:
        Report: every gap that was too short, or shorter than the one before it
    """
    violations = []
    gaps = [later - earlier for earlier, later in zip(starts, starts[1:])]
    for index, gap in enumerate(gaps):
        if gap < minimum:
            violations.append(f"restart {index + 1} came after {gap:.2f}s, inside the {minimum}s minimum")
        # Jitter is expected; a gap that collapses is not. Ten per cent of the previous gap
        # is slack for scheduling, not for a backoff that has stopped growing.
        elif index and gap < gaps[index - 1] * 0.9:
            violations.append(
                f"restart {index + 1} came after {gap:.2f}s, shorter than the previous {gaps[index - 1]:.2f}s"
            )
    return Report(name="restart backoff holds", violations=violations)


def nothing_leaked(before, after, allowance=0):
    """No process, thread or descriptor growth across the run.

    Args:
        before (Census): the census taken at the start
        after (Census): the census taken at the end
        allowance (int): growth to tolerate, for a count that is legitimately not stable

    Returns:
        Report: each count that grew, and each count that could not be compared
    """
    report = Report(name="nothing leaked across the run")
    for field_name in ("processes", "threads", "descriptors"):
        start, end = getattr(before, field_name), getattr(after, field_name)
        if start is None or end is None:
            report.skipped.append(f"{field_name} were not counted on this platform")
            continue
        if end > start + allowance:
            report.violations.append(f"{field_name} grew from {start} to {end}")
    return report


def kept_cycling(endpoint, period, tolerance=3.0, ignore_before=None, until=None):
    """The loop never stalled: the far end kept being asked, at roughly its cadence.

    Measured at the endpoint rather than from a heartbeat, because a heartbeat is the
    subject's own account of itself. A loop that stopped doing any work while continuing to
    say it was alive is exactly the failure this is looking for.

    **The silence after the last request counts too.** A loop that stalls at the end of the
    window leaves no later request to make an oversized gap with, so checking only the gaps
    between requests reports success for the one shape of stall a scenario is most likely to
    produce: kill something, then look. That was the first version of this, and it would
    have passed a run that died halfway through.

    Args:
        endpoint (StubEndpoint): the endpoint the loop talks to
        period (float): the cycle time in seconds
        tolerance (float): how many periods may pass with no request at all
        ignore_before (float or None): a ``time.monotonic()`` reading to start from
        until (float or None): the end of the window the loop was meant to be running for;
            now, when None

    Returns:
        Report: each silence that was too long
    """
    moments = [r.at for r in endpoint.requests if ignore_before is None or r.at >= ignore_before]
    ended = time.monotonic() if until is None else until
    violations = []
    for earlier, later in zip(moments, moments[1:]):
        if later - earlier > period * tolerance:
            violations.append(f"nothing was asked for {later - earlier:.2f}s, more than {tolerance} cycles")
    if moments and ended - moments[-1] > period * tolerance:
        violations.append(
            f"nothing was asked for the last {ended - moments[-1]:.2f}s of the window, " f"more than {tolerance} cycles"
        )
    if not moments:
        violations.append("the endpoint was never asked for anything at all")
    return Report(name="the loop never stalls", violations=violations)


def check(*reports) -> None:
    """Fail on any violation, and warn about every check that could not be made.

    Args:
        *reports: the reports to check

    Raises:
        AssertionError: one or more invariants were violated
    """
    for report in reports:
        for reason in report.skipped:
            # Warned rather than collected silently: a check that was not made reads
            # exactly like a check that passed, and only one of those is true.
            warnings.warn(f"invariant {report.name!r} skipped a check: {reason}", stacklevel=2)
    broken = [f"{report.name}: {violation}" for report in reports for violation in report.violations]
    assert not broken, "invariants violated:\n  " + "\n  ".join(broken)
