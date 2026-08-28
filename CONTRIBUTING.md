# Contributing to send-to-influx

Contributions are welcome. By participating, you're expected to uphold the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Before you start

`send-to-influx` collects data from various smart home / energy monitoring devices and APIs and
writes it to InfluxDB using the [line protocol](https://docs.influxdata.com/influxdb/v1/write_protocols/line_protocol_tutorial/),
for visualisation in Grafana. Each data source is a small, mostly-independent Python class; the
practical mental model for finding your way around is a parent/child class hierarchy with one
module per source.

The `architecture/` directory is the deep implementation reference, one file per area. This file
(`CONTRIBUTING.md`) covers the practical day-to-day: project layout, the checklist for adding a new
source, testing, and submitting changes. `AGENTS.md` is the shared instruction file that briefs
both Claude Code and GitHub Copilot, and carries the same rules in condensed form. If something
isn't covered here, look in `architecture/`.

## Project layout

```
.
├── sendtoinflux.py         # entry point - CLI parsing, single/multi-source worker loops, retry/backoff
├── toinflux/                # the package
│   ├── __init__.py         # re-exports the public factory/settings functions and all source classes
│   ├── general.py          # load_settings(), validate_settings(), get_class() (factory), configure_logging()
│   ├── exceptions.py       # ConfigError (fatal) / SourceConnectionError (retryable)
│   ├── influx.py           # DataHandler base class - owns send_data() (line protocol + InfluxDB HTTP POST)
│   ├── philipshue.py       # Hue
│   ├── myenergi.py         # MyEnergi (shared auth) + Zappi / Eddi / Harvi
│   ├── carbonintensity.py  # CarbonIntensity
│   ├── openmeteo.py        # OpenMeteo
│   ├── octopus.py          # Octopus
│   └── speedtest.py        # Speedtest
├── tests/                  # pytest suite, mirrors toinflux/ one-to-one
│   └── conftest.py         # shared fixtures (e.g. sample_settings)
├── packaging/              # .deb + systemd packaging (see architecture/packaging.md)
├── architecture/           # deep implementation reference, one file per area
├── example_settings.yaml   # template settings file - copy to settings.yaml to run
└── UNITS.md                # field-by-field reference of what each source collects and its units
```

Every subclass inherits `DataHandler` (`toinflux/influx.py`) and implements `get_data()`, which
populates `self.data` (dict) and `self.influx_header` (InfluxDB measurement/tag string);
`send_data()` in the base class takes it from there - formatting, escaping, timestamping, and
POSTing to InfluxDB are all handled once, in one place.

## Conventions

- Line length is 120 characters (enforced by `flake8`/`black`).
- Docstrings follow the existing `:param:`/`:type:`/`:return:`/`:rtype:` style.
- Raise `SourceConnectionError` for a transient problem talking to a source's API (network error,
  bad auth, bad response) - the worker loop retries these with backoff. Raise `ConfigError` for a
  fatal, non-retryable problem (missing/invalid settings, unknown source) - these exit immediately
  in single-source mode, or stop just that source's worker in multi-source mode, without retrying.
  Don't call `sys.exit()` directly from library code (`toinflux/`) - only `sendtoinflux.py` itself
  should ever call `sys.exit()`.
- Mock `load_settings`, HTTP calls, and file I/O in tests so they run without real config or
  network access - see "Testing conventions" below.

## Testing conventions

Unit tests mock the settings loader and HTTP calls rather than hitting a real device/API. A
minimal example, from `tests/test_octopus.py`:

```python
from unittest.mock import patch
from toinflux.octopus import Octopus

def test_get_data_sets_timestamp_from_interval_start(sample_settings):
    with patch("toinflux.influx.load_settings") as mock_load_settings:
        mock_load_settings.return_value = sample_settings
        handler = Octopus(source="octopus")
        with patch.object(handler.session, "get", side_effect=_mock_get([consumption_response])):
            handler.get_data()
            assert handler.timestamp == 1783328400
```

Shared fixtures (e.g. `sample_settings`, a minimal valid settings dict) live in
`tests/conftest.py` - reuse them rather than building settings dicts from scratch in each test
file. No real configuration or network access is required to run the suite; the same tests run
in CI on every push and pull request.

## Checklist when adding a new data source

This is the canonical checklist. Every step is a convention something already depends on, so work
through it rather than inferring the shape from an existing source.

### The collector

1. **`toinflux/newsource.py`** - a class inheriting `DataHandler`, implementing `get_data()`, which
   populates `self.data` and `self.influx_header`. If it is a new device from a manufacturer that
   already has a module (another MyEnergi device, say), add a subclass to the existing file instead of
   a new one. For an MQTT-based source inherit `MqttDataHandler` instead, and add the source's name to
   `MQTT_SOURCES` in `toinflux/general.py` so `--check-config` validates the shared `mqtt:` block for
   it.
