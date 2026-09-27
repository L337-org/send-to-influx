"""The record of when each device last moved, and what it forbids.

``min_transition_seconds`` used to be kept only inside a cycle window, so a minimum longer
than the window could not be honoured and was refused by validation. These are the tests
that make it mean what it says: across windows, and across a restart, which is the case the
earlier reasoning gave up on.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import copy
import json
import logging
import os
import sys

import pytest

from tests.harness.bridge import bulb
from toinflux.controls import device_identity
from toinflux.exceptions import ConfigError
from toinflux.staging import build_ladder, plan_window, reachable_ladder
from tests.harness.installation import record_command
from toinflux.transitions import TransitionLog, forget_control, transition_path


def _log(installation, name="conservatory", now=None):
    """Return a log for a control, with a clock the test drives.

    Args:
        installation (Installation): the installation holding the state directory
        name (str): the control's name
        now (list or None): a one-element list holding the current epoch seconds

    Returns:
        tuple: (TransitionLog, the mutable clock list)
    """
    moment = now if now is not None else [1000.0]
    return TransitionLog(name, installation.settings_file, clock=lambda: moment[0]), moment


class TestWhatCountsAsATransition:
    def test_a_command_that_changes_nothing_does_not_restart_the_clock(self, state_directory):
        """Otherwise the minimum would mean "this long since anybody mentioned it" rather
        than "this long since it moved", and a control commanding its safe state on every
        restart would hold a device frozen for ever."""
        log, now = _log(state_directory)
        log.record({"heater": True})
        now[0] += 50
        log.record({"heater": True})
        assert log.elapsed("heater") == 50

    def test_a_command_that_changes_it_does(self, state_directory):
        log, now = _log(state_directory)
        log.record({"heater": True})
        now[0] += 50
        log.record({"heater": False})
        assert log.elapsed("heater") == 0

    def test_a_device_never_commanded_has_no_age(self, state_directory):
        """Distinct from "commanded a long time ago": there is nothing it is too soon after,
        so it must not be frozen on the strength of a missing record."""
        log, _now = _log(state_directory)
        assert log.elapsed("heater") is None
        assert log.frozen(lambda _d: 600, ("heater",)) == frozenset()


class TestSurvivingARestart:
    """The objection that kept this from being built, answered.

    The supervisor restarts a control on every document edit as well as on failure, and a
    restart asserting its safe state does not necessarily change anything - so an in-memory
    clock would come back empty and the device could move again at once.
    """

    def test_a_new_log_reads_what_the_last_one_wrote(self, state_directory):
        first, now = _log(state_directory)
        first.record({"heater": True})
        second, _ = _log(state_directory, now=now)
        assert second.elapsed("heater") == 0
        assert second.states() == {"heater": True}

    def test_the_minimum_still_binds_after_the_restart(self, state_directory):
        first, now = _log(state_directory)
        first.record({"heater": False})
        now[0] += 5
        second, _ = _log(state_directory, now=now)
        assert second.frozen(lambda _d: 120, ("heater",)) == frozenset({"heater"})

    def test_a_safe_state_that_changes_nothing_does_not_free_the_device(self, state_directory):
        """The exact sequence: heater off at t=0, process restarts at t=5 and asserts
        `unenergised`, which commands off again and is not a transition. At t=10 the loop
        wants it on, and must be told no."""
        first, now = _log(state_directory)
        first.record({"heater": False})
        now[0] += 5
        restarted, _ = _log(state_directory, now=now)
        restarted.record({"heater": False})
        now[0] += 5
        assert restarted.frozen(lambda _d: 120, ("heater",)) == frozenset({"heater"})


class TestTheMinimumIsNeverAnticipated:
    """A device is free once its minimum has elapsed and not a moment before.

    An earlier version brought a transition forward by up to half a window, so that a
    minimum expiring just after a boundary was not rounded up to the next one. It released
    devices early - 899 seconds against a 900-second minimum, 100 against 120 - and being
    early is the one direction that breaks the promise the setting makes. Asking once per
    window means the answer can be late; it must never be early.
    """

    def test_one_second_short_of_the_minimum_is_still_held(self, state_directory):
        log, now = _log(state_directory)
        log.record({"heater": True})
        now[0] += 119
        assert log.frozen(lambda _d: 120, ("heater",)) == frozenset({"heater"})

    def test_exactly_the_minimum_is_free(self, state_directory):
        """The boundary belongs to the free side: "not more often than every 120 seconds"
        is satisfied by 120."""
        log, now = _log(state_directory)
        log.record({"heater": True})
        now[0] += 120
        assert log.frozen(lambda _d: 120, ("heater",)) == frozenset()

    def test_a_long_minimum_on_a_short_cycle_is_not_shortened(self, state_directory):
        """The regime the freeze exists for, and the one the allowance damaged most."""
        log, now = _log(state_directory)
        log.record({"heater": True})
        now[0] += 870
        assert log.frozen(lambda _d: 900, ("heater",)) == frozenset({"heater"})


class TestAClockThatStepsBackwards:
    """Epoch seconds carry no time zone, but NTP and `date` can both move them."""

    def test_a_negative_age_reads_as_no_time_elapsed(self, state_directory):
        log, now = _log(state_directory)
        log.record({"heater": True})
        now[0] -= 3600
        assert log.elapsed("heater") == 0

    def test_which_holds_the_device_rather_than_freeing_it(self, state_directory):
        log, now = _log(state_directory)
        log.record({"heater": True})
        now[0] -= 3600
        assert log.frozen(lambda _d: 120, ("heater",)) == frozenset({"heater"})


class TestALogThatCannotBeRead:
    """A cache of what happened, not a document the control depends on. The worst an unusable
    one costs is one transition sooner than asked for, which is not worth refusing to heat
    over - but it is worth saying."""

    @pytest.mark.parametrize(
        "content", [pytest.param("not json at all", id="unparseable"), pytest.param('["a list"]', id="not-a-mapping")]
    )
    def test_it_starts_empty_and_says_so(self, state_directory, caplog, content):
        path = transition_path("conservatory", state_directory.settings_file)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        log = TransitionLog("conservatory", state_directory.settings_file)
        assert log.entries == {}
        assert "transition log" in caplog.text

    def test_a_missing_one_is_not_a_fault(self, state_directory):
        """The ordinary state of a control that has never run."""
        assert TransitionLog("conservatory", state_directory.settings_file).entries == {}


class TestTheFileItself:
    def test_it_is_written_readable_only_by_its_owner(self, state_directory):
        log, _now = _log(state_directory)
        log.record({"heater": True})
        assert oct(os.stat(log.path).st_mode)[-3:] == "600"

    def test_it_leaves_no_temporary_files_behind(self, state_directory):
        log, now = _log(state_directory)
        for index in range(5):
            now[0] += 1
            log.record({"heater": bool(index % 2)})
        assert sorted(os.listdir(os.path.dirname(log.path))) == ["conservatory.json"]

    def test_a_name_the_store_would_refuse_is_refused_here_too(self, state_directory):
        """The path is built from the name, so a name that could escape the directory must
        not reach it."""
        with pytest.raises(ConfigError):
            TransitionLog("../elsewhere", state_directory.settings_file)

    def test_a_deleted_control_takes_its_log_with_it(self, state_directory):
        log, _now = _log(state_directory)
        log.record({"heater": True})
        forget_control("conservatory", state_directory.settings_file)
        assert not os.path.exists(log.path)

    def test_forgetting_one_that_never_ran_is_not_a_fault(self, state_directory):
        forget_control("conservatory", state_directory.settings_file)


class TestTheLadderAFrozenDeviceLeaves:
    LADDER = build_ladder(
        [
            {"level": 0, "set": {"far": False, "near": False}},
            {"level": 750, "set": {"far": True, "near": False}},
            {"level": 1500, "set": {"far": True, "near": True}},
        ]
    )

    def test_nothing_frozen_leaves_the_whole_ladder(self):
        assert reachable_ladder(self.LADDER, frozenset(), {"far": False, "near": False}) is self.LADDER

    def test_a_frozen_device_removes_the_rungs_that_would_move_it(self):
        """`far` off and frozen: only the rung that leaves it off remains, so the control
        cannot raise the heat until it comes free."""
        available = reachable_ladder(self.LADDER, frozenset({"far"}), {"far": False, "near": False})
        assert [rung.level for rung in available] == [0]

    def test_the_other_device_still_moves_freely(self):
        """`far` on and frozen: the two rungs that keep it on are both available, so `near`
        trims across them at its own rate. This is the point of the whole change."""
        available = reachable_ladder(self.LADDER, frozenset({"far"}), {"far": True, "near": False})
        assert [rung.level for rung in available] == [750, 1500]

    def test_a_state_the_ladder_does_not_describe_falls_back_to_all_of_it(self):
        """After a safe state on a document with no all-off rung there is no such thing as
        "keep them where they are". The minimum is a promise about wear and the safe state is
        the safety mechanism, so this must not become an outage."""
        partial = build_ladder(
            [{"level": 750, "set": {"far": True, "near": False}}, {"level": 1500, "set": {"far": True, "near": True}}]
        )
        available = reachable_ladder(partial, frozenset({"far"}), {"far": False, "near": False})
        assert available is partial

    def test_a_frozen_device_with_no_recorded_state_pins_nothing(self):
        """A device nobody has a state for cannot pin anything, and defaulting it to off
        would pin every rung that switches it on. Equal rather than identical: this goes
        through the filter, it just keeps everything."""
        assert reachable_ladder(self.LADDER, frozenset({"far"}), {}) == self.LADDER


class TestAMinimumLongerThanTheWindowIsKept:
    """End to end, through the real loop, which is the claim the validation rule used to
    exist because we could not make.

    The planner alone would pass these: it is handed one window and would happily choose a
    different rung in the next. What makes them pass is the log between the two.
    """

    @staticmethod
    def _control(installation, minimum, cycle=1):
        """Store a control whose heater may not move for `minimum` seconds.

        Args:
            installation (Installation): the installation to write into
            minimum (float): the device's min_transition_seconds
            cycle (float): the cycle window

        Returns:
            str: the control's name
        """
        from tests.harness.installation import conservatory

        document = conservatory(name="slow")
        document.pop("active_period", None)
        document.pop("enable_when", None)
        document["devices"] = {"far": {"source": "hue", "device": "far", "min_transition_seconds": minimum}}
        document["output"] = dict(
            document["output"],
            cycle_seconds=cycle,
            min_transition_seconds=minimum,
            stages=[{"level": 0, "set": {"far": False}}, {"level": 1500, "set": {"far": True}}],
        )
        document["output"].pop("max_level", None)
        installation.write_control(document, name="slow")
        return "slow"

    def test_the_document_is_accepted_although_the_minimum_dwarfs_the_cycle(self, state_directory):
        """A 900-second minimum on a one-second window: refused outright until now."""
        from toinflux.controls import load_control, validate_control

        name = self._control(state_directory, minimum=900, cycle=1)
        document = load_control(name, state_directory.settings_file)
        assert validate_control(name, document, state_directory.settings) == []

    def test_a_device_inside_its_minimum_is_held_across_windows(self, state_directory, bridge):
        """The heart of it. The heater is commanded on, and every subsequent window is
        planned from the rungs that leave it there - so it is not switched off a second
        later even though the loop recomputes every second."""
        from toinflux.control_process import ControlProcess

        name = self._control(state_directory, minimum=900, cycle=1)
        control = ControlProcess(name, settings_file=state_directory.settings_file)
        try:
            control.transitions.record({"far": True})
            frozen = control.transitions.frozen(control.controller.min_transition_for, ("far",))
            assert frozen == frozenset({"far"})
            # The ladder the next window may use: only the rung that keeps it on.
            from toinflux.staging import reachable_ladder

            available = reachable_ladder(control.controller.ladder, frozen, control.transitions.states())
            assert [rung.level for rung in available] == [1500]
        finally:
            # Stop the guard as the loop's own `finally` does, not just close the session:
            # DeviceGuard registers an exit handler that commands the devices safe, and a
            # test that leaves it registered has it fire at interpreter exit against a stub
            # bridge that stopped long ago - an error per test, in every later run's output.
            control.guard.stop("the test is finished")
            control.close()

    def test_commanding_through_the_loop_records_it(self, state_directory, bridge):
        """`command_devices` is the only place a device is switched and the only place this
        is written, so a path added later cannot skip it."""
        from toinflux.control_process import ControlProcess

        name = self._control(state_directory, minimum=900, cycle=1)
        control = ControlProcess(name, settings_file=state_directory.settings_file)
        try:
            control._apply({"far": True})
            assert control.transitions.states() == {"far": True}
            assert control.transitions.elapsed("far") is not None
        finally:
            # Stop the guard as the loop's own `finally` does, not just close the session:
            # DeviceGuard registers an exit handler that commands the devices safe, and a
            # test that leaves it registered has it fire at interpreter exit against a stub
            # bridge that stopped long ago - an error per test, in every later run's output.
            control.guard.stop("the test is finished")
            control.close()

    def test_the_supervisor_making_devices_safe_records_it_too(self, state_directory, bridge):
        """The other caller. A safe state that actually switches something off is a
        transition, and a control restarted straight afterwards must see it."""
        from toinflux.control_process import command_devices
        from toinflux.controls import load_control

        name = self._control(state_directory, minimum=900, cycle=1)
        document = load_control(name, state_directory.settings_file)
        command_devices(name, document, {"far": True}, state_directory.settings_file)
        command_devices(name, document, {"far": False}, state_directory.settings_file)
        log = TransitionLog(name, state_directory.settings_file)
        assert log.states() == {"far": False}


class TestASafeStateTrumpsTheMinimum:
    """Both directions, which is the point.

    Going in it always did: a safe state is commanded directly rather than planned, so no
    minimum was ever consulted. Going out it did not - the safe state was recorded like any
    other move, so a heater forced off by a transient fault sat there for a whole minimum
    after the fault cleared. That is the setting protecting the hardware from the safety
    mechanism, which is the wrong way round.

    The state is still recorded, because the planner has to know where the devices actually
    are; it is the timing that stops binding.
    """

    def test_a_forced_move_is_recorded_so_the_planner_knows_where_it_is(self, state_directory):
        log, _now = _log(state_directory)
        log.record({"heater": False}, forced=True)
        assert log.states() == {"heater": False}

    def test_but_does_not_hold_the_control_off_afterwards(self, state_directory):
        log, now = _log(state_directory)
        log.record({"heater": True})
        now[0] += 5
        log.record({"heater": False}, forced=True)
        now[0] += 5
        assert log.frozen(lambda _d: 900, ("heater",)) == frozenset()

    def test_an_ordinary_move_afterwards_restores_the_normal_rule(self, state_directory):
        """The exemption belongs to the safe state that earned it, not to the device."""
        log, now = _log(state_directory)
        log.record({"heater": False}, forced=True)
        now[0] += 5
        log.record({"heater": True})
        now[0] += 5
        assert log.frozen(lambda _d: 900, ("heater",)) == frozenset({"heater"})

    def test_an_ordinary_command_confirming_the_forced_state_clears_it_too(self, state_directory):
        """The state does not change, so nothing is timed - but the exemption must still go,
        or a safe state that happened to leave the device where the ladder wanted it would
        exempt it for ever."""
        log, now = _log(state_directory)
        log.record({"heater": False}, forced=True)
        now[0] += 5
        log.record({"heater": False})
        assert log.released("heater") is False

    def test_it_survives_a_restart_like_everything_else_here(self, state_directory):
        first, now = _log(state_directory)
        first.record({"heater": False}, forced=True)
        second, _ = _log(state_directory, now=now)
        assert second.released("heater") is True
        assert second.frozen(lambda _d: 900, ("heater",)) == frozenset()


class TestWhatACycleSaysForItself:
    """Nothing in this subsystem said anything during a healthy cycle, so a control holding
    the wrong temperature produced a curve and no record of what it was thinking. At DEBUG,
    because it is per cycle and the answer to "is it working" is not in the log by default.
    """

    @staticmethod
    def _controller(**output):
        """Return a controller over a three-rung ladder.

        Args:
            **output: overrides for the output section

        Returns:
            Controller: ready to step
        """
        from toinflux.controller import Controller

        return Controller(
            {
                "parameters": {"target": 20.0},
                "inputs": {"inside": {"source": "hue", "field": "t"}},
                "pid": {"input": "inside", "setpoint": "target", "kp": 100.0, "ki": 0.0, "kd": 0.0},
                "output": dict(
                    {
                        "cycle_seconds": 300,
                        "min_transition_seconds": 60,
                        "stages": [
                            {"level": 0, "set": {"far": False, "near": False}},
                            {"level": 750, "set": {"far": True, "near": False}},
                            {"level": 1500, "set": {"far": True, "near": True}},
                        ],
                    },
                    **output,
                ),
                "devices": {"far": {"source": "hue", "device": "far"}, "near": {"source": "hue", "device": "near"}},
            }
        )

    def test_it_records_what_it_read_what_it_chased_and_what_it_asked_for(self, caplog):
        """The terms a tuning argument is actually had in. Without them, kp is guesswork."""
        with caplog.at_level(logging.DEBUG):
            self._controller().step({"inside": 16.0, "target": 20.0}, dt=300)
        line = caplog.text
        assert "input=16.000" in line
        assert "setpoint=20.000" in line
        assert "demand=400.0" in line
        assert "p=400.0" in line, "the PID's own terms, so a runaway integral is visible"
        assert "level 0 for 140s" in line and "level 750 for 160s" in line

    def test_a_held_device_is_named_when_it_is_holding(self, caplog):
        """The case that is otherwise unreadable: the demand asks for almost nothing and the
        control commands level 750 anyway, because `far` may not switch off yet. Without the
        held list that looks like a loop doing the opposite of what it was told."""
        with caplog.at_level(logging.DEBUG):
            self._controller().step(
                {"inside": 18.5, "target": 20.0}, dt=300, frozen=frozenset({"far"}), states={"far": True}
            )
        assert "demand=150.0" in caplog.text
        assert "level 750 for 300s" in caplog.text
        assert "held='far'" in caplog.text

    def test_nothing_is_held_when_nothing_is_held(self, caplog):
        """An empty list every cycle is noise that teaches people to skim the line."""
        with caplog.at_level(logging.DEBUG):
            self._controller().step({"inside": 16.0, "target": 20.0}, dt=300)
        assert "held=" not in caplog.text

    def test_it_says_nothing_at_info(self, caplog):
        """Once per cycle is once every few seconds on a fast control."""
        with caplog.at_level(logging.INFO):
            self._controller().step({"inside": 16.0, "target": 20.0}, dt=300)
        assert caplog.text == ""

    def test_the_rungs_it_commands_name_the_devices(self, state_directory, bridge, caplog):
        """`level 750` does not say which heater that turned on, and the question asked of
        this log is always about a particular device."""
        from toinflux.control_process import ControlProcess

        document = TestAMinimumLongerThanTheWindowIsKept._control(state_directory, minimum=1, cycle=1)
        control = ControlProcess(document, settings_file=state_directory.settings_file)
        try:
            with caplog.at_level(logging.DEBUG):
                control.cycle(dt=1, sleep=lambda _seconds: None)
            assert "'far'=" in caplog.text, "the commanded states are not in the log"
        finally:
            control.guard.stop("the test is finished")
            control.close()


class TestAPartialFailureStillRecordsWhatMoved:
    """Two devices, the first commanded and the second unreachable.

    The record used to sit after the whole grouped loop, so the exception carried straight
    past it: the first device had moved at the far end and nothing knew when. The next cycle
    or the next restart would switch it again inside its minimum - the failure this log
    exists to prevent, arriving exactly when the far end is already misbehaving.
    """

    @staticmethod
    def _two_device_control(installation):
        """Store a control over two devices on one bridge.

        Args:
            installation (Installation): the installation to write into

        Returns:
            dict: the stored document
        """
        from tests.harness.bridge import plug
        from tests.harness.installation import conservatory

        installation.bridge.lights["9"] = plug("second")
        document = conservatory(name="pair")
        document.pop("active_period", None)
        document.pop("enable_when", None)
        document["devices"] = {
            "one": {"source": "hue", "device": "far"},
            "two": {"source": "hue", "device": "second"},
        }
        document["output"] = dict(
            document["output"],
            cycle_seconds=1,
            min_transition_seconds=900,
            stages=[
                {"level": 0, "set": {"one": False, "two": False}},
                {"level": 1500, "set": {"one": True, "two": True}},
            ],
        )
        document["output"].pop("max_level", None)
        installation.write_control(document, name="pair")
        return document

    def test_the_device_that_moved_is_written_down_although_the_call_failed(self, state_directory, bridge, monkeypatch):
        from toinflux.control_process import command_devices
        from toinflux.exceptions import SourceConnectionError
        from toinflux.philipshue import Hue

        document = self._two_device_control(state_directory)
        calls = []
        real = Hue.mcp_set_device_state

        def one_then_fail(self, device, **kwargs):
            """Accept the first device and refuse every one after it.

            Args:
                self (DataHandler): the handler
                device (str): the far-end device name
                **kwargs: the states asked for

            Returns:
                object: whatever the real method returns, for the first device

            Raises:
                SourceConnectionError: on every device after the first
            """
            calls.append(device)
            if len(calls) > 1:
                raise SourceConnectionError("the bridge stopped answering")
            return real(self, device, **kwargs)

        monkeypatch.setattr(Hue, "mcp_set_device_state", one_then_fail)
        with pytest.raises(SourceConnectionError):
            command_devices("pair", document, {"one": True, "two": True}, state_directory.settings_file)

        log = TransitionLog("pair", state_directory.settings_file)
        assert log.states() == {"one": True}, "the device that actually moved was not recorded"
        assert log.elapsed("one") is not None

    def test_and_is_therefore_held_by_its_minimum_afterwards(self, state_directory, bridge):
        """The consequence, which is the reason the record matters: the failure must not
        hand the device a free transition on the next cycle."""
        log, now = _log(state_directory, name="pair")
        log.record({"one": True})
        now[0] += 5
        assert log.frozen(lambda _d: 900, ("one", "two")) == frozenset({"one"})


class TestTheMinimumHoldsOverALongRun:
    """Drive hundreds of windows and measure the gaps that actually occurred.

    Every other test here asks whether the rule fires. This one asks the question the rule
    exists to answer - was any device ever switched twice inside its minimum - and it is the
    test that would have caught the early-release allowance, which satisfied every unit test
    written for it while releasing a device at 899 seconds against a 900-second minimum.

    It also pins something worth knowing: below `minimum <= cycle_seconds` the freeze never
    decides anything, because `plan_window` already spaces the changes on its own. That is
    why it is not a defect that the shipped examples never freeze anything.
    """

    LADDER = build_ladder(
        [
            {"level": 0, "set": {"far": False, "near": False}},
            {"level": 750, "set": {"far": True, "near": False}},
            {"level": 1500, "set": {"far": True, "near": True}},
        ]
    )
    # Wanders across every rung and lands on each of them, which is what makes devices move.
    DEMANDS = [(step * 137) % 1600 for step in range(400)]

    @classmethod
    def _shortest_gap(cls, cycle, minimum, freeze=True):
        """Run the planner over many windows and return the shortest gap any device saw.

        Args:
            cycle (float): the cycle window
            minimum (float): every device's min_transition_seconds
            freeze (bool): whether to apply the cross-window freeze at all

        Returns:
            float: the shortest interval between two changes of one device, or inf where no
                device ever changed twice
        """
        now, state, last, shortest = 0.0, {"far": False, "near": False}, {}, {}
        for demand in cls.DEMANDS:
            held = {d for d in state if freeze and d in last and now - last[d] < minimum}
            available = reachable_ladder(cls.LADDER, frozenset(held), state)
            at = now
            for dwell in plan_window(available, demand, cycle, lambda _device: minimum):
                for device, value in dwell.stage.states.items():
                    if state[device] != value:
                        if device in last:
                            shortest[device] = min(shortest.get(device, at - last[device]), at - last[device])
                        last[device], state[device] = at, value
                at += dwell.seconds
            now += cycle
        return min(shortest.values()) if shortest else float("inf")

    @pytest.mark.parametrize(
        "cycle, minimum",
        [
            pytest.param(300, 60, id="shipped-normal"),
            pytest.param(300, 120, id="shipped-slow-response"),
            pytest.param(60, 10, id="shipped-fast-adjustment"),
            pytest.param(60, 900, id="minimum-far-longer-than-the-window"),
            pytest.param(50, 120, id="minimum-just-over-the-window"),
            pytest.param(29, 900, id="window-that-divides-the-minimum-badly"),
            pytest.param(7, 120, id="very-short-window"),
            pytest.param(13, 300, id="another-awkward-ratio"),
            pytest.param(120, 120, id="minimum-equal-to-the-window"),
            pytest.param(100, 60, id="minimum-under-the-window"),
        ],
    )
    def test_no_device_is_ever_switched_inside_its_minimum(self, cycle, minimum):
        gap = self._shortest_gap(cycle, minimum)
        assert gap >= minimum, f"a device changed after {gap}s against a {minimum}s minimum"

    @pytest.mark.parametrize("cycle, minimum", [(60, 900), (50, 120), (29, 900)])
    def test_and_the_freeze_is_what_is_doing_it(self, cycle, minimum):
        """Where the minimum is longer than the window, removing the freeze breaks it - so
        these cases are not passing for some unrelated reason."""
        assert self._shortest_gap(cycle, minimum, freeze=False) < minimum

    @pytest.mark.parametrize("cycle, minimum", [(300, 60), (300, 120), (60, 10)])
    def test_while_below_that_the_planner_alone_is_enough(self, cycle, minimum):
        """The shipped examples. The freeze never fires here and does not need to: two dwells
        each at least the minimum also space a change at a boundary from the one before it."""
        assert self._shortest_gap(cycle, minimum, freeze=False) >= minimum

    def test_lateness_is_bounded_by_one_window(self, state_directory):
        """What asking once per window costs, stated rather than left to be discovered. It is
        always less than the minimum itself, because the freeze only decides anything when the
        minimum is the longer of the two."""
        log, now = _log(state_directory)
        log.record({"heater": True})
        now[0] += 900
        assert log.frozen(lambda _device: 900, ("heater",)) == frozenset()


class TestADeviceNameCannotForgeALogLine:
    """Device keys come from the control document, an MCP client can write one, and nothing
    constrains their characters - so an unquoted one containing a newline writes its own line
    into the journal, and into whatever is reading it.

    The same reason `_render_names` exists in the store, applied to the per-rung debug record
    that names which device each rung switched.
    """

    FORGED = "heater\n2026-01-01 00:00:00 ERROR    this line was not written by the service"

    def test_a_newline_in_a_device_key_stays_on_one_line(self, state_directory, bridge, caplog):
        from toinflux.control_process import ControlProcess
        from toinflux.controls import save_control

        from tests.harness.installation import conservatory

        document = conservatory(name="forged")
        document.pop("active_period", None)
        document.pop("enable_when", None)
        document["devices"] = {self.FORGED: {"source": "hue", "device": "far"}}
        document["output"] = dict(
            document["output"],
            cycle_seconds=1,
            min_transition_seconds=1,
            stages=[{"level": 0, "set": {self.FORGED: False}}, {"level": 1500, "set": {self.FORGED: True}}],
        )
        document["output"].pop("max_level", None)
        save_control("forged", document, state_directory.settings_file)

        control = ControlProcess("forged", settings_file=state_directory.settings_file)
        try:
            with caplog.at_level(logging.DEBUG):
                control.cycle(dt=1, sleep=lambda _seconds: None)
        finally:
            control.guard.stop("the test is finished")
            control.close()
        commanding = [record for record in caplog.records if "commanding level" in record.getMessage()]
        assert commanding, "the rung was never logged, so this proves nothing"
        for record in commanding:
            message = record.getMessage()
            assert "\n" not in message, f"a device name reached the log as {message.count(chr(10)) + 1} lines"
        assert "\\n" in commanding[0].getMessage(), "the newline should survive as an escape rather than vanish"


class TestAdjustingADrivenDevice:
    """`min_transition_seconds` for a device that is adjusted rather than switched means how
    often the adjustment is made. The same log answers it, but only once it holds the value
    commanded rather than a flag."""

    def test_a_dimmer_moving_between_two_on_values_is_a_move(self, state_directory):
        """Under a boolean comparison both 40 and 5 were simply "on", so nothing was timed and
        the minimum meant nothing at all for the one kind of device whose job is to change by
        degrees."""
        log, now = _log(state_directory)
        log.record({"lamp": 40})
        now[0] += 10
        log.record({"lamp": 5})
        assert log.elapsed("lamp") == 0

    def test_and_the_same_value_again_is_not(self, state_directory):
        log, now = _log(state_directory)
        log.record({"lamp": 40})
        now[0] += 10
        log.record({"lamp": 40})
        assert log.elapsed("lamp") == 10

    def test_the_value_survives_a_restart_rather_than_collapsing_to_a_flag(self, state_directory):
        first, now = _log(state_directory)
        first.record({"lamp": 40})
        second, _ = _log(state_directory, now=now)
        assert second.states() == {"lamp": 40}

    def test_a_dimmer_inside_its_minimum_is_held(self, state_directory):
        log, now = _log(state_directory)
        log.record({"lamp": 40})
        now[0] += 10
        assert log.frozen(lambda _device: 60, ("lamp",)) == frozenset({"lamp"})


class TestReadingTheFile:
    """Two sections, each read from its own key, and nothing else read at all."""

    def _stored(self, state_directory, stored):
        """Write a cache file verbatim and read it back.

        Args:
            state_directory: the fixture naming the settings file
            stored (object): exactly what the file should contain

        Returns:
            TransitionLog: the log built from it, at a fixed clock
        """
        path = transition_path("conservatory", state_directory.settings_file)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(stored, handle)
        return TransitionLog("conservatory", state_directory.settings_file, clock=lambda: 2000.0)

    def test_a_flat_log_is_ignored_and_says_so(self, state_directory, caplog):
        """The shape an unreleased build wrote before the sections existed is not read.

        Reading it as well meant telling two shapes apart that can look identical, because a flat
        log may name its devices `devices` and `pid`, and every rule for doing that could be
        fooled by corrupting one value. Nothing released wrote it, so it is dropped with a warning:
        the cost is at most one transition sooner than asked, once, on a machine that ran that
        build.
        """
        with caplog.at_level(logging.WARNING):
            log = self._stored(state_directory, {"heater": {"state": True, "at": 1000.0}})
        assert log.states() == {}, "a flat log was read"
        assert log.loop == {}
        assert "unknown keys: 'heater'" in caplog.text, "dropping a device's minimum said nothing"
        assert "conservatory" in caplog.text

    def test_a_flat_log_of_the_two_reserved_names_makes_no_phantoms(self, state_directory):
        """The flat log every earlier reader stumbled on: devices named after the sections.

        Read as sections, `devices` holds a `state` and an `at` that are not entries and are
        dropped, and `pid` holds no fingerprint so it cannot be resumed. Nothing is invented.
        """
        log = self._stored(
            state_directory, {"devices": {"state": True, "at": 1000.0}, "pid": {"state": False, "at": 1000.0}}
        )
        assert log.states() == {}, "a section's fields came back as devices"
        assert log.loop_state("fp", 10_000) is None, "a device's record was resumed as loop memory"

    @pytest.mark.parametrize(
        "key, value",
        [
            ("heater", True),
            ("heater", {"state": False, "at": 1.0}),
            ("lamp", {"state": True, "at": 1000.0}),
            ("state", True),
            ("at", 1000.0),
            ("fingerprint", "fp"),
        ],
        ids=["device-name-bool", "device-name-record", "new-name-record", "state", "at", "fingerprint"],
    )
    def test_a_stray_top_level_key_changes_nothing(self, state_directory, key, value):
        """A key the writer never produces is ignored, whatever it is called and holds.

        When the reader merged top-level keys into the device namespace, a stray key named after
        a device replaced that device's record - losing it outright, or resetting its moment so
        its minimum was released early - and a stray record became a device nobody declared.
        """
        stored = {
            "devices": {"heater": {"state": True, "at": 1000.0}},
            "pid": {"integral": 3.5, "at": 1000.0, "fingerprint": "fp"},
            key: value,
        }
        log = self._stored(state_directory, stored)
        assert log.states() == {"heater": True}, f"a stray {key!r} changed the devices"
        assert log.elapsed("heater", 2000.0) == 1000.0, f"a stray {key!r} moved heater's moment"
        assert log.loop_state("fp", 10_000) is not None, f"a stray {key!r} lost the loop"

    def test_a_file_the_writer_produced_logs_nothing(self, state_directory, caplog):
        """The warning is for a damaged file, so an ordinary one must not raise it on every start."""
        log, _now = _log(state_directory)
        log.record({"heater": True})
        log.record_loop({"integral": 1.0}, "fp")
        with caplog.at_level(logging.WARNING):
            _log(state_directory)
        assert caplog.text == "", "a well-formed file was reported as damaged"

    def test_one_bad_device_entry_costs_only_itself(self, state_directory):
        """`_usable_entries` drops a bad entry on its own, and the rest of the section stays."""
        log = self._stored(
            state_directory,
            {
                "devices": {"lamp": "corrupt", "heater": {"state": True, "at": 1000.0}},
                "pid": {"integral": 5.0, "fingerprint": "abc", "at": 1000.0},
            },
        )
        assert log.states() == {"heater": True}, "the good entry went the way of the bad one"
        assert log.loop.get("integral") == 5.0, "the loop's memory went with it"

    @pytest.mark.parametrize("name, corrupt", [("state", None), ("at", 123)], ids=["state", "at"])
    def test_a_corrupt_device_named_after_a_record_field_costs_only_itself(self, state_directory, name, corrupt):
        """Nothing reserves `state` or `at` as device names, and earlier readers looked inside a
        section for exactly those, so one corrupt device of either name used to cost the file."""
        log = self._stored(
            state_directory,
            {
                "devices": {name: corrupt, "heater": {"state": True, "at": 1000.0}},
                "pid": {"integral": 3.5, "at": 1000.0, "fingerprint": "abc"},
            },
        )
        assert log.states() == {"heater": True}, "a real device was lost to a corrupt namesake"
        assert log.elapsed("heater", 2000.0) == 1000.0, "the surviving device lost its moment"
        assert log.loop.get("integral") == 3.5, "the loop's memory went with it"

    @pytest.mark.parametrize("unreadable", [None, [], 5, "x"], ids=["null", "list", "number", "string"])
    def test_a_corrupt_section_does_not_take_the_other_one_with_it(self, state_directory, unreadable):
        """The two sections are independent: one that cannot be read says nothing about the other."""
        devices_kept = self._stored(
            state_directory, {"devices": {"heater": {"state": True, "at": 1000.0}}, "pid": unreadable}
        )
        assert devices_kept.states() == {"heater": True}, "an unreadable loop lost the devices"

        loop_kept = self._stored(
            state_directory, {"devices": unreadable, "pid": {"integral": 3.5, "at": 1000.0, "fingerprint": "abc"}}
        )
        assert loop_kept.loop.get("integral") == 3.5, "an unreadable device section lost the loop"
        assert loop_kept.states() == {}, "the loop's memory came back as a device"

    @pytest.mark.parametrize("missing", ["pid", "devices"], ids=["no-loop-key", "no-devices-key"])
    def test_a_section_that_is_gone_entirely_does_not_take_the_other_with_it(self, state_directory, missing):
        """A file holding one section is read for that section."""
        stored = {
            "devices": {"heater": {"state": True, "at": 1000.0}},
            "pid": {"integral": 3.5, "at": 1000.0, "fingerprint": "fp"},
        }
        del stored[missing]
        log = self._stored(state_directory, stored)
        if missing == "pid":
            assert log.states() == {"heater": True}, "losing the loop key lost every device"
        else:
            assert log.loop_state("fp", 10_000) is not None, "losing the device key lost the loop"

    def test_a_stray_key_in_the_loop_is_still_the_loop(self, state_directory):
        """A `state` arriving in the loop section is only an extra key in the loop's memory."""
        log = self._stored(
            state_directory,
            {"devices": {}, "pid": {"state": 1, "integral": 5.0, "fingerprint": "fp", "at": 1000.0}},
        )
        assert log.loop_state("fp", 10_000) is not None, "a usable integral was thrown away"
        assert log.states() == {}, "the loop's memory came back as a device"

    def test_a_loop_that_has_lost_its_fingerprint_is_kept_but_not_resumed(self, state_directory):
        """Kept, so `get_control_state` can show what is written down; declined by `loop_state`."""
        log = self._stored(state_directory, {"devices": {}, "pid": {"at": 1000.0, "integral": 5.0}})
        assert log.loop_state("f", 10_000) is None, "a memory with no fingerprint was resumed"
        assert log.loop == {"at": 1000.0, "integral": 5.0}
        assert log.states() == {}, "a loop nobody can use came back as a device"

    @pytest.mark.parametrize(
        "stored", [[], ["heater"], "heater", 5, None], ids=["list", "names", "string", "number", "null"]
    )
    def test_a_file_that_is_not_a_mapping_at_all_reads_as_empty(self, state_directory, stored):
        """Valid JSON that is not an object has no sections, and must not raise on the way past."""
        log = self._stored(state_directory, stored)
        assert log.states() == {}, "something was read out of a file that is not a mapping"
        assert log.loop == {}, "a loop was read out of a file that is not a mapping"

    @pytest.mark.parametrize(
        "pair", [[1, "x"], ["x", 1], [None, None], [["a"], ["b"]]], ids=["key", "value", "both", "nested"]
    )
    def test_an_identity_pair_that_is_not_two_strings_is_refused(self, state_directory, pair):
        """An identity is pairs of strings, and anything else is normalised away rather than
        trusted - the entry keeps its moment and loses only the scale it cannot prove."""
        log = self._stored(
            state_directory,
            {"devices": {"lamp": {"state": 40, "at": 1000.0, "for": [pair]}}, "pid": {}},
        )
        assert log.identities() == {"lamp": None}, "a pair that is not two strings was kept"
        assert log.elapsed("lamp", 2000.0) == 1000.0, "the entry lost its moment with its identity"

    @pytest.mark.parametrize(
        "record",
        [{"state": 2700, "at": 1000.0, "for": "corrupt"}, {"state": 2700, "at": 1000.0}],
        ids=["unreadable-identity", "no-identity"],
    )
    def test_a_record_that_cannot_prove_its_device_is_not_held(self, state_directory, record):
        """The writer always records an identity, so a missing one and an unreadable one are both
        damage, and neither may hold the device - or `_hold` pins a lamp logged at 2700 under one
        parameter to 2700 as a brightness after it was redeclared."""
        log = self._stored(state_directory, {"devices": {"lamp": record}, "pid": {}})
        live = {"lamp": device_identity({"source": "hue", "device": "L", "parameter": "brightness_pct"})}
        held = log.frozen(lambda _d: 900.0, ["lamp"], now=1010.0, identities=live)
        assert held == frozenset(), "a state on an unprovable scale was held as if it were trusted"

    def test_a_record_with_a_matching_identity_is_held(self, state_directory):
        """The other side, or the check above would have turned the minimum off altogether."""
        spec = {"source": "hue", "device": "L", "parameter": "brightness_pct"}
        identity = [list(pair) for pair in device_identity(spec)]
        log = self._stored(state_directory, {"devices": {"lamp": {"state": 40, "at": 1000.0, "for": identity}}})
        held = log.frozen(lambda _d: 900.0, ["lamp"], now=1010.0, identities={"lamp": device_identity(spec)})
        assert held == frozenset({"lamp"}), "a record matching its device lost its minimum"

    def test_what_is_written_back_keeps_the_devices_that_were_read(self, state_directory):
        """The next write is what would make a misreading permanent, so it must keep what the read
        kept - and it drops what the read ignored, which is how a damaged file heals."""
        log = self._stored(
            state_directory,
            {
                "devices": {"state": None, "heater": {"state": True, "at": 1000.0}},
                "pid": {"integral": 3.5, "at": 1000.0, "fingerprint": "abc"},
                "stray": {"state": True, "at": 1.0},
            },
        )
        log.record_loop({"integral": 4.0}, "abc")
        with open(transition_path("conservatory", state_directory.settings_file), encoding="utf-8") as handle:
            on_disk = json.load(handle)
        assert set(on_disk) == {"devices", "pid"}, "the write carried an ignored key forward"
        assert set(on_disk["devices"]) == {"heater"}, "the good device was written out of existence"

    # --- The invariant, asked of every single-point change rather than a few -----------------

    #: A device of every name that has confused a reader of this file, beside an ordinary one and
    #: a driven one - so a sweep damaging one of them has the others as bystanders.
    DEVICE_NAMES = ("heater", "lamp", "state", "at", "for", "devices", "pid", "fingerprint", "integral")
    LOOP = {"integral": 3.5, "at": 1000.0, "fingerprint": "fp"}
    #: What a value can be damaged into: every JSON type, a moment, and shapes that look like a
    #: record, a section and an identity, since a lookalike is what fooled every earlier reader.
    DAMAGE = [
        None,
        0,
        1000.0,
        "x",
        [],
        {},
        True,
        {"at": 1000.0},
        {"state": True, "at": 1.0},
        {"heater": {"state": False, "at": 1.0}},
        [["device", "heater"]],
    ]
    DELETE = object()

    def _base(self):
        """Return a well-formed file holding every device in `DEVICE_NAMES` and a loop.

        Returns:
            dict: the file as the writer would produce it
        """
        devices = {name: {"state": True, "at": 1000.0, "for": [["device", name]]} for name in self.DEVICE_NAMES}
        devices["lamp"] = {"state": 40, "at": 1000.0, "for": [["device", "lamp"], ["parameter", "brightness_pct"]]}
        return {"devices": devices, "pid": dict(self.LOOP)}

    def _changes(self):
        """Yield every single-point change to the base file, with what it is allowed to cost.

        The owner is the one thing the change may damage: a device, the loop, a whole section, or
        nothing at all for a key added at the top. Everything else must come through untouched.

        Yields:
            tuple: (where, what, owner), where `where` is a path of keys, `what` the new value or
            `DELETE`, and `owner` one of ``("device", name)``, ``("loop",)``, ``("section", key)``
            or ``("nothing",)``
        """
        yield from self._damage()
        yield from self._additions()

    def _damage(self):
        """Yield every change to, or removal of, a value the base file already holds.

        Yields:
            tuple: (where, what, owner), as for `_changes`
        """
        base = self._base()
        values = [*self.DAMAGE, self.DELETE]
        for section in ("devices", "pid"):
            for what in values:
                yield (section,), what, ("section", section)
        for name, entry in base["devices"].items():
            for where in [("devices", name)] + [("devices", name, field) for field in entry]:
                for what in values:
                    yield where, what, ("device", name)
        for field in self.LOOP:
            for what in values:
                yield ("pid", field), what, ("loop",)

    def _additions(self):
        """Yield every key added where the base file has none, at every level.

        Yields:
            tuple: (where, what, owner), as for `_changes`
        """
        places = [
            (("devices", name, extra), ("device", name))
            for name in self.DEVICE_NAMES
            for extra in ("fingerprint", "integral", "devices", "pid", "heater")
        ]
        places += [(("pid", extra), ("loop",)) for extra in ("state", "for", "heater", "devices")]
        places += [(("devices", "newcomer"), ("device", "newcomer"))]
        # Top-level keys other than the two sections, whose replacement `_damage` already covers.
        places += [
            ((key,), ("nothing",))
            for key in (*self.DEVICE_NAMES, "newcomer", "version")
            if key not in ("devices", "pid")
        ]
        for where, owner in places:
            for what in self.DAMAGE:
                yield where, what, owner

    @staticmethod
    def _apply(base, where, what, delete):
        """Return a copy of a file with one change made.

        Args:
            base (dict): the file as it should be
            where (tuple): the path to change
            what (object): the value to put there, or `delete`
            delete (object): the marker meaning "remove the key"

        Returns:
            dict: the changed file
        """
        stored = copy.deepcopy(base)
        node = stored
        for step in where[:-1]:
            node = node[step]
        if what is delete:
            node.pop(where[-1], None)
        else:
            node[where[-1]] = what
        return stored

    def _assert_bystanders_intact(self, log, owner, label):
        """Check every device and the loop the change did not own came through untouched.

        Args:
            log (TransitionLog): the log read from the changed file
            owner (tuple): what the change was allowed to damage
            label (str): the change, for the failure message
        """
        base_devices = self._base()["devices"]
        states = log.states()
        if owner != ("section", "devices"):
            for name, entry in base_devices.items():
                if owner == ("device", name):
                    continue
                assert states.get(name) == entry["state"], f"{label} changed {name!r}'s state"
                assert log.elapsed(name, 2000.0) == 1000.0, f"{label} moved {name!r}'s moment"
                expected = tuple(tuple(pair) for pair in entry["for"])
                assert log.identities().get(name) == expected, f"{label} changed {name!r}'s identity"
        allowed = set(base_devices) | ({owner[1]} if owner[0] == "device" else set())
        assert set(states) <= allowed, f"{label} invented {sorted(set(states) - allowed)!r}"
        if owner not in (("loop",), ("section", "pid")):
            assert log.loop_state("fp", 10_000) == self.LOOP, f"{label} lost the loop"

    def test_no_single_change_anywhere_costs_anything_it_does_not_own(self, state_directory):
        """The invariant every earlier version of this reader broke, asked of every case.

        A change may damage the one thing it touches - a device's own entry, the loop, or a whole
        section when the section itself is replaced - and nothing else: every other device keeps
        its state, its moment and its identity, the loop stays resumable, and no device appears
        that was not there. Asked of every key and field of every entry, of both sections, of keys
        added at every level including the top, and of devices named after every word the file
        uses, against every JSON type and every lookalike of a record, a section and an identity.

        Earlier sweeps checked one bystander device and never added keys, and passed against
        readers that lost devices; this one checks every bystander and the loop.
        """
        base = self._base()
        cases = 0
        for where, what, owner in self._changes():
            label = f"{'.'.join(where)}={'<deleted>' if what is self.DELETE else what!r}"
            log = self._stored(state_directory, self._apply(base, where, what, self.DELETE))
            self._assert_bystanders_intact(log, owner, label)
            cases += 1
        assert cases > 1000, f"the sweep shrank to {cases} cases, so it is no longer asking what it says"

    def test_and_a_write_keeps_whatever_the_read_kept(self, state_directory):
        """A misreading does its real damage at the next write, which puts it on disk.

        So for every change, the file written back after one loop update must read the same
        devices, moments and identities as the damaged file did.
        """
        base = self._base()
        for where, what, _owner in self._changes():
            label = f"{'.'.join(where)}={'<deleted>' if what is self.DELETE else what!r}"
            log = self._stored(state_directory, self._apply(base, where, what, self.DELETE))
            before = (log.states(), {name: log.elapsed(name, 2000.0) for name in log.states()}, log.identities())
            log.record_loop({"integral": 9.0}, "fp")
            after_log = TransitionLog("conservatory", state_directory.settings_file, clock=lambda: 2000.0)
            after = (
                after_log.states(),
                {name: after_log.elapsed(name, 2000.0) for name in after_log.states()},
                after_log.identities(),
            )
            assert after == before, f"{label} lost state across a write"

    def test_a_parameter_change_is_a_move_even_at_the_same_number(self, state_directory):
        """The no-move test compared only the value, so a device moved between parameters at
        the same number kept the old parameter for ever - and `_hold` then refused the record
        as being on a different scale every time, so that device never got its minimum back."""
        log = TransitionLog("conservatory", state_directory.settings_file, clock=lambda: 1000.0)
        record_command(
            log, {"devices": {"lamp": {"source": "hue", "device": "lamp", "parameter": "color_temp_k"}}}, {"lamp": 2700}
        )
        record_command(
            log,
            {"devices": {"lamp": {"source": "hue", "device": "lamp", "parameter": "brightness_pct"}}},
            {"lamp": 2700},
        )
        assert dict(log.identities()["lamp"]).get("parameter") == "brightness_pct", "the stale scale outlived it"

    def test_the_same_value_on_the_same_parameter_is_still_not_a_move(self, state_directory):
        """Or the minimum would restart every cycle and mean nothing at all."""
        moment = [1000.0]
        log = TransitionLog("conservatory", state_directory.settings_file, clock=lambda: moment[0])
        record_command(
            log, {"devices": {"lamp": {"source": "hue", "device": "lamp", "parameter": "brightness_pct"}}}, {"lamp": 40}
        )
        moment[0] += 60
        record_command(
            log, {"devices": {"lamp": {"source": "hue", "device": "lamp", "parameter": "brightness_pct"}}}, {"lamp": 40}
        )
        assert log.elapsed("lamp") == 60, "commanding an unchanged value restarted its clock"

    @pytest.mark.parametrize(
        "bad",
        [
            1,
            "hue",
            {"a": 1},
            True,
            [["source", "hue"], "bad"],
            [["parameter"]],
            [[1, 2, 3]],
            # `dict(["ab"])` is `{"a": "b"}`, so a two-character string passes a length check
            # and turns a corrupt file into a plausible-looking identity rather than a fault.
            ["ab"],
        ],
        ids=["int", "str", "dict", "bool", "ragged", "short-pair", "long-pair", "two-char-string"],
    )
    def test_a_corrupt_identity_is_dropped_without_taking_the_entry_with_it(self, bad, state_directory):
        """This file is a cache, so an unreadable one costs a transition sooner than asked -
        never a control that will not run. A corrupt `target` came back from `targets()` as a
        TypeError and took `get_control_state` and the next command with it.

        Normalised rather than discarded: the entry keeps its moment, so the device is still
        held for its minimum, and loses only the actuator it cannot prove - which makes
        `_hold` decline to pin it, so the device gets a fresh command. Dropping the entry
        would have thrown the timestamp away too and switched a device sooner than its
        document promised.
        """
        path = transition_path("conservatory", state_directory.settings_file)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"devices": {"lamp": {"state": 40, "at": 1000.0, "for": bad}}, "pid": {}}, handle)
        log = TransitionLog("conservatory", state_directory.settings_file, clock=lambda: 1060.0)
        assert log.identities() == {"lamp": None}
        # The reader the shape actually has to satisfy: `get_control_state` builds a dict out
        # of it, and a ragged identity raised there rather than reading as unusable.
        assert dict(log.identities()["lamp"] or ()) == {}
        assert log.elapsed("lamp") == 60.0, "the moment was thrown away with the actuator"
        assert log.states() == {"lamp": 40}

    @pytest.mark.parametrize(
        "at",
        [float("inf"), float("-inf"), float("nan"), True, "soon"],
        ids=["inf", "-inf", "nan", "bool", "string"],
    )
    def test_a_moment_that_is_not_a_moment_takes_its_entry_with_it(self, at, state_directory):
        """Discarded here, unlike a corrupt identity, because without a usable moment there is
        nothing left to keep: the entry exists to say *when*.

        An infinite `at` makes `now - at` negative for ever, which the backwards-clock clamp
        reads as "no time has passed" - so the device looks as though it has just moved, on
        every cycle, and its transition minimum freezes it permanently. A nan does the same by
        another route, because every comparison against it is False.
        """
        path = transition_path("conservatory", state_directory.settings_file)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"devices": {"lamp": {"state": 40, "at": at}}, "pid": {}}, handle)
        log = TransitionLog("conservatory", state_directory.settings_file, clock=lambda: 1_000_000.0)
        assert log.states() == {}, "an unusable moment was kept"
        assert log.frozen(lambda device: 600.0, ("lamp",)) == set(), "the device was frozen by a bad clock"

    @pytest.mark.parametrize("at", [float("inf"), float("nan"), True], ids=["inf", "nan", "bool"])
    def test_the_loop_half_refuses_the_same(self, at, state_directory):
        """The same rule, because the same file holds both and a reader of either can be
        handed a moment it cannot measure against."""
        path = transition_path("conservatory", state_directory.settings_file)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"devices": {}, "pid": {"integral": 5.0, "fingerprint": "abc", "at": at}}, handle)
        log = TransitionLog("conservatory", state_directory.settings_file, clock=lambda: 1_000_000.0)
        assert log.loop_state("abc", 3600.0) is None
        assert log.loop_age() is None

    def test_a_switched_device_repointed_elsewhere_is_not_frozen(self, state_directory):
        """The identity used to be checked only where driven devices are pinned, so a switched
        one stayed in `frozen` and `reachable_ladder` kept the rungs that leave it where the
        *old* target was - pinning the new switch to a state it never received until the
        minimum expired. Asked here now, where both kinds pass through.
        """
        from toinflux.controls import device_identity

        log = TransitionLog("conservatory", state_directory.settings_file, clock=lambda: 1000.0)
        was = {"source": "hue", "device": "old-plug"}
        now_points_at = {"source": "hue", "device": "new-plug"}
        log.record({"heater": True}, identities={"heater": device_identity(was)})
        minimum = lambda device: 600.0  # noqa: E731 - one line, used twice below

        assert log.frozen(minimum, ("heater",)) == frozenset({"heater"}), "the clock alone should hold it"
        assert (
            log.frozen(minimum, ("heater",), identities={"heater": device_identity(now_points_at)}) == frozenset()
        ), "a state recorded against another switch held the new one"
        assert log.frozen(minimum, ("heater",), identities={"heater": device_identity(was)}) == frozenset(
            {"heater"}
        ), "an unchanged device stopped being held"

    @pytest.mark.parametrize("at", [10**1000, -(10**1000)], ids=["huge", "hugely-negative"])
    def test_an_integer_too_large_to_convert_is_refused_not_raised(self, at, state_directory):
        """`math.isfinite` and `float` both raise OverflowError on an arbitrarily large JSON
        integer, and `10**1000` is a legal literal in a file somebody can edit. The guard that
        exists to keep a corrupt cache recoverable would have been the thing that stopped the
        control starting."""
        path = transition_path("conservatory", state_directory.settings_file)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"devices": {"lamp": {"state": 40, "at": %d}}, "pid": {}}' % at)
        log = TransitionLog("conservatory", state_directory.settings_file, clock=lambda: 1000.0)
        assert log.states() == {}, "an unconvertible moment was kept"

    @pytest.mark.parametrize(
        "integral",
        [True, float("nan"), float("inf"), "lots"],
        ids=["bool", "nan", "inf", "string"],
    )
    def test_a_loop_integral_it_cannot_use_is_not_offered_as_resumable(self, integral, state_directory):
        """A state returned from this reader is one the caller announces it has resumed.

        `resume_from` refuses a value it cannot use, but the announcement happens either way -
        so a nan left a control logging that it had put back a memory it had in fact
        discarded, `true` came back as an integral of 1.0, and a string broke the log line's
        own formatting. The device half already refuses what it cannot use.
        """
        path = transition_path("conservatory", state_directory.settings_file)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {"devices": {}, "pid": {"integral": integral, "fingerprint": "abc", "at": 1000.0}},
                handle,
            )
        log = TransitionLog("conservatory", state_directory.settings_file, clock=lambda: 1100.0)
        assert log.loop_state("abc", 3600.0) is None

    def test_a_usable_loop_integral_still_resumes(self, state_directory):
        """The other side, or this would have turned resumption off altogether."""
        path = transition_path("conservatory", state_directory.settings_file)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"devices": {}, "pid": {"integral": 5.0, "fingerprint": "abc", "at": 1000.0}}, handle)
        log = TransitionLog("conservatory", state_directory.settings_file, clock=lambda: 1100.0)
        assert (log.loop_state("abc", 3600.0) or {}).get("integral") == 5.0

    def test_both_halves_come_from_one_read(self, state_directory, monkeypatch):
        """They were read through separate opens, and `_read_loop` claimed in its own docstring
        that they could not disagree about which version of the file they came from. The
        control rewrites this file every cycle, so a reader landing between the two opens could
        pair one generation's devices with another's loop state."""
        path = transition_path("conservatory", state_directory.settings_file)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"devices": {"heater": {"state": True, "at": 1.0}}, "pid": {"integral": 5.0}}, handle)
        opens = []
        real = open

        def counted(*args, **kwargs):
            if args and str(args[0]) == path:
                opens.append(args[0])
            return real(*args, **kwargs)

        monkeypatch.setattr("builtins.open", counted)
        log = TransitionLog("conservatory", state_directory.settings_file)
        assert log.states() == {"heater": True}
        assert log.loop == {"integral": 5.0}
        assert len(opens) == 1, f"the file was opened {len(opens)} times, so the halves can disagree"


