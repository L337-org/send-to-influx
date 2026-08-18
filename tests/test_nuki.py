"""Unit tests for toinflux.nuki (Nuki smart lock via MQTT)."""

from unittest.mock import MagicMock, patch
import pytest

from toinflux.nuki import Nuki

# A typical retained-message set for one lock, as (topic, payload) pairs from the
# shared transport (payloads already UTF-8 decoded).
FRONT_DOOR = [
    ("nuki/2BB28570/deviceType", "4"),
    ("nuki/2BB28570/name", "Front Door"),
    ("nuki/2BB28570/firmware", "3.9.5"),
    ("nuki/2BB28570/mode", "2"),
    ("nuki/2BB28570/state", "1"),
    ("nuki/2BB28570/batteryCritical", "false"),
    ("nuki/2BB28570/batteryChargeState", "85"),
    ("nuki/2BB28570/batteryCharging", "false"),
    ("nuki/2BB28570/doorsensorState", "2"),
    ("nuki/2BB28570/doorsensorBatteryCritical", "false"),
    ("nuki/2BB28570/serverConnected", "true"),
    ("nuki/2BB28570/timestamp", "2026-07-17T10:00:00+00:00"),
    ("nuki/2BB28570/connected", "true"),
]


def _nuki_settings(base, **overrides):
    """Layer the shared mqtt block and the nuki source block onto the base fixture,
    without touching the shared conftest fixture itself."""
    settings = {**base}
    settings["mqtt"] = {
        "broker_host": "mqtt.example.com",
        "username": "sendtoinflux",
        "password": "test_password",
    }
    settings["nuki"] = {"db": "nuki_db", "interval": 300, "timeout": 3, **overrides}
    return settings


def _nuki(settings, messages):
    """Instantiate Nuki with the transport mocked to return the given messages -
    the transport itself is tested in test_mqtt.py, not re-tested here."""
    with patch("toinflux.influx.load_settings") as mock_load_settings:
        mock_load_settings.return_value = settings
        nuki = Nuki(source="nuki")
    nuki.collect_mqtt_messages = MagicMock(return_value=messages)
    return nuki


