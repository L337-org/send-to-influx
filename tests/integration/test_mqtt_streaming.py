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
    """Publish one message to the broker and block until it's actually sent."""
    pub = _new_client()
    pub.connect(BROKER_HOST, BROKER_PORT)
    pub.loop_start()
    try:
        pub.publish(topic, payload, qos=1, retain=retain).wait_for_publish(timeout=5)
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
    # Seed the retained name so the field key is prefixed with it (delivered on subscribe).
    _publish(f"nuki/{DEVICE_ID}/name", "Integration Lock", retain=True)

    stop = threading.Event()
    args = SimpleNamespace(print=False, dump=False, settings=None)
    stream = threading.Thread(target=sendtoinflux.stream_source_data, args=("nuki", args, handler, stop), daemon=True)
    stream.start()
    try:
        # Let the subscription establish and the retained name be delivered/remembered.
        time.sleep(2)
        posts.clear()  # ignore anything redelivered during the initial subscribe
        # Retained, as real Nuki publishes its state topics - so this exercises the
        # retained delivery path, not just a transient live message.
        _publish(f"nuki/{DEVICE_ID}/doorsensorState", "3", retain=True)
        body = _wait_for_write(posts, "Integration_Lock_doorsensorStateValue=3")
        assert body.startswith("nuki,host=")
    finally:
        stop.set()
        stream.join(timeout=WRITE_TIMEOUT)
    assert not stream.is_alive(), "stream thread did not stop after should_stop was set"
