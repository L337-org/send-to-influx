<!-- Architecture note: implementation detail for contributors and assistants.
     Not user documentation - see README.md for that. -->

# Collector internals

Deep detail behind the class hierarchy in [../AGENTS.md](../AGENTS.md): the write buffer,
Hue bridge slots, the Nuki device-tag migration, MQTT streaming, and MyEnergi device
selection. Read this before changing anything under `toinflux/` other than the MCP modules.

## Class hierarchy

```
DataHandler      (toinflux/influx.py)          - base; owns send_data() -> InfluxDB HTTP POST
├── CarbonIntensity(toinflux/carbonintensity.py)
├── Hue            (toinflux/philipshue.py)
├── OpenMeteo      (toinflux/openmeteo.py)
├── Octopus        (toinflux/octopus.py)
├── Speedtest      (toinflux/speedtest.py)
├── MqttDataHandler(toinflux/mqtt.py)        - intermediate parent for MQTT transport
│   └── Nuki       (toinflux/nuki.py)
└── MyEnergi       (toinflux/myenergi.py)     - intermediate parent for MyEnergi API auth
    ├── Zappi      (toinflux/myenergi.py)
    ├── Eddi       (toinflux/myenergi.py)
    └── Harvi      (toinflux/myenergi.py)
```

Implement `get_data()` in each subclass to populate `self.data` and `self.influx_header`;
`send_data()` in the base takes it from there. Points carry an explicit unix-epoch-seconds
timestamp: `self.timestamp` when `get_data()` set one - Octopus uses the reading's own
`interval_start`, so re-writing a reading overwrites rather than duplicates - otherwise the time
`send_data()` is called. Field keys are escaped per line protocol rules (commas, `=`, spaces).

## The write buffer

A failed write buffers the point in memory rather than dropping it, and still raises
`InfluxWriteError`, so the worker's backoff is unaffected.

`DataHandler._write_buffers` is a per-*worker* `deque(maxlen=MAX_BUFFERED_POINTS)` of
`[line, rejection_count]` entries. Two properties are load-bearing:

- **Class-level, not an instance attribute.** The worker loop in `sendtoinflux.py` discards and
  reconstructs the `DataHandler` after every failure, so only a buffer outliving the instance
  survives to be flushed.
- **Keyed by `DataHandler.worker_key`**, the `(source, instance)` tuple, never by source name. A
  source with several instances runs one worker each and they must not share a deque, because
  `_flush_buffer`/`_flush_head` do read-then-`popleft` sequences that are not atomic across
  threads. `instance` is `None` for single-target sources, so their key is `(source, None)`. The
  `maxlen` bound is therefore per worker: N instances can hold N x `MAX_BUFFERED_POINTS` between
  them.

Keep `worker_key` a tuple rather than a joined `source@instance` string. An instance may be an
IPv6 literal, so any delimiter is ambiguous to a later split, and callers needing the
settings-block name back - the stall watchdog reads `settings[source]["interval"]` - must not
re-parse it. `worker_label` (`source` or `source@instance`) is the display-only counterpart: use
it in log messages, never as a key or an emitted tag value.

Every buffered-path `send_data()` flushes the backlog first, including calls with no data of
their own; only an empty buffer *and* empty data skips the HTTP round trip. Flushes go in
newline-joined chunks of `FLUSH_CHUNK_SIZE` per POST, so a 500-point recovery costs about 5
requests rather than 500. When a buffer fills, the oldest point is dropped with a warning. An
identical line is never added twice, since Octopus re-serves the same reading for around 30
minutes and flushing is an idempotent overwrite.

Buffers are not persisted across a restart, and flush to whatever the *current* settings resolve
to - editing `influx.url` or the bucket mid-backlog re-routes it. Accepted limitation, recorded in
the `_write_buffers` comment.

### Do not treat a status code as a verdict

`InfluxWriteError.status_code` carries the HTTP status, or `None` for a connection failure.
`_flush_buffer()`/`_flush_head()` count how often the server has *rejected* each specific point
and drop it only after `MAX_POINT_REJECTIONS` separate rejections.