class TestAHeldDimmerKeepsTheValueItHas:
    """The two kinds of device are held still by different means, because "do not change"
    means different things to them. A switched one is kept where it is by planning the window
    only from the rungs that leave it there; a driven one has no rung to be kept on, so its
    minimum is honoured by commanding the value it already has.
    """

    @staticmethod
    def _control(installation):
        """Store a lamp control and return a running process for it.

        Args:
            installation (Installation): the installation to write into

        Returns:
            ControlProcess: built from the stored document
        """
        from tests.harness.installation import conservatory
        from toinflux.control_process import ControlProcess
        from toinflux.controls import save_control

        document = conservatory(name="lamp")
        document.pop("active_period", None)
        document.pop("enable_when", None)
        document["devices"] = {"lamp": {"source": "hue", "device": "far", "parameter": "brightness_pct"}}
        document["output"] = dict(
            document["output"],
            cycle_seconds=1,
            min_transition_seconds=600,
            stages=[{"level": 0, "set": {"lamp": 0}}, {"level": 1500, "set": {"lamp": 100}}],
        )
        document["output"].pop("max_level", None)
        save_control("lamp", document, installation.settings_file)
        return ControlProcess("lamp", settings_file=installation.settings_file)

    def test_it_is_commanded_its_last_value_rather_than_the_new_one(self, state_directory, bridge):
        control = self._control(state_directory)
        try:
            # Through the harness helper, which builds the identity from the document the
            # way `command_devices` does.
            record_command(control.transitions, control.document, {"lamp": 35})
            held = control.transitions.frozen(control.controller.min_transition_for, ("lamp",))
            assert held == frozenset({"lamp"}), "a 600s minimum did not hold a lamp moved a moment ago"
            plan = control._hold(
                tuple(
                    type(dwell)(stage=dwell.stage, seconds=dwell.seconds)
                    for dwell in control.controller.step({"inside": 5.0, "target": 20.0, "dew": 1.0}, dt=1)
                ),
                held & set(control.controller.driven),
            )
            assert {dwell.stage.states["lamp"] for dwell in plan} == {35}
        finally:
            control.guard.stop("the test is finished")
            control.close()

    def test_and_moves_freely_once_its_minimum_has_passed(self, state_directory, bridge):
        control = self._control(state_directory)
        try:
            control.transitions.record({"lamp": 35}, now=0.0)
            held = control.transitions.frozen(control.controller.min_transition_for, ("lamp",), now=1000.0)
            assert held == frozenset()
        finally:
            control.guard.stop("the test is finished")
            control.close()


