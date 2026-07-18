# Copilot Instructions for send-to-influx

## Project Overview
send-to-influx is a Python application that collects data from various smart home and energy monitoring devices and sends them to InfluxDB for time-series monitoring and visualization. The project is designed with a modular architecture that makes it easy to add new data sources.

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
- **`toinflux/general.py`**: `load_settings(settings_file=None)` (loads YAML configuration and returns a dictionary; raises `ConfigError` on missing/invalid YAML; defaults to `settings.yaml` in the project root, overridable via the `--settings` CLI flag), `get_class(source, settings_file=None)` (case-insensitive factory function to instantiate data source classes dynamically; raises `ConfigError` for an unknown source, including `DataHandler` itself, since it's the abstract base, not a selectable source), `configure_logging(logfile=None, loglevel="INFO", log_max_bytes=..., log_backup_count=...)` (sets up timestamped stdout logging with an optional rotating file handler; raises `ConfigError` - not a raw `OSError` - if `logfile` can't be opened for writing)
- **`toinflux/influx.py`**: `DataHandler` (base class for all data sources). `send_data()` buffers a point in memory (`DataHandler._write_buffers`, a per-source `deque(maxlen=MAX_BUFFERED_POINTS)` of `[line, rejection_count]` entries) instead of dropping it if the InfluxDB write fails, flushing the backlog (oldest first, in newline-batched chunks of `FLUSH_CHUNK_SIZE` per POST) at the start of every buffered `send_data()` call - including empty-data calls, so recovery isn't gated on the next non-empty reading; still raises `InfluxWriteError` either way, so worker backoff/retry is unaffected. The buffer is class-level (not per-instance) because the worker loop discards and reconstructs the `DataHandler` on every failure, is not persisted across a process restart, and never stores duplicate identical lines. `InfluxWriteError.status_code` carries the HTTP status (`None` on connection failure); a point is dropped only after `MAX_POINT_REJECTIONS` separate server rejections (a non-transient 4xx; 408/429 are excluded via `TRANSIENT_CLIENT_ERRORS`) - connection failures, 5xx, and rate-limit/timeout 4xxs never count, so outages and rate-limit bursts can't age points out, and a middlebox transiently answering 4xx can't mass-discard the backlog. A rejected batch falls back to per-point posting to isolate the offender. Heartbeats pass `use_buffer=False` (no flush, no buffering - live signal, no replay value). `validate_settings()` rejects duplicate `sources:` entries (two workers would race on one buffer).
- **`toinflux/exceptions.py`**: `ConfigError` (fatal, not retried) and `SourceConnectionError` (transient, retried with backoff)

