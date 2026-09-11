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

import pytest

from toinflux.exceptions import ConfigError
from toinflux.inputs import MAX_LOCK_BACKOFF, MIN_LOCK_BACKOFF, fetch_lock, fetch_lock_path, resolve_poll_floor


def _holder_script(state_dir, hold_seconds):
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
    """Start a holder and return it once it reports the lock is taken."""
    child = subprocess.Popen(
        [sys.executable, "-c", _holder_script(state_dir, hold_seconds)],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "STATE_DIRECTORY": str(state_dir), "PYTHONPATH": ""},
    )
    assert child.stdout.readline().strip() == "held"
    return child


class TestResolvePollFloor:
    def test_an_explicit_floor_wins(self):
        assert resolve_poll_floor("hue", {"hue": {"interval": 300, "poll_floor": 60}}) == 60.0

    def test_the_collection_interval_is_the_default(self):
        """A collector already asks the device this often, so the cadence is known to be
        acceptable. Anything lower has to be chosen deliberately."""
        assert resolve_poll_floor("hue", {"hue": {"interval": 300}}) == 300.0

    def test_zero_is_a_floor_rather_than_a_missing_value(self):
        """Zero means "ask whenever a control wants it", which is right for a source that is
        cheap to read. A falsy check would silently substitute the interval instead."""
        assert resolve_poll_floor("hue", {"hue": {"interval": 300, "poll_floor": 0}}) == 0.0

    def test_the_floor_is_read_per_source(self):
        """The floor binds every control sharing a source, so it cannot come from one
        control's document: two controls each honouring 60 s still reach the device at 30 s
        combined. This test is the structural half of that - one settings document, two
        sources, two different answers."""
        settings = {"hue": {"interval": 300}, "openmeteo": {"interval": 1800, "poll_floor": 600}}
        assert resolve_poll_floor("hue", settings) == 300.0
        assert resolve_poll_floor("openmeteo", settings) == 600.0

    @pytest.mark.parametrize("settings", [{}, {"hue": None}, {"hue": "300"}, {"hue": []}])
    def test_a_source_without_a_usable_section_is_a_config_error(self, settings):
        """Including a section present but empty, which is what commenting out every field
        leaves behind and parses as null."""
        with pytest.raises(ConfigError, match="cannot resolve the live-fetch floor"):
            resolve_poll_floor("hue", settings)

    def test_a_section_with_neither_key_names_the_setting_to_add(self):
        """The message has to say what to write, because the reader is an operator looking
        at a journal line rather than at this code."""
        with pytest.raises(ConfigError, match="hue.interval is required"):
            resolve_poll_floor("hue", {"hue": {"db": "x"}})

    @pytest.mark.parametrize("bad", [True, False, "60", None, [60]])
    def test_a_non_numeric_floor_is_refused(self, bad):
        """A bool included: `bool` subclasses `int`, so `poll_floor: true` would otherwise
        behave as a one-second floor, which is not what typing `true` meant."""
        with pytest.raises(ConfigError, match="must be a number of seconds|is required"):
            resolve_poll_floor("hue", {"hue": {"interval": 300, "poll_floor": bad}})

    def test_a_negative_floor_is_refused(self):
        with pytest.raises(ConfigError, match="must not be negative"):
            resolve_poll_floor("hue", {"hue": {"interval": 300, "poll_floor": -5}})

    def test_a_bad_interval_is_refused_when_it_is_the_fallback(self):
        """The default path validates too. An unusable interval reaching a control as a
        floor of None would fail much later, somewhere less obvious."""
        with pytest.raises(ConfigError, match="hue.interval must be a number of seconds"):
            resolve_poll_floor("hue", {"hue": {"interval": "300"}})


class TestTheFetchLockSerialisesLiveFetches:
    """The lock exists so two controls crossing max_age together do not both go live.

    Every test here runs a real second process. A thread in this interpreter shares the
    file descriptor, and flock is per descriptor, so a threaded version of these tests
    would pass against code that locks nothing.
    """

    def test_an_uncontended_control_is_not_delayed(self, tmp_path, monkeypatch):
        """The holder proceeds immediately. A design that made everyone wait a little would
        put a delay on the common path to fix the rare one."""
        monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
        started = time.monotonic()
        with fetch_lock("probe", budget=5) as held:
            assert held
            assert time.monotonic() - started < 0.1

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
