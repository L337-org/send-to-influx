"""Tests for one control's closed loop.

Run against a simulated plant rather than a room, so convergence, overshoot and the effect
of a cap are observable in milliseconds. The plant is deliberately crude - a room is not
first-order - which is enough to show the loop working and to make a regression that
introduces oscillation visible.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT"

import pytest

from toinflux.controller import Controller, first_order_plant, simulate
from toinflux.exceptions import ConfigError

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
