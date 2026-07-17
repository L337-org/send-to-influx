"""Shared MQTT transport for data handlers whose source publishes to an MQTT broker"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT License"
__version__ = "1.0"

import logging
import time
from paho.mqtt import client as mqtt_client
from toinflux.influx import DataHandler
from toinflux.exceptions import ConfigError, SourceConnectionError

# How long each call into paho's network loop blocks waiting for traffic. Small enough
# that the collection window's deadline (and a failed-CONNACK abort) is honoured
# promptly, large enough not to busy-spin.
LOOP_INTERVAL = 0.5


class MqttDataHandler(DataHandler):
    """
    Intermediate parent class for data handlers that collect from an MQTT broker.

    Owns the generic transport only - connect, subscribe, collect for a fixed window,
    disconnect - the same way MyEnergi is a shared parent holding common API auth for
    Zappi/Eddi/Harvi. Everything vendor-specific (topic naming, payload decoding, how
    messages map to InfluxDB fields) stays in each child class, since another vendor's
    MQTT usage may share none of those conventions.

    Broker connection settings come from the shared top-level ``mqtt`` block of
    settings.yaml (``broker_host``, optional ``broker_port``/``username``/``password``),
    mirroring how the shared ``influx`` block holds the write-side connection settings -
    the broker (and its credential) is per-install infrastructure, not per-source.

    Not a selectable source itself: like DataHandler, it has no get_data() and is never
    registered in get_class().
    """

    def collect_mqtt_messages(self, topic_filter, timeout):
        """
        Connect to the configured MQTT broker, subscribe, and collect messages.

        Runs the client's network loop for ``timeout`` seconds, then disconnects and
        returns whatever arrived. Sources whose brokers hold retained messages (e.g.
        Nuki) receive the full last-known state immediately on subscribing, which is
        what makes this fixed-window connect-per-poll model equivalent to an HTTP GET.

        Subscribing happens inside the on_connect callback, not straight after
        ``connect()`` - a subscription issued before the CONNACK completes can be
        silently lost, which would look like "no data" rather than an error. A CONNACK
        that reports failure (e.g. bad credentials - MQTT delivers auth failures
        asynchronously, not as an exception from ``connect()``) aborts the collection
        window immediately.

        :param topic_filter: MQTT topic filter to subscribe to (e.g. ``nuki/+/+``)
        :type topic_filter: str
        :param timeout: how many seconds to collect messages for
        :type timeout: float
        :return: (topic, payload) pairs in arrival order, payloads decoded as UTF-8
        :rtype: list
        :raises ConfigError: if the shared ``mqtt`` settings block (or its
            ``broker_host``) is missing - a config-shape problem, fatal like a missing
            source block, not something the worker loop should retry
        :raises SourceConnectionError: if the broker is unreachable, refuses the
            connection (including bad credentials via the CONNACK reason code), or
            accepts TCP but never completes the MQTT handshake within the window
        """
        mqtt_settings = self.settings.get("mqtt")
        if not mqtt_settings or not mqtt_settings.get("broker_host"):
            raise ConfigError("MQTT sources need a top-level 'mqtt' settings block with 'broker_host' set")
        host = mqtt_settings["broker_host"]
        port = mqtt_settings.get("broker_port", 1883)
        messages = []
        connack_failure = []
        connected = []

        def on_connect(client, userdata, connect_flags, reason_code, properties):
            if reason_code.is_failure:
                connack_failure.append(str(reason_code))
            else:
                connected.append(True)
                client.subscribe(topic_filter)

        def on_message(client, userdata, message):
            messages.append((message.topic, message.payload.decode("utf-8", errors="replace")))

        client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2)
        if mqtt_settings.get("username"):
            client.username_pw_set(mqtt_settings["username"], mqtt_settings.get("password"))
        client.on_connect = on_connect
        client.on_message = on_message
        try:
            client.connect(host, port)
            deadline = time.monotonic() + timeout
            remaining = timeout
            while remaining > 0 and not connack_failure:
                client.loop(timeout=min(LOOP_INTERVAL, remaining))
                remaining = deadline - time.monotonic()
        except (OSError, ValueError) as e:
            logging.error("Error connecting to MQTT broker %s:%s - %s", host, port, e)
            raise SourceConnectionError(str(e)) from e
        finally:
            client.disconnect()
        self._raise_for_failed_connection(host, port, timeout, connack_failure, connected)
        return messages

    @staticmethod
    def _raise_for_failed_connection(host, port, timeout, connack_failure, connected):
        """
        Raise SourceConnectionError if the collection window ended without a usable
        connection - either the broker refused the CONNACK (e.g. bad credentials), or
        it accepted TCP but never completed the MQTT handshake at all (stalled
        network, hung broker). Without the latter check an unfinished handshake would
        return an empty message list, indistinguishable from a healthy broker with
        nothing retained - silently masking a connection failure as "no data".

        :param host: broker host (for the error message)
        :param port: broker port (for the error message)
        :param timeout: the collection window length (for the error message)
        :param connack_failure: CONNACK failure reasons recorded by on_connect
        :type connack_failure: list
        :param connected: truthy entries recorded by on_connect on success
        :type connected: list
        :return: None
        :raises SourceConnectionError: as described above; no-op on a healthy outcome
        """
        if connack_failure:
            error = f"MQTT broker {host}:{port} refused the connection - {connack_failure[0]}"
        elif not connected:
            error = f"MQTT broker {host}:{port} did not complete the MQTT handshake within {timeout}s"
        else:
            return
        logging.error(error)
        raise SourceConnectionError(error)
