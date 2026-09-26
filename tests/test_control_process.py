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
import unittest.mock
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
from toinflux.control_process import ControlProcess, command_devices, gather, heartbeat_writer, run_control
from toinflux.exceptions import ConfigError, SourceConnectionError, ToolParamError
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


class TestSayingItRecovered:
    """The recovery line is the only thing that explains a gap in the journal, so it has to
    arrive when the cycle really did complete and not merely when it was attempted."""

    def _fail_once(self, control, bridge, influx):
        """Run one cycle that cannot be completed.

        Args:
            control (ControlProcess): the control to cycle
            bridge (StubBridge): the bridge to strand
            influx (StubInflux): the database holding the stale point
        """
        influx.age_reading("conservatory_temperature", 86400)
        with faults.frozen(influx), faults.unreachable(bridge):
            assert control.cycle(dt=60, moment=NIGHT, sleep=_never_sleep) is None

    def test_a_failing_cycle_does_not_claim_it_completed(self, control, bridge, influx, caplog):
        """It said so four seconds before the same cycle failed, on a real control during a
        bridge outage: the inputs are read inside the window, so a cycle that has reached the
        window has not yet done the part that fails.

        **The second consecutive failure, not the first.** The recovery line is silent where
        nothing was being reported, so the first failure has nothing to wrongly clear and a
        one-cycle outage shows nothing at all. It takes two to see it, which is what an outage
        actually looks like.
        """
        control.cycle(dt=60, moment=NIGHT, sleep=_never_sleep)
        self._fail_once(control, bridge, influx)
        caplog.clear()
        # DEBUG, because the repeated failure is deliberately throttled down to it - the
        # point here is that the recovery line is absent, and capturing only INFO and above
        # would assert that by capturing nothing at all.
        with caplog.at_level(logging.DEBUG):
            self._fail_once(control, bridge, influx)
        assert "could not complete a cycle" in caplog.text
        assert "completed a cycle again" not in caplog.text

    def test_a_sustained_outage_reports_each_problem_once_and_counts_the_rest(self, control, bridge, influx, caplog):
        """Every cycle of the outage said everything afresh: the staleness message carries the
        reading's age, which grows by a cycle every cycle, and the unreachable-bridge message
        carries an exception whose repr carries the connection object's address.  Neither was
        ever equal to the one before it, so a five-minute outage logged four ERRORs every
        thirty seconds instead of one at the start."""
        control.cycle(dt=60, moment=NIGHT, sleep=_never_sleep)
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            for _ in range(3):
                self._fail_once(control, bridge, influx)
        for phrase in ("could not complete a cycle", "could not reach its devices"):
            levels = [record.levelno for record in caplog.records if phrase in record.getMessage()]
            assert levels == [logging.ERROR, logging.DEBUG, logging.DEBUG], f"{phrase}: {levels}"
        caplog.set_level(logging.INFO)
        caplog.clear()
        influx.write_reading("conservatory_temperature", 16.0)
        assert control.cycle(dt=60, moment=NIGHT, sleep=_never_sleep) is not None
        assert "completed a cycle again (after 3 failure(s))" in caplog.text

    def test_a_cycle_that_really_completed_says_so(self, control, bridge, influx, caplog):
        """The other half: the line still has to appear, or the fix would be indistinguishable
        from deleting it."""
        control.cycle(dt=60, moment=NIGHT, sleep=_never_sleep)
        self._fail_once(control, bridge, influx)
        caplog.set_level(logging.INFO)
        caplog.clear()
        influx.write_reading("conservatory_temperature", 16.0)
        assert control.cycle(dt=60, moment=NIGHT, sleep=_never_sleep) is not None
        assert "completed a cycle again" in caplog.text


