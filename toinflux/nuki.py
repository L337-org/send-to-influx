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

# Numeric-code-to-label tables from the Nuki MQTT API spec (v1.6). Unlike the Bridge
# HTTP API, MQTT publishes only the numeric codes - the human-readable labels have to
# be resolved here. An unrecognised code is written through as its raw number rather
# than dropped: a new code is more likely a future firmware addition than bad data.
LOCK_STATE_NAMES = {
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

DOORSENSOR_STATE_NAMES = {
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

    def get_data(self):
        """
        Get the current state of every Nuki device from the MQTT broker

        :return: data
        :rtype: dict
        """
        self.influx_header = "nuki "
        self.data = self.parse_nuki_data()
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
        for device_id, fields in devices.items():
            prefix = fields.pop("name", device_id).replace(" ", "_")
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
            logging.warning("No Nuki device state received from the MQTT broker")
        return data

    @staticmethod
    def _decode_field(field, raw):
        """
        Decode one state topic's payload into an InfluxDB field key and value.

        The numeric ``state``/``doorsensorState`` codes are resolved to their
        human-readable labels (as ``stateName``/``doorsensorStateName``); an
        unrecognised code keeps the original field key and its raw numeric value.
        Everything else is cast by shape: true/false to bool, numeric strings to
        int/float, anything else left as a string.

        :param field: the topic's field name (last topic segment)
        :type field: str
        :param raw: the payload as received (UTF-8 decoded)
        :type raw: str
        :return: (field key, decoded value)
        :rtype: tuple
        """
        value = Nuki._decode_scalar(raw)
        if field == "state" and value in LOCK_STATE_NAMES:
            return "stateName", LOCK_STATE_NAMES[value]
        if field == "doorsensorState" and value in DOORSENSOR_STATE_NAMES:
            return "doorsensorStateName", DOORSENSOR_STATE_NAMES[value]
        return field, value

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
