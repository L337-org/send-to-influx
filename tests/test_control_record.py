"""Tests for the controls' PID history: where it goes, what a point carries, and that a
failure to write one stays out of the cycle.

What the control loop writes, and when, is tested in ``tests/test_control_process.py``
against the stub InfluxDB. What is here is the record's own contract.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import logging

import pytest
import requests

from toinflux.control_record import (
    ACTIVE,
    FAIL_SAFE,
    RECORD_SOURCE,
    ControlRecord,
    log_record_destination,
    record_destination,
    record_settings_errors,
)
from toinflux.controller import StepTerms
from toinflux.exceptions import ConfigError
from toinflux.general import _validate_controls_block, known_sources, source_class

V1 = {"url": "http://influx", "user": "u", "password": "p"}
V2 = {"url": "http://influx", "token": "t", "org": "o"}

TERMS = StepTerms(input=16.0, setpoint=18.0, demand=460.0, p=400.0, i=60.0, d=0.0, kp=200.0, ki=0.5, kd=0.0, curve=())


class TestWhereTheRecordGoes:
    def test_v1_takes_db(self):
        assert record_destination({"influx": V1, "controls": {"db": "control_db"}}) == "control_db"

    def test_v1_ignores_a_bucket_as_the_writer_does(self):
        """The same choice the write path makes, or reads and writes would disagree about
        which database the history is in."""
        assert record_destination({"influx": V1, "controls": {"bucket": "b"}}) is None

    def test_v2_takes_bucket_then_db(self):
        assert record_destination({"influx": V2, "controls": {"bucket": "b", "db": "d"}}) == "b"
        assert record_destination({"influx": V2, "controls": {"db": "d"}}) == "d"

    @pytest.mark.parametrize("block", [None, {}, {"enabled": True}, "controls"])
    def test_nothing_configured_is_no_record(self, block):
        assert record_destination({"influx": V1, "controls": block}) is None


class TestTheSettingIsChecked:
    @pytest.mark.parametrize("value", ["", "  ", 7, True, "control\ndb"])
    def test_an_unusable_name_is_an_error_naming_the_setting(self, value):
        (error,) = record_settings_errors({"controls": {"db": value}})
        assert error.startswith("controls.db ")

    def test_a_usable_name_is_not(self):
        assert record_settings_errors({"controls": {"db": "control_db", "bucket": "controls"}}) == []

    def test_it_is_part_of_validating_the_controls_block(self):
        """Where --check-config finds it, rather than at the first cycle's write."""
        assert _validate_controls_block({"controls": {"enabled": True, "bucket": ""}}) != []


class TestItIsNotACollector:
    def test_it_is_not_a_known_source(self):
        """There is nothing to poll. Registered as a collector, `sources: [controls]` would
        pass validation and then fail at its first collection."""
        assert RECORD_SOURCE not in known_sources()
        with pytest.raises(ConfigError):
            source_class(RECORD_SOURCE)


@pytest.fixture
def record(installation):
    """Yield the record for a control named with a space, written through a real session.

    Yields:
        ControlRecord: the writer, closed afterwards
    """
    installation.set_settings("controls", db="control_db")
    writer = ControlRecord(installation.settings_file, instance="back room")
    try:
        yield writer
    finally:
        writer.session.close()


def _written(influx):
    return [body.decode() if isinstance(body, bytes) else body for body in influx.writes]


class TestAPoint:
    def test_an_active_point_carries_every_term(self, record, influx):
        record.write(ACTIVE, TERMS, delivered=750.0, timestamp=1700000000)
        (line,) = _written(influx)
        assert line == (
            "control,control=back\\ room input=16.0,setpoint=18.0,demand=460.0,p=400.0,i=60.0,d=0.0,"
            'kp=200.0,ki=0.5,kd=0.0,delivered=750.0,state="active" 1700000000'
        )

    def test_a_fail_safe_point_carries_its_state_alone(self, record, influx):
        record.write(FAIL_SAFE, timestamp=1700000000)
        assert _written(influx) == ['control,control=back\\ room state="fail_safe" 1700000000']

    def test_every_field_it_writes_is_described_to_readers(self, record, influx):
        """An agent tuning the loop reads the meanings from here; a field written with none
        would reach it as a bare name."""
        record.write(ACTIVE, TERMS, delivered=750.0, timestamp=1700000000)
        _series, fields, _stamp = _written(influx)[0].rsplit(" ", 2)
        written = {pair.split("=", 1)[0] for pair in fields.split(",")}
        assert written == set(ControlRecord.MCP_FIELD_METADATA)


class TestAFailedWrite:
    def test_it_does_not_raise_and_the_point_waits(self, record, influx, monkeypatch):
        """An unreachable InfluxDB is the writer's: the cycle that recorded the point carries
        on, and the point is sent once InfluxDB is back."""
        from toinflux import writer

        monkeypatch.setattr(writer.current()._session, "post", _refuse)
        record.write(FAIL_SAFE, timestamp=1700000000)
        assert writer.current().pending()
        monkeypatch.undo()
        writer.current().run_until_idle()
        assert _written(influx) == ['control,control=back\\ room state="fail_safe" 1700000000']


def _refuse(*_args, **_kwargs):
    raise requests.exceptions.ConnectionError("connection refused")


class TestSayingWhereItGoes:
    def test_absent_says_so_and_names_the_settings(self, caplog):
        with caplog.at_level(logging.INFO):
            log_record_destination({"influx": V1, "controls": {"enabled": True}})
        assert "not recording their PID history" in caplog.text
        assert "controls.db" in caplog.text

    def test_present_names_the_database(self, caplog):
        with caplog.at_level(logging.INFO):
            log_record_destination({"influx": V1, "controls": {"db": "control_db"}})
        assert "'control_db'" in caplog.text