class TestRestartingOutsideTheWindow:
    """A restart outside the active period must not carry an integral into the next opening.

    simple-pid's `set_auto_mode(True)` resets only on an actual manual-to-automatic change.
    After a restart the loop is already automatic and, outside its window, nothing ever holds
    it - so `resume` at the opening edge did nothing and whatever was restored at startup was
    used hours later with no age check at all. The conservatory reopening at 23:30 would have
    started from where the previous night left off, which is the case the age limit exists
    for.
    """

    def _restart(self, installation, moment):
        """Build a control process as a restart would, at a given moment.

        Args:
            installation (Installation): the state directory to build against
            moment (datetime.datetime): when the restart happens

        Returns:
            ControlProcess: the new process
        """
        with unittest.mock.patch("toinflux.control_process.datetime") as clock:
            clock.datetime.now.return_value = moment
            clock.timezone = datetime.timezone
            return ControlProcess("conservatory", settings_file=installation.settings_file)

    def test_a_restart_outside_it_holds_so_the_opening_decides(self, state_directory, bridge, influx):
        """Held, rather than left running, so the age is judged when the window opens rather
        than assumed to be fine."""
        state_directory.write_control(conservatory())
        started = ControlProcess("conservatory", settings_file=state_directory.settings_file)
        try:
            started.cycle(dt=60, moment=NIGHT, sleep=_never_sleep)
            assert started.controller.pid._integral != 0, "nothing was learned, so this proves nothing"
        finally:
            started.guard.close()
            started.close()

        restarted = self._restart(state_directory, DAY)
        try:
            assert restarted.controller.pid.auto_mode is False, "a restart outside the window left the loop running"
        finally:
            restarted.guard.close()
            restarted.close()

    def test_a_restart_inside_it_carries_straight_on(self, state_directory, bridge, influx):
        """The other side: the ordinary restart must stay free, or this would have traded one
        fault for a loop that always starts from nothing.

        Asserted on the integral after a cycle rather than on the mode at construction. Every
        restored loop is held now, whichever side of the window it lands on; what differs is
        whether the next cycle releases it, and that is the thing worth pinning.
        """
        state_directory.write_control(conservatory())
        started = ControlProcess("conservatory", settings_file=state_directory.settings_file)
        try:
            started.cycle(dt=60, moment=NIGHT, sleep=_never_sleep)
            learned = started.controller.pid._integral
            assert learned != 0, "nothing was learned, so this proves nothing"
        finally:
            started.guard.close()
            started.close()

        restarted = self._restart(state_directory, NIGHT)
        try:
            restarted.cycle(dt=60, moment=NIGHT, sleep=_never_sleep)
            assert restarted.controller.pid.auto_mode is True, "an acting cycle did not release the loop"
            assert restarted.controller.pid._integral != 0, "the ordinary restart started from nothing"
        finally:
            restarted.guard.close()
            restarted.close()


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


class TestLeaveUnchanged:
    """`leave_unchanged` is an instruction, not an omission - and every path that can
    produce it has to say so, because the one that did not crashed the control at the exact
    moment its window closed."""

    @pytest.fixture
    def left_alone(self, state_directory):
        """Yield a control that is to be left alone at both ends.

        Yields:
            ControlProcess: the control, closed afterwards
        """
        document = conservatory(safe_state="leave_unchanged")
        document["active_period"] = dict(document["active_period"], end_state="leave_unchanged")
        state_directory.write_control(document)
        process = ControlProcess("conservatory", settings_file=state_directory.settings_file)
        try:
            yield process
        finally:
            process.guard.close()
            process.close()

    def test_the_window_closing_touches_nothing(self, left_alone, bridge):
        left_alone.cycle(dt=60, moment=NIGHT, sleep=_never_sleep)
        bridge.clear()
        decision = left_alone.cycle(dt=60, moment=DAY, sleep=_never_sleep)
        assert decision.edge == "closed"
        assert bridge.commanded() == []

    def test_a_failed_cycle_touches_nothing(self, left_alone, bridge, influx):
        left_alone.cycle(dt=60, moment=NIGHT, sleep=_never_sleep)
        bridge.clear()
        with faults.unreachable(influx):
            assert left_alone.cycle(dt=60, moment=NIGHT, sleep=_never_sleep) is None
        assert bridge.commanded() == []


class TestCommandingDevices:
    def test_a_source_with_no_write_path_is_refused(self, installation):
        document = conservatory()
        document["devices"] = {"far": {"source": "openmeteo", "device": "far"}}
        with pytest.raises(ConfigError, match="cannot switch a device"):
            command_devices("conservatory", document, {"far": True}, installation.settings_file)

    def test_a_writable_source_that_cannot_switch_a_device_is_refused_too(self, installation):
        """The case the old check let through. `MCP_WRITABLE` says only that *some* write
        path exists, and the shapes differ per source: Speedtest's is `mcp_trigger_run()`,
        which cannot actuate anything. It passed the check and raised AttributeError on the
        next line - and AttributeError is not one of the types the supervisor's safe-state
        pass handles, so one control's document could end the thread supervising all of
        them.

        Asserted against the real handler rather than a stub, because the claim is about
        what this project actually ships: a source that is writable and cannot switch a
        device.
        """
        from toinflux.speedtest import Speedtest

        assert Speedtest.MCP_WRITABLE is True
        assert not hasattr(Speedtest, "mcp_set_device_state")

        document = conservatory()
        document["devices"] = {"far": {"source": "speedtest", "device": "far"}}
        with pytest.raises(ConfigError, match="cannot switch a device"):
            command_devices("conservatory", document, {"far": True}, installation.settings_file)

    def test_the_refusal_names_the_control_s_own_device_keys(self, installation):
        """What an operator edits is the entry they wrote, not the name the far end knows
        the device by - and not the instance, which cannot be the fault here: actuating is
        a property of the source, so every instance of it answers the same way."""
        document = conservatory()
        document["devices"] = {
            "upstairs": {"source": "speedtest", "device": "line-1"},
            "downstairs": {"source": "speedtest", "device": "line-2"},
        }
        with pytest.raises(ConfigError) as exc:
            command_devices(
                "conservatory", document, {"upstairs": True, "downstairs": False}, installation.settings_file
            )
        assert "'downstairs'" in str(exc.value) and "'upstairs'" in str(exc.value)
        assert "line-1" not in str(exc.value)

    def test_it_refuses_before_building_a_handler(self, installation):
        """Speedtest is deliberately *not* configured on this installation. Asked of the
        class, the refusal names the real fault; asked of a constructed handler, settings
        are loaded first and the answer is "not found in settings" - which sends an operator
        off to configure a source that could never have worked anyway.

        Building one also opens a session, and the supervisor calls this on every death.
        """
        assert "speedtest" not in installation.settings
        document = conservatory()
        document["devices"] = {"far": {"source": "speedtest", "device": "far"}}
        with pytest.raises(ConfigError) as exc:
            command_devices("conservatory", document, {"far": True}, installation.settings_file)
        assert "cannot switch a device" in str(exc.value)
        assert "not found in settings" not in str(exc.value)

    def test_a_device_declaring_no_source_is_a_config_error_not_a_key_error(self, installation):
        """The supervisor calls this to make a dead control's devices safe and handles the
        project's own types, so a KeyError out of one corrupt document would escape that
        handler and stop every other control being supervised."""
        document = conservatory()
        document["devices"] = {"far": {"device": "far"}}
        with pytest.raises(ConfigError, match="declares no"):
            command_devices("conservatory", document, {"far": True}, installation.settings_file)

    def test_a_device_the_document_does_not_declare_is_refused(self, installation):
        with pytest.raises(ConfigError, match="not declared"):
            command_devices("conservatory", conservatory(), {"nosuchdevice": True}, installation.settings_file)

    def test_the_far_end_s_refusal_reaches_the_caller(self, installation, bridge):
        with faults.erroring(bridge, 503):
            with pytest.raises(SourceConnectionError):
                command_devices("conservatory", conservatory(), {"far": True}, installation.settings_file)


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