class TestNuki:
    """Tests for the Nuki class."""

    def test_get_data_returns_state_keyed_by_lock(self, sample_settings):
        """Happy path: one entry per lock, bare field keys, state codes renamed.

        get_data no longer sets a header, because there is one per lock now and send_data
        builds them - see test_writes_one_point_per_lock."""
        nuki = _nuki(_nuki_settings(sample_settings), FRONT_DOOR)
        result = nuki.get_data()
        assert nuki.influx_header is None
        assert nuki.data == result
        assert result["Front_Door"]["stateValue"] == 1
        assert result["Front_Door"]["doorsensorStateValue"] == 2
        assert result["Front_Door"]["batteryCritical"] is False
        assert result["Front_Door"]["batteryChargeState"] == 85
        assert result["Front_Door"]["connected"] is True
        assert result["Front_Door"]["timestamp"] == "2026-07-17T10:00:00+00:00"

    def test_raw_field_names_and_name_field_absent_from_output(self, sample_settings):
        """state/doorsensorState are renamed to their *Value counterparts, and the
        name topic is consumed as the prefix rather than written as a redundant field."""
        nuki = _nuki(_nuki_settings(sample_settings), FRONT_DOOR)
        result = nuki.get_data()
        assert "state" not in result["Front_Door"]
        assert "doorsensorState" not in result["Front_Door"]
        assert "name" not in result["Front_Door"]

    def test_transport_called_with_nuki_filter_and_timeout(self, sample_settings):
        """The nuki topic filter and the nuki.timeout collection window are used."""
        nuki = _nuki(_nuki_settings(sample_settings, timeout=7), FRONT_DOOR)
        nuki.get_data()
        nuki.collect_mqtt_messages.assert_called_once_with("nuki/+/+", 7)

    def test_multiple_devices_merged_into_one_dict(self, sample_settings):
        """Every device the broker reports appears, each under its own name prefix."""
        back_door = [
            ("nuki/11AA22BB/name", "Back Door"),
            ("nuki/11AA22BB/state", "3"),
            ("nuki/11AA22BB/doorsensorState", "3"),
        ]
        nuki = _nuki(_nuki_settings(sample_settings), FRONT_DOOR + back_door)
        result = nuki.get_data()
        assert result["Front_Door"]["stateValue"] == 1
        assert result["Back_Door"]["stateValue"] == 3
        assert result["Back_Door"]["doorsensorStateValue"] == 3

    def test_control_and_event_topics_filtered_out(self, sample_settings):
        """A command/event topic arriving during the window must not become a field."""
        noise = [
            ("nuki/2BB28570/lockActionEvent", "1,0,54322,0,1"),
            ("nuki/2BB28570/commandResponse", "0"),
            ("nuki/2BB28570/lockAction", "2"),
        ]
        nuki = _nuki(_nuki_settings(sample_settings), FRONT_DOOR + noise)
        result = nuki.get_data()
        assert not any("lockAction" in key or "commandResponse" in key for key in result)

    def test_device_without_name_topic_falls_back_to_hex_id(self, sample_settings):
        """A device whose name topic didn't arrive is still reported, keyed by its ID."""
        messages = [("nuki/2BB28570/state", "1")]
        nuki = _nuki(_nuki_settings(sample_settings), messages)
        assert nuki.get_data() == {"2BB28570": {"stateValue": 1}}

    def test_blank_name_payload_falls_back_to_hex_id(self, sample_settings):
        """A blank/whitespace name payload gets the ID fallback too - an empty prefix
        would produce keys like _stateValue and collide across devices."""
        messages = [("nuki/2BB28570/name", "   "), ("nuki/2BB28570/state", "1")]
        nuki = _nuki(_nuki_settings(sample_settings), messages)
        assert nuki.get_data() == {"2BB28570": {"stateValue": 1}}

    def test_duplicate_device_names_warn_and_last_wins(self, sample_settings, caplog):
        """Two devices with the same Nuki-app name collide - highest device ID wins
        deterministically (by sorted ID, not fragile MQTT arrival order), loudly."""
        # Deliberately deliver BBBB0002's messages *first*, to prove the outcome is
        # fixed by device-ID sort order, not by arrival order.
        messages = [
            ("nuki/BBBB0002/name", "Door"),
            ("nuki/BBBB0002/state", "3"),
            ("nuki/AAAA0001/name", "Door"),
            ("nuki/AAAA0001/state", "1"),
        ]
        nuki = _nuki(_nuki_settings(sample_settings), messages)
        with caplog.at_level("WARNING"):
            result = nuki.get_data()
        # BBBB0002 sorts after AAAA0001, so it wins (state 3 = unlocked) regardless of
        # the reversed arrival order above.
        assert result["Door"]["stateValue"] == 3
        assert any("Duplicate Nuki device name" in record.message for record in caplog.records)

    def test_empty_window_returns_empty_dict_with_debug_log(self, sample_settings, caplog):
        """A connected broker with nothing retained is no data, not an error - logged at
        DEBUG only, since send_data()'s central missing-data path warns once already."""
        nuki = _nuki(_nuki_settings(sample_settings), [])
        with caplog.at_level("DEBUG"):
            assert nuki.get_data() == {}
        records = [r for r in caplog.records if "No Nuki device state" in r.message]
        assert records and all(r.levelname == "DEBUG" for r in records)

    def test_undocumented_state_code_still_written_as_raw_number(self, sample_settings):
        """A code with no documented meaning in UNITS.md (e.g. a future firmware
        addition) is still written through as-is - the renamed field carries the raw
        code regardless of whether Nuki has documented it."""
        messages = [("nuki/2BB28570/state", "42"), ("nuki/2BB28570/doorsensorState", "99")]
        nuki = _nuki(_nuki_settings(sample_settings), messages)
        result = nuki.get_data()
        assert result["2BB28570"]["stateValue"] == 42
        assert result["2BB28570"]["doorsensorStateValue"] == 99

    def test_textual_fields_never_shape_cast(self, sample_settings):
        """firmware/timestamp stay strings even when they happen to look numeric - a
        firmware of "4.0" cast to float would type-conflict the whole point against
        an install whose firmware field was established as a string."""
        messages = [
            ("nuki/2BB28570/firmware", "4.0"),
            ("nuki/2BB28570/timestamp", "20260718"),
        ]
        nuki = _nuki(_nuki_settings(sample_settings), messages)
        result = nuki.get_data()
        assert result["2BB28570"]["firmware"] == "4.0"
        assert result["2BB28570"]["timestamp"] == "20260718"

    def test_timestamp_left_none(self, sample_settings):
        """Nuki reports current state - send_data() should default to poll time."""
        nuki = _nuki(_nuki_settings(sample_settings), FRONT_DOOR)
        nuki.get_data()
        assert nuki.timestamp is None

    def test_malformed_topic_ignored(self, sample_settings):
        """Topics that don't match nuki/<id>/<field> are skipped, not fatal."""
        messages = [("nuki/oddness", "x"), ("nuki/2BB28570/state/extra", "1")] + FRONT_DOOR
        nuki = _nuki(_nuki_settings(sample_settings), messages)
        assert nuki.get_data()["Front_Door"]["stateValue"] == 1

    def test_decodes_float_and_non_numeric_payloads(self, sample_settings):
        """_decode_scalar's float and string-fallback branches: MQTT payloads are bare
        strings, so anything not bool/int must still land as a usable field value."""
        messages = [
            ("nuki/2BB28570/batteryChargeState", "85.5"),
            ("nuki/2BB28570/mode", "door mode"),
        ]
        nuki = _nuki(_nuki_settings(sample_settings), messages)
        result = nuki.get_data()
        assert result["2BB28570"]["batteryChargeState"] == 85.5
        assert result["2BB28570"]["mode"] == "door mode"

    def test_bool_payload_never_renamed_into_the_numeric_value_field(self, sample_settings):
        """A malformed "true"/"false" payload on the state topic is shape-cast to
        bool but kept under the original "state" key rather than renamed to
        stateValue - InfluxDB fixes a field's type on first write, so a stray bool
        renamed into stateValue would poison that field against every later,
        real numeric write."""
        messages = [("nuki/2BB28570/state", "true")]
        nuki = _nuki(_nuki_settings(sample_settings), messages)
        result = nuki.get_data()
        assert result["2BB28570"]["state"] is True
        assert "stateValue" not in result["2BB28570"]


