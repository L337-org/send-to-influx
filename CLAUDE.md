# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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
```

CI runs `pytest` and `flake8` in parallel on every push/PR (`.github/workflows/premerge.yaml`, Python 3.10).

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
└── MyEnergi       (toinflux/myenergi.py)     — intermediate parent for MyEnergi API auth
    ├── Zappi      (toinflux/myenergi.py)
    ├── Eddi       (toinflux/myenergi.py)
    └── Harvi      (toinflux/myenergi.py)
```

Each subclass implements `get_data()` which populates `self.data` (dict) and `self.influx_header` (InfluxDB measurement/tag string); `send_data()` in the base class takes it from there. Points are written with an explicit unix-epoch-seconds timestamp: `self.timestamp` if `get_data()` set it (e.g. Octopus uses the reading's own `interval_start` so re-writes of the same reading overwrite rather than duplicate), otherwise the time `send_data()` is called. Field keys are escaped per line protocol rules (commas, `=`, spaces).

### Entry point (`sendtoinflux.py`)

- **Single-source mode** (`--source <name>`): continuous loop, fixed interval per source. Failures are retried with exponential backoff (base 5 s, max 300 s).
- **Multi-source mode** (no `--source`): reads `sources` list from `settings.yaml`, spawns one daemon thread per source with a configurable startup stagger (`stagger_seconds`, default 10). Dead threads are detected and restarted with the same exponential backoff.
- `--dump`: one-time raw JSON to stdout, then exit (single source only).
- `--print`: parsed data to stdout instead of InfluxDB.
- Handles SIGINT and SIGTERM for graceful shutdown.
- On startup, logs an INFO line with the version and the source(s) that will run, so process (re)starts are visible in the logs.

### Factory / settings

- `toinflux/general.py`: `load_settings(file)` (exits with code 1 on missing/invalid YAML), `get_class(source)` (case-insensitive factory → correct DataHandler subclass), `flatten_dict()` (used by Speedtest to flatten nested JSON), `configure_logging(logfile=None)` (sets up timestamped stdout logging, plus optional file handler).
- `configure_logging()` is called in `main()` after settings are loaded. Log messages use the format `YYYY-MM-DD HH:MM:SS LEVEL message`.
- Config file: `settings.yaml` (copy from `example_settings.yaml`). Required at runtime; not committed. Optional `logfile` key adds a file log destination.

### Adding a new data source

1. Create `toinflux/newsource.py` — class inheriting `DataHandler`, implement `get_data()`.
2. Register it in `get_class()` in `toinflux/general.py` and add it to `toinflux/__init__.py`.
3. Add a section to `example_settings.yaml`.
4. Add tests in `tests/test_newsource.py`, reusing fixtures from `tests/conftest.py`.
5. Update README.md, CLAUDE.md, and `.github/copilot-instructions.md`.

### Testing conventions

- Mock `load_settings`, HTTP calls, and file I/O so tests run without real config or network.
- Shared fixtures (e.g. `sample_settings`) live in `tests/conftest.py`.

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Normal exit |
| 1 | Configuration error (missing/invalid `settings.yaml`) |
| 2 | Connection error (API or InfluxDB) |