2. **`toinflux/general.py`** - register the class in `get_class()`'s factory map. Only collectable
   sources belong there: an abstract parent that is registered will validate, construct, and then die
   with `AttributeError` on every cycle.
3. **`toinflux/__init__.py`** - add the import and re-export.
4. **`example_settings.yaml`** - add a commented-out section showing the required and optional keys.
   Any credential field also needs an entry in `CREDENTIAL_FIELDS` and `PLACEHOLDER_VALUES`
   (`toinflux/credentials.py`); that alone makes `send-to-influx-set-credential <name>` work, because
   the machinery is fully table-driven.
5. **`tests/test_newsource.py`** - unit tests using mocks, no real config or network, reusing the
   fixtures in `tests/conftest.py`.

### MCP metadata

Needed for the read tools, the resources and `suggest_dashboard_panels`, which all derive from it.

6. **`MCP_FIELD_METADATA`** on the class, from the UNITS.md entry: `unit` where the field has one,
   `codes` for any numeric-coded field, a `kind` on **every** entry (`gauge`, `interval`, `counter`
   or `state` - `interval` for a quantity accumulated over its reporting period, which is summed
   rather than averaged),
   and a `description` **only** where the name, unit and coded values do not already say what the
   field is. Add the UNITS.md row in the same change: `tests/test_field_metadata.py` fails on an entry
   with no kind, one saying nothing at all, a description whose every word comes from the field key,
   or a unit or code that disagrees with UNITS.md.
7. **`MCP_DESCRIPTION`** - one line on what the source reports, surfaced by `list_sources`, the
   documentation tool and the per-source resources.
7a. **Only if the source's field keys are not knowable in advance** - because they are the operator's
   own device names, say - override `mcp_field_metadata()` instead of declaring a static table, and
   have the collector record whatever the keys mean as it collects. Hue is the worked example: it
   writes each device's class to a companion measurement and reads it back. Two rules for such an
   override: read InfluxDB, never the device (every schema tool touches InfluxDB and nothing else,
   and a device read answers differently between calls); and never raise, since metadata is an
   annotation and must degrade rather than fail the call.
8. **`MCP_MEASUREMENT` and `MCP_TAG_FILTERS`** - only if the InfluxDB measurement is not the source's
   own name, or it shares a measurement with others.