class TestAnEndStateThatCouldNotBeCommanded:
    """One transient bridge failure at the end of the active period used to strand the
    devices for the whole day.

    The closing edge was recorded as spent before its command had been run, so the failure
    left nothing to retry: the next cycle saw no change, so no edge and no actuation, and
    the loop slept with the heaters still on until the window reopened. Nothing else covered
    it - the fail-safe applies `safe_state` rather than the `end_state` that just failed, and
    the supervisor only makes devices safe when a control dies or is stopped.
    """

    def test_the_devices_are_commanded_again_on_the_next_cycle(self, control, bridge):
        """The retry, and the whole point of the fix."""
        control.cycle(dt=60, moment=NIGHT, sleep=_never_sleep)
        assert bridge.energised()["far"] is True, "the control should have been heating"
        with faults.unreachable(bridge):
            control.cycle(dt=60, moment=DAY, sleep=_never_sleep)
        decision = control.cycle(dt=60, moment=DAY, sleep=_never_sleep)
        assert decision.edge == "closed", "the failed edge was not re-delivered, so nothing retries it"
        assert bridge.energised() == {"far": False, "near": False}

    def test_a_successful_close_is_not_repeated(self, control, bridge):
        """The converse, because a retry that never stops is its own defect: edge-triggering
        exists so a control does not re-command devices already in place every cycle."""
        control.cycle(dt=60, moment=NIGHT, sleep=_never_sleep)
        assert control.cycle(dt=60, moment=DAY, sleep=_never_sleep).edge == "closed"
        bridge.clear()
        assert control.cycle(dt=60, moment=DAY, sleep=_never_sleep).edge is None
        assert bridge.commanded() == []


class TestRecoveringFromAFailedCycle:
    """A transient failure must not cost the control its ability to respond.

    `_fail_safe` holds the controller, so the loop does not integrate an error it never acted
    on. Nothing released that hold: the gate had not closed, so no `opened` edge followed, so
    the `resume` on that branch never ran. The PID stayed in manual mode and simple-pid
    returns the last demand unchanged while it is there - measured at 642 whether the room
    was 5 degrees or 25, which is a heater stuck on and a control that has stopped being one.

    The existing coverage asserted the next cycle returned a decision rather than None, which
    is true of a wholly unresponsive loop.
    """

    def test_the_controller_is_automatic_again_on_the_next_cycle(self, control, bridge):
        control.cycle(dt=60, moment=NIGHT, sleep=_never_sleep)
        control._fail_safe(SourceConnectionError("a transient failure"))
        assert control.controller.pid.auto_mode is False, "the fail-safe should have held it"
        control.cycle(dt=60, moment=NIGHT, sleep=_never_sleep)
        assert control.controller.pid.auto_mode is True, "nothing ever let go of the hold"

    def test_the_demand_responds_to_the_room_again(self, control, influx, bridge):
        """The property the mode is only a proxy for: a held loop answers the same whatever
        it is told, so asserting on the mode alone would miss a different way of freezing."""
        control.cycle(dt=60, moment=NIGHT, sleep=_never_sleep)
        control._fail_safe(SourceConnectionError("a transient failure"))

        def demand_at(temperature):
            """Run one cycle with the room at that temperature and return what was commanded.

            Args:
                temperature (float): the conservatory temperature to report

            Returns:
                list: the device states commanded during the cycle
            """
            influx.write_reading("temperature_conservatory", temperature)
            bridge.clear()
            control.cycle(dt=60, moment=NIGHT, sleep=_never_sleep)
            return bridge.commanded()

        cold = demand_at(5.0)
        warm = demand_at(25.0)
        assert cold != warm, f"the loop answered the same for 5 and 25 degrees: {cold} then {warm}"

    def test_an_ordinary_cycle_is_unaffected(self, control, bridge):
        """`set_auto_mode(True)` only resets when the mode actually changes, which is what
        lets the cycle call it unconditionally. A loop already running must not be reset
        every cycle - that would discard the integral continuously and never converge."""
        control.cycle(dt=60, moment=NIGHT, sleep=_never_sleep)
        integral = control.controller.pid._integral
        control.cycle(dt=60, moment=NIGHT, sleep=_never_sleep)
        assert control.controller.pid._integral != integral, "the integral stopped moving, so it is being reset"


