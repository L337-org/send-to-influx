"""Tests for one control's loop, driven against the stub endpoints.

Real HTTP both ways: the control reads its inputs from the stub InfluxDB and commands its
devices on the stub bridge, through the project's own handlers. What is checked is mostly
what the *bridge* recorded, because that is the only account of what a control did that the
control did not write.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import datetime
import logging
import os
import selectors
import signal
import subprocess
import sys

import pytest
import requests

from tests.harness import faults, invariants
from tests.harness.installation import conservatory
from toinflux.control_process import ControlProcess, command_devices, gather, heartbeat_writer
from toinflux.exceptions import ConfigError, SourceConnectionError
from toinflux.rules import RuleEvaluationError, parse_rule

# Inside the conservatory's 23:35-05:25 window, and well outside it.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NIGHT = datetime.datetime(2026, 1, 15, 2, 0, tzinfo=datetime.timezone.utc)
DAY = datetime.datetime(2026, 1, 15, 12, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture
def control(state_directory):
    """Yield a built control process for the example conservatory.

    Takes ``state_directory`` rather than ``installation`` so this process resolves the
    control store the way a supervised child does, from the environment.

    Yields:
        ControlProcess: the control, closed afterwards
    """
    installation = state_directory
    installation.write_control(conservatory())
    process = ControlProcess("conservatory", settings_file=installation.settings_file)
    try:
        yield process
    finally:
        process.guard.close()
        process.close()


def _unevaluable_rule():
    """Return a parsed rule that produces nan, which is not an answer to act on.

    Reachable from finite input, which is why the gate refuses it rather than reading it as
    true - `not nan` is False, so an unevaluable gate would otherwise let a control actuate
    because its answer could not be computed.

    Returns:
        Rule: the parsed rule
    """
    return parse_rule("1e400 - 1e400")


def _never_sleep(_seconds):
    """Spend no real time waiting out a dwell.

    Args:
        _seconds (float): how long the loop wanted to wait, ignored
    """


class TestACycleThatRuns:
    def test_a_cold_room_energises_and_the_state_is_one_the_ladder_declares(self, control, bridge):
        """The whole loop end to end: read 16 degrees against an 18 degree target, run the
        PID, pick rungs, command the bridge."""
        control.cycle(dt=60, moment=NIGHT, sleep=_never_sleep)
        assert bridge.commanded(), "nothing was commanded at all"
        assert invariants.states_were_declared(bridge, control.document).violations == []
        assert any(state for state in bridge.energised().values()), bridge.energised()

    def test_the_devices_are_commanded_through_the_source_the_document_names(self, control, bridge):
        control.cycle(dt=60, moment=NIGHT, sleep=_never_sleep)
        assert {command.name for command in bridge.commanded()} <= {"far", "near"}

    def test_a_warm_room_asks_for_nothing(self, control, bridge, influx):
        """The other half of the loop working: the setpoint is met, so the ladder settles at
        its bottom rung rather than heating anyway."""
        influx.write_reading("conservatory_temperature", 24.0)
        control.cycle(dt=60, moment=NIGHT, sleep=_never_sleep)
        assert bridge.energised() == {"far": False, "near": False}


class TestWhenItShouldNotBeActing:
    def test_outside_the_active_period_nothing_is_read_at_all(self, control, bridge, influx):
        """The gate's check order is only worth having if the cost is deferred: a control
        outside its window should not pay for a sensor read to be told so."""
        influx.clear()
        bridge.clear()
        decision = control.cycle(dt=60, moment=DAY, sleep=_never_sleep)
        assert decision.actuating is False
        assert influx.requests == []

    def test_the_first_cycle_outside_the_window_is_not_an_edge(self, control, bridge):
        """A control starting up has not *become* inactive, so nothing is commanded for a
        transition that did not happen."""
        bridge.clear()
        assert control.cycle(dt=60, moment=DAY, sleep=_never_sleep).edge is None
        assert bridge.commanded() == []

    def test_leaving_the_window_puts_the_devices_in_the_end_state(self, control, bridge):
        control.cycle(dt=60, moment=NIGHT, sleep=_never_sleep)
        decision = control.cycle(dt=60, moment=DAY, sleep=_never_sleep)
        assert decision.edge == "closed"
        assert bridge.energised() == {"far": False, "near": False}


class TestAFailedCycleIsNotAFailedControl:
    def test_a_stale_reading_fails_safe_and_the_control_carries_on(self, control, bridge, influx):
        """Acting on a value that stopped being true a day ago is the failure the bound
        exists to prevent. The next cycle with fresh data works.

        The stored point is aged *and* the bridge is unreachable, which is the real shape of
        it: a stale reading triggers a live fetch, so a reachable device would simply
        refresh it and there would be nothing stale to act on. The collector has stopped and
        the device is unreachable - then, and only then, is what InfluxDB holds all there is.
        """
        control.cycle(dt=60, moment=NIGHT, sleep=_never_sleep)
        influx.age_reading("conservatory_temperature", 86400)
        with faults.frozen(influx), faults.unreachable(bridge):
            assert control.cycle(dt=60, moment=NIGHT, sleep=_never_sleep) is None
        influx.write_reading("conservatory_temperature", 16.0)
        assert control.cycle(dt=60, moment=NIGHT, sleep=_never_sleep) is not None

    def test_the_fail_safe_commands_the_devices_when_it_can_reach_them(self, control, bridge):
        """The stale-reading case above cannot show this: in that example the sensor and the
        heaters are on the same bridge, so the outage that strands the reading also strands
        the command. A gate that cannot be evaluated fails the same cycle with everything
        still reachable, which is where the safe state actually lands."""
        control.cycle(dt=60, moment=NIGHT, sleep=_never_sleep)
        assert any(bridge.energised().values()), "nothing was on to be made safe"
        control.gate._enable_when = _unevaluable_rule()
        assert control.cycle(dt=60, moment=NIGHT, sleep=_never_sleep) is None
        assert bridge.energised() == {"far": False, "near": False}

    def test_an_unreachable_database_fails_safe_rather_than_stopping(self, control, bridge, influx):
        control.cycle(dt=60, moment=NIGHT, sleep=_never_sleep)
        with faults.unreachable(influx):
            assert control.cycle(dt=60, moment=NIGHT, sleep=_never_sleep) is None
        assert bridge.energised() == {"far": False, "near": False}

    def test_a_bridge_that_cannot_be_reached_to_be_made_safe_is_logged_not_raised(
        self, control, bridge, influx, caplog
    ):
        """There is nothing left for the control to do about it, and the supervisor's own
        safe-state pass is what covers the case."""
        with faults.unreachable(influx), faults.unreachable(bridge):
            assert control.cycle(dt=60, moment=NIGHT, sleep=_never_sleep) is None
        assert "could not reach its devices" in caplog.text


class TestGathering:
    def test_a_reading_past_its_max_age_is_a_cycle_failure_not_a_config_fault(self, installation, influx, bridge):
        """RuleEvaluationError, so the fail-safe covers the cycle and the control is still
        there next time - the same distinction the loop uses for its own rules.

        The device is unreachable as well as the point being old, because a stale reading
        triggers a live fetch: with a reachable bridge there would be nothing stale left to
        refuse.
        """
        influx.age_reading("conservatory_temperature", 86400)
        with faults.frozen(influx), faults.unreachable(bridge):
            with pytest.raises(RuleEvaluationError, match="past the"):
                with requests.Session() as session:
                    gather(conservatory(), installation.settings, session, settings_file=installation.settings_file)

    def test_the_parameters_are_there_for_the_rules_to_read(self, installation):
        with requests.Session() as session:
            bindings = gather(conservatory(), installation.settings, session, settings_file=installation.settings_file)
        assert bindings["target"] == 18.0
        assert bindings["inside"] == 16.0


class TestCommandingDevices:
    def test_a_source_with_no_write_path_is_refused(self, installation):
        document = conservatory()
        document["devices"] = {"far": {"source": "openmeteo", "device": "far"}}
        with pytest.raises(ConfigError, match="no write path"):
            command_devices(document, {"far": True}, installation.settings_file)

    def test_a_device_the_document_does_not_declare_is_refused(self, installation):
        with pytest.raises(ConfigError, match="not declared"):
            command_devices(conservatory(), {"nosuchdevice": True}, installation.settings_file)

    def test_the_far_end_s_refusal_reaches_the_caller(self, installation, bridge):
        with faults.erroring(bridge, 503):
            with pytest.raises(SourceConnectionError):
                command_devices(conservatory(), {"far": True}, installation.settings_file)


def _quick_control(**overrides):
    """Return the example control with a window short enough to watch.

    Args:
        **overrides: top-level keys to replace

    Returns:
        dict: a control document
    """
    document = conservatory(**overrides)
    document["output"] = dict(document["output"], cycle_seconds=1, min_transition_seconds=1)
    # No window, so the child acts whatever time the test happens to run at. The period is
    # covered in process by the tests above; what this file's child processes are for is
    # everything that only exists once there is a process.
    document.pop("active_period", None)
    return document


def _next_beat(beats, timeout=15):
    """Return the next heartbeat line, or fail if none arrives in time.

    Bounded, and every read of the pipe goes through it. A bare ``readline()`` on a control
    that has stopped beating blocks for ever, so a regression in the heartbeat hangs the
    suite instead of failing it - which is how the first version of these tests behaved when
    the beat was deliberately removed to check they would notice.

    Args:
        beats (io.TextIOBase): the read end of the heartbeat pipe
        timeout (float): how long to wait

    Returns:
        str: the line, without its newline

    Raises:
        AssertionError: nothing arrived, or the pipe reached EOF
    """
    selector = selectors.DefaultSelector()
    selector.register(beats, selectors.EVENT_READ)
    try:
        assert selector.select(timeout=timeout), f"no heartbeat within {timeout}s"
    finally:
        selector.close()
    line = beats.readline()
    assert line, "the heartbeat pipe reached EOF instead of beating"
    return line.strip()


def _start_control(installation, name="conservatory", extra=()):
    """Start a real control process with a heartbeat pipe, and return it.

    Returns:
        tuple: the child and the read end of its heartbeat pipe, as a file object
    """
    read_fd, write_fd = os.pipe()
    child = subprocess.Popen(
        [
            sys.executable,
            os.path.join(ROOT, "sendtoinflux.py"),
            "--control",
            name,
            "--settings",
            installation.settings_file,
            "--heartbeat-fd",
            str(write_fd),
            *extra,
        ],
        pass_fds=(write_fd,),
        stderr=subprocess.PIPE,
        text=True,
        env=installation.environment(),
    )
    os.close(write_fd)
    return child, os.fdopen(read_fd, "r")


class TestTheHeartbeatWriter:
    def test_it_says_the_pipe_has_gone_once_rather_than_every_cycle(self, caplog):
        """A control with a fifteen-minute cycle would otherwise log the same warning four
        times an hour for as long as it runs, and a pipe that has gone does not come back."""
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        beat = heartbeat_writer(write_fd)
        with caplog.at_level(logging.WARNING):
            for _ in range(5):
                beat()
        assert caplog.text.count("heartbeat could not be written") == 1

    def test_a_beat_reaches_the_other_end(self, tmp_path):
        read_fd, write_fd = os.pipe()
        beat = heartbeat_writer(write_fd)
        beat()
        with os.fdopen(read_fd, "r") as beats:
            assert float(beats.readline()) > 0


class TestAsARealProcess:
    """Everything that only exists once there is a process: the entry point, the heartbeat,
    and what a signal leaves behind."""

    def test_it_beats_once_a_cycle_and_commands_the_bridge(self, installation, bridge):
        installation.write_control(_quick_control())
        child, beats = _start_control(installation)
        try:
            first, second = _next_beat(beats), _next_beat(beats)
            assert float(first) > 0 and float(second) >= float(first)
            assert bridge.commanded(), "the control never reached the bridge"
        finally:
            child.kill()
            child.wait(timeout=30)
            beats.close()

    def test_the_safe_state_is_asserted_before_anything_is_read(self, installation, bridge, influx):
        """A control that has just been restarted after a kill has devices in an unknown
        state, and finding out what the temperature is can wait until they are not."""
        installation.write_control(_quick_control())
        influx.clear()
        child, beats = _start_control(installation)
        try:
            _next_beat(beats)
            first = bridge.commanded()[0]
            assert first.state == {"on": False}, bridge.commanded()[:3]
        finally:
            child.kill()
            child.wait(timeout=30)
            beats.close()

    def test_a_handled_signal_leaves_the_devices_safe(self, installation, bridge):
        """systemctl stop signals every process in the unit's cgroup at once, so a control
        handles its own SIGTERM - and the exit handler only runs because that handler exits
        rather than letting the default action kill the process."""
        installation.write_control(_quick_control())
        child, beats = _start_control(installation)
        try:
            _next_beat(beats)
            child.send_signal(signal.SIGTERM)
            assert child.wait(timeout=30) == 0
            assert bridge.energised() == {"far": False, "near": False}
        finally:
            child.kill()
            beats.close()

    def test_the_pipe_reaches_eof_when_the_control_dies(self, installation):
        """Death detection, free: the write end closes however the process dies, so a parent
        blocked on the read gets told without asking anybody."""
        installation.write_control(_quick_control())
        child, beats = _start_control(installation)
        try:
            _next_beat(beats)
            child.kill()
            child.wait(timeout=30)
            # Bounded, and the same way a supervisor will wait: a readable pipe that yields
            # nothing is EOF. A bare read() would prove the same thing by hanging for ever
            # when it is wrong, which is not a test result.
            selector = selectors.DefaultSelector()
            selector.register(beats, selectors.EVENT_READ)
            try:
                assert selector.select(timeout=10), "the pipe never became readable"
                assert beats.readline() == "", "the pipe never reached EOF"
            finally:
                selector.close()
        finally:
            beats.close()

    def test_a_control_that_cannot_run_says_so_and_exits_rather_than_looping(self, installation):
        """A configuration fault is not something a respawn fixes, so it must be
        distinguishable from a crash - the operator's problem, reported as theirs."""
        child, beats = _start_control(installation, name="nosuchcontrol")
        try:
            assert child.wait(timeout=30) == 1
            assert "cannot run" in child.stderr.read()
        finally:
            beats.close()
