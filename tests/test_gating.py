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

from tests.harness.installation import conservatory

# Imported rather than restated: it derives the transition days from the zone database
# instead of naming a date, so a copy here would stop being a daylight-saving scenario
# the year the rules move, and would do it quietly.
from tests.test_schedule import _transitions
from toinflux.controls import validate_control
from toinflux.exceptions import ConfigError, SourceConnectionError
from toinflux.gating import DeviceGuard, Gate, commands_for
from toinflux.rules import RuleEvaluationError
from toinflux.schedule import parse_active_period

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

    def test_the_bindings_are_not_gathered_for_a_control_that_is_switched_off(self):
        """The check order only pays if the cost is deferred. Gathering a control's inputs
        means reading InfluxDB and possibly the device itself, and a control that is
        disabled or outside its window should not pay for a sensor read to be told so."""
        gathered = []

        def bindings():
            gathered.append(True)
            return {"outside": 5.0}

        assert Gate(_document(enabled=False)).decide(bindings, NIGHT).actuating is False
        assert Gate(_document()).decide(bindings, DAY).actuating is False
        assert gathered == []
        assert Gate(_document()).decide(bindings, NIGHT).actuating is True
        assert gathered == [True]

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
        """The shape an operator would recognise, rather than a property in isolation.

        Confirms each closing edge the way the loop does, because a closing edge is not
        spent until its command has been applied. Without that the close repeats every
        cycle, which is exactly the retry the repetition exists to provide.
        """
        gate = Gate(_document())
        edges = []
        for hour, outside in ((23, 10.0), (0, 9.0), (3, 9.0), (4, 20.0), (5, 20.0), (12, 20.0)):
            moment = datetime.datetime(2026, 1, 15, hour, 40, tzinfo=LONDON)
            decision = gate.decide({"outside": outside}, moment)
            if decision.edge:
                edges.append((f"{hour:02d}:40", decision.edge))
            if decision.edge == "closed":
                gate.closed()
        assert edges == [("04:40", "closed")], edges


class TestTheGateAcrossADaylightSavingChange:
    """The story asks for a *repeated activation and deactivation* when the clocks go back,
    and that is a claim about edges rather than about the window predicate.

    `tests/test_schedule.py` proves `is_inside` handles both transitions, measured in real
    minutes. This is the half that does not follow from it on its own: that the gate reports
    the open and the close twice over, which is what a control acts on. Every other edge test
    in this file runs on an ordinary January day.
    """

    @staticmethod
    def _edges(window, day, hours=3):
        """Walk a night in UTC a minute at a time and return the edges the gate reported.

        UTC, because the point is elapsed time rather than what the clock read: an hour that
        happens twice cannot be seen by walking local time-of-day.

        Args:
            window (dict): the active period to give the control
            day (datetime.date): the UTC day to walk
            hours (int): how many hours from midnight UTC to cover

        Returns:
            list: ``(HH:MM UTC, edge)`` for each edge, in order
        """
        gate = Gate(_document(active_period=window))
        start = datetime.datetime(day.year, day.month, day.day, tzinfo=datetime.timezone.utc)
        edges = []
        for minute in range(hours * 60):
            moment = start + datetime.timedelta(minutes=minute)
            decision = gate.decide({"outside": 5.0}, moment)
            if decision.edge:
                edges.append((moment.strftime("%H:%M"), decision.edge))
            # As the loop does: a closing edge is not spent until its command has been
            # applied, and without this it repeats every cycle and swamps the sequence.
            if decision.edge == "closed":
                gate.closed()
        return edges

    def test_the_clocks_going_back_opens_and_closes_it_twice(self):
        """01:15 to 01:45 local happens once in BST and once again in GMT, so the control
        becomes active, inactive, active and inactive again - sixty real minutes of running
        for a thirty-minute window."""
        autumn = _transitions(2026, LONDON)[1][0]
        window = {"from": "01:15", "to": "01:45", "end_state": "unenergised"}
        assert self._edges(window, autumn) == [
            ("00:15", "opened"),
            ("00:45", "closed"),
            ("01:15", "opened"),
            ("01:45", "closed"),
        ]

    def test_an_ordinary_night_opens_and_closes_it_once(self):
        """The control for the one above: the same window on a day with no transition gives
        one pair, so the second pair is the clocks going back and not the walk itself."""
        window = {"from": "01:15", "to": "01:45", "end_state": "unenergised"}
        assert self._edges(window, datetime.date(2026, 1, 15)) == [
            ("01:15", "opened"),
            ("01:45", "closed"),
        ]

    def test_the_clocks_going_forward_never_opens_it_at_all(self):
        """The other edge: that local half hour does not occur, so there is nothing to open,
        and a control that reported an edge here would be acting on an hour that never
        happened."""
        spring = _transitions(2026, LONDON)[0][0]
        window = {"from": "01:15", "to": "01:45", "end_state": "unenergised"}
        assert self._edges(window, spring) == []


