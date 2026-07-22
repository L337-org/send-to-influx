# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Contributor-facing project structure and conventions live in [CONTRIBUTING.md](CONTRIBUTING.md) (this
file is the deeper architecture reference); see also [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md),
[SECURITY.md](SECURITY.md), and [PRIVACY.md](PRIVACY.md).

## Commands

All Python tooling must use the repo-local virtual environment (`.venv`), not system Python.

```bash
# Setup
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

# Tests
.venv/bin/pytest -v                  # all tests
.venv/bin/pytest -v tests/test_hue.py::TestClass::test_name  # single test

# Lint / format
.venv/bin/flake8                     # max line length 120, complexity 10
.venv/bin/black .                    # auto-format
.venv/bin/mypy toinflux sendtoinflux.py   # static type check (permissive, see pyproject.toml)
```

CI runs `pytest` (with coverage, matrixed across Python 3.10-3.14), `flake8`, `mypy`, `arm64-verify`
(builds the `.deb` on a real `ubuntu-24.04-arm` runner and runs the `packaging/deb/test-packaging.sh`
scenario suite against it - install/upgrade/reconfigure/purge lifecycle, see "Packaging" below), and
`bookworm-verify` (the same scenario suite inside a `debian:12` container - systemd 252, the oldest
systemd-creds actually supported; the arm64 runner's systemd 255+ masks pre-254 behaviour
differences, which let a real systemd-creds decrypt regression reach the 4.1 release) in
parallel on every push to `main` and every PR (`.github/workflows/premerge.yaml`) - all are required
status checks on `main`'s ruleset ("Verify .deb on Debian 12 (systemd 252)" needs adding to the
ruleset when it first appears), so a failure blocks merging rather than only being noticed
afterward. Dependency and GitHub Actions updates are managed by Dependabot
(`.github/dependabot.yml`), weekly.

## Architecture