Only a non-transient 4xx counts: `TRANSIENT_CLIENT_ERRORS` excludes 408 and 429, because
rate-limiting and timeouts say nothing about the point. Connection failures and 5xx never count
either, so an arbitrarily long outage cannot age points out, and a middlebox transiently
answering 4xx for a down InfluxDB cannot mass-discard a backlog. What is given up on is a point
the server itself keeps refusing: malformed (400), outside the retention window (422 on InfluxDB
v2), oversized (413).

A rejected chunk falls back to per-point posting to isolate the offender. Heartbeat writes pass
`use_buffer=False` - a heartbeat is a live signal with no replay value, so it neither consumes
capacity nor triggers a second flush per failed cycle. `validate_settings()` rejects duplicate
entries in `sources:` with a `ConfigError`, since two workers for one name would share and race
on one buffer.

## Hue

**Build every bridge request URL through `Hue._api_base()`** (`https://<host>/api/<user>`), shared
by the read path (`get_data_from_hue_bridge`) and the MCP write path (`_put_light_state`). The bug
this prevents existed in *two* copies of the same f-string, which is exactly how one path silently
keeps it.

The host passes through `_url_host()`, which brackets a bare IPv6 literal:
`https://2001:db8::1/...` is ambiguous because everything from the first colon parses as a port.
Hostnames, IPv4 literals and already-bracketed values are returned unchanged, so it is idempotent.
Bracketing is a URL-construction concern only - `get_data()` tags the point with
`self.settings['hue']['host']` verbatim, because normalising the tag would change series identity
for anyone already running an IPv6 bridge.

### Bridge slots

`enumerate_bridges()` (`toinflux/philipshue.py`) is the single source of truth for which bridges
are configured, shared by `validate_settings()`, the worker spawner and the CLI modes.

Slot 1 is the unnumbered `host`/`user` pair; further bridges are `hostN`/`userN`, uncapped. **Slot
numbers carry no ordering, need not be contiguous, and nothing renumbers.** The slot number *is*
the binding between a host and its token, so a vacated slot stays vacant rather than shifting the
ones above it onto the wrong credentials - which fails silently, the surviving bridge presenting
as a bad token rather than a config error. `bridge_field_names()` is the only place that knows the
numbering; never build `f"host{n}"` at a call site.

The severity split is load-bearing:

- **Fatal `ConfigError`** for self-contradictory config: a non-canonical slot field (`host1` is
  canonical, `host02` is not), a non-string host, or two slots addressing the same bridge -
  compared via `_comparable_host()`, which normalises IPv6 spelling and hostname case *for
  comparison only*.
- **Warning** for "not usable yet": no host, or a host whose token is blank, a placeholder or an
  unsubstituted sentinel. `example_settings.yaml` ships `hue` in `sources:` beside a placeholder
  token, so a fresh install is exactly that state; raising would stop every collector and break
  the packaging suite's invariant that the example's placeholders pass validation while workers
  merely retry.
- **DEBUG** for a leftover `userN` with no `hostN`, the resting state after `--remove`, which
  blanks the token and leaves clearing the host as a separate step.

Warnings are opt-in via `validate_settings(..., warn=True)` and used only by `--check-config`.
`validate_settings()` runs inside `load_settings()`, so unconditional logging would repeat per
source at startup and again on every failure-triggered handler rebuild.

### Which bridge a handler collects from

`Hue.bridge()` resolves `self.instance` against `enumerate_bridges()`. `instance=None` means the
first configured bridge, which keeps a single-bridge install - and every caller constructing a
handler without an instance, notably the MCP tools - behaving as it did before slots existed.
`_api_base()` and `get_data()` both build from the resolved bridge, so a worker uses its own
bridge's host and token rather than slot 1's.

An instance matching no configured bridge, a malformed block, or no usable bridge raises
`ConfigError`, not `SourceConnectionError`, so a worker whose bridge has gone stops instead of
authenticating in a loop forever. That is where acceptance criterion 6's intent lands: per worker,
rather than fatally at load time.

The `host` tag is passed through `escape_key_or_tag_value()` and **never normalised**.
`send_data()` escapes field keys and takes the header verbatim, so a host containing a comma,
equals sign or space would end the tag set early and silently write a corrupt point - while
rewriting the value would change the series identity of an install already running IPv6.

