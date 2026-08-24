# Copilot Instructions for send-to-influx

Python daemon collecting smart-home and energy-device metrics into InfluxDB (v1 and v2) via the line
protocol, plus an optional MCP server exposing that data, and control of the devices that support it,
to an AI client.

This file is the review-facing mirror of [CLAUDE.md](../CLAUDE.md). Keep the two in step: they must be
updated in the same change.

## Review priorities

In order. The first two are where real defects in this codebase have come from.

1. **Emitted-data changes.** A measurement, tag key or field key is an interface nothing type-checks.
   A rename silently breaks Grafana panels and stored queries.
2. **Silent-failure invariants**, listed below. Each one has already caused a defect that logged
   nothing useful or nothing at all.
3. Correctness, then error handling, then tests, then documentation.

## Read before reviewing a change to these areas

| Area | File |
|---|---|
| `toinflux/mcp*.py`, `toinflux/mcpserver.py` | [architecture/mcp-server.md](../architecture/mcp-server.md) |
| any collector under `toinflux/` | [architecture/collectors.md](../architecture/collectors.md) |
| `sendtoinflux.py`, `toinflux/general.py` | [architecture/runtime.md](../architecture/runtime.md) |
| `packaging/`, `credentials.py`, `credential_cli.py` | [architecture/packaging.md](../architecture/packaging.md) |
| a new data source | [CONTRIBUTING.md](../CONTRIBUTING.md) checklist |

## Invariants a change must not break

### Collectors

- `get_data()` populates `self.data` and `self.influx_header`; `send_data()` in `DataHandler` does the
  rest. Timestamps are explicit unix-epoch seconds.
- Field keys are escaped per line protocol. The header is taken verbatim, so an unescaped tag value
  ends the tag set early and writes a corrupt point.
- **A tag value is escaped, never normalised.** Rewriting one changes the series identity of an
  existing install.
- `DataHandler._write_buffers` is **class-level**, keyed by `worker_key` (the `(source, instance)`
  tuple). An instance attribute does not survive the worker rebuilding the handler after a failure.
  Keying by source name alone races two instances of one source.
- `worker_label` is display-only. Never use it as a key or a tag value.
- **Every Hue bridge URL goes through `Hue._api_base()`**, hosts through `_url_host()` (which brackets
  a bare IPv6 literal). Reject a second copy of that f-string: the original bug was two copies.
- **Every Hue error message passes `Hue._redact()` before being logged or raised.** The token is in the
  URL path and `requests` puts the URL in exception messages. Missing this leaks it to the journal, the
  logfile and any MCP client.
- Hue bridge slot numbers bind a host to its token. **Never renumber, never assume contiguity.** Only
  `bridge_field_names()` knows the numbering, so reject any `f"host{n}"`.
- Self-contradictory config is a fatal `ConfigError`; "not usable yet" (absent host, placeholder token)
  is only a **warning**. The shipped example is that state, so raising stops every collector.
- Nuki writes **one point per lock**, tagged `device`, sharing one timestamp per cycle, and flushes the
  backlog **once per cycle, not per lock**. Per-lock flushing burns the whole rejection allowance in
  one cycle.
- `Nuki._is_per_device()` discriminates on every value being a mapping, not on whether `data` was
  passed. Getting this wrong made Nuki write no heartbeat at all, silently.
- **External values are named with `!r`** in every message. A lock name comes from MQTT; one containing
  a newline forges a journal line and reaches MCP clients.
- MQTT subscribes from inside `on_connect`. A subscription issued before CONNACK can be silently lost.
- In the streaming path the paho thread **only enqueues**; one worker thread does all InfluxDB I/O, so
  a slow write cannot stall keepalives.
- Speedtest rejects `ping` >= 5000 ms. That is the ceiling for a real measurement; a total probe
  failure otherwise reports ~1,800,000 ms as if genuine.

### MCP server

- **A disabled capability is not registered at all**, never registered-and-refusing. Flag anything that
  registers a tool then refuses it based on config.
- Per-source domain knowledge lives on the `DataHandler` subclass as class attributes
  (`MCP_MEASUREMENT`, `MCP_TAG_FILTERS`, `MCP_INSTANCE_TAG`, `MCP_FIELD_METADATA`, `MCP_DESCRIPTION`,
  `MCP_LIVE_STATE`). Reject a parallel schema inside the MCP modules.
- **Tools register through `register_tool()`, never `@server.tool()` directly.** CPython 3.13+ strips
  docstring indentation and 3.10-3.12 do not, so a direct registration advertises ~1.2 KB of leading
  whitespace on the older half of the supported range.
- Every tool needs a `title=` distinct from its own name, and `annotations=ToolAnnotations(...)` with
  `read_only_hint` set explicitly plus `destructive_hint` when it is `False`.
- Grafana vocabulary stays in `toinflux/mcp_dashboards.py`. `mcp_read` must not import it.
- Every `MCP_FIELD_METADATA` entry declares a `kind`, and getting it wrong is silent: `interval` is a
  quantity accumulated over its reporting period and must be summed, `gauge` is instantaneous and must
  not be. A field whose description says "during one interval" while its kind says `gauge` is the bug
  this vocabulary exists to prevent - check the two agree.
- **Injection defence:** measurement and tags from the static schema, fields matched against a
  live-discovered allowlist, every identifier charset-validated and quoted, times re-emitted as
  RFC3339, aggregations from a fixed map. Reject any query path that bypasses this.
