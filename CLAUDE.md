# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Contributor-facing project structure and conventions live in [CONTRIBUTING.md](CONTRIBUTING.md); see
also [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), [SECURITY.md](SECURITY.md), and
[PRIVACY.md](PRIVACY.md).

## Read these before changing the matching area

Each file carries constraints whose violation breaks the product silently. These are not optional
background.

| Before changing | Read |
|---|---|
| `toinflux/mcp*.py`, `toinflux/mcpserver.py` | [architecture/mcp-server.md](architecture/mcp-server.md) |
| any collector under `toinflux/` (not the MCP modules) | [architecture/collectors.md](architecture/collectors.md) |
| `sendtoinflux.py`, `toinflux/general.py` | [architecture/runtime.md](architecture/runtime.md) |
| `packaging/`, `credentials.py`, `credential_cli.py`, or adding a settings section | [architecture/packaging.md](architecture/packaging.md) |
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
.venv/bin/flake8                     # max line length 120, complexity 10
.venv/bin/black .                    # auto-format
.venv/bin/mypy toinflux sendtoinflux.py   # static type check (permissive, see pyproject.toml)
```

### CI

`.github/workflows/premerge.yaml` runs on every push to `main` and every PR: `pytest` (coverage,
matrixed 3.10-3.14), `flake8`, `mypy`, `action-pins`, `arm64-verify` (builds the `.deb` on
`ubuntu-24.04-arm` and runs `packaging/deb/test-packaging.sh` against it), `bookworm-verify` (same
suite in `debian:12` for systemd 252, the oldest systemd-creds supported), and `integration-run` (the
`integration`-marked tests against a mosquitto broker on the runner). Dependabot runs weekly
(`.github/dependabot.yml`).

- **Classify a new check by cost, not importance.** Cheap (seconds) means it gates every tier, so a
  mistake is caught on the branch where it was made. Multi-minute means it gates `main` only, so the
  looser tiers stay fast. See "Branch protection" for the current split.
- **A job GitHub kills on `timeout-minutes` reports `conclusion: cancelled`** - never `failure`, never
  `timed_out`. No failure notification fires and any sweep for failed runs misses it. `release.yaml`
  therefore carries a `report-cancelled-as-failure` job to turn that into a reported failure, because
  it runs unattended and a timeout would mean the `.deb` was never attached with nothing saying so.
  **Never add that job to `premerge.yaml`** - it would fire on every superseded re-push.
- **`mirror-check` ("Check docs mirror") is the one job that is deliberately not required**, so its
  red X is a prompt, never a blocker - one-sided edits are legitimate and a blocker would get
  switched off wholesale. It flags a PR touching part of the documentation set but not all of it:
  `CLAUDE.md`, `.github/copilot-instructions.md`, and the rule-carrying detail layer
  (`architecture/` and `CONTRIBUTING.md`, which owns the new-data-source checklist).
  **Membership is about carrying a rule, not about being linked to** - both instruction files also
  link to README, SECURITY, PRIVACY and CODE_OF_CONDUCT, and a change to those implies nothing about
  the mirror. The list is therefore curated, not derived, and needs revisiting whenever a document
  starts carrying rules. **It is also a touch-test, not a semantics test**: it cannot tell that the
  *same rule* reached each file, so an unrelated edit to the other mirror in the same PR masks a
  genuine one-sided change. Ported from `docker-mcp`'s job of the same name, extended for the detail
  layer this repo has and that one does not.
- **`action-pins` requires every `uses:` in `.github/workflows` and `.github/actions` to name a full
  40-hex commit SHA**, not a tag or branch: a tag can be repointed, and `release.yaml` runs at
  `contents: write`. No exemption for first-party `actions/*`. Local `./` actions *are* exempt. The
  trailing `# vX.Y.Z` comment is convention and deliberately not enforced.

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

- **Timestamps are explicit** unix-epoch seconds: `self.timestamp` if `get_data()` set it, else the
  time `send_data()` is called. Octopus sets it from the reading's own `interval_start` so re-writes
  overwrite rather than duplicate.
- **Field keys are escaped** per line protocol (commas, `=`, spaces). The header is taken verbatim, so
  an unescaped tag value ends the tag set early and writes a corrupt point.
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
- **Nuki writes one point per lock**, tagged `device`, all sharing one timestamp per cycle. The
  backlog is flushed **once per cycle, not once per lock** (`send_data(flush=...)`) - flushing per lock
  charges the head buffered point one rejection each time and burns the whole allowance in one cycle.
- **`Nuki._is_per_device()` discriminates on every value being a mapping**, not on whether `data` was
  given: a lock carries a dict of fields, a heartbeat field never does. Getting this wrong made Nuki
  write no heartbeat at all, silently.
- **Name external values with `!r` in every message.** A lock name comes from MQTT and one containing
  a newline forges a journal line and reaches MCP clients.
- **Subscribe from inside `on_connect`** - a subscription issued before CONNACK completes can be
  silently lost.
- **In the streaming path the paho thread only enqueues**; one worker thread does all InfluxDB I/O, so
  a slow write cannot stall keepalives and drop the connection.
- **Speedtest rejects a `ping` >= 5000 ms** as `SourceConnectionError`: that is the ceiling for a
  genuine measurement, and a total probe failure otherwise reports ~1,800,000 ms as if real.
- **Changing a measurement, tag set or field key means sweeping `tests/integration/` too.** Those
  tests are deselected by default and `pytest -m integration` without a broker skips cleanly, so a
  green local run proves nothing about them. Grep for the old names, run the suite against a real
  broker, then mutate the product back and confirm the test fails.

### MCP server

Optional, off unless both `mcp.user` and `mcp.password` are set (or forced off by `mcp.disabled:
true`). A Streamable-HTTP server on the `mcp` SDK with built-in OAuth 2.1, in its own daemon thread.
Read-only by default: six read tools, one dashboard tool, resources and prompts. A source becomes
writable only when it is `MCP_WRITABLE` *and* the operator sets `<source>.mcp_read_write: true`.

- **A disabled capability is not registered at all**, never registered-and-refusing.
- **Per-source domain knowledge lives on the `DataHandler` subclass** as class attributes
  (`MCP_MEASUREMENT`, `MCP_TAG_FILTERS`, `MCP_INSTANCE_TAG`, `MCP_FIELD_METADATA`, `MCP_DESCRIPTION`,
  `MCP_LIVE_STATE`), never as a parallel schema in the MCP modules.
- **Every tool registers through `register_tool()`** in `toinflux/mcp_common.py`, never
  `@server.tool()` directly: CPython 3.13+ strips docstring indentation at compile time and 3.10-3.12
  do not, so a direct registration advertises ~1.2 KB of leading whitespace on the older half of the
  supported range, invisible to anyone developing on 3.13+.
- **Grafana vocabulary stays in `toinflux/mcp_dashboards.py`.** `mcp_read` does not import it, so the
  leak is structurally impossible rather than merely avoided.
- **Injection defence:** measurement and tags come from the static schema, a field must match a
  live-discovered key, every identifier is charset-validated and quoted, times are re-emitted as
  RFC3339, aggregations come from a fixed map. Never add a query path that bypasses this.
- **The advertised surface is held to the AI-consumer standard** and guarded by
  `tests/test_mcp_surface.py`: a description and distinct title on everything, a `SIBLINGS` entry, no
  reference to a tool that does not exist, and a recorded byte budget.

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
- **Diagnostics go to stderr; stdout carries the program's data.** Every log level, plus
  `--check-config`'s error output, is on stderr; `--dump`/`--print` JSON and `Configuration OK` are on
  stdout. This is what makes `--dump | jq` reliable when a dump partially fails.

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
- **Assert the promise, not the implementation.** A `MagicMock` handler never runs the source's own
  override, so a test built on one asserts what the caller *asked for* and never what the handler
  *did* - which is how a source writing no heartbeat at all passed the suite.
- **`tests/test_field_metadata.py` is the coverage ratchet** for MCP field metadata: every entry must
  say something and declare a valid `kind`, and UNITS.md must agree about every unit and coded value.
  The UNITS.md check is deliberately one-way and compares units and codes only, never prose.

### Adding a new data source

The checklist is in [CONTRIBUTING.md](CONTRIBUTING.md), which covers the code, the MCP metadata, the
debconf install flow and the packaging test suite. Follow it there rather than reconstructing it.

## Branch protection

Three rulesets, decreasing strictness. All three enforce Copilot auto-review on push and block branch
deletion.

- **`main`**: no force-push, signed commits, 1 approval with code-owner review, squash-only, and all
  twelve checks required.
- **`release/**/*`**: same PR requirements, no force-push, but the four expensive checks
  (CodeQL, `arm64-verify`, `bookworm-verify`, `integration-run`) are not merge gates.
- **`feature/**/*`**: **no PR rule at all** - push directly, force-push and rebase allowed. The eight
  cheap checks still gate it. A fix for a review comment on a feature -> `main` PR goes straight to the
  feature branch.

The eight cheap checks gating every tier: "Run flake8", "Run mypy", "Run pytest (3.10)" through
"(3.14)", and "Action pins are immutable".

**Read the live config before trusting this section** - `gh api repos/L337-org/send-to-influx/rulesets`.
It is a hand-maintained mirror and has been wrong before. If a ruleset rejects a push that used to
report "Bypassed rule violations", check `bypass_actors` has not been emptied.

## Rejected designs

Do not re-propose these. Reasoning is in the Confluence decision records.

- **Environment-variable secrets** (`INFLUX_TOKEN` etc. via an `EnvironmentFile=`). Removed: it was an
  organisational boundary, not a security one, and added `/proc/<pid>/environ` exposure without
  removing any existing path. `systemd-creds` was implemented instead, and does create a real
  boundary. See [architecture/packaging.md](architecture/packaging.md).
- **A deprecation window on an MCP tool parameter.** A client fetches the schema at session start, so
  there is no stored caller to break and an alias costs context on every session to cover a window
  shorter than one conversation. Ask whether the caller persists before adding one anywhere.
