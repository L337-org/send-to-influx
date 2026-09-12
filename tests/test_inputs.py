"""Tests for reading a control's inputs.

The live-fetch floor is the part with a decision behind it rather than a mechanism: it is
per source rather than per control, and it defaults to the source's own collection
interval. Both of those are properties a later change could quietly undo, so they are
asserted here rather than left to the design note.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT"

import os
import random
import signal

# Spawning a real holder is the point of these tests: the properties are the kernel's. The
# hygiene guard that routes process execution through toinflux.process scopes itself to
# product code, so this import is within the rule rather than an exception to it.
import subprocess
import sys
import textwrap
import time

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from toinflux.exceptions import ConfigError, SourceConnectionError, ToolParamError
from toinflux.influx import InfluxWriteError
from toinflux.inputs import (
    MAX_LOCK_BACKOFF,
    MIN_LOCK_BACKOFF,
    InputReading,
    fetch_lock,
    fetch_lock_path,
    read_input,
    resolve_minimum_interval,
    stored_reading,
)


@contextmanager
def _never_held():
    """Stand in for a fetch lock another process is holding for longer than the budget."""
    yield False


def _holder_script(hold_seconds):
    """Return a script that takes the fetch lock, says so, and holds it.

    A real process rather than a thread: the properties being tested are the kernel's, and
    a thread in this interpreter would share the descriptor and prove nothing.
    """
    return textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))!r})
        from toinflux.inputs import fetch_lock
        with fetch_lock("probe", budget=5) as held:
            print("held" if held else "gave-up", flush=True)
            time.sleep({hold_seconds})
        """)


def _start_holder(state_dir, hold_seconds):
    """Start a holder and return it once it reports the lock is taken.

    The child finds the lock through STATE_DIRECTORY in its environment, which is what a
    real control process does, so state_dir is passed as that rather than as an argument.
    """
    child = subprocess.Popen(
        [sys.executable, "-c", _holder_script(hold_seconds)],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "STATE_DIRECTORY": str(state_dir), "PYTHONPATH": ""},
    )
    assert child.stdout.readline().strip() == "held"
    return child


