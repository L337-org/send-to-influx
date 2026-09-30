"""Tests for turning a control's demand into device states over a cycle window.

The decisions worth pinning are the ones a later change could undo without failing
anything obvious: which rung an equal-level tie picks, whether the cap limits the ladder or
the demand, and whose minimum transition time a window has to respect.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT"

import random
from types import MappingProxyType

import pytest

from toinflux.exceptions import ConfigError
from toinflux.rules import RuleEvaluationError
from toinflux.staging import (
    Dwell,
    Stage,
    bracket,
    build_ladder,
    cap_ladder,
    delivered_level,
    pinned_plan,
    plan_window,
    reachable_ladder,
)

# The design note's worked example: two independently switchable heaters.
CONSERVATORY = [
    {"level": 0, "set": {"far": False, "near": False}},
    {"level": 750, "set": {"far": True, "near": False}},
    {"level": 750, "set": {"far": False, "near": True}},
    {"level": 1500, "set": {"far": True, "near": True}},
]


def _no_minimum(_name):
    return 0


class TestBuildingTheLadder:
    def test_rungs_sort_by_level_not_document_order(self):
        """ "The next stage up" is a question about magnitude. An operator listing 1500
        before 750 has written an unusual document, not a different ladder."""
        shuffled = [CONSERVATORY[3], CONSERVATORY[0], CONSERVATORY[1]]
        assert [rung.level for rung in build_ladder(shuffled)] == [0.0, 750.0, 1500.0]

    def test_equal_levels_keep_their_declaration_order(self):
        """Two rungs can reach the same level by different means and not be
        interchangeable: one heater sits next to the temperature sensor. Declaring the far
        one first is how the operator says which should do the steady-state work, so the
        order is preserved rather than merged or refused."""
        ladder = build_ladder(CONSERVATORY)
        at_750 = [rung for rung in ladder if rung.level == 750]
        assert [rung.states for rung in at_750] == [
            {"far": True, "near": False},
            {"far": False, "near": True},
        ]

    def test_an_empty_ladder_is_a_config_error(self):
        with pytest.raises(ConfigError, match="no ladder to work with"):
            build_ladder([])

    def test_a_level_written_as_an_int_becomes_a_float(self):
        """So arithmetic on it cannot surprise: integer division is not what a demand
        between two rungs wants."""
        assert isinstance(build_ladder(CONSERVATORY)[0].level, float)


class TestBracketing:
    def setup_method(self):
        self.ladder = build_ladder(CONSERVATORY)

    @pytest.mark.parametrize(
        "demand,expected",
        [
            pytest.param(-50, (0.0, 0.0), id="below-the-bottom-rung"),
            pytest.param(0, (0.0, 0.0), id="on-the-bottom-rung"),
            pytest.param(300, (0.0, 750.0), id="between"),
            pytest.param(1237, (750.0, 1500.0), id="the-worked-example"),
            pytest.param(1500, (1500.0, 1500.0), id="on-the-top-rung"),
            pytest.param(2000, (1500.0, 1500.0), id="above-the-top-rung"),
        ],
    )
    def test_a_demand_finds_the_rungs_it_sits_between(self, demand, expected):
        lower, upper = bracket(self.ladder, demand)
        assert (lower.level, upper.level) == expected

    def test_a_demand_beyond_the_ladder_is_not_pretended_away(self):
        """A demand of 2000 against a ladder topping out at 1500 cannot be met. Both ends of
        the bracket are the top rung, so the caller spends the whole window there and the
        shortfall shows up as error the integral can see, rather than being hidden."""
        lower, upper = bracket(self.ladder, 2000)
        assert lower is upper is self.ladder[-1]

    def test_landing_exactly_on_a_rung_collapses_the_bracket(self):
        """Like either end of the ladder does, rather than returning the next rung up with a
        zero share.

        That happened to work, because a dwell of no length was dropped downstream - but it
        made this function correct only while that filter existed, and a caller reading "the
        rungs a demand sits between" got two rungs for a demand sitting on one.
        """
        lower, upper = bracket(self.ladder, 750)
        assert lower is upper
        assert lower.level == 750.0

    def test_landing_on_a_shared_level_takes_the_earliest_declared(self):
        """Which is what the declaration order was preserved for."""
        lower, _ = bracket(self.ladder, 750)
        assert dict(lower.states) == {"far": True, "near": False}


class TestCappingTheLadder:
    def setup_method(self):
        self.ladder = build_ladder(CONSERVATORY)

    def test_the_cap_removes_rungs_rather_than_limiting_the_demand(self):
        """The difference is the point of having it. Capping the demand at 1000 would still
        time-proportion between 750 and 1500, so the actuator would spend part of every
        window at 1500 and average 1000 - which satisfies a preference and breaches a limit.
        The rule behind the cap is house load or grid carbon, and neither is met by
        exceeding the figure briefly and often.
        """
        capped = cap_ladder(self.ladder, 1000)
        assert [rung.level for rung in capped] == [0.0, 750.0, 750.0]
        assert plan_window(capped, 1000, 900, _no_minimum)[0].stage.level == 750.0

    def test_no_cap_leaves_the_ladder_alone(self):
        assert cap_ladder(self.ladder, None) is self.ladder

    def test_a_cap_below_every_rung_keeps_the_lowest(self):
        """An empty ladder has nothing to command. The lowest rung is the least the control
        can do rather than a breach of the cap."""
        capped = cap_ladder(self.ladder, -1)
        assert [rung.level for rung in capped] == [0.0]


class TestPlanningAWindow:
    def setup_method(self):
        self.ladder = build_ladder(CONSERVATORY)

    def test_the_worked_example_splits_as_the_design_note_says(self):
        """A demand of 1237 with rungs at 750 and 1500 gives 65% of the window at 1500."""
        lower, upper = plan_window(self.ladder, 1237, 900, _no_minimum)
        assert (lower.stage.level, upper.stage.level) == (750.0, 1500.0)
        assert upper.seconds == pytest.approx(900 * (1237 - 750) / 750)
        assert lower.seconds + upper.seconds == pytest.approx(900)

    def test_a_demand_on_a_rung_fills_the_window_with_it(self):
        plan = plan_window(self.ladder, 750, 900, _no_minimum)
        assert len(plan) == 1
        assert (plan[0].stage.level, plan[0].seconds) == (750.0, 900.0)

    def test_a_short_dwell_collapses_onto_the_nearer_rung(self):
        """Commanding a heater on for forty seconds when it needs five minutes between
        changes is not a shorter burst of heat - it is a command the device ignores, or
        obeys at a cost the operator asked it not to pay."""
        plan = plan_window(self.ladder, 800, 900, lambda name: 300)
        assert len(plan) == 1
        assert plan[0].stage.level == 750.0, "800 is nearer 750 than 1500"

    def test_snapping_picks_the_nearer_rung_in_both_directions(self):
        assert plan_window(self.ladder, 1450, 900, lambda name: 300)[0].stage.level == 1500.0
        assert plan_window(self.ladder, 800, 900, lambda name: 300)[0].stage.level == 750.0

    def test_only_devices_that_change_are_asked_for_their_minimum(self):
        """The far heater is on at both 750 and 1500, so it is not transitioning and its own
        minimum has nothing to say about this window. Consulting it anyway would collapse a
        window that the device actually changing is perfectly happy with - the same rule as
        "a stage change that leaves one heater untouched must not restart that heater's
        clock", applied a window earlier.
        """
        minimums = {"far": 3600, "near": 60}
        plan = plan_window(self.ladder, 1237, 900, lambda name: minimums[name])
        assert len(plan) == 2, "the far heater's minimum should not have collapsed this"

    def test_a_changing_device_does_collapse_it(self):
        """The same window, with the minimum on the device that actually changes."""
        minimums = {"far": 0, "near": 3600}
        assert len(plan_window(self.ladder, 1237, 900, lambda name: minimums[name])) == 1

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), True, "750", None])
    def test_a_demand_that_is_not_a_finite_number_is_refused(self, bad):
        """Not a plan this cycle, so the caller falls safe rather than acting.

        A nan did not do nothing, which is what makes this worth an exception rather than a
        filter: every comparison against it is False, so it fell through bracket() to the top
        of the ladder and commanded a full window at maximum. The heaters full on, because of
        arithmetic nobody could see.
        """
        with pytest.raises(RuleEvaluationError, match="not a level to hold"):
            plan_window(self.ladder, bad, 900, _no_minimum)

    @pytest.mark.parametrize("bad", [0, -1, "900", None, True, float("nan")])
    def test_an_unusable_cycle_window_is_refused(self, bad):
        with pytest.raises(ConfigError, match="cycle_seconds"):
            plan_window(self.ladder, 1237, bad, _no_minimum)

    def test_the_dwells_always_fill_the_window(self):
        """Whatever the demand, the window is accounted for: a gap would be an unstated
        stretch during which the devices are in whatever state they were left in."""
        for demand in (-50, 0, 1, 374, 750, 751, 1237, 1500, 2000):
            plan = plan_window(self.ladder, demand, 900, _no_minimum)
            assert sum(dwell.seconds for dwell in plan) == pytest.approx(900), demand

    def test_a_ladder_of_one_rung_is_that_rung(self):
        """A control with a single stage is an on/off control with extra steps, and must not
        divide by a zero level range."""
        only = build_ladder([{"level": 0, "set": {"far": False}}])
        plan = plan_window(only, 500, 900, _no_minimum)
        assert (len(plan), plan[0].seconds) == (1, 900.0)

    def test_two_rungs_with_the_same_states_do_not_divide_by_zero(self):
        """Degenerate but expressible: two levels whose device states happen to match. No
        device changes, so no minimum applies and the split is ordinary arithmetic."""
        ladder = build_ladder([{"level": 0, "set": {"far": False}}, {"level": 100, "set": {"far": False}}])
        plan = plan_window(ladder, 50, 900, lambda name: 3600)
        assert sum(dwell.seconds for dwell in plan) == pytest.approx(900)


class TestTheStageRecord:
    def test_a_stage_is_immutable(self):
        """The ladder is read every cycle and handed around; a rung that could be edited in
        place would make one cycle's plan depend on another's."""
        stage = build_ladder(CONSERVATORY)[0]
        with pytest.raises(AttributeError):
            stage.level = 99

    def test_the_states_cannot_be_edited_either(self):
        """frozen=True stops the attribute being rebound and does nothing about the dict
        behind it, so this passed against a plain dict while the states stayed editable -
        the assertion above says "immutable" and only covered half of it."""
        stage = build_ladder(CONSERVATORY)[0]
        with pytest.raises(TypeError):
            stage.states["far"] = True


class TestValuesThatCannotBeOrdered:
    """nan compares False against everything, so it neither sorts nor brackets.

    A ladder containing one is silently unordered and a demand lands wherever it happens to,
    for ever, with nothing logged - which is why these are refused rather than tolerated.
    """

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), True, "750", None])
    def test_a_level_that_is_not_a_finite_number_is_refused(self, bad):
        with pytest.raises(ConfigError, match="must be a finite number"):
            build_ladder([{"level": 0, "set": {"far": False}}, {"level": bad, "set": {"far": True}}])

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, "750"])
    def test_a_cap_that_is_not_a_finite_number_is_refused(self, bad):
        """A cap comes from evaluating a rule, and the rule language really can produce
        these: `1e400` is inf and `1e400 - 1e400` is nan. A nan would have collapsed the
        ladder to its lowest rung - fail-safe by accident, and indistinguishable from a cap
        that genuinely forbids everything.

        RuleEvaluationError rather than ConfigError: the cap came out of a rule, so it is
        this cycle that failed and not the document. The same rule may produce a usable
        number next cycle."""
        with pytest.raises(RuleEvaluationError, match="not a level to cap at"):
            cap_ladder(build_ladder(CONSERVATORY), bad)

    def test_the_rule_language_can_actually_produce_one(self):
        """So the guard above is not defending against a value that cannot arrive."""
        from toinflux.rules import parse_rule

        assert parse_rule("1e400").evaluate({}) == float("inf")


class TestADeviceSetToAValueRatherThanSwitched:
    """A dimmer has a middle setting, which is the whole reason it does not need one made out
    of time. Time-proportioning it would be flicker rather than control.

    So the ladder is a transfer curve for these devices: they take the value it describes *at*
    the demand and hold it for the whole window, while a switched device beside them is
    proportioned between two rungs exactly as before. One control can hold both, each driven
    by the method its own hardware supports, off the same demand.
    """

    LAMP = build_ladder([{"level": 0, "set": {"lamp": 0}}, {"level": 1000, "set": {"lamp": 100}}])
    MIXED = build_ladder(
        [
            {"level": 0, "set": {"lamp": 0, "heater": False}},
            {"level": 1000, "set": {"lamp": 100, "heater": True}},
        ]
    )
    DRIVEN = {"lamp": "brightness_pct"}

    @pytest.mark.parametrize(
        "demand, expected",
        [
            pytest.param(0, 0.0, id="at-the-bottom"),
            pytest.param(250, 25.0, id="quarter"),
            pytest.param(500, 50.0, id="half"),
            pytest.param(1000, 100.0, id="at-the-top"),
            pytest.param(1500, 100.0, id="beyond-the-top-is-clamped"),
            pytest.param(-50, 0.0, id="below-the-bottom-is-clamped"),
        ],
    )
    def test_it_takes_the_ladders_value_at_the_demand(self, demand, expected):
        plan = plan_window(self.LAMP, demand, 30, lambda _device: 1, self.DRIVEN)
        assert [dwell.stage.states["lamp"] for dwell in plan] == [expected]

    def test_a_window_with_nothing_else_in_it_is_not_split(self):
        """Both rungs would command the same value, so splitting issues one command twice,
        logs it twice, and buys nothing."""
        plan = plan_window(self.LAMP, 250, 30, lambda _device: 1, self.DRIVEN)
        assert len(plan) == 1
        assert plan[0].seconds == 30

    def test_a_switched_device_beside_it_is_still_proportioned(self):
        plan = plan_window(self.MIXED, 200, 30, lambda _device: 1, self.DRIVEN)
        assert [dwell.stage.states["heater"] for dwell in plan] == [False, True]
        assert [dwell.seconds for dwell in plan] == [24.0, 6.0]

    def test_and_the_driven_one_holds_one_value_across_both_dwells(self):
        plan = plan_window(self.MIXED, 200, 30, lambda _device: 1, self.DRIVEN)
        assert {dwell.stage.states["lamp"] for dwell in plan} == {20.0}

    def test_a_driven_device_does_not_constrain_how_the_window_is_split(self):
        """Its value is the same in both dwells, so nothing about it changes when the window
        is split - and letting its minimum bind here would stop a switched device beside it
        from proportioning for no reason."""
        long_for_the_lamp = plan_window(self.MIXED, 200, 30, lambda device: 300 if device == "lamp" else 1, self.DRIVEN)
        assert len(long_for_the_lamp) == 2, "the lamp's minimum collapsed a window it has no say in"

    def test_a_switched_devices_minimum_still_binds(self):
        collapsed = plan_window(self.MIXED, 200, 30, lambda device: 300 if device == "heater" else 1, self.DRIVEN)
        assert len(collapsed) == 1

    def test_with_nothing_driven_the_plan_is_exactly_as_it_was(self):
        assert plan_window(self.MIXED, 200, 30, lambda _device: 1, {}) == plan_window(
            self.MIXED, 200, 30, lambda _device: 1
        )

    def test_a_rung_that_omits_the_device_is_left_to_the_validator(self):
        """Guessing a value here would command a lamp off the strength of a document already
        known to be wrong."""
        broken = build_ladder([{"level": 0, "set": {}}, {"level": 1000, "set": {"lamp": 100}}])
        plan = plan_window(broken, 500, 30, lambda _device: 1, self.DRIVEN)
        assert "lamp" not in plan[0].stage.states or plan[0].stage.states.get("lamp") is None


class TestWhatAWindowDelivers:
    """The level the devices were told to reach, read back from the plan.

    Rung levels alone are right for switched devices and wrong for driven ones: a lamp on a
    0 to 1000 ladder asked for 400 is planned as one dwell *at level 0* with the lamp at 40%,
    so reading the rung would record 0 for a lamp that is plainly on.
    """

    SWITCHED = build_ladder([{"level": 0, "set": {"heater": False}}, {"level": 750, "set": {"heater": True}}])
    LAMP = build_ladder([{"level": 0, "set": {"lamp": 0}}, {"level": 1000, "set": {"lamp": 100}}])
    MIXED = build_ladder(
        [
            {"level": 0, "set": {"heater": False, "lamp": 0}},
            {"level": 500, "set": {"heater": False, "lamp": 100}},
            {"level": 1000, "set": {"heater": True, "lamp": 100}},
        ]
    )
    DRIVEN = {"lamp": "brightness_pct"}

    def test_switched_devices_deliver_the_time_weighted_rung_levels(self):
        plan = plan_window(self.SWITCHED, 400, 300, _no_minimum)
        assert [dwell.stage.level for dwell in plan] == [0.0, 750.0]
        assert delivered_level(plan, self.SWITCHED) == pytest.approx(400)

    def test_a_driven_device_delivers_the_level_its_value_stands_for(self):
        plan = plan_window(self.LAMP, 400, 300, _no_minimum, self.DRIVEN)
        assert [dwell.stage.level for dwell in plan] == [0.0], "the case this exists for has changed shape"
        assert delivered_level(plan, self.LAMP, self.DRIVEN) == pytest.approx(400)

    @pytest.mark.parametrize("demand", [0, 250, 500, 750, 1000])
    def test_a_mixed_ladder_delivers_its_demand(self, demand):
        plan = plan_window(self.MIXED, demand, 300, _no_minimum, self.DRIVEN)
        assert delivered_level(plan, self.MIXED, self.DRIVEN) == pytest.approx(demand)

    @pytest.mark.parametrize("demand", [250, 750])
    def test_two_dimmers_ramped_one_after_the_other(self, demand):
        """Found in review. On the upper stretch the first dimmer is at 100 at both ends, and
        on the lower the second is at 0 at both ends; a device constant along a stretch was
        skipped rather than allowed to rule it out, so at 750 the lower stretch fitted too and
        the tie went to the planned rung, reading 500."""
        ladder = build_ladder(
            [
                {"level": 0, "set": {"a": 0, "b": 0}},
                {"level": 500, "set": {"a": 100, "b": 0}},
                {"level": 1000, "set": {"a": 100, "b": 100}},
            ]
        )
        driven = {"a": "brightness_pct", "b": "brightness_pct"}
        plan = plan_window(ladder, demand, 300, _no_minimum, driven)
        assert delivered_level(plan, ladder, driven) == pytest.approx(demand)

    def test_a_driven_device_held_at_its_old_value_shows_the_hold(self):
        """The gap between demand and delivered is what makes a held device visible, so the
        held value has to be what is read back rather than the demand."""
        held = (Dwell(stage=_with(self.LAMP[0], lamp=70.0), seconds=300.0),)
        assert delivered_level(held, self.LAMP, self.DRIVEN) == pytest.approx(700)

    def test_two_driven_devices_that_disagree_average(self):
        pair = build_ladder([{"level": 0, "set": {"a": 0, "b": 0}}, {"level": 1000, "set": {"a": 100, "b": 100}}])
        held = (Dwell(stage=_with(pair[0], a=20.0, b=60.0), seconds=60.0),)
        assert delivered_level(held, pair, {"a": "brightness_pct", "b": "brightness_pct"}) == pytest.approx(400)

    def test_states_the_curve_does_not_describe_fall_back_to_the_rung(self):
        """A switched device in a state no stretch of the curve gives it: there is nothing to
        read the driven value against, and the rung it was planned on is what is left."""
        odd = (Dwell(stage=_with(self.MIXED[0], heater=True, lamp=50.0), seconds=60.0),)
        assert delivered_level(odd, self.MIXED, self.DRIVEN) == 0.0

    def test_a_plan_with_no_length_delivers_nothing(self):
        assert delivered_level((), self.SWITCHED) is None

    def test_a_crossfade_reads_its_demand(self):
        """Found by the generated ladders below. `a` rises then falls and `b` rises then falls
        further, so at 1043 the states also fitted the stretch below the planned rung once the
        two lamps' disagreeing positions there were averaged, and that stretch was nearer."""
        ladder = build_ladder(
            [
                {"level": 50, "set": {"a": 55, "b": 15}},
                {"level": 200, "set": {"a": 75, "b": 80}},
                {"level": 1750, "set": {"a": 75, "b": 50}},
                {"level": 2000, "set": {"a": 20, "b": 10}},
            ]
        )
        driven = {"a": "brightness_pct", "b": "brightness_pct"}
        plan = plan_window(ladder, 1043, 300, _no_minimum, driven)
        assert delivered_level(plan, ladder, driven) == pytest.approx(1043)

    def test_a_curve_that_doubles_back_at_the_planned_rung(self):
        """The lamp's 42.5% lies on both stretches either side of 650, and the planned rung sits
        between them, so nearest-to-the-rung chose the wrong side."""
        ladder = build_ladder(
            [
                {"level": 550, "set": {"lamp": 90}},
                {"level": 650, "set": {"lamp": 40}},
                {"level": 900, "set": {"lamp": 70}},
            ]
        )
        plan = plan_window(ladder, 670.86, 300, _no_minimum, self.DRIVEN)
        assert delivered_level(plan, ladder, self.DRIVEN) == pytest.approx(670.86)

    def test_a_hold_on_a_proportioned_window_leaves_the_rungs(self):
        """The lamp is part way along its ramp at both rungs, so it places neither dwell: the
        heater does, and a hold on the lamp does not move them."""
        ladder = build_ladder(
            [{"level": 0, "set": {"heater": False, "lamp": 0}}, {"level": 1000, "set": {"heater": True, "lamp": 100}}]
        )
        plan = plan_window(ladder, 400, 300, _no_minimum, self.DRIVEN)
        assert len(plan) == 2, "the case this exists for has changed shape"
        held = pinned_plan(plan, {"lamp": 10.0}, ladder, self.DRIVEN)
        assert [dwell.level for dwell in held] == [0.0, 1000.0]
        assert delivered_level(held, ladder, self.DRIVEN) == pytest.approx(400)

    def test_a_hold_on_a_dwell_the_lamp_places_moves_it(self):
        """At 750 the lamp is already at 100 on the rung below, so that dwell sits on the curve
        at its rung, and holding the lamp at 10 puts it where 10 is: 50."""
        plan = plan_window(self.MIXED, 750, 300, _no_minimum, self.DRIVEN)
        held = pinned_plan(plan, {"lamp": 10.0}, self.MIXED, self.DRIVEN)
        assert [dwell.level for dwell in held] == [pytest.approx(50.0), 1000.0]