### Redact the token from every Hue message

The token sits in the URL path, and `requests` puts the request URL into its exception messages -
both `Max retries exceeded with url: /api/<token>` and
`503 Server Error ... for url: https://host/api/<token>/...`, confirmed by reproduction. So pass
every Hue error through `Hue._redact()` before logging *or* raising it. Without it one unreachable
bridge wrote the token to the journal and `/var/log/send-to-influx.log` via the worker loop's
`Source '%s' failed` line, and handed it to any connected MCP client, since a
`SourceConnectionError` from a tool is returned to the caller.

Only the token is replaced, with `<redacted>`; host, status and underlying cause survive verbatim
so a failure stays diagnosable from the log alone. An absent, blank or non-string token
short-circuits the replacement, because `"".replace()` splices the marker between every character.
The wrapped cause (`raise ... from e`) deliberately keeps the unredacted text: the cause chain must
be preserved and is only exposed by printing a traceback, which no path does for these errors.

Hue is the only source needing this - every other passes credentials via an auth tuple, digest auth
or a header. Pre-5.2 logs still contain the token; see SECURITY.md for the revoke advice.

`_redact()` covers *every* configured bridge's token, not just the resolved one, so it is safe to
call from an exception handler and cannot miss a token from an unexpected slot.

### The systemd-creds caveat

A credential migrated to systemd-creds reads as unset when `--check-config` runs by hand, because
systemd mounts `$CREDENTIALS_DIRECTORY` only for the service. `_credstore_caveat()` appends the
explanation **to the unset-token warning itself**, and only when `CREDENTIALS_DIRECTORY` is unset -
under the service the value really was substituted, so an unset token is genuinely unset and the
note would misdirect.

Attach it to the finding it explains rather than reporting it once per run. Emitted per run it also
landed beside "no Hue bridge is configured", an absent host with no credential involved, sending
the reader to the credential store for something that was never missing. It reaches the runtime
`ConfigError` from `Hue.bridge()` for free, since that reuses the same warning text.

## Speedtest

`get_data()` rejects an implausible `ping` of 5000 ms or more as a `SourceConnectionError` rather
than writing it.

speedtest-cli's `get_best_server()` times each of the 3 latency probes per candidate server with a
hardcoded 10-second connection timeout, baked into `SpeedtestHTTPConnection`/
`SpeedtestHTTPSConnection`'s constructor default and never overridden, so it applies regardless of
the `timeout` passed to `speedtest.Speedtest()`. A probe that does not complete raises
`socket.timeout`, which is penalised with a hardcoded `3600` seconds instead of a real sample. The
3 samples are summed, divided by a fixed 6 and converted to milliseconds, so a genuine measurement
cannot exceed `(3 * 10 / 6) * 1000 = 5000` ms. When every probe to a server fails - observed during
a transient network blip - the reported `ping` is around 1,800,000 ms and would otherwise be
written as though real.

## Nuki and MQTT

`MqttDataHandler` (`toinflux/mqtt.py`) owns the transport: connect, subscribe from inside
`on_connect` - a subscription issued before the CONNACK completes can be silently lost - collect
for a fixed window, disconnect. Broker config comes from the shared top-level `mqtt:` block,
mirroring `influx:`, because the broker and its `mqtt-password` credential are per-install
infrastructure rather than per-source.

The per-interval snapshot works over MQTT only because Nuki publishes every state topic with the
retain flag set, so a short subscribe window receives the last-known state of every provisioned
lock - equivalent to an HTTP GET.

Map failures strictly. Bad credentials arrive asynchronously as a failed CONNACK, never as an
exception from `connect()`, and a broker that accepts TCP but never completes the handshake raises
`SourceConnectionError` rather than returning an empty result. Either would otherwise masquerade as
"no data".

`Nuki` (`toinflux/nuki.py`) holds vendor logic only: filtering to known state topics (command and
event topics are ignored), grouping by device ID, labelling each lock with its Nuki-app name, and
renaming `state`/`doorsensorState` to `stateValue`/`doorsensorStateValue`. Grafana visualises
numeric fields far better than text, so these are always written as their raw numeric code, and a
code with no documented meaning is written through unchanged. See UNITS.md for what each means.