#### Current Data Sources
- **`toinflux/philipshue.py`**: Philips Hue Bridge integration
- **`toinflux/myenergi.py`**: MyEnergi Zappi/Eddi/Harvi devices integration (HTTP Digest auth)
- **`toinflux/carbonintensity.py`**: National Grid carbon intensity and generation fuel mix (no API key)
- **`toinflux/openmeteo.py`**: Open-Meteo weather data (no API key, lat/lon configuration)
- **`toinflux/octopus.py`**: Octopus Energy electricity/gas consumption and unit rates (API key auth)
- **`toinflux/nuki.py`**: Nuki smart lock + door sensor state via the local Nuki MQTT API (retained-topic collection through the shared `toinflux/mqtt.py` transport; read-only, never publishes)
- **`toinflux/speedtest.py`**: Speedtest network performance integration; rejects an implausible `ping` (>= 5000 ms - the ceiling imposed by speedtest-cli's own hardcoded 10s per-probe connection timeout, `(3 * 10 / 6) * 1000`) as a connection error instead of writing it

### Configuration (`settings.yaml`)
YAML-based configuration supporting multiple data sources:
- **Orchestration**:
  - `sources`: list of sources to run in parallel when `--source` is omitted
  - `stagger_seconds`: optional start delay between sources (default `10`)
- **Defaults**:
  - `default_source`: used when no `sources` list is configured and `--source` is omitted
- **Logging**:
  - `logfile`: optional path to write logs to a file in addition to stdout (rotated automatically)
  - `log_max_bytes`/`log_backup_count`: optional rotation size (default 10 MiB) and backup count (default 3) for `logfile`
  - `loglevel`: optional log level name (default `INFO`); overridden by the `-v`/`--verbose` CLI flag
- **Hue**: Bridge connection, sensor mappings, temperature units
- **MyEnergi**: API endpoints, authentication, device serials (shared across Zappi/Eddi/Harvi)
- **Zappi/Eddi/Harvi**: Field selection, collection intervals, individual device serials
- **CarbonIntensity**: `include_generation` flag; no credentials required
- **OpenMeteo**: Latitude, longitude, field list (see open-meteo.com/en/docs)
- **Octopus**: API key, MPAN, meter serial; optional `gas_mprn`+`gas_meter_serial` for gas consumption, and optional product/tariff codes for unit rate collection
- **Speedtest**: Field selection, collection intervals
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
5. **Documentation**: Update docstrings and comments

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
- **Collects**: lock state and door-sensor state labels, battery/keypad/door-sensor battery flags, connectivity flags, per provisioned lock
- **Transport**: local MQTT broker (shared `mqtt:` settings block) via `MqttDataHandler` (`toinflux/mqtt.py`); all Nuki state topics are retained, so a short subscribe window per cycle gets the full last-known state
- **Configuration**: `db`, `interval`, `timeout` (collection window); field keys are prefixed with each lock's Nuki-app name
- **Note**: read-only - command/event topics are filtered out and never published to; numeric state codes are resolved to labels from hardcoded spec tables (unrecognised codes pass through as raw numbers)
- **InfluxDB measurement**: `nuki`

## Dependencies

### Core Dependencies
- `requests`: HTTP requests for APIs and InfluxDB
- `urllib3`: HTTP client library; `InsecureRequestWarning` is suppressed only when the relevant `insecure` setting is true - for the Hue bridge request (`toinflux/philipshue.py`, defaults to insecure) and for InfluxDB writes (`toinflux/influx.py`, defaults to secure)
- `pyyaml`: YAML configuration file parsing
- `speedtest-cli`: Speedtest library for collecting network perf data
- `paho-mqtt`: MQTT client for MQTT-based sources (Nuki); imported only in `toinflux/mqtt.py`, v2 callback API

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
- `packaging/deb/build-deb.sh` builds a `.deb` bundling the app and its dependencies into a venv under `/opt/send-to-influx`, with a systemd unit (`packaging/send-to-influx.service`, kept at the top level since it's format-agnostic - the `.deb`-specific files live under `packaging/deb/`) to run it as a service. Package is `Architecture: all` — the venv's `python3` is a symlink to the system-provided `/usr/bin/python3` (`Depends: python3 (>= 3.10), python3 (<< 3.31)`, not bundled), and any optional compiled accelerators pip pulls in (PyYAML, charset-normalizer) are stripped post-install in favour of pure-Python fallbacks. Since everything left is pure Python, the script also symlinks every minor from 3.10 through 3.30's `lib/pythonX.Y` to the one actually populated, so the package works on any target whose `python3` falls in that range (rather than pinning `Depends:` to the build host's exact minor, which broke once a real target's Python drifted from CI's). Verified on real arm64 hardware by the `arm64-verify` CI job (every push/PR, required status check), which also runs the `packaging/deb/test-packaging.sh` scenario suite - install/upgrade/reconfigure/purge lifecycle against the built package; `bookworm-verify` re-runs the suite in a `debian:12` container for systemd-252 (Raspberry Pi OS bookworm) coverage — see the README's "Running as a systemd service" section.

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
default_source: "hue"
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
- **Running tests**: Install dev dependencies (`.venv/bin/pip install -r requirements-dev.txt`) then run `.venv/bin/pytest -v` (or `.venv/bin/python -m pytest -v`). CI runs this (matrixed across Python 3.10-3.14, with coverage), plus `flake8`, `mypy`, `arm64-verify` (builds the `.deb` on a real `ubuntu-24.04-arm` runner and runs the `packaging/deb/test-packaging.sh` scenario suite against it), and `bookworm-verify` (the same suite in a `debian:12`/systemd-252 container), on every push to `main` and every pull request - all are required status checks on `main`'s ruleset. Dependabot keeps pip and GitHub Actions dependencies up to date weekly.
- **Adding tests**: When adding a new data source or changing behaviour, add or update tests in the appropriate `tests/test_*.py` module. Reuse fixtures from `tests/conftest.py` (e.g. `sample_settings`) where applicable.

## Development Workflow
1. **Setup**: Copy `example_settings.yaml` to `settings.yaml` and configure
2. **Development**: Use `--print` mode for testing without affecting InfluxDB
3. **Unit tests**: Run `.venv/bin/pytest -v` and add/update tests for your changes
4. **Linting**: Run `.venv/bin/flake8` to check code style
5. **Formatting**: Run `.venv/bin/black` to format code
6. **Integration**: Test with actual devices and InfluxDB instance
