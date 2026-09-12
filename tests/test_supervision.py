"""The static scenarios: two real controls, one scripted fault each, invariants after.

Everything here runs real processes against the stub endpoints, and every assertion reads
either the bridge's own record of what it was commanded or the operating system's account
of what is running. Nothing asks the supervisor how it thinks it behaved.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import os
import sys
import time

import pytest

from tests.harness import census, faults, invariants
from tests.harness.bridge import plug
from tests.harness.installation import conservatory
from toinflux.exceptions import ConfigError
from toinflux.supervision import Supervisor, stall_seconds

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _refuse_to_spawn(*_args, **_kwargs):
    """Fail the way an unresolvable binary does.

    Args:
        *_args: ignored
        **_kwargs: ignored

    Raises:
        ConfigError: always
    """
    raise ConfigError("could not start 'send-to-influx': no such file")


def _quick(name, devices, **overrides):
    """Return a control with a short window, its own devices, and no active period.

    Args:
        name (str): the control's name
        devices (dict): its devices section
        **overrides: further top-level keys

    Returns:
        dict: a control document
    """
    document = conservatory(name=name, **overrides)
    document.pop("active_period", None)
    document["devices"] = devices
    document["output"] = dict(
        document["output"],
        cycle_seconds=1,
        min_transition_seconds=1,
        stages=[
            {"level": 0, "set": {key: False for key in devices}},
            {"level": 1500, "set": {key: True for key in devices}},
        ],
    )
    return document


def _two_controls(installation):
    """Write two controls that share a bridge but no devices.

    Args:
        installation (Installation): the installation to write into

    Returns:
        tuple: the two control names
    """
    installation.bridge.lights["9"] = plug("porch-heater")
    installation.write_control(_quick("conservatory", {"far": {"source": "hue", "device": "far"}}))
    installation.write_control(_quick("porch", {"porch": {"source": "hue", "device": "porch-heater"}}))
    return "conservatory", "porch"


@pytest.fixture
def supervisor(state_directory):
    """Yield a supervisor over two real controls, stopped afterwards.

    Yields:
        Supervisor: with both controls written but not yet started
    """
    installation = state_directory
    names = _two_controls(installation)

    def argv_for(name):
        """Start a control from this checkout rather than the installed console script.

        Args:
            name (str): the control to start

        Returns:
            list: the command
        """
        return [
            sys.executable,
            os.path.join(ROOT, "sendtoinflux.py"),
            "--control",
            name,
            "--settings",
            installation.settings_file,
        ]

    running = Supervisor(
        names,
        settings_file=installation.settings_file,
        argv_for=argv_for,
        # A real backoff would make every test a minute long; what matters is that it grows,
        # which has its own test against the real one.
        backoff=lambda failures: 0.05 * failures,
    )
    try:
        yield running
    finally:
        running.stop_all()


def _wait_for(supervisor, kind, name=None, seconds=30):
    """Poll until an event of this kind arrives, or fail.

    Args:
        supervisor (Supervisor): the supervisor to poll
        kind (str): the event kind to wait for
        name (str or None): the control it must concern, or any
        seconds (float): how long to wait

    Returns:
        Event: the matching event

    Raises:
        AssertionError: it never arrived
    """
    deadline = time.monotonic() + seconds
    seen = []
    while time.monotonic() < deadline:
        for event in supervisor.poll(timeout=0.2):
            seen.append(f"{event.kind}:{event.name}")
            if event.kind == kind and (name is None or event.name == name):
                return event
    # The state of every child, not just the timeout. A supervisor that stopped restarting
    # says nothing about why from the outside, and a thirty-second timeout in CI with no
    # further detail is a message that has to be reproduced before it can be read - which
    # for a timing-dependent failure on somebody else's machine is the expensive case.
    now = time.monotonic()
    state = "; ".join(
        f"{child.name}: running={child.running} failures={child.failures} "
        f"restart_in={'-' if child.restart_at is None else f'{child.restart_at - now:.2f}s'} "
        f"silent_for={now - child.last_beat:.1f}s"
        for child in supervisor.children.values()
    )
    raise AssertionError(
        f"no {kind!r} event for {name or 'any control'} within {seconds}s. "
        f"Saw: {seen or 'nothing'}. Children: {state}"
    )


class TestStartingAndWatching:
    def test_each_control_gets_its_own_process_and_beats(self, supervisor):
        supervisor.start_all()
        assert {child.process.pid for child in supervisor.children.values()} != {None}
        _wait_for(supervisor, "beat", "conservatory")
        _wait_for(supervisor, "beat", "porch")

    def test_a_stall_threshold_comes_from_the_control_s_own_window(self):
        """A control says nothing while it is spending a cycle, so a flat threshold shorter
        than the window would flag every slow control on every cycle."""
        assert stall_seconds({"output": {"cycle_seconds": 900}}) == 2700
        assert stall_seconds({"output": {"cycle_seconds": 1}}) == 30.0


class TestWhenOneDies:
    def test_the_parent_makes_the_devices_safe_itself(self, supervisor, bridge):
        """The child applies its own safe state on the way out, and a killed one did not get
        to. Commanding a device off twice is free; assuming somebody else did it is how a
        heater stays on."""
        supervisor.start_all()
        _wait_for(supervisor, "beat", "conservatory")
        bridge.lights[bridge.id_of("far")]["state"]["on"] = True
        supervisor.children["conservatory"].process.kill()
        _wait_for(supervisor, "died", "conservatory")
        assert bridge.energised()["far"] is False

    def test_it_comes_back(self, supervisor):
        supervisor.start_all()
        _wait_for(supervisor, "beat", "conservatory")
        first = supervisor.children["conservatory"].process.pid
        supervisor.children["conservatory"].process.kill()
        _wait_for(supervisor, "died", "conservatory")
        _wait_for(supervisor, "started", "conservatory")
        assert supervisor.children["conservatory"].process.pid != first

    def test_killing_one_leaves_the_other_controlling(self, supervisor, bridge):
        """Isolation, stated as what can actually be observed. The survivor keeps beating
        and keeps commanding its own device, and every state it settles into is one its own
        ladder declares.

        Not `untouched_since`: a *running* control commands its device every cycle, so
        "nobody touched it" is true only of a bystander that is idle. That invariant is for
        the idle case and this scenario is not it - the first version of this test asserted
        it anyway, passed alone and failed in a full run, which is the honest outcome for an
        assertion that was never true.

        What this cannot see is the parent making the *wrong* control safe, because
        everything-off is a rung of the survivor's ladder too. `make_safe` is tested
        directly for that below, where the question has a definite answer.
        """
        supervisor.start_all()
        _wait_for(supervisor, "beat", "conservatory")
        _wait_for(supervisor, "beat", "porch")
        supervisor.children["conservatory"].process.kill()
        _wait_for(supervisor, "died", "conservatory")
        _wait_for(supervisor, "beat", "porch")
        porch = {"porch": {"source": "hue", "device": "porch-heater"}}
        report = invariants.states_were_declared(bridge, _quick("porch", porch), settle=0.5)
        assert report.violations == [], report.violations

    def test_making_one_control_safe_touches_only_its_own_devices(self, supervisor, bridge):
        """The question the scenario above cannot answer: a parent that made every control
        safe after any death would be invisible at the bridge, because everything-off is a
        rung of every ladder."""
        bridge.lights[bridge.id_of("far")]["state"]["on"] = True
        bridge.lights[bridge.id_of("porch-heater")]["state"]["on"] = True
        bridge.clear()
        supervisor.make_safe("conservatory")
        assert [command.name for command in bridge.commanded()] == ["far"]
        assert bridge.energised()["porch-heater"] is True

    def test_a_control_that_stops_beating_is_killed_and_restarted(self, supervisor):
        """SIGSTOP: alive by every cheap test, holding its file descriptors, saying nothing.
        The case a heartbeat exists for."""
        supervisor.start_all()
        _wait_for(supervisor, "beat", "conservatory")
        child = supervisor.children["conservatory"]
        child.stall_seconds = 0.5
        with faults.stopped(child.process):
            event = _wait_for(supervisor, "stalled", "conservatory")
        assert "no heartbeat" in event.detail
        _wait_for(supervisor, "started", "conservatory")


class TestTheRunAsAWhole:
    def test_nothing_leaks_across_a_kill_and_restart(self, supervisor):
        """A supervisor that leaked a descriptor or a zombie per restart would report itself
        healthy throughout, and the only place the truth exists is the kernel's accounting."""
        supervisor.start_all()
        _wait_for(supervisor, "beat", "conservatory")
        before = census.take(os.getpid())
        for _ in range(3):
            supervisor.children["conservatory"].process.kill()
            _wait_for(supervisor, "died", "conservatory")
            _wait_for(supervisor, "started", "conservatory")
            _wait_for(supervisor, "beat", "conservatory")
        invariants.check(invariants.nothing_leaked(before, census.take(os.getpid())))

    def test_stopping_leaves_nothing_running_and_nothing_energised(self, supervisor, bridge):
        supervisor.start_all()
        _wait_for(supervisor, "beat", "conservatory")
        _wait_for(supervisor, "beat", "porch")
        supervisor.stop_all()
        assert all(child.process is None for child in supervisor.children.values())
        invariants.check(invariants.devices_unenergised(bridge, ["far", "porch-heater"]))


