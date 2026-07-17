"""Unit tests for toinflux.nuki (Nuki smart lock via MQTT)."""

from unittest.mock import patch

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
    patch.object(nuki, "collect_mqtt_messages", return_value=messages).start()
    return nuki


class TestNuki:
    """Tests for the Nuki class."""

    def teardown_method(self):
        patch.stopall()

    def test_get_data_sets_header_and_parses_state(self, sample_settings):
        """Happy path: one device, fields prefixed by its name, labels resolved."""
        nuki = _nuki(_nuki_settings(sample_settings), FRONT_DOOR)
        result = nuki.get_data()
        assert nuki.influx_header == "nuki,host=mqtt.example.com "
        assert nuki.data == result
        assert result["Front_Door_stateName"] == "locked"
        assert result["Front_Door_doorsensorStateName"] == "door closed"
        assert result["Front_Door_batteryCritical"] is False
        assert result["Front_Door_batteryChargeState"] == 85
        assert result["Front_Door_connected"] is True
        assert result["Front_Door_timestamp"] == "2026-07-17T10:00:00+00:00"

    def test_raw_numeric_codes_and_name_field_absent_from_output(self, sample_settings):
        """state/doorsensorState are replaced by their labels, and the name topic is
        consumed as the prefix rather than written as a redundant field."""
        nuki = _nuki(_nuki_settings(sample_settings), FRONT_DOOR)
        result = nuki.get_data()
        assert "Front_Door_state" not in result
        assert "Front_Door_doorsensorState" not in result
        assert "Front_Door_name" not in result

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
        assert result["Front_Door_stateName"] == "locked"
        assert result["Back_Door_stateName"] == "unlocked"
        assert result["Back_Door_doorsensorStateName"] == "door opened"

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
        assert nuki.get_data() == {"2BB28570_stateName": "locked"}

    def test_blank_name_payload_falls_back_to_hex_id(self, sample_settings):
        """A blank/whitespace name payload gets the ID fallback too - an empty prefix
        would produce keys like _stateName and collide across devices."""
        messages = [("nuki/2BB28570/name", "   "), ("nuki/2BB28570/state", "1")]
        nuki = _nuki(_nuki_settings(sample_settings), messages)
        assert nuki.get_data() == {"2BB28570_stateName": "locked"}

    def test_duplicate_device_names_warn_and_last_wins(self, sample_settings, caplog):
        """Two devices with the same Nuki-app name collide - later value wins, loudly."""
        messages = [
            ("nuki/AAAA0001/name", "Door"),
            ("nuki/AAAA0001/state", "1"),
            ("nuki/BBBB0002/name", "Door"),
            ("nuki/BBBB0002/state", "3"),
        ]
        nuki = _nuki(_nuki_settings(sample_settings), messages)
        with caplog.at_level("WARNING"):
            result = nuki.get_data()
        # Devices are processed in message order (insertion-ordered dict), so the
        # later device (BBBB0002, state 3 = unlocked) deterministically wins.
        assert result["Door_stateName"] == "unlocked"
        assert any("Duplicate Nuki device name" in record.message for record in caplog.records)

    def test_empty_window_returns_empty_dict_with_debug_log(self, sample_settings, caplog):
        """A connected broker with nothing retained is no data, not an error - logged at
        DEBUG only, since send_data()'s central missing-data path warns once already."""
        nuki = _nuki(_nuki_settings(sample_settings), [])
        with caplog.at_level("DEBUG"):
            assert nuki.get_data() == {}
        records = [r for r in caplog.records if "No Nuki device state" in r.message]
        assert records and all(r.levelname == "DEBUG" for r in records)

    def test_unrecognised_state_code_kept_as_raw_number(self, sample_settings):
        """An unknown numeric code (e.g. a future firmware addition) keeps the original
        field key and its raw value rather than being dropped or mislabelled."""
        messages = [("nuki/2BB28570/state", "42"), ("nuki/2BB28570/doorsensorState", "99")]
        nuki = _nuki(_nuki_settings(sample_settings), messages)
        result = nuki.get_data()
        assert result["2BB28570_state"] == 42
        assert result["2BB28570_doorsensorState"] == 99

    def test_timestamp_left_none(self, sample_settings):
        """Nuki reports current state - send_data() should default to poll time."""
        nuki = _nuki(_nuki_settings(sample_settings), FRONT_DOOR)
        nuki.get_data()
        assert nuki.timestamp is None

    def test_malformed_topic_ignored(self, sample_settings):
        """Topics that don't match nuki/<id>/<field> are skipped, not fatal."""
        messages = [("nuki/oddness", "x"), ("nuki/2BB28570/state/extra", "1")] + FRONT_DOOR
        nuki = _nuki(_nuki_settings(sample_settings), messages)
        assert nuki.get_data()["Front_Door_stateName"] == "locked"
