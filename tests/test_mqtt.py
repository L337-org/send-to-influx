"""Unit tests for toinflux.mqtt (MqttDataHandler, the shared MQTT transport)."""

import queue
import threading
from unittest.mock import MagicMock, patch
import pytest

from paho.mqtt import client as mqtt_client
from toinflux.mqtt import MqttDataHandler
from toinflux.exceptions import ConfigError, SourceConnectionError

# Short collection window so the fixed-window loop doesn't slow the test run down.
WINDOW = 0.05


def _mqtt_settings(base, **overrides):
    """Layer a shared mqtt block (and a minimal source block to instantiate with) onto
    the base fixture, without touching the shared conftest fixture itself."""
    settings = {**base}
    settings["mqtt"] = {
        "broker_host": "mqtt.example.com",
        "broker_port": 1883,
        "username": "sendtoinflux",
        "password": "test_password",
        **overrides,
    }
    settings["mqttsource"] = {"db": "mqtt_db", "interval": 300}
    return settings


def _handler(settings):
    """Instantiate MqttDataHandler directly - it's an intermediate parent, never
    registered in get_class(), so tests construct it with any source block present in
    the settings."""
    with patch("toinflux.influx.load_settings") as mock_load_settings:
        mock_load_settings.return_value = settings
        return MqttDataHandler(source="mqttsource")


class _FakeReasonCode:
    """Stands in for paho's CONNACK ReasonCode - just the is_failure/str surface the
    transport uses."""

    def __init__(self, failure):
        self.is_failure = failure

    def __str__(self):
        return "Not authorized" if self.is_failure else "Success"


def _drive_callbacks(mock_client, messages, connack_failure=False, subscribe_result=0):
    """Make the mocked client's loop() fire on_connect (and deliver the given
    (topic, payload-bytes) messages) on its first call, as a real broker would after
    the CONNACK - subsequent loop() calls are no-ops until the window expires."""
    state = {"delivered": False}
    # paho's subscribe() returns (error_code, mid); the transport checks the code.
    mock_client.subscribe.return_value = (subscribe_result, 1)

    def fake_loop(timeout=1.0):
        if not state["delivered"]:
            state["delivered"] = True
            mock_client.on_connect(mock_client, None, None, _FakeReasonCode(connack_failure), None)
            for topic, payload in messages:
                mock_client.on_message(mock_client, None, MagicMock(topic=topic, payload=payload))
        return 0

    mock_client.loop.side_effect = fake_loop


