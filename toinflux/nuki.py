"""Functions to get data from Nuki smart locks via MQTT and format it for InfluxDB"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT License"
__version__ = "1.0"

import logging
from toinflux.mqtt import MqttDataHandler

# The read-only per-device state topics from the Nuki MQTT API spec (v1.6). Anything
# else under nuki/<id>/ (lockAction, lock, unlock, commandResponse, lockActionEvent)
# is a control/event topic and is filtered out - a command event that happens to fire
# during a collection window must not pollute the InfluxDB schema.
KNOWN_STATE_FIELDS = frozenset(
    {
        "deviceType",
        "name",
        "firmware",
        "mode",
        "state",
        "batteryCritical",
        "batteryChargeState",
        "batteryCharging",
        "keypadBatteryCritical",
        "doorsensorState",
        "doorsensorBatteryCritical",
        "ringactionTimestamp",
        "serverConnected",
        "timestamp",
        "connected",
    }
)

# Fields whose values are inherently text and must never be shape-cast: a firmware
# of "4.0" would otherwise become a float while "3.9.5" stays a string, and since an
# InfluxDB field's type is fixed by its first write, the type conflict would reject
# the WHOLE point (all fields, all devices) until it aged out of the write buffer -
# real data loss caused by a cosmetic field.
STRING_FIELDS = frozenset({"firmware", "timestamp", "ringactionTimestamp"})

# state/doorsensorState are reported under these renamed keys, numeric always -
# Grafana struggles to visualise text (see UNITS.md for what each code means), and a
# fixed field name/type per lock is simpler to chart than one whose name changes
# depending on whether a given code happens to be documented.
STATE_VALUE_FIELDS = {
    "state": "stateValue",
    "doorsensorState": "doorsensorStateValue",
}


# Nuki state-code meanings (MQTT API spec v1.6), for the MCP read tool to decode
# the numeric stateValue/doorsensorStateValue fields into labels - see UNITS.md.
# An undocumented code is passed through with a null label, matching the
# collector's own raw-passthrough rule.
STATE_VALUE_CODES = {
    0: "uncalibrated",
    1: "locked",
    2: "unlocking",
    3: "unlocked",
    4: "locking",
    5: "unlatched",
    6: "unlocked (lock 'n' go)",
    7: "unlatching",
    254: "motor blocked",
    255: "undefined",
}
DOORSENSOR_STATE_CODES = {
    1: "deactivated",
    2: "door closed",
    3: "door opened",
    4: "door state unknown",
    5: "calibrating",
    16: "uncalibrated",
    240: "tampered",
    255: "unknown",
}


class Nuki(MqttDataHandler):
    """
    Child class of MqttDataHandler to get lock/door-sensor state from Nuki smart locks.

    Nuki devices publish their state to the configured MQTT broker with the retain
    flag set on every state topic, so a short subscribe window per collection cycle
    receives the full last-known state of every provisioned lock - equivalent to an
    HTTP GET against the other sources' APIs. Every device the broker knows about is
    reported automatically, with field keys prefixed by the device's own Nuki-app
    name, so multiple locks need no per-lock configuration.
    """

    MCP_DESCRIPTION = "Nuki smart locks and door sensors: lock state, door state and battery levels."
    # Fields carry a per-lock name prefix (Front_Door_stateValue), so the read
    # tool's metadata keys on the suffix - see ReadSchema.metadata_for.
    MCP_FIELD_METADATA = {
        "stateValue": {"codes": STATE_VALUE_CODES},
        "doorsensorStateValue": {"codes": DOORSENSOR_STATE_CODES},
        "batteryChargeState": {"unit": "%"},
    }

    # The live subscription filter for the streaming path - the same topic filter the
    # fixed-window snapshot uses (see parse_nuki_data). Setting it (with
    # decode_stream_message below) is what tells the worker this source is wired to
    # stream rather than poll (see sendtoinflux._should_stream).
    STREAM_TOPIC_FILTER = "nuki/+/+"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Per-device name memory for the streaming path. Retained `name` topics arrive as
        # their own messages, so decode_stream_message remembers each device's name to
        # prefix that device's later state messages with (the snapshot path consumes
        # `name` inline instead). Keyed by device ID; refreshed whenever a `name` message
        # arrives, including the retained one redelivered on every (re)subscribe.
        self._device_names = {}

    def get_data(self):
        """
        Get the current state of every Nuki device from the MQTT broker

        :return: data
        :rtype: dict
        """
        # Parse first: collect_mqtt_messages raises ConfigError for a missing mqtt
        # block/broker_host before the header would need to read it.
        self.data = self.parse_nuki_data()
        self.influx_header = f"nuki,host={self.settings['mqtt']['broker_host']} "
        return self.data

    def parse_nuki_data(self):
        """
        Collect retained MQTT messages and parse them into InfluxDB fields.

        Messages are grouped per device by the ID segment of the topic
        (``nuki/<id>/<field>``); each device's ``name`` topic is consumed as its
        field-key prefix (falling back to the ID if no name arrived) rather than
        written as a field of its own, and the remaining fields are merged into one
        flat dict for a single point per collection cycle.

        :return: data
        :rtype: dict
        """
        timeout = self.settings["nuki"].get("timeout", 3)
        devices = {}
        for topic, payload in self.collect_mqtt_messages("nuki/+/+", timeout):
            parts = topic.split("/")
            if len(parts) != 3 or parts[2] not in KNOWN_STATE_FIELDS:
                logging.debug("Ignoring non-state MQTT topic %s", topic)
                continue
            devices.setdefault(parts[1], {})[parts[2]] = payload
        data = {}
        # Iterate by device ID, not MQTT arrival order: brokers don't guarantee a
        # stable retained-message delivery order, so on a same-name collision this
        # keeps "last wins" deterministic (highest device ID) across cycles rather
        # than letting fields flap between devices.
        for device_id, fields in sorted(devices.items()):
            # A blank/whitespace name gets the same device-ID fallback as an absent one -
            # an empty prefix would produce keys like "_stateValue" and collide across devices.
            prefix = (fields.pop("name", "").strip() or device_id).replace(" ", "_")
            for field, raw in fields.items():
                key, value = self._decode_field(field, raw)
                if f"{prefix}_{key}" in data:
                    logging.warning(
                        "Duplicate Nuki device name '%s' - field %s overwritten; give each lock a"
                        " distinct name in the Nuki app",
                        prefix,
                        key,
                    )
                data[f"{prefix}_{key}"] = value
        if not data:
            # DEBUG, not WARNING: send_data()'s central _log_missing_data path already
            # warns once per empty cycle - a second warning here would just duplicate it.
            logging.debug("No Nuki device state received from the MQTT broker")
        return data

    def decode_stream_message(self, topic, payload):
        """
        Decode one streamed Nuki message into a single InfluxDB field (the interrupt path).

        The event-driven counterpart to parse_nuki_data's per-topic handling: a ``name``
        topic is remembered as that device's field-key prefix and produces no point of its
        own; any other known state topic is decoded via ``_decode_field`` and returned as a
        one-field dict keyed ``<device name or id>_<field>``. Control/event topics
        (lockAction, ...) and malformed topics are ignored (return None).

        The device-name prefix falls back to the device ID until a ``name`` message has been
        seen, exactly as parse_nuki_data falls back for a device with no name. Because Nuki
        publishes ``name`` retained, it's redelivered on every (re)subscribe ahead of the
        state topics, so in practice the fallback is only ever hit for a device that has no
        name set at all.

        :param topic: the message's MQTT topic (e.g. ``nuki/2BB28570/state``)
        :type topic: str
        :param payload: the payload as received (UTF-8 decoded)
        :type payload: str
        :return: a single ``{field_key: value}`` to write immediately, or None to ignore
            the message (a control/event/malformed topic, or a ``name`` update consumed as
            a prefix)
        :rtype: dict or None
        """
        parts = topic.split("/")
        if len(parts) != 3 or parts[2] not in KNOWN_STATE_FIELDS:
            logging.debug("Ignoring non-state MQTT topic %s", topic)
            return None
        device_id, field = parts[1], parts[2]
        if field == "name":
            self._remember_device_name(device_id, payload)
            return None
        # A per-message write can arrive before the first periodic snapshot's get_data()
        # has set the header, so set it here too (send_data reads influx_header).
        self.influx_header = f"nuki,host={self.settings['mqtt']['broker_host']} "
        prefix = self._name_prefix(self._device_names.get(device_id, ""), device_id)
        key, value = self._decode_field(field, payload)
        return {f"{prefix}_{key}": value}

    @staticmethod
    def _name_prefix(name, device_id):
        """
        The field-key prefix for a device: its name with spaces underscored, or the device
        ID when the name is blank/absent (an empty prefix would produce keys like
        ``_stateValue`` and collide across devices). Matches parse_nuki_data's prefix rule.

        :param name: the device's Nuki-app name (may be blank/whitespace)
        :type name: str
        :param device_id: the device's ID, used as the fallback prefix
        :type device_id: str
        :return: the field-key prefix
        :rtype: str
        """
        return (name.strip() or device_id).replace(" ", "_")

    def _remember_device_name(self, device_id, name):
        """
        Record a device's name for use as its streaming field-key prefix.

        Warns if the name resolves to a prefix already claimed by a *different* device -
        two locks sharing a Nuki-app name would silently merge their field keys into one
        ambiguous time series. The snapshot path (parse_nuki_data) warns on the same
        condition per cycle; this surfaces it on the streaming path too, when the name is
        set, rather than silently. A device re-sending its own retained name (e.g. on
        reconnect) is not a collision.

        :param device_id: the device the name belongs to
        :type device_id: str
        :param name: the name payload as received (UTF-8 decoded)
        :type name: str
        :return: None
        """
        prefix = self._name_prefix(name, device_id)
        for other_id, other_name in self._device_names.items():
            if other_id != device_id and self._name_prefix(other_name, other_id) == prefix:
                logging.warning(
                    "Duplicate Nuki device name '%s' - devices %s and %s share a field-key prefix, so"
                    " their fields will collide; give each lock a distinct name in the Nuki app",
                    prefix,
                    other_id,
                    device_id,
                )
                break
        self._device_names[device_id] = name

    @staticmethod
    def _decode_field(field, raw):
        """
        Decode one state topic's payload into an InfluxDB field key and value.

        ``state``/``doorsensorState`` are renamed to ``stateValue``/
        ``doorsensorStateValue`` (see :data:`STATE_VALUE_FIELDS`); their value is
        always the raw numeric code - see UNITS.md for what each code means.
        Everything else is cast by shape: true/false to bool, numeric strings to
        int/float, anything else left as a string.

        :param field: the topic's field name (last topic segment)
        :type field: str
        :param raw: the payload as received (UTF-8 decoded)
        :type raw: str
        :return: (field key, decoded value)
        :rtype: tuple
        """
        if field in STRING_FIELDS:
            return field, raw
        value = Nuki._decode_scalar(raw)
        # bool is an int subclass, but stateValue/doorsensorStateValue are
        # documented (UNITS.md) as always numeric - an InfluxDB field's type is
        # fixed by its first write, so a stray "true"/"false" payload renamed into
        # *Value would poison the field type against every later, real numeric
        # write. Leave it under its original field key instead.
        if isinstance(value, bool):
            return field, value
        return STATE_VALUE_FIELDS.get(field, field), value

    @staticmethod
    def _decode_scalar(raw):
        """
        Cast a bare MQTT payload string to the most specific Python type it matches.

        :param raw: the payload as received (UTF-8 decoded)
        :type raw: str
        :return: bool, int, float, or the original string
        """
        if raw in ("true", "false"):
            return raw == "true"
        try:
            return int(raw)
        except ValueError:
            pass
        try:
            return float(raw)
        except ValueError:
            return raw