class TestADrivenDevicesValueReachesTheLog:
    """Rendering a number as on/off threw the value away entirely: a lamp at 56% and the same
    lamp at 5% both logged as "on", which is the one thing the line exists to say."""

    def test_the_value_is_logged_rather_than_a_flag(self, state_directory, bridge, caplog):
        from toinflux.control_process import ControlProcess
        from toinflux.controls import save_control

        from tests.harness.installation import conservatory

        document = conservatory(name="lamp")
        document.pop("active_period", None)
        document.pop("enable_when", None)
        # A dimmable bulb rather than the default plug: `far` is on/off only, and the
        # capability check refuses brightness on it - correctly, and naming the control's own
        # device key, which is what the runtime half of the parameter validation is for.
        state_directory.bridge.lights["9"] = bulb("office-lamp")
        document["devices"] = {"lamp": {"source": "hue", "device": "office-lamp", "parameter": "brightness_pct"}}
        document["output"] = dict(
            document["output"],
            cycle_seconds=1,
            min_transition_seconds=1,
            stages=[{"level": 0, "set": {"lamp": 0}}, {"level": 1500, "set": {"lamp": 100}}],
        )
        document["output"].pop("max_level", None)
        save_control("lamp", document, state_directory.settings_file)
        control = ControlProcess("lamp", settings_file=state_directory.settings_file)
        try:
            with caplog.at_level(logging.DEBUG):
                control.cycle(dt=1, sleep=lambda _seconds: None)
        finally:
            control.guard.stop("the test is finished")
            control.close()
        commanding = [r.getMessage() for r in caplog.records if "commanding" in r.getMessage()]
        assert commanding, "the rung was never logged"
        assert "=on" not in commanding[0] and "=off" not in commanding[0], commanding[0]

    def test_a_switched_device_still_reads_as_on_or_off(self, state_directory, bridge, caplog):
        """A boolean is not more readable as 1 and 0."""
        from toinflux.control_process import ControlProcess
        from toinflux.controls import save_control

        from tests.harness.installation import conservatory

        document = conservatory(name="switched")
        document.pop("active_period", None)
        document.pop("enable_when", None)
        document["devices"] = {"heater": {"source": "hue", "device": "far"}}
        document["output"] = dict(
            document["output"],
            cycle_seconds=1,
            min_transition_seconds=1,
            stages=[{"level": 0, "set": {"heater": False}}, {"level": 1500, "set": {"heater": True}}],
        )
        document["output"].pop("max_level", None)
        save_control("switched", document, state_directory.settings_file)
        control = ControlProcess("switched", settings_file=state_directory.settings_file)
        try:
            with caplog.at_level(logging.DEBUG):
                control.cycle(dt=1, sleep=lambda _seconds: None)
        finally:
            control.guard.stop("the test is finished")
            control.close()
        commanding = [r.getMessage() for r in caplog.records if "commanding" in r.getMessage()]
        assert commanding and ("=on" in commanding[0] or "=off" in commanding[0]), commanding


