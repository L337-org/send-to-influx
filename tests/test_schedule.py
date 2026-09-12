"""Tests for a control's active period, including both daylight-saving edges.

The transition dates are found from the zone rather than written down, so these keep
testing the real thing when 2026 stops being next year.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT"

import datetime
import os
import subprocess
import sys
import textwrap
from zoneinfo import ZoneInfo

import pytest

from toinflux.exceptions import ConfigError
from toinflux.schedule import control_zone, is_inside, parse_active_period

LONDON = ZoneInfo("Europe/London")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _period(start, end, **extra):
    return parse_active_period({"timezone": "Europe/London", "active_period": {"from": start, "to": end, **extra}})


def _transitions(year, zone):
    """Return the days in a year where the zone's offset changes.

    Derived rather than hardcoded: a test naming 2026-03-29 stops testing a spring forward
    the moment somebody reads it in a different year.
    """
    days, previous = [], None
    for offset in range(366):
        noon = datetime.datetime(year, 1, 1, 12, 0, tzinfo=zone) + datetime.timedelta(days=offset)
        current = noon.utcoffset()
        if previous is not None and current != previous:
            days.append((noon.date(), previous, current))
        previous = current
    return days


def _minutes_inside(period, day, zone, hours=6):
    """Count the real minutes spent inside a window during a span of one UTC day.

    Walked in UTC, so the count is of actual elapsed time rather than of local clock
    readings - which is the only way to see an hour that happens twice.
    """
    start = datetime.datetime(day.year, day.month, day.day, tzinfo=datetime.timezone.utc)
    return sum(1 for m in range(hours * 60) if is_inside(period, start + datetime.timedelta(minutes=m)))


def _minutes_overnight(period, evening, zone):
    """Count the real minutes an overnight window runs, starting from a given evening.

    A fixed span of UTC will not do for this: the window's local boundaries move relative to
    UTC across a transition, and counting a UTC day gives 350 minutes on every day of the
    year - which is true and answers a different question. The night is what an operator
    cares about, so the span starts at 20:00 local and runs long enough to contain it.
    """
    start = datetime.datetime(evening.year, evening.month, evening.day, 20, 0, tzinfo=zone)
    start = start.astimezone(datetime.timezone.utc)
    return sum(1 for m in range(16 * 60) if is_inside(period, start + datetime.timedelta(minutes=m)))


class TestAnOrdinaryDay:
    @pytest.mark.parametrize(
        "when,expected",
        [
            pytest.param((23, 30), False, id="before-it-opens"),
            pytest.param((23, 35), True, id="the-moment-it-opens"),
            pytest.param((2, 0), True, id="the-middle-of-the-night"),
            pytest.param((5, 24), True, id="the-last-minute"),
            pytest.param((5, 25), False, id="the-moment-it-closes"),
            pytest.param((12, 0), False, id="the-middle-of-the-day"),
        ],
    )
    def test_an_overnight_window_wraps_midnight(self, when, expected):
        period = _period("23:35", "05:25")
        moment = datetime.datetime(2026, 1, 15, when[0], when[1], tzinfo=LONDON)
        assert is_inside(period, moment) is expected

    def test_the_end_is_exclusive(self):
        """So two windows meeting at a time cannot both claim it, and a window ending at
        05:25 is not still running at 05:25."""
        period = _period("09:00", "17:00")
        assert is_inside(period, datetime.datetime(2026, 1, 15, 17, 0, tzinfo=LONDON)) is False
        assert is_inside(period, datetime.datetime(2026, 1, 15, 16, 59, tzinfo=LONDON)) is True

    def test_a_daytime_window_does_not_wrap(self):
        period = _period("09:00", "17:00")
        assert is_inside(period, datetime.datetime(2026, 1, 15, 3, 0, tzinfo=LONDON)) is False

    def test_no_period_means_always(self):
        """A control that names no window runs whenever it is otherwise enabled."""
        assert is_inside(None, datetime.datetime(2026, 1, 15, 3, 0, tzinfo=LONDON)) is True


class TestDaylightSaving:
    """Both behaviours are correct, and neither is special-cased anywhere in the code.

    A wall-clock window should skip a local hour the clocks jumped over and run twice
    through one they repeated, because that is what the wall clock did. Comparing the
    current local time-of-day against the boundaries produces both by construction.
    """

    def test_the_transitions_are_where_the_zone_says(self):
        """Guards the two tests below: if the zone database moves these, the scenarios stop
        being a spring forward and an autumn back and would quietly test nothing."""
        transitions = _transitions(2026, LONDON)
        assert len(transitions) == 2, transitions
        spring, autumn = transitions
        assert spring[2] > spring[1], "the first transition should put the clocks forward"
        assert autumn[2] < autumn[1], "the second should put them back"

    def test_a_window_inside_the_lost_hour_never_opens(self):
        """The clocks go forward over 01:15-01:45, so that window does not happen that day.

        Not an error and not something to compensate for: nobody was heating a conservatory
        during an hour that did not occur.
        """
        spring = _transitions(2026, LONDON)[0][0]
        assert _minutes_inside(_period("01:15", "01:45"), spring, LONDON) == 0

    def test_a_window_inside_the_repeated_hour_runs_twice(self):
        """The clocks go back over 01:15-01:45, so that window happens twice - sixty real
        minutes for a thirty-minute window.

        Also correct. The wall clock read 01:15 twice, and a schedule written in wall-clock
        time means what the wall clock says.
        """
        autumn = _transitions(2026, LONDON)[1][0]
        assert _minutes_inside(_period("01:15", "01:45"), autumn, LONDON) == 60

    def test_an_overnight_window_is_an_hour_shorter_and_longer_across_them(self):
        """The consequence for the real conservatory window, as real minutes of heating.

        Measured over the night rather than a UTC day. A UTC day gives 350 minutes on every
        date of the year, including both transitions - true, and the answer to a different
        question, which is how the first version of this test looked like it was measuring
        something it was not.
        """
        spring, autumn = (day for day, _, _ in _transitions(2026, LONDON))
        window = _period("23:35", "05:25")
        ordinary = _minutes_overnight(window, datetime.date(2026, 1, 15), LONDON)
        assert ordinary == 350
        # The night before each transition is the one containing it.
        assert _minutes_overnight(window, spring - datetime.timedelta(days=1), LONDON) == ordinary - 60
        assert _minutes_overnight(window, autumn - datetime.timedelta(days=1), LONDON) == ordinary + 60


class TestBuildingOne:
    def test_a_window_that_starts_when_it_ends_is_refused(self):
        """Always open or never open, and the two readings differ by a day of heating."""
        with pytest.raises(ConfigError, match="starts when it ends"):
            _period("09:00", "09:00")

    @pytest.mark.parametrize("bad", ["9:00", "24:00", "09:60", "0900", "", None, 900, "23:35\n"])
    def test_a_time_that_is_not_a_24_hour_clock_time_is_refused(self, bad):
        with pytest.raises(ConfigError, match="HH:MM"):
            _period(bad, "05:25")

    def test_an_unknown_end_state_is_refused(self):
        with pytest.raises(ConfigError, match="end_state"):
            _period("23:35", "05:25", end_state="explode")

    def test_the_end_state_defaults_to_unenergised(self):
        assert _period("23:35", "05:25").end_state == "unenergised"

    def test_no_active_period_is_none_rather_than_an_empty_window(self):
        assert parse_active_period({}) is None

    def test_an_unknown_timezone_is_refused(self):
        with pytest.raises(ConfigError, match="not a time zone this machine knows"):
            control_zone({"timezone": "Mars/Olympus_Mons"})

    def test_no_timezone_means_local_time_rather_than_a_captured_zone(self):
        """None, and deliberately: the machine's local time at the moment of comparison,
        which is what somebody writing "23:35" on a machine in their own house means.

        A zone captured here would be a fixed offset - `datetime.now().astimezone().tzinfo`
        reports the same offset in January as in July - and a control process runs for
        months. See the test below for what that costs."""
        assert control_zone({}) is None


class TestTheMomentMustBeAware:
    def test_a_naive_moment_is_refused(self):
        """ "What is the local time" has no answer for a moment that names no instant, and
        guessing would make the window silently wrong by the machine's offset."""
        with pytest.raises(ConfigError, match="aware moment"):
            is_inside(_period("23:35", "05:25"), datetime.datetime(2026, 1, 15, 2, 0))

    def test_a_moment_in_another_zone_is_converted_not_compared_raw(self):
        """The window is the control's wall clock, not the caller's. A UTC moment during a
        British summer is an hour behind the local reading, and comparing it raw would run
        the window an hour early for half the year."""
        period = _period("23:35", "05:25")
        # 22:40 UTC in July is 23:40 in London: inside the window, though 22:40 is not.
        july = datetime.datetime(2026, 7, 15, 22, 40, tzinfo=datetime.timezone.utc)
        assert is_inside(period, july) is True