`paho-mqtt` is a source-specific runtime dependency like `speedtest-cli`, pure Python so the
`.deb`'s `Architecture: all` design holds, and is imported only in `toinflux/mqtt.py`.

### One point per lock (5.3)

`parse_nuki_data()`/`decode_stream_message()` return `{device: {field: value}}`, and
`Nuki.send_data()` writes one point per lock tagged `device=<lock>` with bare field keys,
delegating each to the base implementation with the header swapped in - the same idiom
`send_heartbeat()` uses, so buffering, retry and the `InfluxWriteError` contract are untouched
rather than reimplemented.

- **Every lock in one cycle shares a timestamp.** Letting each call default independently would
  scatter one snapshot across a second or two, so "what was the state at time T" could see one lock
  and not another.
- **A failure on one lock does not stop the rest.** Each is attempted and one error raised at the
  end, so the worker still backs off, and re-writing a lock that already succeeded is harmless
  because points are idempotent.
- `MCP_INSTANCE_TAG = "device"`, and `MCP_LIVE_STATE_COVERS_ALL_INSTANCES = True` because Nuki is
  the only source whose one live read covers every producer - a single retained-state subscription
  returns all locks, unlike Hue where each bridge needs its own handler.

The broker `host` tag was dropped in the same change rather than as a separate series break: every
lock arrives through one broker, so it identified nothing, and moving broker should not start a new
series.

**This was a breaking change to emitted data, and the field-key prefix was the original mistake.**
Before 5.3 every lock's fields were flattened into one shared point with the lock's name built into
each field key (`Front_Door_Lock_stateValue`), which is why the lock could not be queried as a
dimension. Existing history keeps working but sits in different series from new points.

### Discriminate on the payload's shape, not on whether data was given

`send_heartbeat()` sets its own `collector_status` header and passes a flat `{field: value}` dict
through the *same* `send_data()`, and the streaming path passes per-device data explicitly. So
"was `data` given?" cannot tell them apart. `_is_per_device()` decides on every value being a
mapping, since a lock always carries a dict of fields and a field never does.

Getting this wrong treated `ok`/`consecutive_failures` as lock names whose scalar values were then
skipped, so **Nuki wrote no heartbeat at all**, silently, with only warnings - the exact gap the
heartbeat exists to prevent.

The tests did not catch it because every heartbeat test used a `MagicMock` handler, whose
`send_data` never runs the source's own override: those tests assert what `send_heartbeat` *asked
for*, never what the handler *did*. `test_every_source_actually_writes_a_heartbeat_point` drives a
real handler per source down to the HTTP boundary, written across every source because the break
was one subclass violating a shared contract.

### Name external values with `!r` in every message

A lock name comes from the retained MQTT `name` topic, and one containing a newline turned a
per-lock failure message into *two* journal lines: the worker logs `Source '%s' failed: %s`, so a
forged entry with its own timestamp and ERROR level appeared as though the daemon wrote it, and the
same text reaches an MCP client as a tool error.

`escape_key_or_tag_value`'s own message was already safe; the prefix wrapped around it was not.
Sweep rather than patching the reported line - the same shape existed in `mcp_write`'s
unreachable-bridge list and `mcp_read`'s all-instances-failed message, both of which reach a
client. Report the name still, just escaped: a failure has to stay diagnosable from its output.

### Flush the backlog once per cycle, not once per lock

The write buffer is keyed by worker, so calling the base `send_data()` per lock flushed it per lock
too, charging the head buffered point one rejection each time. With `MAX_POINT_REJECTIONS` at 5, a
five-lock install burned the whole allowance in one cycle and discarded the backlog after a single
cycle instead of five - defeating the guarantee that a middlebox answering 4xx cannot mass-discard
it.

`DataHandler.send_data()` therefore takes `flush=`, and Nuki passes it only for its first lock.
Every lock still buffers its own point on failure; only the flush is shared. Measured before and
after - 1/3/3 charged with five dropped outright, now 1 at any lock count - and the test asserts
the count, because the count *is* the property.

