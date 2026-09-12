"""Tests for the process harness itself.

A harness nobody has watched fail is scaffolding with a green tick on it. Two things are
proven here: that the stub endpoints behave enough like the real ones that the project's
own handlers cannot tell, and that every invariant catches the violation it claims to
catch. The second half is the one that matters - an invariant checker that has only ever
been run against correct behaviour is indistinguishable from one that returns an empty list.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import os
import stat
import subprocess
import sys
import time

import pytest
import requests

from tests.harness import census, faults, invariants
from tests.harness.bridge import StubBridge
from tests.harness.influxdb import StubInflux
from tests.harness.installation import Installation, conservatory
from toinflux.controls import control_dir, load_control, validate_control
from toinflux.exceptions import SourceConnectionError
from toinflux.inputs import stored_reading
from toinflux.philipshue import Hue


@pytest.fixture
def bridge():
    """Yield a running stub bridge.

    Yields:
        StubBridge: the bridge, stopped afterwards
    """
    with StubBridge() as running:
        yield running


@pytest.fixture
def influx():
    """Yield a running stub InfluxDB.

    Yields:
        StubInflux: the database, stopped afterwards
    """
    with StubInflux({"temperature_2m": 8.5, "conservatory_temperature": 16.0}) as running:
        yield running


@pytest.fixture
def installation(tmp_path, bridge, influx):
    """Yield an installation wired to both stubs.

    Yields:
        Installation: the installation
    """
    yield Installation(tmp_path, bridge=bridge, influx=influx)


def _hue(installation):
    """Return the collector's own Hue handler, pointed at the stub bridge.

    Args:
        installation (Installation): the installation to read settings from

    Returns:
        Hue: the handler
    """
    return Hue("hue", settings_file=installation.settings_file, instance=installation.bridge.host)


class TestTheStubBridgeIsABridge:
    """Driven with `toinflux.philipshue.Hue` rather than with requests, so what is proven
    is that the code under test cannot tell the difference - its own session, its own
    authentication, its own error mapping."""

    def test_the_collector_lists_its_devices(self, installation):
        listed = {device["name"] for device in _hue(installation).mcp_list_writable_devices()}
        assert listed == {"far", "near"}

    def test_setting_a_light_changes_what_the_bridge_reports(self, installation, bridge):
        _hue(installation).mcp_set_device_state("far", on=True)
        assert bridge.energised() == {"far": True, "near": False}

    def test_every_command_is_recorded_at_the_far_end(self, installation, bridge):
        handler = _hue(installation)
        handler.mcp_set_device_state("far", on=True)
        handler.mcp_set_device_state("far", on=False)
        assert [command.state for command in bridge.commanded("far")] == [{"on": True}, {"on": False}]

    def test_an_unknown_user_is_the_clip_error_list_rather_than_a_status(self, installation, bridge):
        """A real bridge answers an unauthorised request with a 200 carrying a list, which
        is the branch the handler's list-response guard exists for. A stub that returned 401
        would leave that branch untested."""
        installation.set_settings("hue", user="not-the-user")
        with pytest.raises(SourceConnectionError, match="unauthorized"):
            _hue(installation).mcp_list_writable_devices()

    def test_an_unreachable_bridge_raises_the_project_s_own_error(self, installation, bridge):
        with faults.unreachable(bridge):
            with pytest.raises(SourceConnectionError):
                _hue(installation).mcp_list_writable_devices()

    def test_a_bridge_slower_than_the_timeout_raises(self, installation, bridge):
        """The fault that finds a missing timeout, which is why it is a number rather than
        a flag: it has to be settable either side of the client's own."""
        installation.set_settings("hue", timeout=0.2)
        with faults.hanging(bridge, 1.0):
            with pytest.raises(SourceConnectionError):
                _hue(installation).mcp_list_writable_devices()

    def test_a_bridge_answering_an_error_status_raises(self, installation, bridge):
        with faults.erroring(bridge, 503):
            with pytest.raises(SourceConnectionError):
                _hue(installation).mcp_list_writable_devices()

    def test_a_fault_clears_itself_when_the_body_raises(self, installation, bridge):
        """A fault left switched on makes the next scenario fail for a reason that has
        nothing to do with it, and the second failure is the one people read."""
        with pytest.raises(ZeroDivisionError):
            with faults.unreachable(bridge):
                raise ZeroDivisionError
        assert bridge.unreachable is False
        assert _hue(installation).mcp_list_writable_devices()