9. **`MCP_INSTANCE_TAG`** - only if several *producers* write to one measurement and a tag tells them
   apart (Speedtest's collecting `host`). That makes the read tools enumerate it, accept `instance`,
   and report per producer instead of merging. Leave it `None` otherwise, which keeps the flat payload
   shape. A source setting it should also override `heartbeat_tags()`, or its `collector_status` points
   are attributable to no single writer and overwrite each other.
10. **`MCP_LIVE_STATE`** - leave at its `True` default unless `get_data()` is expensive or pointless to
    call live (a full Speedtest run, or Octopus's ~24 h delayed data). Set `False` and current-state
    reads the latest InfluxDB point instead.
11. **`GRAFANA_UNITS`** (`toinflux/mcp_dashboards.py`) - map the unit to a real Grafana identifier,
    read out of a running Grafana rather than guessed. Where Grafana has none, use its custom-suffix
    form (`suffix:W/m²`) rather than leaving the panel unitless: an unrecognised id is *not* dropped,
    it renders as a literal suffix, so a typo appears on the axis rather than vanishing. Prefer
    `suffix:` to a bare string, since a bare one would silently adopt Grafana's own formatter -
    possibly one that rescales - if an identifier of that name were ever added. A test fails if a
    unit is neither mapped nor listed as having no honest label.

### Write support

Only if the source can be *controlled* or *actioned* and its vendor API has a documented write path.
Most sources are read-only and skip this.

12. Set **`MCP_WRITABLE = True`** and implement the vendor write logic as methods on the class, keeping
    the name-to-id, parameter and capability mapping there (see `Hue.mcp_set_device_state` or
    `Speedtest.mcp_trigger_run`).
13. Add a per-source registrar to **`_WRITE_TOOL_REGISTRARS`** in `toinflux/mcp_write.py`. A
    write-enabled source with no registrar is logged and skipped, not silently controllable.
14. Every tool needs a **`title=`** distinct from its own name and an **`annotations=ToolAnnotations(...)`**
    with `read_only_hint` set explicitly, plus `destructive_hint` when `read_only_hint=False`. A
    client's auto-permission logic and a registry review read these fields, never the description.
15. Add **`<source>.mcp_read_write`** (bool, default false) to `example_settings.yaml`. The tools
    appear once the operator opts in.

### The debconf install flow

Mechanical, not a judgment call: every rule below is an existing tested convention.

16. Add the source's name to the `sources-to-configure` multiselect `Choices` in
    `packaging/deb/send-to-influx.templates`, **appending at the end**. The question-visibility
    scenario in `test-packaging.sh` selects sources by position number, so inserting mid-list silently
    retargets those tests.
17. Credential, identity and connection fields get conditional questions (templates plus a
    `case "$SOURCES"` block in `packaging/deb/config`), all at priority `high`. Debconf's default
    threshold is `high`, so anything lower is silently skipped on a normal install. Tuning fields
    (`interval`, `db`, `timeout`, `fields` lists) are **never** prompted for.
18. Secrets are `Type: password` templates. `postinst` migrates them via
    `send-to-influx-set-credential` and clears the stored answer with `db_set ""` immediately after
    `db_get` - never `db_unregister`, which deletes the seen flag and causes blank re-prompts on every
    upgrade. Add every credential-bearing answer to the final unconditional sweep loop too.
19. A *shared* infrastructure block (like `mqtt:`) is asked once, gated on any source needing it being
    selected - not per source, and not unconditionally, which is InfluxDB's special status only. A
    credential already in systemd-creds satisfies a blank secret prompt on reconfigure, and non-secret
    fields provided alongside a blank secret are still applied.
20. If the source introduces a **new settings section**, `postinst` must `--ensure-section` it before
    writing fields or enabling the source. `settings.yaml` is written once at install time and never
    rewritten by an upgrade, so a section added by a later release simply does not exist on existing
    installs: `--set-field` fails, `--enable-source` then writes the source into `sources:` with no
    block behind it, and `load_settings()` raises a fatal `ConfigError` that stops the **whole
    service**.
21. Auto-enable with `--enable-source` only when every required field actually resolved and
    `INFLUX_OK=1`. "Was it ticked" is not enough; otherwise print a specific "not fully configured"
    warning and leave it opt-in.
22. Extend `packaging/deb/test-packaging.sh`: seed the new source's answers in the seeded-install
    scenario (asserting fields land in `settings.yaml`, credentials in the credstore, and the
    plaintext secret is absent from both `settings.yaml` and debconf's own database), and extend the
    question-visibility scenario.

### Documentation

23. Update **README.md** (a short section on the source and any setup steps such as getting an API
    key) and **UNITS.md** (the fields it collects and their units).
24. Update **AGENTS.md** and the relevant file under
    **`architecture/`**. `AGENTS.md` is the only instruction file to update: `CLAUDE.md` and
    `.github/copilot-instructions.md` are pointers to it and carry no rules of their own.

If you are only adding a field or fixing a bug in an *existing* source, steps 2, 3 and 16-22 do not
apply, but the rest still do wherever relevant.

## Local development

On Intel macOS, installing dev dependencies builds `cryptography` (pulled in transitively via
`mcp` -> `pyjwt[crypto]`) from source, since it has shipped no `x86_64`/`universal2` macOS wheel
since 49.0.0 - this needs Rust and OpenSSL 3.x installed first: `brew install rust openssl@3`.
Apple Silicon and Linux are unaffected; this is a permanent upstream change, not something to
work around by pinning `cryptography` here.

```bash
# Setup (creates .venv, installs runtime + dev deps, editable-installs the package itself
# so __version__ resolves to something other than "0.0.0-dev")
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

# Run the app
.venv/bin/python sendtoinflux.py --source hue --print   # print parsed data instead of sending it
.venv/bin/python sendtoinflux.py --check-config          # validate settings.yaml and exit

# Tests
.venv/bin/pytest -v
.venv/bin/pytest -v tests/test_hue.py::TestClass::test_name   # single test

# Lint / format / type-check
.venv/bin/flake8
.venv/bin/black .
.venv/bin/mypy toinflux sendtoinflux.py
```

## Submitting your change

CI (`.github/workflows/premerge.yaml`) runs on every push and pull request, and must pass before
merging:

- `pytest` (with coverage), matrixed across Python 3.10-3.14.
- `flake8` - max line length 120, max complexity 10.
- `mypy` - permissive config (see `pyproject.toml`'s `[tool.mypy]`); doesn't require exhaustive
  type-hint coverage, but shouldn't introduce new errors.

Run all three locally before pushing (see "Local development" above) to avoid CI failures.

Per repo convention, update `README.md`, the relevant file under `architecture/` and `AGENTS.md`
alongside any behaviour change, before committing - see the "Checklist when adding a new data
source" above for the common case, but the same applies to any change to CLI flags, settings
keys, or exit-code/retry behaviour.

Keep PRs focused - one logical change per PR is easier to review than a bundle of unrelated
fixes. For anything beyond a small, self-contained fix, consider opening an issue first (see
"Reporting issues" below) so the approach can be discussed before you invest time in an
implementation.

## Reporting issues

Bug reports and feature requests have templates that you can choose when you
[create an issue](https://github.com/L337-org/send-to-influx/issues/new/choose). Please select
the correct issue type and follow the template. For security issues, see
[SECURITY.md](SECURITY.md) instead of filing a public issue.