- `ToolParamError` for a caller/model mistake, `SourceConnectionError` for a transient transport
  failure. Keep them distinct.

### Packaging and install

- `/etc/send-to-influx/settings.yaml` is **deliberately not a dpkg conffile**. Do not "fix" this;
  maintainer scripts write to it and Debian Policy 10.7.3 forbids the combination.
- **A new settings section must be `--ensure-section`ed by `postinst` before any field is written or the
  source enabled.** Otherwise `--set-field` fails, the source is still enabled, and `load_settings()`
  raises a fatal `ConfigError` that stops every collector on existing installs.
- `preinst` must keep its `DEBCONF_RECONFIGURE=1` early exit. `dpkg-reconfigure` runs `preinst` with no
  unpack following, so without the guard it destroys the installation.
- Clear a consumed debconf password with `db_set <question> ""`, **never `db_unregister`** (which
  deletes the seen flag and causes blank re-prompts on every upgrade).
- Auto-enable a source only when every required field resolved. "Was it ticked" is not enough.
- The OAuth state file lives in the systemd `StateDirectory`. `/etc/send-to-influx` stays root-owned;
  the service user must not get write access to the directory holding the credential store.
- New conditional debconf questions are priority `high`. Debconf's default threshold is `high`, so
  anything lower is silently skipped on a normal install.
- New entries in the `sources-to-configure` `Choices` list are **appended**. `test-packaging.sh` selects
  by position number.

### Runtime and settings

- `expand_sources()` serves `--source`, the supervisor and `--dump` alike. Reject a second
  implementation. All restart, stall and buffer bookkeeping is keyed by work unit, not source name.
- Configuration faults are caught at validation, not at first collection.
- `--check-config` prints OK only if validation passes **and** something is actually requested.
- **Diagnostics to stderr, program data to stdout.** All log levels and `--check-config` errors on
  stderr; `--dump`/`--print` JSON and `Configuration OK` on stdout.
- Only collectable sources belong in the `get_class()` registry. A registered abstract parent validates,
  constructs, then dies with `AttributeError` every cycle.

## Style and conventions

- flake8: max line length 120, complexity 10. black for formatting. mypy (permissive).
- Docstrings on every public module, class and function; standalone first-line summary, blank line,
  structured params/returns/raises. No change history, authors or issue keys.
- Project-defined exceptions across module boundaries, never bare built-ins. Preserve the cause when
  wrapping. Never catch broadly and continue.
- **Errors must not destroy information the caller needs.** If the operation still produced a useful
  result, return it including the failure status; if it did not, raise.
- Prose is **British English in plain ASCII punctuation**: no em or en dashes (use `-` or `:`), `...`
  not an ellipsis character, `x` not the multiplication sign, straight quotes, hyphens in ranges.
  Applies to every file that ships, including code comments, README, UNITS.md, CONTRIBUTING.md and
  `architecture/`. Keep a non-ASCII character only where it is a symbol carrying meaning (`m³`, `≥`,
  box-drawing in a diagram). `CLAUDE.md` and this file are exempt.
- **Every `uses:` in `.github/workflows` and `.github/actions` must name a full 40-hex commit SHA**, not
  a tag or branch: a tag can be repointed, and `release.yaml` runs at `contents: write`. No exemption
  for first-party `actions/*`. Local `./` actions are exempt. The trailing `# vX.Y.Z` comment is
  convention and not enforced, so do not flag its absence or format.
- A `TODO` without a raised issue is not a TODO. In this public repo, describe the work rather than
  naming an issue key.
- **No issue keys and no wiki links anywhere in the repo**, including commit messages and PR
  descriptions.

## Testing requirements

- Test module per source module. Mock `load_settings`, HTTP and file I/O; shared fixtures in
  `tests/conftest.py`.
- Integration tests are deselected by default and skip cleanly without a broker.
- **Test the failure paths, and test what the failure says**, not only that it happened.
- **Assert the promise, not the implementation.** A `MagicMock` handler never runs the source's own
  override, so a test built on one asserts what the caller asked for, never what the handler did. That
  is how a source writing no heartbeat at all passed the suite.
- **A change to a measurement, tag set or field key must sweep `tests/integration/`.** A green local run
  proves nothing there. Flag any emitted-data change whose diff does not touch those tests.
- **A rename or behaviour change must be accompanied by a search for tests and docstrings naming the
  changed thing.** A passing test can assert something that has stopped being true.
- `tests/test_field_metadata.py` is the ratchet for MCP field metadata and its agreement with UNITS.md.
  A new or changed `MCP_FIELD_METADATA` entry needs the UNITS.md row in the same change.

## Documentation requirements

- Documentation changes in the **same** commit as the behaviour change, never a follow-up.
- **`CLAUDE.md` and this file are mirrors and must change together.** This is the most-forgotten step.
  Flag a change that touches one and not the other.
- A change to a subsystem's behaviour updates the relevant `architecture/` file too.
- Emitted-data changes update UNITS.md, and a breaking one updates UPGRADING.md.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Normal exit |
| 1 | Fatal `ConfigError`, or nothing to collect. Marked `RestartPreventExitStatus` in the unit. |
| 2 | `SourceConnectionError` in `--dump` mode only. Continuous mode retries with backoff instead. |

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
- Environment-variable secrets were removed deliberately. Do not propose an `EnvironmentFile=` for
  secrets; `systemd-creds` is the supported mechanism.
- MCP tool parameters get no deprecation window: clients fetch the schema each session, so there is no
  stored caller to break.