class TestTheStubInfluxIsAnInflux:
    def test_a_control_input_reads_a_value_and_an_age(self, installation):
        with requests.Session() as session:
            reading = stored_reading(
                session,
                installation.settings,
                "openmeteo",
                "temperature_2m",
                settings_file=installation.settings_file,
            )
        assert reading.value == 8.5
        assert reading.age < 5

    def test_a_frozen_source_answers_promptly_with_something_no_longer_true(self, installation, influx):
        """The fault a control has to notice by itself. Nothing fails: the far end answers
        successfully, and only the age says the collector stopped an hour ago."""
        influx.age_reading("temperature_2m", 3600)
        with faults.frozen(influx):
            with requests.Session() as session:
                reading = stored_reading(
                    session,
                    installation.settings,
                    "openmeteo",
                    "temperature_2m",
                    settings_file=installation.settings_file,
                )
        assert reading.value == 8.5
        assert reading.age > 3500

    def test_a_field_nothing_has_written_is_none_rather_than_an_error(self, installation):
        with requests.Session() as session:
            assert (
                stored_reading(
                    session,
                    installation.settings,
                    "openmeteo",
                    "dew_point_2m",
                    settings_file=installation.settings_file,
                )
                is None
            )

    def test_an_unreachable_database_raises_the_project_s_own_error(self, installation, influx):
        with faults.unreachable(influx):
            with requests.Session() as session:
                with pytest.raises(SourceConnectionError):
                    stored_reading(
                        session,
                        installation.settings,
                        "openmeteo",
                        "temperature_2m",
                        settings_file=installation.settings_file,
                    )


class TestTheInstallation:
    def test_a_control_it_writes_is_one_the_store_reads_back(self, installation, monkeypatch):
        """Through the store's own loader, in the directory the store resolves for itself,
        rather than by reading the file back with yaml."""
        installation.write_control(conservatory())
        monkeypatch.setenv("STATE_DIRECTORY", installation.state_dir)
        assert control_dir() == os.path.join(installation.state_dir, "controls")
        assert load_control("conservatory")["pid"]["input"] == "inside"

    def test_the_document_it_ships_is_structurally_valid(self):
        """The harness's own example is the one every scenario starts from, so an invalid
        one would report the control subsystem's complaint as the scenario's result."""
        assert validate_control("conservatory", conservatory()) == []

    def test_it_refuses_to_write_an_invalid_control(self, installation):
        document = conservatory()
        del document["pid"]
        with pytest.raises(AssertionError, match="invalid control"):
            installation.write_control(document)

    def test_the_settings_file_is_not_readable_by_anyone_else(self, installation):
        """The service warns about a readable settings file at startup, and a harness that
        produced that warning would train everybody to ignore it."""
        mode = stat.S_IMODE(os.stat(installation.settings_file).st_mode)
        assert mode == 0o600, oct(mode)

    def test_a_child_finds_the_state_directory_the_way_systemd_gives_it(self, installation):
        installation.write_control(conservatory())
        script = (
            "import sys;"
            f"sys.path.insert(0, {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))!r});"
            "from toinflux.controls import list_controls;"
            "print(','.join(list_controls()))"
        )
        found = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
            env=installation.environment(),
            check=True,
        )
        assert found.stdout.strip() == "conservatory"


class TestTheCensus:
    def test_it_counts_children_and_not_its_own_ps(self):
        """A census that was wrong by exactly one would be believed."""
        assert census.take(os.getpid()).processes == 0
        children = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]) for _ in range(2)]
        try:
            assert census.take(os.getpid()).processes == 2
        finally:
            for child in children:
                child.kill()
                child.wait()

    def test_it_says_which_counts_it_could_not_take(self):
        """A count that is not available reads exactly like a count of zero, and only one
        of those is true."""
        taken = census.take(os.getpid())
        for name in ("threads", "descriptors"):
            if getattr(taken, name) is None:
                assert any(name in reason for reason in taken.skipped), taken


