# AGENTS.md

The shared instruction file for this repository. Every assistant reads this one; `CLAUDE.md` and
`.github/copilot-instructions.md` are pointers to it.

**send-to-influx** collects metrics from smart home and energy devices and writes them to InfluxDB
using the line protocol, and optionally exposes that data, and control of the devices that support
it, to an AI client over MCP. Both InfluxDB v1 (user/password) and v2 (token/org/bucket) are
supported.

## Review priorities

In order. The first two are where real defects in this codebase have come from.

1. **Emitted-data changes.** A measurement, tag key or field key is an interface nothing type-checks.
   A rename silently breaks Grafana panels and stored queries.
2. **Silent-failure invariants**, listed below. Each one has already caused a defect that logged
   nothing useful or nothing at all.
3. Correctness, then error handling, then tests, then documentation.

## Read these before changing the matching area

Each file carries constraints whose violation breaks the product silently. These are not optional
background.

| Before changing | Read |
|---|---|
| `toinflux/mcp*.py`, `toinflux/mcpserver.py` | [architecture/mcp-server.md](architecture/mcp-server.md) |
| any collector under `toinflux/` (not the MCP modules) | [architecture/collectors.md](architecture/collectors.md) |
| `sendtoinflux.py`, `toinflux/general.py` | [architecture/runtime.md](architecture/runtime.md) |
| `packaging/`, `toinflux/credentials.py`, `toinflux/credential_cli.py`, or adding a settings section | [architecture/packaging.md](architecture/packaging.md) |
| adding a data source | the checklist in [CONTRIBUTING.md](CONTRIBUTING.md) |

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
.venv/bin/flake8
.venv/bin/black .                    # auto-format
.venv/bin/mypy toinflux sendtoinflux.py
```

## Architecture

**send-to-influx** collects metrics from smart home / energy devices and writes them to InfluxDB using
the [line protocol](https://docs.influxdata.com/influxdb/v1/write_protocols/line_protocol_tutorial/).
Both InfluxDB v1 (user/password) and v2 (token/org/bucket) are supported.

### Class hierarchy

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

Each subclass implements `get_data()`, which populates `self.data` (dict) and `self.influx_header`
(measurement/tag string); `send_data()` in the base takes it from there. `MyEnergi` and `DataHandler`
are abstract and are **not** registered as selectable sources.

Full detail: [architecture/collectors.md](architecture/collectors.md).

### Collector invariants

Break one of these and the failure is silent or data-destroying.

- **A tag value is never normalised**, only escaped - rewriting one changes the series identity of an
  existing install.
- **`DataHandler._write_buffers` is class-level, keyed by `worker_key`** (the `(source, instance)`
  tuple). The worker loop discards and rebuilds the handler after every failure, so an instance
  attribute would not survive to be flushed; keying by source name alone would race two instances of
  one source against each other. `worker_label` is display-only and never a key or a tag value.
- **Every Hue bridge URL goes through `Hue._api_base()`**, and hosts through `_url_host()`, which
  brackets a bare IPv6 literal. The bug existed in two copies of one f-string, which is exactly how a
  second copy would reintroduce it.
- **Every Hue error message passes through `Hue._redact()` before being logged *or* raised.** The
  token sits in the URL path and `requests` puts the URL in its exception messages, so without this it
  reaches the journal, the logfile, and any connected MCP client. Hue is the only source needing this.
- **Bridge slot numbers are the binding between a host and its token. Never renumber, never assume
  contiguity.** `enumerate_bridges()` is the single source of which bridges exist;
  `bridge_field_names()` is the only place that knows the numbering, so never build `f"host{n}"`.
- **The severity split is load-bearing:** self-contradictory config is a fatal `ConfigError`; "not
  usable yet" (no host, or a placeholder token) is only a **warning**, because the shipped example is
  exactly that state and raising would stop every collector.
- **Name external values with `!r` in every message.** A lock name comes from MQTT and one containing
  a newline forges a journal line and reaches MCP clients.
- **Changing a measurement, tag set or field key means sweeping `tests/integration/` too.** Those
  tests are deselected by default and `pytest -m integration` without a broker skips cleanly, so a
  green local run proves nothing about them. Grep for the old names, run the suite against a real
  broker, then mutate the product back and confirm the test fails.

**Guarded, so not review items** - `tests/test_repo_hygiene.py::test_every_named_guard_exists`
keeps these names honest, and each guard's docstring carries the reasoning.

- `tests/test_influx.py::TestDataHandler::test_send_data_appends_explicit_timestamp`, and its two
  siblings for `self.timestamp` and the default
- `tests/test_influx.py::TestDataHandler::test_send_data_escapes_field_keys`
- `tests/test_octopus.py::TestOctopus::test_get_data_sets_timestamp_from_interval_start`
- `tests/test_repo_hygiene.py::test_every_dynamic_tag_value_in_a_header_is_escaped`
- `tests/test_nuki.py::TestPerLockPoints::test_the_backlog_is_flushed_once_per_cycle_not_once_per_lock`
- `tests/test_nuki.py::TestPerLockPoints::test_the_shape_discriminator` - `_is_per_device()` keys on
  every value being a mapping
- `tests/test_mqtt.py::TestStreamMqttMessages::test_resubscribes_on_every_connect`
- `tests/test_mqtt.py::TestStreamMqttMessages::test_message_callback_runs_off_the_network_thread`
- `tests/test_speedtest.py::TestSpeedtest::test_get_data_raises_source_connection_error_on_implausible_ping`
  - the ceiling is 5000 ms

### MCP server

Optional, off unless both `mcp.user` and `mcp.password` are set (or forced off by `mcp.disabled:
true`). A Streamable-HTTP server on the `mcp` SDK with built-in OAuth 2.1, in its own daemon thread.
Read-only by default: the read tools, the dashboard tool, resources and prompts. A source becomes
writable only when it is `MCP_WRITABLE` *and* the operator sets `<source>.mcp_read_write: true`. A
disabled capability is not registered at all rather than registered-and-refusing:
`tests/test_mcp_write.py::TestWriteToolRegistration::test_no_write_tools_when_nothing_enabled`.

- **Per-source domain knowledge lives on the `DataHandler` subclass** as class attributes
  (`MCP_MEASUREMENT`, `MCP_TAG_FILTERS`, `MCP_INSTANCE_TAG`, `MCP_FIELD_METADATA`, `MCP_DESCRIPTION`,
  `MCP_LIVE_STATE`), never as a parallel schema in the MCP modules.
- **A source whose field keys are per-install describes them through `mcp_field_metadata()`**, the
  hook the read layer calls instead of reading `MCP_FIELD_METADATA` directly. Hue is the only one:
  its keys are the operator's device names, so the collector records each device's class to the
  `hue_devices` measurement and the hook reads it back. An override must be best-effort and never
  raise - metadata is an annotation, and a field with none is a smaller failure than a schema call
  that errors. Read [architecture/mcp-server.md](architecture/mcp-server.md) before changing it.
- **Register every tool through `register_tool()`** (`toinflux/mcp_common.py`) and every resource
  through `_register_resource()` (`toinflux/mcp_resources.py`), never `@server.tool()`/
  `@server.resource()` directly. Both re-raise a `ToInfluxError` as the SDK's
  `ToolError`/`ResourceError`; without that the SDK treats it as a crash and the caller is told only
  `Error executing tool <name>` or `Error reading resource <uri>`. `register_tool()` additionally
  dedents the docstring - 3.13+ strips indentation at compile time and 3.10-3.12 do not, so a direct
  registration advertises ~1.2 KB of leading whitespace on the older half of the supported range.
  `_register_resource()` does not dedent, because every resource passes `description=` explicitly;
  a resource that ever relies on its docstring instead needs the same `cleandoc` treatment.
- **Never widen the catch to `Exception`** - a real bug must stay a crash, logged with its traceback
  and its text withheld. The translation catches `ToInfluxError` rather than a list of subclasses
  precisely so that inheriting is enough: when it was a list, `ConfigError` was missing from it and
  every unconfigured-device failure reached the model saying nothing. Inheritance is swept for by
  `tests/test_mcp_common.py::TestEveryDeliberateFailureIsTranslated::test_every_project_exception_is_a_toinflux_error`;
  the widening is not, so that half is yours to check.
- **A new tool or resource needs a test for what it says when it fails**, not only for what it
  returns when it works. `tests/test_mcp_surface.py` fails any registration that bypasses the
  registrars, because a bypass returns the right payload and passes every behaviour test.
- **Grafana vocabulary stays in `toinflux/mcp_dashboards.py`.** `mcp_read` does not import it, so the
  leak is structurally impossible rather than merely avoided.
- **Injection defence:** measurement and tags come from the static schema, a field must match a
  live-discovered key, every identifier is charset-validated and quoted, times are re-emitted as
  RFC3339, aggregations come from a fixed map. Never add a query path that bypasses this.
- **The advertised surface is held to the AI-consumer standard** in full by
  `tests/test_mcp_surface.py` - descriptions, titles, siblings, dangling references, byte budget.
  Read it, not this file, before changing a description: all of it fails CI on its own.

Full detail: [architecture/mcp-server.md](architecture/mcp-server.md).

### Entry point and settings

`sendtoinflux.py` expands requested sources into `(source, instance)` work units via
`expand_sources()` and runs one worker per unit - single-source mode on the main thread, otherwise one
daemon thread each with a startup stagger. `SourceConnectionError` is retried with exponential backoff
(5 s base, 300 s max); `ConfigError` is never retried.

- **`expand_sources()` serves `--source`, the supervisor and `--dump` alike**, so they cannot disagree
  about what runs. All restart, stall and buffer bookkeeping is keyed by work unit, not source name.
- **Configuration faults are caught at validation, not at first collection.** A non-mapping source
  section and an uncollectable source name are both terminal errors from validation.
- **`--check-config` prints OK only if validation passes *and* something is actually requested.** "OK"
  must not mean "nothing will happen".
- **Diagnostics go to stderr; stdout carries the program's data**, so `--dump | jq` stays reliable
  when a dump partially fails. Guarded end to end by `tests/test_sendtoinflux.py::TestOutputStreams`:
  `test_every_level_goes_to_stderr`, `test_check_config_verdict_on_stdout_failure_on_stderr` and
  `test_a_partially_failing_dump_leaves_stdout_parseable`.

Full detail: [architecture/runtime.md](architecture/runtime.md).

### Packaging

`packaging/deb/build-deb.sh` builds an `Architecture: all` `.deb` bundling a venv under
`/opt/send-to-influx`, with a systemd unit and debconf-driven configuration. Secrets can optionally
move into `systemd-creds`.

- **`/etc/send-to-influx/settings.yaml` is deliberately not a dpkg conffile** - maintainer scripts
  write to it, and Debian Policy 10.7.3 forbids the combination. The example ships to
  `/usr/share/send-to-influx/` and is copied in only if absent. Upgrades never touch the live file.
- **A new settings section must be `--ensure-section`ed by `postinst` before any field is written or
  the source enabled.** `settings.yaml` is written once at install time, so a section added by a later
  release does not exist on existing installs: `--set-field` fails, the source still gets enabled, and
  `load_settings()` then raises a fatal `ConfigError` that stops **every** collector.
- **`preinst` deletes the whole bundled venv, and the `DEBCONF_RECONFIGURE=1` early exit at the top is
  essential.** `dpkg-reconfigure` also runs `preinst` as `upgrade <version>`, indistinguishable by
  arguments alone, but with no unpack following - so without the guard a reconfigure destroys the
  installation.
- **Clear a consumed debconf password with `db_set <question> ""`, never `db_unregister`** - the latter
  deletes the seen flag and causes blank re-prompts on every upgrade.
- **Auto-enable a source only when every required field actually resolved.** "Was it ticked" is not
  enough.
- **The OAuth state file lives in the systemd `StateDirectory`, not `/etc`.** `/etc/send-to-influx`
  stays root-owned; the service user must never get write access to the directory holding
  `settings.yaml` and the credential store.

Full detail, including the debconf flow and the credential CLI:
[architecture/packaging.md](architecture/packaging.md).

### Exceptions (`toinflux/exceptions.py`)

- `ConfigError`: fatal, non-retryable (missing/invalid settings, unknown source). Exits the process.
- `SourceConnectionError`: transient problem talking to a source's API. Retried with backoff.
- `ToolParamError`: a non-retryable caller/model mistake on an MCP tool. Surfaced to the model.

**Swapping `ConfigError` for `SourceConnectionError` is a real defect, not a style choice**: one stops
a worker that can never succeed, the other retries forever. Both directions are mutation-tested.

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Normal exit |
| 1 | A condition needing manual intervention that never resolves by waiting: fatal `ConfigError`, or "nothing to collect". `packaging/send-to-influx.service` marks this `RestartPreventExitStatus`, so the packaged service is not respawned instead of crash-looping. |
| 2 | `SourceConnectionError` in `--dump` mode only - there is no worker loop to retry a one-shot dump. Continuous mode always retries with backoff instead of exiting. |

### Testing conventions

- Mock `load_settings`, HTTP calls, and file I/O so tests run without real config or network.
- Shared fixtures live in `tests/conftest.py`.
- **`tests/test_field_metadata.py` is the coverage ratchet** for MCP field metadata: every entry must
  say something and declare a valid `kind`, and UNITS.md must agree about every unit and coded value.
  The UNITS.md check is deliberately one-way and compares units and codes only, never prose.

## Rejected designs

Do not re-propose these. The reasoning is recorded with the decision.

- **Environment-variable secrets** (`INFLUX_TOKEN` etc. via an `EnvironmentFile=`). Removed: it was an
  organisational boundary, not a security one, and added `/proc/<pid>/environ` exposure without
  removing any existing path. `systemd-creds` was implemented instead, and does create a real
  boundary. See [architecture/packaging.md](architecture/packaging.md).
- **A deprecation window on an MCP tool parameter.** A client fetches the schema at session start, so
  there is no stored caller to break and an alias costs context on every session to cover a window
  shorter than one conversation. Ask whether the caller persists before adding one anywhere.

## Deliberate: do not flag these

Each has been raised before and declined with reasons recorded.

- `settings.yaml` is not a dpkg conffile (above).
- The Hue `host` tag is escaped but not normalised, and an IPv6 bridge keeps its original spelling.
- `SHOW FIELD KEYS` is per-measurement, so `list_fields` shows sibling MyEnergi devices' fields. A query
  for a cross-device field returns no points. Accepted limitation.
- The UNITS.md consistency check is one-way (metadata implies a row, not the reverse) and compares units
  and codes only, never prose.
- `sno` is written as a field on any install with no `fields` list configured.
- `LIMIT` applies per series once a query groups by a tag; the read layer divides it and reports
  `limit_per_instance`.
- The write buffer is not persisted across a restart, and flushes to whatever destination current
  settings resolve to.

## Docstrings

**Google style** - `Args:` and `Returns:` sections, capitalised, with the type in the entry
because this code carries no annotations. `flake8-docstrings` enforces it, and the convention
and the exemptions live in `tox.ini`. There is no second dialect: the Sphinx `:param:` /
`:return:` form this code used to carry is gone.

**No pydocstyle rule is ignored**, so nothing is parked there. Two things are exempt by name
rather than by rule, both covered below: tool docstrings, and tests.

**`pydoclint` answers the question pydocstyle cannot**: does the docstring agree with the
signature? D417 only checks the parameters of a section a docstring already has, so a function
documenting none of its parameters passes it, silently, in every dialect this repository has
used. `pydoclint` runs as a flake8 plugin in the same job. Its
backlog is the `DOC` entries in `tox.ini`: an entry comes off with the change that fixes
everything it names, nothing is added to it, and every code absent from the list is enforced.

One of its judgements is worth knowing before it surprises you: a bare `return` counts as
returning something, so a function that exits early without a value still wants a `Returns:`
section by DOC201 - even though writing `Returns: None` is DOC202. Both are in the backlog.

Two of its judgements are worth knowing before they surprise you, because they are finer than
"document what you return":

* A function with **no `return` statement at all** omits `Returns:` - writing `Returns: None`
  there is DOC202.
* A function with a **bare `return`** as an early exit must *have* a `Returns:` section -
  omitting it is DOC201 - even though it returns nothing either. Say what the caller gets;
  `check-return-types = False` is what stops the absent annotation making that a type mismatch.

**Tool docstrings are exempt, and must stay exempt.** A tool's docstring *is* its advertised
description, and the schema beside it already carries every parameter's type - so CS.6.14 hands
it to the AI-consumer rules instead, where D417 would otherwise demand an `Args:` block
duplicating the schema on every session that loads the surface. `ignore-decorators` in `tox.ini`
does that, and `tests/test_repo_hygiene.py::test_the_docstring_exemption_matches_the_decorators_in_use`
fails if a rename ever makes the pattern stop matching.

**Prompts and resources are not exempt.** Both pass `description=` explicitly at registration,
so their docstrings reach no client at all and there is nothing to trade off. They follow the
ordinary convention, and the same guard fails if either is added back to the pattern.

Tests are exempt too: a test's name is its documentation.

<!-- BEGIN GENERATED -->
## Read these when they apply

- Read `.agents/policy/review-context.md` always - these apply to every activity.
- Read `.agents/policy/testing.md` when writing or running tests, or adding behaviour that needs them.
- Read `.agents/policy/architecture.md` when changing module structure, public surface, docstrings, generated files, deprecation, or log levels.

<!-- END GENERATED -->
