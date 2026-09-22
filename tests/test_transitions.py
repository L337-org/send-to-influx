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
import os

import pytest

from toinflux.exceptions import ConfigError
from toinflux.staging import build_ladder, reachable_ladder
from toinflux.transitions import EARLY_FRACTION, TransitionLog, forget_control, transition_path


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
        assert log.frozen(lambda _d: 600, ("heater",), 60) == frozenset()


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
        assert second.frozen(lambda _d: 120, ("heater",), 10) == frozenset({"heater"})

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
        assert restarted.frozen(lambda _d: 120, ("heater",), 10) == frozenset({"heater"})


class TestTheEarlyAllowance:
    """Without it the check is asked once per window and always rounds the minimum up to the
    next whole one, which is invisible and permanent."""

    def test_a_minimum_that_comes_due_early_in_the_window_is_allowed_now(self, state_directory):
        # 100s elapsed of a 120s minimum, with a 60s window: the next chance to ask is at
        # 160s, so waiting would turn a 120s minimum into 160.
        log, now = _log(state_directory)
        log.record({"heater": True})
        now[0] += 100
        assert log.frozen(lambda _d: 120, ("heater",), 60) == frozenset()

    def test_a_minimum_with_most_of_the_window_still_to_run_is_not(self, state_directory):
        log, now = _log(state_directory)
        log.record({"heater": True})
        now[0] += 60
        assert log.frozen(lambda _d: 120, ("heater",), 60) == frozenset({"heater"})

    def test_the_allowance_is_bounded_by_the_window(self, state_directory):
        """It is a fraction of the coming window rather than a constant, so a short cycle
        cannot let a device move long before its minimum."""
        log, now = _log(state_directory)
        log.record({"heater": True})
        now[0] += 120 - (2 * EARLY_FRACTION)
        assert log.frozen(lambda _d: 120, ("heater",), 2) == frozenset()
        assert log.frozen(lambda _d: 240, ("heater",), 2) == frozenset({"heater"})


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
        assert log.frozen(lambda _d: 120, ("heater",), 10) == frozenset({"heater"})


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
            frozen = control.transitions.frozen(control.controller.min_transition_for, ("far",), control.cycle_seconds)
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