class TestResumingTheLoopAfterARestart:
    """A slow plant spends a long time earning its integral, and a restart threw it away.

    The supervisor restarts a control on every document edit, so that was the ordinary cost
    of changing a setpoint by one degree: an hour of sitting below target while the loop
    earned back what it already knew.
    """

    FINGERPRINT = "abc123"

    def test_what_was_saved_comes_back(self, state_directory):
        log, now = _log(state_directory)
        log.record_loop({"integral": 42.0}, self.FINGERPRINT)
        reopened, _ = _log(state_directory, now=now)
        assert reopened.loop_state(self.FINGERPRINT, 300)["integral"] == 42.0

    def test_a_different_document_is_not_resumed(self, state_directory):
        """An integral is in the output's units, so the same number means one thing under one
        tuning and something else under another - and an edit is the commonest restart."""
        log, now = _log(state_directory)
        log.record_loop({"integral": 42.0}, self.FINGERPRINT)
        reopened, _ = _log(state_directory, now=now)
        assert reopened.loop_state("a-different-document", 300) is None

    def test_a_memory_older_than_its_welcome_is_not_resumed(self, state_directory):
        log, now = _log(state_directory)
        log.record_loop({"integral": 42.0}, self.FINGERPRINT)
        now[0] += 301
        reopened, _ = _log(state_directory, now=now)
        assert reopened.loop_state(self.FINGERPRINT, 300) is None

    def test_and_one_inside_it_is(self, state_directory):
        log, now = _log(state_directory)
        log.record_loop({"integral": 42.0}, self.FINGERPRINT)
        now[0] += 299
        reopened, _ = _log(state_directory, now=now)
        assert reopened.loop_state(self.FINGERPRINT, 300) is not None

    def test_a_clock_that_stepped_backwards_does_not_make_it_fresh(self, state_directory):
        log, now = _log(state_directory)
        log.record_loop({"integral": 42.0}, self.FINGERPRINT)
        now[0] -= 3600
        reopened, _ = _log(state_directory, now=now)
        assert reopened.loop_state(self.FINGERPRINT, 300) is None

    def test_nothing_stored_is_not_an_error(self, state_directory):
        log, _now = _log(state_directory)
        assert log.loop_state(self.FINGERPRINT, 300) is None

    def test_the_device_half_still_works_beside_it(self, state_directory):
        log, now = _log(state_directory)
        log.record({"heater": True})
        log.record_loop({"integral": 42.0}, self.FINGERPRINT)
        reopened, _ = _log(state_directory, now=now)
        assert reopened.states() == {"heater": True}
        assert reopened.loop_state(self.FINGERPRINT, 300)["integral"] == 42.0