class TestADeviceTheBridgeDoesNotHave:
    """The name came from a stored document, so no retry fixes it.

    `mcp_set_device_state` raises `ToolParamError` for a device it cannot resolve, which is
    right when a model asked - the model can pick another. Here it escaped the child's own
    handler, which catches `ConfigError`, so the control died with a traceback and the
    supervisor restarted it with backoff for ever against a name that will never resolve.
    """

    @pytest.fixture
    def misnamed(self, state_directory):
        """Write a control naming a device the stub bridge does not have.

        Returns:
            Installation: the installation holding it
        """
        document = conservatory()
        document["devices"] = {"far": {"source": "hue", "device": "no-such-light"}}
        document["output"]["stages"] = [
            {"level": 0, "set": {"far": False}},
            {"level": 1500, "set": {"far": True}},
        ]
        state_directory.write_control(document)
        return state_directory

    def test_it_is_a_config_error_the_child_can_report(self, misnamed, bridge, influx):
        control = ControlProcess("conservatory", settings_file=misnamed.settings_file)
        try:
            with pytest.raises(ConfigError):
                control.cycle(dt=60, moment=NIGHT, sleep=_never_sleep)
        finally:
            control.guard.close()
            control.close()

    def test_the_message_names_the_control_s_own_key_and_what_the_bridge_has(self, misnamed, bridge, influx):
        """The document is what has to be edited, and the handler only knows the bridge's
        name for the device - so both halves are needed to act on it."""
        control = ControlProcess("conservatory", settings_file=misnamed.settings_file)
        try:
            with pytest.raises(ConfigError) as raised:
                control.cycle(dt=60, moment=NIGHT, sleep=_never_sleep)
        finally:
            control.guard.close()
            control.close()
        assert "'far'" in str(raised.value), "the control's own device key is missing"
        assert "no-such-light" in str(raised.value)
        assert "available devices" in str(raised.value), "the bridge's own list is what makes it actionable"

    def test_the_original_is_kept_as_the_cause(self, misnamed, bridge, influx):
        """Wrapping must not discard what was raised."""
        control = ControlProcess("conservatory", settings_file=misnamed.settings_file)
        try:
            with pytest.raises(ConfigError) as raised:
                control.cycle(dt=60, moment=NIGHT, sleep=_never_sleep)
        finally:
            control.guard.close()
            control.close()
        assert isinstance(raised.value.__cause__, ToolParamError)


class TestANonFiniteReadingIsRefusedWhereItHasAName:
    """The other half of the nan defence, at the layer that knows which input it was.

    `gather`'s docstring used to say the controller checked finiteness "because that is where
    a nan does its damage". It is not where a nan does its damage: a nan reaching
    `max(target, dew + 5)` comes out finite, so the check at the far end sees nothing wrong.
    Refused here too, where the input has a name to report it by.
    """

    @staticmethod
    def _gathering(value, installation, monkeypatch):
        """Run gather with one input reading `value`.

        Args:
            value (float): what the reading holds
            installation (Installation): the installation
            monkeypatch (pytest.MonkeyPatch): to stub the read

        Returns:
            dict: the bindings
        """
        from types import SimpleNamespace

        from tests.harness.installation import conservatory

        document = conservatory()
        document["inputs"] = {"inside": {"source": "hue", "field": "temperature_conservatory"}}
        document["parameters"] = {"target": 18.0}
        monkeypatch.setattr(
            "toinflux.control_process.read_input",
            lambda *a, **k: SimpleNamespace(value=value, timestamp=0.0, age=0.0, live=False),
        )
        monkeypatch.setattr("toinflux.control_process.input_max_age", lambda *a, **k: 900.0)
        return gather(document, installation.settings, None, settings_file=installation.settings_file)

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_it_fails_the_cycle_rather_than_reaching_the_rules(self, value, installation, monkeypatch):
        with pytest.raises(RuleEvaluationError) as raised:
            self._gathering(value, installation, monkeypatch)
        assert "inside" in str(raised.value), "the message must name the input"

    def test_a_number_still_arrives(self, installation, monkeypatch):
        assert self._gathering(16.5, installation, monkeypatch)["inside"] == 16.5

    def test_it_is_this_cycle_and_not_this_control(self, installation, monkeypatch):
        """RuleEvaluationError rather than ConfigError: a bad point is not a bad document,
        and the next point written may be fine. The loop catches this one and fails safe."""
        with pytest.raises(RuleEvaluationError):
            self._gathering(float("nan"), installation, monkeypatch)