**send-to-influx** collects metrics from smart home / energy devices and writes them to InfluxDB using the [line protocol](https://docs.influxdata.com/influxdb/v1/write_protocols/line_protocol_tutorial/). Both InfluxDB v1 (user/password) and v2 (token/org/bucket) are supported.

### Class hierarchy

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

If a write to InfluxDB fails, `send_data()` buffers the point in memory instead of dropping it (raising `InfluxWriteError` either way, so the worker's existing backoff/retry is unaffected) - see `DataHandler._write_buffers`: a per-source `deque(maxlen=MAX_BUFFERED_POINTS)` of `[line, rejection_count]` entries, class-level rather than an instance attribute because the worker loop in `sendtoinflux.py` discards and reconstructs the `DataHandler` instance after every failure, so only a buffer that outlives the instance survives to be flushed. Every buffered-path `send_data()` call flushes the backlog first - including calls with no data of their own (an empty reading still delivers the backlog; only an empty-buffer-and-empty-data call skips the HTTP round trip entirely) - in newline-joined chunks of `FLUSH_CHUNK_SIZE` per POST (InfluxDB's write endpoints accept multi-point bodies natively, so a 500-point recovery costs ~5 requests, not 500). If a source's outage runs long enough to fill its buffer, the oldest buffered point is dropped to make room, logged as a warning; an identical line already in the buffer is never added twice (Octopus re-serves the same reading/timestamp for ~30 min, and duplicates would only waste capacity since flushing is an idempotent overwrite). Not persisted across a process restart, and flushed to whatever destination the *current* settings resolve to (editing `influx.url`/bucket/db mid-backlog re-routes the backlog - accepted limitation, documented in the `_write_buffers` comment).

Failure classification is deliberately **not** trusted per-status-code as a verdict: `InfluxWriteError.status_code` carries the HTTP status (or `None` for a connection failure), and `_flush_buffer()`/`_flush_head()` count how many times the server has *rejected* (a non-transient 4xx - 408/429 are excluded via `TRANSIENT_CLIENT_ERRORS`, since rate-limiting/timeouts say nothing about the point) each specific point, dropping it with a warning only after `MAX_POINT_REJECTIONS` separate rejections. Connection failures, 5xx, 408, and 429 never count, so an arbitrarily long outage or rate-limit burst can't age points out - only a point the server itself keeps refusing (malformed → 400, outside the retention window → 422 on InfluxDB v2, oversized → 413) is given up on, and a middlebox transiently answering 4xx for a down InfluxDB can't mass-discard the backlog (each point survives `MAX_POINT_REJECTIONS` attempts). When a batched chunk is rejected, the flush falls back to per-point posting for that chunk to isolate the offender(s). Heartbeat writes pass `use_buffer=False` - a heartbeat is a live signal with no replay value, so it neither consumes buffer capacity nor triggers a redundant second flush per failed cycle. `validate_settings()` rejects duplicate entries in `sources:` (`ConfigError`) - two workers for one source name would share (and race on) one buffer.

Speedtest's `get_data()` additionally rejects an implausible `ping` (>= 5000 ms) as a `SourceConnectionError` rather than writing it. speedtest-cli's `get_best_server()` times each of the 3 latency probes it makes per candidate server with a hardcoded 10-second connection timeout (baked into `SpeedtestHTTPConnection`/`SpeedtestHTTPSConnection`'s constructor default - never overridden by `get_best_server()`, so it applies regardless of the `timeout` passed to `speedtest.Speedtest()`); a probe that doesn't complete within that raises `socket.timeout`, which is caught alongside every other connection failure and penalised with a hardcoded `3600` (seconds) instead of a real sample. The 3 per-server samples (real or penalty) are summed, divided by a fixed 6, and converted to milliseconds - so a real (non-penalised) probe can never contribute more than 10s to that sum, making `(3 * 10 / 6) * 1000 = 5000` ms the true ceiling for a genuine measurement. If every probe to a server fails (observed in practice during a transient network blip), the reported `ping` comes out around 1,800,000 ms instead of triggering an error, and would otherwise be written to InfluxDB as if it were real.

Nuki is the first MQTT-based source: `MqttDataHandler` (`toinflux/mqtt.py`) owns the generic
transport (connect, subscribe from inside `on_connect` - a subscription issued before the CONNACK
completes can be silently lost - collect for a fixed window, disconnect), reading broker config from
the shared top-level `mqtt:` settings block (mirroring `influx:` - the broker and its `mqtt-password`
credential are per-install infrastructure, not per-source). The polling-per-interval architecture
works over MQTT only because Nuki publishes every state topic with the retain flag set, so a short
subscribe window receives the full last-known state of every provisioned lock - equivalent to an HTTP
GET. Failure mapping is deliberately strict: bad credentials arrive asynchronously as a failed CONNACK
(never as an exception from `connect()`), and a broker that accepts TCP but never completes the MQTT
handshake raises `SourceConnectionError` rather than returning an empty result - either would
otherwise masquerade as "no data". `Nuki` (`toinflux/nuki.py`) holds only vendor logic: filtering to
known state topics (command/event topics are ignored), grouping by device ID, prefixing field keys
with each lock's own Nuki-app name, and renaming `state`/`doorsensorState` to `stateValue`/
`doorsensorStateValue` - Grafana visualises numeric fields far better than text, so unlike the
Bridge HTTP API's `stateName` strings, these are always written as their raw numeric code (a code
with no documented meaning is written through unchanged); see UNITS.md for what each code means.
`paho-mqtt` (a
source-specific runtime dependency like `speedtest-cli`, pure Python so the `.deb`'s
`Architecture: all` design holds) is imported only in `toinflux/mqtt.py`.

### MCP server (`toinflux/mcpserver.py`)

The optional remote MCP server (new in 5.0) is *not* a `DataHandler` - it's the project's
first inbound-network-facing component, a Streamable-HTTP server built on the official `mcp` SDK's
`FastMCP` + built-in OAuth 2.1 authorization server, run in its own daemon thread (`anyio` inside
the thread; nothing else in the synchronous codebase changes). Enabled iff both `mcp.user` and
`mcp.password` are set (no separate flag; one without the other is a `ConfigError` - see
`mcp_block_errors()`/`mcp_enabled()` in `toinflux/general.py`); started from
`sendtoinflux.py`'s `maybe_start_mcp_server()` (skipped in `--print`/`--dump` modes). Key
decisions:

- **Bind vs public**: binds `mcp.bind_address` (default `127.0.0.1:8420`) in plain HTTP;
  `validate_settings()` refuses `0.0.0.0`/`::` outright with no override (plain-HTTP OAuth on a
  public interface is never valid - TLS termination belongs to the user's reverse proxy). The
  external HTTPS address is `mcp.public_url` (required when enabled, must be `https://`): the
  OAuth issuer/discovery metadata and login-page redirects are built from it, never from the bind
  address. The SDK's DNS-rebinding protection stays enabled with the public hostname allowlisted
  (a reverse-proxied request carries the public Host header, which the SDK's localhost-only
  default would reject).
- **OAuth storage** (`SendToInfluxOAuthProvider` + `OAuthStateStore`): dynamic client
  registrations and refresh tokens persist across restarts in `mcp.state_file` (default
  `mcp-oauth-state.json` next to settings.yaml - the one path the packaged service's sandbox
  guarantees writable), written atomically at 0600; refresh tokens stored as SHA-256 hashes only.
  postinst restarts the service on every upgrade, so in-memory-only state would break the Claude
  connector on every unattended upgrade. Access tokens are in-memory (1 h TTL) - a restart
  invalidates them and the client recovers silently via refresh. The SDK's token endpoint does
  PKCE/expiry/client-binding verification itself; the provider only stores, loads, and issues.
- **Login page** (`/login`, via `FastMCP.custom_route`): resource-owner step gated on
  `mcp.user`/`mcp.password` (constant-time comparison), single-use unguessable transaction ids
  minted by `authorize()`. Failed attempts are throttled per client address
  (`LoginThrottle`: 5 failures → 300 s lockout, WARNING-logged) - behind a reverse proxy every
  request carries the proxy's address, so the lockout is effectively global, which is the intended
  behaviour for a single-user login page, not a limitation.
- `mcp-password` is in `CREDENTIAL_FIELDS` like every other secret; its `PLACEHOLDER_VALUES`
  entry is deliberately the empty string (empty-means-disabled is the block's enablement
  mechanism, and `--remove` reverting to `""` is exactly the disabled state).
- The `mcp` SDK is imported only inside `toinflux/mcpserver.py` (lazily, gated on `mcp_enabled()`),
  like `paho-mqtt` in the MQTT transport - but unlike paho it is **not** pure Python: its chain
  needs `pydantic_core` and `rpds-py` (Rust-compiled, no fallback), which is what the packaging
  section's compiled-wheel matrix exists for. `rpds-py` is held to `~=0.30.0` in requirements.txt
  (2026.x CalVer releases dropped Python 3.10 wheels; the build fails loudly if coverage regresses).
**Write tools** (`toinflux/mcp_write.py`, `register_write_tools()`): the MCP server is read-only by
default. A source becomes controllable only when it's both `MCP_WRITABLE` (a class flag - Hue is the
only one today) *and* the operator opts in with `<source>.mcp_read_write: true`
(`DataHandler.mcp_write_enabled()`, strict `is True`; `validate_settings()` rejects a non-bool so a
mistyped `"true"` fails loud instead of silently staying off). Design points:
  - **Least privilege**: when no source is write-enabled, `register_write_tools()` registers *nothing*
    - the `set_device_state`/`list_writable_devices` tools are absent from the server's advertised
      surface entirely, not present-and-refusing. So the capability can't be probed or bypassed when
      it's off.
  - **Generic primitive**: `set_device_state(source, device, on, brightness_pct)` dispatches to the
    source class's own `mcp_set_device_state()`; the device-specific logic (name→bridge-id
    resolution, the friendly-param→vendor-API mapping) lives on the source, like the read domain
    knowledge. Deliberately source-agnostic so SI-7's PID actuation can drive the same tool with no
    new MCP wiring.
  - **Hue** (`toinflux/philipshue.py`): `mcp_set_device_state()` resolves the target against the live
    device list (`mcp_list_writable_devices()`, the write allowlist - an unknown or ambiguous name is
    refused, never guessed, since actuating the wrong light isn't recoverable), maps brightness 0-100%
    to the bridge's 1-254 `bri` (0% is min-on, not off - off is `on=False`, keeping the two controls
    independent), auto-adds `on=True` when setting brightness (the bridge ignores `bri` on an off
    light) unless `on` is explicitly false, and `PUT`s to `/api/{user}/lights/{id}/state` over the
    collector's own session/auth and `hue.insecure` TLS policy. The CLIP API returns 200 with a
    per-key success/error list, so a bridge-reported error is surfaced as `SourceConnectionError`.
  - Per-call handler/session lifecycle and the ToolParamError-vs-SourceConnectionError split are the
    same as the read tools: the shared per-call plumbing (`resolve_handler`, `close_session`,
    `configured_sources`) lives in `toinflux/mcp_common.py`, which every tool module imports from
    rather than from each other. Every applied write is logged at INFO.
  - This is the project's first device-control capability and gets a dedicated `/security-review`
    before the feature branch merges to `main`.