class TestNukiStreaming:
    """Tests for decode_stream_message - the per-message interrupt path (slice 3)."""

    def _handler(self, sample_settings):
        # collect_mqtt_messages is unused by the streaming path, but _nuki mocks it harmlessly.
        return _nuki(_nuki_settings(sample_settings), [])

    def test_stream_topic_filter_matches_the_snapshot_filter(self):
        """The live subscription uses the same filter as the fixed-window snapshot."""
        assert Nuki.STREAM_TOPIC_FILTER == "nuki/+/+"

    def test_name_topic_is_remembered_and_yields_no_point(self, sample_settings):
        """A name message produces no point of its own but is remembered as the prefix for
        that device's later state messages."""
        nuki = self._handler(sample_settings)
        assert nuki.decode_stream_message("nuki/2BB28570/name", "Front Door") is None
        assert nuki.decode_stream_message("nuki/2BB28570/state", "1") == {"Front_Door": {"stateValue": 1}}

    def test_state_before_any_name_falls_back_to_device_id(self, sample_settings):
        """Until a name is seen, fields are keyed by the device ID (same fallback as the
        snapshot path); in practice the retained name arrives first on subscribe."""
        nuki = self._handler(sample_settings)
        assert nuki.decode_stream_message("nuki/2BB28570/state", "1") == {"2BB28570": {"stateValue": 1}}

    def test_doorsensor_state_is_renamed(self, sample_settings):
        nuki = self._handler(sample_settings)
        result = nuki.decode_stream_message("nuki/2BB28570/doorsensorState", "3")
        assert result == {"2BB28570": {"doorsensorStateValue": 3}}

    def test_control_and_event_topics_are_ignored(self, sample_settings):
        nuki = self._handler(sample_settings)
        assert nuki.decode_stream_message("nuki/2BB28570/lockAction", "2") is None
        assert nuki.decode_stream_message("nuki/2BB28570/commandResponse", "0") is None

    def test_malformed_topic_is_ignored(self, sample_settings):
        nuki = self._handler(sample_settings)
        assert nuki.decode_stream_message("nuki/2BB28570/state/extra", "1") is None
        assert nuki.decode_stream_message("nuki/oddness", "x") is None

    def test_returns_the_same_shape_as_the_snapshot_path(self, sample_settings):
        """The two paths previously agreed only by both happening to build the same
        prefix_field string in two separate places. Returning the same {device: {field:
        value}} shape means one write path serves both and they cannot drift."""
        nuki = self._handler(sample_settings)
        nuki.decode_stream_message("nuki/2BB28570/name", "Front Door")
        streamed = nuki.decode_stream_message("nuki/2BB28570/state", "1")
        assert streamed == {"Front_Door": {"stateValue": 1}}
        # No header is set here either; send_data builds one per lock.
        assert nuki.influx_header is None

    def test_name_with_spaces_becomes_underscores(self, sample_settings):
        nuki = self._handler(sample_settings)
        nuki.decode_stream_message("nuki/2BB28570/name", "Front Door")
        result = nuki.decode_stream_message("nuki/2BB28570/batteryChargeState", "85")
        assert result == {"Front_Door": {"batteryChargeState": 85}}

    def test_blank_name_falls_back_to_device_id(self, sample_settings):
        nuki = self._handler(sample_settings)
        assert nuki.decode_stream_message("nuki/2BB28570/name", "   ") is None
        assert nuki.decode_stream_message("nuki/2BB28570/state", "1") == {"2BB28570": {"stateValue": 1}}

    def test_bool_state_payload_kept_under_original_key(self, sample_settings):
        """Same guard as the snapshot path: a stray bool state payload isn't renamed into
        the numeric stateValue field (which InfluxDB would then type-lock as boolean)."""
        nuki = self._handler(sample_settings)
        assert nuki.decode_stream_message("nuki/2BB28570/state", "true") == {"2BB28570": {"state": True}}

    def test_name_memory_is_per_device(self, sample_settings):
        """Each device's remembered name prefixes only its own fields."""
        nuki = self._handler(sample_settings)
        nuki.decode_stream_message("nuki/AAAA0001/name", "Front Door")
        nuki.decode_stream_message("nuki/BBBB0002/name", "Back Door")
        assert nuki.decode_stream_message("nuki/AAAA0001/state", "1") == {"Front_Door": {"stateValue": 1}}
        assert nuki.decode_stream_message("nuki/BBBB0002/state", "3") == {"Back_Door": {"stateValue": 3}}

    def test_duplicate_device_name_warns(self, sample_settings, caplog):
        """Two devices sharing a name (same field-key prefix) would silently merge their
        series, so setting the second one warns - matching the snapshot path's behaviour."""
        nuki = self._handler(sample_settings)
        nuki.decode_stream_message("nuki/AAAA0001/name", "Front Door")
        with caplog.at_level("WARNING"):
            nuki.decode_stream_message("nuki/BBBB0002/name", "Front Door")
        assert any("Duplicate Nuki device name" in r.message for r in caplog.records)

    def test_same_device_resending_its_name_does_not_warn(self, sample_settings, caplog):
        """A device re-sending its own retained name (e.g. on reconnect) is not a
        collision and must not warn."""
        nuki = self._handler(sample_settings)
        nuki.decode_stream_message("nuki/AAAA0001/name", "Front Door")
        with caplog.at_level("WARNING"):
            nuki.decode_stream_message("nuki/AAAA0001/name", "Front Door")
        assert not any("Duplicate Nuki device name" in r.message for r in caplog.records)


