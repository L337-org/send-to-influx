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
import json
import logging
import os
import signal
import subprocess
import sys
import textwrap
from zoneinfo import ZoneInfo

import pytest

from toinflux.exceptions import ConfigError, SourceConnectionError
from toinflux.gating import DeviceGuard, Gate, commands_for
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

    def test_the_reason_quotes_the_rule_it_names(self):
        """A newline is ordinary whitespace to the rule parser, so a rule can carry one -
        and this reason reaches a log line. Quoted, it cannot write a second entry."""
        decision = Gate(_document(enable_when="outside <\n 15")).decide({"outside": 20.0}, NIGHT)
        assert "\n" not in decision.reason

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


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _recorder():
    """Return a command callable and the list of mappings it was given."""
    commanded = []
    return commanded.append, commanded


def _guard_script(record, *, handle_term=False, assert_only=False):
    """Return a child program that builds a guard and records what it commands.

    A real process rather than a patched ``atexit``: a test that asserted registration
    would pass against a guard whose handler never ran, which is the only property worth
    testing here.
    """
    return textwrap.dedent(f"""
        import json, signal, sys, time
        sys.path.insert(0, {ROOT!r})
        from toinflux.gating import DeviceGuard

        def command(commands):
            with open({record!r}, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(commands) + "\\n")

        guard = DeviceGuard("conservatory", "unenergised", ["far", "near"], command)
        if {assert_only!r}:
            guard.assert_safe_state()
            sys.exit(0)
        if {handle_term!r}:
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        print("ready", flush=True)
        time.sleep(30)
        """)


def _commanded_in(record):
    """Return the mappings a child recorded, in order."""
    if not record.exists():
        return []
    return [json.loads(line) for line in record.read_text().splitlines() if line]


def _run_child(script, signal_number=None):
    """Run a child to completion, optionally signalling it once it is ready.

    Returns:
        subprocess.Popen: the finished child, for its return code; what it commanded is
        read from the record file with :func:`_commanded_in`
    """
    child = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": ""},
    )
    if signal_number is not None:
        assert child.stdout.readline().strip() == "ready"
        child.send_signal(signal_number)
    child.wait(timeout=30)
    child.stdout.close()
    return child


class TestTheStartupAssertion:
    def test_it_commands_every_device_off_before_the_loop_runs(self):
        """The half that has to exist. A control that only tidied up on the way out would
        leave the heaters running after any death that skips the exit handler."""
        command, commanded = _recorder()
        DeviceGuard("conservatory", "unenergised", ["far", "near"], command).assert_safe_state()
        assert commanded == [{"far": False, "near": False}]

    def test_leave_unchanged_commands_nothing_at_all(self):
        """Not an empty mapping through the same path: the device's state is not this
        control's to reset, so nothing is sent."""
        command, commanded = _recorder()
        DeviceGuard("conservatory", "leave_unchanged", ["far"], command).assert_safe_state()
        assert commanded == []

    def test_leave_unchanged_says_so_at_startup(self, caplog):
        """It hollows out the assertion, so the operator is told once, at the point where
        the protection is not happening rather than in the documentation."""
        command, _ = _recorder()
        with caplog.at_level(logging.WARNING):
            DeviceGuard("conservatory", "leave_unchanged", ["far"], command).assert_safe_state()
        assert "leave_unchanged" in caplog.text and "conservatory" in caplog.text

    def test_a_failure_reaches_the_caller_with_its_own_type(self):
        """A device that is missing and a bridge that is briefly unreachable want opposite
        responses - stop and retry - and wrapping both in one type would destroy the
        distinction on the one path where the supervisor has to act on it."""
        command, _ = _recorder()

        def refuse(_commands):
            raise SourceConnectionError("the bridge did not answer")

        guard = DeviceGuard("conservatory", "unenergised", ["far"], refuse)
        with pytest.raises(SourceConnectionError):
            guard.assert_safe_state()
        guard.close()

    def test_a_device_name_cannot_write_its_own_log_line(self, caplog):
        """Device names come from a control document an MCP client can write, and nothing
        constrains one to a single line. Joined raw, a name carrying a newline writes its
        own entry in the journal, and the log stops being evidence."""
        command, _ = _recorder()
        guard = DeviceGuard("conservatory", "unenergised", ["far\nWARNING  heating disabled"], command)
        with caplog.at_level(logging.INFO):
            guard.assert_safe_state()
        guard.close()
        assert "\n" not in caplog.records[-1].getMessage()

    def test_an_unknown_safe_state_is_refused_when_the_guard_is_built(self):
        """Rather than at exit, which is the one path that can do nothing about it."""
        command, _ = _recorder()
        with pytest.raises(ConfigError, match="not a safe state"):
            DeviceGuard("conservatory", "somewhere_in_between", ["far"], command)


