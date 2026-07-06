# Contributing to send-to-influx

Contributions are welcome. By participating, you're expected to uphold the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Before you start

`send-to-influx` collects data from various smart home / energy monitoring devices and APIs and
writes it to InfluxDB using the [line protocol](https://docs.influxdata.com/influxdb/v1/write_protocols/line_protocol_tutorial/),
for visualisation in Grafana. Each data source is a small, mostly-independent Python class; the
practical mental model for finding your way around is a parent/child class hierarchy with one
module per source.

`CLAUDE.md` (repo root) is the full architecture reference - written to brief Claude Code, but
equally the canonical source for humans. This file (`CONTRIBUTING.md`) covers the practical
day-to-day: project layout, the checklist for adding a new source, testing, and submitting
changes. If something isn't covered here, it's almost certainly in `CLAUDE.md`.

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
├── packaging/              # .deb + systemd packaging (see CLAUDE.md's "Packaging" section)
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

1. **`toinflux/newsource.py`** - a class inheriting `DataHandler`, implementing `get_data()`. If
   it's a new device from a manufacturer that already has a module (e.g. another MyEnergi
   device), add a subclass to the existing file instead of a new one.
2. **`toinflux/general.py`** - register the class in `get_class()`'s factory map.
3. **`toinflux/__init__.py`** - add the import/re-export.
4. **`example_settings.yaml`** - add a commented-out section showing the required/optional keys.
5. **`tests/test_newsource.py`** - unit tests using mocks (no real config or network), reusing
   `tests/conftest.py` fixtures.
6. **`README.md`** - a short section describing the source and any setup steps (getting an API
   key, etc.), and **`UNITS.md`** - the fields it collects and their units.
7. **`CLAUDE.md`** and **`.github/copilot-instructions.md`** - update the class hierarchy and any
   other architecture notes that changed.

If you're only adding a field or fixing a bug in an *existing* source rather than adding a new
one, items 2-3 don't apply, but the rest still do wherever relevant.

## Local development

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

Per repo convention, update `README.md`, `CLAUDE.md`, and `.github/copilot-instructions.md`
alongside any behaviour change, before committing - see the "Checklist when adding a new data
source" above for the common case, but the same applies to any change to CLI flags, settings
keys, or exit-code/retry behaviour.

Keep PRs focused - one logical change per PR is easier to review than a bundle of unrelated
fixes. For anything beyond a small, self-contained fix, consider opening an issue first (see
"Reporting issues" below) so the approach can be discussed before you invest time in an
implementation.

## Reporting issues

Bug reports and feature requests have templates that you can choose when you
[create an issue](https://github.com/GavinLucas/send-to-influx/issues/new/choose). Please select
the correct issue type and follow the template. For security issues, see
[SECURITY.md](SECURITY.md) instead of filing a public issue.
