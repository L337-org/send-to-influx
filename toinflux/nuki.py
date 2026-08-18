"""Functions to get data from Nuki smart locks via MQTT and format it for InfluxDB"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT License"
__version__ = "1.0"

import logging
import time
from toinflux.influx import InfluxWriteError, escape_key_or_tag_value
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


def _is_per_device(payload):
    """Whether a payload is this source's ``{device: {field: value}}`` shape.

    Decided by shape rather than by which argument was passed, because both are legitimate:
    the streaming path passes per-device data explicitly, and ``send_heartbeat()`` passes a
    flat ``{field: value}`` point with its own header already set. Every value being a mapping
    is what separates them - a lock always carries a dict of fields, and a field never does.

    Deliberately strict: anything else, including a mixed payload no code path produces, goes
    to the base implementation, which is the conservative direction (it honours the header the
    caller set instead of overwriting it with a lock's).

    :param payload: the data handed to ``send_data()``
    :return: True if it should be written as one point per lock
    :rtype: bool
    """
    if not payload or not isinstance(payload, dict):
        return False
    return all(isinstance(fields, dict) for fields in payload.values())


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
    # Each lock is its own point, tagged with the lock. Before this, every lock's state was
    # flattened into one point per cycle with keys like Front_Door_stateValue, so the device
    # was encoded in the field key and could not be queried as a dimension at all.
    MCP_INSTANCE_TAG = "device"
    # One MQTT subscription receives every lock's retained state, so a single live read
    # covers every producer - unlike Hue, where each bridge has its own handler, or Speedtest,
    # where a live read can only speak for the local host. That is what lets the live
    # current-state path report per lock rather than only the handler's own.
    MCP_LIVE_STATE_COVERS_ALL_INSTANCES = True
    # Field keys are bare now (stateValue, not Front_Door_stateValue), so the metadata keys
    # on the field name directly with no suffix matching.
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
        Get the current state of every Nuki device from the MQTT broker.

        Returns ``{device: {field: value}}`` - one entry per lock - rather than the single
        flattened dict this used to return. ``send_data()`` writes one point per entry.

        :return: per-device state
        :rtype: dict
        """
        self.data = self.parse_nuki_data()
        # No header set here: there is one per device now, built by send_data(). The broker
        # host tag is gone - every lock arrives via the one broker, so it never distinguished
        # anything, and changing broker should not change the data.
        return self.data

    def send_data(self, data=None, timestamp=None, use_buffer=True):
        """
        Write one point per lock, rather than one point carrying every lock's fields.

        ``self.data`` is ``{device: {field: value}}``, so this walks it and delegates each
        entry to the base implementation with that lock's header swapped in - the same
        header-swap idiom ``send_heartbeat()`` uses, which keeps buffering, retry and the
        InfluxWriteError contract exactly as they are rather than reimplementing them.

        Every lock in one cycle shares a single timestamp. Letting each call default
        independently would scatter one snapshot across a second or two, so a query asking
        "what was the state at time T" could see one lock's reading and not another's.

        A failure on one lock does not stop the rest: each is attempted, and one
        InfluxWriteError is raised at the end if any failed, so the worker still backs off.
        That covers a lock whose *name* cannot be used as well as one whose write fails - see
        the loop below, where building the header is deliberately inside the guarded block.
        Points are idempotent - same measurement, tag set and timestamp overwrite - so the
        retry re-writing a lock that already succeeded is harmless.

        **A flat ``{field: value}`` payload is passed straight to the base**, because this
        override must not capture every caller of ``send_data()``. ``send_heartbeat()`` sets its
        own ``collector_status`` header and passes a flat dict, and the streaming path passes
        per-device data explicitly - so "was ``data`` given?" cannot tell them apart, and the
        shape is what actually distinguishes them. Getting this wrong meant the heartbeat's
        ``ok``/``consecutive_failures`` were treated as *lock names* whose scalar values were
        then skipped as non-dicts: Nuki wrote no heartbeat at all, silently, which is precisely
        the "silent gap" the heartbeat exists to prevent.

        :param data: per-device ``{device: {field: value}}`` data, or a flat
            ``{field: value}`` point for the caller's own header; defaults to ``self.data``
        :type data: dict or None
        :param timestamp: unix epoch seconds for every point in this snapshot
        :type timestamp: int or None
        :param use_buffer: as the base implementation
        :type use_buffer: bool
        :return: None
        :raises InfluxWriteError: if any lock's write failed
        """
        per_device = self.data if data is None else data
        if not _is_per_device(per_device):
            # Either nothing collected - hand it to the base so the empty-reading logging and
            # the buffer flush still happen exactly as for any other source - or a flat point
            # from a caller that set its own header, which is the base's contract, not ours.
            return super().send_data(data=per_device, timestamp=timestamp, use_buffer=use_buffer)
        if timestamp is None:
            timestamp = self.timestamp if self.timestamp is not None else int(time.time())
        original_header = self.influx_header
        failures = []
        try:
            for index, (label, fields) in enumerate(sorted(per_device.items())):
                try:
                    # Header construction is inside the try because it can fail: a lock name
                    # carrying a newline cannot be escaped (a newline is what separates points)
                    # and escape_key_or_tag_value raises. Built outside, that one lock aborted
                    # the loop and every lock sorting after it went unwritten - breaking the
                    # promise two lines up. Lock names come from the retained MQTT `name` topic,
                    # so they are external input, not config.
                    self.influx_header = f"nuki,device={escape_key_or_tag_value(label)} "
                    # Flush the shared backlog on the first lock only. The buffer is per
                    # *worker*, so flushing once per lock charged the head buffered point one
                    # rejection per lock - a five-lock install burned all of
                    # MAX_POINT_REJECTIONS in one cycle and dropped the backlog after a single
                    # cycle instead of five, defeating the guarantee that a middlebox answering
                    # 4xx for a down InfluxDB cannot mass-discard it. Every lock still buffers
                    # its own point on failure; only the flush is done once.
                    super().send_data(data=fields, timestamp=timestamp, use_buffer=use_buffer, flush=index == 0)
                except InfluxWriteError as exc:
                    # label!r, never the raw label. A lock name comes from the retained MQTT
                    # `name` topic, and one containing a newline turned this message into two
                    # log lines - the worker loop logs it as "Source '%s' failed: %s", so a
                    # forged line with its own timestamp and ERROR level appeared in the journal
                    # as though the daemon had written it. The same text reaches an MCP client.
                    # escape_key_or_tag_value's own message was already safe for this reason;
                    # this prefix was not.
                    failures.append(f"{label!r}: {exc}")
        finally:
            self.influx_header = original_header
        if failures:
            raise InfluxWriteError("; ".join(failures))
        return None

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
            label = self._device_label(fields.pop("name", ""), device_id)
            if label in data:
                # %r, not '%s': a lock name is external input and a newline in it would
                # otherwise split this warning into two journal lines - see send_data().
                logging.warning(
                    "Duplicate Nuki device name %r - one lock's readings will overwrite the"
                    " other's; give each lock a distinct name in the Nuki app",
                    label,
                )
            decoded = {}
            for field, raw in fields.items():
                key, value = self._decode_field(field, raw)
                decoded[key] = value
            data[label] = decoded
        if not data:
            # DEBUG, not WARNING: send_data()'s central _log_missing_data path already
            # warns once per empty cycle - a second warning here would just duplicate it.
            logging.debug("No Nuki device state received from the MQTT broker")
        return data

    def decode_stream_message(self, topic, payload):
        """
        Decode one streamed Nuki message into a single InfluxDB field (the interrupt path).

        The event-driven counterpart to parse_nuki_data's per-topic handling: a ``name``
        topic is remembered as that device's label and produces no point of its own; any
        other known state topic is decoded via ``_decode_field`` and returned in the same
        ``{device: {field: value}}`` shape the snapshot path uses, so one write path serves
        both. Control/event topics (lockAction, ...) and malformed topics are ignored.

        Returning the same shape as the snapshot is what stopped these two paths drifting:
        they previously agreed only by both happening to build the same ``prefix_field``
        string, in two separate places.

        The label falls back to the device ID until a ``name`` message has been seen, exactly
        as parse_nuki_data falls back for a device with no name. Because Nuki publishes
        ``name`` retained, it is redelivered on every (re)subscribe ahead of the state topics,
        so in practice the fallback is only ever hit for a device with no name set at all.

        :param topic: the message's MQTT topic (e.g. ``nuki/2BB28570/state``)
        :type topic: str
        :param payload: the payload as received (UTF-8 decoded)
        :type payload: str
        :return: ``{device: {field: value}}`` for the one field this message carries, or None
            to ignore the message (a control/event/malformed topic, or a ``name`` update
            consumed as a label)
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
        label = self._device_label(self._device_names.get(device_id, ""), device_id)
        key, value = self._decode_field(field, payload)
        return {label: {key: value}}

    @staticmethod
    def _device_label(name, device_id):
        """
        The ``device`` tag value for a lock: its Nuki-app name with spaces underscored, or the
        device ID when the name is blank or absent.

        **The underscores stay, even though a tag value does not need them.** They were
        originally there because the name was part of a field key. The migration for existing
        data can only recover the underscored form - the old key was ``Front_Door_stateValue``,
        so the original spaces are gone for good - and if the collector wrote ``Front Door``
        while migrated history said ``Front_Door``, every lock would end up with two series
        that never join. Keeping them is what makes the migrated history usable.

        A blank or whitespace name falls back to the device ID, as an empty label would make
        every unnamed lock the same series.

        :param name: the device's Nuki-app name (may be blank/whitespace)
        :type name: str
        :param device_id: the device's ID, used as the fallback
        :type device_id: str
        :return: the tag value identifying this lock
        :rtype: str
        """
        return (name.strip() or device_id).replace(" ", "_")

    def _remember_device_name(self, device_id, name):
        """
        Record a device's name for use as its streaming label.

        Warns if the name resolves to a label already claimed by a *different* device - two
        locks sharing a Nuki-app name now share one series, so their readings interleave
        rather than their field keys colliding, but it is the same ambiguity and the same
        remedy. The snapshot path warns on the condition per cycle; this surfaces it on the
        streaming path too, when the name is set, rather than silently. A device re-sending
        its own retained name (e.g. on reconnect) is not a collision.

        :param device_id: the device the name belongs to
        :type device_id: str
        :param name: the name payload as received (UTF-8 decoded)
        :type name: str
        :return: None
        """
        label = self._device_label(name, device_id)
        for other_id, other_name in self._device_names.items():
            if other_id != device_id and self._device_label(other_name, other_id) == label:
                # %r for the same reason as the snapshot path above.
                logging.warning(
                    "Duplicate Nuki device name %r - devices %s and %s share one series, so their"
                    " readings will interleave; give each lock a distinct name in the Nuki app",
                    label,
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