class TestWhatTheControllerKeepsAndPutsBack:
    @staticmethod
    def _controller(**pid):
        """Return a controller over a simple ladder.

        Args:
            **pid: overrides for the pid section

        Returns:
            Controller: the controller
        """
        from toinflux.controller import Controller

        return Controller(
            {
                "parameters": {"target": 20.0},
                "inputs": {"inside": {"source": "hue", "field": "t"}},
                "pid": dict({"input": "inside", "setpoint": "target", "kp": 10.0, "ki": 1.0, "kd": 0.0}, **pid),
                "output": {
                    "cycle_seconds": 60,
                    "min_transition_seconds": 1,
                    "stages": [{"level": 0, "set": {"a": False}}, {"level": 100, "set": {"a": True}}],
                },
                "devices": {"a": {"source": "hue", "device": "A"}},
            }
        )

    def test_the_integral_is_what_is_kept(self, state_directory):
        controller = self._controller()
        controller.step({"inside": 18.0, "target": 20.0}, dt=60)
        assert controller.capture()["integral"] > 0

    def test_putting_it_back_shortens_the_climb(self, state_directory):
        # A small error and a gentle integral, so neither run saturates at the top rung -
        # a comparison where both are pinned at full output shows nothing, which is what an
        # earlier version of this test did.
        tuning = {"kp": 10.0, "ki": 0.1}
        warm = self._controller(**tuning)
        for _ in range(4):
            warm.step({"inside": 19.5, "target": 20.0}, dt=60)
        cold = self._controller(**tuning)
        first_cold = cold.step({"inside": 19.5, "target": 20.0}, dt=60)
        resumed = self._controller(**tuning)
        resumed.resume_from(warm.capture())
        # `resume_from` restores into a hold, so the loop is released before it is stepped -
        # which is what the cycle does, and what decides whether the memory is still current.
        resumed.resume()
        first_resumed = resumed.step({"inside": 19.5, "target": 20.0}, dt=60)
        cold_level = sum(d.stage.level * d.seconds for d in first_cold)
        warm_level = sum(d.stage.level * d.seconds for d in first_resumed)
        assert warm_level > cold_level, "resuming bought nothing"

    def test_an_integral_beyond_the_ladder_is_clamped_on_the_way_in(self, state_directory):
        """A file is a file. A number that escaped the range would command past the top rung."""
        controller = self._controller()
        controller.resume_from({"integral": 10_000_000.0})
        assert controller.pid._integral <= controller.ladder[-1].level

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), "42", None])
    def test_something_that_is_not_a_number_is_ignored(self, bad, state_directory):
        controller = self._controller()
        before = controller.pid._integral
        controller.resume_from({"integral": bad})
        assert controller.pid._integral == before

    def test_the_clock_is_not_restored(self, state_directory):
        """It belongs to this process. A timestamp from a previous one would make the first
        interval either enormous or negative depending on which way the clock had moved."""
        controller = self._controller()
        assert "last_time" not in controller.capture()

    def test_the_fingerprint_follows_the_things_the_integral_depends_on(self, state_directory):
        base = self._controller().fingerprint
        assert self._controller().fingerprint == base
        assert self._controller(kp=999.0).fingerprint != base
        assert self._controller(ki=999.0).fingerprint != base

    def test_the_fingerprint_follows_the_scale_and_the_cap_too(self, state_directory):
        """Both were missing, and both change what the integral means rather than merely how
        the control behaves.

        A device moved from `brightness_pct` to `color_temp_k` keeps its rung numbers while
        every one of them comes to mean something else, and a changed `max_level` changes the
        range the integral is clamped into. Either left the digest identical, so a loop earned
        against one scale was handed straight back for another.
        """
        from toinflux.controller import Controller

        def built(
            parameter="brightness_pct",
            cap=None,
            field="t",
            setpoint="target",
            reads="inside",
            target=20.0,
            device="A",
            instance=None,
            source="hue",
        ):
            output = {
                "cycle_seconds": 60,
                "min_transition_seconds": 1,
                "stages": [{"level": 0, "set": {"a": 0}}, {"level": 100, "set": {"a": 100}}],
            }
            if cap:
                output["max_level"] = cap
            return Controller(
                {
                    "parameters": {"target": target},
                    "inputs": {
                        "inside": {"source": "hue", "field": field},
                        "other": {"source": "hue", "field": "o"},
                    },
                    "pid": {"input": reads, "setpoint": setpoint, "kp": 10.0, "ki": 1.0, "kd": 0.0},
                    "output": output,
                    "devices": {
                        "a": {"source": source, "instance": instance, "device": device, "parameter": parameter}
                    },
                }
            ).fingerprint

        base = built()
        assert built() == base, "the same document did not agree with itself"
        assert built(parameter="color_temp_k") != base, "the scale changed and the loop was kept"
        assert built(cap="50") != base, "the cap changed and the loop was kept"
        assert built(reads="other") != base, "pid.input was repointed and the loop was kept"
        assert built(setpoint="target + 1") != base, "the setpoint changed and the loop was kept"
        # The same repointing one level down: the rule still says `inside`, but `inside` now
        # reads a different sensor, so every number the loop remembers describes something else.
        assert built(field="o") != base, "an input was repointed at another sensor and the loop was kept"
        # The commonest edit of all: the setpoint rule still says `target`, and `target` is a
        # different number. The integral is the accumulated error against the old one.
        assert built(target=30.0) != base, "the setpoint value changed and the loop was kept"
        # What a device points at, not only what it is driven by. Two lamps take the same
        # brightness ladder and are different plants, so an integral learned from one would
        # command the other from history that was never about it.
        assert built(device="Other") != base, "the actuator changed and the loop was kept"
        assert built(instance="bridge2") != base, "the bridge changed and the loop was kept"
        assert built(source="mqtt") != base, "the source changed and the loop was kept"

    def test_a_field_nobody_foresaw_discards_the_loop_by_default(self, state_directory):
        """The point of the whole inversion, and the only assertion here that could not have
        been written before it.

        This digest listed what mattered and the list was wrong eight times over: the ladder's
        scale, the cap, the input and setpoint rules, what those inputs read, the adjustable
        parameters, the actuator. It is now the document less a named few, so a key added to
        the format is covered without anybody remembering to add it.
        """
        from toinflux.controller import Controller

        document = {
            "parameters": {"target": 20.0},
            "inputs": {"inside": {"source": "hue", "field": "t"}},
            "pid": {"input": "inside", "setpoint": "target", "kp": 10.0, "ki": 1.0, "kd": 0.0},
            "output": {
                "cycle_seconds": 60,
                "min_transition_seconds": 1,
                "stages": [{"level": 0, "set": {"a": False}}, {"level": 100, "set": {"a": True}}],
            },
            "devices": {"a": {"source": "hue", "device": "A"}},
        }
        before = Controller(document).fingerprint
        document["output"]["a_knob_invented_after_this_test"] = 7
        assert Controller(document).fingerprint != before, "an unknown setting was silently ignored"

    @pytest.mark.parametrize("named", ["max_age", "min_transition_seconds"])
    def test_a_name_that_matches_an_excluded_setting_is_still_counted(self, named, state_directory):
        """The exclusions are positions in the format, not words.

        Stripping every key so called, wherever it appeared, also stripped a device or a
        parameter the operator had *named* `max_age` - removing that binding or that value
        from the digest entirely, which is the opposite of what the exclusion is for. A
        control's own names share a namespace with nothing.
        """
        from toinflux.controller import Controller

        def built(value):
            return Controller(
                {
                    "parameters": {named: value, "target": 20.0},
                    "inputs": {"inside": {"source": "hue", "field": "t"}},
                    "pid": {"input": "inside", "setpoint": "target", "kp": 10.0, "ki": 1.0, "kd": 0.0},
                    "output": {
                        "cycle_seconds": 60,
                        "min_transition_seconds": 1,
                        "stages": [{"level": 0, "set": {"a": False}}, {"level": 100, "set": {"a": True}}],
                    },
                    "devices": {"a": {"source": "hue", "device": "A"}},
                }
            ).fingerprint

        assert built(1) != built(2), f"a parameter named {named!r} was dropped from the digest"

    def test_and_not_the_things_it_does_not(self, state_directory):
        """Discarding a hard-won integral because a gate rule changed would throw away the
        settling time this exists to save."""
        from toinflux.controller import Controller

        document = {
            "parameters": {"target": 20.0},
            "inputs": {"inside": {"source": "hue", "field": "t"}, "outside": {"source": "hue", "field": "o"}},
            "pid": {"input": "inside", "setpoint": "target", "kp": 10.0, "ki": 1.0, "kd": 0.0},
            "output": {
                "cycle_seconds": 60,
                "min_transition_seconds": 1,
                "stages": [{"level": 0, "set": {"a": False}}, {"level": 100, "set": {"a": True}}],
            },
            "devices": {"a": {"source": "hue", "device": "A"}},
        }
        before = Controller(document).fingerprint
        document["enable_when"] = "outside < 15"
        document["safe_state"] = "leave_unchanged"
        assert Controller(document).fingerprint == before

    def test_it_is_stable_across_processes(self, state_directory):
        """Built-in hash() is salted per process, so a fingerprint written by one control
        would never match the one that read it back."""
        import subprocess

        code = (
            "import sys; sys.path.insert(0, '.');"
            "from toinflux.controls import CONTROL_EXAMPLES;"
            "from toinflux.controller import Controller;"
            "print(Controller(CONTROL_EXAMPLES['normal']['document']).fingerprint)"
        )
        runs = {
            subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
            for _ in range(2)
        }
        assert len(runs) == 1, f"the fingerprint changed between processes: {runs}"