class TestTheMachineSOwnZoneFollowsDaylightSaving:
    """The property a fixed offset cannot have, measured in a child process so the zone is
    this test's to choose on any machine - Python has no tzset on every platform, and a
    test that only checks this where the developer happens to live checks nothing in CI."""

    @staticmethod
    def _readings():
        """Return what a zone-less period makes of the same wall clock in winter and summer.

        The window is ten minutes wide on purpose. An overnight window that wraps midnight
        answers "inside" for both the right reading and the hour-out one, so the first
        version of this test passed against the very bug it was written for.

        Returns:
            list: two booleans, for a January and a July moment that both read 23:40 locally
        """
        script = textwrap.dedent(f"""
            import datetime, sys
            sys.path.insert(0, {ROOT!r})
            from toinflux.schedule import parse_active_period, is_inside
            period = parse_active_period({{"active_period": {{"from": "23:35", "to": "23:45"}}}})
            january = datetime.datetime(2026, 1, 15, 23, 40, tzinfo=datetime.timezone.utc)
            july = datetime.datetime(2026, 7, 15, 22, 40, tzinfo=datetime.timezone.utc)
            print(is_inside(period, january), is_inside(period, july))
            """)
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "TZ": "Europe/London", "PYTHONPATH": ""},
            check=True,
        )
        return result.stdout.split()

    def test_both_moments_read_as_the_same_local_time(self):
        """23:40 GMT in January and 23:40 BST in July are the same wall clock and the same
        answer. No single fixed offset can produce both, so a zone captured at startup gets
        one of them wrong for months - the hour of drift this module claims not to have.
        """
        assert self._readings() == ["True", "True"]