class TestResolveMinimumInterval:
    def test_an_explicit_floor_wins(self):
        assert resolve_minimum_interval("hue", {"hue": {"interval": 300, "minimum_interval": 60}}) == 60.0

    def test_the_source_class_value_beats_the_collection_interval(self):
        """The class value describes what the far end tolerates; the interval describes how
        often this operator wants data. Reading the floor off the interval would be hours
        wrong in either direction - openmeteo ships at 900 and the API tolerates 600."""
        assert resolve_minimum_interval("openmeteo", {"openmeteo": {"interval": 21600}}) == 600.0

    def test_an_operator_override_beats_the_source_class(self):
        """Only the operator knows their own estate: a bridge on a congested network may
        want more room than the class assumes."""
        assert resolve_minimum_interval("hue", {"hue": {"interval": 300, "minimum_interval": 45}}) == 45.0

    def test_a_source_that_declares_nothing_falls_back_to_the_interval(self, monkeypatch):
        """No shipped source does - a hygiene test says so - but the fallback has to work
        for one under development, and it must not be silently zero."""
        handler = MagicMock()
        handler.MINIMUM_INTERVAL = None
        monkeypatch.setattr("toinflux.inputs.source_class", lambda source: handler)
        assert resolve_minimum_interval("hue", {"hue": {"interval": 300}}) == 300.0

    def test_zero_is_a_floor_rather_than_a_missing_value(self):
        """Zero means "ask whenever a control wants it", which is right for a source that is
        cheap to read. A falsy check would silently substitute the interval instead."""
        assert resolve_minimum_interval("hue", {"hue": {"interval": 300, "minimum_interval": 0}}) == 0.0

    def test_the_floor_is_read_per_source(self):
        """The floor binds every control sharing a source, so it cannot come from one
        control's document: two controls each honouring 60 s still reach the device at 30 s
        combined. This test is the structural half of that - one settings document, two
        sources, two different answers."""
        settings = {"hue": {"interval": 300}, "openmeteo": {"interval": 1800, "minimum_interval": 1200}}
        assert resolve_minimum_interval("hue", settings) == 10.0
        assert resolve_minimum_interval("openmeteo", settings) == 1200.0

    def test_the_source_name_is_case_insensitive(self):
        """get_class() accepts any case and lowercases, and settings sections are
        canonically lowercase because validate_settings matches them against
        known_sources(). A control spec saying "Hue" must land on the same floor."""
        assert resolve_minimum_interval("Hue", {"hue": {"interval": 300, "minimum_interval": 60}}) == 60.0

    @pytest.mark.parametrize("settings", [{}, {"hue": None}, {"hue": "300"}, {"hue": []}])
    def test_a_source_without_a_usable_section_is_a_config_error(self, settings):
        """Including a section present but empty, which is what commenting out every field
        leaves behind and parses as null."""
        with pytest.raises(ConfigError, match="cannot resolve the live-fetch floor"):
            resolve_minimum_interval("hue", settings)

    def test_a_section_with_nothing_usable_names_the_setting_to_add(self, monkeypatch):
        """Only reachable for a source declaring no class value, since otherwise that
        answers first. The message still has to say what to write, because the reader is an
        operator looking at a journal line rather than at this code."""
        handler = MagicMock()
        handler.MINIMUM_INTERVAL = None
        monkeypatch.setattr("toinflux.inputs.source_class", lambda source: handler)
        with pytest.raises(ConfigError, match="'hue.interval' is required"):
            resolve_minimum_interval("hue", {"hue": {"db": "x"}})

    @pytest.mark.parametrize("bad", [True, False, "60", None, [60]])
    def test_a_non_numeric_floor_is_refused(self, bad):
        """A bool included: `bool` subclasses `int`, so `minimum_interval: true` would otherwise
        behave as a one-second floor, which is not what typing `true` meant."""
        with pytest.raises(ConfigError, match="must be a number of seconds|is required"):
            resolve_minimum_interval("hue", {"hue": {"interval": 300, "minimum_interval": bad}})

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_a_non_finite_floor_is_refused(self, bad):
        """YAML .nan and .inf are floats, so type and sign checks both pass them, and
        neither fails loudly later - which is the problem. A nan floor compares False
        against any age, so the floor never holds and every cycle goes live; an inf floor
        compares True, so nothing ever does."""
        with pytest.raises(ConfigError, match="must be a finite number of seconds"):
            resolve_minimum_interval("hue", {"hue": {"interval": 300, "minimum_interval": bad}})

    def test_a_negative_floor_is_refused(self):
        with pytest.raises(ConfigError, match="must not be negative"):
            resolve_minimum_interval("hue", {"hue": {"interval": 300, "minimum_interval": -5}})

    def test_a_bad_interval_is_refused_when_it_is_the_fallback(self, monkeypatch):
        """The fallback path validates too. An unusable interval reaching a control as a
        floor of None would fail much later, somewhere less obvious."""
        handler = MagicMock()
        handler.MINIMUM_INTERVAL = None
        monkeypatch.setattr("toinflux.inputs.source_class", lambda source: handler)
        with pytest.raises(ConfigError, match="'hue.interval' must be a number of seconds"):
            resolve_minimum_interval("hue", {"hue": {"interval": "300"}})


