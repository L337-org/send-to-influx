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

import pytest
import requests

from tests.harness import faults, invariants
from tests.harness.installation import conservatory
from toinflux.control_process import ControlProcess, command_devices, gather
from toinflux.exceptions import ConfigError, SourceConnectionError
from toinflux.rules import RuleEvaluationError, parse_rule

# Inside the conservatory's 23:35-05:25 window, and well outside it.
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


class TestCommandingDevices:
    def test_a_source_with_no_write_path_is_refused(self, installation):
        document = conservatory()
        document["devices"] = {"far": {"source": "openmeteo", "device": "far"}}
        with pytest.raises(ConfigError, match="no write path"):
            command_devices(document, {"far": True}, installation.settings_file)

    def test_a_device_the_document_does_not_declare_is_refused(self, installation):
        with pytest.raises(ConfigError, match="not declared"):
            command_devices(conservatory(), {"nosuchdevice": True}, installation.settings_file)

    def test_the_far_end_s_refusal_reaches_the_caller(self, installation, bridge):
        with faults.erroring(bridge, 503):
            with pytest.raises(SourceConnectionError):
                command_devices(conservatory(), {"far": True}, installation.settings_file)