class TestWhatRunControlAssertsBeforeItsFirstCycle:
    """Driving `run_control` itself, which nothing did - every test of it patched it out.

    That gap is why this matters: `Gate.starting_state` can be entirely correct and simply
    never be called, and the whole suite stays green. Removing the call from `run_control`
    was caught by no test until this one.
    """

    @staticmethod
    def _store(installation, safe_state, period):
        """Write a single-heater control and return its name.

        Args:
            installation (Installation): the installation to write into
            safe_state (str): the control's safe_state
            period (dict or None): its active_period, or None for no window

        Returns:
            str: the control's name
        """
        from tests.harness.installation import conservatory

        document = conservatory(name="pump")
        document.pop("enable_when", None)
        document["safe_state"] = safe_state
        if period is None:
            document.pop("active_period", None)
        else:
            document["active_period"] = period
        document["devices"] = {"pump": {"source": "hue", "device": "far"}}
        document["output"] = dict(
            document["output"],
            cycle_seconds=1,
            min_transition_seconds=1,
            stages=[{"level": 0, "set": {"pump": False}}, {"level": 1500, "set": {"pump": True}}],
        )
        document["output"].pop("max_level", None)
        installation.write_control(document, name="pump")
        return "pump"

    def _first_and_last(self, state_directory, safe_state, period):
        """Run a control for no cycles and return what it commanded, first and last.

        Args:
            state_directory (Installation): the installation
            safe_state (str): the control's safe_state
            period (dict or None): its active_period

        Returns:
            tuple: (the first commanded on-state, the last)
        """
        name = self._store(state_directory, safe_state, period)
        state_directory.bridge.clear()
        run_control(name, settings_file=state_directory.settings_file, cycles=0, sleep=lambda _seconds: None)
        commands = state_directory.bridge.commanded("far")
        assert commands, "the control commanded nothing at all"
        return commands[0].state.get("on"), commands[-1].state.get("on")

    # A window that is closed for all but six hours of the night, so "now" is outside it
    # whenever this suite runs in daylight - and inside it whenever it does not, which is why
    # the test below picks its own window rather than trusting the clock.
    ALL_DAY = {"from": "00:00", "to": "23:59", "end_state": "unenergised"}
    NEVER = {"from": "23:58", "to": "23:59", "end_state": "unenergised"}

    def test_outside_its_window_it_starts_in_the_end_state(self, state_directory, bridge):
        """The defect: a pump with `safe_state: energised` restarted outside its window used
        to run in its failure state through normal scheduled downtime."""
        first, _last = self._first_and_last(state_directory, "energised", self.NEVER)
        assert first is False, "it started in its safe state rather than its end state"

    def test_inside_its_window_it_starts_in_the_safe_state(self, state_directory, bridge):
        first, _last = self._first_and_last(state_directory, "energised", self.ALL_DAY)
        assert first is True, "it started in its end state while inside its window"

    def test_with_no_window_the_safe_state_is_the_only_answer(self, state_directory, bridge):
        first, _last = self._first_and_last(state_directory, "energised", None)
        assert first is True

    def test_and_the_exit_half_still_applies_the_safe_state(self, state_directory, bridge):
        """Whatever the clock said on the way in. A process that is ending leaves nothing
        supervising the devices, which is what a safe state is for."""
        first, last = self._first_and_last(state_directory, "energised", self.NEVER)
        assert (first, last) == (False, True)