class TestTheFetchLockSerialisesLiveFetches:
    """The lock exists so two controls crossing max_age together do not both go live.

    Every test here runs a real second process. A thread in this interpreter shares the
    file descriptor, and flock is per descriptor, so a threaded version of these tests
    would pass against code that locks nothing.
    """

    def test_an_uncontended_control_is_not_delayed(self, tmp_path, monkeypatch):
        """The holder proceeds immediately. A design that made everyone wait a little would
        put a delay on the common path to fix the rare one.

        Asserted as "never sleeps" rather than "took under 0.1s": the property is that the
        backoff path is not entered at all, and a wall-clock threshold would test how busy
        the machine is as much as what the code does.
        """
        monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
        slept = []
        with fetch_lock("probe", budget=5, sleep=slept.append) as held:
            assert held
        assert slept == [], f"waited on an uncontended lock: {slept}"

    def test_a_second_process_waits_for_the_holder(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
        holder = _start_holder(tmp_path, hold_seconds=0.75)
        try:
            started = time.monotonic()
            with fetch_lock("probe", budget=5) as held:
                waited = time.monotonic() - started
            assert held, "the waiter never got the lock the holder released"
            assert waited > 0.25, f"acquired after only {waited:.3f}s, so nothing was actually locked"
        finally:
            holder.wait(timeout=10)

    def test_the_kernel_releases_the_lock_when_the_holder_is_killed(self, tmp_path, monkeypatch):
        """The reason flock was chosen over a lock file carrying a PID.

        This subsystem is about processes being killed. A lock needing manual cleanup after
        a SIGKILL would be worse than the problem it solves, and no atexit handler runs
        here, so nothing in this project's own code releases it.
        """
        monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
        holder = _start_holder(tmp_path, hold_seconds=30)
        # Both halves, or this passes against a lock that locks nothing: first prove the
        # holder really excludes us, and only then that killing it lets us straight in.
        with fetch_lock("probe", budget=0.2) as held:
            assert held is False, "the holder was not actually excluding anyone"
        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=10)
        started = time.monotonic()
        with fetch_lock("probe", budget=5) as held:
            assert held, "the lock outlived the process holding it"
        assert time.monotonic() - started < 1.0

    def test_a_control_gives_up_rather_than_waiting_for_a_wedged_fetch(self, tmp_path, monkeypatch):
        """Yields False instead of raising, because this is an ordinary outcome with a
        defined response: use the stored value, and fall to the safe state only if it is too
        old. One wedged fetch must not stall every control sharing the source."""
        monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
        holder = _start_holder(tmp_path, hold_seconds=30)
        try:
            started = time.monotonic()
            with fetch_lock("probe", budget=0.5) as held:
                assert held is False
            assert time.monotonic() - started < 5
        finally:
            holder.send_signal(signal.SIGKILL)
            holder.wait(timeout=10)

    def test_each_wait_is_randomised_and_bounded(self, tmp_path, monkeypatch):
        """Randomised because the release is itself a synchronising event: a fixed interval
        would wake every waiter together and re-collide, which is what the lock is for.

        Driven through injected sleep and clock rather than by timing a real run, so the
        assertion is about the interval chosen rather than about how busy the machine is.
        """
        monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
        holder = _start_holder(tmp_path, hold_seconds=30)
        slept = []
        elapsed = [0.0]

        def fake_sleep(seconds):
            slept.append(seconds)
            elapsed[0] += seconds

        try:
            with fetch_lock(
                "probe",
                budget=3,
                rng=random.Random(1),
                sleep=fake_sleep,
                monotonic=lambda: elapsed[0],
            ) as held:
                assert held is False
            assert len(slept) > 1, "gave up without retrying"
            assert len(set(slept)) > 1, "every wait was identical, so waiters would re-collide"
            assert all(wait <= MAX_LOCK_BACKOFF for wait in slept), slept
            # Every wait but the last sits in the band. The last is truncated to whatever
            # is left of the budget, so it can be shorter or zero: the budget is the
            # promise to the caller, and overshooting it to honour a minimum would break
            # the one that matters.
            assert all(wait >= MIN_LOCK_BACKOFF for wait in slept[:-1]), slept
            assert sum(slept) <= 3, "waited past the budget"
        finally:
            holder.send_signal(signal.SIGKILL)
            holder.wait(timeout=10)

    def test_locks_live_in_their_own_directory(self, tmp_path, monkeypatch):
        """Not loose in the state directory. Off systemd that directory is wherever
        settings.yaml is, which for a source checkout is the repository root, and a loose
        fetch-<source>.lock there is easy to commit by accident - one already was."""
        monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
        path = fetch_lock_path("hue")
        assert os.path.dirname(path) == str(tmp_path / "locks")
        with fetch_lock("hue", budget=1) as held:
            assert held
        assert os.path.isdir(tmp_path / "locks"), "the lock directory was not created"
        assert not list(tmp_path.glob("fetch-*.lock")), "a lock file landed in the state directory itself"

    def test_the_lock_name_is_case_insensitive(self, tmp_path, monkeypatch):
        """Otherwise two controls naming the same source in different cases take two
        different locks and both go live, which is the one thing the lock exists to stop -
        and it fails silently, since each control sees a lock it always wins."""
        monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
        assert fetch_lock_path("Hue") == fetch_lock_path("hue")

    def test_an_unopenable_lock_file_is_a_config_error_not_an_oserror(self, tmp_path, monkeypatch):
        """A bare OSError crossing this boundary breaks the documented contract and reaches
        an operator as a traceback instead of a message naming the file."""
        monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
        # A directory where the lock file should be: open() cannot open it for writing.
        os.makedirs(os.path.join(tmp_path, "locks", "fetch-hue.lock"))
        with pytest.raises(ConfigError, match="cannot open the fetch lock"):
            with fetch_lock("hue", budget=1):
                pass

    def test_a_lock_that_cannot_work_is_reported_not_retried(self, tmp_path, monkeypatch):
        """Contention is EAGAIN, which arrives as BlockingIOError. Any other OSError means
        the lock is not working - a filesystem without flock support, a bad descriptor.

        Retried as if it were contention, that failure is indistinguishable from a busy
        lock: every control waits its whole budget, reports "busy", and carries on with
        serialisation silently off. A lock that appears to work and guarantees nothing is
        worse than one that says it cannot.
        """
        monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
        monkeypatch.setattr("fcntl.flock", MagicMock(side_effect=OSError(45, "Operation not supported")))
        with pytest.raises(ConfigError, match="cannot lock .* to serialise live fetches of 'hue'"):
            with fetch_lock("hue", budget=5):
                pass

    def test_each_source_has_its_own_lock(self, tmp_path, monkeypatch):
        """Two controls reading different sources have no reason to wait for each other."""
        monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
        assert fetch_lock_path("hue") != fetch_lock_path("openmeteo")
        holder = _start_holder(tmp_path, hold_seconds=30)
        try:
            with fetch_lock("openmeteo", budget=0.5) as held:
                assert held, "a lock on one source blocked a fetch of another"
        finally:
            holder.send_signal(signal.SIGKILL)
            holder.wait(timeout=10)