class TestMqttDataHandler:
    """Tests for the generic collect_mqtt_messages transport."""

    def test_collect_returns_messages_received_during_window(self, sample_settings):
        """Messages delivered while the window is open come back as (topic, str) pairs."""
        handler = _handler(_mqtt_settings(sample_settings))
        with patch("toinflux.mqtt.mqtt_client.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            _drive_callbacks(mock_client, [("nuki/2BB28570/state", b"1"), ("nuki/2BB28570/batteryCritical", b"false")])
            result = handler.collect_mqtt_messages("nuki/+/+", WINDOW)
            assert result == [("nuki/2BB28570/state", "1"), ("nuki/2BB28570/batteryCritical", "false")]
            mock_client.subscribe.assert_called_once_with("nuki/+/+")
            mock_client.disconnect.assert_called_once()

    def test_uses_v2_callback_api(self, sample_settings):
        """The client is constructed against paho's v2 callback API, not the removed v1 default."""
        handler = _handler(_mqtt_settings(sample_settings))
        with patch("toinflux.mqtt.mqtt_client.Client") as mock_client_cls:
            _drive_callbacks(mock_client_cls.return_value, [])
            handler.collect_mqtt_messages("nuki/+/+", WINDOW)
            assert mock_client_cls.call_args.kwargs["callback_api_version"] is mqtt_client.CallbackAPIVersion.VERSION2

    def test_connect_failure_raises_source_connection_error(self, sample_settings):
        """A network-level connect failure (OSError family) maps to SourceConnectionError."""
        handler = _handler(_mqtt_settings(sample_settings))
        with patch("toinflux.mqtt.mqtt_client.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.connect.side_effect = ConnectionRefusedError("connection refused")
            with pytest.raises(SourceConnectionError):
                handler.collect_mqtt_messages("nuki/+/+", WINDOW)
            mock_client.disconnect.assert_called_once()

    def test_connack_failure_raises_source_connection_error(self, sample_settings):
        """Bad credentials arrive asynchronously as a failed CONNACK, not an exception -
        the transport must still surface them as SourceConnectionError."""
        handler = _handler(_mqtt_settings(sample_settings))
        with patch("toinflux.mqtt.mqtt_client.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            _drive_callbacks(mock_client, [], connack_failure=True)
            with pytest.raises(SourceConnectionError, match="rejected the connection"):
                handler.collect_mqtt_messages("nuki/+/+", WINDOW)
            mock_client.subscribe.assert_not_called()
            mock_client.disconnect.assert_called_once()

    def test_subscribe_failure_raises_source_connection_error(self, sample_settings):
        """A failed subscribe would otherwise leave the client looping out the window
        and returning an empty list - a transport failure disguised as "no data"."""
        handler = _handler(_mqtt_settings(sample_settings))
        with patch("toinflux.mqtt.mqtt_client.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            _drive_callbacks(mock_client, [], subscribe_result=1)
            with pytest.raises(SourceConnectionError, match="could not subscribe"):
                handler.collect_mqtt_messages("nuki/+/+", WINDOW)
            mock_client.disconnect.assert_called_once()

    def test_empty_window_returns_empty_list(self, sample_settings):
        """A successful connection with nothing published is no data, not an error -
        the caller decides what an empty result means for its source."""
        handler = _handler(_mqtt_settings(sample_settings))
        with patch("toinflux.mqtt.mqtt_client.Client") as mock_client_cls:
            _drive_callbacks(mock_client_cls.return_value, [])
            assert handler.collect_mqtt_messages("nuki/+/+", WINDOW) == []

    def test_credentials_passed_to_client_when_configured(self, sample_settings):
        """mqtt.username/password are applied via username_pw_set before connecting."""
        handler = _handler(_mqtt_settings(sample_settings))
        with patch("toinflux.mqtt.mqtt_client.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            _drive_callbacks(mock_client, [])
            handler.collect_mqtt_messages("nuki/+/+", WINDOW)
            mock_client.username_pw_set.assert_called_once_with("sendtoinflux", "test_password")

    def test_anonymous_when_no_username_configured(self, sample_settings):
        """Without mqtt.username the connection is anonymous - username_pw_set untouched."""
        settings = _mqtt_settings(sample_settings)
        settings["mqtt"]["username"] = ""  # the shipped example's anonymous shape
        settings["mqtt"]["password"] = ""
        handler = _handler(settings)
        with patch("toinflux.mqtt.mqtt_client.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            _drive_callbacks(mock_client, [])
            handler.collect_mqtt_messages("nuki/+/+", WINDOW)
            mock_client.username_pw_set.assert_not_called()

    def test_broker_port_defaults_to_1883(self, sample_settings):
        """mqtt.broker_port is optional and defaults to the standard MQTT port."""
        settings = _mqtt_settings(sample_settings)
        del settings["mqtt"]["broker_port"]
        handler = _handler(settings)
        with patch("toinflux.mqtt.mqtt_client.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            _drive_callbacks(mock_client, [])
            handler.collect_mqtt_messages("nuki/+/+", WINDOW)
            mock_client.connect.assert_called_once_with("mqtt.example.com", 1883)

    def test_no_connack_within_window_raises_source_connection_error(self, sample_settings):
        """TCP connect succeeding but the MQTT handshake never completing (stalled
        network, hung broker) must raise, not return an empty list that masquerades as
        a healthy broker with nothing retained."""
        handler = _handler(_mqtt_settings(sample_settings))
        with patch("toinflux.mqtt.mqtt_client.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.loop.return_value = 0  # network loop runs, but no CONNACK ever arrives
            with pytest.raises(SourceConnectionError, match="handshake"):
                handler.collect_mqtt_messages("nuki/+/+", WINDOW)
            mock_client.disconnect.assert_called_once()

    def test_mid_window_disconnect_stops_collecting_and_returns_partial(self, sample_settings):
        """A connection that dies after the CONNACK stops the collection early (no
        busy-spin through the rest of the window) and returns what already arrived -
        retained state that was delivered is still valid last-known data."""
        handler = _handler(_mqtt_settings(sample_settings))
        with patch("toinflux.mqtt.mqtt_client.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.subscribe.return_value = (0, 1)
            state = {"calls": 0}

            def fake_loop(timeout=1.0):
                state["calls"] += 1
                if state["calls"] == 1:
                    mock_client.on_connect(mock_client, None, None, _FakeReasonCode(False), None)
                    mock_client.on_message(mock_client, None, MagicMock(topic="nuki/A/state", payload=b"1"))
                    return 0
                return 7  # e.g. MQTT_ERR_CONN_LOST

            mock_client.loop.side_effect = fake_loop
            # A deliberately long window: without the early break this would spin
            # for the full 5 seconds instead of stopping at the second loop call.
            result = handler.collect_mqtt_messages("nuki/+/+", 5)
            assert result == [("nuki/A/state", "1")]
            assert state["calls"] == 2
            mock_client.disconnect.assert_called_once()

    def test_non_numeric_timeout_raises_config_error(self, sample_settings):
        """`timeout: "3"` in YAML is a string and would otherwise raise a raw
        TypeError from the deadline arithmetic - which the worker loop would catch
        and retry with backoff forever, instead of failing fast on what is a
        permanent configuration mistake."""
        handler = _handler(_mqtt_settings(sample_settings))
        with patch("toinflux.mqtt.mqtt_client.Client"):
            for bad in ("3", None, True, 0, -1):
                with pytest.raises(ConfigError, match="positive number of seconds"):
                    handler.collect_mqtt_messages("nuki/+/+", bad)

    def test_missing_mqtt_block_raises_config_error(self, sample_settings):
        """No top-level mqtt block is a config-shape problem - fatal ConfigError, not a
        retryable failure, matching how a missing source block behaves."""
        settings = _mqtt_settings(sample_settings)
        del settings["mqtt"]
        handler = _handler(settings)
        with patch("toinflux.mqtt.mqtt_client.Client"):
            with pytest.raises(ConfigError, match="mqtt"):
                handler.collect_mqtt_messages("nuki/+/+", WINDOW)

    def test_malformed_mqtt_block_raises_config_error_at_runtime(self, sample_settings):
        """The transport re-checks the block rather than trusting validate_settings():
        load_settings() only validates *configured* sources, so a one-off
        `--source nuki` on an install without nuki in sources: arrives here
        unvalidated - a scalar mqtt block must be a ConfigError, not AttributeError."""
        settings = _mqtt_settings(sample_settings)
        settings["mqtt"] = "just-a-hostname-string"
        handler = _handler(settings)
        with patch("toinflux.mqtt.mqtt_client.Client"):
            with pytest.raises(ConfigError, match="mqtt must be a mapping"):
                handler.collect_mqtt_messages("nuki/+/+", WINDOW)

    def test_invalid_broker_port_raises_config_error_at_runtime(self, sample_settings):
        """Same for a non-integer/out-of-range port, which would otherwise surface as
        a TypeError from deep inside the client."""
        settings = _mqtt_settings(sample_settings, broker_port="1883")
        handler = _handler(settings)
        with patch("toinflux.mqtt.mqtt_client.Client"):
            with pytest.raises(ConfigError, match="broker_port"):
                handler.collect_mqtt_messages("nuki/+/+", WINDOW)

    def test_missing_broker_host_raises_config_error(self, sample_settings):
        """An mqtt block without broker_host is equally fatal, with a specific message."""
        settings = _mqtt_settings(sample_settings)
        del settings["mqtt"]["broker_host"]
        handler = _handler(settings)
        with patch("toinflux.mqtt.mqtt_client.Client"):
            with pytest.raises(ConfigError, match="broker_host"):
                handler.collect_mqtt_messages("nuki/+/+", WINDOW)

    def test_undecodable_payload_bytes_are_replaced_not_fatal(self, sample_settings):
        """A payload that isn't valid UTF-8 is decoded with replacement characters
        rather than raising - one odd message must not kill the whole collection."""
        handler = _handler(_mqtt_settings(sample_settings))
        with patch("toinflux.mqtt.mqtt_client.Client") as mock_client_cls:
            _drive_callbacks(mock_client_cls.return_value, [("nuki/2BB28570/name", b"\xff\xfe")])
            result = handler.collect_mqtt_messages("nuki/+/+", WINDOW)
            assert result[0][0] == "nuki/2BB28570/name"
            assert "�" in result[0][1]


def _drive_stream(mock_client, messages, connack_failure=False, subscribe_result=0, connects=1):
    """Make the mocked client's loop_start() synchronously fire on_connect (``connects``
    times, to exercise reconnect re-subscription) and enqueue the given (topic,
    payload-bytes) messages via the client's on_message callback, as a real broker would
    after the CONNACK. Because the mock is synchronous, this all happens before
    stream_mqtt_messages awaits the handshake, so the await returns immediately and the
    messages are already queued for the drain loop to process."""
    mock_client.subscribe.return_value = (subscribe_result, 1)

    def fake_loop_start():
        for _ in range(connects):
            mock_client.on_connect(mock_client, None, None, _FakeReasonCode(connack_failure), None)
        for topic, payload in messages:
            mock_client.on_message(mock_client, None, MagicMock(topic=topic, payload=payload))

    mock_client.loop_start.side_effect = fake_loop_start


def _stop_after_iterations(n):
    """A stand-in for the should_stop Event whose is_set() returns False ``n`` times (one
    per drain-loop iteration) then True, so _run_stream_loop runs a known number of
    iterations without any real waiting or a background thread."""
    should_stop = MagicMock()
    should_stop.is_set.side_effect = [False] * n + [True]
    return should_stop


class TestStreamMqttMessages:
    """Connection/lifecycle tests for stream_mqtt_messages; the drain-loop behaviour is
    covered separately in TestRunStreamLoop."""

    def test_delivers_each_message_to_on_message_callback(self, sample_settings):
        """A message arriving over the held-open subscription is passed to the caller's
        on_message callback with its payload UTF-8 decoded - the interrupt path."""
        handler = _handler(_mqtt_settings(sample_settings))
        received = []
        with patch("toinflux.mqtt.mqtt_client.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            _drive_stream(mock_client, [("nuki/A/doorsensorState", b"3"), ("nuki/A/state", b"1")])
            handler.stream_mqtt_messages(
                "nuki/+/+",
                on_message=lambda t, p: received.append((t, p)),
                periodic=lambda: None,
                interval=300,
                should_stop=_stop_after_iterations(2),
            )
            assert received == [("nuki/A/doorsensorState", "3"), ("nuki/A/state", "1")]
            mock_client.subscribe.assert_called_with("nuki/+/+")
            mock_client.loop_stop.assert_called_once()
            mock_client.disconnect.assert_called_once()

    def test_message_callback_runs_off_the_network_thread(self, sample_settings):
        """The paho on_message callback only enqueues - it must not call the caller's
        on_message directly, so a slow write can't block paho's network loop. Firing the
        client callback delivers nothing to the caller until the drain loop runs."""
        handler = _handler(_mqtt_settings(sample_settings))
        received = []
        with patch("toinflux.mqtt.mqtt_client.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.subscribe.return_value = (0, 1)

            def fake_loop_start():
                mock_client.on_connect(mock_client, None, None, _FakeReasonCode(False), None)
                mock_client.on_message(mock_client, None, MagicMock(topic="nuki/A/state", payload=b"1"))
                # only the queue has been fed at this point - the caller hasn't been called
                assert received == []

            mock_client.loop_start.side_effect = fake_loop_start
            handler.stream_mqtt_messages(
                "nuki/+/+",
                on_message=lambda t, p: received.append((t, p)),
                periodic=lambda: None,
                interval=300,
                should_stop=_stop_after_iterations(1),
            )
            assert received == [("nuki/A/state", "1")]  # drained on the worker thread

    def test_resubscribes_on_every_connect(self, sample_settings):
        """on_connect re-subscribes on each (re)connect - paho drops subscriptions on a
        reconnect, and re-subscribing is what makes a dropped connection self-heal (and,
        for retained topics, redeliver current state)."""
        handler = _handler(_mqtt_settings(sample_settings))
        with patch("toinflux.mqtt.mqtt_client.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            _drive_stream(mock_client, [], connects=3)
            handler.stream_mqtt_messages(
                "nuki/+/+",
                on_message=lambda t, p: None,
                periodic=lambda: None,
                interval=300,
                should_stop=_stop_after_iterations(0),
            )
            assert mock_client.subscribe.call_count == 3

    def test_enables_automatic_reconnect(self, sample_settings):
        """Automatic reconnect backoff is configured so a mid-stream drop self-heals."""
        handler = _handler(_mqtt_settings(sample_settings))
        with patch("toinflux.mqtt.mqtt_client.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            _drive_stream(mock_client, [])
            handler.stream_mqtt_messages(
                "nuki/+/+",
                on_message=lambda t, p: None,
                periodic=lambda: None,
                interval=300,
                should_stop=_stop_after_iterations(0),
            )
            mock_client.reconnect_delay_set.assert_called_once()

    def test_connect_failure_raises_source_connection_error(self, sample_settings):
        """A network-level connect failure maps to SourceConnectionError before the
        background loop is ever started."""
        handler = _handler(_mqtt_settings(sample_settings))
        with patch("toinflux.mqtt.mqtt_client.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.connect.side_effect = ConnectionRefusedError("connection refused")
            with pytest.raises(SourceConnectionError):
                handler.stream_mqtt_messages(
                    "nuki/+/+",
                    on_message=lambda t, p: None,
                    periodic=lambda: None,
                    interval=300,
                    should_stop=_stop_after_iterations(0),
                )
            mock_client.loop_start.assert_not_called()

    def test_connack_failure_raises_source_connection_error(self, sample_settings):
        """A failed CONNACK (e.g. bad credentials) at startup surfaces as
        SourceConnectionError, so the worker retries it with backoff, and the background
        loop is torn down cleanly."""
        handler = _handler(_mqtt_settings(sample_settings))
        with patch("toinflux.mqtt.mqtt_client.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            _drive_stream(mock_client, [], connack_failure=True)
            with pytest.raises(SourceConnectionError, match="rejected the connection"):
                handler.stream_mqtt_messages(
                    "nuki/+/+",
                    on_message=lambda t, p: None,
                    periodic=lambda: None,
                    interval=300,
                    should_stop=_stop_after_iterations(0),
                )
            mock_client.subscribe.assert_not_called()
            mock_client.loop_stop.assert_called_once()
            mock_client.disconnect.assert_called_once()

    def test_subscribe_failure_raises_source_connection_error(self, sample_settings):
        """A failed subscribe at startup raises rather than silently streaming nothing."""
        handler = _handler(_mqtt_settings(sample_settings))
        with patch("toinflux.mqtt.mqtt_client.Client") as mock_client_cls:
            _drive_stream(mock_client_cls.return_value, [], subscribe_result=1)
            with pytest.raises(SourceConnectionError, match="could not subscribe"):
                handler.stream_mqtt_messages(
                    "nuki/+/+",
                    on_message=lambda t, p: None,
                    periodic=lambda: None,
                    interval=300,
                    should_stop=_stop_after_iterations(0),
                )

    def test_no_connack_within_timeout_raises_source_connection_error(self, sample_settings):
        """TCP up but the MQTT handshake never completing raises after the connect
        timeout, rather than blocking startup forever."""
        handler = _handler(_mqtt_settings(sample_settings))
        with (
            patch("toinflux.mqtt.mqtt_client.Client") as mock_client_cls,
            patch("toinflux.mqtt.STREAM_CONNECT_TIMEOUT", 0.05),
        ):
            mock_client = mock_client_cls.return_value
            mock_client.loop_start.side_effect = lambda: None  # loop runs, no CONNACK ever
            with pytest.raises(SourceConnectionError, match="handshake"):
                handler.stream_mqtt_messages(
                    "nuki/+/+",
                    on_message=lambda t, p: None,
                    periodic=lambda: None,
                    interval=300,
                    should_stop=_stop_after_iterations(0),
                )
            mock_client.loop_stop.assert_called_once()
            mock_client.disconnect.assert_called_once()

    def test_disconnect_before_loop_stop_on_shutdown(self, sample_settings):
        """Clean shutdown disconnects (queuing the DISCONNECT for the still-running
        network thread) before stopping the loop."""
        handler = _handler(_mqtt_settings(sample_settings))
        order = []
        with patch("toinflux.mqtt.mqtt_client.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            _drive_stream(mock_client, [])
            mock_client.disconnect.side_effect = lambda: order.append("disconnect")
            mock_client.loop_stop.side_effect = lambda: order.append("loop_stop")
            handler.stream_mqtt_messages(
                "nuki/+/+",
                on_message=lambda t, p: None,
                periodic=lambda: None,
                interval=300,
                should_stop=_stop_after_iterations(0),
            )
            assert order == ["disconnect", "loop_stop"]

    def test_invalid_interval_raises_config_error(self, sample_settings):
        """A non-positive/non-numeric snapshot interval is a fatal config-shape problem,
        not a retryable failure."""
        handler = _handler(_mqtt_settings(sample_settings))
        with patch("toinflux.mqtt.mqtt_client.Client"):
            for bad in ("300", None, True, 0, -1):
                with pytest.raises(ConfigError, match="positive number of seconds"):
                    handler.stream_mqtt_messages(
                        "nuki/+/+",
                        on_message=lambda t, p: None,
                        periodic=lambda: None,
                        interval=bad,
                        should_stop=_stop_after_iterations(0),
                    )

    def test_missing_mqtt_block_raises_config_error(self, sample_settings):
        """No mqtt block is fatal here too, before any connection is attempted."""
        settings = _mqtt_settings(sample_settings)
        del settings["mqtt"]
        handler = _handler(settings)
        with patch("toinflux.mqtt.mqtt_client.Client"):
            with pytest.raises(ConfigError, match="mqtt"):
                handler.stream_mqtt_messages(
                    "nuki/+/+",
                    on_message=lambda t, p: None,
                    periodic=lambda: None,
                    interval=300,
                    should_stop=_stop_after_iterations(0),
                )

    def test_uses_v2_callback_api(self, sample_settings):
        """The streaming client is constructed against paho's v2 callback API."""
        handler = _handler(_mqtt_settings(sample_settings))
        with patch("toinflux.mqtt.mqtt_client.Client") as mock_client_cls:
            _drive_stream(mock_client_cls.return_value, [])
            handler.stream_mqtt_messages(
                "nuki/+/+",
                on_message=lambda t, p: None,
                periodic=lambda: None,
                interval=300,
                should_stop=_stop_after_iterations(0),
            )
            assert mock_client_cls.call_args.kwargs["callback_api_version"] is mqtt_client.CallbackAPIVersion.VERSION2


class TestRunStreamLoop:
    """Tests for the single-threaded drain loop that processes queued messages and runs
    the periodic snapshot, with no network layer involved."""

    def test_dispatches_queued_messages_in_order(self, sample_settings):
        """Queued (topic, payload) pairs are handed to on_message in FIFO order."""
        handler = _handler(_mqtt_settings(sample_settings))
        message_queue = queue.Queue()
        message_queue.put(("nuki/A/state", "1"))
        message_queue.put(("nuki/A/doorsensorState", "3"))
        received = []
        handler._run_stream_loop(
            message_queue,
            on_message=lambda t, p: received.append((t, p)),
            periodic=lambda: None,
            interval=300,
            should_stop=_stop_after_iterations(2),
        )
        assert received == [("nuki/A/state", "1"), ("nuki/A/doorsensorState", "3")]

    def test_runs_periodic_when_the_interval_elapses(self, sample_settings):
        """With nothing queued, the loop still fires the periodic snapshot once the
        interval passes."""
        handler = _handler(_mqtt_settings(sample_settings))
        ticks = []
        should_stop = threading.Event()

        def periodic():
            ticks.append(1)
            should_stop.set()  # stop after the first snapshot

        handler._run_stream_loop(
            queue.Queue(),
            on_message=lambda t, p: None,
            periodic=periodic,
            interval=0.01,
            should_stop=should_stop,
        )
        assert ticks == [1]

    def test_failing_on_message_is_logged_and_swallowed(self, sample_settings):
        """A message whose handler raises is swallowed - the loop survives (if the error
        propagated, this test would error out instead of asserting)."""
        handler = _handler(_mqtt_settings(sample_settings))
        message_queue = queue.Queue()
        message_queue.put(("nuki/A/state", "1"))
        processed = []

        def boom(topic, payload):
            processed.append((topic, payload))
            raise ValueError("bad message")

        handler._run_stream_loop(
            message_queue,
            on_message=boom,
            periodic=lambda: None,
            interval=300,
            should_stop=_stop_after_iterations(1),
        )
        assert processed == [("nuki/A/state", "1")]

    def test_periodic_exception_propagates(self, sample_settings):
        """Unlike on_message, a periodic failure is not swallowed - it propagates so the
        caller's error handling and the worker's backoff apply."""
        handler = _handler(_mqtt_settings(sample_settings))

        def boom():
            raise RuntimeError("snapshot failed")

        with pytest.raises(RuntimeError, match="snapshot failed"):
            handler._run_stream_loop(
                queue.Queue(),
                on_message=lambda t, p: None,
                periodic=boom,
                interval=0.01,
                should_stop=threading.Event(),
            )