class TestANumericSafeState:
    """A driven device has more than two states to be left in, so a percentage is a safe
    state like any other.

    The validator learned that and the two runtime checks did not, so a document naming one
    passed `--check-config` and every MCP tool and then refused to start, with the code that
    handles the number sitting downstream correct and unreachable. Nothing built a `Gate`
    with one, which is how a whole feature could be accepted everywhere and work nowhere.
    """

    @pytest.mark.parametrize("value", [0, 40, 100, 2700])
    def test_the_gate_accepts_what_the_validator_accepted(self, value):
        gate = Gate(_document(safe_state=value))
        assert gate.safe_state == value

    @pytest.mark.parametrize("value", [0, 40, 100])
    def test_a_period_accepts_one_as_its_end_state(self, value):
        period = parse_active_period(_document(active_period={"from": "23:35", "to": "05:25", "end_state": value}))
        assert period.end_state == value

    def test_it_reaches_the_devices_as_the_value_to_set(self):
        """The half that already worked, pinned here because it is what the rest is for."""
        devices = {"lamp": {"source": "hue", "device": "Office Lamp", "parameter": "brightness_pct"}}
        assert commands_for(40, devices) == {"lamp": 40}

    @pytest.mark.parametrize(
        "value", [40, 0, 100, -1, "unenergised", "energised", "leave_unchanged", "wrong", None, True, "40"]
    )
    def test_validation_and_the_runtime_agree_about_every_value(self, value):
        """The guard for the class of fault rather than the instance of it.

        Three copies of one rule, and the bug was that they disagreed: whether a document is
        accepted must not depend on which of them looked at it. Checked both ways round, so
        a runtime that quietly grew *laxer* than the validator would fail here too.
        """
        # A complete document, not this file's minimal one: `validate_control` checks the
        # whole thing, so a stub would be refused for reasons that have nothing to do with
        # the value under test and the comparison would hold vacuously.
        document = conservatory(safe_state=value, active_period={"from": "23:35", "to": "05:25", "end_state": value})
        accepted_by_validation = validate_control(document["name"], document, None) == []
        try:
            Gate(document)
            parse_active_period(document)
            accepted_at_runtime = True
        except ConfigError:
            accepted_at_runtime = False
        assert accepted_by_validation == accepted_at_runtime, (
            f"{value!r} is "
            f"{'accepted' if accepted_by_validation else 'refused'} by validation and "
            f"{'accepted' if accepted_at_runtime else 'refused'} at runtime"
        )


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
            guard.assert_starting_state()
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
        DeviceGuard("conservatory", "unenergised", ["far", "near"], command).assert_starting_state()
        assert commanded == [{"far": False, "near": False}]

    def test_leave_unchanged_commands_nothing_at_all(self):
        """Not an empty mapping through the same path: the device's state is not this
        control's to reset, so nothing is sent."""
        command, commanded = _recorder()
        DeviceGuard("conservatory", "leave_unchanged", ["far"], command).assert_starting_state()
        assert commanded == []

    def test_leave_unchanged_says_so_at_startup(self, caplog):
        """Said once at startup, at INFO.

        It does hollow out the assertion, and that consequence is real - but it is what the
        operator asked for, and for the case the option exists to serve, a light that should
        not go out because a server rebooted, there is no safety question at all. The
        consequence belongs where somebody reads it while choosing, which is CONTROLS.md at
        the point the option is configured. A warning on every start for the lifetime of a
        correct configuration is the noise that teaches people to skim warnings.
        """
        command, _ = _recorder()
        with caplog.at_level(logging.INFO):
            DeviceGuard("conservatory", "leave_unchanged", ["far"], command).assert_starting_state()
        assert "leave_unchanged" in caplog.text and "conservatory" in caplog.text
        assert not [
            record for record in caplog.records if record.levelno >= logging.WARNING
        ], "the configuration the operator chose is not a warning"

    def test_a_failure_reaches_the_caller_with_its_own_type(self):
        """A device that is missing and a bridge that is briefly unreachable want opposite
        responses - stop and retry - and wrapping both in one type would destroy the
        distinction on the one path where the supervisor has to act on it."""
        command, _ = _recorder()

        def refuse(_commands):
            raise SourceConnectionError("the bridge did not answer")

        guard = DeviceGuard("conservatory", "unenergised", ["far"], refuse)
        with pytest.raises(SourceConnectionError):
            guard.assert_starting_state()
        guard.close()

    def test_a_device_name_cannot_write_its_own_log_line(self, caplog):
        """Device names come from a control document an MCP client can write, and nothing
        constrains one to a single line. Joined raw, a name carrying a newline writes its
        own entry in the journal, and the log stops being evidence."""
        command, _ = _recorder()
        guard = DeviceGuard("conservatory", "unenergised", ["far\nWARNING  heating disabled"], command)
        with caplog.at_level(logging.INFO):
            guard.assert_starting_state()
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


