"""Tests for reading a control's inputs.

The live-fetch floor is the part with a decision behind it rather than a mechanism: it is
per source rather than per control, and it defaults to the source's own collection
interval. Both of those are properties a later change could quietly undo, so they are
asserted here rather than left to the design note.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT"

import pytest

from toinflux.exceptions import ConfigError
from toinflux.inputs import resolve_poll_floor


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