def _handler(live=True, timeout=5, data=None, fails=None, timestamp=None):
    """A stand-in source handler for the live-fetch path."""
    handler = MagicMock()
    handler.MCP_LIVE_STATE = live
    handler.source_settings = {"timeout": timeout}
    handler.timestamp = timestamp
    if fails is not None:
        handler.get_data.side_effect = fails
    else:
        handler.get_data.return_value = data or {}
    return handler


SETTINGS = {"hue": {"interval": 300, "db": "x"}, "influx": {"url": "http://x", "user": "u", "password": "p"}}
SPEC = {"source": "hue", "field": "temperature", "max_age": 900}


class TestStoredReading:
    def test_an_unusable_field_name_is_a_config_error_not_a_tool_error(self, monkeypatch):
        """The identifier check refuses a control character and says ToolParamError, because
        its other caller is an MCP tool taking a model's argument. Here the name came from a
        control document, so it is a configuration fault: stop, and no retry helps. A caller
        catching ConfigError to mean "stop" would otherwise miss it entirely.
        """

        def fake_get_class(source, settings_file=None, instance=None):
            handler = MagicMock()
            handler.MCP_MEASUREMENT = None
            handler.source = source
            handler.mcp_tag_filters.return_value = {}
            handler.source_settings = {"db": "x"}
            return handler

        monkeypatch.setattr("toinflux.inputs.get_class", fake_get_class)
        with pytest.raises(ConfigError, match="unusable field"):
            stored_reading(None, SETTINGS, "hue", "temp\nDROP MEASUREMENT hue")

    def test_a_field_named_time_is_refused_rather_than_guessed(self):
        """A result carries its timestamp in a column called "time", so a field of that name
        gives two columns with one name and nothing to tell them apart. Reading the wrong
        one yields an age rather than an error, and a wrong age is the single thing a
        control must not be handed quietly.

        The project already assumes this cannot happen - annotate_rows lands on the field
        column by its fallback rather than by choosing it - so this makes the assumption
        loud instead of leaving it implicit.
        """
        with pytest.raises(ConfigError, match="cannot read a field named 'time'"):
            stored_reading(None, SETTINGS, "hue", "time")

    def test_the_wrapper_escapes_even_if_the_inner_message_stops_doing_so(self, monkeypatch):
        """What the outer !r is actually for, which is not what it first looks like.

        Today the field name is escaped before it reaches this wrapper: _validate_identifier
        quotes it, so a newline never arrives raw. Writing this test the obvious way - pass
        a field containing a newline, assert the message has none - therefore passes with or
        without the outer quoting and proves nothing. It did exactly that, which is why it
        is written this way instead.

        The outer quoting guards against the inner message format changing, so the test has
        to supply an inner exception whose text is genuinely raw.
        """

        def fake_get_class(source, settings_file=None, instance=None):
            handler = MagicMock()
            handler.MCP_MEASUREMENT = None
            handler.source = source
            handler.mcp_tag_filters.return_value = {}
            handler.source_settings = {"db": "x"}
            return handler

        monkeypatch.setattr("toinflux.inputs.get_class", fake_get_class)
        monkeypatch.setattr(
            "toinflux.inputs.build_latest_query",
            MagicMock(side_effect=ToolParamError("invalid field name: temp\nDROP MEASUREMENT hue")),
        )
        with pytest.raises(ConfigError) as raised:
            stored_reading(None, SETTINGS, "hue", "temperature")
        assert "\n" not in str(raised.value), str(raised.value)

    def test_the_settings_file_reaches_the_handler(self, monkeypatch):
        """Otherwise the handler loads the default settings.yaml while the caller passes a
        different document, and the two disagree about which database to read - invisible
        until someone runs with -s, and then wrong rather than broken."""
        seen = {}

        def fake_get_class(source, settings_file=None, instance=None):
            seen.update(source=source, settings_file=settings_file, instance=instance)
            handler = MagicMock()
            handler.MCP_MEASUREMENT = None
            handler.source = source
            handler.mcp_tag_filters.return_value = {}
            handler.source_settings = {"db": "x"}
            return handler

        monkeypatch.setattr("toinflux.inputs.get_class", fake_get_class)
        monkeypatch.setattr("toinflux.inputs.resolve_db", lambda *a: "db")
        monkeypatch.setattr("toinflux.inputs.run_query", lambda *a: [])
        monkeypatch.setattr("toinflux.inputs.single_series", lambda series: ([], []))
        stored_reading(None, SETTINGS, "hue", "temperature", "bridge1", "/etc/other.yaml")
        assert seen == {"source": "hue", "settings_file": "/etc/other.yaml", "instance": "bridge1"}


