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

Each subclass implements `get_data()` which populates `self.data` (dict) and `self.influx_header` (InfluxDB measurement/tag string); `send_data()` in the base class takes it from there.

### Entry point (`sendtoinflux.py`)

- **Single-source mode** (`--source <name>`): continuous loop, fixed interval per source. Failures are retried with exponential backoff (base 5 s, max 300 s).
- **Multi-source mode** (no `--source`): reads `sources` list from `settings.yaml`, spawns one daemon thread per source with a configurable startup stagger (`stagger_seconds`, default 10). Dead threads are detected and restarted with the same exponential backoff.
- `--dump`: one-time raw JSON to stdout, then exit (single source only).
- `--print`: parsed data to stdout instead of InfluxDB.
- `--settings <path>`: use a settings file at a path other than `settings.yaml` in the project root (e.g. `/etc/send-to-influx/settings.yaml` for a packaged install). Threaded through `toinflux.get_class()`/`load_settings()`.
- Handles SIGINT and SIGTERM for graceful shutdown.
- On startup, logs an INFO line with the version and the source(s) that will run, so process (re)starts are visible in the logs.

### Factory / settings

- `toinflux/general.py`: `load_settings(settings_file=None)` (exits with code 1 on missing/invalid YAML; `settings_file` defaults to `settings.yaml` in the project root when omitted), `get_class(source, settings_file=None)` (case-insensitive factory → correct DataHandler subclass, threads `settings_file` through to the handler's `load_settings()` call), `flatten_dict()` (used by Speedtest to flatten nested JSON), `configure_logging(logfile=None)` (sets up timestamped stdout logging, plus optional file handler).
- `configure_logging()` is called in `main()` after settings are loaded. Log messages use the format `YYYY-MM-DD HH:MM:SS LEVEL message`.
- Config file: `settings.yaml` (copy from `example_settings.yaml`), or a custom path via `--settings`. Required at runtime; not committed. Optional `logfile` key adds a file log destination. `INFLUX_TOKEN`/`INFLUX_PASSWORD` environment variables override the matching `influx` settings block values, for keeping secrets out of the file on disk.

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

### Packaging (`packaging/`)

- `pyproject.toml` is the single source of truth for the package version (`[project].version`) and runtime dependencies (dynamically read from `requirements.txt`). Bump the version there, not in `sendtoinflux.py`.
- `sendtoinflux.py`'s `__version__` is read from installed package metadata (`importlib.metadata.version("send-to-influx")`), falling back to `"0.0.0-dev"` when run from a source checkout without the package installed. `requirements-dev.txt` includes `-e .` so dev/test environments have it installed and see the real version.
- `packaging/build-deb.sh` builds a `.deb` that bundles the app + dependencies into a venv under `/opt/send-to-influx`, with a systemd unit (`packaging/send-to-influx.service`) and maintainer scripts (`postinst`/`prerm`/`postrm`). Must be built on the target architecture. See the README's "Running as a systemd service" section.
- `.github/workflows/release.yaml`: pushing a `v*` tag runs the test suite, verifies the tag matches `pyproject.toml`'s version, builds the `.deb`, and attaches it to a GitHub Release. A second job publishes it to a flat APT repo on the `gh-pages` branch (served via GitHub Pages) - it prunes to the last `KEEP_LAST_N` (currently 5) `.deb` files, full history stays in Releases.
  - The APT repo job needs a one-time setup and is skipped (not failed) until it exists: generate a GPG key (`gpg --batch --gen-key`), add the private key as the `APT_GPG_PRIVATE_KEY` repo secret (`gpg --export-secret-keys --armor <key-id> | base64`), and the public key ends up published as `send-to-influx.gpg` in the repo automatically on first successful run.