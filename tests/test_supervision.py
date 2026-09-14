"""The static scenarios: two real controls, one scripted fault each, invariants after.

Everything here runs real processes against the stub endpoints, and every assertion reads
either the bridge's own record of what it was commanded or the operating system's account
of what is running. Nothing asks the supervisor how it thinks it behaved.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import dataclasses
import logging
import os
import sys
import time
from collections import deque
from types import SimpleNamespace

import pytest
import yaml

from tests.harness import census, faults, invariants
from tests.harness.bridge import plug
from tests.harness.installation import conservatory
from toinflux.exceptions import ConfigError
from toinflux.supervision import Supervisor, _snapshot, stall_seconds

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _refuse_to_spawn(*_args, **_kwargs):
    """Fail the way an unresolvable binary does.

    Args:
        *_args: ignored
        **_kwargs: ignored

    Raises:
        ConfigError: always
    """
    raise ConfigError("could not start 'send-to-influx': no such file")


def _quick(name, devices, **overrides):
    """Return a control with a short window, its own devices, and no active period.

    Args:
        name (str): the control's name
        devices (dict): its devices section
        **overrides: further top-level keys

    Returns:
        dict: a control document
    """
    document = conservatory(name=name, **overrides)
    document.pop("active_period", None)
    document["devices"] = devices
    document["output"] = dict(
        document["output"],
        cycle_seconds=1,
        min_transition_seconds=1,
        stages=[
            {"level": 0, "set": {key: False for key in devices}},
            {"level": 1500, "set": {key: True for key in devices}},
        ],
    )
    return document


def _bend(installation, name) -> None:
    """Overwrite one control's document with something that will not parse.

    Args:
        installation (Installation): the installation holding the control store
        name (str): the control whose document to break
    """
    path = os.path.join(installation.state_dir, "controls", f"{name}.yaml")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("output: [not a control\n")


def _two_controls(installation):
    """Write two controls that share a bridge but no devices.

    Args:
        installation (Installation): the installation to write into

    Returns:
        tuple: the two control names
    """
    installation.bridge.lights["9"] = plug("porch-heater")
    installation.write_control(_quick("conservatory", {"far": {"source": "hue", "device": "far"}}))
    installation.write_control(_quick("porch", {"porch": {"source": "hue", "device": "porch-heater"}}))
    return "conservatory", "porch"


@pytest.fixture
def supervisor(state_directory):
    """Yield a supervisor over two real controls, stopped afterwards.

    Yields:
        Supervisor: with both controls written but not yet started
    """
    installation = state_directory
    names = _two_controls(installation)

    def argv_for(name):
        """Start a control from this checkout rather than the installed console script.

        Args:
            name (str): the control to start

        Returns:
            list: the command
        """
        return [
            sys.executable,
            os.path.join(ROOT, "sendtoinflux.py"),
            "--control",
            name,
            "--settings",
            installation.settings_file,
        ]

    running = Supervisor(
        names,
        settings_file=installation.settings_file,
        argv_for=argv_for,
        # A real backoff would make every test a minute long; what matters is that it grows,
        # which has its own test against the real one.
        backoff=lambda failures: 0.05 * failures,
    )
    try:
        yield running
    finally:
        running.stop_all()


def _wait_for(supervisor, kind, name=None, seconds=30):
    """Poll until an event of this kind arrives, or fail.

    Args:
        supervisor (Supervisor): the supervisor to poll
        kind (str): the event kind to wait for
        name (str or None): the control it must concern, or any
        seconds (float): how long to wait

    Returns:
        Event: the matching event

    Raises:
        AssertionError: it never arrived
    """
    # Events left over from a previous wait, kept on the supervisor so consecutive waits
    # read one stream rather than each starting fresh. One poll can produce several events -
    # a death and the restart that follows it land in the same pass whenever the backoff is
    # shorter than the safe-state command takes - and a helper that returned on the first
    # match and discarded the rest would leave the next wait looking for something that had
    # already happened. That is exactly what failed in CI and passed here: locally the safe
    # state is applied in under the 0.05s backoff, and under coverage on a CI runner a TLS
    # handshake is not.
    pending = getattr(supervisor, "_pending_events", None)
    if pending is None:
        pending = supervisor._pending_events = deque()
    deadline = time.monotonic() + seconds
    seen = []
    while time.monotonic() < deadline:
        pending.extend(supervisor.poll(timeout=0.2))
        while pending:
            event = pending.popleft()
            seen.append(f"{event.kind}:{event.name}")
            if event.kind == kind and (name is None or event.name == name):
                return event
    # The state of every child, not just the timeout. A supervisor that stopped restarting
    # says nothing about why from the outside, and a thirty-second timeout in CI with no
    # further detail is a message that has to be reproduced before it can be read - which
    # for a timing-dependent failure on somebody else's machine is the expensive case.
    now = time.monotonic()
    state = "; ".join(
        f"{child.name}: running={child.running} failures={child.failures} "
        f"restart_in={'-' if child.restart_at is None else f'{child.restart_at - now:.2f}s'} "
        f"silent_for={now - child.last_beat:.1f}s"
        for child in supervisor.children.values()
    )
    raise AssertionError(
        f"no {kind!r} event for {name or 'any control'} within {seconds}s. "
        f"Saw: {seen or 'nothing'}. Children: {state}"
    )


class TestStartingAndWatching:
    def test_each_control_gets_its_own_process_and_beats(self, supervisor):
        supervisor.start_all()
        assert {child.process.pid for child in supervisor.children.values()} != {None}
        _wait_for(supervisor, "beat", "conservatory")
        _wait_for(supervisor, "beat", "porch")

    def test_a_stall_threshold_comes_from_the_control_s_own_window(self):
        """A control says nothing while it is spending a cycle, so a flat threshold shorter
        than the window would flag every slow control on every cycle."""
        assert stall_seconds({"output": {"cycle_seconds": 900}}) == 2700
        assert stall_seconds({"output": {"cycle_seconds": 1}}) == 30.0


class TestWhenOneDies:
    def test_the_parent_makes_the_devices_safe_itself(self, supervisor, bridge):
        """The child applies its own safe state on the way out, and a killed one did not get
        to. Commanding a device off twice is free; assuming somebody else did it is how a
        heater stays on."""
        supervisor.start_all()
        _wait_for(supervisor, "beat", "conservatory")
        bridge.lights[bridge.id_of("far")]["state"]["on"] = True
        supervisor.children["conservatory"].process.kill()
        _wait_for(supervisor, "died", "conservatory")
        assert bridge.energised()["far"] is False

    def test_it_comes_back(self, supervisor):
        supervisor.start_all()
        _wait_for(supervisor, "beat", "conservatory")
        first = supervisor.children["conservatory"].process.pid
        supervisor.children["conservatory"].process.kill()
        _wait_for(supervisor, "died", "conservatory")
        _wait_for(supervisor, "started", "conservatory")
        assert supervisor.children["conservatory"].process.pid != first

    def test_killing_one_leaves_the_other_controlling(self, supervisor, bridge):
        """Isolation, stated as what can actually be observed. The survivor keeps beating
        and keeps commanding its own device, and every state it settles into is one its own
        ladder declares.

        Not `untouched_since`: a *running* control commands its device every cycle, so
        "nobody touched it" is true only of a bystander that is idle. That invariant is for
        the idle case and this scenario is not it - the first version of this test asserted
        it anyway, passed alone and failed in a full run, which is the honest outcome for an
        assertion that was never true.

        What this cannot see is the parent making the *wrong* control safe, because
        everything-off is a rung of the survivor's ladder too. `make_safe` is tested
        directly for that below, where the question has a definite answer.
        """
        supervisor.start_all()
        _wait_for(supervisor, "beat", "conservatory")
        _wait_for(supervisor, "beat", "porch")
        supervisor.children["conservatory"].process.kill()
        _wait_for(supervisor, "died", "conservatory")
        _wait_for(supervisor, "beat", "porch")
        porch = {"porch": {"source": "hue", "device": "porch-heater"}}
        report = invariants.states_were_declared(bridge, _quick("porch", porch), settle=0.5)
        assert report.violations == [], report.violations

    def test_making_one_control_safe_touches_only_its_own_devices(self, supervisor, bridge):
        """The question the scenario above cannot answer: a parent that made every control
        safe after any death would be invisible at the bridge, because everything-off is a
        rung of every ladder."""
        bridge.lights[bridge.id_of("far")]["state"]["on"] = True
        bridge.lights[bridge.id_of("porch-heater")]["state"]["on"] = True
        bridge.clear()
        supervisor.make_safe("conservatory")
        assert [command.name for command in bridge.commanded()] == ["far"]
        assert bridge.energised()["porch-heater"] is True

    def test_a_control_that_stops_beating_is_killed_and_restarted(self, supervisor):
        """SIGSTOP: alive by every cheap test, holding its file descriptors, saying nothing.
        The case a heartbeat exists for."""
        supervisor.start_all()
        _wait_for(supervisor, "beat", "conservatory")
        child = supervisor.children["conservatory"]
        child.stall_seconds = 0.5
        with faults.stopped(child.process):
            event = _wait_for(supervisor, "stalled", "conservatory")
        assert "no heartbeat" in event.detail
        _wait_for(supervisor, "started", "conservatory")


class TestADocumentThatChanged:
    """A control edited while it is running. The parent stops it with the signal the child
    already handles, and starts it again from the new document - so every assertion here is
    about a *deliberate* stop: what the bridge was commanded afterwards, and that nothing
    treated the exit as a failure."""

    def test_the_new_document_is_what_comes_back(self, supervisor, state_directory, bridge):
        """A second device, which the document it was started with could not name at all.
        Asserting on the pid alone would pass for a supervisor that restarted the control
        from the old document, which is the bug worth catching."""
        supervisor.start_all()
        _wait_for(supervisor, "beat", "conservatory")
        first = supervisor.children["conservatory"].process.pid
        bridge.lights["10"] = plug("spare-heater")
        state_directory.write_control(
            _quick(
                "conservatory",
                {"far": {"source": "hue", "device": "far"}, "spare": {"source": "hue", "device": "spare-heater"}},
            )
        )
        bridge.clear()
        supervisor.request_reload("conservatory")
        _wait_for(supervisor, "reloaded", "conservatory")
        _wait_for(supervisor, "beat", "conservatory")
        assert supervisor.children["conservatory"].process.pid != first
        assert "spare-heater" in {command.name for command in bridge.commanded()}
        # The operating system's account rather than the supervisor's. A reload that started
        # the new process without stopping the old one would satisfy every assertion above
        # and leave two controls commanding the same heater from different documents, which
        # is the one outcome worse than the edit not taking effect at all.
        with pytest.raises(ProcessLookupError):
            os.kill(first, 0)

    def test_it_is_not_counted_against_the_control(self, supervisor, caplog):
        """The whole point of a deliberate stop. Left to the ordinary death path, the exit
        would be an ERROR in the journal, a failure on the child, and a new document that
        waits out a backoff earned by the old one."""
        supervisor.start_all()
        _wait_for(supervisor, "beat", "conservatory")
        supervisor.children["conservatory"].failures = 4
        with caplog.at_level(logging.ERROR, logger="root"):
            supervisor.request_reload("conservatory")
            _wait_for(supervisor, "started", "conservatory")
        assert supervisor.children["conservatory"].failures == 0
        assert "conservatory" not in caplog.text

    def test_a_death_is_still_a_death(self, supervisor):
        """The other half of the same claim: the deliberate path must not have made every
        exit free. A control that is killed still counts a failure and still backs off."""
        supervisor.start_all()
        _wait_for(supervisor, "beat", "conservatory")
        supervisor.children["conservatory"].process.kill()
        _wait_for(supervisor, "died", "conservatory")
        assert supervisor.children["conservatory"].failures == 1

    def test_a_run_of_saves_restarts_it_once(self, supervisor):
        """A client saving three edits in a row should not stop and start a heater three
        times, and the document on disk now is the only one any of them was asking for."""
        supervisor.start_all()
        _wait_for(supervisor, "beat", "conservatory")
        for _ in range(3):
            supervisor.request_reload("conservatory")
        events = supervisor.poll(timeout=0.2)
        assert [event.kind for event in events].count("reloaded") == 1

    def test_a_newly_stored_control_is_taken_on(self, supervisor, state_directory, bridge):
        """Without this a control saved while the collector is running does nothing at all
        until somebody restarts the service, which is the surprise the restart-on-edit
        design exists to avoid."""
        bridge.lights["11"] = plug("study-heater")
        state_directory.write_control(_quick("study", {"study": {"source": "hue", "device": "study-heater"}}))
        assert "study" not in supervisor.children
        supervisor.request_reload("study")
        _wait_for(supervisor, "beat", "study")
        assert supervisor.children["study"].running

    def test_a_deleted_document_stops_the_control_and_forgets_it(self, supervisor, state_directory, bridge):
        supervisor.start_all()
        _wait_for(supervisor, "beat", "conservatory")
        os.remove(os.path.join(state_directory.state_dir, "controls", "conservatory.yaml"))
        supervisor.request_reload("conservatory")
        _wait_for(supervisor, "dropped", "conservatory")
        assert "conservatory" not in supervisor.children
        _wait_for(supervisor, "beat", "porch")

    def test_a_deleted_document_still_makes_its_devices_safe(self, supervisor, state_directory, bridge):
        """The case the started-from copy exists for. The process is killed, so its own
        guard never ran, and the document is gone, so there is nothing on disk to read - the
        only description of which devices this control owned is the one the parent kept."""
        supervisor.start_all()
        _wait_for(supervisor, "beat", "conservatory")
        bridge.lights[bridge.id_of("far")]["state"]["on"] = True
        supervisor.children["conservatory"].process.kill()
        os.remove(os.path.join(state_directory.state_dir, "controls", "conservatory.yaml"))
        bridge.clear()
        supervisor.request_reload("conservatory")
        _wait_for(supervisor, "dropped", "conservatory")
        assert bridge.energised()["far"] is False
        # Not an exhaustive list: the other control is still running and commanding its own
        # device throughout. That `far` was commanded at all is the claim, because the only
        # thing that could have done it is the parent working from its kept copy.
        assert "far" in [command.name for command in bridge.commanded()]

    def test_a_document_that_will_not_read_leaves_the_control_running(self, supervisor, state_directory, caplog):
        """A half-finished hand edit is not a reason to stop a control that is holding a
        room at temperature. The next reload gets another go."""
        supervisor.start_all()
        _wait_for(supervisor, "beat", "conservatory")
        running = supervisor.children["conservatory"].process.pid
        _bend(state_directory, "conservatory")
        with caplog.at_level(logging.ERROR):
            supervisor.request_reload("conservatory")
            event = _wait_for(supervisor, "reload-failed", "conservatory")
        assert supervisor.children["conservatory"].process.pid == running
        assert "not valid YAML" in event.detail
        assert "still running the document it started with" in caplog.text
        _wait_for(supervisor, "beat", "conservatory")

    def test_an_ordinary_restart_picks_up_an_edit_nobody_announced(self, supervisor, state_directory):
        """A control can be edited by hand and then die on its own, with no reload asked for.
        The child re-reads its own document on the way up either way, so a parent still
        working from the copy it read at construction would hold a stall threshold computed
        from a cycle window that no longer exists - and kill the control for silence it is
        entitled to."""
        supervisor.start_all()
        _wait_for(supervisor, "beat", "conservatory")
        assert supervisor.children["conservatory"].stall_seconds == 30.0
        edited = _quick("conservatory", {"far": {"source": "hue", "device": "far"}})
        edited["output"]["cycle_seconds"] = 20
        state_directory.write_control(edited)
        supervisor.children["conservatory"].process.kill()
        _wait_for(supervisor, "died", "conservatory")
        _wait_for(supervisor, "started", "conservatory")
        assert supervisor.children["conservatory"].stall_seconds == 60.0

    def test_an_ordinary_restart_lets_go_of_a_device_the_control_gave_up(self, supervisor, state_directory, bridge):
        """The same staleness at the bridge rather than in an attribute. Making a control
        safe commands the devices its process was started with as well as the ones the file
        names now, so a parent holding a copy from before the edit would keep switching off
        a device this control no longer owns - and would do it to whichever control owns it
        next."""
        supervisor.start_all()
        _wait_for(supervisor, "beat", "conservatory")
        bridge.lights["13"] = plug("annexe-heater")
        state_directory.write_control(_quick("conservatory", {"annexe": {"source": "hue", "device": "annexe-heater"}}))
        supervisor.children["conservatory"].process.kill()
        _wait_for(supervisor, "died", "conservatory")
        _wait_for(supervisor, "started", "conservatory")
        _wait_for(supervisor, "beat", "conservatory")
        bridge.clear()
        supervisor.make_safe("conservatory")
        # Membership rather than an exact list: the other control is running throughout and
        # commands its own device on its own cycle. `far` is the claim - a parent still
        # holding the copy from before the edit would switch off a device this control has
        # given up, and would do it to whichever control owns it next.
        commanded = {command.name for command in bridge.commanded()}
        assert "annexe-heater" in commanded
        assert "far" not in commanded

    def test_a_start_that_cannot_re_read_keeps_the_copy_it_has_and_says_so(self, supervisor, state_directory, caplog):
        """A document that will not read does not stop the control being started: the child
        reads the same file and fails on it in its own process, where the restart path
        already handles it."""
        _bend(state_directory, "conservatory")
        with caplog.at_level(logging.WARNING):
            supervisor.start("conservatory")
        assert supervisor.children["conservatory"].running
        assert "still working from the document it last read" in caplog.text

    def test_a_start_that_cannot_re_read_and_has_no_copy_says_that_instead(self, supervisor, state_directory, caplog):
        """The same failure with nothing to fall back on, which is the case an operator
        actually needs woken up for: the parent has no description of this control's devices
        and cannot make them safe. Reachable because a control taken on by a reload is built
        with no document and its file was readable only a moment earlier.

        `document` is cleared by hand because that is the state `_reload` builds. Racing the
        window between its read and the start would be a race rather than a test.
        """
        _bend(state_directory, "conservatory")
        supervisor.children["conservatory"].document = None
        with caplog.at_level(logging.WARNING):
            supervisor.start("conservatory")
        assert "no description of this control at all" in caplog.text
        assert "still working from the document it last read" not in caplog.text

    def test_a_reload_asked_for_while_stopping_is_refused_and_says_so(
        self, supervisor, state_directory, bridge, caplog
    ):
        """The request arrives from another thread, so it can arrive during shutdown. It is
        refused rather than queued, because a control started after stop_all would outlive
        the supervisor with nothing watching it - and refused audibly, because a save that
        appears to have been accepted and was not is the worse failure.

        Asserted on the log rather than on a later poll: stop_all closes the selector, so
        there is no later poll to make. That is the shape of the real shutdown too, where
        the loop's own finally calls it and the loop has already ended.
        """
        supervisor.start_all()
        _wait_for(supervisor, "beat", "conservatory")
        supervisor.stop_all()
        bridge.lights["12"] = plug("late-heater")
        state_directory.write_control(_quick("late", {"late": {"source": "hue", "device": "late-heater"}}))
        with caplog.at_level(logging.DEBUG):
            supervisor.request_reload("late")
        assert "the supervisor is stopping" in caplog.text
        assert "late" not in supervisor.children


class TestTheRunAsAWhole:
    def test_nothing_leaks_across_a_kill_and_restart(self, supervisor):
        """A supervisor that leaked a descriptor or a zombie per restart would report itself
        healthy throughout, and the only place the truth exists is the kernel's accounting."""
        supervisor.start_all()
        _wait_for(supervisor, "beat", "conservatory")
        # Settled on both sides. A census taken while the stub bridge is still winding down
        # the connection that made a control's devices safe counts that thread and its
        # socket, and reports a handshake as a leak - which is what this test did in CI
        # while passing here, where descriptors are not counted at all.
        before = census.take(os.getpid())
        for _ in range(3):
            supervisor.children["conservatory"].process.kill()
            _wait_for(supervisor, "died", "conservatory")
            _wait_for(supervisor, "started", "conservatory")
            _wait_for(supervisor, "beat", "conservatory")
        invariants.check(invariants.nothing_leaked(before, census.quiet_after(before, os.getpid())))

    def test_stopping_leaves_nothing_running_and_nothing_energised(self, supervisor, bridge):
        supervisor.start_all()
        _wait_for(supervisor, "beat", "conservatory")
        _wait_for(supervisor, "beat", "porch")
        supervisor.stop_all()
        assert all(child.process is None for child in supervisor.children.values())
        invariants.check(invariants.devices_unenergised(bridge, ["far", "porch-heater"]))