**Packaging** (debconf + systemd): the `mcp:` block is the third shared-infrastructure block after
`influx:` and `mqtt:`, but gated on its own `mcp-enable` boolean (asked at priority `high`, default
no) rather than a source selection - the MCP server is an interface over all sources, not a source.
When enabled, debconf collects `mcp-public-url`/`mcp-user`/`mcp-password`; `bind_address` is a
defaulted tuning field and never prompted. `postinst` back-fills the `mcp:` section with
`--ensure-section` (settings.yaml is never rewritten by an upgrade, so the section is absent on
installs predating this feature) and requires public_url + user + a password (typed or already in
systemd-creds) all present before enabling - a partial `mcp:` block makes `load_settings()` raise a
fatal `ConfigError` that stops **every** collector, not just the server. `public_url` and `user` are
required strings that persist and pre-fill on reconfigure (like `mqtt-broker-host`/`hue-host` - only
the *secret* `mcp-password` is cleared after use, so password-only rotation works: leave the
pre-filled url/user and type just the new password). Because the service is only (re)started at the
very end of `postinst`, only the final settings state matters, so on a failed password store: if no
credential existed yet (a fresh enable) the username is reverted (enable-then-revert → coherent
disabled block); if one already existed (a reconfigure) the previously-stored password is kept and
the working install is left enabled - never disabled out from under a running server.
`hue.mcp_read_write` stays hand-edited (a tuning toggle, never prompted). The MCP server also made
this the first inbound-network-facing service, so the systemd unit gained a conservative hardening
set (`ProtectKernel*`, `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`, empty
`CapabilityBoundingSet`, `SystemCallFilter=@system-service`, etc. - `MemoryDenyWriteExecute` and a
hand-rolled narrower syscall filter deliberately omitted as Python-fragile); `ReadWritePaths` already
covers the OAuth state file (it lives in `/etc/send-to-influx`). `test-packaging.sh` seeds the MCP
answers in the fresh-install scenario (asserting public_url/user land in settings.yaml, the password
in the credstore and not in plaintext) and, where real systemd is present, asserts the server
actually binds `127.0.0.1:8420` under the full hardened sandbox (the real test that the hardening +
`LoadCredentialEncrypted` don't break the network-facing server).

**Read tools** (`toinflux/mcp_read.py`, registered onto the server by `register_read_tools()`):
three read-only tools - `list_sources`, `list_fields`, and `query_history` - exposing each
configured collector's InfluxDB history, domain-aware rather than a raw passthrough. The read
mechanics live in `mcp_read.py`; the per-source domain knowledge lives on the `DataHandler`
subclasses as three class attributes (`MCP_MEASUREMENT`, `MCP_TAG_FILTERS`, `MCP_FIELD_METADATA`)
so there's no parallel schema to keep in step - `ReadSchema`/`build_schema()` combine those with a
live field set. Design points:
  - **Measurements aren't always the source name**: `openmeteo` writes to `weather`, and the three
    MyEnergi devices share the `myenergi` measurement distinguished by a `device` tag - so their
    classes set `MCP_MEASUREMENT`/`MCP_TAG_FILTERS`, or a query for one device would return all
    three. Every other source owns its measurement (`MCP_MEASUREMENT` stays `None` → source name).
  - **Injection defence, layered** (InfluxQL has no identifier parameter binding): the measurement
    and tags come from the source class's static schema, never model input; a requested field must
    exactly match a key discovered live via `SHOW FIELD KEYS` (the field set *is* the allowlist,
    and it handles collectors with dynamic field names - Hue sensors, per-lock Nuki prefixes);
    every identifier is additionally charset-validated and double-quoted with escaping; time bounds
    are parsed in Python and re-emitted as RFC3339 (the model's raw string never reaches the query);
    aggregation is a fixed name→InfluxQL-function map and any GROUP BY interval matches a duration
    grammar. Result size is capped (`MAX_RESULT_POINTS`).
  - **A single query path serves v1 and v2**, mirroring `_build_write_request()`'s branch: `GET
    /query` with a `Token` header (v2) or HTTP basic auth (v1), `epoch=s`. v2's v1-compatibility
    `/query` endpoint needs no extra provisioning in the default case (virtual DBRP mappings keyed
    by bucket name since InfluxDB 2.9) - verified against real v1 and v2 containers.
  - **`SHOW FIELD KEYS` is per-measurement, not per-tag**, so for the three shared-measurement
    MyEnergi devices `list_fields` shows the others' fields too; a query for a cross-device field
    is safe and returns no points (the tag filter excludes it). Documented accepted limitation.
  - **`MCP_FIELD_METADATA`** maps a field key - or a `_`-delimited suffix, for dynamically-prefixed
    fields like Nuki's `Front_Door_stateValue` - to `{"unit": ...}` and/or `{"codes": {int: str}}`;
    `annotate_rows()` attaches units and decodes coded values to labels (an undocumented code passes
    through with a null label, matching the collector's raw-passthrough rule). Sourced from UNITS.md.
  - Blocking InfluxDB HTTP runs in a worker thread (`anyio.to_thread.run_sync`) so a query doesn't
    stall the server's async event loop. `ToolParamError` (a bad field/time/aggregation/device -
    shared by the read and write tools, defined in `toinflux/exceptions.py`; a non-retryable
    caller/model mistake) surfaces to the model as a tool error; `SourceConnectionError` is a
    transient transport failure the collector loop would retry, so the two are kept distinct.

### Entry point (`sendtoinflux.py`)

- **Single-source mode** (`--source <name>`): continuous loop, fixed interval per source. Connection failures (`SourceConnectionError`) are retried with exponential backoff (base 5 s, max 300 s); a `ConfigError` is not retried — it exits the process immediately with code 1.
- **Multi-source mode** (no `--source`): reads `sources` list from `settings.yaml`, spawns one daemon thread per source with a configurable startup stagger (`stagger_seconds`, default 10). Dead threads are detected and restarted with the same exponential backoff — unless the source's worker stopped because of a `ConfigError`, in which case it is logged and left stopped (other sources keep running).
- `--dump`: one-time raw JSON to stdout, then exit (single source only).
- `--print`: parsed data to stdout instead of InfluxDB.
- `--settings <path>`: use a settings file at a path other than `settings.yaml` in the project root (e.g. `/etc/send-to-influx/settings.yaml` for a packaged install). Threaded through `toinflux.get_class()`/`load_settings()`.
- `--version`: print `__version__` and exit; parsed before settings are loaded, so it works without a `settings.yaml` present.
- `--check-config`: load and validate the settings file (via `load_settings()`), print `Configuration OK`, exit 0. Exits 1 with details if invalid (same validation as a normal run). If `--source` is also given, that source's block is validated too even if it isn't in `sources`/`default_source` (`validate_settings(settings, source=...)`), so checking config for a one-off `--source` can't report a false "OK".
- `-v`/`--verbose`: force `DEBUG`-level logging, overriding the `loglevel` settings.yaml key.
- Handles SIGINT and SIGTERM for graceful shutdown.
- On startup, logs an INFO line with the version and the source(s) that will run, so process (re)starts are visible in the logs.
- CLI arguments are parsed *before* `load_settings()` is called, so `--version`/`--help` don't require a config file to exist.
- After every collection cycle, `maybe_send_heartbeat()` writes a `collector_status,source=<name>` point (fields `ok`, `consecutive_failures`) via `send_heartbeat()`, which reuses the source's own `DataHandler.send_data()` with a swapped-in header. Skipped in `--print` mode.

### Exceptions (`toinflux/exceptions.py`)

- `ConfigError`: a fatal, non-retryable problem (missing/invalid settings, unknown source name). Raised by `toinflux/general.py` (`load_settings()`, `get_class()`, `configure_logging()` if `logfile` can't be opened for writing - e.g. an unwritable path under the packaged systemd service's sandboxing) and `DataHandler.__init__()`.
- `SourceConnectionError`: a transient problem talking to a source's API (network error, bad auth, bad response). Raised from each handler's `get_data()`/API-call code and retried with backoff by the worker loop.

### Factory / settings

- `toinflux/general.py`: `load_settings(settings_file=None)` (raises `ConfigError` on missing/invalid YAML; `settings_file` defaults to `settings.yaml` in the project root when omitted), `get_class(source, settings_file=None)` (case-insensitive factory → correct DataHandler subclass; raises `ConfigError` for an unknown source, including `DataHandler` itself — it's the abstract base, not a selectable source; threads `settings_file` through to the handler's `load_settings()` call), `flatten_dict()` (used by Speedtest to flatten nested JSON), `configure_logging(logfile=None, loglevel="INFO", log_max_bytes=..., log_backup_count=...)` (sets up timestamped stdout logging, plus an optional `RotatingFileHandler`; raises `ConfigError` instead of a raw `OSError` if `logfile` can't be opened, e.g. a permissions problem).
- `configure_logging()` is called via `_configure_logging_or_exit()` in `main()` after settings are loaded and `--check-config` has short-circuited - this catches that `ConfigError`, logs it (the stdout handler is already attached by the time it's raised, so this still reaches the journal under systemd as a normal formatted line, not a traceback), and exits 1. Log messages use the format `YYYY-MM-DD HH:MM:SS LEVEL message`. Effective log level is `-v`/`--verbose` (forces `DEBUG`) > `loglevel` settings.yaml key > `INFO` default.
- Config file: `settings.yaml` (copy from `example_settings.yaml`), or a custom path via `--settings`. Required at runtime; not committed. Optional `logfile` key adds a rotating file log destination (`log_max_bytes`/`log_backup_count` settings keys control rotation, defaulting to 10 MiB / 3 backups). Some fields can optionally be sourced from `systemd-creds` instead on the packaged install - see "Credential storage (`systemd-creds`)" below; an environment-variable secret-override mechanism was considered and deliberately rejected instead - see "Rejected: environment-variable secrets" below.

### Adding a new data source

1. Create `toinflux/newsource.py` — class inheriting `DataHandler`, implement `get_data()`.
   (For an MQTT-based source, inherit `MqttDataHandler` instead and also add the source's name
   to `MQTT_SOURCES` in `toinflux/general.py`, so `--check-config` validates the shared `mqtt`
   block for it.)
2. Register it in `get_class()` in `toinflux/general.py` and add it to `toinflux/__init__.py`.
3. Add a section to `example_settings.yaml`. Any credential field also gets an entry in
   `CREDENTIAL_FIELDS`/`PLACEHOLDER_VALUES` (`toinflux/credentials.py`) — that alone makes
   `send-to-influx-set-credential <name>` work, the machinery is fully table-driven.
4. Add tests in `tests/test_newsource.py`, reusing fixtures from `tests/conftest.py`.
4b. For the MCP read tool: set `MCP_FIELD_METADATA` on the class (units, and `codes` for any
   numeric-coded field) from the UNITS.md entry, and set `MCP_MEASUREMENT`/`MCP_TAG_FILTERS` if the
   source's InfluxDB measurement isn't its own name or it shares a measurement with others. Nothing
   else is needed - the source is exposed by the read tools automatically once it's in `sources:`.
4c. (Only if the source can be *controlled*, and its vendor API has a documented write path.) Set
   `MCP_WRITABLE = True` and implement `mcp_set_device_state(device, *, on=..., brightness_pct=...)`
   plus `mcp_list_writable_devices()` on the class (see `Hue`), keeping the vendor-specific
   name→id/param mapping there. Add `<source>.mcp_read_write` (bool, default false) to
   `example_settings.yaml`; validation already rejects a non-bool. The `set_device_state` tool then
   dispatches to it once the operator opts in. Most sources are read-only and skip this.
5. Update README.md, UNITS.md, CLAUDE.md, and `.github/copilot-instructions.md`.
6. Wire the source into the debconf install flow — a mechanical checklist, not a judgment call
   (every rule below is an existing, tested convention; the scenario suite enforces most of them):
   - (a) Add the source's name to the `sources-to-configure` multiselect `Choices` in
     `packaging/deb/send-to-influx.templates`, **appending at the end** — the question-visibility
     scenario in `test-packaging.sh` selects existing sources by position number, so inserting
     mid-list silently retargets those tests.
   - (b) Credential/identity/connection fields get conditional questions (templates + a
     `case "$SOURCES"` block in `packaging/deb/config`), all priority `high` — debconf's default
     threshold is `high`, so anything lower is silently skipped on a normal install. Tuning
     fields (`interval`, `db`, `timeout`, `fields` lists) are **never** prompted for.
   - (c) Secrets are `Type: password` templates; `postinst` migrates them via
     `send-to-influx-set-credential` (stdin pipe, best-effort via the `set_secret` helper) and
     clears the stored answer with `db_set ""` immediately after `db_get` — never
     `db_unregister` (it deletes the seen flag, causing blank re-prompts on every upgrade). Add
     every credential-bearing answer to the final unconditional sweep loop too.
   - (d) If the source uses a *shared* infrastructure block (like `mqtt:`), its questions are
     asked once, gated on any source needing that block being selected — not per-source, and
     not unconditionally (that's InfluxDB's special status only, since every install needs it).
     A credential already stored in systemd-creds satisfies a blank secret prompt on
     reconfigure, and non-secret fields provided alongside a blank secret are still applied.
   - (e) Auto-enable (`--enable-source`) only when every required field actually resolved (and
     `INFLUX_OK=1`) — "was it ticked" is not enough; otherwise print a specific
     "not fully configured" warning and leave it opt-in.
   - (e2) If the source introduces a **new settings section** (its own block, or shared
     infrastructure like `mqtt:`), `postinst` must `--ensure-section` it before writing fields or
     enabling the source. `settings.yaml` is written once at install time and never rewritten by an
     upgrade, so a section added by a later release simply doesn't exist on existing installs:
     `--set-field` fails, and `--enable-source` then writes the source into `sources:` with no block
     behind it, which makes `load_settings()` raise a fatal `ConfigError` and stops the **whole
     service**, taking every already-working source down with it.
   - (f) Extend `packaging/deb/test-packaging.sh`: seed the new source's answers in the
     seeded-install scenario (assert fields land in settings.yaml, credentials in the credstore,
     plaintext absent from settings.yaml *and* debconf's database), and extend the
     question-visibility scenario (questions appear when the source is selected, and for
     conditional shared blocks, do **not** appear when it isn't).

### Testing conventions

- Mock `load_settings`, HTTP calls, and file I/O so tests run without real config or network.
- Shared fixtures (e.g. `sample_settings`) live in `tests/conftest.py`.

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Normal exit |
| 1 | Configuration error (`ConfigError`: missing/invalid settings, unknown source) |
| 2 | Connection error (`SourceConnectionError`) in `--dump` mode only - there's no worker loop to retry a one-shot dump with backoff. In continuous mode (single- or multi-source), connection errors are always retried with backoff instead of exiting. |

### Packaging (`packaging/`)

- `pyproject.toml` is the single source of truth for the package version (`[project].version`) and runtime dependencies (dynamically read from `requirements.txt`). Bump the version there, not in `sendtoinflux.py`.
- `sendtoinflux.py`'s `__version__` is read from installed package metadata (`importlib.metadata.version("send-to-influx")`), falling back to `"0.0.0-dev"` when run from a source checkout without the package installed. `requirements-dev.txt` includes `-e .` so dev/test environments have it installed and see the real version.
- `packaging/deb/build-deb.sh` builds a `.deb` that bundles the app + dependencies into a venv under `/opt/send-to-influx`, with a systemd unit (`packaging/send-to-influx.service` - kept at the top level of `packaging/` since it's format-agnostic; a future `.rpm` would ship the identical unit file) and maintainer scripts (`packaging/deb/preinst`/`postinst`/`prerm`/`postrm`). `/etc/send-to-influx/settings.yaml` is deliberately *not* a dpkg conffile: `postinst` and `send-to-influx-set-credential` write debconf answers/sentinels into it, and dpkg's conffile machinery treats any maintainer-script write as a local modification (Debian Policy 10.7.3 forbids the combination) - guaranteeing a "modified (by you or by a script)" prompt on every upgrade that ships a changed example, with a one-keypress path to replacing a configured file with the pristine example. Instead the example ships at `/usr/share/send-to-influx/example_settings.yaml` and `postinst` copies it into place only if `/etc/send-to-influx/settings.yaml` doesn't exist (the Policy 10.7.3 "configuration files handled by maintainer scripts" pattern; `postrm` removes it on purge, as that pattern requires) - so upgrades never touch the live file at all. On upgrade (and after a `dpkg-reconfigure` that rewrote configuration), `postinst` restarts the service if - and only if - it's currently running, so unattended upgrades don't leave the replaced code running until the next reboot; a stopped service is never started. Package is `Architecture: all`: the venv's own interpreter is a symlink to the system-provided `/usr/bin/python3` (declared as a `Depends: python3 (>= 3.10), python3 (<< 3.31)`, not bundled), and any optional compiled accelerators pulled in by pip (e.g. PyYAML's `_yaml`, charset-normalizer's `md`/`cd`) are stripped post-install in favour of their pure-Python fallbacks - see the comments at the top of `build-deb.sh`. The two exceptions since the MCP server landed are `pydantic_core` and `rpds-py` (required by the `mcp` SDK, Rust-compiled, no pure-Python fallback): the blanket strip removes them too, and a dedicated compiled-wheel-matrix step then merges the compiled extensions from the prebuilt manylinux wheels for every supported minor (3.10-3.14, `COMPILED_WHEEL_MINORS`) x both architectures into the shared site-packages - CPython only imports a `.so` tagged with its own exact ABI, so the variants coexist; the build fails loudly if any minor/arch combination is missing, which (together with the `rpds-py~=0.30.0` hold in requirements.txt) is the guard against a wheel version dropping a supported minor. A venv's `site-packages` normally lives under `lib/pythonX.Y/` (named after the exact interpreter that created it), which would otherwise tie the package to whichever Python the *build host* happened to have; since everything left after the accelerator-stripping is pure Python, the script instead renames it to the version-independent `lib/python3` and `postinst` symlinks every supported minor to it (see the `preinst`/layout bullet below for why the symlinks are created there rather than shipped; both bounds come from `PYTHON_MIN_SUPPORTED_MINOR`/`PYTHON_MAX_SUPPORTED_MINOR`, which also drive `Depends:` and are substituted into `postinst`, so the range can't drift apart), so the package installs correctly on any target with a matching `python3`, regardless of which minor in that range. (An earlier version pinned `Depends:` to the exact build-time minor instead - that broke in practice the first time the target's Python drifted out of sync with whatever GitHub's CI runner image shipped.) `.github/workflows/premerge.yaml`'s `arm64-verify` job builds the same script's output on an `ubuntu-24.04-arm` runner on every push/PR (a required status check) and runs `packaging/deb/test-packaging.sh` against it - catching both a future dependency change that makes a compiled extension load-bearing rather than optional, and any regression in the maintainer-script behaviour below, before it can merge; `bookworm-verify` re-runs the same suite in a `debian:12` container for systemd-252 coverage (the restart scenario self-skips there - no running systemd - but the systemd-creds *tooling* is the real 252 binaries, which is what caught out 4.1). See the README's "Running as a systemd service" section.
- `packaging/deb/preinst` deletes the whole bundled venv (`/opt/send-to-influx/venv`) so the
  unpack that follows lays down a pristine one. The venv is entirely package-owned and recreated by
  every install - no configuration (that's `/etc/send-to-influx`), no credentials (the credstore),
  nothing user-editable - and wiping it removes several failure modes at once: stale modules being
  imported in preference to new ones (a locally-built 4.4 once logged its 4.4 banner while running
  pre-4.3 library code, failing with "unexpected keyword argument 'use_buffer'" and "Source nuki not
  found"), leftover `lib/python<major.minor>/` trees from a package built against a different
  interpreter, and runtime-generated `__pycache__` files that dpkg doesn't own and won't clean up.
  **The safety guard is the `DEBCONF_RECONFIGURE=1` early exit at the top**, and it is essential:
  `dpkg-reconfigure` also runs `preinst`, as `upgrade <version>` - indistinguishable from a real
  upgrade by its arguments alone - but with *no unpack following it*, so anything deleted on that
  path is gone permanently. An earlier version without that guard destroyed the installation on
  every reconfigure. `DEBCONF_RECONFIGURE` is the same flag `postinst` uses to tell the two apart,
  and is verified to be visible in `preinst`.
- Relatedly, `build-deb.sh` names the venv's real site-packages directory `lib/python3` (version
  *independent*), and `postinst` - not the package - creates the `lib/python3.X -> python3` symlinks
  across the supported range, removed again by `postrm`. Both details exist to keep dpkg quiet and
  correct: a version-named real directory would need to swap places with a symlink whenever the
  build interpreter differed from the installed one (which dpkg cannot reliably do), and shipping
  the symlinks in the package would leave them in place during dpkg's post-unpack cleanup, so old
  `lib/python3.<minor>/...` paths would resolve through them into the freshly-unpacked tree and fail
  to `rmdir` - ~166 "unable to delete old directory" warnings on an upgrade where nothing was
  actually wrong. The supported range lives once, as `PYTHON_MIN/MAX_SUPPORTED_MINOR` in
  `build-deb.sh`, which drives `Depends:` and is substituted into `postinst` at build time (the
  build fails if a placeholder survives).
- `packaging/deb/test-packaging.sh` is the scenario suite for the maintainer scripts - shell behaviour pytest can't reach. Against a built `.deb` it asserts, in order: upgrade over the *latest published release* (obsolete-conffile handover, no re-prompt of the old `db_unregister`-era secret, config/credentials preserved; skipped gracefully offline or via `SKIP_RELEASE_UPGRADE=1`); a fresh debconf-seeded install (fields applied, credential migrated, plaintext secret absent from both `settings.yaml` and debconf's own database, ownership/modes, no conffiles, `/opt` root-owned); plain-upgrade silence with an *interactive* frontend over a hand-edited config (no prompts, no warnings, file byte-identical); restart-on-upgrade of a running service (real `MainPID` change - the example config's placeholder values pass validation, workers just retry, so the service stays active without a real InfluxDB; skipped where systemd isn't running, e.g. containers); `dpkg-reconfigure` semantics (answers re-applied, a stored systemd-creds credential satisfies the blank secret prompt, running service restarted); post-upgrade `dpkg-reconfigure` against a release-era `settings.yaml` (the `mqtt:`/`nuki:` sections back-filled by `--ensure-section`, the venv surviving - `preinst` also runs on that path - and the result still passing `--check-config`); incoherent MQTT auth (a username with no password material warns instead of auto-enabling); per-source question visibility at debconf's *default* priority (`high`, via the teletype frontend), including that the conditional `mqtt-*` questions are absent when no MQTT source is selected; and purge (config, credentials, debconf answers, service, and the postinst-created venv symlinks all gone). It is deliberately destructive - CI runners or throwaway containers only, requires root. Every assertion maps to something that regressed, or nearly regressed, during PR #48.
- `.github/workflows/release.yaml`: triggered by a GitHub Release being **published** (`on: release: types: [published]`), *not* by tag pushes - so tags can be created for other purposes without triggering a build. The release process is: draft the release in the UI (tag = bare `MAJOR.MINOR` matching `pyproject.toml`, no `v` prefix; hand-written notes on top of the generated ones) and publish; the workflow then runs the test suite, verifies the release tag matches `pyproject.toml`'s version exactly, builds the `.deb`, and attaches it to the release. Uploads go by **release id straight from the event payload** - the `releases/tags/{tag}` lookup endpoint is never called, after it served 503s for an extended period during the 4.3 release, which `gh release upload <tag>` rendered as a bogus "release not found" that failed the run (the 4.3 `.deb` was rescued by hand-uploading the run's saved artifact via the release-id endpoint - the same path the workflow now uses; the artifact upload exists precisely so that manual rescue is always possible). No in-job retries by design: a failure is reported plainly and the remedy is re-running the failed job. APT publishing moved out on 2026-07-15.
  - The flat APT repo at `https://apt.l337.org/` is owned by [L337-org/apt](https://github.com/L337-org/apt) - an hourly single-writer aggregator that pulls `.deb` assets from L337-org projects' GitHub Releases (this repo is listed in its `repos.yaml`), regenerates and signs the index (`APT_GPG_PRIVATE_KEY`/`CI_COMMIT_SIGNING_KEY` live *there* now, not here), and pushes to its own `gh-pages`. A new release here appears in the APT repo within the hour (or immediately via that repo's *Run workflow* button). This repo's own `gh-pages` branch and publishing secrets are vestigial once this lands and are removed as a post-merge follow-up.
  - `https://gavinlucas.github.io/send-to-influx/` (the pre-org-move URL) serves a frozen 4.2 snapshot from a placeholder repo at the old name, so pre-move installs keep working; the repo has lived in the `L337-org` org since 2026-07-15.

### Rejected: environment-variable secrets

An earlier version of this project let `INFLUX_TOKEN`/`INFLUX_PASSWORD` environment variables
(sourced from an optional `/etc/send-to-influx/environment`, via the systemd unit's
`EnvironmentFile=`) override the corresponding `settings.yaml` values, intended to let a packaged
install keep secrets out of the settings file. Removed after review concluded it added no real
security value:

- Both files end up owned by the same service user with the same permissions - there is no actual
  security *boundary* between "secrets in settings.yaml" and "secrets in an env file," only an
  organizational one. Splitting secrets into a separate file that is equally (or, in practice, worse
  - see below) protected is security theatre, not a mitigation.
- The environment file was never created or permission-locked by `postinst` - a user following the
  documented advice (`sudo nano /etc/send-to-influx/environment`) would create it with whatever
  default permissions their editor/umask gave it, likely world-readable, ending up *less* secure than
  leaving the secret in the already-`chmod 600` settings file. A real implementation would need
  `postinst` to pre-create and lock down that file, but even then:
- Environment variables add a genuinely distinct exposure path a plain file doesn't have -
  `/proc/<pid>/environ` and (if ever enabled) core dumps both capture them - without removing any
  existing one, since `settings.yaml` still needs to stay locked down regardless (not every field is
  a candidate for env-var override, and the override is opt-in/unverifiable).
- The one semi-plausible benefit (a locked-down settings file is safer to attach to a bug report)
  doesn't hold up: since moving a secret to the env file is optional and unenforced, you can never
  trust that a given user's `settings.yaml` has no secrets in it, so the advice would always have to
  be "redact before sharing" regardless of whether this feature exists.

`systemd`'s `LoadCredential=`/`systemd-creds encrypt` is now implemented for exactly this reason - it
creates a *real* boundary (TPM-bound or host-key encryption at rest, credentials materialized only in
a restricted tmpfs for the service's lifetime) rather than an organizational one. See "Credential
storage (`systemd-creds`)" below. It only helps the packaged systemd install, same as this rejected
approach would have - the plain screen-session/source-checkout path this project treats as equally
first-class is unaffected either way, since `$CREDENTIALS_DIRECTORY` is simply unset there.

### Credential storage (`systemd-creds`)

For the packaged `.deb`/systemd install, secrets can optionally be moved out of `settings.yaml` and
into `systemd-creds` - a real security boundary (TPM-bound or host-key-derived encryption at rest,
decrypted only into a restricted tmpfs for the service's lifetime), unlike the rejected env-var
mechanism above. This is opt-in: the plain-YAML path is unaffected and remains equally first-class for
the source-checkout/screen-session path, where `systemd-creds` doesn't apply at all.

- `toinflux/credentials.py`: `CREDENTIAL_FIELDS` is the single source of truth mapping a systemd-creds
  credential name (e.g. `influx-token`) to the `(top-level key, field)` it overlays in the parsed
  settings dict (e.g. `("influx", "token")`) - 8 credentials across `influx` (`token`, `user`,
  `password`), `hue` (`user`), `mqtt` (`password`), `mcp` (`password`), `myenergi` (`apikey`),
  `octopus` (`api_key`). `PLACEHOLDER_VALUES`
  matches `example_settings.yaml`'s literal placeholder text per field (`mcp-password`'s is
  deliberately the empty string - see the MCP server section above); `sentinel_for(name)` returns
  the cosmetic string written into `settings.yaml` once a field is migrated (never read back for real
  use - purely informational for a human reading the file). `apply_credential_substitution(settings)`
  overlays whatever's decrypted into `$CREDENTIALS_DIRECTORY` (set by systemd when the unit's
  `LoadCredentialEncrypted=` directives are active) into the settings dict - a no-op when that env var
  is unset, which is what keeps the source-checkout path and any not-yet-migrated packaged install
  byte-for-byte unaffected.
- `toinflux/general.py`'s `load_settings()` calls, in order, right after `yaml.safe_load()` and before
  any other logic touches the parsed dict: `_enforce_settings_file_permissions()` (against an explicit
  `copy.deepcopy()` snapshot of the raw, pre-substitution dict - not dependent on being called before
  `apply_credential_substitution()`, which mutates its input in place), `apply_credential_substitution()`,
  then `_clear_unsubstituted_credential_sentinels()` (blanks any of the 6 fields still holding sentinel
  text after substitution - e.g. `settings.yaml` was migrated but the matching `.cred` file wasn't
  found - so a decoy string can't pass `validate_settings()`'s truthiness checks as if it were real,
  which would otherwise let the daemon start "successfully" and then fail auth forever as a retried
  `SourceConnectionError` instead of failing fast as the `ConfigError` it actually is).
- `_enforce_settings_file_permissions()` is content-aware, not purely mode-based: it only warns/refuses
  when `settings.yaml` is group/other-readable *and* actually contains a real credential (not just a
  placeholder or sentinel) - this is what makes `postinst`'s fresh-install default of `644` (not `600`)
  safe, since a freshly-packaged file never contains a real secret unless a human hand-edits one in.
  Controlled by the `enforce_permissions` settings.yaml key (default `false` when the key is absent, so
  every pre-existing `settings.yaml` keeps working with just a warning; `example_settings.yaml` ships
  `true` explicitly, so new installs enforce by default) - `true` additionally raises `ConfigError`
  instead of just warning.
- `toinflux/credential_cli.py` (`send-to-influx-set-credential`, a second `pyproject.toml` entry point):
  `<name>` encrypts a secret (read from stdin if piped, else an interactive masked prompt) via
  `systemd-creds encrypt`, writes it to `/etc/send-to-influx/credstore.encrypted/<name>.cred`,
  regenerates a systemd drop-in (`/etc/systemd/system/send-to-influx.service.d/50-credentials.conf`,
  rebuilt from a fresh directory listing on every call - idempotent, self-healing, no separate state
  file) with the matching `LoadCredentialEncrypted=` line, and rewrites the corresponding
  `settings.yaml` field to the sentinel text via a `yaml.compose()`-based surgical edit (preserves every
  other byte of the file - comments, ordering - rather than a full load+dump round trip, and refuses
  rather than corrupts if the target isn't a plain single-line scalar). `--remove` reverses this, in a
  specific order for two independent reasons: it rewrites `settings.yaml` back to the placeholder
  *first*, before touching anything else - if that fails (the same "not a plain single-line scalar"
  refusal), nothing else has happened yet, so the credential is still fully intact rather than ending
  up deleted from `systemd-creds` while `settings.yaml` still holds the now-orphaned sentinel (which a
  later `load_settings()` would blank out via `_clear_unsubstituted_credential_sentinels()` and then
  fail `validate_settings()` on - a broken, unrecoverable service for what should have been a clean,
  reversible failure). Only then does it regenerate the drop-in (dropping the credential's line)
  *before* deleting the `.cred` file, never after, since `LoadCredentialEncrypted=NAME:PATH`
  referencing a missing `PATH` hard-fails unit startup with `243/CREDENTIALS` (confirmed via systemd's
  own issue tracker: systemd/systemd#35077, #32667) - the drop-in must never be left pointing at a file
  that's already gone, even transiently.
  `--list` shows configured/not-set per credential. `--set-field`/`--detect-influx-version`/
  `--ensure-influx-storage` support the debconf-driven install flow (below).
- The base `packaging/send-to-influx.service` unit ships zero `LoadCredentialEncrypted=` directives -
  all credential wiring lives purely in the drop-in the CLI manages, so a fresh install that's never
  run the script is byte-for-byte identical to before this feature existed.
- `systemd-creds` availability is checked at runtime (`systemd-creds --version`, must be >= 250, the
  version that introduced it), not via a package-wide `Depends:` floor - Ubuntu 22.04/jammy ships
  systemd 249, one version short, and is otherwise a currently-supported platform (unlike Debian
  11/bullseye, already excluded by the existing `python3 (>= 3.10)` `Depends:`) - a `Depends:` bump
  would make the whole package uninstallable there just to gate one opt-in feature. A missing/too-old
  `systemd-creds` fails with a specific message rather than blocking install.

#### debconf-driven install

`packaging/deb/send-to-influx.templates` + `packaging/deb/config` (copied into `DEBIAN/templates`/
`DEBIAN/config` by `build-deb.sh`, which also adds `debconf (>= 0.5)` to `Depends:`): `config`'s job
is *only* asking questions and stashing answers in debconf's database - a hard Debian packaging
convention, so `dpkg-reconfigure`/backing out of an install never leaves partial side effects. It
never touches the filesystem and never calls into `credential_cli.py` - that only happens later,
from `postinst`, once package files are unpacked and everything's been answered.

- **Install/upgrade/reconfigure gating**: both scripts run their debconf flow only on a genuinely
  fresh install or an explicit `dpkg-reconfigure` - a plain package upgrade neither asks questions
  nor applies answers. `config` is invoked as `configure <previously-configured-version>` by
  `dpkg-preconfigure` (`$2` empty only on a fresh install) and as `reconfigure <version>` by
  `dpkg-reconfigure`, so it early-exits when `$1 != reconfigure && $2` is non-empty. `postinst` is
  invoked as `configure <version>` in both the upgrade and reconfigure cases, so it can't use its
  arguments alone - it checks `DEBCONF_RECONFIGURE=1`, which `dpkg-reconfigure` exports precisely so
  postinsts can tell the two apart (its source calls this "a hack to let postinsts know when they're
  being reconfigured"). Without this gate, debconf's database - a UI cache that persists every
  non-password answer indefinitely, not a change log - was effectively treated as a signal that
  configuration had happened *this* run: every upgrade re-prompted for `influx-secret` (blank,
  contextless - see the `db_set ""` note below), re-warned "not fully configured - not enabling it"
  for every previously-selected source whose password answers had (deliberately) been cleared, and
  re-wrote Open-Meteo's latitude/longitude into `settings.yaml` from the original install's answers,
  reverting any hand edits made since.
- InfluxDB's `influx-url`/`influx-identity`/`influx-secret` are asked *first*, unconditionally -
  deliberately **not** gated on any source being selected. An earlier version of this design asked
  them only after `sources-to-configure` and only if at least one source was picked, exiting `config`
  immediately otherwise - this made InfluxDB unreachable both interactively (an admin who only wants
  to migrate an already-configured InfluxDB credential into systemd-creds, without touching source
  config at all, had no way to get there) and via `dpkg-reconfigure` on a later run. That's a real,
  common case: an admin upgrading an already-working install has no reason to re-answer per-source
  questions for sources that already work, but commonly does want the new systemd-creds option for
  the credential they already have. `identity`/`secret` are generic (org+token for v2, user+password
  for v1), asked without knowing which version applies yet. Version detection deliberately does
  **not** happen in `config`: `config` runs *before* the package is unpacked on a first install, so it
  can't rely on the app's own venv/`requests`, and more fundamentally, gating *what gets asked* on
  being able to reach an arbitrary, possibly-remote, possibly-not-yet-provisioned URL at the exact
  moment of package install would defeat the point of the URL being configurable. Detection happens
  later, in `postinst`, via `send-to-influx-set-credential --detect-influx-version`. This always skips
  TLS verification, unconditionally - unlike `--ensure-influx-storage` (which respects
  `influx.insecure`), it never transmits a credential (both `/health` and `/ping` are unauthenticated
  probes) and its result only picks which prompt fields get routed to, not a trust decision a MITM'd
  response could meaningfully downgrade; `influx.insecure` also isn't necessarily known yet at this
  point, since debconf never asks for it (only ever hand-edited into settings.yaml afterwards).
- `send-to-influx/sources-to-configure` (`Type: multiselect`, priority `high` - matching InfluxDB's
  questions above, so a debconf priority threshold can't show one but silently hide the other) is
  asked next. Per-source blocks are only shown (via `db_input` called conditionally, not declaratively
  in the template file) for sources actually picked, so choosing one or two sources doesn't walk
  through prompts for the other six. Those conditional per-source questions are also priority `high`,
  not `medium` - debconf's default threshold is `high` (`debconf/priority` defaults to `high`), so a
  `medium` follow-up would be silently skipped on a normal install: the user ticks a source in the
  checklist, is never asked for the fields it needs, and postinst then reports it "not fully
  configured" (only `dpkg-reconfigure`, which shows low-priority questions regardless of the
  threshold, ever revealed them - which is why this wasn't caught by reconfigure-based testing).
  There's no prompt-spam risk in `high` here, since each question is only asked at all when its
  source was explicitly selected. Tuning fields (`interval`, `timeout`, `fields` lists,
  `stagger_seconds`/`default_source`) are never prompted for - see the "Template structure" reasoning
  in the original plan for why (`fields` particularly can't be validated against a source's real field
  names at install time). The one deliberate exception is `hue-temperature-units`, which gets a
  *computed* default (checks `$LC_ALL`/`$LANG` for a `_US` territory code, defaulting to Celsius
  otherwise) via `db_set` before the first `db_input`, rather than a silent guess - getting temperature
  units wrong is immediately visible to the user in a way the other tuning fields aren't.
- The shared MQTT broker block (`mqtt-broker-host`/`mqtt-username`/`mqtt-password`) is the second
  shared-infrastructure question group after InfluxDB, but *conditional* where InfluxDB's is
  unconditional: it's only asked (and only processed by `postinst`) when an MQTT-based source
  (currently `nuki`) is in the `sources-to-configure` selection - every install needs InfluxDB,
  but only MQTT sources have a broker, so a non-MQTT install must never be prompted for one.
  Its stored-credential semantics mirror InfluxDB's: a blank `mqtt-password` on reconfigure is
  satisfied by an existing `mqtt-password.cred`, and non-secret fields provided alongside a blank
  secret are still applied - except `mqtt-broker-host`, which is *required* like `hue-host` (not
  blank-keeps like `influx-url`): debconf string answers persist across reconfigures, so any
  install configured through the prompts always has a non-blank host anyway, and a blank one
  means hand-configured - where auto-enable would be speculative (possibly against the shipped
  placeholder host), so those installs hand-edit `sources:` instead, same precedent as
  plaintext-settings credentials. Blank username *and* password mean anonymous broker access - a
  valid configuration, not an incomplete one. Auth must be *coherent* to auto-enable though: a
  username with no password material (neither typed nor stored) warns instead of enabling a
  guaranteed auth-rejection retry loop. And switching an existing authenticated install to
  anonymous is not expressible through the prompts (blank means keep, per the standing
  no-clearing-via-debconf convention) - it's done by blanking `mqtt.username` in settings.yaml
  plus `send-to-influx-set-credential mqtt-password --remove`.
  `mqtt-username` is cleared from debconf's database after use like
  `influx-identity` (the other half of a credential pair), and both are in the final sweep.
- `postinst` (inside the fresh-install-or-reconfigure gate above): `sources-to-configure` is read
  first (`$SOURCES`, purely to know whether *anything* was
  selected - no processing happens from it yet), then InfluxDB is processed unconditionally,
  independent of that selection - a run where every question was
  left blank (e.g. non-interactive) is still a no-op for all of it, since each per-source block below
  self-gates on
  `$SOURCES` containing that source's name; nothing here requires the outer "was anything selected"
  gate the earlier design used. The one place `$SOURCES` matters this early: the "InfluxDB not
  provided" warning only fires if the admin engaged with the prompts this run (selected a source, or
  entered a secret without the matching identity) - a fully-blank run stays silent. Resolves the
  InfluxDB block via
  `--detect-influx-version` and routes `identity`/`secret` accordingly - v2 writes `identity` to the
  plain (non-secret) `influx.org` field via `--set-field` and `secret` to the `influx-token` credential;
  v1 routes both `identity` and `secret` to the `influx-user`/`influx-password` credentials - if
  detection comes back `unknown` (URL unreachable at install time, expected to happen sometimes since
  the URL can point anywhere), nothing is written and no source is auto-enabled this run; a secret
  prompt is never pre-filled with a previous answer on a later `dpkg-reconfigure` (see the `Type:
  password` note below), so that re-run cleanly re-collects them rather than silently reusing something
  stale. Then, per selected source: secrets via `send-to-influx-set-credential <name>`, non-secret fields via
  `--set-field`, both reusing the same CLI rather than a second YAML-patcher in shell. **Auto-enable**:
  a source is only added to `sources:` (via `--enable-source` - `default_source:` is never touched,
  since `sendtoinflux.py` only falls back to it when `sources:` is absent entirely, which is never true
  once `example_settings.yaml` has shipped it non-empty) if *every* required field for it (and the
  InfluxDB block) actually resolved - not just "was it ticked" - with `--ensure-influx-storage`
  attempted first (best-effort database/bucket creation, logged-not-raised on failure). A secret
  field also counts as resolved when its credential is already stored in systemd-creds (`.cred`
  file present in the credstore) - secret prompts always come back blank on a reconfigure ("blank
  keeps the stored value"), so an already-configured install revisiting the prompts (e.g. to add
  one new source) isn't wrongly reported "not fully configured", and `--ensure-influx-storage`
  resolves stored credentials itself, so InfluxDB counting as configured this way still lets
  newly-added sources auto-enable. (A credential kept in plaintext `settings.yaml` and never
  migrated doesn't get this treatment - postinst can't cheaply distinguish it from a placeholder -
  so that setup re-enters secrets on reconfigure or hand-edits `sources:`.)
- `Type: password` answers *are* written to disk by debconf - contrary to an earlier version of this
  note - into a dedicated `passwords.dat` store kept separate from its general-purpose, more widely
  readable answer database, and restricted to `chmod 600`. Debian's own developers' guide
  (`debconf-devel(7)`) advises clearing a password value out of it "as soon as is possible" once
  consumed, so `postinst` does: immediately after each `db_get` on a password-type template
  (`influx-secret`, `hue-user`, `myenergi-apikey`, `octopus-api-key`), it clears the stored answer
  with `db_set <question> ""` (plus `influx-identity`, string-typed but a v1 *username*, and a final
  unconditional sweep of all of them so a preseed for an unselected source can't leave a secret in
  `passwords.dat`),
  regardless of whether the subsequent `systemd-creds` migration for that value goes on to succeed or
  fail. (An earlier version used `db_unregister`, which cleared the value equally well but deleted
  the question's `seen` flag with it - the question was recreated fresh/unseen from the templates
  file on the next run, so debconf re-asked it, blank, on every upgrade; `db_set ""` empties the
  value while leaving the question registered and seen.) Separately, and unrelated to the clearing,
  debconf *never* redisplays/pre-fills a previous password answer in the prompt on a later
  invocation - a UI convention for this template type. So a reconfigure always shows secret prompts
  blank, with no way for `postinst` to distinguish "leave it as-is" from "clear it" from that alone -
  resolved by not supporting clearing via debconf at all: `postinst` treats blank as "keep the existing
  systemd-creds value," and removing a credential goes through `send-to-influx-set-credential <name>
  --remove` directly instead.

### Branch protection

Three rulesets, in decreasing order of strictness - `release/**/*` and `feature/**/*` mirror the same
tiering pattern used on the maintainer's other repos (e.g. `docker-mcp`), adapted to this repo's own
CI check names:

- `main`: no force-pushes/deletion, PR required (1 approval, code-owner review, resolved review
  threads, squash-merge only), Copilot auto-review, CodeQL code scanning, and every check from
  `premerge.yaml` required ("Run flake8", "Run mypy", "Run pytest (3.10)"-"Run pytest (3.14)", "Verify
  .deb build on arm64").
- `release/**/*`: same PR requirements as `main` (1 approval, code-owner review, resolved threads) but
  merge method widened to squash/merge/rebase, and CodeQL and "Verify .deb build on arm64" dropped from
  the required checks (still run, just not a merge-blocking gate at this tier) - kept for longer-lived
  release-prep branches that don't need the full ceremony of `main` on every push.
- `feature/**/*`: one tier looser again - force-pushes/rebasing allowed (no `non_fast_forward` rule),
  and the PR rule relaxed to 0 required approvals and no code-owner review (changes still go through a
  PR and must have review threads resolved, just without needing anyone's sign-off) - for fast
  iteration on shared topic branches without losing CI coverage entirely.
(A fourth `gh-pages` ruleset existed until 2026-07-15, when the branch, its ruleset, and the APT
publish job were retired in favour of [L337-org/apt](https://github.com/L337-org/apt) - that repo
now carries the equivalent `gh-pages` ruleset: `non_fast_forward`, `deletion`,
`required_signatures`, same bypass actors as below.)

All three use the same pair of bypass actors: `OrganizationAdmin` and the repo-admin `RepositoryRole`
(id 5), both `bypass_mode: "always"`. These were re-added by hand on 2026-07-15 - the repo transfer
to `L337-org` silently stripped `bypass_actors` from every ruleset (the rules themselves survived),
which showed up as feature-branch pushes being rejected with "required status checks expected".
The pre-transfer setup had `main` scoped to `bypass_mode: "pull_request"` while the newer three used
`"always"`; the re-add reconciled them all to `"always"`. If a ruleset ever rejects a push that used
to say "Bypassed rule violations", check `bypass_actors` hasn't been emptied again.
