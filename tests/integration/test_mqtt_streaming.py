"""Integration test for the MQTT streaming path against a real broker.

Unlike the unit tests (which mock the paho client), this exercises the whole real path:
a real paho subscription held open by ``MqttDataHandler.stream_mqtt_messages``, a real
message published to a real broker, decoded by ``Nuki.decode_stream_message`` and written
- immediately, not on the periodic snapshot - through ``_StreamSink``/``send_data``. Only
InfluxDB is stubbed, at the ``requests`` layer, so no InfluxDB instance is needed.

Marked ``integration`` (excluded from the default ``pytest`` run - see pyproject) and run
with ``pytest -m integration``. Skips cleanly when no broker is reachable, so an explicit
run without one is a skip rather than a failure. Point it at a broker with
``MQTT_TEST_BROKER_HOST``/``MQTT_TEST_BROKER_PORT`` (default ``localhost:1883``); the broker
must allow anonymous access.
"""

import os
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from paho.mqtt import client as mqtt_client

import sendtoinflux
from toinflux.nuki import Nuki

pytestmark = pytest.mark.integration

BROKER_HOST = os.environ.get("MQTT_TEST_BROKER_HOST", "localhost")
BROKER_PORT = int(os.environ.get("MQTT_TEST_BROKER_PORT", "1883"))
DEVICE_ID = "INTEG0001"
# Well under the handler's interval below, so a write appearing this quickly can only be
# the immediate per-message path, never the periodic snapshot.
WRITE_TIMEOUT = 10


def _broker_reachable():
    try:
        with socket.create_connection((BROKER_HOST, BROKER_PORT), timeout=2):
            return True
    except OSError:
        return False


def _new_client():
    return mqtt_client.Client(callback_api_version=mqtt_client.CallbackAPIVersion.VERSION2)


def _publish(topic, payload, retain=True):
    """Publish one message to the broker and block until it's confirmed sent.

    Waits for the CONNACK (on_connect) before publishing, so the test is deterministic on
    a slow runner (paho completes the handshake on the network-loop thread) and a rejected
    connection fails here with a clear message rather than as a later "no InfluxDB write".
    ``wait_for_publish`` doesn't raise on timeout, so ``is_published()`` is checked too.
    """
    pub = _new_client()
    connected = threading.Event()
    outcome = {}

    def on_connect(client, userdata, connect_flags, reason_code, properties):
        outcome["reason_code"] = reason_code
        connected.set()

    pub.on_connect = on_connect
    pub.connect(BROKER_HOST, BROKER_PORT)
    pub.loop_start()
    try:
        assert connected.wait(timeout=5), f"publisher did not connect to {BROKER_HOST}:{BROKER_PORT} within 5s"
        assert not outcome["reason_code"].is_failure, f"publisher connection rejected: {outcome['reason_code']}"
        info = pub.publish(topic, payload, qos=1, retain=retain)
        info.wait_for_publish(timeout=5)
        assert info.is_published(), f"publish to {topic!r} was not confirmed within 5s"
    finally:
        pub.loop_stop()
        pub.disconnect()


def _clear_retained(*fields):
    """Delete a device's retained topics (empty retained payload) so a rerun starts clean."""
    for field in fields:
        _publish(f"nuki/{DEVICE_ID}/{field}", "", retain=True)


@pytest.fixture
def broker():
    if not _broker_reachable():
        pytest.skip(f"no MQTT broker at {BROKER_HOST}:{BROKER_PORT} (set MQTT_TEST_BROKER_HOST/PORT)")
    _clear_retained("name", "state", "doorsensorState")
    yield
    _clear_retained("name", "state", "doorsensorState")


@pytest.fixture
def streaming_nuki(broker, sample_settings):
    """A real Nuki handler pointed at the test broker, with InfluxDB stubbed at the
    requests layer so writes are captured instead of sent. Returns (handler, posts) where
    posts is the list of line-protocol bodies that would have gone to InfluxDB."""
    settings = {**sample_settings}
    settings["mqtt"] = {"broker_host": BROKER_HOST, "broker_port": BROKER_PORT}
    # A long interval so the periodic snapshot can't fire during the test - any write we
    # observe must therefore be the immediate per-message path.
    settings["nuki"] = {"db": "nuki_db", "interval": 3600, "timeout": 3}
    with patch("toinflux.influx.load_settings", return_value=settings):
        handler = Nuki(source="nuki")

    posts = []
    ok_response = MagicMock(status_code=204, text="")
    ok_response.raise_for_status = MagicMock()
    handler.session = MagicMock()
    handler.session.post.side_effect = lambda url, data=None, **kwargs: (posts.append(data), ok_response)[1]
    return handler, posts


def _wait_for_write(posts, needle):
    deadline = time.monotonic() + WRITE_TIMEOUT
    while time.monotonic() < deadline:
        for body in list(posts):
            if body and needle in body:
                return body
        time.sleep(0.1)
    raise AssertionError(f"no InfluxDB write containing {needle!r} within {WRITE_TIMEOUT}s; captured: {posts}")


def test_door_state_change_is_written_immediately(streaming_nuki):
    """A doorsensorState message published to the broker is decoded and written to InfluxDB
    within seconds - via the persistent subscription, not the (1h-away) periodic snapshot."""
    handler, posts = streaming_nuki
    # Seed the retained name so the point is tagged with it (delivered on subscribe).
    _publish(f"nuki/{DEVICE_ID}/name", "Integration Lock", retain=True)

    stop = threading.Event()
    args = SimpleNamespace(print=False, dump=False, settings=None)
    stream = threading.Thread(target=sendtoinflux.stream_source_data, args=("nuki", args, handler, stop), daemon=True)
    stream.start()
    try:
        # Actively wait until the retained name has been received and remembered, rather
        # than sleeping a fixed guess (flaky on a slow/loaded runner): its arrival proves
        # both that the subscription is established and that the lock label the assertion
        # below depends on (device=Integration_Lock) is in place.
        deadline = time.monotonic() + WRITE_TIMEOUT
        while time.monotonic() < deadline and handler._device_names.get(DEVICE_ID) != "Integration Lock":
            time.sleep(0.05)
        assert (
            handler._device_names.get(DEVICE_ID) == "Integration Lock"
        ), "handler did not receive the retained device name within the timeout"
        posts.clear()  # ignore anything redelivered during the initial subscribe
        # Retained, as real Nuki publishes its state topics - so this exercises the
        # retained delivery path, not just a transient live message.
        _publish(f"nuki/{DEVICE_ID}/doorsensorState", "3", retain=True)
        body = _wait_for_write(posts, "doorsensorStateValue=3")
        # Since 5.3 the lock is a tag and the field key is bare - it used to be
        # "Integration_Lock_doorsensorStateValue" on a point tagged with the broker host.
        # Asserted as the whole header rather than a substring, so neither half can regress
        # silently: a returning field-key prefix and a returning broker tag both fail here.
        assert body.startswith("nuki,device=Integration_Lock "), body
        assert "Integration_Lock_doorsensorStateValue" not in body, body
        assert ",host=" not in body, f"the broker host tag was dropped in 5.3: {body}"
    finally:
        stop.set()
        stream.join(timeout=WRITE_TIMEOUT)
    assert not stream.is_alive(), "stream thread did not stop after should_stop was set"