class TestPerLockPoints:
    """Each lock is its own point, tagged with the lock, on
    both the snapshot and the streaming path."""

    @staticmethod
    def _handler(sample_settings):
        settings = _nuki_settings(sample_settings)
        with patch("toinflux.influx.load_settings", return_value=settings):
            handler = Nuki("nuki")
        handler.session = MagicMock()
        return handler

    @staticmethod
    def _captured(handler):
        """Record what the base send_data would have written, per call."""
        written = []
        base = Nuki.__mro__[2]
        patcher = patch.object(
            base,
            "send_data",
            side_effect=lambda **kw: written.append((handler.influx_header, kw["data"], kw["timestamp"])),
        )
        return written, patcher

    def test_writes_one_point_per_lock(self, sample_settings):
        handler = self._handler(sample_settings)
        handler.data = {
            "Front_Door": {"stateValue": 1, "batteryChargeState": 90},
            "Back_Door": {"stateValue": 3},
        }
        written, patcher = self._captured(handler)
        with patcher:
            handler.send_data()
        assert [header for header, _, _ in written] == ["nuki,device=Back_Door ", "nuki,device=Front_Door "]
        assert [fields for _, fields, _ in written] == [{"stateValue": 3}, {"stateValue": 1, "batteryChargeState": 90}]

    def test_the_broker_host_tag_is_gone(self, sample_settings):
        """Deliberate: every lock arrives via the one broker, so the tag never distinguished
        anything, and changing broker should not change the data."""
        handler = self._handler(sample_settings)
        handler.data = {"Front_Door": {"stateValue": 1}}
        written, patcher = self._captured(handler)
        with patcher:
            handler.send_data()
        assert "host=" not in written[0][0]

    def test_every_lock_in_a_snapshot_shares_one_timestamp(self, sample_settings):
        """Letting each write default independently would scatter one snapshot across a
        second or two, so a query asking what the state was at time T could see one lock's
        reading and not another's."""
        handler = self._handler(sample_settings)
        handler.data = {f"Lock_{n}": {"stateValue": n} for n in range(5)}
        written, patcher = self._captured(handler)
        with patcher:
            handler.send_data()
        assert len({timestamp for _, _, timestamp in written}) == 1

    def test_a_label_needing_escaping_is_escaped(self, sample_settings):
        """The header is written verbatim, so an unescaped space or comma would end the tag
        set early and silently corrupt the point."""
        handler = self._handler(sample_settings)
        handler.data = {"odd label,x": {"stateValue": 1}}
        written, patcher = self._captured(handler)
        with patcher:
            handler.send_data()
        assert written[0][0] == "nuki,device=odd\\ label\\,x "

    def test_one_failing_lock_does_not_stop_the_others(self, sample_settings):
        """And the worker still backs off, because an InfluxWriteError is raised at the end.
        Points are idempotent, so the retry re-writing a lock that already succeeded is
        harmless."""
        from toinflux.influx import InfluxWriteError

        handler = self._handler(sample_settings)
        handler.data = {"A": {"stateValue": 1}, "B": {"stateValue": 2}, "C": {"stateValue": 3}}
        attempted = []

        def flaky(**kw):
            attempted.append(handler.influx_header)
            if "B" in handler.influx_header:
                raise InfluxWriteError("boom")

        with patch.object(Nuki.__mro__[2], "send_data", side_effect=flaky):
            with pytest.raises(InfluxWriteError, match="B: boom"):
                handler.send_data()
        assert len(attempted) == 3, "a failure must not abandon the remaining locks"

    def test_an_empty_reading_still_reaches_the_base_implementation(self, sample_settings):
        """So the empty-reading logging and the buffer flush both still happen, exactly as
        for any other source."""
        handler = self._handler(sample_settings)
        handler.data = {}
        with patch.object(Nuki.__mro__[2], "send_data") as base:
            handler.send_data()
        base.assert_called_once()
        assert base.call_args.kwargs["data"] == {}

    def test_a_flat_payload_goes_to_the_base_with_the_callers_header(self, sample_settings):
        """The override must not capture every caller of send_data().

        send_heartbeat() sets its own collector_status header and passes a flat
        {field: value} dict; the streaming path passes per-device data explicitly. So "was
        data given?" cannot tell them apart - the shape is what does. Treating the flat
        payload as per-device made Nuki write no heartbeat at all.
        """
        handler = self._handler(sample_settings)
        handler.influx_header = "collector_status,source=nuki "
        written, patcher = self._captured(handler)
        with patcher:
            handler.send_data(data={"ok": 1, "consecutive_failures": 0}, timestamp=1700000000)
        assert len(written) == 1
        header, data, _ = written[0]
        # The base receives the flat payload untouched, under the caller's own header - not a
        # per-lock header built from the field names.
        assert data == {"ok": 1, "consecutive_failures": 0}
        assert header == "collector_status,source=nuki "
        assert handler.influx_header == "collector_status,source=nuki ", "the caller's header was not restored"

    @pytest.mark.parametrize(
        "payload,per_device",
        [
            ({"Front_Door": {"stateValue": 1}}, True),
            ({"Front_Door": {}}, True),
            ({"ok": 1, "consecutive_failures": 0}, False),
            ({"ok": 1}, False),
            ({}, False),
            (None, False),
            ("not a dict", False),
            # A payload no code path produces. The conservative direction is the base, which
            # honours the caller's header rather than overwriting it with a lock's.
            ({"Front_Door": {"stateValue": 1}, "ok": 1}, False),
        ],
    )
    def test_the_shape_discriminator(self, payload, per_device):
        from toinflux.nuki import _is_per_device

        assert _is_per_device(payload) is per_device

    def test_an_unusable_lock_name_does_not_stop_the_other_locks(self, sample_settings):
        """The docstring promises one lock failing does not stop the rest, and that has to
        cover a lock whose *name* is unusable as well as one whose write fails.

        A lock name comes from the retained MQTT ``name`` topic, so it is external input. One
        containing a newline cannot be escaped - a newline is what separates points - so
        escape_key_or_tag_value raises. Built outside the guarded block, that aborted the loop
        and every lock sorting after the bad one went unwritten, which is a promise the
        docstring made and the code did not keep.
        """
        from toinflux.influx import InfluxWriteError

        handler = self._handler(sample_settings)
        handler.data = {
            "Aaa_Good": {"stateValue": 1},
            "Bad\nnuki,device=Injected fake": {"stateValue": 2},
            "Zzz_Good": {"stateValue": 3},
        }
        written, patcher = self._captured(handler)
        with patcher:
            # Still raises, so the worker keeps backing off rather than treating the cycle as
            # healthy - the bad name will not fix itself, but the good locks keep reporting.
            with pytest.raises(InfluxWriteError, match="cannot contain a newline"):
                handler.send_data(timestamp=1700000000)

        headers = [header for header, _, _ in written]
        assert headers == ["nuki,device=Aaa_Good ", "nuki,device=Zzz_Good "], headers
        assert not any("Injected" in header for header in headers)

    def test_the_header_is_restored_after_writing(self, sample_settings):
        """send_data() swaps a per-lock header in for each write, and must put back what it
        found. Left dirty, the handler holds the *last* lock's header, and any later write that
        reads it - a heartbeat's own save/restore, an empty-reading delegation to the base -
        silently carries one lock's identity into something that is not that lock.
        """
        handler = self._handler(sample_settings)
        handler.data = {"Front_Door": {"stateValue": 1}, "Back_Door": {"stateValue": 3}}
        before = handler.influx_header
        written, patcher = self._captured(handler)
        with patcher:
            handler.send_data()
        assert len(written) == 2
        assert handler.influx_header == before

    def test_the_header_is_restored_even_when_a_write_fails(self, sample_settings):
        """The restore is in a finally for this case: a failing lock must not leave the header
        dirty either, or one InfluxDB outage permanently mislabels the handler."""
        handler = self._handler(sample_settings)
        handler.data = {"Front_Door": {"stateValue": 1}, "Back_Door": {"stateValue": 3}}
        before = handler.influx_header
        from toinflux.influx import InfluxWriteError

        with patch.object(Nuki.__mro__[2], "send_data", side_effect=InfluxWriteError("boom")):
            with pytest.raises(InfluxWriteError):
                handler.send_data()
        assert handler.influx_header == before


