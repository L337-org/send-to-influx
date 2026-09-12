"""Tests for turning a control's demand into device states over a cycle window.

The decisions worth pinning are the ones a later change could undo without failing
anything obvious: which rung an equal-level tie picks, whether the cap limits the ladder or
the demand, and whose minimum transition time a window has to respect.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT"

import pytest

from toinflux.exceptions import ConfigError
from toinflux.staging import bracket, build_ladder, cap_ladder, plan_window

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

    def test_landing_on_a_shared_level_takes_the_earliest_declared(self):
        """Which is what the declaration order was preserved for."""
        lower, _ = bracket(self.ladder, 750)
        assert lower.states == {"far": True, "near": False}


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