## The Nuki device-tag migration

`scripts/migrate-nuki-device-tag.py`, shipped to `/usr/share/send-to-influx/` and documented in
`UPGRADING.md`. Points that cost real debugging:

- **It cannot be InfluxQL.** There is no UPDATE; `SELECT ... INTO` preserves existing tags via
  `GROUP BY *` but has no syntax to set a tag to a new literal, which is the entire job; and the
  lock name lives in the field *key*, which InfluxQL has no expression to operate on.
- **Two phases, separately invoked.** Phase 1 is non-destructive by construction - the new format is
  a different series - so old and new coexist and backing out means not running phase 2. Phase 2 is
  driven by a manifest phase 1 writes and scoped by the old `host` tag, so it can only drop what
  phase 1 confirmed it carried across. An earlier version did an unscoped `DROP SERIES FROM "nuki"`,
  which would have destroyed the migration's own output.
- **It reads no credentials on any install type.** A fallback to `settings.yaml` where it happens to
  be readable would make the safeguard depend on how you installed. It needs `requests`, which the
  package keeps in its venv, so the documented invocation is `/opt/send-to-influx/venv/bin/python3` -
  a bare `python3` fails where `python3-requests` is absent.
- **Its value formatter duplicates `_format_field_value()` and must stay identical.** It emitted the
  `i` integer suffix at first; a field's type is fixed by its first write, so that established these
  fields as integer and every subsequent *collector* write failed with a 400 type conflict - the
  migration would have broken the running collector. Only writing both outputs to one real InfluxDB
  surfaces it, and a test pins the equivalence for every value shape. It is deliberately stricter in
  one respect: a numeric value on a text field is still quoted.
- **Its field list is a superset of the collector's, and was hand-copied wrongly.** Listing a real
  database's field keys found `stateName`/`doorsensorStateName` - text state names an earlier release
  wrote before the numeric rename - still holding years of points and absent from the copy, so the
  migration halted on the very data it exists to rescue. Without the halt, phase 2 would have deleted
  them. Test a migration against real data from the previous release, not a fixture matched to its own
  assumptions.
- **The split is longest-known-suffix, and the underscore in the comparison is load-bearing**: a key
  merely *ending* in a field name is not ours and must halt. Longest-match itself is unreachable
  today - two known fields could only both match if one ended with `_<the other>`, and none contains
  an underscore - so no test kills it. Documented as the guard for a future underscored field rather
  than left looking tested.
- **Halt, never skip.** An unrecognised field key stops the run with nothing written: a skipped key is
  data silently left behind that phase 2 would then delete, and it would look like success.
- **Statements travel in a POST body, never the URL.** The rewrite phase names every old field key in
  one `SELECT`, one per lock per field, so a ten-lock estate is kilobytes of statement, and in a
  request line a reverse proxy can refuse it - failing the migration on a statement InfluxDB would
  have accepted. The same shape the read layer hit, which is why `build_edge_time_query` selects `*`.
  POST verified equivalent to GET on real 1.8 and 2.7 for every statement this script issues.
- **v2 has no `DROP SERIES`, so phase 2 differs by version.** Its v1-compatibility endpoint answers
  HTTP *200* carrying `{"error": "not implemented: DROP SERIES"}`, verified on 2.7, so the error check
  catches it rather than mistaking it for success - but it can never succeed. Phase 1 works fully on
  v2, so the operator would be left with migrated data and no way to finish; phase 2 translates that
  one rejection into the `/api/v2/delete` request that does work, built with `json.dumps` because the
  predicate's own value contains double quotes and hand-assembly produced invalid JSON. The emitted
  command was run verbatim against a real 2.7 (204, old `host=` series gone, migrated `device=` kept).
  It is deliberately not run automatically: it needs the organisation, which the script cannot know and
  must not guess for a delete that cannot be undone. Only "not implemented" is translated; any other
  failure surfaces as itself.

## Sweep `tests/integration/` when emitted data changes

Integration tests are deselected from the default `pytest` run because they need a broker, so a green
local run says nothing about them - and `pytest -m integration` *without* a broker skips cleanly, so it
proves nothing either.

