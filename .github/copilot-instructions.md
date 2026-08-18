# Copilot Instructions for send-to-influx

## Project Overview
send-to-influx is a Python application that collects data from smart-home and energy-monitoring devices into InfluxDB, and (introduced in 5.0) exposes their live and historical state - plus control of the ones that support it (Hue lights/plugs, on-demand Speedtest runs) - in natural language through Claude via an optional built-in MCP server. Landing the data in InfluxDB keeps it available for Grafana and other consumers too. The project uses a modular architecture that makes it easy to add new data sources.

Contributor-facing project structure and conventions live in [CONTRIBUTING.md](../CONTRIBUTING.md); see also [CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md), [SECURITY.md](../SECURITY.md), and [PRIVACY.md](../PRIVACY.md).

## Architecture

### Main Application (`sendtoinflux.py`)
- **Entry Point**: Command-line script with signal handling for graceful shutdown
- **Source Selection**:
  - `--source <name>` runs a single source
  - if `--source` is omitted, starts one worker per entry in `sources` from `settings.yaml`
- **CLI Modes**: 
  - `--dump`: One-time data export (JSON format)
  - `--print`: Continuous monitoring with JSON output to console
  - `--version`: print version and exit (parsed before settings are loaded, so no `settings.yaml` is required)
  - `--check-config`: validate `settings.yaml`, print `Configuration OK`, exit 0 (or 1 with details if invalid)
  - `-v`/`--verbose`: force `DEBUG`-level logging, overriding the `loglevel` settings.yaml key
  - Normal mode: Continuous data collection and transmission to InfluxDB
  - `--settings <path>`: use a settings file at a path other than `settings.yaml` in the project root
- **Timing**:
  - per-source interval-based timing system to avoid drift
  - multi-source startup stagger via optional `stagger_seconds` setting (default `10`)
- **Resilience**:
  - transient failures (`SourceConnectionError`) are retried with exponential backoff (base `5s`, max `300s`) in either single-source or multi-source mode; in multi-source mode, only the failed source is retried, others keep running
  - configuration problems (`ConfigError`) are not retried: single-source mode exits immediately with code `1`; in multi-source mode that source's worker stops permanently (logged as critical) while other sources keep running
- **Signals**: handles both SIGINT (Ctrl-C) and SIGTERM (systemd/container stop) for graceful shutdown
- **Startup logging**: logs an INFO line with the version and the source(s) that will run, so process (re)starts are visible in the logs
- **Heartbeat**: after every collection cycle, writes a `collector_status,source=<name>` point (fields `ok`, `consecutive_failures`) to InfluxDB via the source's own DataHandler, so a dead collector is visible as `ok=0` rather than a silent gap; skipped in `--print` mode

### Modular Data Sources (`toinflux/` package)
The project uses a plugin-like architecture where each data source is implemented as a separate module:

#### Base Classes
- **`toinflux/general.py`**: `load_settings(settings_file=None)` (loads YAML configuration and returns a dictionary; raises `ConfigError` on missing/invalid YAML; defaults to `settings.yaml` in the project root, overridable via the `--settings` CLI flag), `get_class(source, settings_file=None)` (case-insensitive factory function to instantiate data source classes dynamically; raises `ConfigError` for an unknown source, including `DataHandler` itself, since it's the abstract base, not a selectable source), `configure_logging(logfile=None, loglevel="INFO", log_max_bytes=..., log_backup_count=...)` (sets up timestamped **stderr** logging with an optional rotating file handler; diagnostics go to stderr so stdout carries only the program's data - `--dump`/`--print` JSON and `Configuration OK` - which is what makes `--dump | jq` reliable when a source fails; raises `ConfigError` - not a raw `OSError` - if `logfile` can't be opened for writing)
- **`toinflux/influx.py`**: `DataHandler` (base class for all data sources). `send_data()` buffers a point in memory (`DataHandler._write_buffers`, a per-*worker* `deque(maxlen=MAX_BUFFERED_POINTS)` of `[line, rejection_count]` entries, keyed by `worker_key` = the `(source, instance)` tuple - `instance` is `None` for every single-target source; a source with several instances, i.e. a multi-bridge Hue install, runs one worker per instance and they must not share a deque because the flush does non-atomic read-then-`popleft`. The `maxlen` bound is per worker, so N instances can hold N × `MAX_BUFFERED_POINTS`. Use `worker_label` (`source` or `source@instance`) in log messages and `worker_key` for keys - never a joined string as a key, since an instance may be an IPv6 literal) instead of dropping it if the InfluxDB write fails, flushing the backlog (oldest first, in newline-batched chunks of `FLUSH_CHUNK_SIZE` per POST) at the start of every buffered `send_data()` call - including empty-data calls, so recovery isn't gated on the next non-empty reading; still raises `InfluxWriteError` either way, so worker backoff/retry is unaffected. The buffer is class-level (not per-instance) because the worker loop discards and reconstructs the `DataHandler` on every failure, is not persisted across a process restart, and never stores duplicate identical lines. `InfluxWriteError.status_code` carries the HTTP status (`None` on connection failure); a point is dropped only after `MAX_POINT_REJECTIONS` separate server rejections (a non-transient 4xx; 408/429 are excluded via `TRANSIENT_CLIENT_ERRORS`) - connection failures, 5xx, and rate-limit/timeout 4xxs never count, so outages and rate-limit bursts can't age points out, and a middlebox transiently answering 4xx can't mass-discard the backlog. A rejected batch falls back to per-point posting to isolate the offender. Heartbeats pass `use_buffer=False` (no flush, no buffering - live signal, no replay value). `validate_settings()` rejects duplicate `sources:` entries (two workers would race on one buffer).
- **`toinflux/exceptions.py`**: `ConfigError` (fatal, not retried) and `SourceConnectionError` (transient, retried with backoff)

#### Current Data Sources
- **`toinflux/philipshue.py`**: Philips Hue Bridge integration
- **`toinflux/myenergi.py`**: MyEnergi Zappi/Eddi/Harvi devices integration (HTTP Digest auth)
- **`toinflux/carbonintensity.py`**: National Grid carbon intensity and generation fuel mix (no API key)
- **`toinflux/openmeteo.py`**: Open-Meteo weather data (no API key, lat/lon configuration)
- **`toinflux/octopus.py`**: Octopus Energy electricity/gas consumption and unit rates (API key auth)
- **`toinflux/nuki.py`**: Nuki smart lock + door sensor state via the local Nuki MQTT API (persistent streaming subscription + retained-topic snapshot through the shared `toinflux/mqtt.py` transport; read-only, never publishes)
- **`toinflux/speedtest.py`**: Speedtest network performance integration; rejects an implausible `ping` (>= 5000 ms - the ceiling imposed by speedtest-cli's own hardcoded 10s per-probe connection timeout, `(3 * 10 / 6) * 1000`) as a connection error instead of writing it

#### MCP server (`toinflux/mcpserver.py`)
Optional remote MCP server (introduced in 5.0) - NOT a data source/`DataHandler`: a Streamable-HTTP server (official `mcp` SDK, `MCPServer` - `FastMCP` before the SDK's 2.0 rename - + its built-in OAuth 2.1 authorization server) run in a daemon thread by `sendtoinflux.py`'s `maybe_start_mcp_server()` (skipped in `--print`/`--dump`). Enabled iff both `mcp.user` and `mcp.password` are set (one without the other is a `ConfigError`; `mcp_block_errors()`/`mcp_enabled()` in `toinflux/general.py`), unless `mcp.disabled: true` is set, which forces the server off and skips the user/password coherence check entirely - a kill switch independent of credential state, for when the password lives in systemd-creds and blanking the YAML fields alone can't reach a coherent disabled state. Binds `mcp.bind_address` (default `127.0.0.1:8420`) in plain HTTP - validation refuses the any-interface wildcards (`0.0.0.0`/`::`) and any globally-routable public IP with no override (loopback/private allowed; a non-IP hostname warns); TLS is the user's reverse proxy, whose external address is `mcp.public_url` (required when enabled, must be `https://` - OAuth issuer/discovery metadata is built from it, never the bind address). OAuth client registrations + refresh-token SHA-256 hashes persist across restarts in `mcp.state_file` (default `mcp-oauth-state.json` next to settings.yaml, atomic 0600 writes); access tokens are in-memory, 1 h TTL. Resource-owner login page at `/login` (constant-time credential comparison, single-use transaction ids, 5-failures/300 s per-address lockout with WARNING logging). `mcp-password` is a normal `CREDENTIAL_FIELDS` entry whose placeholder is deliberately the empty string. The `mcp` SDK is imported only inside `toinflux/mcpserver.py`, lazily. Transport options (`host`/`port`/`streamable_http_path`/`transport_security`) are per-run in mcp 2.x, not constructor args as in 1.x: `app_options()`/`run_options()` are the one canonical builder every call site derives from, because omitting `transport_security=` silently reverts to the SDK's localhost-only DNS-rebinding default and rejects every reverse-proxied request. The shared per-call handler-lifecycle plumbing (`resolve_handler`/`close_session`/`configured_sources`) lives in `toinflux/mcp_common.py`, which the read and write tool modules both import from (rather than from each other). **Read tools** live in `toinflux/mcp_read.py` (`register_read_tools()` wires `list_sources`, `list_fields`, `query_history`, `get_current_state`, `get_data_range`, `get_documentation` onto the server): domain-aware live + historical state, not a raw passthrough. Per-source domain knowledge is class attributes on the `DataHandler` subclasses - `MCP_MEASUREMENT` (when the measurement isn't the source name: openmeteo->weather, the myenergi trio share `myenergi`), `MCP_TAG_FILTERS` (device tag for the myenergi trio), `MCP_INSTANCE_TAG`, `MCP_FIELD_METADATA` (units + `codes` for numeric-coded fields like Nuki state, keyed by field name or `_`-suffix), `MCP_DESCRIPTION` (one-line "what this source is", on `list_sources`/docs), and `MCP_LIVE_STATE` (default True). `query_history` is trends/"when did X change"; `get_current_state(source)` is "what is X now" - it calls the source's live `get_data()` and decodes via the same `MCP_FIELD_METADATA`, so a coded value reads back as its label. `MCP_LIVE_STATE=False` on Speedtest (its get_data() runs a full test - never on a read) and Octopus (~24h delayed), which instead read the latest InfluxDB point (`build_latest_query`); the result's `state` says `live`/`last_recorded`. `get_data_range(source)` answers "how far back does this go": the oldest/newest points present (`build_edge_time_query`, either edge via `ORDER BY time ASC|DESC`, sharing `_build_single_point_query` with `build_latest_query` but selecting `*` since only `time` is read - enumerating fields made a 120-field measurement a 3.4 KB query string in a GET parameter, and measurements grow with device count) plus the configured retention - v1 via `SHOW RETENTION POLICIES` on the existing query path, v2 via `/api/v2/buckets` and NOT the v1-compat endpoint, which reports the DBRP's `0s`/168h rather than the bucket's real 720h/24h (verified on 2.7) and would claim unlimited retention; no broader token scope is needed since querying a bucket already requires `read:buckets`. A failed retention read degrades to `known: false` with a reason and keeps the range, reported not omitted so it cannot read as "nothing expires". **A tag is either a constant to pin or an axis to enumerate, and they are separate attributes**: `MCP_TAG_FILTERS` pins one value to disambiguate a source in a shared measurement; `MCP_INSTANCE_TAG` names the tag separating *producers* within one source's measurement (`host` on Speedtest). Having only the first is why a two-host Speedtest install got wrong answers - both hosts interleaved in one unlabelled series, `aggregation="mean"` averaging across them, while Grafana honoured the dimension all along. Per source, not one global "collector" tag: the axis means different things (collecting host, bridge, lock, device) and most sources have one producer. `discover_tag_values()` (`SHOW TAG VALUES`) is the exact analogue of `discover_fields()` - the live allowlist an `instance` argument is validated against, so a value never written is refused rather than answering confidently with nothing; discovered not configured, so a new host is queryable with no config change; verified identical on real 1.8 and 2.7's v1-compat endpoint, which matters since that endpoint reports bucket retention as `0s`. **Payload shape depends on the source, never the producer count**: scoped, or no axis, returns flat `points` exactly as before; unscoped with an axis returns `instances` keyed by value, keyed even for one producer (same reasoning as Hue's per-bridge map). Never merged - two hosts' ping in one unlabelled list is a wrong answer, not a partial one. **`LIMIT` is per series once grouped by a tag** (verified: `LIMIT 2` across two hosts returned two rows each, not two total), so N producers would multiply `MAX_RESULT_POINTS` and the cap would stop capping - the limit is divided across known producers and reported as `limit_per_instance`, not `limit`. **The axis is NOT `INSTANCED_SOURCES`**, which names a collector work unit (a Hue bridge with its own credentials/worker) and would reject Speedtest, whose hosts are separate processes: the axis exists in the data without the collector having any notion of instances, hence `query_history` carrying both `bridge` and `instance` until SI-33 unifies them. `get_current_state`/`get_data_range` report per producer too (a host added last week and one collecting for a year share a merged span true of the measurement and false of both). `speedtest_run` names the host it ran on and refuses another - it can only measure the machine the server runs on. `get_documentation()` synthesises a static InfluxDB-free Markdown reference from every source's description + field metadata. Injection defence is layered: measurement/tags from the static class schema, the requested field validated against the live `SHOW FIELD KEYS` allowlist, all identifiers charset-checked and double-quoted, times parsed to RFC3339 in Python, aggregation a fixed name->function map, result size capped. One `GET /query` path serves v1 (basic auth) and v2 (Token header, v1-compat endpoint) - verified against real v1/v2 containers. **Resources** live in `toinflux/mcp_resources.py` (`register_resources()`): the listable counterpart of the read data (rule: anything a resource is also a tool) - `docs://reference` plus per-source `schema://<source>` and `state://<source>`, built from the same public `mcp_read` builders (`build_documentation`/`list_fields_result`/`current_state_result`), registered concretely one per source. **Prompts** live in `toinflux/mcp_prompts.py` (`register_prompts()`): parameterised task templates - `home_status` and `usage_trends` always registered, `control_device` only when a source has writes enabled (same `writable_enabled_sources` gate as the write tools, so a read-only install offers no control prompt). **Write tools** live in `toinflux/mcp_write.py` (`register_write_tools()`): the server is read-only by default; write tools are registered ONLY for sources both `MCP_WRITABLE` (class flag; Hue and Speedtest today) and opted in via `<source>.mcp_read_write: true` (strict `is True`; validate_settings rejects a non-bool). When none is enabled, no write tool exists on the server at all (least privilege, not registered-and-refusing). Writes are heterogeneous, so each writable source gets its own bespoke tool(s) via a per-source registrar in `_WRITE_TOOL_REGISTRARS` (keyed by source name; a write-enabled source with no registrar is logged+skipped, not silently controllable) - not one generic primitive. Hue's `hue_list_devices`/`hue_set_light` (via `mcp_set_device_state`) resolve name→bridge-id against the live device list (unknown/ambiguous refused, never guessed) and are **capability-aware per capability**: brightness/`color_temp_k` (kelvin→ct, clamped to the light's range)/`color` (hex or name→xy) are independent across the white-only/CT/full-RGB tiers, and asking for one a light lacks is a ToolParamError; ct/xy mutually exclusive; brightness 0-100%→bri 1-254 (0%=min-on, off is on=False); setting brightness/temp/colour auto-ons unless on=false; PUTs to /api/{user}/lights/{id}/state over the collector's session/auth. Speedtest's `speedtest_run` (via `mcp_trigger_run`) runs a test on the local host only (a class-level `_run_lock` in `get_data()` enforces one run at a time per host, shared with the collector worker) and records it best-effort. Gets a dedicated /security-review before feature->main.

### Configuration (`settings.yaml`)
YAML-based configuration supporting multiple data sources:
- **Orchestration**:
  - `sources`: list of sources to run in parallel when `--source` is omitted. Empty or absent (and no `--source`) is a valid "nothing configured" state: the process logs that plainly and exits (code 1) rather than falling back to anything - there is no `default_source` fallback (removed outright, no deprecation window)
  - `stagger_seconds`: optional start delay between sources (default `10`)
- **Logging**:
  - `logfile`: optional path to write logs to a file in addition to stderr (rotated automatically)
  - `log_max_bytes`/`log_backup_count`: optional rotation size (default 10 MiB) and backup count (default 3) for `logfile`
  - `loglevel`: optional log level name (default `INFO`); overridden by the `-v`/`--verbose` CLI flag
- **Hue**: Bridge connection, sensor mappings, temperature units
- **MyEnergi**: API endpoints, authentication, device serials (shared across Zappi/Eddi/Harvi)
- **Zappi/Eddi/Harvi**: Field selection, collection intervals, individual device serials
- **CarbonIntensity**: `include_generation` flag; no credentials required
- **OpenMeteo**: Latitude, longitude, field list (see open-meteo.com/en/docs)
- **Octopus**: API key, MPAN, meter serial; optional `gas_mprn`+`gas_meter_serial` for gas consumption, and optional product/tariff codes for unit rate collection
- **Speedtest**: Field selection, collection intervals
- **MCP**: optional `mcp:` block (`disabled`, `bind_address`, `public_url`, `user`, `password`, `state_file`) enabling the remote MCP server when user+password are both set and `disabled` isn't `true` - see the MCP server section above
- **MQTT**: shared broker connection (`broker_host`/`broker_port`/`username`/`password`) used by all MQTT-based sources, like the InfluxDB block; blank username/password = anonymous access
- **Nuki**: `db`, `interval`, and `timeout` (retained-message collection window) only - locks need no per-device config
- **InfluxDB**: Connection details, database/bucket settings; supports v1 (user/password/db) and v2 (token/org/bucket)

## Code Style & Standards

### Python Style
- **Line Length**: 120 characters (Black formatter)
- **Type Hints**: Use where appropriate for function parameters and return types
- **Docstrings**: Comprehensive docstrings with parameter and return type documentation
- **Naming**: Meaningful variable and function names following PEP 8
- **Complexity**: Maximum complexity of 10 (flake8 configuration)

### Error Handling
- **Exit Codes**:
  - `0`: Normal exit
  - `1`: Configuration errors (missing/invalid settings.yaml)
  - `2`: Connection errors (API endpoints, InfluxDB) - only in `--dump` mode; continuous mode always retries connection errors with backoff instead of exiting
- **Error Messages**: Logged via Python's `logging` module with timestamps and log level (WARNING, ERROR, CRITICAL)
- **Network Handling**: Proper timeout handling and connection failure management
- **Validation**: Configuration validation before processing

## Development Guidelines

### Adding New Data Sources
1. **Create Module**: Add new file in `toinflux/` directory (e.g., `toinflux/newsource.py`)
2. **Implement Class**: Create class inheriting from `general.DataHandler`
3. **Required Methods**:
   - `get_data()`: Return processed data as dictionary
   - `send_data(data)`: Send data to InfluxDB (inherited from base class)
4. **Configuration**: Add corresponding section to `settings.yaml`
5. **MCP write (only if controllable/actionable)**: set `MCP_WRITABLE=True` + implement the vendor write method(s) on the class (see Hue's `mcp_set_device_state`/`mcp_list_writable_devices` or Speedtest's `mcp_trigger_run`) + add a per-source registrar to `_WRITE_TOOL_REGISTRARS` in mcp_write.py, add `<source>.mcp_read_write` (bool, default false) to example_settings.yaml; most sources are read-only and skip this. Every `@server.tool()` needs a `title=` distinct from its name and `annotations=ToolAnnotations(...)` with an explicit `read_only_hint` (plus `destructive_hint` when false) - enforced by tests in test_mcp_read.py/test_mcp_write.py
6. **MCP read metadata**: set `MCP_FIELD_METADATA` (units + `codes` for coded fields, from UNITS.md) and a one-line `MCP_DESCRIPTION`; `MCP_MEASUREMENT`/`MCP_TAG_FILTERS` if the InfluxDB measurement isn't the source name or is shared; `MCP_INSTANCE_TAG` only if several *producers* write to one measurement and a tag tells them apart (Speedtest's `host`) - that makes the read tools enumerate it, accept `instance`, and report per producer rather than merging, and such a source should also override `heartbeat_tags()` so its `collector_status` points are attributable to one writer; leave `MCP_LIVE_STATE` True unless `get_data()` is expensive/pointless to call live (then False → current-state reads the latest InfluxDB point) - the read tools and resources then expose the source automatically
7. **Documentation**: Update docstrings and comments

### Configuration Schema
Each data source should have its own section in `settings.yaml`:
```yaml
newsource:
  # API endpoint
  url: "https://api.example.com/endpoint"
  # Authentication
  api_key: "your_api_key"
  # Collection settings
  interval: 300
  timeout: 5
  # Source-specific settings
  fields:
    - "field1"
    - "field2"
```

### Error Handling Patterns
```python
import logging
from toinflux.exceptions import SourceConnectionError

try:
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
except requests.exceptions.RequestException as e:
    logging.error("Error connecting to API - %s", e)
    raise SourceConnectionError(str(e)) from e
```

## Current Data Sources

### Philips Hue (`toinflux/philipshue.py`)
- **Supported Sensors**:
  - `ZLLTemperature`: Temperature sensors (converted to C/F/K)
  - `ZLLLightLevel`: Light level sensors (converted to lux)
  - `ZLLPresence`: Motion/presence sensors (converted to 0/1)
- **Lights**: Brightness percentage (0-100) or boolean on/off (0/1)
- **Configuration**: Bridge host, username, sensor name mappings, temperature units
- **Work units**: every mode expands source names into `(source, instance)` units via `expand_sources()` - one per worker, same shape as `worker_key`. `INSTANCED_SOURCES` (only `hue`) expands to one unit per configured bridge, each with its own thread/backoff/buffer. `--source`, the supervisor and `--dump` all use that one function so they can't disagree. No units at all ⇒ log "Nothing to collect" and exit 1, never idle-but-healthy. Startup logs `workers=` (per-bridge labels), not `sources=`. `run_workers()` staggers across the expanded list and keys restart/stall bookkeeping by unit; `run_one_worker()` keeps the main thread for a single unit (clean streaming shutdown). `--dump` keys its JSON by instance whenever the source is instanced, prints what succeeded and exits 2 if any bridge failed. The heartbeat adds a `host` tag for an instanced worker, escaped - `collector_status,source=hue,host=<bridge>`.
- **Bridge slots**: `enumerate_bridges()` is the single source of truth for which bridges are configured - `validate_settings()`, the worker spawner and the CLI modes all call it, never their own scan. Slot 1 is the unnumbered `host`/`user`; further bridges are `hostN`/`userN`, uncapped, non-contiguous, and **never renumbered** (the slot number is the host↔token binding; shifting slots would silently pair a bridge with another's token). Build field names only via `bridge_field_names()`. Severity split matters: self-contradictory config (non-canonical slot like `host1`, non-string host, duplicate hosts) is fatal; "not usable yet" (no host, or blank/placeholder/sentinel token) is only a **warning**, because `example_settings.yaml` ships `hue` in `sources:` with the placeholder token, so raising would stop every collector on a fresh install. Warnings are opt-in (`validate_settings(..., warn=True)`, `--check-config` only) since `validate_settings()` runs inside `load_settings()` on every handler construction.
- **Slot credentials**: `hue-user2`, `hue-user3`, … are uncapped credentials. Never test `CREDENTIAL_FIELDS` membership directly - use the shared predicate in `credentials.py` (`credential_field`, `credential_name_for`, `is_credential_field`, `placeholder_for`, `slot_credential_names`), or a slot slips past. Two consumers are security-relevant: `_cmd_set_field`'s refusal (else `--set-field hue.user2 <token>` writes plaintext) and `_contains_real_secret` (else that token is invisible to the permissions check). `CANONICAL_SLOT_SUFFIX_RE` lives in `credentials.py` so the settings and credential sides cannot disagree. `_rewrite_settings_field` creates a missing `hue.hostN`/`hue.userN` (gated by `_is_creatable_field` - the refusal is the typo guard), inserting at `_last_scalar_line()` rather than the section's `end_mark` (which sits past the next section's leading comment), double-quoted like every other written value.
- **Heartbeat tags come from the source**: `collector_status` extra tags via `DataHandler.heartbeat_tags()` - an instanced source tags its bridge, `Speedtest` tags the collecting machine via `Speedtest.collector_host()`, which its *data* uses too so the two cannot drift (a health series tagged differently from the measurement it reports on is unjoinable to it). Until this existed every Speedtest host wrote `collector_status,source=speedtest` and overwrote the others at second precision, so a dead collector looked exactly like a healthy estate. Adding the tag is a deliberate emitted-data change: pre-change heartbeat points sit in an untagged series, accepted for a liveness signal whose old data was already wrong.
- **MCP reads across bridges**: `get_current_state` groups per bridge under `instances` (keyed whenever the source is instanced, even for one bridge; a single-*target* source keeps the flat `fields`/`as_of`) because two bridges can share a field name. One failing bridge gets an `error` entry and the others still report; all failing raises `SourceConnectionError`. **Scoping a history read goes through the shared instance mechanism, not a Hue-specific path** (SI-33): Hue sets `MCP_INSTANCE_TAG = "host"`, so `query_history`'s `instance` scopes to one bridge exactly as it scopes Speedtest to one host - the handler is resolved *unscoped* and the filter applied at the query, replacing the older `resolve_schema(instance=...)` route that added the tag via `Hue.mcp_tag_filters()`. That override still exists and is still load-bearing (it forces `Hue.bridge()` to resolve so `resolve_handler` refuses an unconfigured bridge), but reads no longer scope through it. `bridge` is the **deprecated** spelling of `instance` (`_resolve_deprecated_bridge`): deprecated 5.3, removed 6.0, emitting a real DeprecationWarning *and* logging, since that warning class is suppressed by default in an application and would never reach an operator running the service; both spellings with different values is refused as ambiguous; the result echoes back whichever spelling was used. **An unscoped Hue query now reports per bridge rather than merging** - a deliberate reversal of the old span-everything default, because two bridges can each hold a "Kitchen" so a merged series is a wrong answer. **The instance allowlist is the union of values present in the data and targets currently configured** (`configured_instances()` via `expand_sources()`): discovered alone would refuse a bridge configured but not yet collecting (which `bridge` accepted) and leave query_history disagreeing with get_current_state, which reads live from config; neither half suffices, since a decommissioned bridge still has queryable history and a new one has config but no data. The refusal says *accepted* values, never *recorded* - the union includes targets that recorded nothing.
- **MCP writes across bridges**: both Hue write tools use `resolve_handlers()` (`mcp_common.py`) - one handler per configured bridge, from the same `expand_sources()` the collectors use. `hue_list_devices` labels each device with its `bridge` and lists an unreachable bridge under `unreachable` (never omit it silently). `hue_set_light` takes an optional `bridge`; `_resolve_hue_target()` refuses any device matching more than one light across the estate rather than guessing - light ids repeat on every bridge, names often do too, and actuating the wrong light is unrecoverable. Arbitration belongs in `mcp_write.py` (tool parameters), not on the Hue class (which already resolves within one bridge). The write opt-in stays per source, not per bridge.
- **Which bridge a handler serves**: `Hue.bridge()` resolves `self.instance` (a bridge host) via `enumerate_bridges()`; `instance=None` means the first configured bridge, which keeps single-bridge installs and the MCP tools (which construct handlers without an instance) behaving as before. `_api_base()`/`get_data()` build from the resolved bridge, never slot 1. An unknown instance, a malformed block, or no usable bridge raises `ConfigError` (not `SourceConnectionError`), so that worker stops rather than looping on a doomed login. The `host` tag is escaped via `escape_key_or_tag_value()` but never normalised - `send_data()` takes the header verbatim, so an unescaped comma/space/equals silently corrupts the point, while rewriting the value would change an existing install's series identity. `_redact()` covers every configured bridge's token, not just the resolved one.
- **Error messages**: every Hue error is passed through `Hue._redact()` before being logged *or* raised. The CLIP v1 token is in the URL path and `requests` puts the request URL in its exception messages, so without this one unreachable bridge writes the token to the journal/logfile, again via the worker's `Source '%s' failed` line, and returns it to any connected MCP client (a `SourceConnectionError` from a read/write tool becomes the tool's error). Only the token is replaced with `<redacted>`; host/status/cause stay verbatim so failures remain diagnosable. An absent/blank/non-string token skips replacement (`"".replace()` would splice the marker between every character). The wrapped cause still holds the unredacted text by design - preserve the cause chain; nothing prints a traceback for these. Never add a new Hue error path that logs or raises a `requests` exception without `_redact()`. Hue is the only source affected - all others use an auth tuple, digest auth, or a header.
- **Request URLs**: built only via `Hue._api_base()` (`https://<host>/api/<user>`), shared by the read path (`get_data_from_hue_bridge`) and the MCP write path (`_put_light_state`). The host goes through `_url_host()`, which brackets a bare IPv6 literal (`https://2001:db8::1/...` is ambiguous - everything after the first colon parses as a port - so an unbracketed address failed every request until this was fixed); hostnames, IPv4 literals and already-bracketed values pass through unchanged, so it is idempotent. Bracketing is a URL concern only: `get_data()` still tags the point with the configured host verbatim, since normalising the tag would change series identity for an existing IPv6 install. Never reintroduce a second copy of that f-string - the bug existed in two copies, which is how one path would silently keep it.

### MyEnergi (`toinflux/myenergi.py`)
- **Devices**:
  - `Zappi` (EV charger): real-time status fields + daily energy totals (Charge/Import/Export/Genera)
  - `Eddi` (hot water diverter): real-time status fields (frq, vol, div, sta, hno, che, tp1, tp2)
  - `Harvi` (CT clamp monitor): CT clamp power readings (ectp1/ectp2/ectp3) and channel names
- **Authentication**: HTTP Digest authentication with device serial/API key
- **Configuration**: Shared `myenergi` block (API endpoints, apikey) + per-device block (serial, fields, interval)

### National Grid Carbon Intensity (`toinflux/carbonintensity.py`)
- **No API key required**; data updates every 30 minutes
- **Collects**: `intensity_actual` and `intensity_forecast` (gCO2/kWh)
- **Optional**: generation fuel mix (`gen_gas`, `gen_wind`, `gen_solar`, etc.) via `include_generation: true`
- **InfluxDB measurement**: `carbonintensity,source=national_grid`
- API docs: https://carbon-intensity.github.io/api-definitions/

### Open-Meteo (`toinflux/openmeteo.py`)
- **No API key required**; free, no rate limiting
- **Configuration**: latitude, longitude, list of `current` weather variable names
- **Recommended interval**: 900 s (15 min) or longer
- **InfluxDB measurement**: `weather,source=open-meteo`

### Octopus Energy (`toinflux/octopus.py`)
- **Collects**: latest half-hourly electricity consumption; optionally gas consumption and current unit rate
- **Authentication**: HTTP Basic auth with API key as username
- **Configuration**: `api_key`, `mpan`, `meter_serial`; optional `gas_mprn`+`gas_meter_serial` for gas; optional `product_code`+`tariff_code` for unit rate
- **Note**: smart meter consumption data typically arrives with up to 24 hour delay
- **Note**: gas consumption unit depends on meter type (kWh for SMETS1 Secure, m3 for SMETS2) and is sent unconverted as `gas_consumption`
- **InfluxDB measurement**: `octopus,source=octopus_energy`

### Nuki Smart Lock (`toinflux/nuki.py`)
- **Collects**: lock state and door-sensor state as numeric codes (`stateValue`/`doorsensorStateValue`), battery/keypad/door-sensor battery flags, connectivity flags, per provisioned lock
- **Transport**: local MQTT broker (shared `mqtt:` settings block) via `MqttDataHandler` (`toinflux/mqtt.py`). **Streaming (5.1):** `STREAMING = True` + `STREAM_TOPIC_FILTER = "nuki/+/+"` hold a persistent subscription open, so `Nuki.decode_stream_message()` writes a point the instant a (retained) state message arrives; the per-`interval` poll is kept as a full-state snapshot *and* an active health probe. The paho net thread only enqueues onto a bounded queue; one worker thread drains it and does all writes (immediate + snapshot), so a slow write can't stall keepalives. `sendtoinflux._should_stream()` (STREAMING + a filter) gates it, so an unwired MQTT transport keeps polling. Emitted data unchanged (same measurement/field names) - behaviour change, not breaking; no new config
- **Configuration**: `db`, `interval` (snapshot/heartbeat cadence), `timeout` (snapshot collection window); field keys are prefixed with each lock's Nuki-app name (remembered from the retained `name` topic; a duplicate-name prefix collision warns)
- **Note**: read-only - command/event topics are filtered out and never published to; `state`/`doorsensorState` are renamed to `stateValue`/`doorsensorStateValue` and always written as their raw numeric code (Grafana handles numeric fields far better than text) - see UNITS.md for what each code means
- **InfluxDB measurement**: `nuki`

## Dependencies

### Core Dependencies
- `requests`: HTTP requests for APIs and InfluxDB
- `urllib3`: HTTP client library; `InsecureRequestWarning` is suppressed only when the relevant `insecure` setting is true - for the Hue bridge request (`toinflux/philipshue.py`, defaults to insecure) and for InfluxDB writes (`toinflux/influx.py`, defaults to secure)
- `pyyaml`: YAML configuration file parsing
- `speedtest-cli`: Speedtest library for collecting network perf data
- `paho-mqtt`: MQTT client for MQTT-based sources (Nuki); imported only in `toinflux/mqtt.py`, v2 callback API
- `mcp`: official MCP SDK for the optional remote MCP server; imported only in `toinflux/mcpserver.py`. NOT pure Python: needs `pydantic_core`, `rpds-py`, `cffi` and `cryptography` (compiled, no fallback; `cryptography` via `pyjwt[crypto]`, imported unconditionally by `mcp/server/request_state.py` so it is load-bearing). `rpds-py` is held to `~=0.30.0` because 2026.x CalVer releases dropped Python 3.10 wheels. The `.deb` build's compiled-wheel-matrix step fails loudly if wheel coverage regresses

### Development Dependencies
- `black`: Code formatting
- `flake8`: Linting with bugbear and black plugins
- `flake8-bugbear`: Additional linting rules
- `flake8-black`: Black integration for flake8
- `pytest` / `pytest-cov`: Unit test framework and coverage reporting
- `mypy` / `types-PyYAML` / `types-requests`: Static type checking (permissive config, see `pyproject.toml`'s `[tool.mypy]`)

Install runtime requirements with `.venv/bin/pip install -r requirements.txt`, or development requirements (which include runtime) with `.venv/bin/pip install -r requirements-dev.txt`.

## CLI Usage
```bash
# Normal operation for all configured sources in settings.yaml
python sendtoinflux.py

# Normal operation for a single source
python sendtoinflux.py --source hue

# One-time data export
python sendtoinflux.py --source zappi --dump

# Continuous monitoring (console output)
python sendtoinflux.py --source hue --print

# Validate settings.yaml without starting any collectors
python sendtoinflux.py --check-config

# Print the installed version
python sendtoinflux.py --version

# Available sources: hue, zappi, speedtest (and any other implemented sources)
# Multi-source mode uses the settings.yaml `sources` list.

# Use a settings file at a non-default location (e.g. a packaged install)
python sendtoinflux.py --settings /etc/send-to-influx/settings.yaml
```

## Packaging & Deployment

- `pyproject.toml` is the single source of truth for the package version (`[project].version`) and dependencies (dynamically sourced from `requirements.txt`). `sendtoinflux.py`'s `__version__` is read back from installed package metadata via `importlib.metadata`, falling back to `"0.0.0-dev"` when running from an uninstalled source checkout.
- `packaging/deb/build-deb.sh` builds a `.deb` bundling the app and its dependencies into a venv under `/opt/send-to-influx`, with a systemd unit (`packaging/send-to-influx.service`, kept at the top level since it's format-agnostic - the `.deb`-specific files live under `packaging/deb/`) to run it as a service. Package is `Architecture: all` — the venv's `python3` is a symlink to the system-provided `/usr/bin/python3` (`Depends: python3 (>= 3.10), python3 (<< 3.31)`, not bundled), and any optional compiled accelerators pip pulls in (PyYAML, charset-normalizer) are stripped post-install in favour of pure-Python fallbacks; the exceptions - `pydantic_core`, `rpds-py`, `cffi` and `cryptography` (required by the `mcp` SDK, no fallback) - are re-added by a compiled-wheel-matrix step in two shapes. Per-minor packages (`pydantic_core`, `rpds-py`, `cffi`) have the minor AND arch in the `.so` filename, so all (3.10-3.14) x both architectures merge into the shared site-packages and coexist. `cryptography` ships one stable-ABI (`cp39-abi3`) wheel per arch whose `.so` name has NEITHER tag - identically named in both arch wheels - so merging would have one overwrite the other; it is staged as `<name>.so.<arch>` (never un-suffixed) and `postinst` symlinks the one matching `dpkg --print-architecture`, by pattern so a future abi3 dep needs no change. On an arch with no staged variant nothing is linked (collectors unaffected; the MCP server reports its usual "could not be imported" ConfigError). The build fails if any minor/arch combination is missing or an abi3 extension appears un-suffixed. Since everything else left is pure Python, the script also symlinks every minor from 3.10 through 3.30's `lib/pythonX.Y` to the one actually populated, so the package works on any target whose `python3` falls in that range (rather than pinning `Depends:` to the build host's exact minor, which broke once a real target's Python drifted from CI's). Verified on real arm64 hardware by the `arm64-verify` CI job (every push/PR, required status check), which also runs the `packaging/deb/test-packaging.sh` scenario suite - install/upgrade/reconfigure/purge lifecycle against the built package; `bookworm-verify` re-runs the suite in a `debian:12` container for systemd-252 (Raspberry Pi OS bookworm) coverage — see the README's "After installing" section (under "Using the .deb package"). The package also ships rsyslog/logrotate config (`/etc/rsyslog.d/49-send-to-influx.conf`, `/etc/logrotate.d/send-to-influx`, both real dpkg conffiles - the first use of that mechanism here, since unlike settings.yaml no maintainer script rewrites either) mirroring the real haproxy package's own pattern rather than the app managing its own logfile: `:programname, isequal, "send-to-influx"` redirects to `/var/log/send-to-influx.log` and `stop`s further processing, removing these messages from the shared daemon.log/syslog rather than duplicating them - zero app code changes, since journald already forwards stdout to syslog tagged with the program name. `Recommends: rsyslog, logrotate`, not `Depends:` - the service works via the journal alone either way. `postinst` best-effort try-restarts rsyslog on every configure (not just fresh-install, since the config's content can change between releases); `postrm` removes the runtime-created logfile/backups on purge. The MCP server is wired into this flow too: debconf gates it on an `mcp-enable` boolean (not a source selection - it's an interface over all sources), collecting public_url/user/password when enabled; `postinst` `--ensure-section`s the `mcp:` block and only enables it when public_url+user+password all resolve (a partial block fatally stops every collector). public_url/user are required strings that persist and pre-fill on reconfigure (only the secret is cleared, so password-only rotation works). On a failed password store: revert the username if no credential existed yet (fresh enable, enable-then-revert); keep the existing credential and stay enabled if one did (reconfigure) - never disable a running install. Safe because the service only restarts at the end of postinst. The systemd unit gained conservative sandbox hardening (`ProtectKernel*`, `RestrictAddressFamilies`, empty `CapabilityBoundingSet`, `SystemCallFilter=@system-service`; `MemoryDenyWriteExecute` omitted as Python-fragile) since the MCP server made this the first inbound-network service, and `test-packaging.sh` asserts the server actually binds under that hardened sandbox where real systemd is present.

## Configuration Examples

### Hue Configuration
```yaml
hue:
  host: "hue.example.com"
  user: "your_hue_user"
  timeout: 5
  interval: 300
  temperature_units: "C"
  sensors:
    "Hue ambient light sensor 1": "Room1_Light_Sensor"
    "Hue temperature sensor 1": "Room1_Temperature_Sensor"
```

### MyEnergi Configuration
```yaml
myenergi:
  zappi_url: "https://s18.myenergi.net/cgi-jstatus-Z"
  dayhour_url: "https://s18.myenergi.net/cgi-jdayhour-Z"
  apikey: "your_api_key"
  timeout: 5

zappi:
  interval: 300
  serial: "your_zappi_serial"
  fields:
    - "frq"
    - "vol"
    - "gen"
    - "grd"
```

### Multi-source Configuration
```yaml
sources:
  - "hue"
  - "zappi"
  - "speedtest"

stagger_seconds: 10
```

### Speedtest settings
```yaml
speedtest:
  db: "speedtest_db"
  interval: 21600
  fields:
    - "download"
    - "upload"
    - "ping"
```

### InfluxDB Configuration

InfluxDB v1 (user/password, per-source `db`):
```yaml
influx:
  url: "https://influx.example.com:8086"
  user: "your_influx_user"
  password: "your_influx_password"
  timeout: 5
```

InfluxDB v2 (token/org, per-source `bucket`; falls back to `db` if `bucket` is absent):
```yaml
influx:
  url: "https://influx.example.com:8086"
  token: "your_token"
  org: "your_org"
  timeout: 5
```

Optional `insecure: true` in the `influx` block skips TLS certificate verification for `https` URLs
(needed for self-signed/internal certs); it defaults to `false` (verification enabled).

The `hue` block has its own `insecure` option with the opposite default (`true`), since Hue
bridges are commonly reached over a self-signed local certificate; set `insecure: false` there
if yours has a valid cert.

## Data Format
- **InfluxDB Line Protocol**: `measurement,tag=value field=value timestamp`
- **Timestamp Precision**: Seconds. `send_data()` uses `self.timestamp` if `get_data()` set it (e.g. Octopus uses the reading's own `interval_start`), otherwise the time `send_data()` is called
- **Data Types**: Numeric values (integers, floats) for time-series data
- **Field Names**: Sanitized device names (spaces replaced with underscores); field keys are also escaped per line protocol rules (commas, `=`, spaces)

## Performance Considerations
- **Timeouts**: Appropriate timeouts for all network operations (default: 5 seconds)
- **Intervals**: Configurable collection intervals per data source
- **Memory**: Efficient data structures for processing
- **Rate Limiting**: Consider API rate limits when setting intervals
- **Error Recovery**: Graceful handling of temporary network issues

## Security Notes
- **Credentials**: Store sensitive data in `settings.yaml` with appropriate file permissions if you
  keep them there in plaintext - the packaged install's fresh-install default is `644`, not `600`
  (safe because a freshly-packaged file never contains a real secret, only placeholder/sentinel text,
  unless hand-edited). An environment-variable secrets override was implemented and then deliberately
  removed - see CLAUDE.md's "Rejected: environment-variable secrets" section before re-proposing it.
- **`systemd-creds`**: on the packaged install (`systemd >= 250`), `send-to-influx-set-credential
  <name>` moves a credential out of `settings.yaml` into `systemd-creds` (TPM/host-key encryption at
  rest) - see CLAUDE.md's "Credential storage (`systemd-creds`)" section. Opt-in, per-field;
  `toinflux/credentials.py` is the single source of truth for which fields are eligible.
- **`enforce_permissions`**: settings.yaml key, default `false`; `true` makes `send-to-influx` refuse
  to start (not just warn) if the file is group/other-readable and contains a real credential. New
  installs ship it `true`.
- **HTTPS**: Use HTTPS for all API connections in production
- **Validation**: Validate all input data before processing
- **Logging**: Avoid logging sensitive information

## Common Tasks

### Debugging Issues
1. **Configuration**: Use `--dump` mode to inspect raw API data
2. **Processing**: Use `--print` mode to see processed data without sending to InfluxDB
3. **Validation**: Check `settings.yaml` syntax and values
4. **Connectivity**: Verify network connectivity to APIs and InfluxDB

### Adding New Sensor Types
1. **Identify**: Find sensor type in API response
2. **Process**: Add processing logic in data source's `parse_data()` method
3. **Convert**: Handle unit conversions if needed
4. **Document**: Update configuration documentation

### Modifying Data Format
1. **Update**: Modify InfluxDB line protocol formatting in `send_data()` method
2. **Compatibility**: Ensure backward compatibility with existing data
3. **Document**: Update configuration and usage documentation

## Testing

### Unit tests
- **Framework**: pytest. Tests live under `tests/`.
- **Coverage**: Write unit tests for new and modified code. Tests should cover public functions and classes; use mocks for `load_settings`, file I/O, and HTTP so tests run without real config or network.
- **Virtual environment requirement**: Always run Python tooling from the repo-local virtual environment (`.venv`). Do not rely on globally installed `python`, `pip`, or `pytest`.
- **Running tests**: Install dev dependencies (`.venv/bin/pip install -r requirements-dev.txt`) then run `.venv/bin/pytest -v` (or `.venv/bin/python -m pytest -v`). CI runs this (matrixed across Python 3.10-3.14, with coverage), plus `flake8`, `mypy`, `arm64-verify` (builds the `.deb` on a real `ubuntu-24.04-arm` runner and runs the `packaging/deb/test-packaging.sh` scenario suite against it), `bookworm-verify` (the same suite in a `debian:12`/systemd-252 container), the `Run integration tests (MQTT broker)` job (the marked `integration` tests against a runner-local mosquitto), and `action-pins`/`Action pins are immutable`, on every push to `main` and every pull request - all are required status checks on `main`'s ruleset, and the cheap ones (flake8, mypy, the five pytest legs, and `action-pins`) also gate the `release/**/*` and `feature/**/*` branch tiers, which drop only the slow ones (CodeQL, both packaging jobs, integration). A new check goes on every tier if it is quick and on `main` alone if it takes minutes - cost decides, not importance. Dependabot keeps pip and GitHub Actions dependencies up to date weekly.
- **Action pins**: every `uses:` reference in `.github/workflows` and `.github/actions` must name a full 40-hex commit SHA, and the `action-pins` premerge job fails the build when one names a tag or branch instead. Add a trailing `# vX.Y.Z` comment naming the tag the SHA corresponds to: that part is convention rather than something CI checks - the immutability of the ref is the security property, and the comment only tells a human which tag it was - so write it, but do not expect a failure if it is missing. A tag can be repointed by its owner at any time, so `@v7` runs whatever they last pushed to it - and `release.yaml` runs at `contents: write` with `GITHUB_TOKEN` available to the step that attaches the `.deb`, so a repointed tag would land next to a write-scoped token. First-party `actions/*` get no exemption; the pin costs nothing to hold because Dependabot's weekly `github-actions` run bumps the SHA and rewrites the version comment. Local `./` actions are exempt (this repo's own code at its own commit), and bare, quoted (`uses: "owner/action@<sha>"`) and uppercase SHAs are all accepted - hex is case-insensitive and GitHub resolves an uppercase ref, so failing one would fail a legitimately pinned action. Ported from the identically-named job in [L337-org/apt](https://github.com/L337-org/apt) - keep the logic the same rather than writing a second implementation. When adding a step, look up the SHA for the tag you want (`gh api repos/<owner>/<action>/git/ref/tags/<tag> --jq .object.sha`) rather than writing `@vN`.
- **Adding tests**: When adding a new data source or changing behaviour, add or update tests in the appropriate `tests/test_*.py` module. Reuse fixtures from `tests/conftest.py` (e.g. `sample_settings`) where applicable.

## Development Workflow
1. **Setup**: Copy `example_settings.yaml` to `settings.yaml` and configure
2. **Development**: Use `--print` mode for testing without affecting InfluxDB
3. **Unit tests**: Run `.venv/bin/pytest -v` and add/update tests for your changes
4. **Linting**: Run `.venv/bin/flake8` to check code style
5. **Formatting**: Run `.venv/bin/black` to format code
6. **Integration**: Test with actual devices and InfluxDB instance