class TestDeliveredOverGeneratedLadders:
    """`delivered` against ladders nobody wrote by hand, checked by a reading that shares none
    of its method.

    The cases above are the shapes somebody thought of. These are seeded random ladders of two
    to five rungs mixing switched devices with driven ones whose ramps rise, fall or double
    back, and they found what the written cases missed: a curve that doubles back fits a
    dwell's states in more than one place, and choosing among them without the demand chose
    wrongly. The reference reading samples the curve finely and compares states, where the
    code under test solves for positions along each stretch.

    Ladders with two adjacent rungs commanding the same thing are left out: the window
    collapses onto one of them and every level along that stretch is as true as another, so
    there is no single right answer to check against.
    """

    CASES = 1500
    #: Fewer for the held case, where each is checked against the sampled curve.
    HELD_CASES = 400
    SAMPLES = 1000

    @staticmethod
    def _ladder(rng):
        while True:
            count = rng.randint(2, 5)
            levels = sorted(rng.sample(range(0, 2001, 50), count))
            switched = [f"s{index}" for index in range(rng.randint(0, 3))]
            driven = [f"d{index}" for index in range(rng.randint(0 if switched else 1, 2))]
            stages = []
            for level in levels:
                states = {device: rng.random() < 0.5 for device in switched}
                states.update({device: rng.choice(range(0, 101, 5)) for device in driven})
                stages.append({"level": level, "set": states})
            ladder = build_ladder(stages)
            if all(dict(a.states) != dict(b.states) for a, b in zip(ladder, ladder[1:])):
                return ladder, {device: "brightness_pct" for device in driven}

    @staticmethod
    def _curve_at(curve, level):
        """What the curve commands at a level, with None for a switched device it leaves undefined."""
        lower = max((rung for rung in curve if rung.level <= level), key=lambda rung: rung.level, default=curve[0])
        upper = min((rung for rung in curve if rung.level >= level), key=lambda rung: rung.level, default=curve[-1])
        span = upper.level - lower.level
        at = {}
        for device, low in lower.states.items():
            high = upper.states[device]
            if isinstance(low, bool):
                at[device] = low if low == high else None
            else:
                at[device] = low + (high - low) * ((level - lower.level) / span if span else 0.0)
        return at

    def _brute_fits(self, curve, states):
        """Every sampled interval of the curve that passes through the states.

        Crossings rather than closeness: a driven device's value fits between two samples where
        the curve is on one side of it at the first and the other side at the second, which a
        curve that only approaches it - turning just short at a rung - never does. Rungs are
        among the samples, so no interval straddles a change of slope and the crossing is found
        by straight interpolation. Several driven devices fit an interval only if each crosses
        at the same point within it.

        Returns:
            tuple: the levels that fit, and the sample spacing
        """
        low, high = curve[0].level, curve[-1].level
        step = (high - low) / self.SAMPLES
        levels = sorted({low + index * step for index in range(self.SAMPLES + 1)} | {rung.level for rung in curve})
        fits = []
        previous = self._curve_at(curve, levels[0])
        for first, second in zip(levels, levels[1:]):
            here, there = previous, self._curve_at(curve, second)
            previous = there
            crossings = []
            for device, state in states.items():
                if isinstance(state, bool):
                    if here[device] != state or there[device] != state:
                        break
                    continue
                below, above = here[device] - state, there[device] - state
                if below * above > 0:
                    break
                crossings.append(first if below == above else first + (second - first) * below / (below - above))
            else:
                # Interpolated within an interval no rung falls inside, so the crossings are
                # exact and two devices that cross at different points do not fit together.
                if crossings and max(crossings) - min(crossings) <= 1e-6:
                    fits.append(sum(crossings) / len(crossings))
        # A rung on its own: where the switched devices differ either side of it, no interval
        # reaching it fits, and the rung itself still can.
        fits += [
            rung.level
            for rung in curve
            if all(
                rung.states[device] == state if isinstance(state, bool) else abs(rung.states[device] - state) <= 1e-9
                for device, state in states.items()
            )
        ]
        return fits, step

    def test_with_nothing_held_or_capped_it_is_the_demand(self):
        rng = random.Random(4601)
        for case in range(self.CASES):
            ladder, driven = self._ladder(rng)
            demand = rng.uniform(ladder[0].level - 100, ladder[-1].level + 100)
            plan = plan_window(ladder, demand, 300, _no_minimum, driven, curve=ladder)
            expected = min(max(demand, ladder[0].level), ladder[-1].level)
            assert delivered_level(plan, ladder, driven) == pytest.approx(expected), (case, ladder, demand)

    def test_a_cap_delivers_no_more_than_it_allows(self):
        rng = random.Random(4602)
        for case in range(self.CASES):
            ladder, driven = self._ladder(rng)
            capped = cap_ladder(ladder, rng.uniform(ladder[0].level, ladder[-1].level))
            demand = rng.uniform(ladder[0].level, ladder[-1].level)
            plan = plan_window(capped, demand, 300, _no_minimum, driven, curve=capped)
            expected = min(max(demand, capped[0].level), capped[-1].level)
            assert delivered_level(plan, capped, driven) == pytest.approx(expected), (case, ladder, demand)

    def test_with_a_device_frozen_a_lone_dwell_reads_nearest_the_demand(self):
        """A frozen switched device narrows the rungs the window may use while driven values
        still come off the whole curve, so a lone dwell can carry values from a stretch its
        rung is not on, and a curve that doubles back offers more than one place they fit."""
        rng = random.Random(4604)
        checked = 0
        for case in range(self.HELD_CASES):
            ladder, driven = self._ladder(rng)
            switched = [device for device in ladder[0].states if device not in driven]
            # A rung of switched devices alone stands for its own level; only driven values are
            # read back.
            if not switched or not driven:
                continue
            frozen = rng.choice(switched)
            reachable = reachable_ladder(ladder, frozenset({frozen}), {frozen: rng.random() < 0.5})
            demand = rng.uniform(ladder[0].level, ladder[-1].level)
            plan = plan_window(reachable, demand, 300, _no_minimum, driven, curve=ladder)
            if len(plan) != 1:
                continue
            fits, step = self._brute_fits(ladder, plan[0].stage.states)
            if not fits:
                # As for a hold: the accepted mean of positions that disagree, checkable only
                # as far as staying on the curve.
                assert ladder[0].level <= plan[0].level <= ladder[-1].level, (case, ladder, demand)
                continue
            nearest = min(abs(level - demand) for level in fits)
            candidates = [level for level in fits if abs(level - demand) <= nearest + 2 * step]
            assert any(plan[0].level == pytest.approx(level, abs=2 * step) for level in candidates), (
                case,
                ladder,
                demand,
                candidates,
            )
            checked += 1
        assert checked > self.HELD_CASES / 20, f"only {checked} lone dwells were compared"

    def test_a_held_value_reads_as_the_level_the_curve_gives_it(self):
        """Every driven device held at what an earlier demand gave it, which is how a hold
        arises: the device keeps the value it was last sent."""
        rng = random.Random(4603)
        checked = 0
        for case in range(self.HELD_CASES):
            ladder, driven = self._ladder(rng)
            demand = rng.uniform(ladder[0].level, ladder[-1].level)
            plan = plan_window(ladder, demand, 300, _no_minimum, driven, curve=ladder)
            earlier = self._curve_at(ladder, rng.uniform(ladder[0].level, ladder[-1].level))
            held = pinned_plan(plan, {device: earlier[device] for device in driven}, ladder, driven)
            for before, after in zip(plan, held):
                at = self._curve_at(ladder, before.level)
                carried = all(
                    at[device] == state if isinstance(state, bool) else abs(at[device] - state) <= 1e-6
                    for device, state in before.stage.states.items()
                )
                if not carried:
                    # Time-proportioned: the switched devices place it, and the hold does not.
                    assert after.level == before.level, (case, ladder, demand)
                    continue
                fits, step = self._brute_fits(ladder, after.stage.states)
                if not fits:
                    # No level gives every device its held value - they would each put the dwell
                    # somewhere different - and the mean of their positions is the accepted
                    # answer, which has no reference to check against beyond staying on the curve.
                    assert ladder[0].level <= after.level <= ladder[-1].level, (case, ladder, demand, earlier)
                    continue
                # Any fit as near as the nearest to within a sample: sampling cannot separate two
                # that are almost equally far from the demand, and either is then as right.
                nearest = min(abs(level - before.level) for level in fits)
                candidates = [level for level in fits if abs(level - before.level) <= nearest + 2 * step]
                assert any(after.level == pytest.approx(level, abs=2 * step) for level in candidates), (
                    case,
                    ladder,
                    demand,
                    earlier,
                    candidates,
                )
                checked += 1
        assert checked > self.HELD_CASES / 4, f"only {checked} held dwells were compared"


def _with(stage, **states):
    """Return a rung with some devices' states replaced, as `_hold` pins them.

    Args:
        stage (Stage): the rung
        **states: device name to the state it is held at

    Returns:
        Stage: the rung as it would be commanded
    """
    return Stage(level=stage.level, declared=stage.declared, states=MappingProxyType({**stage.states, **states}))