class TestAClosingEdgeIsNotSpentUntilItIsApplied:
    """The edge carries a command, and the loop has not run it when `decide` returns.

    Recording it as spent immediately meant a command that raised was never retried: the
    next cycle saw no change, so no edge and no actuation, and the loop slept with the
    devices still energised until the window reopened - eighteen hours for the motivating
    case, from one transient bridge failure at 05:25.

    Nothing else covered it. The fail-safe applies `safe_state`, not the `end_state` that
    just failed, and with `safe_state: leave_unchanged` it commands nothing at all. The
    supervisor's own safe-state pass runs only from `_drop`, `_stop`, `_reap` and
    `stop_all` - every one a death or a stop - so a control that is alive and beating is
    never made safe by the parent.
    """

    @staticmethod
    def _closed_gate():
        """Return a gate that has just acted, and its first closing decision.

        Returns:
            tuple: (the gate, the closing decision)
        """
        gate = Gate(_document())
        assert gate.decide({"outside": 5.0}, NIGHT).actuating is True
        decision = gate.decide({"outside": 5.0}, DAY)
        assert decision.edge == "closed"
        return gate, decision

    def test_the_edge_repeats_while_the_command_has_not_been_confirmed(self):
        """This is the retry. Nothing else re-commands the devices."""
        gate, first = self._closed_gate()
        again = gate.decide({"outside": 5.0}, DAY)
        assert again.edge == "closed", "the edge was consumed before its command was applied"
        assert again.apply == first.apply

    def test_confirming_it_stops_the_repeat(self):
        """Otherwise every subsequent cycle would re-command devices already in place,
        which is the thing edge-triggering exists to avoid."""
        gate, _ = self._closed_gate()
        gate.closed()
        assert gate.decide({"outside": 5.0}, DAY).edge is None

    def test_confirming_twice_is_not_an_error(self):
        gate, _ = self._closed_gate()
        gate.closed()
        gate.closed()
        assert gate.decide({"outside": 5.0}, DAY).edge is None

    def test_reopening_after_a_confirmed_close_still_reports_an_opening_edge(self):
        """The control has to resume, and `resume` is what back-computes the integral."""
        gate, _ = self._closed_gate()
        gate.closed()
        assert gate.decide({"outside": 5.0}, NIGHT).edge == "opened"

    def test_an_opening_edge_is_spent_immediately(self):
        """It carries no command, so there is nothing that could fail and nothing to wait
        for. Repeating it would call `resume` every cycle."""
        gate = Gate(_document())
        assert gate.decide({"outside": 5.0}, DAY).actuating is False
        assert gate.decide({"outside": 5.0}, NIGHT).edge == "opened"
        assert gate.decide({"outside": 5.0}, NIGHT).edge is None


class TestTheNaiveMomentContractHoldsOnEveryPath:
    """`decide`'s docstring promises ConfigError for a naive moment without qualification.

    The check used to live inside `is_inside`, which is reached only for a control that is
    enabled *and* declares an active period - so a disabled control, or one with no period,
    accepted a naive datetime. A guarantee that holds on some paths is worse than one that is
    not claimed, because the caller who relies on it is the one who does not test it.
    """

    NAIVE = datetime.datetime(2026, 1, 1, 12, 0)

    @staticmethod
    def _gate(**overrides):
        """Return a gate over a control with the given overrides.

        Args:
            **overrides: top-level document keys to set

        Returns:
            Gate: the gate
        """
        document = conservatory()
        document.pop("active_period", None)
        document.pop("enable_when", None)
        document.update(overrides)
        return Gate(document)

    @pytest.mark.parametrize(
        "overrides, why",
        [
            pytest.param({"enabled": False}, "a disabled control never reached the check", id="disabled"),
            pytest.param({}, "a control with no active period never reached it either", id="no-active-period"),
            pytest.param(
                {"active_period": {"from": "09:00", "to": "17:00"}}, "the path that always checked", id="with-period"
            ),
        ],
    )
    def test_it_is_refused(self, overrides, why):
        with pytest.raises(ConfigError):
            self._gate(**overrides).decide({}, self.NAIVE)

    def test_an_aware_moment_is_accepted_on_all_of_them(self):
        aware = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)
        for overrides in ({"enabled": False}, {}, {"active_period": {"from": "09:00", "to": "17:00"}}):
            assert self._gate(**overrides).decide({}, aware) is not None