class TestCommandingADeviceSetToAValue:
    """The value travels on the parameter's own keyword, not on `on`."""

    @staticmethod
    def _document(parameter="brightness_pct"):
        """Return a control owning one driven device.

        Args:
            parameter (str): the parameter it is driven by

        Returns:
            dict: the control document
        """
        from tests.harness.installation import conservatory

        document = conservatory(name="lamp")
        document["devices"] = {"lamp": {"source": "hue", "device": "far", "parameter": parameter}}
        return document

    def test_a_value_reaches_the_far_end_on_its_own_keyword(self, installation, monkeypatch):
        seen = []
        monkeypatch.setattr(
            "toinflux.philipshue.Hue.mcp_set_device_state",
            lambda self, device, **kwargs: seen.append((device, kwargs)),
        )
        command_devices("lamp", self._document(), {"lamp": 40}, installation.settings_file)
        assert seen == [("far", {"brightness_pct": 40})]

    def test_zero_is_an_explicit_off_rather_than_the_dimmest_setting(self, installation, monkeypatch):
        """Which is what makes `unenergised` mean the same thing to both kinds of device."""
        seen = []
        monkeypatch.setattr(
            "toinflux.philipshue.Hue.mcp_set_device_state",
            lambda self, device, **kwargs: seen.append((device, kwargs)),
        )
        command_devices("lamp", self._document(), {"lamp": 0}, installation.settings_file)
        assert seen == [("far", {"on": False})]

    def test_a_switched_device_is_unchanged(self, installation, monkeypatch):
        seen = []
        monkeypatch.setattr(
            "toinflux.philipshue.Hue.mcp_set_device_state",
            lambda self, device, **kwargs: seen.append((device, kwargs)),
        )
        document = self._document()
        document["devices"]["lamp"].pop("parameter")
        command_devices("lamp", document, {"lamp": True}, installation.settings_file)
        assert seen == [("far", {"on": True})]

    def test_the_value_is_what_gets_recorded(self, installation, monkeypatch):
        from toinflux.transitions import TransitionLog

        monkeypatch.setattr("toinflux.philipshue.Hue.mcp_set_device_state", lambda self, device, **kwargs: None)
        command_devices("lamp", self._document(), {"lamp": 40}, installation.settings_file)
        assert TransitionLog("lamp", installation.settings_file).states() == {"lamp": 40}


class TestAControlResumesItsLoopOnStart:
    """The wiring, which is the half that can be right and never reached. `Controller.resume_from`
    and `TransitionLog.loop_state` both have their own tests; neither says the control calls them.
    """

    @staticmethod
    def _store(installation):
        """Write a slow lamp control and return its name.

        Args:
            installation (Installation): the installation to write into

        Returns:
            str: the control's name
        """
        from tests.harness.bridge import bulb
        from tests.harness.installation import conservatory
        from toinflux.controls import save_control

        installation.bridge.lights["9"] = bulb("office-lamp")
        document = conservatory(name="lamp")
        document.pop("active_period", None)
        document.pop("enable_when", None)
        document["parameters"] = {"target": 1000}
        document["inputs"] = {"lux": {"source": "hue", "field": "L", "max_age": 60}}
        document["pid"] = {"input": "lux", "setpoint": "target", "kp": 0.05, "ki": 0.0007, "kd": 0}
        document["devices"] = {"lamp": {"source": "hue", "device": "office-lamp", "parameter": "brightness_pct"}}
        document["output"] = {
            "cycle_seconds": 60,
            "min_transition_seconds": 15,
            "stages": [{"level": 0, "set": {"lamp": 0}}, {"level": 100, "set": {"lamp": 100}}],
        }
        save_control("lamp", document, installation.settings_file)
        return "lamp"

    @staticmethod
    def _spend(control, cycles):
        """Step a control and store what it learned, as a running loop does.

        Args:
            control (ControlProcess): the control
            cycles (int): how many cycles to run

        Returns:
            list: the brightness commanded each cycle
        """
        seen = []
        # As the cycle does: a restored loop comes back held, and stepping it before it is
        # released is a caller fault the controller now names.
        control.controller.resume()
        for _ in range(cycles):
            plan = control.controller.step({"lux": 300.0, "target": 1000}, dt=60)
            seen.append(plan[0].stage.states["lamp"])
            control.transitions.record_loop(control.controller.capture(), control.controller.fingerprint)
        return seen

    def test_the_second_run_starts_where_the_first_left_off(self, state_directory, bridge, caplog):
        from toinflux.control_process import ControlProcess

        name = self._store(state_directory)
        first = ControlProcess(name, settings_file=state_directory.settings_file)
        try:
            climb = self._spend(first, 5)
        finally:
            first.guard.stop("done")
            first.close()
        with caplog.at_level(logging.INFO):
            second = ControlProcess(name, settings_file=state_directory.settings_file)
        try:
            resumed = self._spend(second, 5)
        finally:
            second.guard.stop("done")
            second.close()
        assert climb[0] < climb[-1], "the first run did not have to climb, so this proves nothing"
        assert resumed[0] == pytest.approx(climb[-1], abs=1), "the second run started from nothing"
        assert "resumed the loop" in caplog.text

    def test_an_edited_document_starts_afresh(self, state_directory, bridge, caplog):
        """The commonest restart there is, and the one where the memory means something else:
        an integral is in the output's units."""
        from toinflux.control_process import ControlProcess
        from toinflux.controls import load_control, save_control

        name = self._store(state_directory)
        first = ControlProcess(name, settings_file=state_directory.settings_file)
        try:
            self._spend(first, 5)
        finally:
            first.guard.stop("done")
            first.close()
        document = load_control(name, state_directory.settings_file)
        document["pid"]["kp"] = 0.2
        save_control(name, document, state_directory.settings_file)
        with caplog.at_level(logging.INFO):
            second = ControlProcess(name, settings_file=state_directory.settings_file)
        try:
            assert "resumed the loop" not in caplog.text
        finally:
            second.guard.stop("done")
            second.close()

    def test_a_control_that_has_never_run_starts_afresh_quietly(self, state_directory, bridge, caplog):
        from toinflux.control_process import ControlProcess

        name = self._store(state_directory)
        with caplog.at_level(logging.INFO):
            control = ControlProcess(name, settings_file=state_directory.settings_file)
        try:
            assert "resumed the loop" not in caplog.text
        finally:
            control.guard.stop("done")
            control.close()