class TestTheInvariantsCatchViolations:
    """Each one watched failing. An invariant checker that has only ever run against
    correct behaviour is indistinguishable from one that returns an empty list."""

    def test_a_device_left_on_is_a_violation(self, bridge):
        assert invariants.devices_unenergised(bridge, ["far", "near"]).violations == []
        bridge.lights["1"]["state"]["on"] = True
        assert invariants.devices_unenergised(bridge, ["far", "near"]).violations == ["'far' is still on"]

    def test_touching_a_bystander_s_device_is_a_violation(self, installation, bridge):
        since = time.monotonic()
        assert invariants.untouched_since(bridge, ["near"], since).violations == []
        _hue(installation).mcp_set_device_state("near", on=True)
        assert len(invariants.untouched_since(bridge, ["near"], since).violations) == 1

    def test_a_state_no_stage_declares_is_a_violation(self, installation, bridge):
        control = conservatory()
        handler = _hue(installation)
        handler.mcp_set_device_state("far", on=True)
        time.sleep(0.05)
        assert invariants.states_were_declared(bridge, control, settle=0.01).violations == []
        # "near on, far off" is not a rung: the ladder goes 0, far, far+near.
        handler.mcp_set_device_state("far", on=False)
        time.sleep(0.05)
        handler.mcp_set_device_state("near", on=True)
        time.sleep(0.05)
        broken = invariants.states_were_declared(bridge, control, settle=0.01).violations
        assert len(broken) == 1 and "near': True" in broken[0], broken

    def test_a_two_device_transition_is_not_read_as_a_violation(self, installation, bridge):
        """Commands arrive one device at a time, so every legitimate transition passes
        through a combination nobody asked for. Read as settled states, it is one move."""
        handler = _hue(installation)
        handler.mcp_set_device_state("far", on=True)
        handler.mcp_set_device_state("near", on=True)
        assert invariants.states_were_declared(bridge, conservatory(), settle=2.0).violations == []

    def test_a_restart_inside_the_minimum_is_a_violation(self):
        assert invariants.backoff_grew([0.0, 1.0, 3.0, 7.0], minimum=1.0).violations == []
        broken = invariants.backoff_grew([0.0, 0.2], minimum=1.0).violations
        assert len(broken) == 1 and "inside the 1.0s minimum" in broken[0]

    def test_a_backoff_that_stops_growing_is_a_violation(self):
        """A respawn loop that starts out looking like a backoff."""
        broken = invariants.backoff_grew([0.0, 2.0, 4.1, 4.6], minimum=0.4).violations
        assert len(broken) == 1 and "shorter than the previous" in broken[0]

    def test_growth_is_a_violation_and_an_uncountable_thing_is_a_skip(self):
        before = census.Census(processes=3, threads=10, descriptors=None, skipped=("no /proc here",))
        after = census.Census(processes=5, threads=10, descriptors=None)
        report = invariants.nothing_leaked(before, after)
        assert report.violations == ["processes grew from 3 to 5"]
        assert report.skipped == ["descriptors were not counted on this platform"]

    def test_a_silence_longer_than_the_tolerance_is_a_violation(self, bridge, installation):
        handler = _hue(installation)
        handler.mcp_list_writable_devices()
        handler.mcp_list_writable_devices()
        assert invariants.kept_cycling(bridge, period=60, tolerance=3).violations == []
        bridge.requests[0].at -= 600
        broken = invariants.kept_cycling(bridge, period=1, tolerance=3).violations
        assert len(broken) == 1 and "nothing was asked" in broken[0]

    def test_an_endpoint_nobody_asked_anything_is_a_violation(self, bridge):
        """Silence is not success: a control that never started would satisfy every other
        invariant here perfectly."""
        assert invariants.kept_cycling(bridge, period=60).violations == [
            "the endpoint was never asked for anything at all"
        ]


class TestCheckingThemTogether:
    def test_it_reports_every_violation_rather_than_the_first(self, bridge):
        bridge.lights["1"]["state"]["on"] = True
        with pytest.raises(AssertionError) as raised:
            invariants.check(
                invariants.devices_unenergised(bridge, ["far"]),
                invariants.backoff_grew([0.0, 0.1], minimum=1.0),
            )
        assert "is still on" in str(raised.value) and "inside the 1.0s minimum" in str(raised.value)

    def test_a_skipped_check_is_warned_about_rather_than_passed_over(self):
        report = invariants.Report(name="something", skipped=["no /proc/<pid>/fd here"])
        with pytest.warns(UserWarning, match="skipped a check"):
            invariants.check(report)

    def test_it_passes_quietly_when_nothing_is_wrong(self, bridge):
        invariants.check(invariants.devices_unenergised(bridge, ["far", "near"]))


class TestTheProcessFaults:
    def test_a_stopped_process_is_alive_and_silent(self):
        """The case a heartbeat exists for, and the case "is the process still there"
        answers wrongly."""
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys,time\nwhile True:\n print('tick', flush=True); time.sleep(0.05)"],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert child.stdout.readline().strip() == "tick"
            with faults.stopped(child):
                time.sleep(0.3)
                assert child.poll() is None, "a stopped process is still alive"
        finally:
            child.kill()
            child.wait()
            child.stdout.close()

    def test_killing_reports_the_signal_that_ended_it(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        assert faults.kill(child) == -9