class TestTheEnergisedSafeState:
    """The device is not necessarily a heater.

    `unenergised` and `leave_unchanged` were the whole set, which assumed that off is always
    the harmless direction. For a circulation pump whose stopping lets a boiler overheat, a
    valve held open by power, or an extractor that must not stop, off is the dangerous state.
    Refusing the option did not make any of those safer, only unexpressible - and
    `leave_unchanged` was already permitted, which leaves a device wherever it happened to be
    and may well be on, so an explicit `energised` is the more predictable of the two.
    """

    def test_it_commands_every_device_on_by_name(self):
        assert commands_for("energised", ("pump", "valve")) == {"pump": True, "valve": True}

    def test_by_name_rather_than_by_the_top_stage(self):
        """Same reason `unenergised` does not mean "stage 0": a mis-declared ladder must not
        be able to leave a device in the wrong state while the control makes itself safe."""
        assert commands_for("energised", ()) == {}

    def test_unenergised_is_unchanged(self):
        assert commands_for("unenergised", ("pump", "valve")) == {"pump": False, "valve": False}

    def test_leave_unchanged_is_still_none_rather_than_empty(self):
        assert commands_for("leave_unchanged", ("pump",)) is None

    def test_a_document_may_declare_it(self):
        document = conservatory()
        document["safe_state"] = "energised"
        assert validate_control("conservatory", document) == []

    def test_and_may_use_it_as_an_end_state(self):
        """A control can hold a room overnight and leave its pump running at the boundary."""
        document = conservatory()
        document["active_period"] = {"from": "23:35", "to": "05:25", "end_state": "energised"}
        assert validate_control("conservatory", document) == []

    def test_the_gate_reports_it_on_a_closing_edge(self):
        document = conservatory()
        document.pop("active_period", None)
        document.pop("enable_when", None)
        document["safe_state"] = "energised"
        assert Gate(document).closing_state() == "energised"

    def test_something_that_is_not_a_safe_state_is_still_refused(self):
        with pytest.raises(ConfigError, match="not a safe state"):
            commands_for("on", ("pump",))

    def test_the_default_is_still_off(self):
        """Opt-in, because a device failing to energised keeps drawing power with nothing
        supervising it."""
        document = conservatory()
        document.pop("safe_state", None)
        assert Gate(document).safe_state == "unenergised"


