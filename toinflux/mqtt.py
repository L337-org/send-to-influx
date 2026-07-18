"""Shared MQTT transport for data handlers whose source publishes to an MQTT broker"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT License"
__version__ = "1.0"

import logging
import time
from paho.mqtt import client as mqtt_client
from toinflux.influx import DataHandler
from toinflux.general import mqtt_block_errors
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
        :raises ConfigError: if ``timeout`` isn't a positive number, or if the
            shared ``mqtt`` settings block is missing,
            malformed, or has an out-of-range ``broker_port`` - a config-shape
            problem, fatal like a missing source block, not something the worker loop
            should retry. Checked here as well as in ``validate_settings()`` because
            that only covers the *configured* sources: a one-off ``--source nuki`` on
            an install where nuki isn't in ``sources:`` would otherwise reach this
            code unvalidated and fail with a raw AttributeError/TypeError.
        :raises SourceConnectionError: if the broker is unreachable, refuses the
            connection (including bad credentials via the CONNACK reason code), or
            accepts TCP but never completes the MQTT handshake within the window
        """
        errors = mqtt_block_errors(self.settings)
        if errors:
            raise ConfigError("; ".join(errors))
        # YAML coerces silently, so `timeout: "3"` is a string and would blow up in
        # the deadline arithmetic below as a raw TypeError. That matters beyond
        # tidiness: the worker loop catches broad exceptions and retries with
        # backoff, so a permanent configuration mistake would be retried forever
        # instead of failing fast as the ConfigError it is. bool is excluded because
        # it is an int subclass (`timeout: yes` is True, i.e. 1 second).
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ConfigError(f"the MQTT collection window must be a positive number of seconds (got {timeout!r})")
        mqtt_settings = self.settings["mqtt"]
        host = mqtt_settings["broker_host"]
        port = mqtt_settings.get("broker_port", 1883)
        messages = []
        # Any reason the collection window cannot proceed, recorded from the
        # callbacks (which cannot raise usefully - paho runs them inside its own
        # network loop) and turned into a SourceConnectionError once the loop exits.
        failures = []
        connected = []

        def on_connect(client, userdata, connect_flags, reason_code, properties):
            self._subscribe_on_connect(client, reason_code, topic_filter, failures, connected)

        def on_message(client, userdata, message):
            messages.append((message.topic, message.payload.decode("utf-8", errors="replace")))

        # Keyword form deliberately: callback_api_version IS paho 2.x's first
        # positional parameter (that was 2.0's breaking change), so positional
        # works - but naming it makes the call self-documenting and immune to
        # being misread as a client_id.
        client = mqtt_client.Client(callback_api_version=mqtt_client.CallbackAPIVersion.VERSION2)
        if mqtt_settings.get("username"):
            client.username_pw_set(mqtt_settings["username"], mqtt_settings.get("password"))
        client.on_connect = on_connect
        client.on_message = on_message
        try:
            client.connect(host, port)
            deadline = time.monotonic() + timeout
            remaining = timeout
            while remaining > 0 and not failures:
                if client.loop(timeout=min(LOOP_INTERVAL, remaining)) != 0:
                    # The connection died mid-window (paho returns a nonzero
                    # error code once the socket is gone) - stop collecting
                    # instead of busy-spinning out the rest of the window.
                    # Whatever retained state already arrived is still valid
                    # last-known data, so this is an early finish, not an
                    # error (a pre-CONNACK death is still caught below).
                    break
                remaining = deadline - time.monotonic()
        except (OSError, ValueError) as e:
            logging.error("Error connecting to MQTT broker %s:%s - %s", host, port, e)
            raise SourceConnectionError(str(e)) from e
        finally:
            client.disconnect()
        self._raise_for_failed_connection(host, port, timeout, failures, connected)
        return messages

    @staticmethod
    def _subscribe_on_connect(client, reason_code, topic_filter, failures, connected):
        """
        Handle a CONNACK: subscribe if it succeeded, otherwise record why not.

        Both outcomes are recorded rather than raised - paho runs this inside its own
        network loop, where an exception would be swallowed - and turned into a
        SourceConnectionError by _raise_for_failed_connection once the loop exits.

        subscribe() reports client-side failures (a malformed topic filter, a dead
        socket) through its return code rather than an exception. Ignoring it would
        leave a never-subscribed client looping out the whole window and returning an
        empty list - indistinguishable from a healthy broker with nothing retained,
        which is the failure mode this transport exists to avoid everywhere else.

        :param client: the paho client the callback fired on
        :param reason_code: CONNACK reason code
        :param topic_filter: filter to subscribe to
        :type topic_filter: str
        :param failures: accumulator for the reason this window cannot proceed
        :type failures: list
        :param connected: accumulator marking a usable, subscribed connection
        :type connected: list
        :return: None
        """
        if reason_code.is_failure:
            failures.append(f"broker rejected the connection: {reason_code}")
            return
        result = client.subscribe(topic_filter)[0]
        if result != mqtt_client.MQTT_ERR_SUCCESS:
            failures.append(f"could not subscribe to '{topic_filter}' (code {result})")
            return
        connected.append(True)

    @staticmethod
    def _raise_for_failed_connection(host, port, timeout, failures, connected):
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
        :param failures: reasons recorded by on_connect (a rejected CONNACK, or a
            failed subscribe) - the first is reported verbatim, so the message
            carries the specific cause rather than a guess at it
        :type failures: list
        :param connected: truthy entries recorded by on_connect on success
        :type connected: list
        :return: None
        :raises SourceConnectionError: as described above; no-op on a healthy outcome
        """
        if failures:
            error = f"MQTT broker {host}:{port}: {failures[0]}"
        elif not connected:
            error = f"MQTT broker {host}:{port} did not complete the MQTT handshake within {timeout}s"
        else:
            return
        logging.error(error)
        raise SourceConnectionError(error)
