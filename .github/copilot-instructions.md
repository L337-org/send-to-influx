# Copilot Instructions for send-to-influx

## Project Overview
send-to-influx is a Python application that collects data from various smart home and energy monitoring devices and sends them to InfluxDB for time-series monitoring and visualization. The project is designed with a modular architecture that makes it easy to add new data sources.

## Architecture

### Main Application (`sendtoinflux.py`)
- **Entry Point**: Command-line script with signal handling for graceful shutdown
- **Source Selection**:
  - `--source <name>` runs a single source
  - if `--source` is omitted, starts one worker per entry in `sources` from `settings.yaml`
- **CLI Modes**: 
  - `--dump`: One-time data export (JSON format)
  - `--print`: Continuous monitoring with JSON output to console
  - Normal mode: Continuous data collection and transmission to InfluxDB
- **Timing**:
  - per-source interval-based timing system to avoid drift
  - multi-source startup stagger via optional `stagger_seconds` setting (default `10`)
- **Resilience**:
  - transient failures (`SourceConnectionError`) are retried with exponential backoff (base `5s`, max `300s`) in either single-source or multi-source mode; in multi-source mode, only the failed source is retried, others keep running
  - configuration problems (`ConfigError`) are not retried: single-source mode exits immediately with code `1`; in multi-source mode that source's worker stops permanently (logged as critical) while other sources keep running
- **Signals**: handles both SIGINT (Ctrl-C) and SIGTERM (systemd/container stop) for graceful shutdown
- **Startup logging**: logs an INFO line with the version and the source(s) that will run, so process (re)starts are visible in the logs

### Modular Data Sources (`toinflux/` package)
The project uses a plugin-like architecture where each data source is implemented as a separate module:

#### Base Classes
- **`toinflux/general.py`**: `load_settings()` (loads YAML configuration and returns a dictionary; raises `ConfigError` on missing/invalid YAML), `get_class()` (case-insensitive factory function to instantiate data source classes dynamically; raises `ConfigError` for an unknown source), `configure_logging(logfile=None)` (sets up timestamped stdout logging with optional file handler)
- **`toinflux/influx.py`**: `DataHandler` (base class for all data sources)
- **`toinflux/exceptions.py`**: `ConfigError` (fatal, not retried) and `SourceConnectionError` (transient, retried with backoff)

#### Current Data Sources
- **`toinflux/philipshue.py`**: Philips Hue Bridge integration
- **`toinflux/myenergi.py`**: MyEnergi Zappi/Eddi/Harvi devices integration (HTTP Digest auth)
- **`toinflux/carbonintensity.py`**: National Grid carbon intensity and generation fuel mix (no API key)
- **`toinflux/openmeteo.py`**: Open-Meteo weather data (no API key, lat/lon configuration)
- **`toinflux/octopus.py`**: Octopus Energy electricity/gas consumption and unit rates (API key auth)
- **`toinflux/speedtest.py`**: Speedtest network performance integration

### Configuration (`settings.yaml`)
YAML-based configuration supporting multiple data sources:
- **Orchestration**:
  - `sources`: list of sources to run in parallel when `--source` is omitted
  - `stagger_seconds`: optional start delay between sources (default `10`)
- **Defaults**:
  - `default_source`: used when no `sources` list is configured and `--source` is omitted
- **Logging**:
  - `logfile`: optional path to write logs to a file in addition to stdout
- **Hue**: Bridge connection, sensor mappings, temperature units
- **MyEnergi**: API endpoints, authentication, device serials (shared across Zappi/Eddi/Harvi)
- **Zappi/Eddi/Harvi**: Field selection, collection intervals, individual device serials
- **CarbonIntensity**: `include_generation` flag; no credentials required
- **OpenMeteo**: Latitude, longitude, field list (see open-meteo.com/en/docs)
- **Octopus**: API key, MPAN, meter serial; optional `gas_mprn`+`gas_meter_serial` for gas consumption, and optional product/tariff codes for unit rate collection
- **Speedtest**: Field selection, collection intervals
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
  - `2`: Connection errors (API endpoints, InfluxDB)
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

## Dependencies

### Core Dependencies
- `requests`: HTTP requests for APIs and InfluxDB
- `urllib3`: HTTP client library; `InsecureRequestWarning` is suppressed only for the Hue bridge request (which uses a self-signed cert) in `toinflux/philipshue.py`
- `pyyaml`: YAML configuration file parsing
- `speedtest-cli`: Speedtest library for collecting network perf data

### Development Dependencies
- `black`: Code formatting
- `flake8`: Linting with bugbear and black plugins
- `flake8-bugbear`: Additional linting rules
- `flake8-black`: Black integration for flake8
- `pytest`: Unit test framework

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

# Available sources: hue, zappi, speedtest (and any other implemented sources)
# Multi-source mode uses the settings.yaml `sources` list.
```

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

## Data Format
- **InfluxDB Line Protocol**: `measurement,tag=value field=value timestamp`
- **Timestamp Precision**: Seconds
- **Data Types**: Numeric values (integers, floats) for time-series data
- **Field Names**: Sanitized device names (spaces replaced with underscores)

## Performance Considerations
- **Timeouts**: Appropriate timeouts for all network operations (default: 5 seconds)
- **Intervals**: Configurable collection intervals per data source
- **Memory**: Efficient data structures for processing
- **Rate Limiting**: Consider API rate limits when setting intervals
- **Error Recovery**: Graceful handling of temporary network issues

## Security Notes
- **Credentials**: Store sensitive data in `settings.yaml` with appropriate file permissions
- **HTTPS**: Use HTTPS for all API connections in production
- **Validation**: Validate all input data before processing
- **Logging**: Avoid logging sensitive information
- **Environment Variables**: Consider using environment variables for sensitive data

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
- **Running tests**: Install dev dependencies (`.venv/bin/pip install -r requirements-dev.txt`) then run `.venv/bin/pytest -v` (or `.venv/bin/python -m pytest -v`). CI runs this on every push and pull request.
- **Adding tests**: When adding a new data source or changing behaviour, add or update tests in the appropriate `tests/test_*.py` module. Reuse fixtures from `tests/conftest.py` (e.g. `sample_settings`) where applicable.

## Development Workflow
1. **Setup**: Copy `example_settings.yaml` to `settings.yaml` and configure
2. **Development**: Use `--print` mode for testing without affecting InfluxDB
3. **Unit tests**: Run `.venv/bin/pytest -v` and add/update tests for your changes
4. **Linting**: Run `.venv/bin/flake8` to check code style
5. **Formatting**: Run `.venv/bin/black` to format code
6. **Integration**: Test with actual devices and InfluxDB instance