The Nuki device-tag change left `test_mqtt_streaming.py` asserting the old prefixed field key and
`startswith("nuki,host=")`, the exact tag the change removed, and only CI caught it.

When a change alters a measurement, tag set or field key: grep `tests/integration/` for the old names;
run that suite against a real broker (`MQTT_TEST_BROKER_HOST`/`MQTT_TEST_BROKER_PORT` point it
anywhere, so a throwaway `eclipse-mosquitto:2` container is enough); then mutate the product back and
confirm the test fails, since an assertion surviving the old behaviour was never testing the new one.

## Streaming (5.1)

MQTT sources are event-driven rather than timer-polled. `MqttDataHandler` sets `STREAMING = True` and
`stream_mqtt_messages()` holds the subscription open, so a state change is written the instant its
retained message arrives.

**The paho network thread only enqueues.** It puts decoded messages onto a bounded `queue.Queue`, and a
single worker thread (`_run_stream_loop`) drains it and does all InfluxDB I/O - both the immediate
per-message write and the periodic snapshot. A slow write must never stall paho's keepalives, which
would drop the connection and lose exactly the transient events streaming exists to capture. On
overflow the oldest queued message is dropped: freshest state wins, and the snapshot resyncs anyway.

`_should_stream()` in `sendtoinflux.py` gates the stream path on `STREAMING` *and* a non-`None`
`STREAM_TOPIC_FILTER`, so an MQTT transport with no concrete source wired yet keeps polling rather than
subscribing to `None` forever. When eligible, the worker runs a blocking
`stream_source_data()`/`_StreamSink` instead of the poll-then-sleep cycle.

The per-`interval` poll stays as a full-state safety-net snapshot **and** an active health probe. It
hits the same broker as the live stream, so its failure correlates with the stream being down and it
drives the heartbeat's `ok`/`consecutive_failures` - unless a message arrived since the last tick. A
healthy-but-idle lock sends nothing for hours, so the probe proves it live; a live stream with a flaky
one-off probe stays healthy on the message. A failing probe never tears the stream down, since paho
reconnects genuine drops and re-subscribes to redelivered retained state. Shutdown is clean on the main
thread (single-source) and best-effort for daemon workers (multi-source).

Emitted data is unchanged, so this is a behaviour change and not a breaking one, and there is no new
config - streaming is a property of the transport, not an option.

`Nuki.decode_stream_message()` (with `STREAM_TOPIC_FILTER = "nuki/+/+"`) is the per-message vendor
decode, the event-driven counterpart to `parse_nuki_data`, reusing `_decode_field` and remembering each
device's retained `name` as its field-key prefix, warning on a duplicate-name collision as the snapshot
path does. The snapshot path is untouched, so existing Grafana panels keep working, just denser.

## MyEnergi multiple devices

Each of `zappi`/`eddi`/`harvi` collects one worker per configured device, registered through
`_INSTANCE_ENUMERATORS` like Hue's bridges. `enumerate_devices()` (`toinflux/myenergi.py`) is the single
source of which devices are configured, shared by validation, the worker spawner and the handler's own
`device()` resolution, returning `(devices, errors, warnings)` so both instanced sources report problems
alike.

- **Two config shapes, and both may appear together.** A `serial` at the top of the block is the legacy
  single-device form; its `label` is optional and defaults to the source name, which keeps such an
  install writing `device=zappi` as before and is why this needed no data migration. A `devices:` list
  adds more, each naming its `label` explicitly - there is no sensible default for a second device, and
  deriving one from the serial would give exactly the unreadable tag values that tagging by label avoids.
  `fields` resolves device-first, then block-level, then everything the API returns.
- **Labels are the emitted `device` tag and must be unique across all three blocks.** The types share the
  `myenergi` measurement, so a zappi and an eddi agreeing on a label would merge into one series carrying
  both devices' fields. Check whenever any of the three is selected, never per block, because per-block
  checking misses precisely the collision that matters.
- **`MCP_TAG_FILTERS` on the three subclasses is gone.** `mcp_tag_filters()` supplies
  `{"device": <this device's label>}` per instance, and carries the type discrimination the old static
  filter provided: without a device filter, a read of the `myenergi` measurement returns all three types.
