"""Shared MQTT transport for data handlers whose source publishes to an MQTT broker"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT License"
__version__ = "1.0"

import logging
import threading
import time
from paho.mqtt import client as mqtt_client
from toinflux.influx import DataHandler
from toinflux.general import mqtt_block_errors
from toinflux.exceptions import ConfigError, SourceConnectionError

# How long each call into paho's network loop blocks waiting for traffic. Small enough
# that the collection window's deadline (and a failed-CONNACK abort) is honoured
# promptly, large enough not to busy-spin. Also the poll granularity while waiting for
# a persistent stream's initial handshake.
LOOP_INTERVAL = 0.5

# How long stream_mqtt_messages() waits for the initial CONNACK + subscribe before
# giving up and raising SourceConnectionError, so a broker that accepts TCP but never
# completes the handshake fails fast (and is retried with backoff by the worker) rather
# than blocking startup indefinitely. Reconnections *after* a healthy start are handled
# by paho's own background loop, not this.
STREAM_CONNECT_TIMEOUT = 10

# Bounds for paho's automatic reconnect backoff once a persistent stream has started
# (seconds). A dropped connection self-heals in the background - on reconnect the
# on_connect callback re-subscribes, and because state topics are retained the broker
# redelivers current state, so every reconnect doubles as a free resync.
RECONNECT_MIN_DELAY = 1
RECONNECT_MAX_DELAY = 120


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

    MQTT sources are designed to be event-driven rather than timer-driven (see
    ``stream_mqtt_messages``): the transport holds the subscription open and surfaces a
    message the instant it arrives, so a transient event (a door opening then closing
    between two polls) need no longer be missed. ``STREAMING = True`` is the flag the
    worker-integration slice will branch on to take that path instead of the
    poll-then-sleep loop; it's a property of the transport, not a per-source option
    (there's no reason a subscribed source would not want it, and no compatibility
    reason to make it optional). ``sendtoinflux.py`` does not consult it yet.
    """

    STREAMING = True

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

    def stream_mqtt_messages(self, topic_filter, on_message, periodic, interval, should_stop):
        """
        Hold an MQTT subscription open and react to messages as they arrive.

        Unlike ``collect_mqtt_messages`` (a fixed poll window that connects, drains and
        disconnects), this keeps a persistent connection: ``on_message`` is invoked for
        each message the instant it arrives, and ``periodic`` is invoked once every
        ``interval`` seconds for the timer-based safety net (a full-state snapshot and
        the collector heartbeat). Both are the caller's callbacks; this method owns only
        the transport and the concurrency it creates. It blocks until ``should_stop`` is
        set, then disconnects cleanly.

        Concurrency: ``on_message`` runs on paho's background network thread while
        ``periodic`` runs on this (the worker) thread, and both typically write to
        InfluxDB through the same non-thread-safe ``requests`` session and shared write
        buffer. This method serialises them under a single lock, so a message write and
        a snapshot write never overlap; the cost is that a slow InfluxDB write briefly
        holds up message processing, which is the right trade (correctness over a few
        milliseconds of latency). A callback that raises is logged and swallowed for
        ``on_message`` (one bad message must not tear the stream down - the point, if it
        was buffered, flushes on the next write) but allowed to propagate for
        ``periodic`` (so the caller's own error handling and the worker's backoff apply).

        The initial CONNACK and subscribe are awaited up front and mapped to
        ``SourceConnectionError`` on failure/timeout - identical semantics to
        ``collect_mqtt_messages`` - so a bad broker at startup is retried with backoff by
        the worker. A drop *after* a healthy start is not raised: paho's background loop
        reconnects (``reconnect_delay_set``), and on_connect re-subscribes, which for
        retained topics redelivers current state.

        :param topic_filter: MQTT topic filter to subscribe to (e.g. ``nuki/+/+``)
        :type topic_filter: str
        :param on_message: called ``on_message(topic, payload)`` for each message, with
            ``payload`` already UTF-8 decoded; runs under the shared lock
        :type on_message: collections.abc.Callable
        :param periodic: called with no arguments once every ``interval`` seconds, under
            the shared lock - the timer-based snapshot/heartbeat tick
        :type periodic: collections.abc.Callable
        :param interval: seconds between ``periodic`` ticks
        :type interval: float
        :param should_stop: set to end the stream and return; also woken early when set
            so shutdown doesn't wait out a full ``interval``
        :type should_stop: threading.Event
        :return: None
        :raises ConfigError: if the shared ``mqtt`` block is missing/malformed or
            ``interval`` isn't a positive number - fatal config shape, not retried
            (same rationale as ``collect_mqtt_messages``)
        :raises SourceConnectionError: if the broker is unreachable, refuses the CONNACK
            (e.g. bad credentials), or never completes the initial handshake
        """
        errors = mqtt_block_errors(self.settings)
        if errors:
            raise ConfigError("; ".join(errors))
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval <= 0:
            raise ConfigError(f"the MQTT snapshot interval must be a positive number of seconds (got {interval!r})")
        mqtt_settings = self.settings["mqtt"]
        host = mqtt_settings["broker_host"]
        port = mqtt_settings.get("broker_port", 1883)
        client, state = self._build_stream_client(mqtt_settings, host, port, topic_filter, on_message)
        try:
            client.connect(host, port)
        except (OSError, ValueError) as e:
            logging.error("Error connecting to MQTT broker %s:%s - %s", host, port, e)
            raise SourceConnectionError(str(e)) from e
        client.loop_start()
        try:
            self._await_initial_connection(host, port, state["failures"], state["connected"])
            logging.info("Streaming MQTT messages from %s:%s (snapshot every %ss)", host, port, interval)
            # wait() returns True only when should_stop is set (shutdown) and False on
            # timeout (run a periodic tick), so a stop is honoured promptly instead of
            # waiting out a full interval. The first snapshot is one interval in - the
            # retained state that arrives on subscribe is already written immediately by
            # on_message, so there's nothing to snapshot sooner.
            while not should_stop.wait(interval):
                with state["lock"]:
                    periodic()
        finally:
            state["stopping"].set()
            # disconnect() before loop_stop(): with paho's loop_start() background
            # thread, disconnect() queues a clean DISCONNECT that the still-running
            # network thread transmits; stopping the loop first can tear the thread
            # down before that packet is sent. (The fixed-window collector only calls
            # disconnect() because it drives the loop synchronously with no thread to
            # stop.)
            client.disconnect()
            client.loop_stop()

    def _build_stream_client(self, mqtt_settings, host, port, topic_filter, on_message):
        """
        Construct the paho client and callbacks for a persistent stream.

        Kept separate from ``stream_mqtt_messages`` so that method stays focused on the
        connect/await/loop lifecycle. Returns the client alongside the shared mutable
        state its callbacks write to (the handshake accumulators, the send lock that
        serialises on_message against periodic, and the ``stopping`` flag that keeps our
        own shutdown disconnect from being logged as a fault).

        :param mqtt_settings: the shared ``mqtt`` settings block
        :type mqtt_settings: dict
        :param host: broker host (for logging on an unexpected disconnect)
        :param port: broker port (for logging on an unexpected disconnect)
        :param topic_filter: filter the on_connect callback (re)subscribes to
        :type topic_filter: str
        :param on_message: caller callback invoked ``on_message(topic, payload)`` under
            the send lock for each message
        :type on_message: collections.abc.Callable
        :return: (client, state dict with ``failures``/``connected``/``lock``/``stopping``)
        :rtype: tuple
        """
        state = {
            "failures": [],
            "connected": [],
            # Serialises on_message (paho's network thread) against periodic (the worker
            # thread) - see stream_mqtt_messages' concurrency note.
            "lock": threading.Lock(),
            # Distinguishes our own disconnect() at shutdown from an unexpected drop.
            "stopping": threading.Event(),
        }

        def on_connect(client, userdata, connect_flags, reason_code, properties):
            # Fires on the initial connect and every reconnect; re-subscribing here
            # (not once after connect()) is what makes reconnection self-healing, since
            # paho drops subscriptions on a reconnect.
            self._subscribe_on_connect(client, reason_code, topic_filter, state["failures"], state["connected"])

        def dispatch_message(client, userdata, message):
            payload = message.payload.decode("utf-8", errors="replace")
            try:
                with state["lock"]:
                    on_message(message.topic, payload)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                # A single failed/odd message must not kill a long-lived stream; if the
                # handler buffered the point before failing, it flushes on the next write.
                logging.warning("Error handling MQTT message on topic '%s': %s", message.topic, exc, exc_info=True)

        def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
            if not state["stopping"].is_set():
                logging.warning(
                    "MQTT connection to %s:%s lost (%s); paho will attempt to reconnect", host, port, reason_code
                )

        client = mqtt_client.Client(callback_api_version=mqtt_client.CallbackAPIVersion.VERSION2)
        if mqtt_settings.get("username"):
            client.username_pw_set(mqtt_settings["username"], mqtt_settings.get("password"))
        client.on_connect = on_connect
        client.on_message = dispatch_message
        client.on_disconnect = on_disconnect
        client.reconnect_delay_set(min_delay=RECONNECT_MIN_DELAY, max_delay=RECONNECT_MAX_DELAY)
        return client, state

    def _await_initial_connection(self, host, port, failures, connected):
        """
        Block until the initial CONNACK + subscribe resolves, then raise if it failed.

        Polls the ``failures``/``connected`` accumulators the on_connect callback writes
        from paho's network thread, up to ``STREAM_CONNECT_TIMEOUT`` seconds, then defers
        to ``_raise_for_failed_connection`` for the same failure mapping the fixed-window
        collector uses (a rejected CONNACK reported verbatim, or a handshake that never
        completed).

        :param host: broker host (for the error message)
        :param port: broker port (for the error message)
        :param failures: accumulator the on_connect callback records a rejection into
        :type failures: list
        :param connected: accumulator the on_connect callback marks on success
        :type connected: list
        :return: None
        :raises SourceConnectionError: if the handshake failed or never completed in time
        """
        deadline = time.monotonic() + STREAM_CONNECT_TIMEOUT
        while time.monotonic() < deadline and not failures and not connected:
            time.sleep(min(LOOP_INTERVAL, max(0, deadline - time.monotonic())))
        self._raise_for_failed_connection(host, port, STREAM_CONNECT_TIMEOUT, failures, connected)

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
