"""The record of when each device last moved, and what it forbids.

``min_transition_seconds`` used to be kept only inside a cycle window, so a minimum longer
than the window could not be honoured and was refused by validation. These are the tests
that make it mean what it says: across windows, and across a restart, which is the case the
earlier reasoning gave up on.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import json
import logging
import os
import sys

import pytest

from tests.harness.bridge import bulb
from toinflux.exceptions import ConfigError
from toinflux.staging import build_ladder, plan_window, reachable_ladder
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

    def test_an_entry_with_no_usable_moment_is_dropped_and_the_rest_kept(self, state_directory):
        """One corrupt entry is not a reason to forget every device's history."""
        path = transition_path("conservatory", state_directory.settings_file)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"good": {"state": True, "at": 10.0}, "bad": {"state": True, "at": "soon"}}, handle)
        log = TransitionLog("conservatory", state_directory.settings_file)
        assert set(log.entries) == {"good"}

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
    """Two halves of one document, and an older shape that has neither."""

    def test_a_flat_log_with_a_device_called_pid_survives_the_upgrade(self, state_directory):
        """A device may legitimately be named `pid` or `devices`. Treating either name as a
        section header read the rest of the file as nothing and dropped every other device's
        entry, on the one upgrade that had to be seamless. The writer emits both keys, so both
        are required before a file is read as the newer shape."""
        path = transition_path("conservatory", state_directory.settings_file)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"pid": {"state": True, "at": 1000.0}, "heater": {"state": False, "at": 1000.0}}, handle)
        log = TransitionLog("conservatory", state_directory.settings_file, clock=lambda: 1000.0)
        assert log.states() == {"pid": True, "heater": False}

    def test_a_flat_log_with_devices_called_pid_and_devices_survives_too(self, state_directory):
        """Requiring both keys is still not enough: a log holding devices named *both* would
        have its two entries read as the sections and every device in the file lost. What
        separates them is the shape - a section's values are entries, an entry's values are a
        state and a moment."""
        path = transition_path("conservatory", state_directory.settings_file)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"pid": {"state": True, "at": 1000.0}, "devices": {"state": False, "at": 1000.0}}, handle)
        log = TransitionLog("conservatory", state_directory.settings_file, clock=lambda: 1000.0)
        assert log.states() == {"pid": True, "devices": False}
        assert log.loop == {}

    def test_a_parameter_change_is_a_move_even_at_the_same_number(self, state_directory):
        """The no-move test compared only the value, so a device moved between parameters at
        the same number kept the old parameter for ever - and `_hold` then refused the record
        as being on a different scale every time, so that device never got its minimum back."""
        log = TransitionLog("conservatory", state_directory.settings_file, clock=lambda: 1000.0)
        log.record({"lamp": 2700}, parameters={"lamp": "color_temp_k"})
        log.record({"lamp": 2700}, parameters={"lamp": "brightness_pct"})
        assert log.parameters() == {"lamp": "brightness_pct"}, "the stale scale outlived the change"

    def test_the_same_value_on_the_same_parameter_is_still_not_a_move(self, state_directory):
        """Or the minimum would restart every cycle and mean nothing at all."""
        moment = [1000.0]
        log = TransitionLog("conservatory", state_directory.settings_file, clock=lambda: moment[0])
        log.record({"lamp": 40}, parameters={"lamp": "brightness_pct"})
        moment[0] += 60
        log.record({"lamp": 40}, parameters={"lamp": "brightness_pct"})
        assert log.elapsed("lamp") == 60, "commanding an unchanged value restarted its clock"

    @pytest.mark.parametrize("bad", [1, "hue", {"a": 1}, True], ids=["int", "str", "dict", "bool"])
    def test_a_corrupt_actuator_is_dropped_without_taking_the_entry_with_it(self, bad, state_directory):
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
            json.dump({"devices": {"lamp": {"state": 40, "at": 1000.0, "target": bad}}, "pid": {}}, handle)
        log = TransitionLog("conservatory", state_directory.settings_file, clock=lambda: 1060.0)
        assert log.targets() == {"lamp": None}
        assert log.elapsed("lamp") == 60.0, "the moment was thrown away with the actuator"
        assert log.states() == {"lamp": 40}

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
            # With the parameter, as `command_devices` records it: the state and the scale it
            # is on are only meaningful together.
            control.transitions.record(
                {"lamp": 35},
                parameters={"lamp": "brightness_pct"},
                targets={"lamp": ("hue", None, "far")},
            )
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

    def test_a_file_from_before_this_existed_is_still_read(self, state_directory):
        """An installation upgrading in place has the older flat shape on disk. Refusing it
        would cost every device one transition sooner than its minimum asks."""
        path = transition_path("conservatory", state_directory.settings_file)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"heater": {"state": True, "at": 10.0}}, handle)
        log = TransitionLog("conservatory", state_directory.settings_file)
        assert log.states() == {"heater": True}
        assert log.loop_state(self.FINGERPRINT, 300) is None


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