class TestAStateLeftOverFromAnOlderDocument:
    """The transition log outlives a document edit, so it can describe a device that no
    longer exists in the shape it records.

    A device changed from on/off to a driven parameter keeps a `true` or `false` in the log.
    While its minimum has not elapsed the loop pins it to that stored value, which used to
    send `brightness_pct=True`: the Hue handler refuses it as a caller mistake, and
    `command_devices` turns that into the ConfigError that stops a control for good. A
    control halted by its own history with a perfectly valid document, and no retry fixes it.
    """

    @staticmethod
    def _store_driven(installation, parameter, top):
        """Write a lamp control driven by one parameter.

        Args:
            installation (Installation): the state directory to write into
            parameter (str): the device parameter
            top (float): what the top rung sets it to

        Returns:
            dict: the stored document
        """
        from toinflux.controls import save_control

        document = conservatory(name="lamp")
        document.pop("active_period", None)
        document.pop("enable_when", None)
        document["parameters"] = {"target": 1000}
        document["inputs"] = {"lux": {"source": "hue", "field": "L", "max_age": 60}}
        document["pid"] = {"input": "lux", "setpoint": "target", "kp": 0.05, "ki": 0.0007, "kd": 0}
        document["devices"] = {"lamp": {"source": "hue", "device": "office-lamp", "parameter": parameter}}
        document["output"] = {
            "cycle_seconds": 60,
            "min_transition_seconds": 600,
            "stages": [{"level": 0, "set": {"lamp": 0}}, {"level": 100, "set": {"lamp": top}}],
        }
        save_control("lamp", document, installation.settings_file)
        return document

    def test_a_value_past_its_parameter_s_scale_is_clamped_and_said(self, state_directory, bridge, influx, caplog):
        """Validation refuses a percentage past 100 while the caller is still listening. This
        is the path where one arrives anyway, and at that moment the choice is a light at its
        brightest or a control that stops for good.

        WARNING, because the device did something and it was not quite what was asked.
        """
        from tests.harness.bridge import bulb

        bridge.lights["9"] = bulb("office-lamp")
        document = self._store_driven(state_directory, "brightness_pct", 100)
        with caplog.at_level(logging.WARNING):
            command_devices("lamp", document, {"lamp": 150}, state_directory.settings_file)
        commanded = bridge.commanded("office-lamp")
        assert commanded, "nothing was commanded"
        # 254 is the bridge's own full scale, which is what 100 percent maps onto.
        assert commanded[-1].state["bri"] == 254, commanded[-1].state
        assert "tops out" in caplog.text and "150" in caplog.text, caplog.text

    def test_a_value_within_the_scale_is_left_alone(self, state_directory, bridge, influx, caplog):
        """The other side, or the clamp could be rewriting every command it sees."""
        from tests.harness.bridge import bulb

        bridge.lights["9"] = bulb("office-lamp")
        document = self._store_driven(state_directory, "brightness_pct", 100)
        with caplog.at_level(logging.WARNING):
            command_devices("lamp", document, {"lamp": 40}, state_directory.settings_file)
        assert "tops out" not in caplog.text, caplog.text

    def test_a_number_from_a_different_parameter_is_not_pinned_either(self, state_directory, bridge, influx):
        """The same fault with a number instead of a boolean, and the worse of the two.

        A device moved from `color_temp_k` to `brightness_pct` leaves 2700 in the log, which
        arrives as a brightness and is refused. Going the other way leaves 80, which is a
        legal colour temperature, gets clamped, and is silently wrong - so the test cannot be
        "is it a number" but "is it on the scale this device is on now".
        """
        from tests.harness.bridge import bulb
        from toinflux.transitions import TransitionLog

        bridge.lights["9"] = bulb("office-lamp")
        self._store_driven(state_directory, "brightness_pct", 100)
        # What the previous, colour-temperature, version of this document left behind.
        TransitionLog("lamp", state_directory.settings_file).record(
            {"lamp": 2700}, forced=False, parameters={"lamp": "color_temp_k"}
        )

        control = ControlProcess("lamp", settings_file=state_directory.settings_file)
        try:
            influx.write_reading("L", 300.0)
            control.cycle(dt=60, moment=NIGHT, sleep=_never_sleep)
        finally:
            control.guard.close()
            control.close()
        commanded = [command.state for command in bridge.commanded("office-lamp")]
        assert commanded, "the lamp was never commanded at all, so this proves nothing"
        assert all("bri" in state for state in commanded), commanded

    def test_a_state_sent_to_another_actuator_is_not_pinned(self, state_directory, bridge, influx):
        """A control key is a name in a document, and what it points at can be changed
        underneath it. The recorded state belongs to whatever was there at the time, so
        pinning it commands the new actuator a value it has never been given - and under
        `leave_unchanged` it would suppress that actuator's first command entirely."""
        from tests.harness.bridge import bulb
        from toinflux.transitions import TransitionLog

        bridge.lights["9"] = bulb("office-lamp")
        self._store_driven(state_directory, "brightness_pct", 100)
        # The same key, the same scale, a different bulb.
        TransitionLog("lamp", state_directory.settings_file).record(
            {"lamp": 35},
            forced=False,
            parameters={"lamp": "brightness_pct"},
            targets={"lamp": ("hue", None, "a-different-lamp")},
        )
        control = ControlProcess("lamp", settings_file=state_directory.settings_file)
        try:
            held = control.transitions.frozen(control.controller.min_transition_for, ("lamp",))
            assert held == frozenset({"lamp"}), "a 600s minimum did not hold a lamp moved a moment ago"
            control.controller.resume()
            plan = control._hold(
                control.controller.step({"lux": 300.0, "target": 1000}, dt=60),
                held & set(control.controller.driven),
            )
        finally:
            control.guard.close()
            control.close()
        assert {dwell.stage.states["lamp"] for dwell in plan} != {35}, "a value from another bulb was pinned"

    def test_a_value_on_the_same_parameter_is_still_pinned(self, state_directory, bridge, influx):
        """The other side: the minimum must still hold a device that has not changed shape, or
        this would have turned `min_transition_seconds` off for every driven device."""
        from tests.harness.bridge import bulb
        from toinflux.transitions import TransitionLog

        bridge.lights["9"] = bulb("office-lamp")
        self._store_driven(state_directory, "brightness_pct", 100)
        log = TransitionLog("lamp", state_directory.settings_file)
        log.record(
            {"lamp": 35},
            forced=False,
            parameters={"lamp": "brightness_pct"},
            targets={"lamp": ("hue", None, "office-lamp")},
        )

        control = ControlProcess("lamp", settings_file=state_directory.settings_file)
        try:
            held = control.transitions.frozen(control.controller.min_transition_for, ("lamp",))
            assert held == frozenset({"lamp"}), "a 600s minimum did not hold a lamp moved a moment ago"
            control.controller.resume()
            plan = control._hold(
                control.controller.step({"lux": 300.0, "target": 1000}, dt=60),
                held & set(control.controller.driven),
            )
        finally:
            control.guard.close()
            control.close()
        assert {dwell.stage.states["lamp"] for dwell in plan} == {35}

    def test_a_boolean_is_not_pinned_onto_a_device_that_now_takes_a_value(self, state_directory, bridge, influx):
        from tests.harness.bridge import bulb
        from toinflux.controls import save_control
        from toinflux.transitions import TransitionLog

        bridge.lights["9"] = bulb("office-lamp")
        document = conservatory(name="lamp")
        document.pop("active_period", None)
        document.pop("enable_when", None)
        document["parameters"] = {"target": 1000}
        document["inputs"] = {"lux": {"source": "hue", "field": "L", "max_age": 60}}
        document["pid"] = {"input": "lux", "setpoint": "target", "kp": 0.05, "ki": 0.0007, "kd": 0}
        document["devices"] = {"lamp": {"source": "hue", "device": "office-lamp", "parameter": "brightness_pct"}}
        document["output"] = {
            "cycle_seconds": 60,
            "min_transition_seconds": 600,
            "stages": [{"level": 0, "set": {"lamp": 0}}, {"level": 100, "set": {"lamp": 100}}],
        }
        save_control("lamp", document, state_directory.settings_file)
        # What the previous, switched, version of this document left behind. A long minimum so
        # the device is certainly still inside it, which is when the pinning happens at all.
        TransitionLog("lamp", state_directory.settings_file).record({"lamp": False}, forced=False)

        control = ControlProcess("lamp", settings_file=state_directory.settings_file)
        try:
            influx.write_reading("L", 300.0)
            control.cycle(dt=60, moment=NIGHT, sleep=_never_sleep)
        finally:
            control.guard.close()
            control.close()
        # The cycle completing at all is half the claim: under the fault it raised ConfigError
        # out of `cycle`, which catches only a failed read.
        commanded = [command.state for command in bridge.commanded("office-lamp")]
        assert commanded, "the lamp was never commanded at all, so this proves nothing"
        # `bri`, the brightness the plan asked for. Asserted rather than the absence of a
        # boolean, because the CLIP wire format carries `on` alongside `bri` and that one is
        # a boolean by rights - the first version of this test read the protocol and called
        # it the bug.
        assert all("bri" in state for state in commanded), commanded