class TestAControlThatStartsOutsideItsWindow:
    """`safe_state` answers "something is wrong, or nothing is known yet". `end_state`
    answers "the control is deliberately not acting". A control starting outside its active
    period is the second, and used to be given the first.

    Invisible while `unenergised` was the only state either could hold - off and off - and
    consequential the moment `energised` existed: a pump with `safe_state: energised` and a
    window of 23:30 to 05:30, restarted at noon, ran all afternoon in its failure state
    during normal scheduled downtime. Nothing corrected it, and correctly so: the gate
    reports no closing edge, because the control did not *become* inactive, it started that
    way.
    """

    NOON = datetime.datetime(2026, 9, 25, 12, 0, tzinfo=datetime.timezone.utc)
    NIGHT = datetime.datetime(2026, 9, 25, 1, 0, tzinfo=datetime.timezone.utc)

    @staticmethod
    def _gate(**overrides):
        """Return a gate over a control with an overnight window.

        Args:
            **overrides: document keys to set

        Returns:
            Gate: the gate
        """
        document = conservatory()
        document.pop("enable_when", None)
        document["safe_state"] = "energised"
        document["active_period"] = {"from": "23:30", "to": "05:30", "end_state": "unenergised"}
        document.update(overrides)
        return Gate(document)

    def test_outside_the_window_it_starts_in_the_end_state(self):
        assert self._gate().starting_state(self.NOON) == "unenergised"

    def test_inside_the_window_it_starts_in_the_safe_state(self):
        """About to act, and its devices are in an unknown condition until it does."""
        assert self._gate().starting_state(self.NIGHT) == "energised"

    def test_with_no_active_period_the_safe_state_is_the_only_answer(self):
        """There is no "outside the window" for a control that has no window."""
        assert self._gate(active_period=None).starting_state(self.NOON) == "energised"

    def test_the_guard_commands_whichever_it_is_handed(self):
        commanded = []
        guard = DeviceGuard("conservatory", "energised", ["pump"], commanded.append)
        try:
            guard.assert_starting_state("unenergised")
            assert commanded == [{"pump": False}]
        finally:
            guard.stop("the test is finished")

    def test_and_defaults_to_its_own_safe_state(self):
        commanded = []
        guard = DeviceGuard("conservatory", "energised", ["pump"], commanded.append)
        try:
            guard.assert_starting_state()
            assert commanded == [{"pump": True}]
        finally:
            guard.stop("the test is finished")

    def test_the_exit_half_stays_on_the_safe_state(self):
        """A process that is ending leaves nothing behind to supervise the devices, which is
        what a safe state is for, whatever the clock says as it goes."""
        commanded = []
        guard = DeviceGuard("conservatory", "energised", ["pump"], commanded.append)
        guard.assert_starting_state("unenergised")
        guard.stop("the control is stopping")
        assert commanded == [{"pump": False}, {"pump": True}]

    def test_a_naive_moment_is_refused_rather_than_guessed_at(self):
        with pytest.raises(ConfigError):
            self._gate().starting_state(datetime.datetime(2026, 9, 25, 12, 0))


class TestSafeStatesForADeviceSetToAValue:
    """What a state means depends on how the device is driven, which is why `commands_for`
    takes the devices section rather than a list of names."""

    MIXED = {
        "lamp": {"source": "hue", "device": "Office Lamp", "parameter": "brightness_pct"},
        "heater": {"source": "hue", "device": "Heater"},
    }

    @pytest.mark.parametrize(
        "state, expected",
        [
            pytest.param("unenergised", {"lamp": 0, "heater": False}, id="unenergised"),
            pytest.param("energised", {"lamp": 100, "heater": True}, id="energised-is-full-scale"),
            pytest.param(40, {"lamp": 40, "heater": True}, id="a-value-is-on-above-zero-for-a-switch"),
            pytest.param(0, {"lamp": 0, "heater": False}, id="zero-is-off-for-both"),
        ],
    )
    def test_each_device_gets_what_the_state_means_for_it(self, state, expected):
        assert commands_for(state, self.MIXED) == expected

    def test_leave_unchanged_still_touches_nothing(self):
        assert commands_for("leave_unchanged", self.MIXED) is None

    def test_energised_is_refused_where_there_is_no_full_scale(self):
        """100 kelvin is not a bright light, it is a nonsense. Rather than send something
        plausible-looking, this says to give the value outright."""
        warm = {"lamp": {"source": "hue", "device": "L", "parameter": "color_temp_k"}}
        with pytest.raises(ConfigError, match="no meaning"):
            commands_for("energised", warm)

    def test_but_a_value_works_for_it(self):
        warm = {"lamp": {"source": "hue", "device": "L", "parameter": "color_temp_k"}}
        assert commands_for(3000, warm) == {"lamp": 3000}

    @pytest.mark.parametrize("bad", [-1, float("nan"), float("inf")])
    def test_a_value_that_is_not_a_setting_is_refused(self, bad):
        with pytest.raises(ConfigError):
            commands_for(bad, self.MIXED)

    def test_a_bare_list_of_names_still_works_as_all_switched(self):
        """Nothing in the product passes one any more, but the reading has to be the safe one
        rather than silently treating every device as a dimmer."""
        assert commands_for("energised", ("a", "b")) == {"a": True, "b": True}

    def test_a_document_may_declare_a_value_as_its_safe_state(self):
        document = conservatory()
        key = sorted(document["devices"])[0]
        document["devices"][key]["parameter"] = "brightness_pct"
        document["output"]["stages"] = [
            dict(stage, set={**stage["set"], key: 0 if stage["level"] == 0 else 100})
            for stage in document["output"]["stages"]
        ]
        document["safe_state"] = 40
        assert validate_control("conservatory", document) == []

    @pytest.mark.parametrize("bad", [-5, "dim"])
    def test_and_one_that_is_neither_a_name_nor_a_value_is_refused(self, bad):
        document = conservatory()
        document["safe_state"] = bad
        assert any("safe_state" in error for error in validate_control("conservatory", document))