class TestLiveStatePerLock:
    """Nuki is the only source whose single live read covers every producer: the locks
    all arrive over one MQTT subscription. Hue reads live per bridge with a handler each, and
    Speedtest's live read can only speak for the local host - so this third shape needed
    distinguishing rather than assuming."""

    def test_the_flag_is_set_only_where_it_is_true(self):
        from toinflux.general import source_class

        assert source_class("nuki").MCP_LIVE_STATE_COVERS_ALL_INSTANCES is True
        for other in ("hue", "speedtest", "zappi", "openmeteo", "octopus"):
            assert source_class(other).MCP_LIVE_STATE_COVERS_ALL_INSTANCES is False, other

    def test_current_state_reports_every_lock_from_one_live_read(self, sample_settings):
        from toinflux.mcp_read import current_state_result

        settings = {**_nuki_settings(sample_settings), "sources": ["nuki"]}
        with patch("toinflux.influx.load_settings", return_value=settings):
            handler = Nuki("nuki")
        handler.session = MagicMock()
        with (
            patch.object(
                Nuki,
                "get_data",
                return_value={
                    "Front_Door": {"stateValue": 1, "batteryChargeState": 90},
                    "Back_Door": {"stateValue": 3},
                },
            ),
            patch("toinflux.mcp_common.get_class", return_value=handler),
        ):
            result = current_state_result("nuki", settings, None)
        assert result["state"] == "live"
        assert result["instance_tag"] == "device"
        assert set(result["instances"]) == {"Front_Door", "Back_Door"}
        # Coded values still read back as labels, and units still attach.
        assert result["instances"]["Front_Door"]["fields"]["stateValue"]["label"] == "locked"
        assert result["instances"]["Front_Door"]["fields"]["batteryChargeState"]["unit"] == "%"
        assert result["instances"]["Back_Door"]["fields"]["stateValue"]["label"] == "unlocked"
        assert "fields" not in result, "the flat shape must not be returned alongside instances"

    def test_a_failing_live_read_propagates(self, sample_settings):
        """One read covers every lock, so a failure means every lock failed - there is no
        partial answer to give, unlike Hue where one bridge can fail and the others report."""
        from toinflux.exceptions import SourceConnectionError
        from toinflux.mcp_read import current_state_result

        settings = {**_nuki_settings(sample_settings), "sources": ["nuki"]}
        with patch("toinflux.influx.load_settings", return_value=settings):
            handler = Nuki("nuki")
        handler.session = MagicMock()
        with (
            patch.object(Nuki, "get_data", side_effect=SourceConnectionError("broker unreachable")),
            patch("toinflux.mcp_common.get_class", return_value=handler),
        ):
            with pytest.raises(SourceConnectionError, match="broker unreachable"):
                current_state_result("nuki", settings, None)

    def test_an_empty_snapshot_reports_no_locks_rather_than_failing(self, sample_settings):
        """A broker that is reachable but has delivered no retained state is not an error, and
        must not be reported as one. It reports an empty set of locks - the same shape a
        single-target source's empty live read gives, so nothing reading the payload has to
        special-case it."""
        from toinflux.mcp_read import current_state_result

        settings = {**_nuki_settings(sample_settings), "sources": ["nuki"]}
        with patch("toinflux.influx.load_settings", return_value=settings):
            handler = Nuki("nuki")
        handler.session = MagicMock()
        with (
            patch.object(Nuki, "get_data", return_value={}),
            patch("toinflux.mcp_common.get_class", return_value=handler),
        ):
            result = current_state_result("nuki", settings, None)
        assert result["instances"] == {}
        assert result["instance_tag"] == "device"

    def test_the_handler_session_is_closed_even_on_the_per_lock_path(self, sample_settings):
        """The per-lock branch returns early, before the shared per-instance loop. It sits
        inside the same try/finally, but nothing asserted that - and a leaked session per
        current-state call is the kind of thing that only shows up under load."""
        from toinflux.mcp_read import current_state_result

        settings = {**_nuki_settings(sample_settings), "sources": ["nuki"]}
        with patch("toinflux.influx.load_settings", return_value=settings):
            handler = Nuki("nuki")
        handler.session = MagicMock()
        with (
            patch.object(Nuki, "get_data", return_value={"Front_Door": {"stateValue": 1}}),
            patch("toinflux.mcp_common.get_class", return_value=handler),
        ):
            current_state_result("nuki", settings, None)
        handler.session.close.assert_called_once()
