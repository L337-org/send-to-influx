"""Tests for one control's closed loop.

Run against a simulated plant rather than a room, so convergence, overshoot and the effect
of a cap are observable in milliseconds. The plant is deliberately crude - a room is not
first-order - which is enough to show the loop working and to make a regression that
introduces oscillation visible.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT"

import math

import pytest

from toinflux.controls import DEFAULT_CYCLE_SECONDS
from toinflux.controller import RESUMABLE_HOLD_SECONDS, Controller, first_order_plant, simulate
from toinflux.exceptions import ConfigError
from toinflux.rules import RuleEvaluationError

CYCLE = 900

STAGES = [
    {"level": 0, "set": {"far": False, "near": False}},
    {"level": 750, "set": {"far": True, "near": False}},
    {"level": 1500, "set": {"far": True, "near": True}},
]


def _document(**overrides):
    """A conservatory control, with the tunings that converge against the plant below."""
    document = {
        "inputs": {"inside": {"source": "hue", "field": "temperature_conservatory"}},
        "parameters": {"target": 18.0},
        "pid": {"input": "inside", "setpoint": "target", "kp": 60.0, "ki": 0.02, "kd": 0.0},
        # min_transition_seconds is omitted rather than set to 0: the store requires it to
        # be positive when present, so a fixture carrying 0 would be a document
        # validate_control refuses - and a test built on one proves less than it looks.
        "output": {"cycle_seconds": CYCLE, "stages": STAGES},
        "devices": {
            "far": {"source": "hue", "device": "Conservatory heater far"},
            "near": {"source": "hue", "device": "Conservatory heater near"},
        },
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(document.get(key), dict):
            document[key] = {**document[key], **value}
        else:
            document[key] = value
    return document


def _plant(start=10.0):
    return first_order_plant(start=start, gain=0.004, loss=0.06, ambient=8.0)


def _run(controller, cycles, target=18.0, plant=None):
    return simulate(
        controller,
        plant or _plant(),
        cycles,
        lambda pv: {"inside": pv, "target": target},
        dt=CYCLE,
    )


class TestTheLoopControls:
    def test_it_converges_on_the_setpoint(self):
        trace = _run(Controller(_document()), 60)
        settled = [pv for pv, _ in trace[-10:]]
        assert settled[-1] == pytest.approx(18.0, abs=0.05)
        assert max(settled) - min(settled) < 0.1, f"still moving: {settled}"

    def test_a_window_delivers_the_average_of_its_dwells(self):
        """Which is the point of time-proportioning: the room sees one level for the window,
        not a burst at 1500 followed by nothing. The first window from cold averages a level
        that is on no rung of the ladder, which is only possible because it is split.
        """
        trace = _run(Controller(_document()), 1)
        level = trace[0][1]
        assert level not in (0.0, 750.0, 1500.0), f"{level} landed on a rung, so nothing was proportioned"
        assert 0.0 < level < 1500.0

    def test_the_setpoint_is_a_rule_evaluated_every_cycle(self):
        """It is not a constant: the motivating case is max(target, dew + 5), where the
        dew point moves under the control while it runs."""
        controller = Controller(
            _document(pid={"setpoint": "max(target, dew + 5)"}, inputs={"dew": {"source": "openmeteo", "field": "dew"}})
        )
        plant = _plant()
        hot = simulate(controller, plant, 20, lambda pv: {"inside": pv, "target": 18.0, "dew": 30.0}, dt=CYCLE)
        assert hot[-1][0] > 18.5, "a dew point of 30 should have lifted the setpoint well above target"


class TestAntiWindup:
    def test_an_unreachable_setpoint_does_not_accumulate_an_unpayable_demand(self):
        """simple-pid clamps the integral to output_limits, and those are the ladder's own
        range - which is what makes it anti-windup rather than decoration.

        The setpoint has to be genuinely out of reach. Written first against 60 degrees,
        which this plant reaches at full power (its equilibrium is about 108), so the error
        went negative, the integral unwound on its own and the test passed with the limits
        removed. 300 degrees cannot be reached at any level.

        Measured with the limits removed, the failure is not subtle: the integral reaches
        165,166, the recovery sits at full for every one of sixty cycles, and the room runs
        to 107 degrees. That is hours of heaters full on because of arithmetic from a period
        when they were already doing everything they could.
        """
        controller = Controller(_document())
        plant = _plant()
        _run(controller, 40, target=300.0, plant=plant)
        recovery = _run(controller, 60, target=12.0, plant=plant)
        assert recovery[0][1] == 0.0, "should let go immediately, not work off a backlog"
        at_full = [level for _, level in recovery if level == 1500.0]
        assert not at_full, f"spent {len(at_full)} cycles at full working off the integral"
        # The room is legitimately hot at the start of recovery - it was being heated at full
        # for forty cycles - so the signal is what the loop *asks for* from here, and where
        # it ends up. Asserting on peak temperature instead fails either way.
        assert recovery[-1][0] == pytest.approx(12.0, abs=1.0), "never came back down"

    def test_the_limits_are_the_ladder_not_a_guess(self):
        controller = Controller(_document())
        assert controller.pid.output_limits == (0.0, 1500.0)


class TestABadCycleDoesNotPoisonTheLoop:
    """One non-finite reading permanently ruins a PID, which is why this is checked before
    the value reaches it rather than after.

    Measured on simple-pid directly: feed one nan, and the integral is nan from then on -
    three clean readings afterwards all return nan. A control that fails safe for ever
    because of one bad rule evaluation is worse than one that skips a cycle.
    """

    @pytest.mark.parametrize("slot", ["setpoint", "input"])
    def test_a_non_finite_rule_value_is_refused(self, slot):
        """The rule language produces these from finite inputs: 1e400 is inf, and
        1e400 - 1e400 is nan, so nothing upstream has to be wrong."""
        controller = Controller(_document(pid={slot: "1e400 - 1e400"}))
        with pytest.raises(RuleEvaluationError, match=f"pid.{slot}"):
            controller.step({"inside": 17.0, "target": 18.0}, dt=CYCLE)

    @pytest.mark.parametrize(
        "slot,good",
        [pytest.param("setpoint", "target", id="setpoint"), pytest.param("input", "inside", id="input")],
    )
    def test_the_loop_still_works_on_the_next_good_cycle(self, slot, good):
        """The property the ordering exists for, rather than the check itself. A controller
        that raised but had already handed the value to the PID would be dead for ever."""
        controller = Controller(
            _document(
                pid={slot: f"if(poison > 0, 1e400 - 1e400, {good})"},
                inputs={"poison": {"source": "hue", "field": "poison"}},
            )
        )
        with pytest.raises(RuleEvaluationError):
            controller.step({"inside": 17.0, "target": 18.0, "poison": 1.0}, dt=CYCLE)
        plan = controller.step({"inside": 17.0, "target": 18.0, "poison": 0.0}, dt=CYCLE)
        level = sum(d.stage.level * d.seconds for d in plan) / sum(d.seconds for d in plan)
        assert math.isfinite(level), "the loop never recovered"
        assert level > 0, "a degree below target should still ask for heat"

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0, -900, "900", True])
    def test_an_unusable_interval_is_refused_before_the_pid_sees_it(self, bad):
        """dt reaches the PID too, so the check that everything is validated first has to
        include it or the comment saying so is not true.

        A nan poisons the PID exactly as a nan reading does - measured, and the next healthy
        cycle still returns nan - while zero or a negative raises simple-pid's own
        ValueError, a bare built-in crossing this module's boundary.
        """
        controller = Controller(_document())
        with pytest.raises(RuleEvaluationError, match="interval since the last cycle"):
            controller.step({"inside": 17.0, "target": 18.0}, dt=bad)

    def test_the_loop_survives_an_unusable_interval(self):
        """The property, again: refusing is only useful if the next good cycle works."""
        controller = Controller(_document())
        controller.step({"inside": 17.0, "target": 18.0}, dt=CYCLE)
        with pytest.raises(RuleEvaluationError):
            controller.step({"inside": 17.0, "target": 18.0}, dt=float("nan"))
        plan = controller.step({"inside": 17.0, "target": 18.0}, dt=CYCLE)
        level = sum(d.stage.level * d.seconds for d in plan) / sum(d.seconds for d in plan)
        assert math.isfinite(level) and level > 0, "the loop never recovered"

    def test_a_cap_that_cannot_be_evaluated_is_this_cycle_not_this_control(self):
        """RuleEvaluationError rather than ConfigError, so the fail-safe covers the cycle
        and the control is still there next time."""
        controller = Controller(_document(output={"max_level": "1e400 - 1e400"}))
        with pytest.raises(RuleEvaluationError, match="not a level to cap at"):
            controller.step({"inside": 17.0, "target": 18.0}, dt=CYCLE)

    def test_the_setpoint_is_not_left_changed_by_a_refused_cycle(self):
        """Nothing is mutated before everything is checked, so a cycle that cannot run
        leaves the controller exactly as it was."""
        controller = Controller(
            _document(
                pid={"input": "if(poison > 0, 1e400 - 1e400, inside)"},
                inputs={"poison": {"source": "hue", "field": "poison"}},
            )
        )
        controller.step({"inside": 17.0, "target": 18.0, "poison": 0.0}, dt=CYCLE)
        before = controller.pid.setpoint
        with pytest.raises(RuleEvaluationError):
            controller.step({"inside": 17.0, "target": 99.0, "poison": 1.0}, dt=CYCLE)
        assert controller.pid.setpoint == before, "a refused cycle moved the setpoint"


class TestTheCap:
    def test_a_cap_holds_the_control_to_the_rungs_below_it(self):
        """The motivating case: grid carbon is high, so one heater rather than two.

        Started from freezing so the demand genuinely wants more than the cap. Written first
        from a ten-degree start, where the loop never asked for more than 624 anyway - the
        assertion passed without the cap doing anything, which the contrast below now
        catches.
        """
        controller = Controller(
            _document(
                output={"max_level": "if(grid_co2 > 300, 750, 1500)"},
                inputs={"grid_co2": {"source": "openmeteo", "field": "grid_co2"}},
            )
        )
        trace = simulate(
            controller,
            _plant(start=0.0),
            30,
            lambda pv: {"inside": pv, "target": 18.0, "grid_co2": 400.0},
            dt=CYCLE,
        )
        capped = max(level for _, level in trace)
        assert capped <= 750.0, f"a window averaged {capped}, above the cap"

        uncapped = simulate(
            Controller(_document()),
            _plant(start=0.0),
            30,
            lambda pv: {"inside": pv, "target": 18.0},
            dt=CYCLE,
        )
        assert max(level for _, level in uncapped) > 750.0, "the run never wanted more than the cap anyway"

    def test_the_limits_follow_the_cap(self):
        """Left at the full ladder's range, the integral would keep accumulating towards a
        level the cap has just forbidden, and every capped cycle would be paid back as
        overshoot the moment it lifted."""
        controller = Controller(
            _document(output={"max_level": "cap"}, inputs={"cap": {"source": "openmeteo", "field": "cap"}})
        )
        controller.step({"inside": 10.0, "target": 18.0, "cap": 750.0}, dt=CYCLE)
        assert controller.pid.output_limits == (0.0, 750.0)
        controller.step({"inside": 10.0, "target": 18.0, "cap": 1500.0}, dt=CYCLE)
        assert controller.pid.output_limits == (0.0, 1500.0)

    def test_lifting_a_cap_does_not_produce_a_backlog(self):
        """The behaviour the moving limits exist for, rather than the mechanism."""
        controller = Controller(
            _document(output={"max_level": "cap"}, inputs={"cap": {"source": "openmeteo", "field": "cap"}})
        )
        plant = _plant()
        capped = simulate(controller, plant, 30, lambda pv: {"inside": pv, "target": 18.0, "cap": 750.0}, dt=CYCLE)
        assert max(level for _, level in capped) <= 750.0
        lifted = simulate(controller, plant, 30, lambda pv: {"inside": pv, "target": 18.0, "cap": 1500.0}, dt=CYCLE)
        assert lifted[-1][0] == pytest.approx(18.0, abs=0.5), "should settle, not overshoot off a backlog"
        assert max(pv for pv, _ in lifted) < 19.5, "overshot, which is what a cap backlog looks like"


class TestHoldingAndResuming:
    def test_holding_stops_the_integral_moving(self):
        """A control outside its active period is not controlling anything, so an error
        measured against a setpoint nobody is chasing is not information."""
        controller = Controller(_document())
        _run(controller, 5)
        controller.hold()
        before = controller.pid.components
        for _ in range(20):
            controller.pid(5.0, dt=CYCLE)
        assert controller.pid.components == before

    def test_resuming_starts_from_where_it_is_told_rather_than_zero(self):
        """Which is what stops a heater slamming on at the start of every active period."""
        controller = Controller(_document())
        controller.hold()
        controller.resume(last_output=600.0)
        plan = controller.step({"inside": 18.0, "target": 18.0}, dt=CYCLE)
        level = sum(d.stage.level * d.seconds for d in plan) / sum(d.seconds for d in plan)
        assert level == pytest.approx(600.0, abs=1.0)


class TestMinimumTransition:
    def test_a_device_override_beats_the_control_default(self):
        """The constraint belongs to the hardware: one heater on a contactor may need
        minutes where a smart plug beside it does not care."""
        controller = Controller(
            _document(
                output={"min_transition_seconds": 300},
                devices={
                    "far": {"source": "hue", "device": "far", "min_transition_seconds": 900},
                    "near": {"source": "hue", "device": "near"},
                },
            )
        )
        assert controller.min_transition_for("far") == 900.0
        assert controller.min_transition_for("near") == 300.0

    def test_an_unknown_device_takes_the_default(self):
        """Rather than raising: the store has already refused a stage naming a device the
        control does not own, so reaching here with one is a programming error, and failing
        the cycle over it would be worse than being cautious."""
        assert Controller(_document(output={"min_transition_seconds": 42})).min_transition_for("nope") == 42.0

    def test_the_fixture_is_a_document_the_store_would_accept(self):
        """Otherwise these tests describe a control that could never exist."""
        from toinflux.controls import validate_control

        document = {"enabled": True, **_document(), "safe_state": "unenergised"}
        assert validate_control("conservatory", document) == []


class TestBuildingOne:
    def test_a_missing_setpoint_rule_is_a_config_error(self):
        document = _document()
        del document["pid"]["setpoint"]
        with pytest.raises(ConfigError, match="pid.setpoint"):
            Controller(document)

    def test_a_missing_input_rule_is_a_config_error(self):
        document = _document()
        del document["pid"]["input"]
        with pytest.raises(ConfigError, match="pid.input"):
            Controller(document)

    def test_max_level_is_optional(self):
        assert Controller(_document())._max_level_rule is None

    def test_a_rule_naming_an_undeclared_input_is_refused_at_build_time(self):
        """Not at three in the morning. The parser resolves names against what the control
        declared, so this is a --check-config failure rather than a runtime surprise."""
        with pytest.raises(ConfigError):
            Controller(_document(pid={"setpoint": "nosuchthing + 1"}))

    def test_every_call_is_honoured(self):
        """simple-pid's default sample_time returns the previous output for a call arriving
        too soon. For a loop running every fifteen minutes that would mean discarding a
        reading rather than acting on it, so there is no sample time."""
        assert Controller(_document()).pid.sample_time is None


class TestThePlant:
    def test_it_rises_with_applied_level_and_falls_towards_ambient(self):
        plant = first_order_plant(start=10.0, gain=0.004, loss=0.06, ambient=8.0)
        assert plant(None) == 10.0
        assert plant(1500) > 10.0
        cooling = first_order_plant(start=20.0, gain=0.004, loss=0.06, ambient=8.0)
        assert cooling(0) < 20.0

    def test_it_refuses_to_be_driven_with_a_non_finite_level(self):
        """A nan reaching the plant would make every later assertion about the trace
        meaningless, and nan comparisons are all False so nothing downstream would notice."""
        plant = first_order_plant(start=10.0, gain=0.004, loss=0.06, ambient=8.0)
        with pytest.raises(ValueError):
            plant(float("nan"))


class TestAnOmittedCycleWindow:
    """`cycle_seconds` is optional and three readers need it - the loop, the process and the
    supervisor's stall threshold. Two defaulted to 900 and this one left it None, so a
    document that passed `--check-config` raised ConfigError on its first cycle - and a
    ConfigError is how a control says no retry will help, so it died permanently on a
    document it had just been told was fine."""

    def test_a_document_without_one_still_runs_a_cycle(self):
        document = _document()
        del document["output"]["cycle_seconds"]
        plan = Controller(document).step({"inside": 17.0, "target": 18.0}, dt=CYCLE)
        assert plan, "the first cycle produced no plan"

    def test_it_takes_the_same_default_as_everything_else(self):
        document = _document()
        del document["output"]["cycle_seconds"]
        assert Controller(document).cycle_seconds == DEFAULT_CYCLE_SECONDS

    def test_the_three_readers_agree(self):
        """Read from the one definition rather than compared to a literal, so this cannot
        pass while two of them drift apart."""
        from toinflux.control_process import DEFAULT_CYCLE_SECONDS as from_process
        from toinflux.supervision import stall_seconds

        document = _document()
        del document["output"]["cycle_seconds"]
        assert Controller(document).cycle_seconds == from_process
        assert stall_seconds(document) == from_process * 3


class TestHoldTakesNoLastOutput:
    """`hold` used to accept `last_output` and pass it to `set_auto_mode(False, ...)`,
    copying `resume`'s wording. simple-pid reads that argument only in the branch that
    *enables* the controller, so disabling with one discarded it silently - a caller
    trusting the docstring would have got an unannounced actuation level."""

    def test_it_is_not_in_the_signature(self):
        import inspect

        from toinflux.controller import Controller

        assert "last_output" not in inspect.signature(Controller.hold).parameters

    def test_the_library_still_only_reads_it_when_enabling(self):
        """Pinned against the installed version rather than assumed, because the whole point
        is that the docstring and the library had drifted apart."""
        import inspect

        from simple_pid import PID

        source = inspect.getsource(PID.set_auto_mode)
        enabling = source.index("if enabled and not self._auto_mode")
        assert source.index("last_output if") > enabling, "simple-pid now reads last_output when disabling too"

    def test_resume_still_takes_one(self):
        """It is a real option there, which is where the library reads it."""
        import inspect

        from toinflux.controller import Controller

        assert "last_output" in inspect.signature(Controller.resume).parameters


class TestABriefHoldDoesNotCostTheLoopWhatItLearned:
    """A cycle that cannot read its input calls `hold` through the fail-safe, and the next
    healthy cycle used to call `resume` - which resets simple-pid and zeroes the integral.

    Measured on a real install: a control holding a lamp at full output dropped to 64% on a
    single flaky read, with the error unchanged and large. The loop then spent minutes
    earning back what it already knew, and the drop looked from outside like a hidden
    anti-windup mechanism rather than lost state.
    """

    @staticmethod
    def _mixed(max_level=None):
        """Return a controller owning one switched device and one driven by a percentage.

        Args:
            max_level (str or None): a cap rule, where the test wants one

        Returns:
            Controller: built from a two-rung ladder moving both devices
        """
        output = {
            "cycle_seconds": 60,
            "min_transition_seconds": 10,
            "stages": [
                {"level": 0, "set": {"lamp": 0, "heater": False}},
                {"level": 1000, "set": {"lamp": 100, "heater": True}},
            ],
        }
        if max_level:
            output["max_level"] = max_level
        return Controller(
            {
                "parameters": {"target": 1000},
                "inputs": {"lux": {"source": "hue", "field": "L"}},
                "pid": {"input": "lux", "setpoint": "target", "kp": 1.0, "ki": 0.0, "kd": 0.0},
                "output": output,
                "devices": {
                    "lamp": {"source": "hue", "device": "Lamp", "parameter": "brightness_pct"},
                    "heater": {"source": "hue", "device": "Heater"},
                },
            }
        )

    @staticmethod
    def _settled(clock):
        """Return a controller with an integral already built.

        Args:
            clock (list): a one-element list holding the current time

        Returns:
            Controller: settled against a steady error
        """
        controller = Controller(
            {
                "parameters": {"target": 1000},
                "inputs": {"lux": {"source": "hue", "field": "L"}},
                "pid": {"input": "lux", "setpoint": "target", "kp": 0.05, "ki": 0.0007, "kd": 0},
                "output": {
                    "cycle_seconds": 60,
                    "min_transition_seconds": 15,
                    "stages": [{"level": 0, "set": {"lamp": 0}}, {"level": 100, "set": {"lamp": 100}}],
                },
                "devices": {"lamp": {"source": "hue", "device": "L", "parameter": "brightness_pct"}},
            },
            time_fn=lambda: clock[0],
        )
        for _ in range(4):
            clock[0] += 60
            controller.step({"lux": 300.0, "target": 1000}, dt=60)
        return controller

    def test_a_one_cycle_fail_safe_keeps_the_integral(self):
        clock = [0.0]
        controller = self._settled(clock)
        before = controller.pid._integral
        assert before > 0, "nothing was learned, so this proves nothing"
        controller.hold()
        clock[0] += 60
        controller.resume()
        assert controller.pid._integral == pytest.approx(before)

    def test_and_the_output_does_not_drop(self):
        clock = [0.0]
        controller = self._settled(clock)
        before = controller.step({"lux": 300.0, "target": 1000}, dt=60)[0].stage.states["lamp"]
        controller.hold()
        clock[0] += 60
        controller.resume()
        after = controller.step({"lux": 300.0, "target": 1000}, dt=60)[0].stage.states["lamp"]
        assert after == pytest.approx(before), f"output fell from {before} to {after} on an unchanged input"

    def test_a_long_hold_still_starts_afresh(self):
        """An active period lasts eighteen hours, and what the room was doing last night says
        nothing about this evening."""
        clock = [0.0]
        controller = self._settled(clock)
        controller.hold()
        clock[0] += RESUMABLE_HOLD_SECONDS + 1
        controller.resume()
        assert controller.pid._integral == 0

    def test_a_long_outage_holding_every_cycle_still_starts_afresh(self):
        """The test above holds once, which is not what an outage looks like.

        The fail-safe holds on *every* failing cycle, and each call used to restamp the
        clock - so the age measured the gap since the last failure, about one cycle, however
        long the outage ran. Six hours of failing cycles then handed back a six-hour-old
        integral, which is the one thing RESUMABLE_HOLD_SECONDS exists to refuse.
        """
        clock = [0.0]
        controller = self._settled(clock)
        assert controller.pid._integral > 0, "nothing was learned, so this proves nothing"
        for _ in range(int(6 * 3600 / 30)):
            controller.hold()
            clock[0] += 30
        controller.resume()
        assert controller.pid._integral == 0

    def test_a_short_outage_holding_every_cycle_still_carries_on(self):
        """The other side of it: repeated holds must not make a brief outage look long
        either, or the fix would be a different bug."""
        clock = [0.0]
        controller = self._settled(clock)
        before = controller.pid._integral
        for _ in range(10):
            controller.hold()
            clock[0] += 30
        controller.resume()
        assert controller.pid._integral == pytest.approx(before)

    def test_a_restored_loop_starts_held_so_the_release_decides(self):
        """A restart that lands while the control is not acting is held by nothing, and
        simple-pid only resets on a real manual-to-automatic change - so `resume` at the next
        opening did nothing and the restored integral was used hours later unchecked.

        Reached through `enable_when` rather than the clock, which is the second way in after
        the active period, and the reason the hold is no longer conditional on either.
        """
        clock = [0.0]
        controller = self._settled(clock)
        controller.resume_from({"integral": 90.0}, age=0.0)
        assert controller.pid.auto_mode is False, "a restored loop was left running"
        clock[0] += 6 * 3600
        controller.resume()
        assert controller.pid._integral == 0

    def test_a_restored_loop_is_dated_from_when_it_was_written(self):
        """`loop_state` hands back state up to RESUMABLE_CYCLES cycles old, which outlasts
        RESUMABLE_HOLD_SECONDS once the cycle passes six minutes. Without backdating, a
        restart bought a fresh lease on an integral a running process would have dropped."""
        clock = [0.0]
        controller = self._settled(clock)
        controller.resume_from({"integral": 90.0}, age=RESUMABLE_HOLD_SECONDS + 1)
        controller.resume()
        assert controller.pid._integral == 0, "a restart bought more leniency than staying up would"

    def test_an_unusable_age_spends_the_whole_lease(self):
        """A clock that went backwards must not buy extra time."""
        controller = self._settled([0.0])
        controller.resume_from({"integral": 90.0}, age=float("nan"))
        controller.resume()
        assert controller.pid._integral == 0

    def test_stepping_while_held_says_so(self):
        """simple-pid answers with its last output while manual, which is None where it has
        never produced one - and that reached `plan_window` as a demand it could not place,
        so the complaint arrived from staging and named the rungs."""
        controller = self._settled([0.0])
        controller.hold()
        controller.pid._last_output = None
        with pytest.raises(ConfigError, match="held"):
            controller.step({"lux": 300.0, "target": 1000}, dt=60)

    def test_a_frozen_switched_device_does_not_move_a_driven_one(self):
        """Freezing says which rungs may be *switched to*. It says nothing about the number a
        driven device should hold, because that device is not the one being protected.

        Interpolating on the censored ladder let one device's transition minimum drag another
        to an end of its range: a heater frozen off pinned the lamp to 0 and frozen on pinned
        it to 100, when at half demand it belongs at half brightness either way.
        """
        controller = self._mixed()
        for state in (False, True):
            plan = controller.step(
                {"lux": 500.0, "target": 1000},
                dt=60,
                frozen=frozenset({"heater"}),
                states={"heater": state, "lamp": 50},
            )
            lamps = {dwell.stage.states["lamp"] for dwell in plan}
            assert lamps == {50.0}, f"heater frozen {state}: lamp at {lamps}"
            # The heater is still protected: only the rung it is already on was commanded.
            assert {dwell.stage.states["heater"] for dwell in plan} == {state}

    def test_a_cap_does_still_bound_a_driven_device(self):
        """The other side, and the reason this is not simply "ignore the narrowed ladder": a
        cap is a standing instruction about how hard the control may drive, so it bounds the
        driven value where a freeze does not."""
        controller = self._mixed(max_level="0")
        plan = controller.step({"lux": 500.0, "target": 1000}, dt=60)
        assert {dwell.stage.states["lamp"] for dwell in plan} == {0.0}

    def test_an_explicit_last_output_still_wins(self):
        clock = [0.0]
        controller = self._settled(clock)
        controller.hold()
        clock[0] += 60
        controller.resume(last_output=12.0)
        assert controller.pid._integral == pytest.approx(12.0)

    def test_resuming_without_a_hold_changes_nothing(self):
        """The cycle calls `resume` unconditionally, so it must be a no-op while already
        running - which is what makes the ordinary path free."""
        clock = [0.0]
        controller = self._settled(clock)
        before = controller.pid._integral
        controller.resume()
        assert controller.pid._integral == pytest.approx(before)
