<!-- Architecture note: implementation detail for contributors and assistants.
     Not user documentation - see README.md for that. -->

# Collector internals

Deep detail behind the class hierarchy in [../CLAUDE.md](../CLAUDE.md): the write buffer,
Hue bridge slots, the Nuki device-tag migration, MQTT streaming, and MyEnergi device
selection. Read this before changing anything under `toinflux/` other than the MCP modules.

## Class hierarchy

```
DataHandler      (toinflux/influx.py)          — base; owns send_data() → InfluxDB HTTP POST
├── CarbonIntensity(toinflux/carbonintensity.py)
├── Hue            (toinflux/philipshue.py)
├── OpenMeteo      (toinflux/openmeteo.py)
├── Octopus        (toinflux/octopus.py)
├── Speedtest      (toinflux/speedtest.py)
├── MqttDataHandler(toinflux/mqtt.py)        — intermediate parent for MQTT transport
│   └── Nuki       (toinflux/nuki.py)
└── MyEnergi       (toinflux/myenergi.py)     — intermediate parent for MyEnergi API auth
    ├── Zappi      (toinflux/myenergi.py)
    ├── Eddi       (toinflux/myenergi.py)
    └── Harvi      (toinflux/myenergi.py)
```

Each subclass implements `get_data()` which populates `self.data` (dict) and `self.influx_header` (InfluxDB measurement/tag string); `send_data()` in the base class takes it from there. Points are written with an explicit unix-epoch-seconds timestamp: `self.timestamp` if `get_data()` set it (e.g. Octopus uses the reading's own `interval_start` so re-writes of the same reading overwrite rather than duplicate), otherwise the time `send_data()` is called. Field keys are escaped per line protocol rules (commas, `=`, spaces).

If a write to InfluxDB fails, `send_data()` buffers the point in memory instead of dropping it (raising `InfluxWriteError` either way, so the worker's existing backoff/retry is unaffected) - see `DataHandler._write_buffers`: a per-*worker* `deque(maxlen=MAX_BUFFERED_POINTS)` of `[line, rejection_count]` entries, class-level rather than an instance attribute because the worker loop in `sendtoinflux.py` discards and reconstructs the `DataHandler` instance after every failure, so only a buffer that outlives the instance survives to be flushed. Keyed by `DataHandler.worker_key` - the `(source, instance)` tuple - not by source name alone: a source with several instances (a Hue install with more than one bridge) runs one worker per instance, and they must not share a deque, since `_flush_buffer`/`_flush_head` do read-then-`popleft` sequences that are not atomic across threads. `instance` is `None` for every single-target source, so their key is `(source, None)` and their behaviour is unchanged. Note the `maxlen` bound is therefore per worker, so N instances can hold up to N × `MAX_BUFFERED_POINTS` between them. `worker_key` is a tuple rather than a joined `source@instance` string for two reasons: an instance may be an IPv6 literal (so any delimiter is ambiguous to a later split), and callers that need the settings-block name back out - the stall watchdog reads `settings[source]["interval"]` - must not have to re-parse it. `worker_label` (`source` or `source@instance`) is the display-only counterpart, used in log messages and never as a key or an emitted tag value. Every buffered-path `send_data()` call flushes the backlog first - including calls with no data of their own (an empty reading still delivers the backlog; only an empty-buffer-and-empty-data call skips the HTTP round trip entirely) - in newline-joined chunks of `FLUSH_CHUNK_SIZE` per POST (InfluxDB's write endpoints accept multi-point bodies natively, so a 500-point recovery costs ~5 requests, not 500). If a source's outage runs long enough to fill its buffer, the oldest buffered point is dropped to make room, logged as a warning; an identical line already in the buffer is never added twice (Octopus re-serves the same reading/timestamp for ~30 min, and duplicates would only waste capacity since flushing is an idempotent overwrite). Not persisted across a process restart, and flushed to whatever destination the *current* settings resolve to (editing `influx.url`/bucket/db mid-backlog re-routes the backlog - accepted limitation, documented in the `_write_buffers` comment).

Failure classification is deliberately **not** trusted per-status-code as a verdict: `InfluxWriteError.status_code` carries the HTTP status (or `None` for a connection failure), and `_flush_buffer()`/`_flush_head()` count how many times the server has *rejected* (a non-transient 4xx - 408/429 are excluded via `TRANSIENT_CLIENT_ERRORS`, since rate-limiting/timeouts say nothing about the point) each specific point, dropping it with a warning only after `MAX_POINT_REJECTIONS` separate rejections. Connection failures, 5xx, 408, and 429 never count, so an arbitrarily long outage or rate-limit burst can't age points out - only a point the server itself keeps refusing (malformed → 400, outside the retention window → 422 on InfluxDB v2, oversized → 413) is given up on, and a middlebox transiently answering 4xx for a down InfluxDB can't mass-discard the backlog (each point survives `MAX_POINT_REJECTIONS` attempts). When a batched chunk is rejected, the flush falls back to per-point posting for that chunk to isolate the offender(s). Heartbeat writes pass `use_buffer=False` - a heartbeat is a live signal with no replay value, so it neither consumes buffer capacity nor triggers a redundant second flush per failed cycle. `validate_settings()` rejects duplicate entries in `sources:` (`ConfigError`) - two workers for one source name would share (and race on) one buffer.

Hue builds every bridge request URL through one method, `Hue._api_base()` (`https://<host>/api/<user>`),
shared by the read path (`get_data_from_hue_bridge`) and the MCP write path (`_put_light_state`). The
host passes through `_url_host()`, which brackets a **bare IPv6 literal** - `https://2001:db8::1/...`
is ambiguous, since everything from the first colon parses as a port, so an unbracketed address failed
every request until this was fixed. Hostnames, IPv4 literals and already-bracketed values are returned
unchanged, so it is idempotent and safe to apply unconditionally. Bracketing is deliberately a
URL-construction concern only: `get_data()` still tags the point with `self.settings['hue']['host']`
verbatim, because normalising the tag would change the series identity for anyone already running an
IPv6 bridge. The single shared `_api_base()` matters more than it looks - the bug existed in *two*
copies of the same f-string, so a second copy is exactly how one path would silently keep it.

**Bridge slots.** `enumerate_bridges()` in
`toinflux/philipshue.py` is the single source of truth for "which Hue bridges are configured", shared by
`validate_settings()`, the worker spawner and the CLI modes - two separate implementations would
eventually disagree about what runs.
Slot 1 is the unnumbered `host`/`user` pair every install has always had; further bridges are
`hostN`/`userN`, uncapped. **Slot numbers carry no ordering, need not be contiguous, and nothing ever
renumbers** - the slot number *is* the binding between a host and its token, so a vacated slot stays
vacant rather than shifting the ones above it down onto the wrong credentials (which fails silently:
the surviving bridge authenticates with the departed bridge's token and presents as a bad token, not a
config error). `bridge_field_names()` is the only place that knows the numbering, so callers never
build `f"host{n}"` themselves. **The severity split is load-bearing:** self-contradictory config is a
fatal `ConfigError` (a non-canonical slot field like `host1`/`host02`, a non-string host, two slots
addressing the same bridge - compared via `_comparable_host()`, which normalises IPv6 spelling and
hostname case *for comparison only*), while "not usable yet" - no host, or a host with a blank/
placeholder/unsubstituted-sentinel token - is only a **warning**, because `example_settings.yaml` ships
`hue` in `sources:` next to the placeholder token, so a fresh install is exactly that state and raising
would stop *every* collector and break the packaging suite's invariant that the example's placeholders
pass validation while workers merely retry. A leftover `userN` with no `hostN` is DEBUG only - that is
the resting state after `--remove`, which blanks the token and leaves clearing the host as a separate
step. Warnings are opt-in via `validate_settings(..., warn=True)`, used only by `--check-config`:
`validate_settings()` runs inside `load_settings()`, so unconditional logging would repeat per source at
startup and again on every failure-triggered handler rebuild.

**Which bridge a handler collects from.** `Hue.bridge()` resolves `self.instance` (a bridge host) against
`enumerate_bridges()`. `instance=None` means "the first configured bridge", which is what keeps a
single-bridge install - and every caller that constructs a handler without an instance, notably the MCP
tools - behaving exactly as it did before slots existed. `_api_base()` and `get_data()` both build from the
resolved bridge, so a worker uses *its own* bridge's host and token rather than slot 1's. An instance that
matches no configured bridge, a malformed block, or no usable bridge at all raises `ConfigError` - not
`SourceConnectionError` - so a worker whose bridge has gone (or whose token was never set) stops instead of
authenticating in a loop forever; that is where acceptance criterion 6's intent actually lands, per-worker
rather than fatally at load time. The `host` tag is `escape_key_or_tag_value()`d but **never normalised**:
`send_data()` escapes field keys and takes the header verbatim, so a host containing a comma, equals sign or
space would otherwise end the tag set early and silently write a corrupt point - while rewriting the value
would change the series identity of an install already running an IPv6 bridge. `_redact()` covers *every*
configured bridge's token, not just the resolved one, so it stays safe to call from an exception handler
(enumeration cannot raise) and cannot miss a token that arrived from an unexpected slot.

One consequence of validating bridges at all: a credential migrated to systemd-creds reads as *unset* when
`--check-config` is run by hand, because systemd mounts `$CREDENTIALS_DIRECTORY` only for the service. Before
multi-bridge support `validate_settings()` did not look at the `hue` block, so this confusion is new - the
unset-token warning therefore carries an explanatory caveat (`_credstore_caveat()`), added **only when
`CREDENTIALS_DIRECTORY` is unset** (under the service the value really was substituted, so an unset token is
genuinely unset and the note would be misdirection). It is appended to *that warning* rather than reported
once per run, which is the point: emitted per-run it also landed next to "no Hue bridge is configured" - an
absent host, no credential involved - sending the reader to the credential store to look for something that
was never missing. Attaching it to the finding it explains makes that structurally impossible rather than
merely handled, and it reaches the runtime `ConfigError` from `Hue.bridge()` for free, since that reuses the
same warning text.

Because that token sits in the URL path, every Hue error message is passed through `Hue._redact()`
before it is logged *or* raised - `requests` puts the request URL into its exception messages (both
"Max retries exceeded with url: /api/&lt;token&gt;" and "503 Server Error ... for url:
https://host/api/&lt;token&gt;/...", confirmed by reproduction), so without it one unreachable bridge
wrote the token to the journal and `/var/log/send-to-influx.log`, again via the worker loop's own
`Source '%s' failed` line, and - worst - handed it to any connected MCP client, since a
`SourceConnectionError` from a read/write tool is returned to the caller as the tool's error. Only the
token is replaced (with `<redacted>`); host, status and underlying cause survive verbatim, so a failure
is still diagnosable from the log alone. An absent/blank/non-string token short-circuits the
replacement, because `"".replace()` would splice the marker between every character. The wrapped cause
(`raise ... from e`) deliberately still holds the unredacted text - the cause chain must be preserved,
and it is only exposed by printing a traceback, which no path does for these errors. Hue is the only
source needing this: every other source passes credentials via an auth tuple, digest auth, or a header,
never in a URL. Pre-5.2 logs therefore still contain the token - see SECURITY.md for the revoke advice.

Speedtest's `get_data()` additionally rejects an implausible `ping` (>= 5000 ms) as a `SourceConnectionError` rather than writing it. speedtest-cli's `get_best_server()` times each of the 3 latency probes it makes per candidate server with a hardcoded 10-second connection timeout (baked into `SpeedtestHTTPConnection`/`SpeedtestHTTPSConnection`'s constructor default - never overridden by `get_best_server()`, so it applies regardless of the `timeout` passed to `speedtest.Speedtest()`); a probe that doesn't complete within that raises `socket.timeout`, which is caught alongside every other connection failure and penalised with a hardcoded `3600` (seconds) instead of a real sample. The 3 per-server samples (real or penalty) are summed, divided by a fixed 6, and converted to milliseconds - so a real (non-penalised) probe can never contribute more than 10s to that sum, making `(3 * 10 / 6) * 1000 = 5000` ms the true ceiling for a genuine measurement. If every probe to a server fails (observed in practice during a transient network blip), the reported `ping` comes out around 1,800,000 ms instead of triggering an error, and would otherwise be written to InfluxDB as if it were real.

Nuki is the first MQTT-based source: `MqttDataHandler` (`toinflux/mqtt.py`) owns the generic
transport (connect, subscribe from inside `on_connect` - a subscription issued before the CONNACK
completes can be silently lost - collect for a fixed window, disconnect), reading broker config from
the shared top-level `mqtt:` settings block (mirroring `influx:` - the broker and its `mqtt-password`
credential are per-install infrastructure, not per-source). The per-interval snapshot (see
streaming below) works over MQTT only because Nuki publishes every state topic with the retain flag
set, so a short subscribe window receives the full last-known state of every provisioned lock -
equivalent to an HTTP GET. Failure mapping is deliberately strict: bad credentials arrive asynchronously as a failed CONNACK
(never as an exception from `connect()`), and a broker that accepts TCP but never completes the MQTT
handshake raises `SourceConnectionError` rather than returning an empty result - either would
otherwise masquerade as "no data". `Nuki` (`toinflux/nuki.py`) holds only vendor logic: filtering to
known state topics (command/event topics are ignored), grouping by device ID, labelling each lock
with its own Nuki-app name, and renaming `state`/`doorsensorState` to `stateValue`/
`doorsensorStateValue` - Grafana visualises numeric fields far better than text, so unlike the
Bridge HTTP API's `stateName` strings, these are always written as their raw numeric code (a code
with no documented meaning is written through unchanged); see UNITS.md for what each code means.
`paho-mqtt` (a
source-specific runtime dependency like `speedtest-cli`, pure Python so the `.deb`'s
`Architecture: all` design holds) is imported only in `toinflux/mqtt.py`.

**Per-lock points (5.3).** `parse_nuki_data()`/`decode_stream_message()` return
`{device: {field: value}}`, and `Nuki.send_data()` writes **one point per lock** tagged
`device=<lock>` with bare field keys, delegating each to the base implementation with the header
swapped in - the same idiom `send_heartbeat()` uses, so buffering, retry and the `InfluxWriteError`
contract are untouched rather than reimplemented. Every lock in one cycle shares a single
timestamp: letting each call default independently would scatter one snapshot across a second or
two, so "what was the state at time T" could see one lock and not another. A failure on one lock
does not stop the rest - each is attempted and one error raised at the end, so the worker still
backs off, and re-writing a lock that already succeeded is harmless because points are idempotent.
`MCP_INSTANCE_TAG = "device"`, and `MCP_LIVE_STATE_COVERS_ALL_INSTANCES = True` because Nuki is the
only source whose *one* live read covers every producer (a single retained-state subscription
returns all locks), unlike Hue where each bridge needs its own handler.

The broker `host` tag was **dropped** in the same change rather than as a separate series break:
every lock arrives through one broker, so it identified nothing, and moving broker should not start
a new series.

**This was a breaking change to emitted data, and the field-key prefix was the original mistake.**
Before 5.3 every lock's fields were flattened into one shared point with the lock's name built into
each field key (`Front_Door_Lock_stateValue`), which is precisely why the lock could not be queried
as a dimension. Existing history keeps working but sits in different series from the new points.

**The migration** (`scripts/migrate-nuki-device-tag.py`, shipped to
`/usr/share/send-to-influx/` and documented in `UPGRADING.md`) converts it. Notes that cost real
debugging:

- **It cannot be documented InfluxQL instead.** There is no UPDATE; `SELECT ... INTO` preserves
  existing tags via `GROUP BY *` but has no syntax to set a tag to a *new literal*, which is the
  entire job; and the lock name lives in the field *key*, which InfluxQL has no expression to
  operate on.
- **Two phases, separately invoked.** Phase 1 is non-destructive by construction - the new format
  is a different series - so old and new coexist and backing out means not running phase 2. Phase 2
  is driven by a manifest phase 1 writes and scoped by the old `host` tag, so it can only drop what
  phase 1 confirmed it carried across. An earlier version did an unscoped `DROP SERIES FROM "nuki"`,
  which would have destroyed the migration's own output.
- **It reads no credentials on any install type**, deliberately and universally. A fallback to
  `settings.yaml` where it happens to be readable would make the safeguard depend on how you
  installed rather than being real. It also needs `requests`, which the package keeps in its venv
  rather than as a system dependency, so the documented invocation is
  `/opt/send-to-influx/venv/bin/python3` - a bare `python3` fails where `python3-requests` is absent.
- **Its value formatter duplicates `_format_field_value()` and must stay identical.** It emitted the
  `i` integer suffix at first; a field's type is fixed by its first write, so that established these
  fields as integer and every subsequent *collector* write then failed with a 400 type conflict. The
  migration would have broken the running collector. Only writing both outputs to one real InfluxDB
  surfaces it, and a test now pins the equivalence for every value shape. It is deliberately
  *stricter* in one respect: a numeric value on a text field is still quoted, closing the same
  failure by the other route.
- **Its field list is a superset of the collector's and was hand-copied wrongly.** Listing a real
  database's field keys found `stateName`/`doorsensorStateName` - text state names an earlier
  release wrote before the numeric rename - still holding years of points and absent from the copy,
  so the migration halted on the very data it exists to rescue. Had the halt not been there, phase 2
  would have deleted them. This is exactly why a migration is tested against real data from the
  previous release rather than a fixture matched to its own assumptions.
- **The split is longest-known-suffix, and the underscore in the comparison is load-bearing** (a key
  merely *ending* in a field name is not one of ours and must halt). Longest-match itself is
  currently unreachable - two known fields could only both match if one ended with `_<the other>`,
  and none contains an underscore - so no test kills it; it is documented as the guard for a future
  underscored field rather than left looking tested.
- **Halting beats skipping.** An unrecognised field key stops the run with nothing written, because a
  skipped key is data silently left behind that phase 2 would then delete, and it would look like
  success.

**`Nuki.send_data()` must not capture every caller, and the discriminator is the payload's
shape.** `send_heartbeat()` sets its own `collector_status` header and passes a flat
`{field: value}` dict through the *same* `send_data()`; the streaming path passes per-device data
explicitly. So "was `data` given?" cannot tell them apart - `_is_per_device()` decides on every
value being a mapping, since a lock always carries a dict of fields and a field never does.
Getting this wrong treated `ok`/`consecutive_failures` as lock names whose scalar values were
then skipped as non-dicts, so **Nuki wrote no heartbeat at all**, silently, with only warnings -
exactly the silent gap the heartbeat exists to prevent. Found in review, not by the tests, because
every existing heartbeat test used a `MagicMock` handler: a mock's `send_data` never runs the
source's own override, so those tests assert what `send_heartbeat` *asked for* and never what the
handler *did*. `test_every_source_actually_writes_a_heartbeat_point` now drives a real handler per
source down to the HTTP boundary - written across every source rather than just Nuki, because the
break was one subclass violating a shared contract and the next override would break it the same
way.

- **External values are named with `!r` in every message, never raw.** A lock name comes from the
  retained MQTT `name` topic, and one containing a newline turned the per-lock failure message
  into *two* journal lines - the worker logs `Source '%s' failed: %s`, so a forged entry with its
  own timestamp and ERROR level appeared as though the daemon had written it, and the same text
  reaches an MCP client as a tool error. `escape_key_or_tag_value`'s own message was already safe
  for exactly this reason; the prefix wrapped around it was not. Swept rather than patched at the
  one reported line: the same shape existed in `mcp_write`'s unreachable-bridge list and
  `mcp_read`'s all-instances-failed message, both of which reach a client. The name is still
  reported, just escaped - a failure has to stay diagnosable from its output alone.

- **The backlog is flushed once per cycle, not once per lock.** The write buffer is keyed by
  *worker*, so calling the base `send_data()` per lock flushed it per lock too, charging the head
  buffered point one rejection each time. With `MAX_POINT_REJECTIONS` at 5, a five-lock install
  burned the whole allowance in one cycle and discarded the backlog after a single cycle instead
  of five - defeating the documented guarantee that a middlebox answering 4xx for a down InfluxDB
  cannot mass-discard it. `DataHandler.send_data()` therefore takes `flush=`, and Nuki passes it
  only for its first lock; every lock still buffers its own point on failure, only the flush is
  shared. Measured before and after (1/3/3 charged, five dropped outright; now 1 at any lock
  count), and the test asserts the count because the count *is* the property.

- **Statements travel in a POST body, never the URL.** The rewrite phase names every old field
  key in one `SELECT` - one per lock per field - so a ten-lock estate is kilobytes of statement,
  and in a request line a reverse proxy can refuse it, failing the migration on a statement
  InfluxDB would have accepted. The same shape the read layer already hit, which is why
  `build_edge_time_query` selects `*`. POST verified equivalent to GET on real 1.8 and 2.7 for
  every statement this script issues.
- **v2 has no `DROP SERIES`, so phase 2 differs by version.** Its v1-compatibility endpoint
  answers HTTP *200* carrying `{"error": "not implemented: DROP SERIES"}` (verified on 2.7) - so
  the error check catches it rather than mistaking it for success, but it can never succeed.
  Phase 1 works fully on v2, so the operator would be left with migrated data and no way to
  finish; phase 2 therefore translates that one rejection into the `/api/v2/delete` request that
  does work. Built with `json.dumps`, because the predicate's own value contains double quotes
  and hand-assembly produced invalid JSON that would have failed if pasted - the emitted command
  was run verbatim against a real 2.7 (204, old `host=` series gone, migrated `device=` kept).
  Deliberately *not* run automatically: it needs the organisation, which the script cannot know
  and must not guess for a delete that cannot be undone. Only "not implemented" is translated;
  any other failure surfaces as itself.

**Changing emitted data means sweeping `tests/integration/` too, and that is easy to miss.**
Integration tests are deselected from the default `pytest` run (by design - they need a broker),
so a local green run says nothing about them. Worse, running `pytest -m integration` *without* a
broker skips cleanly rather than failing, so it also proves nothing. The Nuki device-tag change
left `test_mqtt_streaming.py` asserting the old prefixed field key *and* `startswith("nuki,host=")`
- the exact tag the change removed - and only CI caught it. When a change alters a measurement,
tag set or field key: grep `tests/integration/` for the old names, and run that suite against a
real broker (`MQTT_TEST_BROKER_HOST`/`MQTT_TEST_BROKER_PORT` point it anywhere, so a throwaway
`eclipse-mosquitto:2` container is enough). Then mutate the product back and confirm the test
fails, since an assertion that survives the old behaviour was never testing the new one.

**Streaming (5.1):** MQTT sources are event-driven, not timer-polled. `MqttDataHandler` sets
`STREAMING = True` and `stream_mqtt_messages()` holds the subscription open, so a state change is
written the instant its (retained) message arrives rather than only when a poll happens to land on
it. The paho network thread only *enqueues* decoded messages onto a bounded `queue.Queue`; a single
worker thread (`_run_stream_loop`) drains it and does *all* InfluxDB I/O - both the immediate
per-message write and the periodic snapshot - so a slow write can never stall paho's keepalives
(which would drop the connection and lose exactly the transient events streaming exists to capture).
On overflow the oldest queued message is dropped (freshest state wins; the snapshot resyncs full
state anyway). In `sendtoinflux.py`, `_should_stream()` gates the stream path on `STREAMING` *and* a
non-`None` `STREAM_TOPIC_FILTER`, so an MQTT transport with no concrete source wired yet keeps
polling rather than subscribing to `None` and retrying forever; when it's eligible the worker runs a
blocking `stream_source_data()`/`_StreamSink` instead of the poll-then-sleep cycle. The existing
per-`interval` poll is kept as a full-state safety-net snapshot **and** an active health probe:
because it hits the same broker as the live stream, its failure correlates with the stream being
down, so it drives the heartbeat's `ok`/`consecutive_failures` - *unless* a message arrived since the
last tick (a healthy-but-idle lock sends nothing for hours, so the probe proves it live; a live
stream with a flaky one-off probe stays healthy on the message). A failing probe never tears the
stream down (paho reconnects genuine drops, re-subscribing to redelivered retained state); shutdown
is clean on the main thread (single-source) and best-effort for the daemon workers (multi-source).
Emitted data is unchanged (same measurement and field names), so it's a behaviour change but not a
breaking one, and there's no new config - streaming is a property of the transport, not an option.
`Nuki.decode_stream_message()` (with `STREAM_TOPIC_FILTER = "nuki/+/+"`) is the per-message vendor
decode: the event-driven counterpart to `parse_nuki_data`, reusing `_decode_field` and remembering
each device's retained `name` as its field-key prefix (warning on a duplicate-name prefix collision,
as the snapshot path does). The snapshot path itself is untouched, so existing Grafana panels keep
working - just denser.

## MyEnergi multiple devices

Each of `zappi`/`eddi`/`harvi` collects **one worker per configured device**, registered through
`_INSTANCE_ENUMERATORS` like Hue's bridges. `enumerate_devices()` in `toinflux/myenergi.py` is the
single source of "which devices are configured", shared by validation, the worker spawner and the
handler's own `device()` resolution - the same shape as `enumerate_bridges()`, returning
`(devices, errors, warnings)` so the two instanced sources report problems alike.

- **Two config shapes, and both may appear together.** A `serial` at the top of the block is the
  legacy single-device form; its `label` is optional and **defaults to the source name**, which is
  what keeps such an install writing `device=zappi` exactly as before and is why this needed no
  data migration. A `devices:` list adds more, each naming its `label` explicitly - there is no
  sensible default for a second device, and deriving one from the serial would give exactly the
  unreadable tag values that tagging by label exists to avoid. `fields` resolves device-first,
  then block-level, then everything the API returns.
- **Labels are the emitted `device` tag and must be unique across all three blocks.** The types
  share the `myenergi` measurement, so a zappi and an eddi agreeing on a label would merge into
  one series carrying both devices' fields. Checked whenever any of the three is selected, never
  per block, because per-block checking misses precisely the collision that matters.
- **`MCP_TAG_FILTERS` on the three subclasses is gone**; `mcp_tag_filters()` supplies
  `{"device": <this device's label>}` per instance. That method also carries the type
  discrimination the old static filter provided: without a device filter, a read of the
  `myenergi` measurement returns all three types.
- **`shares_measurement()` decides whether discovered tag values can be trusted.** Once `device`
  carries an arbitrary label, a value found in the data cannot be attributed to a type - so for a
  shared measurement the *configured* devices are the allowlist and `discover_tag_values()` is not
  even called. The config is the authority precisely because it does distinguish the types. A
  source owning its measurement still unions discovered with configured. Reported series are then
  filtered to the allowlist, one rule that reads correctly for both. Consequence: a decommissioned
  MyEnergi device's history stops being reachable by label where a Hue bridge's does not.
- **`heartbeat_tags()` is overridden** to tag `device`, not the base's `host`: a MyEnergi instance
  is a device label, and a health series tagged differently from the measurement it reports on
  cannot be joined to it. This adds a tag to a legacy install's heartbeat where there was none -
  a deliberate emitted-data change on a liveness signal, noted in UNITS.md.
- **`worker_label()` collapses an instance equal to the source name.** A legacy install's label
  defaults to the source name, so without this every log line would read `zappi@zappi`.
  `worker_key` keeps the instance, being an identity rather than a label.
- **`myenergi.auth_serial` optionally overrides the digest username**, defaulting to the device's
  own serial as every install already sends. The credential is account-scoped - the real zappi
  serial authenticates against all three endpoints, verified live - but that is *evidenced, not
  proven* for a second device of one type, since the test account has one zappi. The override
  exists so discovering otherwise needs no config change.

## MyEnergi device selection (`toinflux/myenergi.py`)

The status endpoints are per device **type** (`cgi-jstatus-Z`/`-E`/`-H`) and each returns *every*
device of that type on the account, so the configured `serial` is what picks one out -
`_select_device()`. It used to take index 0, which had two consequences on one line: a second device
of the same type was silently never collected whichever serial was configured, and an account owning
none of that type raised `IndexError`, which the worker loop's broad handler caught and retried
forever logging only "list index out of range".

- **`sno` is the serial field**, confirmed against the live API as the only key in a device object
  whose value equals the configured serial. `deviceClass` and `productCode` are also present if type
  identification is ever wanted.
- **Both sides are compared as strings.** An all-digit serial in `settings.yaml` is an `int` unless
  quoted, so a raw comparison would never match and would present as a wrong serial rather than a
  type mismatch.
- **The two failure modes are deliberately different exception types**, because one is worth retrying
  and the other never is. No device of that type is a `SourceConnectionError` (a device can
  legitimately be mid-provisioning, and an absent response key is not distinguishable here from a
  temporary API oddity); devices present but none matching the serial is a `ConfigError`, since the
  account is reachable and the type exists so the serial is simply wrong - which stops that worker
  rather than backing off forever. Swapping either type is mutation-tested.
- The `ConfigError` **names the serials the account does report**, which is the difference between a
  message the operator can act on and one that only says no. A missing response key is treated as an
  empty list rather than allowed to raise `KeyError`, which would escape the same exception contract
  the `IndexError` escaped.
- Note `sno` is written as a field on any install with no `fields` list configured, since the whole
  device dict is returned then. Long-standing behaviour, not introduced here, but worth knowing
  before adding a `fields` list changes what a dashboard sees.
