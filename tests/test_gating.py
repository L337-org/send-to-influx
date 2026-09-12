"""Tests for whether a control acts, and what its devices do when it stops.

The edge behaviour is the part worth pinning. A control spends almost every cycle unchanged,
so a gate that reported the level rather than the transition would re-command the devices
continuously - and one that reported an edge on its first cycle would apply a safe state
because of a transition that never happened.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT"

import datetime
from zoneinfo import ZoneInfo

import pytest

from toinflux.exceptions import ConfigError
from toinflux.gating import Gate, commands_for
from toinflux.rules import RuleEvaluationError

LONDON = ZoneInfo("Europe/London")
NIGHT = datetime.datetime(2026, 1, 15, 2, 0, tzinfo=LONDON)
DAY = datetime.datetime(2026, 1, 15, 12, 0, tzinfo=LONDON)


def _document(**overrides):
    document = {
        "enabled": True,
        "timezone": "Europe/London",
        "active_period": {"from": "23:35", "to": "05:25", "end_state": "unenergised"},
        "inputs": {"outside": {"source": "openmeteo", "field": "temperature_2m"}},
        "enable_when": "outside < 15",
        "safe_state": "unenergised",
    }
    document.update(overrides)
    return document


class TestTheThreeConditions:
    @pytest.mark.parametrize(
        "overrides,bindings,moment,acting,reason",
        [
            pytest.param({}, {"outside": 5.0}, NIGHT, True, None, id="all-three-hold"),
            pytest.param({"enabled": False}, {"outside": 5.0}, NIGHT, False, "disabled", id="not-enabled"),
            pytest.param({}, {"outside": 5.0}, DAY, False, "active period", id="outside-the-window"),
            pytest.param({}, {"outside": 20.0}, NIGHT, False, "enable_when", id="gate-is-false"),
        ],
    )
    def test_a_control_acts_only_when_all_three_hold(self, overrides, bindings, moment, acting, reason):
        decision = Gate(_document(**overrides)).decide(bindings, moment)
        assert decision.actuating is acting
        if reason is None:
            assert decision.reason is None
        else:
            assert reason in decision.reason

    def test_the_reason_names_the_outermost_condition(self):
        """Checked in the order an operator would ask. A control that is switched off is not
        also reported as being outside its window, even though it is: the first answer is
        the one that explains the situation."""
        decision = Gate(_document(enabled=False)).decide({"outside": 20.0}, DAY)
        assert "disabled" in decision.reason

    def test_a_control_with_no_period_or_gate_just_runs(self):
        document = _document()
        del document["active_period"]
        del document["enable_when"]
        assert Gate(document).decide({}, DAY).actuating is True

    def test_the_gate_rule_reads_the_declared_names(self):
        with pytest.raises(ConfigError):
            Gate(_document(enable_when="nosuchthing < 5"))

    def test_a_gate_that_cannot_be_evaluated_is_this_cycle_not_this_control(self):
        """RuleEvaluationError, so the fail-safe covers the cycle and the control is still
        there next time - the same distinction the loop uses for its own rules."""
        gate = Gate(_document(enable_when="1e400 - 1e400"))
        with pytest.raises(RuleEvaluationError):
            gate.decide({"outside": 5.0}, NIGHT)


class TestEdges:
    def test_the_first_cycle_is_not_an_edge(self):
        """A control starting up has not *become* inactive. Reporting an edge would apply a
        safe state because of a transition that did not happen, and from outside that is
        indistinguishable from one that did."""
        assert Gate(_document()).decide({"outside": 20.0}, DAY).edge is None
        assert Gate(_document()).decide({"outside": 5.0}, NIGHT).edge is None

    def test_an_unchanged_cycle_is_not_an_edge(self):
        """A control spends almost every cycle unchanged. Acting on the level would
        re-command the devices continuously."""
        gate = Gate(_document())
        gate.decide({"outside": 5.0}, NIGHT)
        for _ in range(5):
            assert gate.decide({"outside": 5.0}, NIGHT).edge is None

    def test_closing_reports_an_edge_and_a_state_to_apply(self):
        gate = Gate(_document())
        gate.decide({"outside": 5.0}, NIGHT)
        decision = gate.decide({"outside": 20.0}, NIGHT)
        assert (decision.actuating, decision.edge, decision.apply) == (False, "closed", "unenergised")
        assert "enable_when" in decision.reason

    def test_opening_reports_an_edge_and_nothing_to_apply(self):
        """Resuming is the loop's business: there is no state to command, because the next
        cycle computes one."""
        gate = Gate(_document())
        gate.decide({"outside": 20.0}, NIGHT)
        decision = gate.decide({"outside": 5.0}, NIGHT)
        assert (decision.actuating, decision.edge, decision.apply) == (True, "opened", None)

    def test_any_of_the_three_closing_produces_the_same_edge(self):
        """The point of unifying them: one edge handler, so "what happens when this stops"
        has a single answer rather than three that can drift apart."""
        for overrides, bindings, moment in (
            ({"enabled": False}, {"outside": 5.0}, NIGHT),
            ({}, {"outside": 5.0}, DAY),
            ({}, {"outside": 20.0}, NIGHT),
        ):
            gate = Gate(_document())
            gate.decide({"outside": 5.0}, NIGHT)
            gate.enabled = overrides.get("enabled", True)
            decision = gate.decide(bindings, moment)
            assert (decision.edge, decision.apply) == ("closed", "unenergised")

    def test_a_night_runs_as_one_open_and_one_close(self):
        """The shape an operator would recognise, rather than a property in isolation."""
        gate = Gate(_document())
        edges = []
        for hour, outside in ((23, 10.0), (0, 9.0), (3, 9.0), (4, 20.0), (5, 20.0), (12, 20.0)):
            moment = datetime.datetime(2026, 1, 15, hour, 40, tzinfo=LONDON)
            decision = gate.decide({"outside": outside}, moment)
            if decision.edge:
                edges.append((f"{hour:02d}:40", decision.edge))
        assert edges == [("04:40", "closed")], edges


class TestWhatStoppingApplies:
    def test_the_end_state_is_the_period_s_where_there_is_one(self):
        """A control can want to be left alone on failure and switched off at dawn: one is
        about failure, the other about a schedule."""
        document = _document(safe_state="leave_unchanged")
        document["active_period"]["end_state"] = "unenergised"
        assert Gate(document).closing_state() == "unenergised"

    def test_it_falls_back_to_the_safe_state_without_a_period(self):
        """A control with no window has no "end of the window", so a falling edge from the
        gate is the nearest thing it has to failing."""
        document = _document(safe_state="leave_unchanged")
        del document["active_period"]
        assert Gate(document).closing_state() == "leave_unchanged"

    def test_an_unknown_safe_state_is_refused(self):
        with pytest.raises(ConfigError, match="safe_state"):
            Gate(_document(safe_state="explode"))

    def test_a_non_boolean_enabled_is_refused(self):
        """`enabled: "false"` is a string and truthy, so it would enable the control it was
        written to disable."""
        with pytest.raises(ConfigError, match="enabled"):
            Gate(_document(enabled="false"))


class TestTheCommandsThemselves:
    def test_unenergised_names_every_device(self):
        """Rather than meaning "stage 0". A control whose lowest stage was mis-declared - or
        which has no zero stage - would otherwise energise something while trying to make
        itself safe, and nobody would find out until the day it mattered."""
        assert commands_for("unenergised", ["far", "near"]) == {"far": False, "near": False}

    def test_leave_unchanged_is_none_rather_than_an_empty_mapping(self):
        """Different instructions. A caller treating `{}` as "nothing to do" would be right
        by accident, and would have no way to tell it from "this control owns no devices"."""
        assert commands_for("leave_unchanged", ["far", "near"]) is None
        assert commands_for("unenergised", []) == {}

    def test_an_unknown_state_is_refused_rather_than_ignored(self):
        with pytest.raises(ConfigError, match="not a safe state"):
            commands_for("somewhere_in_between", ["far"])