class TestACycleWritesDownWhatItLearned:
    """Driving `cycle` rather than calling the store directly.

    Every other test here calls `record_loop` itself, which says nothing about whether the
    loop ever does - and deleting the call from `_spend_window` passed all of them. Fourth
    time on this branch that a guard has been correct and unreached.
    """

    def test_the_loop_state_lands_in_the_file(self, state_directory, bridge):
        from toinflux.control_process import ControlProcess

        name = TestAMinimumLongerThanTheWindowIsKept._control(state_directory, minimum=1, cycle=1)
        control = ControlProcess(name, settings_file=state_directory.settings_file)
        try:
            control.cycle(dt=1, sleep=lambda _seconds: None)
        finally:
            control.guard.stop("the test is finished")
            control.close()
        with open(transition_path(name, state_directory.settings_file), encoding="utf-8") as handle:
            stored = json.load(handle)
        assert "pid" in stored, f"a cycle ran and stored no loop state: {sorted(stored)}"
        assert "integral" in stored["pid"], stored["pid"]
        assert stored["pid"]["fingerprint"] == control.controller.fingerprint

    def test_and_the_devices_are_still_beside_it(self, state_directory, bridge):
        """One file, two halves. A change to either must not lose the other."""
        from toinflux.control_process import ControlProcess

        name = TestAMinimumLongerThanTheWindowIsKept._control(state_directory, minimum=1, cycle=1)
        control = ControlProcess(name, settings_file=state_directory.settings_file)
        try:
            control.cycle(dt=1, sleep=lambda _seconds: None)
        finally:
            control.guard.stop("the test is finished")
            control.close()
        with open(transition_path(name, state_directory.settings_file), encoding="utf-8") as handle:
            stored = json.load(handle)
        assert stored["devices"], "the device half was lost when the loop half was written"
