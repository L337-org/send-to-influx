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

import pytest

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
