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
import socket
import ssl
import stat
import subprocess
import sys
import textwrap
import time

import pytest
import requests

from tests.harness import census, faults, invariants
from tests.harness.bridge import StubBridge, plug
from tests.harness.certificates import write_self_signed
from tests.harness.installation import conservatory
from toinflux.controls import control_dir, load_control, validate_control
from toinflux.exceptions import SourceConnectionError
from toinflux.inputs import stored_reading
from toinflux.philipshue import Hue

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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

    def test_an_unreachable_bridge_stays_unreachable_to_a_client_that_is_already_talking(self, installation, bridge):
        """The case that matters, and the one a fresh handler per call cannot see. Every
        client here holds a requests.Session, so its connection is already open when the
        fault arrives - and a check made anywhere earlier in that connection's life has
        already been passed by the time the next request exists. Measured at four answered
        requests with the fault on throughout, which would have proved a control resilient
        to an outage that never happened."""
        handler = _hue(installation)
        handler.mcp_list_writable_devices()
        answered_before = len(bridge.requests)
        with faults.unreachable(bridge):
            for _ in range(3):
                with pytest.raises(SourceConnectionError):
                    handler.mcp_list_writable_devices()
        assert len(bridge.requests) == answered_before, "the bridge answered during an outage"

    def test_a_bridge_slower_than_the_timeout_raises(self, installation, bridge):
        """The fault that finds a missing timeout, which is why it is a number rather than
        a flag: it has to be settable either side of the client's own."""
        installation.set_settings("hue", timeout=0.2)
        with faults.hanging(bridge, 1.0):
            with pytest.raises(SourceConnectionError):
                _hue(installation).mcp_list_writable_devices()

    def test_a_client_that_gives_up_leaves_no_traceback_behind(self, installation, bridge, capfd):
        """The `hanging` fault exists to make a client give up mid-request, which leaves the
        handler writing to a socket that is gone. Unhandled, http.server prints a full
        traceback per occurrence from a background thread, landing in whichever test happens
        to be running - measured as one ConnectionResetError traceback per timed-out
        request before this was handled."""
        installation.set_settings("hue", timeout=0.2)
        with faults.hanging(bridge, 0.4):
            with pytest.raises(SourceConnectionError):
                _hue(installation).mcp_list_writable_devices()
        time.sleep(0.8)
        assert "Traceback" not in capfd.readouterr().err

    @pytest.mark.parametrize(
        "exception,silent",
        [
            pytest.param(ConnectionResetError("gone"), True, id="reset-by-peer"),
            pytest.param(BrokenPipeError("gone"), True, id="broken-pipe"),
            pytest.param(ssl.SSLEOFError("gone"), True, id="tls-eof"),
            pytest.param(ssl.SSLError("handshake failure"), False, id="a-real-tls-fault"),
            pytest.param(ValueError("a bug in the stub"), False, id="a-bug-in-the-harness"),
        ],
    )
    def test_only_the_disconnect_family_is_swallowed(self, bridge, exception, silent, capfd):
        """Which exception a vanished peer produces depends on the Python version: 3.10-3.12
        raise SSLEOFError, which is not a ConnectionError, while 3.13 and later raise
        ConnectionResetError. Filtering on ConnectionError alone was silent on the two
        versions this was written on and noisy on the three older ones. Asserted directly
        rather than through a timed-out request, so it does not depend on the platform's
        TLS stack to reach the interesting case."""
        capfd.readouterr()
        try:
            raise exception
        except Exception:
            bridge._server.handle_error(None, ("127.0.0.1", 1))
        assert ("Traceback" not in capfd.readouterr().err) is silent

    def test_a_bridge_answering_an_error_status_raises(self, installation, bridge):
        with faults.erroring(bridge, 503):
            with pytest.raises(SourceConnectionError):
                _hue(installation).mcp_list_writable_devices()

    def test_it_takes_its_certificate_directory_away_with_it(self):
        """One per endpoint per test is a great many `harness-tls-*` directories under the
        system temp dir over a run, and a test process that litters outside its own tree is
        one somebody cleans up by hand."""
        endpoint = StubBridge()
        generated = endpoint._own_certificate_dir
        assert generated and os.path.isdir(generated)
        endpoint.stop()
        assert not os.path.exists(generated)

    def test_a_generated_certificate_goes_at_exit_even_if_nobody_stops_the_endpoint(self):
        """The backstop under `stop()`, in a real child process rather than a patched
        atexit: a test that dies before its fixture tears down still leaves nothing behind.
        Same shape as the control guard's exit handler, and the same limit - not on SIGKILL.
        """
        script = textwrap.dedent(f"""
            import os, sys
            sys.path.insert(0, {ROOT!r})
            from tests.harness.certificates import write_self_signed
            certificate, _ = write_self_signed()
            print(os.path.dirname(certificate), flush=True)
            """)
        finished = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "PYTHONPATH": ""},
            check=True,
        )
        directory = finished.stdout.strip()
        assert directory and not os.path.exists(directory), directory

    def test_a_server_that_will_not_stop_is_loud_about_it(self):
        """A timed join nobody checks is the silence this harness refuses everywhere else.
        A server thread that outlives its endpoint answers in the background of every test
        that follows, and surfaces as flakiness somewhere unrelated."""
        endpoint = StubBridge()
        real_shutdown = endpoint._server.shutdown
        endpoint._server.shutdown = lambda: None
        endpoint._thread.join = lambda timeout=None: None
        try:
            with pytest.raises(AssertionError, match="did not shut down"):
                endpoint.stop()
        finally:
            endpoint._server.shutdown = real_shutdown
            del endpoint._thread.join
            endpoint.stop()

    def test_a_certificate_it_was_given_is_left_alone(self, tmp_path):
        """A shared certificate outlives the endpoint using it, and removing it would break
        the next endpoint that was handed the same one."""
        shared = write_self_signed(str(tmp_path))
        endpoint = StubBridge(certificate=shared)
        endpoint.stop()
        assert os.path.exists(shared[0])

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

    def test_each_control_it_builds_is_its_own_document(self):
        """Deep-copied. The stage ladder is nested, so a shallow copy would leave every
        caller sharing one list with the constant: a test that changed a stage would change
        it for every test that ran afterwards, and the failure would land somewhere else."""
        first = conservatory()
        first["output"]["stages"][0]["set"]["far"] = True
        first["inputs"]["inside"]["source"] = "somewhere-else"
        second = conservatory()
        assert second["output"]["stages"][0]["set"]["far"] is False
        assert second["inputs"]["inside"]["source"] == "hue"

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