class TestTheBackoffItself:
    def test_it_grows_and_never_collapses(self):
        """Measured against the real backoff rather than the fast one the scenarios inject,
        because the property being claimed is about the real one."""
        from toinflux.supervision import _default_backoff

        starts, moment = [], 0.0
        for failures in range(1, 6):
            moment += _default_backoff(failures)
            starts.append(moment)
        invariants.check(invariants.backoff_grew(starts, minimum=5))


class TestTheRestartDecisionItself:
    """The restart logic on its own, with no processes and a clock the test owns.

    The scenarios above prove the whole thing works; this proves *why*, deterministically,
    and is where a restart that quietly never fires shows up as a failure rather than as a
    thirty-second timeout in CI.
    """

    class _Clock:
        """A monotonic clock the test advances by hand."""

        def __init__(self):
            self.now = 1000.0

        def __call__(self):
            """Return the current reading.

            Returns:
                float: the reading
            """
            return self.now

    def _supervisor(self, state_directory, clock):
        """Return a supervisor over one control that will never really be started.

        Args:
            state_directory (Installation): the installation to read controls from
            clock (callable): the clock to use

        Returns:
            Supervisor: ready to have its children driven by hand
        """
        _two_controls(state_directory)
        return Supervisor(
            ["conservatory"],
            settings_file=state_directory.settings_file,
            argv_for=lambda name: ["/bin/true"],
            clock=clock,
            backoff=lambda failures: 10.0 * failures,
        )

    def test_a_control_is_restarted_once_its_backoff_has_passed(self, state_directory, monkeypatch):
        clock = self._Clock()
        supervisor = self._supervisor(state_directory, clock)
        child = supervisor.children["conservatory"]
        child.restart_at = clock.now + 10
        started = []
        monkeypatch.setattr(supervisor, "start", lambda name: started.append(name))

        supervisor.poll(timeout=0)
        assert started == [], "restarted before its backoff had passed"
        clock.now += 10
        supervisor.poll(timeout=0)
        assert started == ["conservatory"]

    def test_a_control_that_will_not_start_does_not_take_the_others_down(self, state_directory, monkeypatch):
        """One control that cannot be started is one control's problem. Letting the failure
        out of poll() would end the loop and with it every other control's supervision -
        one unstartable control taking the heating down with it."""
        clock = self._Clock()
        supervisor = self._supervisor(state_directory, clock)
        child = supervisor.children["conservatory"]
        child.restart_at = clock.now

        def refuse(name):
            """Fail to start, as an unresolvable binary would.

            Args:
                name (str): the control being started

            Raises:
                ConfigError: always
            """
            raise ConfigError("could not start 'send-to-influx': no such file")

        monkeypatch.setattr(supervisor, "start", refuse)
        events = supervisor.poll(timeout=0)
        assert [event.kind for event in events] == ["start-failed"]
        # And it backs off rather than spinning on the failure every pass.
        assert child.restart_at > clock.now
        assert supervisor.poll(timeout=0) == []

    def test_a_failed_start_leaks_no_descriptor(self, state_directory, monkeypatch):
        """A restart that keeps failing would otherwise leak one descriptor per attempt, and
        a supervisor out of descriptors stops being able to start anything - the failure
        arriving long after the control that caused it."""
        supervisor = self._supervisor(state_directory, self._Clock())
        handed_out = []
        real_pipe = os.pipe

        def watched_pipe():
            """Hand out a pipe and remember both ends.

            Returns:
                tuple: the read and write descriptors
            """
            pair = real_pipe()
            handed_out.append(pair)
            return pair

        monkeypatch.setattr("toinflux.supervision.os.pipe", watched_pipe)
        monkeypatch.setattr("toinflux.supervision.spawn", _refuse_to_spawn)
        with pytest.raises(ConfigError):
            supervisor.start("conservatory")
        assert handed_out, "the test did not observe a pipe being made"
        for descriptor in handed_out[0]:
            with pytest.raises(OSError):
                os.fstat(descriptor)

    def test_a_control_with_no_restart_due_is_left_alone(self, state_directory, monkeypatch):
        clock = self._Clock()
        supervisor = self._supervisor(state_directory, clock)
        started = []
        monkeypatch.setattr(supervisor, "start", lambda name: started.append(name))
        clock.now += 10_000
        supervisor.poll(timeout=0)
        assert started == [], "started a control that had not been running"