class TestTheBackoffItself:
    def test_it_grows_and_never_collapses(self):
        """Measured against the real backoff rather than the fast one the scenarios inject,
        because the property being claimed is about the real one."""
        from toinflux.supervision import _default_backoff

        starts, moment = [], 0.0
        for failures in range(1, 6):
            moment += _default_backoff(failures)
            starts.append(moment)
        invariants.check(invariants.backoff_grew(starts, minimum=5))


class TestTheRestartDecisionItself:
    """The restart logic on its own, with no processes and a clock the test owns.

    The scenarios above prove the whole thing works; this proves *why*, deterministically,
    and is where a restart that quietly never fires shows up as a failure rather than as a
    thirty-second timeout in CI.
    """

    class _Clock:
        """A monotonic clock the test advances by hand."""

        def __init__(self):
            self.now = 1000.0

        def __call__(self):
            """Return the current reading.

            Returns:
                float: the reading
            """
            return self.now

    def _supervisor(self, state_directory, clock):
        """Return a supervisor over one control that will never really be started.

        Args:
            state_directory (Installation): the installation to read controls from
            clock (callable): the clock to use

        Returns:
            Supervisor: ready to have its children driven by hand
        """
        _two_controls(state_directory)
        return Supervisor(
            ["conservatory"],
            settings_file=state_directory.settings_file,
            argv_for=lambda name: ["/bin/true"],
            clock=clock,
            backoff=lambda failures: 10.0 * failures,
        )

    def test_a_control_is_restarted_once_its_backoff_has_passed(self, state_directory, monkeypatch):
        clock = self._Clock()
        supervisor = self._supervisor(state_directory, clock)
        child = supervisor.children["conservatory"]
        child.restart_at = clock.now + 10
        started = []
        monkeypatch.setattr(supervisor, "start", lambda name: started.append(name))

        supervisor.poll(timeout=0)
        assert started == [], "restarted before its backoff had passed"
        clock.now += 10
        supervisor.poll(timeout=0)
        assert started == ["conservatory"]

    def test_a_control_that_will_not_start_does_not_take_the_others_down(self, state_directory, monkeypatch):
        """One control that cannot be started is one control's problem. Letting the failure
        out of poll() would end the loop and with it every other control's supervision -
        one unstartable control taking the heating down with it."""
        clock = self._Clock()
        supervisor = self._supervisor(state_directory, clock)
        child = supervisor.children["conservatory"]
        child.restart_at = clock.now

        def refuse(name):
            """Fail to start, as an unresolvable binary would.

            Args:
                name (str): the control being started

            Raises:
                ConfigError: always
            """
            raise ConfigError("could not start 'send-to-influx': no such file")

        monkeypatch.setattr(supervisor, "start", refuse)
        events = supervisor.poll(timeout=0)
        assert [event.kind for event in events] == ["start-failed"]
        # And it backs off rather than spinning on the failure every pass.
        assert child.restart_at > clock.now
        assert supervisor.poll(timeout=0) == []

    def test_a_failed_start_leaks_no_descriptor(self, state_directory, monkeypatch):
        """A restart that keeps failing would otherwise leak one descriptor per attempt, and
        a supervisor out of descriptors stops being able to start anything - the failure
        arriving long after the control that caused it."""
        supervisor = self._supervisor(state_directory, self._Clock())
        handed_out = []
        real_pipe = os.pipe

        def watched_pipe():
            """Hand out a pipe and remember both ends.

            Returns:
                tuple: the read and write descriptors
            """
            pair = real_pipe()
            handed_out.append(pair)
            return pair

        monkeypatch.setattr("toinflux.supervision.os.pipe", watched_pipe)
        monkeypatch.setattr("toinflux.supervision.spawn", _refuse_to_spawn)
        with pytest.raises(ConfigError):
            supervisor.start("conservatory")
        assert handed_out, "the test did not observe a pipe being made"
        for descriptor in handed_out[0]:
            with pytest.raises(OSError):
                os.fstat(descriptor)

    def test_running_out_of_descriptors_is_that_control_s_failure(self, state_directory, monkeypatch):
        """An OSError from os.pipe would go straight past the restart path, which handles
        this project's own types - one control's exhaustion becoming every control's
        outage. It arrives as a ConfigError, so the restart logic isolates it."""
        supervisor = self._supervisor(state_directory, self._Clock())

        def refuse():
            """Fail the way a process out of descriptors does.

            Raises:
                OSError: always
            """
            raise OSError(24, "Too many open files")

        monkeypatch.setattr("toinflux.supervision.os.pipe", refuse)
        with pytest.raises(ConfigError, match="heartbeat pipe"):
            supervisor.start("conservatory")

    def test_a_control_with_no_restart_due_is_left_alone(self, state_directory, monkeypatch):
        clock = self._Clock()
        supervisor = self._supervisor(state_directory, clock)
        started = []
        monkeypatch.setattr(supervisor, "start", lambda name: started.append(name))
        clock.now += 10_000
        supervisor.poll(timeout=0)
        assert started == [], "started a control that had not been running"