class TestTheNetworkGuard:
    def test_a_unix_socket_is_not_the_internet(self, tmp_path, monkeypatch):
        """Local by construction - the journal, a credential helper, a container runtime -
        so refusing one would fail tests that never left the machine.

        Connected for real rather than asserted on a refusal, because a refusal has more
        than one cause. The path is relative, because an absolute one under the temporary
        directory exceeds the 104 characters AF_UNIX allows on this platform - which is how
        the first version of this test failed.
        """
        monkeypatch.chdir(tmp_path)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind("s.sock")
            listener.listen(1)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect("s.sock")
                # Connecting at all is the assertion: the guard would have raised.
                assert client.fileno() >= 0

    def test_an_address_off_this_machine_is_refused(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as outbound:
            with pytest.raises(AssertionError, match="not this machine"):
                outbound.connect(("93.184.216.34", 80))


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

    def test_a_failed_ps_raises_rather_than_reporting_an_empty_machine(self, monkeypatch):
        """The one place in the census that raises rather than skipping. A `ps` that failed
        yields an empty table, an empty table yields no descendants, and no descendants
        reads exactly like a run that leaked nothing - the census would report the answer it
        exists to detect the absence of."""

        class _Failed:
            pid = -1
            returncode = 1

            def communicate(self, timeout=None):
                """Answer as a ps that wrote nothing and failed.

                Args:
                    timeout (float or None): ignored

                Returns:
                    tuple: empty stdout and an error on stderr
                """
                return "", "ps: unknown option\n"

        monkeypatch.setattr(census.subprocess, "Popen", lambda *a, **k: _Failed())
        with pytest.raises(AssertionError, match="ps exited 1"):
            census.take(os.getpid())

    def test_a_ps_that_will_not_finish_is_killed_rather_than_left_running(self, monkeypatch):
        """An unreaped `ps` would appear in the very count it was spawned to take."""
        killed = []

        class _Hanging:
            pid = -1
            returncode = None

            def communicate(self, timeout=None):
                """Time out the first time, and answer once killed.

                Args:
                    timeout (float or None): present on the first call only

                Returns:
                    tuple: empty output, after the kill

                Raises:
                    subprocess.TimeoutExpired: on the call that carries a timeout
                """
                if timeout is not None:
                    raise subprocess.TimeoutExpired("ps", timeout)
                return "", ""

            def kill(self):
                """Record that the child was killed."""
                killed.append(True)

        monkeypatch.setattr(census.subprocess, "Popen", lambda *a, **k: _Hanging())
        with pytest.raises(AssertionError, match="could not read the process table"):
            census.take(os.getpid())
        assert killed == [True]

    def test_it_waits_out_a_thread_that_is_still_finishing(self):
        """The difference between a leak and a handshake. A census taken the instant a
        connection closes counts the server thread still winding down, and comparing that
        against a quiet earlier reading reports growth that is not there.

        Four hundred milliseconds on purpose: the first version of this waited for two
        readings to agree, which any transient outlasting the gap between samples satisfies
        while it is still running. A leak is growth that *stays*, so the check re-reads
        until nothing exceeds the baseline.
        """
        import threading

        before = census.take(os.getpid())
        started = threading.Event()

        def briefly():
            """Live for a moment and exit."""
            started.set()
            time.sleep(0.4)

        thread = threading.Thread(target=briefly, daemon=True)
        thread.start()
        started.wait(timeout=5)
        assert census.take(os.getpid()).threads > before.threads, "the transient was not observed"
        assert census.quiet_after(before, os.getpid()).threads == before.threads
        thread.join(timeout=5)

    def test_growth_that_stays_is_still_reported(self):
        """The other half: a check that waited for quiet for ever would report nothing."""
        import threading

        before = census.take(os.getpid())
        stop = threading.Event()
        thread = threading.Thread(target=stop.wait, daemon=True)
        thread.start()
        try:
            after = census.quiet_after(before, os.getpid(), attempts=3, pause=0.05)
            assert after.threads > before.threads
        finally:
            stop.set()
            thread.join(timeout=5)

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
        # Offset from the first command, not a raw monotonic reading: that is an arbitrary
        # number of seconds since an arbitrary moment, and says nothing after a "+".
        offset = float(broken[0].split("at +")[1].split("s,")[0])
        assert 0 <= offset < 60, broken

    def test_another_control_s_command_cannot_hide_a_bad_state(self, installation, bridge):
        """The transition was settled against the next command on the *bridge*, which in a
        two-control scenario is somebody else's device. A foreign command arriving inside
        the settle window, with nothing of ours after it, left our last transition never
        settled and so never checked - in exactly the scenario this harness exists for."""
        bridge.lights["9"] = plug("someone-elses")
        handler = _hue(installation)
        # Both of ours, so the combination is complete, and it is one no stage declares:
        # the ladder goes nothing, far, far and near - never near alone.
        handler.mcp_set_device_state("far", on=False)
        handler.mcp_set_device_state("near", on=True)
        handler.mcp_set_device_state("someone-elses", on=True)
        broken = invariants.states_were_declared(bridge, conservatory(), settle=5.0).violations
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

    def test_a_stall_at_the_end_of_the_window_is_a_violation(self, bridge, installation):
        """The shape a scenario is most likely to produce - kill something, then look - and
        the one a gaps-only check reports success for, because a loop that stops leaves no
        later request to make an oversized gap with."""
        _hue(installation).mcp_list_writable_devices()
        bridge.requests[-1].at -= 600
        broken = invariants.kept_cycling(bridge, period=1, tolerance=3).violations
        assert len(broken) == 1 and "the last" in broken[0], broken

    def test_a_control_trying_against_an_unreachable_endpoint_has_not_stalled(self, installation, bridge):
        """A refused connection is still the loop running. Counting only answered requests
        would report a stall for the whole duration of every unreachable fault - failing a
        correct run, which is how an invariant ends up switched off."""
        handler = _hue(installation)
        handler.mcp_list_writable_devices()
        with faults.unreachable(bridge):
            for _ in range(3):
                with pytest.raises(SourceConnectionError):
                    handler.mcp_list_writable_devices()
        assert invariants.kept_cycling(bridge, period=60, tolerance=3).violations == []
        assert len(bridge.attempts) == 3

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