class TestReadInput:
    """InfluxDB first, and the device only when the stored value is too old to use."""

    def test_a_fresh_stored_value_never_touches_the_device(self, monkeypatch):
        stored = InputReading(value=19.5, timestamp=1000.0, age=100.0, live=False)
        handler = _handler()
        monkeypatch.setattr("toinflux.inputs.handler_reading", lambda *a, **k: stored)
        monkeypatch.setattr("toinflux.inputs.get_class", lambda *a, **k: handler)
        assert read_input(None, SETTINGS, SPEC) is stored
        handler.get_data.assert_not_called()

    def test_a_stale_stored_value_is_refreshed_and_written_back(self, monkeypatch, tmp_path):
        """The write-back is what makes the database the shared state: without it the next
        control to ask would go to the device too, and the floor would bind nothing."""
        monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
        stored = InputReading(value=19.5, timestamp=0.0, age=5000.0, live=False)
        handler = _handler(data={"temperature": 21.0, "humidity": 55.0})
        monkeypatch.setattr("toinflux.inputs.handler_reading", lambda *a, **k: stored)
        monkeypatch.setattr("toinflux.inputs.get_class", lambda *a, **k: handler)
        reading = read_input(None, SETTINGS, SPEC, now=9999.0)
        # Age 0 because this handler set no timestamp of its own; see the two tests below.
        assert (reading.value, reading.live, reading.age) == (21.0, True, 0.0)
        handler.send_data.assert_called_once_with({"temperature": 21.0, "humidity": 55.0})

    def test_the_minimum_interval_beats_a_shorter_max_age(self, monkeypatch):
        """A control wanting fresher data than the source's floor allows gets the stored
        value anyway. Deciding otherwise would let one control's document set the rate at
        which every other control's source is polled."""
        stored = InputReading(value=19.5, timestamp=0.0, age=120.0, live=False)
        handler = _handler()
        monkeypatch.setattr("toinflux.inputs.handler_reading", lambda *a, **k: stored)
        monkeypatch.setattr("toinflux.inputs.get_class", lambda *a, **k: handler)
        # 120s old and the control wants 60s, but carbonintensity's floor is 900: the grid
        # publishes half-hourly, so asking sooner spends someone else's capacity for nothing.
        settings = {**SETTINGS, "carbonintensity": {"interval": 1800, "db": "x"}}
        spec = {"source": "carbonintensity", "field": "intensity_actual", "max_age": 60}
        assert read_input(None, settings, spec) is stored
        handler.get_data.assert_not_called()

    def test_the_recheck_after_the_lock_avoids_touching_the_device(self, monkeypatch, tmp_path):
        """Whoever held the lock has almost certainly just written the value. Re-reading
        before fetching is what turns a queue of waiters into one device read."""
        monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
        stale = InputReading(value=19.5, timestamp=0.0, age=5000.0, live=False)
        fresh = InputReading(value=21.0, timestamp=9990.0, age=9.0, live=False)
        handler = _handler()
        readings = iter([stale, fresh])
        monkeypatch.setattr("toinflux.inputs.handler_reading", lambda *a, **k: next(readings))
        monkeypatch.setattr("toinflux.inputs.get_class", lambda *a, **k: handler)
        assert read_input(None, SETTINGS, SPEC) is fresh
        handler.get_data.assert_not_called()

    def test_a_busy_lock_falls_back_to_the_stored_value(self, monkeypatch, tmp_path, caplog):
        """One wedged fetch must not stall every control sharing the source. The value
        comes back stale and the control decides; the log says why it is stale."""
        monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
        stored = InputReading(value=19.5, timestamp=0.0, age=5000.0, live=False)
        handler = _handler()
        monkeypatch.setattr("toinflux.inputs.handler_reading", lambda *a, **k: stored)
        monkeypatch.setattr("toinflux.inputs.get_class", lambda *a, **k: handler)
        monkeypatch.setattr("toinflux.inputs.fetch_lock", lambda *a, **k: _never_held())
        with caplog.at_level("WARNING"):
            assert read_input(None, SETTINGS, SPEC) is stored
        handler.get_data.assert_not_called()
        assert "fetch lock" in caplog.text

    def test_a_source_with_no_live_read_is_never_fetched(self, monkeypatch):
        """Octopus is a day behind and Speedtest is expensive: going live would cost
        something and return nothing fresher."""
        stored = InputReading(value=19.5, timestamp=0.0, age=5000.0, live=False)
        handler = _handler(live=False)
        monkeypatch.setattr("toinflux.inputs.handler_reading", lambda *a, **k: stored)
        monkeypatch.setattr("toinflux.inputs.get_class", lambda *a, **k: handler)
        assert read_input(None, SETTINGS, SPEC) is stored
        handler.get_data.assert_not_called()

    def test_a_failed_live_read_degrades_to_the_stored_value(self, monkeypatch, tmp_path, caplog):
        """An unreachable device and a stale-but-readable one get the same response from
        the control, so they must not differ in whether this raises."""
        monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
        stored = InputReading(value=19.5, timestamp=0.0, age=5000.0, live=False)
        handler = _handler(fails=SourceConnectionError("bridge unreachable"))
        monkeypatch.setattr("toinflux.inputs.handler_reading", lambda *a, **k: stored)
        monkeypatch.setattr("toinflux.inputs.get_class", lambda *a, **k: handler)
        with caplog.at_level("WARNING"):
            assert read_input(None, SETTINGS, SPEC) is stored
        assert "Live read" in caplog.text

    def test_no_stored_point_and_a_failed_fetch_raises(self, monkeypatch, tmp_path):
        """Nothing to return and nothing to fall back to. Returning None here would make
        "no reading" indistinguishable from a reading of zero at the call site."""
        monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
        handler = _handler(fails=SourceConnectionError("bridge unreachable"))
        monkeypatch.setattr("toinflux.inputs.handler_reading", lambda *a, **k: None)
        monkeypatch.setattr("toinflux.inputs.get_class", lambda *a, **k: handler)
        with pytest.raises(SourceConnectionError, match="no value available for 'temperature'"):
            read_input(None, SETTINGS, SPEC)

    def test_a_live_reading_carries_the_point_s_own_time_not_the_time_we_asked(self, monkeypatch, tmp_path):
        """get_data() sets handler.timestamp where the reading is older than the request -
        Nuki does, Octopus does - and send_data writes the point at that same value.

        Reporting age 0 would disagree with what InfluxDB then holds, and would tell a
        control that an hour-old reading was brand new. Freshly fetched is not the same
        thing as fresh.
        """
        monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
        handler = _handler(data={"temperature": 21.0}, timestamp=6000)
        monkeypatch.setattr("toinflux.inputs.handler_reading", lambda *a, **k: None)
        monkeypatch.setattr("toinflux.inputs.get_class", lambda *a, **k: handler)
        reading = read_input(None, SETTINGS, SPEC, now=9600.0)
        assert (reading.timestamp, reading.age, reading.live) == (6000.0, 3600.0, True)

    def test_a_live_reading_with_no_handler_timestamp_is_now(self, monkeypatch, tmp_path):
        """The ordinary case: the handler read the device and the point is the moment."""
        monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
        handler = _handler(data={"temperature": 21.0}, timestamp=None)
        monkeypatch.setattr("toinflux.inputs.handler_reading", lambda *a, **k: None)
        monkeypatch.setattr("toinflux.inputs.get_class", lambda *a, **k: handler)
        reading = read_input(None, SETTINGS, SPEC, now=9600.0)
        assert (reading.timestamp, reading.age) == (9600.0, 0.0)

    def test_a_non_finite_timeout_cannot_hang_the_wait_loop(self, monkeypatch, tmp_path):
        """The worst of the non-finite cases, and the reason the timeout goes through the
        same validation as the floor. fetch_lock's deadline comparison is False forever
        against a nan, so a contended control would wait for ever - a hang, in the
        subsystem whose entire design is about processes not hanging.
        """
        monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
        stored = InputReading(value=19.5, timestamp=0.0, age=5000.0, live=False)
        handler = _handler(timeout=float("nan"))
        monkeypatch.setattr("toinflux.inputs.handler_reading", lambda *a, **k: stored)
        monkeypatch.setattr("toinflux.inputs.get_class", lambda *a, **k: handler)
        with pytest.raises(ConfigError, match="'hue.timeout' must be a finite number"):
            read_input(None, SETTINGS, SPEC)

    def test_a_failed_write_back_still_returns_the_reading(self, monkeypatch, tmp_path, caplog):
        """The value in hand is good; only the coordination failed.

        Raising would throw away a fresh reading because a best-effort write missed, and
        the caller's response to a failed read is the safe state - so an InfluxDB hiccup
        would switch the heating off while the temperature it was holding was perfectly
        well known. What is genuinely lost is that other controls will not see this value,
        so the floor stops binding until a write succeeds, and that goes in the log.
        """
        monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
        handler = _handler(data={"temperature": 21.0})
        handler.send_data.side_effect = InfluxWriteError("influx returned 503")
        monkeypatch.setattr("toinflux.inputs.handler_reading", lambda *a, **k: None)
        monkeypatch.setattr("toinflux.inputs.get_class", lambda *a, **k: handler)
        with caplog.at_level("WARNING"):
            reading = read_input(None, SETTINGS, SPEC, now=9600.0)
        assert (reading.value, reading.live) == (21.0, True)
        assert "write back" in caplog.text

    @pytest.mark.parametrize(
        "age,refreshes",
        [
            pytest.param(2600.0, False, id="inside-three-intervals"),
            pytest.param(2800.0, True, id="past-three-intervals"),
        ],
    )
    def test_an_absent_max_age_tolerates_three_intervals_not_one(self, monkeypatch, age, refreshes):
        """max_age stays optional - it arrived late enough that requiring it would break
        documents people already have - so an absent one needs an internal default.

        Three times the source's minimum interval, matching STALL_INTERVAL_MULTIPLIER. It
        changes nothing about the fetch trigger, since the minimum dominates; it matters
        because max_age is also the staleness past which a control stops acting, and one
        interval would trip on a single missed collection.
        """
        handler = _handler()
        monkeypatch.setattr("toinflux.inputs.get_class", lambda *a, **k: handler)
        settings = {**SETTINGS, "carbonintensity": {"interval": 1800, "db": "x"}}
        spec = {"source": "carbonintensity", "field": "intensity_actual"}
        # carbonintensity's minimum interval is 900, so the default tolerance is 2700.
        reading = InputReading(value=1.0, timestamp=0.0, age=age, live=False)
        monkeypatch.setattr("toinflux.inputs.handler_reading", lambda *a, **k: reading)
        read_input(None, settings, spec)
        assert handler.get_data.called is refreshes

    def test_an_input_without_a_max_age_falls_back_to_the_source_floor(self, monkeypatch):
        """max_age is optional in a control document, so requiring it here would make
        --check-config pass a control that then failed at runtime. An input declaring no
        tolerance of its own gets the source's floor, which is as fresh as anything can ask
        for."""
        stored = InputReading(value=19.5, timestamp=0.0, age=120.0, live=False)
        handler = _handler()
        monkeypatch.setattr("toinflux.inputs.handler_reading", lambda *a, **k: stored)
        monkeypatch.setattr("toinflux.inputs.get_class", lambda *a, **k: handler)
        # 120s old against carbonintensity's 900s floor: inside it, so no live read.
        settings = {**SETTINGS, "carbonintensity": {"interval": 1800, "db": "x"}}
        spec = {"source": "carbonintensity", "field": "intensity_actual"}
        assert read_input(None, settings, spec) is stored
        handler.get_data.assert_not_called()

    @pytest.mark.parametrize("bad", [float("inf"), float("nan"), True, "900"])
    def test_a_non_finite_max_age_cannot_make_an_input_permanently_fresh(self, monkeypatch, bad):
        """The third duration in the same expression, and the one left bare. An .inf max_age
        makes the trigger infinite, so the input reads as perpetually fresh and is never
        refreshed however old it actually gets - the failure mode being that the control
        goes on acting on days-old data without ever falling to its safe state."""
        handler = _handler()
        monkeypatch.setattr("toinflux.inputs.handler_reading", lambda *a, **k: None)
        monkeypatch.setattr("toinflux.inputs.get_class", lambda *a, **k: handler)
        with pytest.raises(ConfigError, match="max_age for input temperature"):
            read_input(None, SETTINGS, {**SPEC, "max_age": bad})

    @pytest.mark.parametrize("missing", ["source", "field"])
    def test_an_incomplete_declaration_says_which_key_is_missing(self, missing):
        """A KeyError traceback from somewhere further in is not a report about a control
        document. The store validates one at --check-config, so this is the backstop."""
        spec = {key: value for key, value in SPEC.items() if key != missing}
        with pytest.raises(ConfigError, match=f"missing '{missing}'"):
            read_input(None, SETTINGS, spec)

    def test_a_misconfigured_source_is_not_degraded_into_a_transient_failure(self, monkeypatch, tmp_path):
        """ConfigError means stop; SourceConnectionError means fail safe and carry on.

        ConfigError is a ToInfluxError, so catching that broadly swallowed it - and Hue's
        bridge() and MyEnergi's device() both raise it. A misconfigured bridge would have
        read as a device that kept being unreachable, for as long as nobody looked.
        """
        monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
        stored = InputReading(value=19.5, timestamp=0.0, age=5000.0, live=False)
        handler = _handler(fails=ConfigError("no such bridge 'bridge9'"))
        monkeypatch.setattr("toinflux.inputs.handler_reading", lambda *a, **k: stored)
        monkeypatch.setattr("toinflux.inputs.get_class", lambda *a, **k: handler)
        with pytest.raises(ConfigError, match="bridge9"):
            read_input(None, SETTINGS, SPEC)

    @pytest.mark.parametrize(
        "age,live,fails",
        [
            pytest.param(1.0, True, None, id="fresh-enough-to-return-before-the-lock"),
            pytest.param(5000.0, False, None, id="source-has-no-live-read"),
            pytest.param(5000.0, True, None, id="fetches-and-writes-back"),
            pytest.param(5000.0, True, SourceConnectionError("unreachable"), id="fetch-failed"),
        ],
    )
    def test_the_handler_session_is_closed_on_every_path(self, monkeypatch, tmp_path, age, live, fails):
        """DataHandler.__init__ opens a requests.Session whether or not anything uses it,
        and nothing in this project closes one. A collector builds a handler per process so
        that never mattered; a control loop reads its inputs every cycle.

        Every path, because the leak is worst on the ones that build a handler and then
        never fetch - which is also where it is easiest to forget.
        """
        monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
        reading = InputReading(value=19.5, timestamp=0.0, age=age, live=False)
        handler = _handler(live=live, data={"temperature": 21.0}, fails=fails)
        monkeypatch.setattr("toinflux.inputs.handler_reading", lambda *a, **k: reading)
        monkeypatch.setattr("toinflux.inputs.get_class", lambda *a, **k: handler)
        read_input(None, SETTINGS, SPEC)
        handler.session.close.assert_called_once()

    def test_a_live_read_missing_the_field_raises_rather_than_inventing_one(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
        handler = _handler(data={"humidity": 55.0})
        monkeypatch.setattr("toinflux.inputs.handler_reading", lambda *a, **k: None)
        monkeypatch.setattr("toinflux.inputs.get_class", lambda *a, **k: handler)
        with pytest.raises(SourceConnectionError, match="no such field"):
            read_input(None, SETTINGS, SPEC)