class TestOneBadControlDocument:
    """A corrupt stored document is that control's problem. The supervisor is built inside
    the collector's own main process, so anything that escapes here takes the collection
    down with it - over a file nobody is using."""

    @pytest.mark.parametrize(
        "cycle",
        [
            pytest.param("soon", id="not-a-number"),
            pytest.param(True, id="a-bool-is-not-one-second"),
            pytest.param(float("nan"), id="nan"),
            pytest.param(float("inf"), id="inf"),
            pytest.param(0, id="zero"),
            pytest.param(-5, id="negative"),
        ],
    )
    def test_an_unusable_cycle_window_is_a_config_error_not_a_crash(self, cycle):
        with pytest.raises(ConfigError, match="cycle_seconds"):
            stall_seconds({"output": {"cycle_seconds": cycle}})

    def test_a_control_that_cannot_be_read_is_skipped_rather_than_fatal(self, state_directory, caplog):
        _two_controls(state_directory)
        with caplog.at_level(logging.ERROR):
            supervisor = Supervisor(["conservatory", "nosuchcontrol"], settings_file=state_directory.settings_file)
        assert list(supervisor.children) == ["conservatory"]
        assert "nosuchcontrol" in caplog.text and "skipped" in caplog.text


class TestADocumentThatIsNotTheRightShape:
    """A stored document parses as YAML and can still be any shape at all. Each wrong shape
    produces its own AttributeError or TypeError somewhere downstream, in a process that is
    also running the collector - so the check belongs where the document is read."""

    @pytest.mark.parametrize(
        "broken",
        [
            pytest.param({"output": "soon"}, id="output-is-a-string"),
            pytest.param({"devices": ["far"]}, id="devices-is-a-list"),
            pytest.param({"inputs": 7}, id="inputs-is-a-number"),
        ],
    )
    def test_it_is_skipped_rather_than_crashing_the_supervisor(self, state_directory, caplog, broken):
        _two_controls(state_directory)
        document = dict(conservatory(), **broken)
        path = os.path.join(state_directory.state_dir, "controls", "bent.yaml")
        with open(path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(document, handle)
        with caplog.at_level(logging.ERROR):
            supervisor = Supervisor(["conservatory", "bent"], settings_file=state_directory.settings_file)
        assert list(supervisor.children) == ["conservatory"]
        assert "bent" in caplog.text

    def test_a_bent_document_falls_back_to_the_one_the_process_was_started_with(self, state_directory, bridge, caplog):
        """The path that runs when something has already gone wrong, and the file has been
        rewritten since the supervisor read it. Reading the file is how the parent learns
        about a device that has just been *added*; the copy the process was started with is
        how it still knows about the ones that were there all along. A bent file leaves only
        the second, and a heater is not left on because a YAML document lost its shape."""
        _two_controls(state_directory)
        supervisor = Supervisor(["conservatory"], settings_file=state_directory.settings_file)
        path = os.path.join(state_directory.state_dir, "controls", "conservatory.yaml")
        with open(path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(dict(conservatory(), devices=["far"]), handle)
        bridge.lights[bridge.id_of("far")]["state"]["on"] = True
        bridge.clear()
        with caplog.at_level(logging.WARNING):
            supervisor.make_safe("conservatory")
        assert bridge.energised()["far"] is False
        assert [command.name for command in bridge.commanded()] == ["far"]
        assert "made safe from the document it was started with" in caplog.text

    def test_a_bent_document_with_nothing_to_fall_back_on_says_so_rather_than_raising(self, state_directory, caplog):
        """No supervised child means no started-from copy, so the file is the only
        description there was and there is now nothing to command."""
        _two_controls(state_directory)
        supervisor = Supervisor([], settings_file=state_directory.settings_file)
        path = os.path.join(state_directory.state_dir, "controls", "conservatory.yaml")
        with open(path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(dict(conservatory(), devices=["far"]), handle)
        with caplog.at_level(logging.ERROR):
            supervisor.make_safe("conservatory")
        assert "made safe from nothing at all" in caplog.text


class TestFindingTheConsoleScript:
    """Where the supervisor looks for the thing it starts. On the packaged install the
    console script is not on the PATH at all - it lives in /opt/send-to-influx/venv/bin and
    only send-to-influx-set-credential is symlinked into /usr/sbin, while the unit sets no
    Environment=PATH. Resolving by name alone means no control ever starts there, and every
    test in this file passes anyway because a checkout has it on the PATH."""

    def test_it_looks_beside_the_running_interpreter_first(self, state_directory, tmp_path, monkeypatch):
        interpreter = tmp_path / "bin" / "python3"
        interpreter.parent.mkdir(parents=True)
        interpreter.touch()
        script = tmp_path / "bin" / "send-to-influx"
        script.touch()
        monkeypatch.setattr("toinflux.supervision.sys.executable", str(interpreter))
        _two_controls(state_directory)
        supervisor = Supervisor(["conservatory"], settings_file=state_directory.settings_file)
        assert supervisor._default_argv("conservatory")[0] == str(script)

    def test_it_falls_back_to_the_path_where_there_is_no_such_script(self, state_directory, tmp_path, monkeypatch):
        """A layout this does not know about is not a reason to refuse: the PATH is still
        where a console script usually is."""
        interpreter = tmp_path / "elsewhere" / "python3"
        interpreter.parent.mkdir(parents=True)
        interpreter.touch()
        monkeypatch.setattr("toinflux.supervision.sys.executable", str(interpreter))
        _two_controls(state_directory)
        supervisor = Supervisor(["conservatory"], settings_file=state_directory.settings_file)
        assert supervisor._default_argv("conservatory")[0] == "send-to-influx"


class TestStoppingTwice:
    """Two callers reach `stop_all` in an ordinary shutdown: the supervisor's own loop when
    SHUTDOWN is set, and the atexit handler covering a signal. It was idempotent by accident
    of the selector implementations rather than by anything in this code."""

    def test_the_second_call_closes_nothing_a_second_time(self, supervisor, bridge, monkeypatch):
        """Counted at the selector, because that is the only thing the second call would
        otherwise touch: every child is already stopped, so asserting no devices were
        commanded passes with or without the guard - which is what the first version of this
        test did.
        """
        supervisor.start_all()
        _wait_for(supervisor, "beat", "conservatory")
        closes = []
        real_close = supervisor._selector.close
        monkeypatch.setattr(supervisor._selector, "close", lambda: (closes.append(True), real_close())[1])

        supervisor.stop_all()
        supervisor.stop_all()
        assert len(closes) == 1, f"the selector was closed {len(closes)} times"
        assert all(child.process is None for child in supervisor.children.values())


class TestTheStatusSnapshot:
    def test_it_reports_a_control_that_has_never_started(self, state_directory):
        """`silent_for` is None rather than the age of the process: a control that has not
        started has not been silent, it has not been asked."""
        _two_controls(state_directory)
        supervisor = Supervisor(["conservatory"], settings_file=state_directory.settings_file)
        (status,) = supervisor.status()
        assert status.name == "conservatory"
        assert status.running is False
        assert status.pid is None
        assert status.silent_for is None

    def test_it_is_a_snapshot_rather_than_a_view(self, state_directory):
        """Frozen, and taken by value. A reader assembling a report out of the live child
        would describe a moment that never existed, because the supervisor's own thread
        rewrites it between one attribute read and the next."""
        _two_controls(state_directory)
        supervisor = Supervisor(["conservatory"], settings_file=state_directory.settings_file)
        (status,) = supervisor.status()
        supervisor.children["conservatory"].failures = 7
        assert status.failures == 0
        with pytest.raises(dataclasses.FrozenInstanceError):
            status.failures = 1

    def test_an_unusable_control_is_absent_because_it_is_not_supervised(self, state_directory):
        """It was skipped at construction, so the supervisor has nothing to say about it.
        `list_controls` still lists it, from the store, which is the division of labour."""
        path = os.path.join(state_directory.state_dir, "controls", "bent.yaml")
        with open(path, "w", encoding="utf-8") as handle:
            yaml.safe_dump({"name": "bent"}, handle)
        supervisor = Supervisor(["bent"], settings_file=state_directory.settings_file)
        assert supervisor.status() == ()

    def test_every_field_is_read_once(self):
        """The supervisor's own thread clears `process` and `restart_at` as a control dies
        and starts again. A ternary that tests an attribute and then reads it again tests
        one value and uses another - `process` passing the None check and being None by the
        time `.pid` is asked for is an AttributeError on the MCP thread, which is one
        control's restart breaking every caller's listing.

        A child whose attributes change on every access, so a second read cannot be
        mistaken for the first.
        """

        class Flipping:
            """A child that answers differently each time it is asked."""

            name = "flip"
            failures = 2
            started_at = 1.0
            last_beat = 2.0

            def __init__(self):
                self.reads = {"process": 0, "restart_at": 0}

            @property
            def process(self):
                """Return a process the first time and None afterwards.

                Returns:
                    object or None: the stand-in process, then None
                """
                self.reads["process"] += 1
                return SimpleNamespace(pid=99) if self.reads["process"] == 1 else None

            @property
            def restart_at(self):
                """Return a deadline the first time and None afterwards.

                Returns:
                    float or None: the deadline, then None
                """
                self.reads["restart_at"] += 1
                return 12.0 if self.reads["restart_at"] == 1 else None

        child = Flipping()
        status = _snapshot(child, now=10.0)
        assert child.reads == {"process": 1, "restart_at": 1}
        # Consistent with each other, because they came from the same read.
        assert status.running is True
        assert status.pid == 99
        assert status.restart_in == 2.0