class TestStopping:
    def test_it_commands_every_device_off(self):
        command, commanded = _recorder()
        DeviceGuard("conservatory", "unenergised", ["far", "near"], command).stop("enabled was set false")
        assert commanded == [{"far": False, "near": False}]

    def test_stopping_twice_commands_once(self):
        """The exit handler runs after the normal shutdown path has already applied it. A
        second command against a torn-down connection would fail and log as though the safe
        state had not been applied, which is the opposite of what happened."""
        command, commanded = _recorder()
        guard = DeviceGuard("conservatory", "unenergised", ["far"], command)
        guard.stop("enabled was set false")
        guard.stop("the process is exiting")
        assert commanded == [{"far": False}]

    def test_a_failure_on_the_way_out_is_logged_rather_than_raised(self, caplog):
        """Nothing above this can handle it, and an exception in an exit handler becomes a
        traceback and nothing else. A device left energised after a shutdown is exactly the
        failure somebody will be searching the journal for."""

        def refuse(_commands):
            raise SourceConnectionError("the bridge did not answer")

        guard = DeviceGuard("conservatory", "unenergised", ["far"], refuse)
        with caplog.at_level(logging.ERROR):
            guard.stop("the process is exiting")
        # The type as well as the message: this line is the only record there will be, and
        # a connection failure worth retrying reads identically to a permanent one without
        # it. A raised message can leave the class to `from exc`; this cannot.
        assert "SourceConnectionError" in caplog.text and "the bridge did not answer" in caplog.text

    def test_a_replaced_guard_stops_commanding(self):
        """A control reloaded in a long-lived process builds a new guard. The old one is
        not the authority on those devices any more, and an exit handler it left behind
        would command them from a configuration nobody is running."""
        command, commanded = _recorder()
        guard = DeviceGuard("conservatory", "unenergised", ["far"], command)
        guard.close()
        guard.stop("the process is exiting")
        assert commanded == []

    def test_leave_unchanged_commands_nothing_on_the_way_out_either(self):
        """A control that said "not mine to reset" at 03:00 did not mean something else at
        shutdown."""
        command, commanded = _recorder()
        DeviceGuard("conservatory", "leave_unchanged", ["far"], command).stop("the process is exiting")
        assert commanded == []


class TestInARealProcess:
    """Spawned rather than patched. Every property here is about whether the interpreter
    runs the handler, which a test that patched `atexit.register` could not see: it would
    pass against a guard that registered nothing and against one that registered a handler
    Python never calls."""

    def test_an_ordinary_exit_applies_the_safe_state(self, tmp_path):
        """Twice, and both are wanted: the startup assertion and the exit handler are
        separate promises rather than one bracketed pair, so a process that never reached
        its loop still leaves the devices off. Only `stop` is once-only."""
        record = tmp_path / "commanded"
        _run_child(_guard_script(str(record), assert_only=True))
        assert _commanded_in(record) == [{"far": False, "near": False}] * 2

    def test_a_handled_signal_applies_the_safe_state(self, tmp_path):
        """`systemctl stop` signals every process in the unit's cgroup at once, so a control
        handles its own SIGTERM. sys.exit() from the handler unwinds to a normal exit, which
        is what lets the exit handler run at all."""
        record = tmp_path / "commanded"
        _run_child(_guard_script(str(record), handle_term=True), signal.SIGTERM)
        assert _commanded_in(record) == [{"far": False, "near": False}]

    def test_an_unhandled_sigterm_applies_nothing(self, tmp_path):
        """Python's default action for SIGTERM kills the process without running exit
        handlers. Measured rather than assumed, because "we handle SIGTERM" is the sort of
        thing that quietly stops being true."""
        record = tmp_path / "commanded"
        child = _run_child(_guard_script(str(record)), signal.SIGTERM)
        assert child.returncode == -signal.SIGTERM
        assert _commanded_in(record) == []

    def test_sigkill_applies_nothing_and_the_next_start_repairs_it(self, tmp_path):
        """The whole argument for the startup assertion, end to end: the heaters are still
        on after the kill, and the next process turns them off before controlling anything.
        A power cut and an OOM kill leave exactly this situation behind."""
        record = tmp_path / "commanded"
        child = _run_child(_guard_script(str(record)), signal.SIGKILL)
        assert child.returncode == -signal.SIGKILL
        assert _commanded_in(record) == []
        _run_child(_guard_script(str(record), assert_only=True))
        assert _commanded_in(record)[0] == {"far": False, "near": False}