- **`shares_measurement()` decides whether discovered tag values can be trusted.** Once `device` carries
  an arbitrary label, a value found in the data cannot be attributed to a type - so for a shared
  measurement the *configured* devices are the allowlist and `discover_tag_values()` is not called at all.
  A source owning its measurement still unions discovered with configured. Reported series are filtered to
  the allowlist either way. Consequence: a decommissioned MyEnergi device's history stops being reachable
  by label, where a Hue bridge's does not.
- **`heartbeat_tags()` is overridden** to tag `device` rather than the base's `host`: a MyEnergi instance
  is a device label, and a health series tagged differently from the measurement it reports on cannot be
  joined to it. This adds a tag to a legacy install's heartbeat where there was none - a deliberate
  emitted-data change on a liveness signal, noted in UNITS.md.
- **`worker_label()` collapses an instance equal to the source name**, or every log line for a legacy
  install would read `zappi@zappi`. `worker_key` keeps the instance, being an identity rather than a label.
- **`myenergi.auth_serial` optionally overrides the digest username**, defaulting to the device's own
  serial as every install already sends. The credential is account-scoped - the real zappi serial
  authenticates against all three endpoints, verified live - but that is evidenced rather than proven for
  a second device of one type, since the test account has one zappi. The override exists so discovering
  otherwise needs no config change.

## MyEnergi device selection (`toinflux/myenergi.py`)

The status endpoints are per device **type** (`cgi-jstatus-Z`/`-E`/`-H`) and each returns every device of
that type on the account, so the configured `serial` picks one out in `_select_device()`. It used to take
index 0, with two consequences on one line: a second device of the same type was never collected whichever
serial was configured, and an account owning none of that type raised `IndexError`, which the worker
loop's broad handler caught and retried forever logging only "list index out of range".

- **`sno` is the serial field**, confirmed against the live API as the only key whose value equals the
  configured serial. `deviceClass` and `productCode` are also present if type identification is wanted.
- **Compare both sides as strings.** An all-digit serial in `settings.yaml` is an `int` unless quoted, so
  a raw comparison would never match and would present as a wrong serial rather than a type mismatch.
- **The two failure modes take different exception types**, because one is worth retrying and the other
  never is. No device of that type is a `SourceConnectionError` - a device can legitimately be
  mid-provisioning, and an absent response key is not distinguishable here from a temporary API oddity.
  Devices present but none matching the serial is a `ConfigError`: the account is reachable and the type
  exists, so the serial is simply wrong, and that worker should stop rather than back off forever.
  Swapping either type is mutation-tested.
- **The `ConfigError` names the serials the account does report**, which is the difference between a
  message the operator can act on and one that only says no. A missing response key is treated as an empty
  list rather than allowed to raise `KeyError`, which would escape the same exception contract the
  `IndexError` escaped.
- `sno` is written as a field on any install with no `fields` list configured, since the whole device dict
  is returned then. Long-standing behaviour, worth knowing before adding a `fields` list changes what a
  dashboard sees.

## Reading back (`toinflux/influx.py`)

`influx.py` owns both directions: `DataHandler.send_data()` writes, and the second half answers what is in
the database - `resolve_db`, `run_query`, the query builders, `discover_measurement_keys`,
`discover_tag_values`, and the identifier validation and quoting they rest on.

The read half lived in `toinflux/mcp_read.py` until the control work needed it. A control process reads
its inputs from InfluxDB and is meant to run with the MCP server absent entirely, so importing that module
would have pulled `mcp`, `anyio`, `pydantic`, `starlette` and `uvicorn` into every control process just to
ask what the last temperature reading was.

**Half the injection defence is here, and which half matters.** What moved is query *construction*: a
measurement and its tags come from the static schema, a field must match a live-discovered key, and every
identifier is charset-validated and quoted before it reaches a query string. Never add a query path that
goes around it - a second way to build a query is how the first one stops being the only one.

What stayed in `mcp_read.py` is what a tool *accepts*, because that describes the MCP surface rather than
how InfluxDB is talked to: `parse_time_bound`, which re-emits a time as RFC3339, the aggregation map, and
the schema objects that describe a source to a model.
